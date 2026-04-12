import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
from aiohttp import web
import asyncio
import random
from datetime import datetime, timedelta

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
VERIFIED_ROLE_NAME = "Verified"
HOST_ROLE_NAME = "Heist Host"
VERIFY_LOG_CHANNEL = "verify-log"
QUEUE_CHANNEL = "heist-queue"
WELCOME_CHANNEL = "welcome"
MAX_QUEUE_SIZE = 3
DATA_FILE = "bot_data.json"
PORT = int(os.environ.get("PORT", 8080))
AUTO_ROLE_NAME = "Member"
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=["!", "?"], intents=intents)

# ── Web server ────────────────────────────────

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Web server running on port {PORT}")

# ── Data helpers ──────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"verified": {}, "queue": [], "session_active": False, "afk": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── Social Club checker ───────────────────────

async def check_social_club(username: str) -> bool:
    url = f"https://socialclub.rockstargames.com/member/{username}/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
    except Exception:
        return False

# ── Helper: resolve member ────────────────────

async def resolve_member(ctx, target: str):
    if ctx.message.mentions:
        return ctx.message.mentions[0]
    try:
        uid = int(target.strip())
        member = ctx.guild.get_member(uid)
        if member:
            return member
        return await ctx.guild.fetch_member(uid)
    except Exception:
        return None

def is_host_or_admin(ctx):
    return (
        discord.utils.get(ctx.author.roles, name=HOST_ROLE_NAME) is not None
        or ctx.author.guild_permissions.administrator
    )

# ── Bot ready ─────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Sync error: {e}")

# ══════════════════════════════════════════════
#  AUTO ROLE + WELCOME
# ══════════════════════════════════════════════

@bot.event
async def on_member_join(member: discord.Member):
    role = discord.utils.get(member.guild.roles, name=AUTO_ROLE_NAME)
    if role:
        try:
            await member.add_roles(role)
        except Exception:
            pass
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if channel:
        embed = discord.Embed(
            title=f"👋 {member.guild.name}'e Hoşgeldin!",
            description=f"Hey {member.mention}, sunucuya hoşgeldin!\nHeist queue'larına katılmak için `/verify <social_club_adın>` ile doğrula.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Üye #{member.guild.member_count}")
        await channel.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if channel:
        embed = discord.Embed(
            description=f"👋 **{member}** sunucudan ayrıldı.",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        await channel.send(embed=embed)

# ══════════════════════════════════════════════
#  AFK SYSTEM
# ══════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    data = load_data()
    user_id = str(message.author.id)
    if user_id in data.get("afk", {}):
        del data["afk"][user_id]
        save_data(data)
        try:
            await message.author.edit(nick=message.author.display_name.replace("[AFK] ", ""))
        except Exception:
            pass
        msg = await message.channel.send(f"✅ Tekrar hoşgeldin {message.author.mention}, AFK durumun kaldırıldı!")
        await asyncio.sleep(5)
        await msg.delete()
    for mentioned in message.mentions:
        mid = str(mentioned.id)
        if mid in data.get("afk", {}):
            reason = data["afk"][mid]["reason"]
            since = data["afk"][mid]["since"]
            await message.channel.send(f"💤 **{mentioned.display_name}** şu an AFK: `{reason}` (<t:{since}:R>'den beri)")
    await bot.process_commands(message)

@bot.command(name="afk")
async def afk(ctx, *, reason: str = "AFK"):
    data = load_data()
    user_id = str(ctx.author.id)
    data.setdefault("afk", {})
    data["afk"][user_id] = {"reason": reason, "since": int(datetime.utcnow().timestamp())}
    save_data(data)
    try:
        await ctx.author.edit(nick=f"[AFK] {ctx.author.display_name}"[:32])
    except Exception:
        pass
    await ctx.send(f"💤 {ctx.author.mention} AFK oldu: `{reason}`")

# ══════════════════════════════════════════════
#  MODERATION — TR + EN
# ══════════════════════════════════════════════

# ── BAN / YASAKLA ─────────────────────────────

async def do_ban(ctx, target, reason):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    try:
        await member.send(
            f"🔨 **{ctx.guild.name}** sunucusundan **yasaklandın**.\n"
            f"Sebep: `{reason}`\nYaklaşan: {ctx.author}"
        )
    except Exception:
        pass
    try:
        await member.ban(reason=f"{reason} | {ctx.author} tarafından")
        await ctx.send(f"🔨 **{member}** yasaklandı. Sebep: `{reason}`")
    except discord.Forbidden:
        await ctx.send("❌ Bu üyeyi yasaklama iznim yok.")

@bot.command(name="ban")
async def ban(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!ban @kullanıcı/ID [sebep]`"); return
    await do_ban(ctx, target, reason)

@bot.command(name="yasakla")
async def yasakla(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!yasakla @kullanıcı/ID [sebep]`"); return
    await do_ban(ctx, target, reason)

# ── UNBAN / YASAKKALDIR ───────────────────────

async def do_unban(ctx, user_id, reason):
    try:
        uid = int(user_id.strip())
        user = await bot.fetch_user(uid)
        await ctx.guild.unban(user, reason=f"{reason} | {ctx.author} tarafından")
        await ctx.send(f"✅ **{user}** yasağı kaldırıldı.")
        try:
            await user.send(f"✅ **{ctx.guild.name}** sunucusundaki yasağın kaldırıldı.")
        except Exception:
            pass
    except Exception:
        await ctx.send("❌ Geçerli bir kullanıcı ID'si gir.")

@bot.command(name="unban")
async def unban(ctx, user_id: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not user_id: await ctx.send("❌ Kullanım: `!unban ID [sebep]`"); return
    await do_unban(ctx, user_id, reason)

@bot.command(name="yasakkaldır")
async def yasakkaldır(ctx, user_id: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not user_id: await ctx.send("❌ Kullanım: `!yasakkaldır ID [sebep]`"); return
    await do_unban(ctx, user_id, reason)

# ── KICK / AT ─────────────────────────────────

async def do_kick(ctx, target, reason):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    try:
        await member.send(
            f"🦵 **{ctx.guild.name}** sunucusundan **atıldın**.\n"
            f"Sebep: `{reason}`\nAtan: {ctx.author}"
        )
    except Exception:
        pass
    try:
        await member.kick(reason=f"{reason} | {ctx.author} tarafından")
        await ctx.send(f"🦵 **{member}** atıldı. Sebep: `{reason}`")
    except discord.Forbidden:
        await ctx.send("❌ Bu üyeyi atma iznim yok.")

@bot.command(name="kick")
async def kick(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!kick @kullanıcı/ID [sebep]`"); return
    await do_kick(ctx, target, reason)

@bot.command(name="at")
async def at(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!at @kullanıcı/ID [sebep]`"); return
    await do_kick(ctx, target, reason)

# ── MUTE / SUSTUR ─────────────────────────────

async def do_mute(ctx, target, reason):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role:
        muted_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, speak=False)
    if muted_role in member.roles:
        await ctx.send(f"⚠️ **{member}** zaten susturulmuş.")
        return
    await member.add_roles(muted_role, reason=f"{reason} | {ctx.author} tarafından")
    try:
        await member.send(f"🔇 **{ctx.guild.name}** sunucusunda **susturuldun**.\nSebep: `{reason}`")
    except Exception:
        pass
    await ctx.send(f"🔇 **{member}** susturuldu. Sebep: `{reason}`")

@bot.command(name="mute")
async def mute(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!mute @kullanıcı/ID [sebep]`"); return
    await do_mute(ctx, target, reason)

@bot.command(name="sustur")
async def sustur(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!sustur @kullanıcı/ID [sebep]`"); return
    await do_mute(ctx, target, reason)

# ── UNMUTE / SUSTURMAKALDIR ───────────────────

async def do_unmute(ctx, target):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role or muted_role not in member.roles:
        await ctx.send(f"⚠️ **{member}** susturulmuş değil.")
        return
    await member.remove_roles(muted_role)
    try:
        await member.send(f"🔊 **{ctx.guild.name}** sunucusundaki susturman kaldırıldı.")
    except Exception:
        pass
    await ctx.send(f"🔊 **{member}** susturması kaldırıldı.")

@bot.command(name="unmute")
async def unmute(ctx, target: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!unmute @kullanıcı/ID`"); return
    await do_unmute(ctx, target)

@bot.command(name="susturmakaldır")
async def susturmakaldır(ctx, target: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!susturmakaldır @kullanıcı/ID`"); return
    await do_unmute(ctx, target)

# ── TIMEOUT / ZAMAN AŞIMI ─────────────────────

async def do_timeout(ctx, target, duration, reason):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    try:
        mins = int(duration)
        until = datetime.utcnow() + timedelta(minutes=mins)
        await member.timeout(until, reason=f"{reason} | {ctx.author} tarafından")
        try:
            await member.send(
                f"⏱️ **{ctx.guild.name}** sunucusunda **{mins} dakika** zaman aşımına uğradın.\n"
                f"Sebep: `{reason}`"
            )
        except Exception:
            pass
        await ctx.send(f"⏱️ **{member}** {mins} dakika zaman aşımına uğratıldı. Sebep: `{reason}`")
    except ValueError:
        await ctx.send("❌ Süre sayı olmalı (dakika cinsinden).")
    except discord.Forbidden:
        await ctx.send("❌ Bu üyeye zaman aşımı uygulama iznim yok.")

@bot.command(name="timeout")
async def timeout_cmd(ctx, target: str = None, duration: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target or not duration: await ctx.send("❌ Kullanım: `!timeout @kullanıcı/ID <dakika> [sebep]`"); return
    await do_timeout(ctx, target, duration, reason)

@bot.command(name="zamanasimi")
async def zamanasimi(ctx, target: str = None, duration: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target or not duration: await ctx.send("❌ Kullanım: `!zamanasimi @kullanıcı/ID <dakika> [sebep]`"); return
    await do_timeout(ctx, target, duration, reason)

# ── UNTIMEOUT / ZAMANASIMIKALDIR ──────────────

async def do_untimeout(ctx, target):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    await member.timeout(None)
    try:
        await member.send(f"✅ **{ctx.guild.name}** sunucusundaki zaman aşımın kaldırıldı.")
    except Exception:
        pass
    await ctx.send(f"✅ **{member}**'ın zaman aşımı kaldırıldı.")

@bot.command(name="untimeout")
async def untimeout_cmd(ctx, target: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!untimeout @kullanıcı/ID`"); return
    await do_untimeout(ctx, target)

@bot.command(name="zamanasimikaldır")
async def zamanasimikaldır(ctx, target: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!zamanasimikaldır @kullanıcı/ID`"); return
    await do_untimeout(ctx, target)

# ── WARN / UYAR ───────────────────────────────

async def do_warn(ctx, target, reason):
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Üye bulunamadı.")
        return
    try:
        await member.send(f"⚠️ **{ctx.guild.name}** sunucusunda **uyarıldın**.\nSebep: `{reason}`")
    except Exception:
        pass
    await ctx.send(f"⚠️ **{member}** uyarıldı. Sebep: `{reason}`")

@bot.command(name="warn")
async def warn(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!warn @kullanıcı/ID [sebep]`"); return
    await do_warn(ctx, target, reason)

@bot.command(name="uyar")
async def uyar(ctx, target: str = None, *, reason: str = "Sebep belirtilmedi"):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    if not target: await ctx.send("❌ Kullanım: `!uyar @kullanıcı/ID [sebep]`"); return
    await do_warn(ctx, target, reason)

# ── CLEAR / TEMİZLE ───────────────────────────

async def do_clear(ctx, amount):
    if not amount or not amount.isdigit():
        await ctx.send("❌ Kullanım: `!temizle 10`")
        return
    count = min(int(amount), 100)
    deleted = await ctx.channel.purge(limit=count + 1)
    msg = await ctx.send(f"🧹 {len(deleted) - 1} mesaj silindi.")
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name="clear")
async def clear_messages(ctx, amount: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    await do_clear(ctx, amount)

@bot.command(name="temizle")
async def temizle(ctx, amount: str = None):
    if not is_host_or_admin(ctx): await ctx.send("❌ Yetkin yok."); return
    await do_clear(ctx, amount)

# ── MODHELP / YARDIM ──────────────────────────

@bot.command(name="modhelp")
async def modhelp(ctx):
    embed = discord.Embed(title="🛡️ Moderasyon Komutları", color=discord.Color.blue())
    embed.add_field(name="!ban / !yasakla @kullanıcı/ID [sebep]", value="Üyeyi yasakla (DM gönderir)", inline=False)
    embed.add_field(name="!unban / !yasakkaldır ID [sebep]", value="Yasağı kaldır", inline=False)
    embed.add_field(name="!kick / !at @kullanıcı/ID [sebep]", value="Üyeyi at (DM gönderir)", inline=False)
    embed.add_field(name="!mute / !sustur @kullanıcı/ID [sebep]", value="Üyeyi sustur (DM gönderir)", inline=False)
    embed.add_field(name="!unmute / !susturmakaldır @kullanıcı/ID", value="Susturmayı kaldır", inline=False)
    embed.add_field(name="!timeout / !zamanasimi @kullanıcı/ID <dakika> [sebep]", value="Zaman aşımı (DM gönderir)", inline=False)
    embed.add_field(name="!untimeout / !zamanasimikaldır @kullanıcı/ID", value="Zaman aşımını kaldır", inline=False)
    embed.add_field(name="!warn / !uyar @kullanıcı/ID [sebep]", value="Uyar (DM gönderir)", inline=False)
    embed.add_field(name="!clear / !temizle [miktar]", value="Mesaj sil (max 100)", inline=False)
    embed.set_footer(text="Tüm komutlar ! ve ? ile çalışır")
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  POLL / ANKET
# ══════════════════════════════════════════════

@bot.command(name="poll")
async def poll(ctx, *, question: str = None):
    if not question:
        await ctx.send("❌ Kullanım: `!poll Soru | Seçenek1 | Seçenek2`")
        return
    parts = [p.strip() for p in question.split("|")]
    if len(parts) < 2:
        await ctx.send("❌ En az bir seçenek gir. `|` ile ayır.")
        return
    options = parts[1:]
    if len(options) > 10:
        await ctx.send("❌ Max 10 seçenek.")
        return
    number_emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    embed = discord.Embed(title=f"📊 {parts[0]}", color=discord.Color.blurple(), timestamp=datetime.utcnow())
    embed.description = "\n".join([f"{number_emojis[i]} {opt}" for i, opt in enumerate(options)])
    embed.set_footer(text=f"Anket: {ctx.author.display_name}")
    poll_msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await poll_msg.add_reaction(number_emojis[i])
    await ctx.message.delete()

@bot.command(name="anket")
async def anket(ctx, *, question: str = None):
    ctx.command = bot.get_command("poll")
    await poll(ctx, question=question)

# ══════════════════════════════════════════════
#  GIVEAWAY / ÇEKİLİŞ
# ══════════════════════════════════════════════

async def do_giveaway(ctx, duration, winners, prize):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ Sadece hostlar çekiliş başlatabilir.")
        return
    if not duration or not winners or not prize:
        await ctx.send("❌ Kullanım: `!çekiliş <dakika> <kazanan_sayısı> <ödül>`\nÖrnek: `!çekiliş 60 1 GTA Online Para`")
        return
    try:
        mins = int(duration)
        win_count = int(winners)
    except ValueError:
        await ctx.send("❌ Dakika ve kazanan sayısı rakam olmalı.")
        return
    end_time = datetime.utcnow() + timedelta(minutes=mins)
    embed = discord.Embed(
        title="🎉 ÇEKİLİŞ / GIVEAWAY 🎉",
        description=f"**Ödül / Prize:** {prize}\n\n🎉 ile katıl / React with 🎉 to enter!\n\n**Kazanan / Winners:** {win_count}\n**Bitiş / Ends:** <t:{int(end_time.timestamp())}:R>",
        color=discord.Color.gold(),
        timestamp=end_time
    )
    embed.set_footer(text=f"Bitiş • {ctx.author.display_name} tarafından düzenlendi")
    giveaway_msg = await ctx.send(embed=embed)
    await giveaway_msg.add_reaction("🎉")
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await asyncio.sleep(mins * 60)
    giveaway_msg = await ctx.channel.fetch_message(giveaway_msg.id)
    reaction = discord.utils.get(giveaway_msg.reactions, emoji="🎉")
    if not reaction:
        await ctx.send("❌ Kimse katılmadı.")
        return
    users = [u async for u in reaction.users() if not u.bot]
    if not users:
        await ctx.send("❌ Geçerli katılımcı yok.")
        return
    actual_winners = random.sample(users, min(win_count, len(users)))
    winner_mentions = ", ".join(w.mention for w in actual_winners)
    embed.description = f"**Ödül / Prize:** {prize}\n\n🏆 **Kazanan(lar) / Winner(s):** {winner_mentions}"
    embed.color = discord.Color.green()
    await giveaway_msg.edit(embed=embed)
    await ctx.send(f"🎉 Tebrikler / Congratulations {winner_mentions}! **{prize}** kazandın!")

@bot.command(name="giveaway")
async def giveaway(ctx, duration: str = None, winners: str = None, *, prize: str = None):
    await do_giveaway(ctx, duration, winners, prize)

@bot.command(name="çekiliş")
async def cekilis(ctx, duration: str = None, winners: str = None, *, prize: str = None):
    await do_giveaway(ctx, duration, winners, prize)

# ══════════════════════════════════════════════
#  PURGE — TÜM KANAL SİL
# ══════════════════════════════════════════════

@bot.tree.command(name="purge", description="Kanaldaki tüm mesajları siler / Deletes all messages in the channel")
async def purge(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator and not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME):
        await interaction.response.send_message("❌ Bu komutu kullanmak için yetkin yok.", ephemeral=True)
        return
    await interaction.response.send_message("🗑️ Tüm mesajlar siliniyor...", ephemeral=True)
    deleted = 0
    while True:
        msgs = [m async for m in interaction.channel.history(limit=100)]
        if not msgs:
            break
        await interaction.channel.delete_messages(msgs)
        deleted += len(msgs)
        if len(msgs) < 100:
            break
        await asyncio.sleep(1)
    confirm = await interaction.channel.send(f"✅ {deleted} mesaj silindi.")
    await asyncio.sleep(5)
    await confirm.delete()

# ══════════════════════════════════════════════
#  VERIFICATION
# ══════════════════════════════════════════════

@bot.tree.command(name="verify", description="Verify yourself with your Social Club name")
@app_commands.describe(social_club_name="Your Rockstar Social Club username")
async def verify(interaction: discord.Interaction, social_club_name: str):
    data = load_data()
    user_id = str(interaction.user.id)
    if user_id in data["verified"]:
        sc = data["verified"][user_id]["social_club"]
        await interaction.response.send_message(f"✅ Zaten **{sc}** olarak doğrulandın.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    exists = await check_social_club(social_club_name)
    if not exists:
        await interaction.followup.send(
            f"❌ **{social_club_name}** adında bir Social Club profili bulunamadı.\n"
            f"Kullanıcı adını doğru yazdığından emin ol (büyük/küçük harf önemli).\n"
            f"Kontrol et: https://socialclub.rockstargames.com/member/{social_club_name}/",
            ephemeral=True
        )
        return
    guild = interaction.guild
    member = guild.get_member(interaction.user.id)
    data["verified"][user_id] = {
        "social_club": social_club_name,
        "discord_tag": str(interaction.user),
        "verified_at": datetime.utcnow().isoformat(),
        "method": "auto"
    }
    save_data(data)
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if role:
        await member.add_roles(role)
    await interaction.followup.send(
        f"✅ Doğrulandın! Hoşgeldin **{social_club_name}**!\nHeist'e katılmak için kuyruktaki butona bas.",
        ephemeral=True
    )
    log_channel = discord.utils.get(guild.text_channels, name=VERIFY_LOG_CHANNEL)
    if log_channel:
        embed = discord.Embed(title="✅ Otomatik Doğrulama", color=discord.Color.green(), timestamp=datetime.utcnow())
        embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="Social Club", value=f"[{social_club_name}](https://socialclub.rockstargames.com/member/{social_club_name}/)", inline=True)
        embed.set_footer(text=f"User ID: {interaction.user.id}")
        await log_channel.send(embed=embed)

@bot.tree.command(name="forceverify", description="[Host] Manually verify a member")
@app_commands.describe(member="Discord member", social_club_name="Their Social Club name")
async def forceverify(interaction: discord.Interaction, member: discord.Member, social_club_name: str):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Yetkin yok.", ephemeral=True)
        return
    data = load_data()
    data["verified"][str(member.id)] = {
        "social_club": social_club_name,
        "discord_tag": str(member),
        "verified_at": datetime.utcnow().isoformat(),
        "verified_by": str(interaction.user),
        "method": "manual"
    }
    save_data(data)
    role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
    if role:
        await member.add_roles(role)
    await interaction.response.send_message(f"✅ {member.mention} **{social_club_name}** olarak doğrulandı.", ephemeral=True)

@bot.tree.command(name="unverify", description="[Host] Remove a member's verification")
@app_commands.describe(member="Discord member to unverify")
async def unverify(interaction: discord.Interaction, member: discord.Member):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Yetkin yok.", ephemeral=True)
        return
    data = load_data()
    user_id = str(member.id)
    if user_id not in data["verified"]:
        await interaction.response.send_message(f"⚠️ {member.mention} doğrulanmamış.", ephemeral=True)
        return
    del data["verified"][user_id]
    save_data(data)
    role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
    if role and role in member.roles:
        await member.remove_roles(role)
    await interaction.response.send_message(f"🗑️ {member.mention} doğrulaması kaldırıldı.", ephemeral=True)

@bot.tree.command(name="whois", description="Look up a member's Social Club name")
@app_commands.describe(member="The Discord member to look up")
async def whois(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    user_id = str(member.id)
    if user_id in data["verified"]:
        sc = data["verified"][user_id]["social_club"]
        method = data["verified"][user_id].get("method", "unknown")
        await interaction.response.send_message(embed=discord.Embed(
            title="🔎 Üye Sorgula", color=discord.Color.green(),
            description=f"**Discord:** {member.mention}\n**Social Club:** [{sc}](https://socialclub.rockstargames.com/member/{sc}/)\n**Yöntem:** {method}"
        ))
    else:
        await interaction.response.send_message(f"❌ {member.mention} doğrulanmamış.", ephemeral=True)

# ══════════════════════════════════════════════
#  QUEUE WITH BUTTON
# ══════════════════════════════════════════════

def is_verified_user(user_id):
    return str(user_id) in load_data()["verified"]

def get_sc(user_id):
    return load_data()["verified"].get(str(user_id), {}).get("social_club", "Unknown")


class QueueJoinView(discord.ui.View):
    def __init__(self, host_id: int):
        super().__init__(timeout=None)
        self.host_id = host_id
        self.message_ref = None

    async def update_embed(self, data):
        if not self.message_ref:
            return
        queue = data["queue"]
        embed = discord.Embed(
            title="🎮 GTA Online Heist Queue",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "Henüz kimse katılmadı."
        embed.add_field(name="Yer", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
        embed.add_field(name="Host", value=f"<@{self.host_id}>", inline=True)
        embed.set_footer(text="Katılmak için butona bas!")
        try:
            await self.message_ref.edit(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Kuyruğa Katıl / Join Queue", style=discord.ButtonStyle.success, emoji="🎮")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if not is_verified_user(user_id):
            await interaction.response.send_message("❌ Önce `/verify <social_club_adın>` ile doğrula.", ephemeral=True)
            return
        data = load_data()
        if any(e["user_id"] == user_id for e in data["queue"]):
            await interaction.response.send_message("⚠️ Zaten kuyruktasın!", ephemeral=True)
            return
        if len(data["queue"]) >= MAX_QUEUE_SIZE:
            await interaction.response.send_message(f"🚫 Kuyruk dolu ({MAX_QUEUE_SIZE}/{MAX_QUEUE_SIZE})!", ephemeral=True)
            return
        sc = get_sc(user_id)
        data["queue"].append({"user_id": user_id, "social_club": sc, "joined_at": datetime.utcnow().isoformat()})
        save_data(data)
        host = interaction.guild.get_member(self.host_id)
        if host:
            try:
                await host.send(
                    f"🎮 **{interaction.user}** kuyruğa katıldı!\n"
                    f"Social Club: **{sc}**\n"
                    f"Kuyruk: {len(data['queue'])}/{MAX_QUEUE_SIZE}"
                )
            except Exception:
                pass
        await interaction.response.send_message(f"✅ **{sc}** olarak kuyruğa katıldın! Sıra: **{len(data['queue'])}**", ephemeral=True)
        await self.update_embed(data)

    @discord.ui.button(label="Kuyruktan Çık / Leave Queue", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        data = load_data()
        before = len(data["queue"])
        data["queue"] = [e for e in data["queue"] if e["user_id"] != user_id]
        if len(data["queue"]) == before:
            await interaction.response.send_message("⚠️ Kuyruğa girmedin.", ephemeral=True)
            return
        save_data(data)
        await interaction.response.send_message("👋 Kuyruktan çıktın.", ephemeral=True)
        await self.update_embed(data)


queue_group = app_commands.Group(name="queue", description="Heist queue commands")

@queue_group.command(name="open", description="[Host] Katılım butonlu heist kuyruğu aç")
async def queue_open(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Sadece hostlar kuyruk açabilir.", ephemeral=True)
        return
    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)
    channel = discord.utils.get(interaction.guild.text_channels, name=QUEUE_CHANNEL) or interaction.channel
    view = QueueJoinView(host_id=interaction.user.id)
    embed = discord.Embed(
        title="🎮 GTA Online Heist Queue",
        description="Henüz kimse katılmadı.",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Host", value=interaction.user.mention, inline=True)
    embed.add_field(name="Yer", value=f"0/{MAX_QUEUE_SIZE}", inline=True)
    embed.set_footer(text="Katılmak için butona bas!")
    await interaction.response.send_message("✅ Kuyruk açıldı!", ephemeral=True)
    msg = await channel.send(embed=embed, view=view)
    view.message_ref = msg

@queue_group.command(name="start", description="[Host] Heist'i başlat")
async def queue_start(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Sadece hostlar başlatabilir.", ephemeral=True)
        return
    data = load_data()
    if not data["queue"]:
        await interaction.response.send_message("⚠️ Kuyruk boş.", ephemeral=True)
        return
    data["session_active"] = True
    save_data(data)
    mentions = " ".join(f"<@{e['user_id']}>" for e in data["queue"])
    sc_list = "\n".join([f"**{e['social_club']}**" for e in data["queue"]])
    await interaction.response.send_message(
        f"🚀 **Heist başladı!**\nOyuncular: {mentions}\nHost: {interaction.user.mention}\n\n**Social Club isimleri:**\n{sc_list}\n\nLobiye gelin!"
    )

@queue_group.command(name="clear", description="[Host] Kuyruğu temizle")
async def queue_clear(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Sadece hostlar temizleyebilir.", ephemeral=True)
        return
    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)
    await interaction.response.send_message("🧹 Kuyruk temizlendi.")

@queue_group.command(name="view", description="Mevcut kuyruğu görüntüle")
async def queue_view(interaction: discord.Interaction):
    data = load_data()
    queue = data["queue"]
    embed = discord.Embed(title="🎮 GTA Online Heist Queue", color=discord.Color.blue(), timestamp=datetime.utcnow())
    embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "Kuyruk boş!"
    embed.add_field(name="Yer", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.add_field(name="Durum", value="🟢 Aktif" if data["session_active"] else "🔴 Bekleniyor", inline=True)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="kick", description="[Host] Oyuncuyu kuyruktan çıkar")
@app_commands.describe(member="Çıkarılacak üye")
async def queue_kick(interaction: discord.Interaction, member: discord.Member):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Sadece hostlar çıkarabilir.", ephemeral=True)
        return
    data = load_data()
    before = len(data["queue"])
    data["queue"] = [e for e in data["queue"] if e["user_id"] != str(member.id)]
    if len(data["queue"]) == before:
        await interaction.response.send_message(f"⚠️ {member.display_name} kuyruğa girmemiş.", ephemeral=True)
        return
    save_data(data)
    await interaction.response.send_message(f"🦵 {member.mention} kuyruktan çıkarıldı.")

# ── RUN ───────────────────────────────────────

async def main():
    await start_web_server()
    bot.tree.add_command(queue_group)
    await bot.start(BOT_TOKEN)

asyncio.run(main())
