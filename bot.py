import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import aiohttp
from aiohttp import web
import asyncio
import random
from datetime import datetime, timedelta
from collections import defaultdict
import re

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
LOG_CHANNEL = "bot-logs"
TICKET_CATEGORY = "Tickets"
LEVEL_UP_CHANNEL = "level-ups"
STARBOARD_CHANNEL = "starboard"
STARBOARD_EMOJI = "⭐"
STARBOARD_THRESHOLD = 3
SUGGESTION_CHANNEL = "suggestions"
MODLOG_CHANNEL = "mod-logs"
BIRTHDAY_CHANNEL = "birthdays"
MODMAIL_CATEGORY = "Modmail"
CURRENCY_NAME = "GTA$"
CURRENCY_EMOJI = "💵"
ANTI_RAID_THRESHOLD = 10
ANTI_RAID_WINDOW = 10
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=["!", "?"], intents=intents, help_command=None)

# ── In-memory caches ─────────────────────────
snipe_cache = {}          # channel_id -> {content, author, time, attachments}
editsnipe_cache = {}      # channel_id -> {before, after, author, time}
spam_tracker = defaultdict(list)   # user_id -> [timestamps]
reminder_tasks = []       # running reminder tasks
invite_cache = {}         # guild_id -> {code: uses}
raid_tracker = defaultdict(list)   # guild_id -> [join_timestamps]
starboard_cache = set()   # message_ids already on starboard
work_cooldowns = {}       # user_id -> timestamp
crime_cooldowns = {}      # user_id -> timestamp
rob_cooldowns = {}        # user_id -> timestamp
daily_cooldowns = {}      # user_id -> date_string
gamble_cooldowns = {}     # user_id -> timestamp

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
    return {
        "verified": {},
        "queue": [],
        "session_active": False,
        "afk": {},
        "xp": {},
        "levels": {},
        "warnings": {},
        "reaction_roles": {},
        "custom_commands": {},
        "sticky_messages": {},
        "notes": {},
        "invites": {},
        "automod": {},
        "cases": {},
        "case_counter": {},
        "temp_bans": [],
        "temp_roles": [],
        "starboard": {},
        "economy": {},
        "suggestions": {},
        "suggestion_counter": {},
        "birthdays": {},
        "auto_responses": {},
        "role_persist": {},
        "reminders": [],
    }

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

# ── XP / Level helpers ───────────────────────

def xp_for_level(level: int) -> int:
    """XP required to reach a given level."""
    return 5 * (level ** 2) + 50 * level + 100

def add_xp(data, user_id: str, amount: int) -> tuple:
    """Add XP and return (new_xp, leveled_up, new_level)."""
    data.setdefault("xp", {})
    data.setdefault("levels", {})
    data["xp"].setdefault(user_id, 0)
    data["levels"].setdefault(user_id, 0)
    data["xp"][user_id] += amount
    current_level = data["levels"][user_id]
    required = xp_for_level(current_level)
    leveled_up = False
    while data["xp"][user_id] >= required:
        data["xp"][user_id] -= required
        data["levels"][user_id] += 1
        current_level = data["levels"][user_id]
        required = xp_for_level(current_level)
        leveled_up = True
    return data["xp"][user_id], leveled_up, data["levels"][user_id]


# ══════════════════════════════════════════════
#  AUTO ROLE + WELCOME
# ══════════════════════════════════════════════

@bot.event
async def on_member_join(member: discord.Member):
    # Auto role
    role = discord.utils.get(member.guild.roles, name=AUTO_ROLE_NAME)
    if role:
        try:
            await member.add_roles(role)
        except Exception:
            pass

    # Welcome message
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if channel:
        embed = discord.Embed(
            title=f"👋 Welcome to {member.guild.name}!",
            description=(
                f"Hey {member.mention}, welcome!\n"
                f"Verify yourself with `/verify <social_club_name>` to join heist queues.\n\n"
                f"📅 Account created: <t:{int(member.created_at.timestamp())}:R>"
            ),
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count}")
        await channel.send(embed=embed)

    # Invite tracking
    try:
        new_invites = await member.guild.invites()
        old_invites = invite_cache.get(member.guild.id, {})
        for inv in new_invites:
            old_uses = old_invites.get(inv.code, 0)
            if inv.uses > old_uses:
                data = load_data()
                data.setdefault("invites", {})
                data["invites"][str(member.id)] = {
                    "invited_by": str(inv.inviter.id) if inv.inviter else "Unknown",
                    "code": inv.code,
                    "joined_at": datetime.utcnow().isoformat()
                }
                save_data(data)
                log_ch = discord.utils.get(member.guild.text_channels, name=LOG_CHANNEL)
                if log_ch:
                    await log_ch.send(
                        f"📨 **{member}** joined using invite `{inv.code}` "
                        f"(by {inv.inviter.mention if inv.inviter else 'Unknown'})"
                    )
                break
        invite_cache[member.guild.id] = {inv.code: inv.uses for inv in new_invites}
    except Exception:
        pass

@bot.event
async def on_member_remove(member: discord.Member):
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if channel:
        embed = discord.Embed(
            description=f"👋 **{member}** has left the server.",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        await channel.send(embed=embed)

# ══════════════════════════════════════════════
#  MESSAGE LOGGING (edit, delete, snipe)
# ══════════════════════════════════════════════

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    snipe_cache[message.channel.id] = {
        "content": message.content,
        "author": str(message.author),
        "author_avatar": message.author.display_avatar.url,
        "time": datetime.utcnow().isoformat(),
        "attachments": [a.url for a in message.attachments]
    }
    log_ch = discord.utils.get(message.guild.text_channels, name=LOG_CHANNEL) if message.guild else None
    if log_ch:
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=message.content[:1024] or "*No text content*",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Author", value=message.author.mention, inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(a.url for a in message.attachments), inline=False)
        await log_ch.send(embed=embed)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or before.content == after.content:
        return
    editsnipe_cache[before.channel.id] = {
        "before": before.content,
        "after": after.content,
        "author": str(before.author),
        "author_avatar": before.author.display_avatar.url,
        "time": datetime.utcnow().isoformat()
    }
    log_ch = discord.utils.get(before.guild.text_channels, name=LOG_CHANNEL) if before.guild else None
    if log_ch:
        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Before", value=before.content[:1024] or "*empty*", inline=False)
        embed.add_field(name="After", value=after.content[:1024] or "*empty*", inline=False)
        embed.add_field(name="Author", value=before.author.mention, inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})", inline=True)
        await log_ch.send(embed=embed)

# ══════════════════════════════════════════════
#  AFK SYSTEM + XP + ANTI-SPAM + AUTO-MOD
# ══════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    data = load_data()
    user_id = str(message.author.id)

    # ── Modmail: forward DMs to staff ──
    if not message.guild:
        for g in bot.guilds:
            member = g.get_member(message.author.id)
            if member:
                category = discord.utils.get(g.categories, name=MODMAIL_CATEGORY)
                if not category:
                    break
                ch_name = f"mail-{message.author.name.lower().replace(' ', '-')}"[:50]
                channel = discord.utils.get(category.text_channels, name=ch_name)
                if not channel:
                    overwrites = {
                        g.default_role: discord.PermissionOverwrite(read_messages=False),
                        g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    }
                    host_role = discord.utils.get(g.roles, name=HOST_ROLE_NAME)
                    if host_role:
                        overwrites[host_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    channel = await g.create_text_channel(name=ch_name, category=category, overwrites=overwrites)
                    intro = discord.Embed(
                        title="📬 New Modmail Thread",
                        description=(
                            f"**From:** {message.author.mention} ({message.author})\n"
                            f"**ID:** {message.author.id}\n\n"
                            f"Use `!reply <message>` to respond.\n"
                            f"Use `!closemail` to close this thread."
                        ),
                        color=discord.Color.blue(), timestamp=datetime.utcnow()
                    )
                    intro.set_thumbnail(url=message.author.display_avatar.url)
                    await channel.send(embed=intro)
                mail_embed = discord.Embed(
                    description=message.content or "*No text*",
                    color=discord.Color.green(), timestamp=datetime.utcnow()
                )
                mail_embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
                if message.attachments:
                    mail_embed.add_field(name="Attachments", value="\n".join(a.url for a in message.attachments))
                await channel.send(embed=mail_embed)
                await message.add_reaction("📬")
                break
        await bot.process_commands(message)
        return

    # ── Auto-Mod checks (guild only, skip admins) ──
    if message.guild and not message.author.guild_permissions.administrator:
        guild_id = str(message.guild.id)
        automod = data.get("automod", {}).get(guild_id, {})

        # Word filter
        if automod.get("word_filter"):
            blocked = automod.get("blocked_words", [])
            msg_lower = message.content.lower()
            for word in blocked:
                if word.lower() in msg_lower:
                    await message.delete()
                    warn_msg = await message.channel.send(
                        f"🚫 {message.author.mention}, that word is not allowed here!"
                    )
                    await asyncio.sleep(5)
                    try: await warn_msg.delete()
                    except: pass
                    return

        # Discord invite filter
        if automod.get("invite_filter"):
            invite_pattern = re.compile(r'(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+', re.I)
            if invite_pattern.search(message.content):
                await message.delete()
                warn_msg = await message.channel.send(
                    f"🚫 {message.author.mention}, Discord invite links are not allowed!"
                )
                await asyncio.sleep(5)
                try: await warn_msg.delete()
                except: pass
                return

        # Link filter
        if automod.get("link_filter"):
            url_pattern = re.compile(r'https?://\S+', re.I)
            if url_pattern.search(message.content):
                await message.delete()
                warn_msg = await message.channel.send(
                    f"🚫 {message.author.mention}, links are not allowed!"
                )
                await asyncio.sleep(5)
                try: await warn_msg.delete()
                except: pass
                return

        # Caps filter (>70% caps in messages longer than 10 chars)
        if automod.get("caps_filter") and len(message.content) > 10:
            alpha_chars = [c for c in message.content if c.isalpha()]
            if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.7:
                await message.delete()
                warn_msg = await message.channel.send(
                    f"🚫 {message.author.mention}, please don't use excessive CAPS!"
                )
                await asyncio.sleep(5)
                try: await warn_msg.delete()
                except: pass
                return

        # Mass mention filter
        if automod.get("mention_filter"):
            max_mentions = automod.get("max_mentions", 5)
            if len(message.mentions) + len(message.role_mentions) > max_mentions:
                await message.delete()
                warn_msg = await message.channel.send(
                    f"🚫 {message.author.mention}, mass mentioning is not allowed!"
                )
                await asyncio.sleep(5)
                try: await warn_msg.delete()
                except: pass
                return

        # Emoji spam filter
        if automod.get("emoji_filter"):
            emoji_pattern = re.compile(r'<a?:\w+:\d+>|[\U00010000-\U0010ffff]', re.U)
            emoji_count = len(emoji_pattern.findall(message.content))
            if emoji_count > automod.get("max_emojis", 10):
                await message.delete()
                warn_msg = await message.channel.send(
                    f"🚫 {message.author.mention}, too many emojis!"
                )
                await asyncio.sleep(5)
                try: await warn_msg.delete()
                except: pass
                return

    # ── Anti-spam check ──
    if message.guild:
        now = datetime.utcnow()
        spam_tracker[user_id] = [
            t for t in spam_tracker[user_id]
            if (now - t).total_seconds() < 5
        ]
        spam_tracker[user_id].append(now)
        if len(spam_tracker[user_id]) >= 5:
            try:
                muted_role = discord.utils.get(message.guild.roles, name="Muted")
                if muted_role and muted_role not in message.author.roles:
                    await message.author.add_roles(muted_role, reason="Auto-mute: spam detected")
                    await message.channel.send(
                        f"🤖 {message.author.mention} has been auto-muted for spamming. "
                        f"A moderator can unmute with `!unmute`."
                    )
                    spam_tracker[user_id] = []
                    await asyncio.sleep(60)
                    try:
                        await message.author.remove_roles(muted_role, reason="Auto-unmute after 60s")
                    except Exception:
                        pass
                    return
            except Exception:
                pass

    # ── AFK removal ──
    if user_id in data.get("afk", {}):
        del data["afk"][user_id]
        save_data(data)
        try:
            if message.author.display_name.startswith("[AFK] "):
                await message.author.edit(nick=message.author.display_name[6:])
        except Exception:
            pass
        msg = await message.channel.send(f"✅ Welcome back {message.author.mention}, AFK removed!")
        await asyncio.sleep(5)
        await msg.delete()

    # ── AFK pings ──
    for mentioned in message.mentions:
        mid = str(mentioned.id)
        if mid in data.get("afk", {}):
            reason = data["afk"][mid]["reason"]
            since = data["afk"][mid]["since"]
            await message.channel.send(f"💤 **{mentioned.display_name}** is AFK: `{reason}` (since <t:{since}:R>)")

    # ── XP gain (cooldown: 1 msg per 60s for XP) ──
    if message.guild and len(message.content) > 3:
        xp_key = f"xp_cooldown_{user_id}"
        last_xp = data.get(xp_key, 0)
        now_ts = int(datetime.utcnow().timestamp())
        if now_ts - last_xp >= 60:
            xp_amount = random.randint(15, 25)
            new_xp, leveled_up, new_level = add_xp(data, user_id, xp_amount)
            data[xp_key] = now_ts
            save_data(data)
            if leveled_up:
                level_ch = discord.utils.get(message.guild.text_channels, name=LEVEL_UP_CHANNEL)
                target_ch = level_ch or message.channel
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=f"{message.author.mention} reached **Level {new_level}**!",
                    color=discord.Color.gold()
                )
                embed.set_thumbnail(url=message.author.display_avatar.url)
                await target_ch.send(embed=embed)
        else:
            save_data(data)

    # ── Custom commands ──
    if message.content.startswith("!") and message.guild:
        cmd_name = message.content.split()[0][1:].lower()
        if cmd_name in data.get("custom_commands", {}):
            response = data["custom_commands"][cmd_name]["response"]
            await message.channel.send(response)

    # ── Auto-responder ──
    if message.guild:
        guild_id = str(message.guild.id)
        responses = data.get("auto_responses", {}).get(guild_id, {})
        for trigger, resp_data in responses.items():
            if trigger.lower() in message.content.lower():
                await message.channel.send(resp_data["response"])
                break

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
    await ctx.send(f"💤 {ctx.author.mention} is now AFK: `{reason}`")

# ══════════════════════════════════════════════
#  MODERATION
# ══════════════════════════════════════════════

@bot.command(name="ban")
async def ban(ctx, target: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to ban members.")
        return
    if not target:
        await ctx.send("❌ Usage: `!ban @user/ID [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        await member.send(
            f"🔨 You have been **banned** from **{ctx.guild.name}**.\n"
            f"Reason: `{reason}`\n"
            f"Banned by: {ctx.author}"
        )
    except Exception:
        pass
    try:
        await member.ban(reason=f"{reason} | Banned by {ctx.author}")
        await ctx.send(f"🔨 **{member}** has been banned. Reason: `{reason}`")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to ban this member.")

@bot.command(name="unban")
async def unban(ctx, user_id: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to unban members.")
        return
    if not user_id:
        await ctx.send("❌ Usage: `!unban USER_ID [reason]`")
        return
    try:
        uid = int(user_id.strip())
        user = await bot.fetch_user(uid)
        await ctx.guild.unban(user, reason=f"{reason} | Unbanned by {ctx.author}")
        await ctx.send(f"✅ **{user}** has been unbanned.")
        try:
            await user.send(f"✅ You have been **unbanned** from **{ctx.guild.name}**.")
        except Exception:
            pass
    except Exception:
        await ctx.send("❌ Could not unban. Make sure you used a valid User ID.")

@bot.command(name="kick")
async def kick(ctx, target: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to kick members.")
        return
    if not target:
        await ctx.send("❌ Usage: `!kick @user/ID [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        await member.send(
            f"🦵 You have been **kicked** from **{ctx.guild.name}**.\n"
            f"Reason: `{reason}`\n"
            f"Kicked by: {ctx.author}"
        )
    except Exception:
        pass
    try:
        await member.kick(reason=f"{reason} | Kicked by {ctx.author}")
        await ctx.send(f"🦵 **{member}** has been kicked. Reason: `{reason}`")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to kick this member.")

@bot.command(name="mute")
async def mute(ctx, target: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to mute members.")
        return
    if not target:
        await ctx.send("❌ Usage: `!mute @user/ID [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role:
        muted_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, speak=False)
    if muted_role in member.roles:
        await ctx.send(f"⚠️ **{member}** is already muted.")
        return
    await member.add_roles(muted_role, reason=f"{reason} | Muted by {ctx.author}")
    try:
        await member.send(f"🔇 You have been **muted** in **{ctx.guild.name}**.\nReason: `{reason}`")
    except Exception:
        pass
    await ctx.send(f"🔇 **{member}** has been muted. Reason: `{reason}`")

@bot.command(name="unmute")
async def unmute(ctx, target: str = None):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to unmute members.")
        return
    if not target:
        await ctx.send("❌ Usage: `!unmute @user/ID`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role or muted_role not in member.roles:
        await ctx.send(f"⚠️ **{member}** is not muted.")
        return
    await member.remove_roles(muted_role)
    try:
        await member.send(f"🔊 You have been **unmuted** in **{ctx.guild.name}**.")
    except Exception:
        pass
    await ctx.send(f"🔊 **{member}** has been unmuted.")

@bot.command(name="timeout")
async def timeout_cmd(ctx, target: str = None, duration: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to timeout members.")
        return
    if not target or not duration:
        await ctx.send("❌ Usage: `!timeout @user/ID <minutes> [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        mins = int(duration)
        until = datetime.utcnow() + timedelta(minutes=mins)
        await member.timeout(until, reason=f"{reason} | Timed out by {ctx.author}")
        try:
            await member.send(
                f"⏱️ You have been **timed out** in **{ctx.guild.name}** for **{mins} minutes**.\n"
                f"Reason: `{reason}`"
            )
        except Exception:
            pass
        await ctx.send(f"⏱️ **{member}** timed out for **{mins} minutes**. Reason: `{reason}`")
    except ValueError:
        await ctx.send("❌ Duration must be a number (minutes).")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to timeout this member.")

@bot.command(name="untimeout")
async def untimeout_cmd(ctx, target: str = None):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target:
        await ctx.send("❌ Usage: `!untimeout @user/ID`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    await member.timeout(None)
    try:
        await member.send(f"✅ Your timeout in **{ctx.guild.name}** has been removed.")
    except Exception:
        pass
    await ctx.send(f"✅ **{member}**'s timeout removed.")

@bot.command(name="warn")
async def warn(ctx, target: str = None, *, reason: str = "No reason provided"):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to warn members.")
        return
    if not target:
        await ctx.send("❌ Usage: `!warn @user/ID [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    # Store warning in data
    data = load_data()
    data.setdefault("warnings", {})
    uid = str(member.id)
    data["warnings"].setdefault(uid, [])
    data["warnings"][uid].append({
        "reason": reason,
        "warned_by": str(ctx.author.id),
        "timestamp": datetime.utcnow().isoformat()
    })
    save_data(data)
    warn_count = len(data["warnings"][uid])
    try:
        await member.send(f"⚠️ You have been **warned** in **{ctx.guild.name}**.\nReason: `{reason}`\nTotal warnings: **{warn_count}**")
    except Exception:
        pass
    await ctx.send(f"⚠️ **{member}** has been warned. Reason: `{reason}` (Total: **{warn_count}**)")

    # Auto-action on warning thresholds
    if warn_count == 3:
        await ctx.send(f"🔔 **{member}** has reached **3 warnings**! Consider muting or timing out.")
    elif warn_count >= 5:
        await ctx.send(f"🚨 **{member}** has **{warn_count} warnings**! Consider a ban.")

@bot.command(name="warnings")
async def warnings_cmd(ctx, target: str = None):
    """View warnings for a user."""
    if not target:
        target = str(ctx.author.id)
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    uid = str(member.id)
    warns = data.get("warnings", {}).get(uid, [])
    if not warns:
        await ctx.send(f"✅ **{member}** has no warnings.")
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.orange())
    for i, w in enumerate(warns[-10:], 1):  # Show last 10
        embed.add_field(
            name=f"#{i} — {w['timestamp'][:10]}",
            value=f"Reason: `{w['reason']}`\nBy: <@{w['warned_by']}>",
            inline=False
        )
    embed.set_footer(text=f"Total warnings: {len(warns)}")
    await ctx.send(embed=embed)

@bot.command(name="clearwarnings")
async def clearwarnings(ctx, target: str = None):
    """Clear all warnings for a user."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target:
        await ctx.send("❌ Usage: `!clearwarnings @user/ID`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    uid = str(member.id)
    data.setdefault("warnings", {})
    data["warnings"][uid] = []
    save_data(data)
    await ctx.send(f"✅ Cleared all warnings for **{member}**.")

@bot.command(name="clear")
async def clear_messages(ctx, amount: str = None):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to clear messages.")
        return
    if not amount or not amount.isdigit():
        await ctx.send("❌ Usage: `!clear 10`")
        return
    count = min(int(amount), 100)
    deleted = await ctx.channel.purge(limit=count + 1)
    msg = await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.")
    await asyncio.sleep(3)
    await msg.delete()

# ══════════════════════════════════════════════
#  SNIPE / EDITSNIPE
# ══════════════════════════════════════════════

@bot.command(name="snipe")
async def snipe(ctx):
    """Show the last deleted message in this channel."""
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        await ctx.send("❌ Nothing to snipe!")
        return
    embed = discord.Embed(
        description=data["content"] or "*No text content*",
        color=discord.Color.red(),
        timestamp=datetime.fromisoformat(data["time"])
    )
    embed.set_author(name=data["author"], icon_url=data.get("author_avatar"))
    if data.get("attachments"):
        embed.add_field(name="Attachments", value="\n".join(data["attachments"]), inline=False)
    embed.set_footer(text="Deleted message")
    await ctx.send(embed=embed)

@bot.command(name="editsnipe")
async def editsnipe(ctx):
    """Show the last edited message in this channel."""
    data = editsnipe_cache.get(ctx.channel.id)
    if not data:
        await ctx.send("❌ Nothing to editsnipe!")
        return
    embed = discord.Embed(
        color=discord.Color.orange(),
        timestamp=datetime.fromisoformat(data["time"])
    )
    embed.set_author(name=data["author"], icon_url=data.get("author_avatar"))
    embed.add_field(name="Before", value=data["before"][:1024] or "*empty*", inline=False)
    embed.add_field(name="After", value=data["after"][:1024] or "*empty*", inline=False)
    embed.set_footer(text="Edited message")
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  SERVER INFO / USER INFO / AVATAR
# ══════════════════════════════════════════════

@bot.command(name="serverinfo", aliases=["si"])
async def serverinfo(ctx):
    """Display server information."""
    guild = ctx.guild
    embed = discord.Embed(title=f"📋 {guild.name}", color=discord.Color.blurple(), timestamp=datetime.utcnow())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="👑 Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="📅 Created", value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="🆔 ID", value=guild.id, inline=True)
    embed.add_field(name="👥 Members", value=f"Total: {guild.member_count}", inline=True)
    embed.add_field(name="💬 Channels", value=f"Text: {len(guild.text_channels)} | Voice: {len(guild.voice_channels)}", inline=True)
    embed.add_field(name="🎭 Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="😀 Emojis", value=f"{len(guild.emojis)}/{guild.emoji_limit}", inline=True)
    embed.add_field(name="🔒 Verification", value=str(guild.verification_level).title(), inline=True)
    embed.add_field(name="🚀 Boost Level", value=f"Level {guild.premium_tier} ({guild.premium_subscription_count} boosts)", inline=True)
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    await ctx.send(embed=embed)

@bot.command(name="userinfo", aliases=["ui", "whoisprefix"])
async def userinfo(ctx, target: str = None):
    """Display user information."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
    embed = discord.Embed(title=f"👤 {member}", color=member.color or discord.Color.blurple(), timestamp=datetime.utcnow())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🆔 ID", value=member.id, inline=True)
    embed.add_field(name="📛 Nickname", value=member.nick or "None", inline=True)
    embed.add_field(name="🤖 Bot", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="📅 Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="📥 Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="🎨 Top Role", value=member.top_role.mention, inline=True)
    embed.add_field(name=f"🎭 Roles [{len(roles)}]", value=" ".join(roles[:10]) if roles else "None", inline=False)

    # XP info
    data = load_data()
    uid = str(member.id)
    level = data.get("levels", {}).get(uid, 0)
    xp = data.get("xp", {}).get(uid, 0)
    embed.add_field(name="📊 Level", value=f"Level {level} ({xp}/{xp_for_level(level)} XP)", inline=True)

    # Warnings count
    warn_count = len(data.get("warnings", {}).get(uid, []))
    embed.add_field(name="⚠️ Warnings", value=str(warn_count), inline=True)

    # Verification status
    if uid in data.get("verified", {}):
        sc = data["verified"][uid]["social_club"]
        embed.add_field(name="✅ Verified", value=f"[{sc}](https://socialclub.rockstargames.com/member/{sc}/)", inline=True)
    else:
        embed.add_field(name="❌ Not Verified", value="Use `/verify`", inline=True)

    await ctx.send(embed=embed)

@bot.command(name="avatar", aliases=["av", "pfp"])
async def avatar(ctx, target: str = None):
    """Show a user's avatar."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    embed = discord.Embed(title=f"🖼️ {member}'s Avatar", color=member.color or discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    embed.add_field(name="Links", value=(
        f"[PNG]({member.display_avatar.with_format('png')}) | "
        f"[JPG]({member.display_avatar.with_format('jpg')}) | "
        f"[WEBP]({member.display_avatar.with_format('webp')})"
    ))
    await ctx.send(embed=embed)

@bot.command(name="banner")
async def banner(ctx, target: str = None):
    """Show a user's banner."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    user = await bot.fetch_user(member.id)
    if not user.banner:
        await ctx.send(f"❌ **{member}** has no banner.")
        return
    embed = discord.Embed(title=f"🖼️ {member}'s Banner", color=member.color or discord.Color.blurple())
    embed.set_image(url=user.banner.url)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  CHANNEL MANAGEMENT
# ══════════════════════════════════════════════

@bot.command(name="slowmode")
async def slowmode(ctx, seconds: str = None):
    """Set channel slowmode. 0 to disable."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if seconds is None or not seconds.isdigit():
        await ctx.send("❌ Usage: `!slowmode <seconds>` (0 to disable)")
        return
    secs = min(int(seconds), 21600)  # Max 6 hours
    await ctx.channel.edit(slowmode_delay=secs)
    if secs == 0:
        await ctx.send("✅ Slowmode disabled.")
    else:
        await ctx.send(f"🐌 Slowmode set to **{secs} seconds**.")

@bot.command(name="lock")
async def lock(ctx, *, reason: str = "No reason provided"):
    """Lock a channel (prevent @everyone from sending messages)."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"This channel has been locked.\nReason: `{reason}`",
        color=discord.Color.red(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Locked by {ctx.author}")
    await ctx.send(embed=embed)

@bot.command(name="unlock")
async def unlock(ctx):
    """Unlock a channel."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description="This channel has been unlocked.",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Unlocked by {ctx.author}")
    await ctx.send(embed=embed)

@bot.command(name="nuke")
async def nuke(ctx):
    """Clone and delete the current channel (reset it)."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can nuke channels.")
        return
    confirm_msg = await ctx.send("⚠️ Are you sure you want to **nuke** this channel? React with ✅ within 10 seconds.")
    await confirm_msg.add_reaction("✅")

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) == "✅" and reaction.message.id == confirm_msg.id

    try:
        await bot.wait_for("reaction_add", timeout=10.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("❌ Nuke cancelled.")
        return

    new_channel = await ctx.channel.clone(reason=f"Nuked by {ctx.author}")
    await ctx.channel.delete()
    embed = discord.Embed(
        title="💥 Channel Nuked",
        description=f"This channel has been nuked by {ctx.author.mention}.",
        color=discord.Color.dark_red()
    )
    await new_channel.send(embed=embed)

# ══════════════════════════════════════════════
#  ANNOUNCE / EMBED BUILDER / SAY
# ══════════════════════════════════════════════

@bot.command(name="announce")
async def announce(ctx, channel: discord.TextChannel = None, *, message: str = None):
    """Send an announcement embed to a channel."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not channel or not message:
        await ctx.send("❌ Usage: `!announce #channel Your message here`")
        return
    embed = discord.Embed(
        title="📢 Announcement",
        description=message,
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Announced by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await channel.send(embed=embed)
    await ctx.send(f"✅ Announcement sent to {channel.mention}.")
    await ctx.message.delete()

@bot.command(name="say")
async def say(ctx, *, message: str = None):
    """Make the bot say something."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not message:
        await ctx.send("❌ Usage: `!say Your message`")
        return
    await ctx.message.delete()
    await ctx.send(message)

@bot.command(name="embed")
async def embed_cmd(ctx, *, text: str = None):
    """Create a custom embed. Format: title | description | color(hex)"""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not text:
        await ctx.send("❌ Usage: `!embed Title | Description | #hex_color`")
        return
    parts = [p.strip() for p in text.split("|")]
    title = parts[0] if len(parts) > 0 else "Embed"
    desc = parts[1] if len(parts) > 1 else ""
    color = discord.Color.blurple()
    if len(parts) > 2:
        try:
            color = discord.Color(int(parts[2].strip().lstrip("#"), 16))
        except Exception:
            pass
    embed = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.utcnow())
    embed.set_footer(text=f"Created by {ctx.author.display_name}")
    await ctx.send(embed=embed)
    await ctx.message.delete()

# ══════════════════════════════════════════════
#  NICKNAME MANAGEMENT
# ══════════════════════════════════════════════

@bot.command(name="nick")
async def nick(ctx, target: str = None, *, new_nick: str = None):
    """Change a member's nickname."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target:
        await ctx.send("❌ Usage: `!nick @user/ID new_nickname` or `!nick @user/ID` to reset")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        await member.edit(nick=new_nick)
        if new_nick:
            await ctx.send(f"✅ {member.mention}'s nickname changed to **{new_nick}**.")
        else:
            await ctx.send(f"✅ {member.mention}'s nickname has been reset.")
    except discord.Forbidden:
        await ctx.send("❌ I can't change this user's nickname.")

# ══════════════════════════════════════════════
#  ROLE MANAGEMENT
# ══════════════════════════════════════════════

@bot.command(name="addrole")
async def addrole(ctx, target: str = None, *, role_name: str = None):
    """Add a role to a member."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target or not role_name:
        await ctx.send("❌ Usage: `!addrole @user/ID RoleName`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    try:
        await member.add_roles(role)
        await ctx.send(f"✅ Added **{role.name}** to {member.mention}.")
    except discord.Forbidden:
        await ctx.send("❌ I can't assign this role.")

@bot.command(name="removerole")
async def removerole(ctx, target: str = None, *, role_name: str = None):
    """Remove a role from a member."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target or not role_name:
        await ctx.send("❌ Usage: `!removerole @user/ID RoleName`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    try:
        await member.remove_roles(role)
        await ctx.send(f"✅ Removed **{role.name}** from {member.mention}.")
    except discord.Forbidden:
        await ctx.send("❌ I can't remove this role.")

@bot.command(name="roleinfo")
async def roleinfo(ctx, *, role_name: str = None):
    """Show information about a role."""
    if not role_name:
        await ctx.send("❌ Usage: `!roleinfo RoleName`")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    members_with_role = len(role.members)
    embed = discord.Embed(title=f"🎭 Role: {role.name}", color=role.color, timestamp=datetime.utcnow())
    embed.add_field(name="🆔 ID", value=role.id, inline=True)
    embed.add_field(name="🎨 Color", value=str(role.color), inline=True)
    embed.add_field(name="📊 Position", value=role.position, inline=True)
    embed.add_field(name="👥 Members", value=members_with_role, inline=True)
    embed.add_field(name="📌 Hoisted", value="Yes" if role.hoist else "No", inline=True)
    embed.add_field(name="🤖 Mentionable", value="Yes" if role.mentionable else "No", inline=True)
    embed.add_field(name="📅 Created", value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  REACTION ROLES
# ══════════════════════════════════════════════

@bot.command(name="reactionrole")
async def reactionrole(ctx, message_id: str = None, emoji: str = None, *, role_name: str = None):
    """Set up a reaction role. Usage: !reactionrole <message_id> <emoji> <role_name>"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can set up reaction roles.")
        return
    if not message_id or not emoji or not role_name:
        await ctx.send("❌ Usage: `!reactionrole <message_id> <emoji> <RoleName>`")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    try:
        msg = await ctx.channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
    except Exception:
        await ctx.send("❌ Could not find message or add reaction.")
        return

    data = load_data()
    data.setdefault("reaction_roles", {})
    data["reaction_roles"][message_id] = data["reaction_roles"].get(message_id, {})
    data["reaction_roles"][message_id][emoji] = {"role_id": role.id, "role_name": role.name}
    save_data(data)
    await ctx.send(f"✅ Reaction role set! React with {emoji} on that message to get **{role.name}**.")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member and payload.member.bot:
        return
    data = load_data()
    msg_id = str(payload.message_id)
    emoji = str(payload.emoji)
    if msg_id in data.get("reaction_roles", {}) and emoji in data["reaction_roles"][msg_id]:
        guild = bot.get_guild(payload.guild_id)
        if guild:
            role_id = data["reaction_roles"][msg_id][emoji]["role_id"]
            role = guild.get_role(role_id)
            if role and payload.member:
                try:
                    await payload.member.add_roles(role)
                except Exception:
                    pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    data = load_data()
    msg_id = str(payload.message_id)
    emoji = str(payload.emoji)
    if msg_id in data.get("reaction_roles", {}) and emoji in data["reaction_roles"][msg_id]:
        guild = bot.get_guild(payload.guild_id)
        if guild:
            role_id = data["reaction_roles"][msg_id][emoji]["role_id"]
            role = guild.get_role(role_id)
            member = guild.get_member(payload.user_id)
            if role and member:
                try:
                    await member.remove_roles(role)
                except Exception:
                    pass

# ══════════════════════════════════════════════
#  REMINDER SYSTEM
# ══════════════════════════════════════════════

@bot.command(name="remind", aliases=["reminder"])
async def remind(ctx, time_str: str = None, *, reminder_text: str = None):
    """Set a reminder. Usage: !remind 30m Do something"""
    if not time_str or not reminder_text:
        await ctx.send("❌ Usage: `!remind <time> <message>`\nExamples: `!remind 30m Check heist`, `!remind 2h Take a break`")
        return

    # Parse time
    match = re.match(r"^(\d+)(s|m|h|d)$", time_str.lower())
    if not match:
        await ctx.send("❌ Invalid time format. Use: `30s`, `5m`, `2h`, `1d`")
        return

    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total_seconds = amount * multipliers[unit]

    if total_seconds > 7 * 86400:  # Max 7 days
        await ctx.send("❌ Maximum reminder time is 7 days.")
        return

    remind_time = datetime.utcnow() + timedelta(seconds=total_seconds)
    await ctx.send(f"⏰ Reminder set! I'll remind you <t:{int(remind_time.timestamp())}:R>: `{reminder_text}`")

    # Store reminder
    data = load_data()
    data.setdefault("reminders", [])
    data["reminders"].append({
        "user_id": str(ctx.author.id),
        "channel_id": ctx.channel.id,
        "text": reminder_text,
        "remind_at": remind_time.isoformat(),
        "created_at": datetime.utcnow().isoformat()
    })
    save_data(data)

@tasks.loop(seconds=30)
async def reminder_check():
    """Background task to check and send reminders."""
    data = load_data()
    reminders = data.get("reminders", [])
    now = datetime.utcnow()
    remaining = []
    for r in reminders:
        remind_at = datetime.fromisoformat(r["remind_at"])
        if now >= remind_at:
            try:
                channel = bot.get_channel(r["channel_id"])
                if channel:
                    await channel.send(
                        f"⏰ <@{r['user_id']}> Reminder: `{r['text']}`"
                    )
            except Exception:
                pass
        else:
            remaining.append(r)
    if len(remaining) != len(reminders):
        data["reminders"] = remaining
        save_data(data)

@reminder_check.before_loop
async def before_reminder_check():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════
#  LEVELING / XP SYSTEM
# ══════════════════════════════════════════════

@bot.command(name="level", aliases=["rank", "xp"])
async def level_cmd(ctx, target: str = None):
    """Check your or someone's level."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    uid = str(member.id)
    level = data.get("levels", {}).get(uid, 0)
    xp = data.get("xp", {}).get(uid, 0)
    required = xp_for_level(level)
    progress = int((xp / required) * 20)
    bar = "█" * progress + "░" * (20 - progress)

    embed = discord.Embed(
        title=f"📊 {member.display_name}'s Level",
        color=member.color or discord.Color.blurple()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"**{level}**", inline=True)
    embed.add_field(name="XP", value=f"**{xp}/{required}**", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}` {int((xp/required)*100)}%", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard(ctx):
    """Show the XP leaderboard."""
    data = load_data()
    levels = data.get("levels", {})
    xp_data = data.get("xp", {})
    if not levels:
        await ctx.send("❌ No one has earned XP yet!")
        return

    # Sort by level then XP
    sorted_users = sorted(
        levels.items(),
        key=lambda x: (x[1], xp_data.get(x[0], 0)),
        reverse=True
    )[:10]

    embed = discord.Embed(title="🏆 XP Leaderboard", color=discord.Color.gold(), timestamp=datetime.utcnow())
    medals = ["🥇", "🥈", "🥉"]
    desc_lines = []
    for i, (uid, level) in enumerate(sorted_users):
        xp = xp_data.get(uid, 0)
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        desc_lines.append(f"{medal} <@{uid}> — Level **{level}** ({xp} XP)")
    embed.description = "\n".join(desc_lines)
    embed.set_footer(text=f"Top {len(sorted_users)} members")
    await ctx.send(embed=embed)

@bot.command(name="setlevel")
async def setlevel(ctx, target: str = None, level: str = None):
    """[Admin] Set a user's level."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can set levels.")
        return
    if not target or not level or not level.isdigit():
        await ctx.send("❌ Usage: `!setlevel @user/ID <level>`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    uid = str(member.id)
    data.setdefault("levels", {})
    data.setdefault("xp", {})
    data["levels"][uid] = int(level)
    data["xp"][uid] = 0
    save_data(data)
    await ctx.send(f"✅ Set **{member}**'s level to **{level}**.")

# ══════════════════════════════════════════════
#  TICKET SYSTEM
# ══════════════════════════════════════════════

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY)

        # Check for existing ticket
        existing = discord.utils.get(guild.text_channels, name=f"ticket-{interaction.user.name.lower()}")
        if existing:
            await interaction.response.send_message(f"❌ You already have an open ticket: {existing.mention}", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        # Give staff/host access
        host_role = discord.utils.get(guild.roles, name=HOST_ROLE_NAME)
        if host_role:
            overwrites[host_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"ticket-{interaction.user.name.lower()}"[:50]
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {interaction.user}"
        )

        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                f"Please describe your issue and a staff member will assist you.\n"
                f"Click **Close Ticket** when your issue is resolved."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Ticket by {interaction.user}")
        await ticket_channel.send(embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"✅ Ticket opened: {ticket_channel.mention}", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


@bot.command(name="ticketpanel")
async def ticketpanel(ctx):
    """[Admin] Create a ticket panel with a button."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can create ticket panels.")
        return
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description="Click the button below to open a support ticket.\nA private channel will be created for you.",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Support System")
    await ctx.send(embed=embed, view=TicketView())
    await ctx.message.delete()

# ══════════════════════════════════════════════
#  CUSTOM COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="addcmd")
async def addcmd(ctx, name: str = None, *, response: str = None):
    """Add a custom text command."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not name or not response:
        await ctx.send("❌ Usage: `!addcmd <name> <response>`")
        return
    name = name.lower()
    data = load_data()
    data.setdefault("custom_commands", {})
    data["custom_commands"][name] = {
        "response": response,
        "created_by": str(ctx.author.id),
        "created_at": datetime.utcnow().isoformat()
    }
    save_data(data)
    await ctx.send(f"✅ Custom command `!{name}` created!")

@bot.command(name="delcmd")
async def delcmd(ctx, name: str = None):
    """Delete a custom command."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not name:
        await ctx.send("❌ Usage: `!delcmd <name>`")
        return
    data = load_data()
    name = name.lower()
    if name not in data.get("custom_commands", {}):
        await ctx.send(f"❌ Command `!{name}` not found.")
        return
    del data["custom_commands"][name]
    save_data(data)
    await ctx.send(f"✅ Custom command `!{name}` deleted.")

@bot.command(name="listcmds")
async def listcmds(ctx):
    """List all custom commands."""
    data = load_data()
    cmds = data.get("custom_commands", {})
    if not cmds:
        await ctx.send("❌ No custom commands set.")
        return
    embed = discord.Embed(title="📜 Custom Commands", color=discord.Color.blurple())
    embed.description = "\n".join([f"• `!{name}` — {info['response'][:50]}" for name, info in cmds.items()])
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  UTILITY COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="ping")
async def ping(ctx):
    """Check bot latency."""
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{latency}ms**",
        color=discord.Color.green() if latency < 200 else discord.Color.red()
    )
    await ctx.send(embed=embed)

@bot.command(name="uptime")
async def uptime(ctx):
    """Show how long the bot has been running."""
    delta = datetime.utcnow() - bot.ready_time if hasattr(bot, 'ready_time') else timedelta(0)
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    await ctx.send(f"⏱️ Uptime: **{hours}h {minutes}m {seconds}s**")

@bot.command(name="invite")
async def invite_cmd(ctx):
    """Get the bot's invite link."""
    perms = discord.Permissions(administrator=True)
    link = discord.utils.oauth_url(bot.user.id, permissions=perms)
    embed = discord.Embed(
        title="🔗 Invite Me!",
        description=f"[Click here to invite]({link})",
        color=discord.Color.blurple()
    )
    await ctx.send(embed=embed)

@bot.command(name="membercount", aliases=["mc"])
async def membercount(ctx):
    """Show the member count breakdown."""
    guild = ctx.guild
    total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots
    online = sum(1 for m in guild.members if m.status != discord.Status.offline)
    embed = discord.Embed(title="👥 Member Count", color=discord.Color.blurple())
    embed.add_field(name="Total", value=total, inline=True)
    embed.add_field(name="Humans", value=humans, inline=True)
    embed.add_field(name="Bots", value=bots, inline=True)
    embed.add_field(name="Online", value=online, inline=True)
    await ctx.send(embed=embed)

@bot.command(name="roll", aliases=["dice"])
async def roll(ctx, sides: str = "6"):
    """Roll a dice."""
    try:
        s = int(sides)
        result = random.randint(1, s)
        await ctx.send(f"🎲 You rolled a **{result}** (1-{s})")
    except ValueError:
        await ctx.send("❌ Usage: `!roll <sides>`")

@bot.command(name="coinflip", aliases=["flip", "coin"])
async def coinflip(ctx):
    """Flip a coin."""
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙"
    await ctx.send(f"{emoji} **{result}!**")

@bot.command(name="choose")
async def choose(ctx, *, options: str = None):
    """Choose between options. Separate with |"""
    if not options:
        await ctx.send("❌ Usage: `!choose option1 | option2 | option3`")
        return
    choices = [c.strip() for c in options.split("|") if c.strip()]
    if len(choices) < 2:
        await ctx.send("❌ Give me at least 2 options separated by `|`")
        return
    choice = random.choice(choices)
    await ctx.send(f"🤔 I choose: **{choice}**")

@bot.command(name="8ball")
async def eightball(ctx, *, question: str = None):
    """Ask the magic 8-ball a question."""
    if not question:
        await ctx.send("❌ Usage: `!8ball <question>`")
        return
    responses = [
        "🎱 It is certain.", "🎱 Without a doubt.", "🎱 Yes, definitely.",
        "🎱 You may rely on it.", "🎱 As I see it, yes.", "🎱 Most likely.",
        "🎱 Outlook good.", "🎱 Yes.", "🎱 Signs point to yes.",
        "🎱 Reply hazy, try again.", "🎱 Ask again later.",
        "🎱 Better not tell you now.", "🎱 Cannot predict now.",
        "🎱 Concentrate and ask again.", "🎱 Don't count on it.",
        "🎱 My reply is no.", "🎱 My sources say no.",
        "🎱 Outlook not so good.", "🎱 Very doubtful."
    ]
    await ctx.send(f"**Q:** {question}\n{random.choice(responses)}")

@bot.command(name="calc")
async def calc(ctx, *, expression: str = None):
    """Simple calculator."""
    if not expression:
        await ctx.send("❌ Usage: `!calc 5 + 3 * 2`")
        return
    # Only allow safe characters
    cleaned = re.sub(r'[^0-9+\-*/().%\s]', '', expression)
    if not cleaned:
        await ctx.send("❌ Invalid expression.")
        return
    try:
        result = eval(cleaned)  # Safe because we sanitize input
        await ctx.send(f"🧮 `{expression}` = **{result}**")
    except Exception:
        await ctx.send("❌ Could not calculate that.")

@bot.command(name="countdown")
async def countdown(ctx, seconds: str = None):
    """Start a countdown."""
    if not seconds or not seconds.isdigit():
        await ctx.send("❌ Usage: `!countdown <seconds>` (max 60)")
        return
    secs = min(int(seconds), 60)
    msg = await ctx.send(f"⏳ Countdown: **{secs}**")
    for i in range(secs - 1, 0, -1):
        await asyncio.sleep(1)
        await msg.edit(content=f"⏳ Countdown: **{i}**")
    await asyncio.sleep(1)
    await msg.edit(content="🎉 **Time's up!**")

# ══════════════════════════════════════════════
#  NOTES / STICKY MESSAGES
# ══════════════════════════════════════════════

@bot.command(name="note")
async def note(ctx, action: str = None, target: str = None, *, text: str = None):
    """Manage notes for users. Usage: !note add @user note text / !note list @user / !note clear @user"""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not action or not target:
        await ctx.send("❌ Usage: `!note add @user/ID <text>`, `!note list @user/ID`, `!note clear @user/ID`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    data.setdefault("notes", {})
    uid = str(member.id)

    if action.lower() == "add":
        if not text:
            await ctx.send("❌ Provide note text.")
            return
        data["notes"].setdefault(uid, [])
        data["notes"][uid].append({
            "text": text,
            "by": str(ctx.author.id),
            "at": datetime.utcnow().isoformat()
        })
        save_data(data)
        await ctx.send(f"📝 Note added for **{member}**. Total: {len(data['notes'][uid])}")

    elif action.lower() == "list":
        notes = data["notes"].get(uid, [])
        if not notes:
            await ctx.send(f"📝 No notes for **{member}**.")
            return
        embed = discord.Embed(title=f"📝 Notes for {member}", color=discord.Color.blurple())
        for i, n in enumerate(notes[-10:], 1):
            embed.add_field(
                name=f"#{i} — {n['at'][:10]}",
                value=f"{n['text']}\n— <@{n['by']}>",
                inline=False
            )
        await ctx.send(embed=embed)

    elif action.lower() == "clear":
        data["notes"][uid] = []
        save_data(data)
        await ctx.send(f"✅ Cleared all notes for **{member}**.")

# ══════════════════════════════════════════════
#  POLL (enhanced)
# ══════════════════════════════════════════════

@bot.command(name="poll")
async def poll(ctx, *, question: str = None):
    if not question:
        await ctx.send("❌ Usage: `!poll Soru | Seçenek1 | Seçenek2`\nOr simple yes/no: `!poll Your question?`")
        return
    parts = [p.strip() for p in question.split("|")]
    if len(parts) < 2:
        # Simple yes/no poll
        embed = discord.Embed(
            title=f"📊 {question}",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=f"Poll by {ctx.author.display_name}")
        poll_msg = await ctx.send(embed=embed)
        await poll_msg.add_reaction("👍")
        await poll_msg.add_reaction("👎")
        await ctx.message.delete()
        return
    options = parts[1:]
    if len(options) > 10:
        await ctx.send("❌ Max 10 seçenek.")
        return
    number_emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    embed = discord.Embed(title=f"📊 {parts[0]}", color=discord.Color.blurple(), timestamp=datetime.utcnow())
    embed.description = "\n".join([f"{number_emojis[i]} {opt}" for i, opt in enumerate(options)])
    embed.set_footer(text=f"Poll by {ctx.author.display_name}")
    poll_msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await poll_msg.add_reaction(number_emojis[i])
    await ctx.message.delete()

# ══════════════════════════════════════════════
#  GIVEAWAY
# ══════════════════════════════════════════════

@bot.command(name="giveaway")
async def giveaway(ctx, duration: str = None, winners: str = None, *, prize: str = None):
    if not is_host_or_admin(ctx):
        await ctx.send("❌ Only hosts can start giveaways.")
        return
    if not duration or not winners or not prize:
        await ctx.send("❌ Usage: `!giveaway <minutes> <winners> <prize>`")
        return
    try:
        mins = int(duration)
        win_count = int(winners)
    except ValueError:
        await ctx.send("❌ Duration and winners must be numbers.")
        return
    end_time = datetime.utcnow() + timedelta(minutes=mins)
    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=f"**Prize:** {prize}\n\nReact with 🎉 to enter!\n\n**Winners:** {win_count}\n**Ends:** <t:{int(end_time.timestamp())}:R>",
        color=discord.Color.gold(),
        timestamp=end_time
    )
    embed.set_footer(text=f"Ends at • Hosted by {ctx.author.display_name}")
    giveaway_msg = await ctx.send(embed=embed)
    await giveaway_msg.add_reaction("🎉")
    await ctx.message.delete()
    await asyncio.sleep(mins * 60)
    giveaway_msg = await ctx.channel.fetch_message(giveaway_msg.id)
    reaction = discord.utils.get(giveaway_msg.reactions, emoji="🎉")
    if not reaction:
        await ctx.send("❌ No one entered the giveaway.")
        return
    users = [u async for u in reaction.users() if not u.bot]
    if not users:
        await ctx.send("❌ No valid entries.")
        return
    actual_winners = random.sample(users, min(win_count, len(users)))
    winner_mentions = ", ".join(w.mention for w in actual_winners)
    embed.description = f"**Prize:** {prize}\n\n**Winner(s):** {winner_mentions}"
    embed.color = discord.Color.green()
    await giveaway_msg.edit(embed=embed)
    await ctx.send(f"🎉 Congratulations {winner_mentions}! You won **{prize}**!")

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
        await interaction.response.send_message(f"✅ You're already verified as **{sc}**.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    exists = await check_social_club(social_club_name)
    if not exists:
        await interaction.followup.send(
            f"❌ Could not find Social Club profile **{social_club_name}**.\n"
            f"Make sure the username is correct (case-sensitive).\n"
            f"Check: https://socialclub.rockstargames.com/member/{social_club_name}/",
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
        f"✅ Verified! Welcome **{social_club_name}**!\nUse the queue button to join a heist.",
        ephemeral=True
    )
    log_channel = discord.utils.get(guild.text_channels, name=VERIFY_LOG_CHANNEL)
    if log_channel:
        embed = discord.Embed(title="✅ Auto Verified", color=discord.Color.green(), timestamp=datetime.utcnow())
        embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="Social Club", value=f"[{social_club_name}](https://socialclub.rockstargames.com/member/{social_club_name}/)", inline=True)
        embed.set_footer(text=f"User ID: {interaction.user.id}")
        await log_channel.send(embed=embed)

@bot.tree.command(name="forceverify", description="[Host] Manually verify a member")
@app_commands.describe(member="Discord member", social_club_name="Their Social Club name")
async def forceverify(interaction: discord.Interaction, member: discord.Member, social_club_name: str):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can use this.", ephemeral=True)
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
    await interaction.response.send_message(f"✅ {member.mention} manually verified as **{social_club_name}**.", ephemeral=True)

@bot.tree.command(name="unverify", description="[Host] Remove a member's verification")
@app_commands.describe(member="Discord member to unverify")
async def unverify(interaction: discord.Interaction, member: discord.Member):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can use this.", ephemeral=True)
        return
    data = load_data()
    user_id = str(member.id)
    if user_id not in data["verified"]:
        await interaction.response.send_message(f"⚠️ {member.mention} is not verified.", ephemeral=True)
        return
    del data["verified"][user_id]
    save_data(data)
    role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
    if role and role in member.roles:
        await member.remove_roles(role)
    await interaction.response.send_message(f"🗑️ {member.mention} has been unverified.", ephemeral=True)

@bot.tree.command(name="whois", description="Look up a member's Social Club name")
@app_commands.describe(member="The Discord member to look up")
async def whois(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    user_id = str(member.id)
    if user_id in data["verified"]:
        sc = data["verified"][user_id]["social_club"]
        method = data["verified"][user_id].get("method", "unknown")
        await interaction.response.send_message(embed=discord.Embed(
            title="🔎 Member Lookup", color=discord.Color.green(),
            description=f"**Discord:** {member.mention}\n**Social Club:** [{sc}](https://socialclub.rockstargames.com/member/{sc}/)\n**Method:** {method}"
        ))
    else:
        await interaction.response.send_message(f"❌ {member.mention} is not verified.", ephemeral=True)

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
        embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "No one has joined yet."
        embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
        embed.add_field(name="Host", value=f"<@{self.host_id}>", inline=True)
        embed.set_footer(text="Press Join to enter the queue!")
        try:
            await self.message_ref.edit(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.success, emoji="🎮")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        if not is_verified_user(user_id):
            await interaction.response.send_message("❌ Verify first with `/verify <social_club_name>`.", ephemeral=True)
            return
        data = load_data()
        if any(e["user_id"] == user_id for e in data["queue"]):
            await interaction.response.send_message("⚠️ You're already in the queue!", ephemeral=True)
            return
        if len(data["queue"]) >= MAX_QUEUE_SIZE:
            await interaction.response.send_message(f"🚫 Queue is full ({MAX_QUEUE_SIZE}/{MAX_QUEUE_SIZE})!", ephemeral=True)
            return
        sc = get_sc(user_id)
        data["queue"].append({"user_id": user_id, "social_club": sc, "joined_at": datetime.utcnow().isoformat()})
        save_data(data)
        # DM host
        host = interaction.guild.get_member(self.host_id)
        if host:
            try:
                await host.send(
                    f"🎮 **{interaction.user}** joined the heist queue!\n"
                    f"Social Club: **{sc}**\n"
                    f"Queue: {len(data['queue'])}/{MAX_QUEUE_SIZE}"
                )
            except Exception:
                pass
        await interaction.response.send_message(f"✅ Joined as **{sc}**! Position: **{len(data['queue'])}**", ephemeral=True)
        await self.update_embed(data)

        # Notify in channel if queue is full
        if len(data["queue"]) >= MAX_QUEUE_SIZE:
            channel = interaction.channel
            if channel:
                mentions = " ".join(f"<@{e['user_id']}>" for e in data["queue"])
                await channel.send(f"🔔 **Queue is full!** {mentions}\nHost <@{self.host_id}> can start with `/queue start`!")

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        data = load_data()
        before = len(data["queue"])
        data["queue"] = [e for e in data["queue"] if e["user_id"] != user_id]
        if len(data["queue"]) == before:
            await interaction.response.send_message("⚠️ You're not in the queue.", ephemeral=True)
            return
        save_data(data)
        await interaction.response.send_message("👋 You left the queue.", ephemeral=True)
        await self.update_embed(data)

    @discord.ui.button(label="Queue Info", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        queue = data["queue"]
        embed = discord.Embed(
            title="ℹ️ Queue Info",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow()
        )
        if queue:
            players = "\n".join([f"`{i+1}.` **{e['social_club']}** — Joined <t:{int(datetime.fromisoformat(e['joined_at']).timestamp())}:R>" for i, e in enumerate(queue)])
            embed.description = players
        else:
            embed.description = "Queue is empty."
        embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
        embed.add_field(name="Host", value=f"<@{self.host_id}>", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


queue_group = app_commands.Group(name="queue", description="Heist queue commands")

@queue_group.command(name="open", description="[Host] Open a heist queue with join button")
async def queue_open(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can open queues.", ephemeral=True)
        return
    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)
    channel = discord.utils.get(interaction.guild.text_channels, name=QUEUE_CHANNEL) or interaction.channel
    view = QueueJoinView(host_id=interaction.user.id)
    embed = discord.Embed(
        title="🎮 GTA Online Heist Queue",
        description="No one has joined yet.",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Host", value=interaction.user.mention, inline=True)
    embed.add_field(name="Spots", value=f"0/{MAX_QUEUE_SIZE}", inline=True)
    embed.set_footer(text="Press Join to enter the queue!")
    await interaction.response.send_message("✅ Queue opened!", ephemeral=True)
    msg = await channel.send(embed=embed, view=view)
    view.message_ref = msg

@queue_group.command(name="start", description="[Host] Start the heist with current queue")
async def queue_start(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can start sessions.", ephemeral=True)
        return
    data = load_data()
    if not data["queue"]:
        await interaction.response.send_message("⚠️ Queue is empty.", ephemeral=True)
        return
    data["session_active"] = True
    save_data(data)
    mentions = " ".join(f"<@{e['user_id']}>" for e in data["queue"])
    sc_list = "\n".join([f"**{e['social_club']}**" for e in data["queue"]])
    await interaction.response.send_message(
        f"🚀 **Heist started!**\nPlayers: {mentions}\nHost: {interaction.user.mention}\n\n**Social Club names:**\n{sc_list}\n\nGet in the lobby!"
    )

@queue_group.command(name="clear", description="[Host] Clear the queue")
async def queue_clear(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can clear the queue.", ephemeral=True)
        return
    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)
    await interaction.response.send_message("🧹 Queue cleared.")

@queue_group.command(name="view", description="View the current heist queue")
async def queue_view(interaction: discord.Interaction):
    data = load_data()
    queue = data["queue"]
    embed = discord.Embed(title="🎮 GTA Online Heist Queue", color=discord.Color.blue(), timestamp=datetime.utcnow())
    embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "Queue is empty!"
    embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.add_field(name="Status", value="🟢 Active" if data["session_active"] else "🔴 Waiting", inline=True)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="kick", description="[Host] Remove a player from the queue")
@app_commands.describe(member="The member to remove")
async def queue_kick(interaction: discord.Interaction, member: discord.Member):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can remove players.", ephemeral=True)
        return
    data = load_data()
    before = len(data["queue"])
    data["queue"] = [e for e in data["queue"] if e["user_id"] != str(member.id)]
    if len(data["queue"]) == before:
        await interaction.response.send_message(f"⚠️ {member.display_name} is not in the queue.", ephemeral=True)
        return
    save_data(data)
    await interaction.response.send_message(f"🦵 {member.mention} removed from the queue.")

# ══════════════════════════════════════════════
#  AUTO-MOD MANAGEMENT COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="automod")
async def automod_cmd(ctx, setting: str = None, *, value: str = None):
    """Configure auto-mod. Usage: !automod <setting> <on/off>"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can configure auto-mod.")
        return
    if not setting:
        data = load_data()
        guild_id = str(ctx.guild.id)
        settings = data.get("automod", {}).get(guild_id, {})
        embed = discord.Embed(title="🛡️ Auto-Mod Settings", color=discord.Color.blue(), timestamp=datetime.utcnow())
        embed.add_field(name="Word Filter", value="✅ On" if settings.get("word_filter") else "❌ Off", inline=True)
        embed.add_field(name="Invite Filter", value="✅ On" if settings.get("invite_filter") else "❌ Off", inline=True)
        embed.add_field(name="Link Filter", value="✅ On" if settings.get("link_filter") else "❌ Off", inline=True)
        embed.add_field(name="Caps Filter", value="✅ On" if settings.get("caps_filter") else "❌ Off", inline=True)
        embed.add_field(name="Mention Filter", value="✅ On" if settings.get("mention_filter") else "❌ Off", inline=True)
        embed.add_field(name="Emoji Filter", value="✅ On" if settings.get("emoji_filter") else "❌ Off", inline=True)
        embed.add_field(name="Max Mentions", value=str(settings.get("max_mentions", 5)), inline=True)
        embed.add_field(name="Max Emojis", value=str(settings.get("max_emojis", 10)), inline=True)
        embed.add_field(name="Blocked Words", value=str(len(settings.get("blocked_words", []))), inline=True)
        embed.set_footer(text="!automod <setting> on/off | !blockedwords add/remove/list")
        await ctx.send(embed=embed)
        return

    valid_settings = ["word_filter", "invite_filter", "link_filter", "caps_filter", "mention_filter", "emoji_filter"]
    setting = setting.lower()
    if setting in ["max_mentions", "max_emojis"]:
        if not value or not value.isdigit():
            await ctx.send(f"❌ Usage: `!automod {setting} <number>`")
            return
        data = load_data()
        guild_id = str(ctx.guild.id)
        data.setdefault("automod", {})
        data["automod"].setdefault(guild_id, {})
        data["automod"][guild_id][setting] = int(value)
        save_data(data)
        await ctx.send(f"✅ **{setting}** set to **{value}**.")
        return

    if setting not in valid_settings:
        await ctx.send(f"❌ Valid settings: `{'`, `'.join(valid_settings)}`, `max_mentions`, `max_emojis`")
        return
    if not value or value.lower() not in ["on", "off", "true", "false", "enable", "disable"]:
        await ctx.send(f"❌ Usage: `!automod {setting} on/off`")
        return
    enabled = value.lower() in ["on", "true", "enable"]
    data = load_data()
    guild_id = str(ctx.guild.id)
    data.setdefault("automod", {})
    data["automod"].setdefault(guild_id, {})
    data["automod"][guild_id][setting] = enabled
    save_data(data)
    status = "✅ enabled" if enabled else "❌ disabled"
    await ctx.send(f"🛡️ **{setting}** has been {status}.")


@bot.command(name="blockedwords")
async def blockedwords_cmd(ctx, action: str = None, *, word: str = None):
    """Manage blocked words. Usage: !blockedwords add/remove/list"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can manage blocked words.")
        return
    data = load_data()
    guild_id = str(ctx.guild.id)
    data.setdefault("automod", {})
    data["automod"].setdefault(guild_id, {})
    data["automod"][guild_id].setdefault("blocked_words", [])

    if not action or action.lower() == "list":
        words = data["automod"][guild_id]["blocked_words"]
        if not words:
            await ctx.send("📝 No blocked words set. Use `!blockedwords add <word>` to add.")
            return
        embed = discord.Embed(title="🚫 Blocked Words", color=discord.Color.red())
        embed.description = ", ".join(f"`{w}`" for w in words)
        embed.set_footer(text=f"{len(words)} word(s)")
        await ctx.send(embed=embed)
        return

    if action.lower() == "add":
        if not word:
            await ctx.send("❌ Usage: `!blockedwords add <word>`")
            return
        if word.lower() not in [w.lower() for w in data["automod"][guild_id]["blocked_words"]]:
            data["automod"][guild_id]["blocked_words"].append(word.lower())
            save_data(data)
            await ctx.send(f"✅ Added `{word}` to blocked words.")
        else:
            await ctx.send(f"⚠️ `{word}` is already blocked.")

    elif action.lower() == "remove":
        if not word:
            await ctx.send("❌ Usage: `!blockedwords remove <word>`")
            return
        words = data["automod"][guild_id]["blocked_words"]
        data["automod"][guild_id]["blocked_words"] = [w for w in words if w.lower() != word.lower()]
        save_data(data)
        await ctx.send(f"✅ Removed `{word}` from blocked words.")

    elif action.lower() == "clear":
        data["automod"][guild_id]["blocked_words"] = []
        save_data(data)
        await ctx.send("✅ Cleared all blocked words.")


# ══════════════════════════════════════════════
#  MOD CASE LOG SYSTEM
# ══════════════════════════════════════════════

async def create_mod_case(guild, action: str, target, moderator, reason: str = "No reason"):
    """Create a moderation case and log it."""
    data = load_data()
    guild_id = str(guild.id)
    data.setdefault("cases", {})
    data.setdefault("case_counter", {})
    data["cases"].setdefault(guild_id, [])
    data["case_counter"].setdefault(guild_id, 0)
    data["case_counter"][guild_id] += 1
    case_num = data["case_counter"][guild_id]
    case = {
        "case_number": case_num,
        "action": action,
        "target_id": str(target.id),
        "target_name": str(target),
        "moderator_id": str(moderator.id),
        "moderator_name": str(moderator),
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat()
    }
    data["cases"][guild_id].append(case)
    save_data(data)

    # Log to mod-logs channel
    log_ch = discord.utils.get(guild.text_channels, name=MODLOG_CHANNEL)
    if log_ch:
        action_colors = {
            "BAN": discord.Color.red(), "TEMPBAN": discord.Color.dark_red(),
            "UNBAN": discord.Color.green(), "KICK": discord.Color.orange(),
            "MUTE": discord.Color.dark_grey(), "UNMUTE": discord.Color.green(),
            "WARN": discord.Color.yellow(), "TIMEOUT": discord.Color.purple(),
            "SOFTBAN": discord.Color.dark_orange(),
        }
        embed = discord.Embed(
            title=f"Case #{case_num} | {action}",
            color=action_colors.get(action, discord.Color.blurple()),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="👤 User", value=f"{target.mention} ({target})", inline=True)
        embed.add_field(name="🛡️ Moderator", value=f"{moderator.mention}", inline=True)
        embed.add_field(name="📝 Reason", value=reason, inline=False)
        embed.set_footer(text=f"User ID: {target.id}")
        await log_ch.send(embed=embed)
    return case_num


@bot.command(name="case")
async def case_cmd(ctx, case_num: str = None):
    """View a specific mod case."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not case_num or not case_num.isdigit():
        await ctx.send("❌ Usage: `!case <number>`")
        return
    data = load_data()
    guild_id = str(ctx.guild.id)
    cases = data.get("cases", {}).get(guild_id, [])
    case = next((c for c in cases if c["case_number"] == int(case_num)), None)
    if not case:
        await ctx.send(f"❌ Case #{case_num} not found.")
        return
    embed = discord.Embed(
        title=f"📋 Case #{case['case_number']} — {case['action']}",
        color=discord.Color.blurple(), timestamp=datetime.fromisoformat(case["timestamp"])
    )
    embed.add_field(name="User", value=f"<@{case['target_id']}> ({case['target_name']})", inline=True)
    embed.add_field(name="Moderator", value=f"<@{case['moderator_id']}>", inline=True)
    embed.add_field(name="Reason", value=case["reason"], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="modlogs")
async def modlogs_cmd(ctx, target: str = None):
    """View mod cases for a user."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target:
        await ctx.send("❌ Usage: `!modlogs @user/ID`")
        return
    member = await resolve_member(ctx, target)
    user_id = str(member.id) if member else target.strip()
    data = load_data()
    guild_id = str(ctx.guild.id)
    cases = [c for c in data.get("cases", {}).get(guild_id, []) if c["target_id"] == user_id]
    if not cases:
        await ctx.send(f"✅ No mod cases found for that user.")
        return
    embed = discord.Embed(
        title=f"📋 Mod Cases ({len(cases)} total)",
        color=discord.Color.blurple(), timestamp=datetime.utcnow()
    )
    for c in cases[-10:]:
        embed.add_field(
            name=f"#{c['case_number']} — {c['action']} ({c['timestamp'][:10]})",
            value=f"By: <@{c['moderator_id']}> | Reason: `{c['reason'][:50]}`",
            inline=False
        )
    await ctx.send(embed=embed)


# ══════════════════════════════════════════════
#  TEMP BAN / SOFTBAN / MASSBAN
# ══════════════════════════════════════════════

@bot.command(name="tempban")
async def tempban(ctx, target: str = None, duration: str = None, *, reason: str = "No reason provided"):
    """Temporarily ban a member. Usage: !tempban @user <minutes> [reason]"""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission to temp-ban members.")
        return
    if not target or not duration:
        await ctx.send("❌ Usage: `!tempban @user/ID <minutes> [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        mins = int(duration)
    except ValueError:
        await ctx.send("❌ Duration must be a number (minutes).")
        return
    unban_at = datetime.utcnow() + timedelta(minutes=mins)
    try:
        await member.send(
            f"🔨 You have been **temporarily banned** from **{ctx.guild.name}** for **{mins} minutes**.\n"
            f"Reason: `{reason}`"
        )
    except Exception:
        pass
    await member.ban(reason=f"[TEMPBAN {mins}m] {reason} | By {ctx.author}")
    data = load_data()
    data.setdefault("temp_bans", [])
    data["temp_bans"].append({
        "guild_id": ctx.guild.id, "user_id": member.id,
        "unban_at": unban_at.isoformat(), "banned_by": str(ctx.author.id)
    })
    save_data(data)
    await create_mod_case(ctx.guild, "TEMPBAN", member, ctx.author, f"{reason} ({mins} min)")
    await ctx.send(f"🔨 **{member}** has been temp-banned for **{mins} minutes**. Reason: `{reason}`")


@bot.command(name="softban")
async def softban(ctx, target: str = None, *, reason: str = "No reason provided"):
    """Ban and immediately unban to clear messages."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target:
        await ctx.send("❌ Usage: `!softban @user/ID [reason]`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        await member.send(f"🔨 You have been **softbanned** from **{ctx.guild.name}**.\nReason: `{reason}`")
    except Exception:
        pass
    await member.ban(reason=f"[SOFTBAN] {reason} | By {ctx.author}", delete_message_days=7)
    await ctx.guild.unban(member, reason="Softban unban")
    await create_mod_case(ctx.guild, "SOFTBAN", member, ctx.author, reason)
    await ctx.send(f"🔨 **{member}** has been softbanned (messages cleared). Reason: `{reason}`")


@bot.command(name="massban")
async def massban(ctx, *, user_ids: str = None):
    """Ban multiple users by ID. Usage: !massban id1 id2 id3"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can mass-ban.")
        return
    if not user_ids:
        await ctx.send("❌ Usage: `!massban <id1> <id2> <id3> ...`")
        return
    ids = user_ids.split()
    banned = []
    failed = []
    for uid in ids:
        try:
            user = await bot.fetch_user(int(uid.strip()))
            await ctx.guild.ban(user, reason=f"Massban by {ctx.author}")
            banned.append(str(user))
        except Exception:
            failed.append(uid)
    embed = discord.Embed(title="🔨 Mass Ban Results", color=discord.Color.red())
    if banned:
        embed.add_field(name=f"✅ Banned ({len(banned)})", value="\n".join(banned[:20]), inline=False)
    if failed:
        embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(failed[:20]), inline=False)
    await ctx.send(embed=embed)


@tasks.loop(seconds=60)
async def tempban_check():
    """Background task to unban temp-banned users."""
    data = load_data()
    temp_bans = data.get("temp_bans", [])
    now = datetime.utcnow()
    remaining = []
    for tb in temp_bans:
        unban_at = datetime.fromisoformat(tb["unban_at"])
        if now >= unban_at:
            try:
                guild = bot.get_guild(tb["guild_id"])
                if guild:
                    user = await bot.fetch_user(tb["user_id"])
                    await guild.unban(user, reason="Temp-ban expired")
                    log_ch = discord.utils.get(guild.text_channels, name=MODLOG_CHANNEL)
                    if log_ch:
                        await log_ch.send(f"✅ **{user}** has been auto-unbanned (temp-ban expired).")
            except Exception:
                pass
        else:
            remaining.append(tb)
    if len(remaining) != len(temp_bans):
        data["temp_bans"] = remaining
        save_data(data)

@tempban_check.before_loop
async def before_tempban_check():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════
#  STARBOARD
# ══════════════════════════════════════════════

@bot.listen('on_raw_reaction_add')
async def starboard_listener(payload: discord.RawReactionActionEvent):
    """Forward starred messages to the starboard channel."""
    if str(payload.emoji) != STARBOARD_EMOJI:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return
    star_channel = discord.utils.get(guild.text_channels, name=STARBOARD_CHANNEL)
    if not star_channel or channel.id == star_channel.id:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return
    reaction = discord.utils.get(message.reactions, emoji=STARBOARD_EMOJI)
    if not reaction or reaction.count < STARBOARD_THRESHOLD:
        return
    data = load_data()
    guild_id = str(guild.id)
    data.setdefault("starboard", {})
    data["starboard"].setdefault(guild_id, {})
    msg_key = str(message.id)
    if msg_key in data["starboard"][guild_id]:
        # Update existing starboard message
        try:
            star_msg = await star_channel.fetch_message(int(data["starboard"][guild_id][msg_key]))
            embed = star_msg.embeds[0] if star_msg.embeds else discord.Embed()
            await star_msg.edit(content=f"⭐ **{reaction.count}** | {channel.mention}")
        except Exception:
            pass
        return
    embed = discord.Embed(
        description=message.content[:2048] if message.content else "*No text*",
        color=discord.Color.gold(), timestamp=message.created_at
    )
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
    if message.attachments:
        embed.set_image(url=message.attachments[0].url)
    star_msg = await star_channel.send(content=f"⭐ **{reaction.count}** | {channel.mention}", embed=embed)
    data["starboard"][guild_id][msg_key] = str(star_msg.id)
    save_data(data)


@bot.command(name="starboard")
async def starboard_settings(ctx, setting: str = None, value: str = None):
    """Configure starboard. Usage: !starboard threshold <number>"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can configure starboard.")
        return
    global STARBOARD_THRESHOLD
    if not setting:
        await ctx.send(f"⭐ **Starboard Settings**\nChannel: `#{STARBOARD_CHANNEL}`\nEmoji: {STARBOARD_EMOJI}\nThreshold: **{STARBOARD_THRESHOLD}** reactions")
        return
    if setting.lower() == "threshold" and value and value.isdigit():
        STARBOARD_THRESHOLD = int(value)
        await ctx.send(f"✅ Starboard threshold set to **{value}**.")


# ══════════════════════════════════════════════
#  ECONOMY SYSTEM
# ══════════════════════════════════════════════

# GTA-themed shop items
SHOP_ITEMS = {
    "armored_kuruma": {"name": "🚗 Armored Kuruma", "price": 525000, "description": "Bulletproof getaway car"},
    "oppressor_mk2": {"name": "🏍️ Oppressor Mk II", "price": 3890250, "description": "Flying rocket bike"},
    "deluxo": {"name": "🚙 Deluxo", "price": 4721500, "description": "Flying DeLorean-style car"},
    "kosatka": {"name": "🚢 Kosatka Submarine", "price": 2200000, "description": "Cayo Perico heist HQ"},
    "nightclub": {"name": "🏢 Nightclub", "price": 1080000, "description": "Passive income generator"},
    "arcade": {"name": "🕹️ Arcade", "price": 1235000, "description": "Casino Heist planning room"},
    "ceo_office": {"name": "🏙️ CEO Office", "price": 1000000, "description": "Executive business hub"},
    "bunker": {"name": "🏗️ Bunker", "price": 1165000, "description": "Weapon manufacturing"},
    "agency": {"name": "🕵️ Agency", "price": 2010000, "description": "VIP Contract missions"},
    "golden_minigun": {"name": "🔫 Gold Minigun", "price": 10000000, "description": "Flex on everyone"},
}

def get_economy(data, user_id: str):
    """Get or create economy data for a user."""
    data.setdefault("economy", {})
    data["economy"].setdefault(user_id, {
        "wallet": 1000, "bank": 0, "inventory": [], "total_earned": 1000
    })
    return data["economy"][user_id]


@bot.command(name="balance", aliases=["bal", "money", "wallet"])
async def balance_cmd(ctx, target: str = None):
    """Check your or someone's balance."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    eco = get_economy(data, str(member.id))
    save_data(data)
    total = eco["wallet"] + eco["bank"]
    embed = discord.Embed(
        title=f"{CURRENCY_EMOJI} {member.display_name}'s Balance",
        color=discord.Color.green(), timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="💰 Wallet", value=f"**{CURRENCY_NAME}{eco['wallet']:,}**", inline=True)
    embed.add_field(name="🏦 Bank", value=f"**{CURRENCY_NAME}{eco['bank']:,}**", inline=True)
    embed.add_field(name="💎 Net Worth", value=f"**{CURRENCY_NAME}{total:,}**", inline=True)
    embed.add_field(name="📈 Total Earned", value=f"{CURRENCY_NAME}{eco.get('total_earned', 0):,}", inline=True)
    embed.add_field(name="🎒 Items", value=str(len(eco.get("inventory", []))), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily_cmd(ctx):
    """Collect your daily reward."""
    user_id = str(ctx.author.id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if daily_cooldowns.get(user_id) == today:
        await ctx.send("❌ You've already claimed your daily reward today! Come back tomorrow.")
        return
    daily_cooldowns[user_id] = today
    data = load_data()
    eco = get_economy(data, user_id)
    amount = random.randint(5000, 25000)
    eco["wallet"] += amount
    eco["total_earned"] = eco.get("total_earned", 0) + amount
    save_data(data)
    embed = discord.Embed(
        title="📅 Daily Reward Claimed!",
        description=f"You received **{CURRENCY_NAME}{amount:,}**!",
        color=discord.Color.green()
    )
    embed.add_field(name="💰 New Balance", value=f"{CURRENCY_NAME}{eco['wallet']:,}")
    await ctx.send(embed=embed)


@bot.command(name="work")
async def work_cmd(ctx):
    """Work a GTA job for cash."""
    user_id = str(ctx.author.id)
    now = datetime.utcnow()
    if user_id in work_cooldowns:
        diff = (now - work_cooldowns[user_id]).total_seconds()
        if diff < 300:
            remaining = int(300 - diff)
            await ctx.send(f"❌ You need to rest! Try again in **{remaining}s**.")
            return
    work_cooldowns[user_id] = now
    jobs = [
        ("delivered supplies for the Bunker", 3000, 8000),
        ("completed a VIP work mission", 5000, 15000),
        ("sold nightclub goods", 4000, 12000),
        ("ran a Headhunter mission", 5000, 10000),
        ("exported a stolen vehicle", 8000, 20000),
        ("completed a Payphone Hit", 7500, 17500),
        ("did a Security Contract for the Agency", 5000, 15000),
        ("hacked a Fleeca Bank terminal", 3000, 9000),
        ("drove a getaway car for Lester", 4000, 11000),
        ("flew cargo for SecuroServ", 6000, 14000),
    ]
    job, low, high = random.choice(jobs)
    amount = random.randint(low, high)
    data = load_data()
    eco = get_economy(data, user_id)
    eco["wallet"] += amount
    eco["total_earned"] = eco.get("total_earned", 0) + amount
    save_data(data)
    embed = discord.Embed(
        title="💼 Work Complete!",
        description=f"You {job} and earned **{CURRENCY_NAME}{amount:,}**!",
        color=discord.Color.green()
    )
    embed.add_field(name="💰 Balance", value=f"{CURRENCY_NAME}{eco['wallet']:,}")
    embed.set_footer(text="Cooldown: 5 minutes")
    await ctx.send(embed=embed)


@bot.command(name="crime")
async def crime_cmd(ctx):
    """Attempt a crime for big money (risky!)."""
    user_id = str(ctx.author.id)
    now = datetime.utcnow()
    if user_id in crime_cooldowns:
        diff = (now - crime_cooldowns[user_id]).total_seconds()
        if diff < 600:
            remaining = int(600 - diff)
            await ctx.send(f"❌ Laying low from the cops! Try again in **{remaining}s**.")
            return
    crime_cooldowns[user_id] = now
    data = load_data()
    eco = get_economy(data, user_id)
    success = random.random() < 0.55
    if success:
        crimes = [
            ("robbed a convenience store", 10000, 30000),
            ("hit the Diamond Casino vault", 20000, 50000),
            ("hijacked an armored truck", 15000, 35000),
            ("stole from Merryweather HQ", 12000, 28000),
            ("completed a Drug Deal", 18000, 40000),
        ]
        crime, low, high = random.choice(crimes)
        amount = random.randint(low, high)
        eco["wallet"] += amount
        eco["total_earned"] = eco.get("total_earned", 0) + amount
        save_data(data)
        embed = discord.Embed(
            title="🦹 Crime Successful!",
            description=f"You {crime} and got **{CURRENCY_NAME}{amount:,}**!",
            color=discord.Color.green()
        )
        embed.add_field(name="💰 Balance", value=f"{CURRENCY_NAME}{eco['wallet']:,}")
    else:
        fine = random.randint(5000, 15000)
        eco["wallet"] = max(0, eco["wallet"] - fine)
        save_data(data)
        embed = discord.Embed(
            title="🚔 Busted!",
            description=f"You got caught and fined **{CURRENCY_NAME}{fine:,}**!",
            color=discord.Color.red()
        )
        embed.add_field(name="💰 Balance", value=f"{CURRENCY_NAME}{eco['wallet']:,}")
    embed.set_footer(text="Cooldown: 10 minutes | 55% success rate")
    await ctx.send(embed=embed)


@bot.command(name="pay", aliases=["give", "transfer"])
async def pay_cmd(ctx, target: str = None, amount: str = None):
    """Pay another user."""
    if not target or not amount or not amount.isdigit():
        await ctx.send("❌ Usage: `!pay @user <amount>`")
        return
    member = await resolve_member(ctx, target)
    if not member or member.id == ctx.author.id:
        await ctx.send("❌ Invalid target.")
        return
    amt = int(amount)
    if amt < 1:
        await ctx.send("❌ Amount must be positive.")
        return
    data = load_data()
    sender_eco = get_economy(data, str(ctx.author.id))
    if sender_eco["wallet"] < amt:
        await ctx.send(f"❌ You only have **{CURRENCY_NAME}{sender_eco['wallet']:,}** in your wallet.")
        return
    receiver_eco = get_economy(data, str(member.id))
    sender_eco["wallet"] -= amt
    receiver_eco["wallet"] += amt
    receiver_eco["total_earned"] = receiver_eco.get("total_earned", 0) + amt
    save_data(data)
    embed = discord.Embed(
        title="💸 Payment Sent!",
        description=f"{ctx.author.mention} paid {member.mention} **{CURRENCY_NAME}{amt:,}**",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)


@bot.command(name="gamble", aliases=["bet", "slots"])
async def gamble_cmd(ctx, amount: str = None):
    """Gamble at the Diamond Casino slots!"""
    if not amount:
        await ctx.send("❌ Usage: `!gamble <amount>` or `!gamble all`")
        return
    data = load_data()
    eco = get_economy(data, str(ctx.author.id))
    if amount.lower() == "all":
        amt = eco["wallet"]
    elif amount.isdigit():
        amt = int(amount)
    else:
        await ctx.send("❌ Enter a valid amount or `all`.")
        return
    if amt < 100:
        await ctx.send(f"❌ Minimum bet is **{CURRENCY_NAME}100**.")
        return
    if eco["wallet"] < amt:
        await ctx.send(f"❌ You only have **{CURRENCY_NAME}{eco['wallet']:,}** in your wallet.")
        return
    symbols = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣", "🔔", "⭐"]
    s1, s2, s3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)
    spin_display = f"「 {s1} | {s2} | {s3} 」"

    if s1 == s2 == s3:
        if s1 == "💎":
            multiplier = 10
        elif s1 == "7️⃣":
            multiplier = 7
        else:
            multiplier = 3
        winnings = amt * multiplier
        eco["wallet"] += winnings
        eco["total_earned"] = eco.get("total_earned", 0) + winnings
        result = f"🎰 **JACKPOT!** You won **{CURRENCY_NAME}{winnings:,}** ({multiplier}x)!"
        color = discord.Color.gold()
    elif s1 == s2 or s2 == s3 or s1 == s3:
        winnings = int(amt * 1.5)
        eco["wallet"] += winnings
        eco["total_earned"] = eco.get("total_earned", 0) + winnings
        result = f"🎰 Two matching! You won **{CURRENCY_NAME}{winnings:,}** (1.5x)!"
        color = discord.Color.green()
    else:
        eco["wallet"] -= amt
        result = f"🎰 No match. You lost **{CURRENCY_NAME}{amt:,}**!"
        color = discord.Color.red()
    save_data(data)
    embed = discord.Embed(title="🎰 Diamond Casino Slots", color=color)
    embed.description = f"{spin_display}\n\n{result}"
    embed.add_field(name="💰 Balance", value=f"{CURRENCY_NAME}{eco['wallet']:,}")
    await ctx.send(embed=embed)


@bot.command(name="rob", aliases=["steal"])
async def rob_cmd(ctx, target: str = None):
    """Attempt to rob another user."""
    if not target:
        await ctx.send("❌ Usage: `!rob @user`")
        return
    member = await resolve_member(ctx, target)
    if not member or member.id == ctx.author.id:
        await ctx.send("❌ Invalid target.")
        return
    user_id = str(ctx.author.id)
    now = datetime.utcnow()
    if user_id in rob_cooldowns:
        diff = (now - rob_cooldowns[user_id]).total_seconds()
        if diff < 900:
            remaining = int(900 - diff)
            await ctx.send(f"❌ You're still laying low! Try again in **{remaining}s**.")
            return
    rob_cooldowns[user_id] = now
    data = load_data()
    robber_eco = get_economy(data, user_id)
    victim_eco = get_economy(data, str(member.id))
    if victim_eco["wallet"] < 500:
        await ctx.send(f"❌ **{member.display_name}** doesn't have enough to rob (min {CURRENCY_NAME}500 in wallet).")
        return
    success = random.random() < 0.40
    if success:
        steal_amount = random.randint(1, min(int(victim_eco["wallet"] * 0.3), 50000))
        robber_eco["wallet"] += steal_amount
        robber_eco["total_earned"] = robber_eco.get("total_earned", 0) + steal_amount
        victim_eco["wallet"] -= steal_amount
        save_data(data)
        embed = discord.Embed(
            title="🦹 Robbery Successful!",
            description=f"You stole **{CURRENCY_NAME}{steal_amount:,}** from {member.mention}!",
            color=discord.Color.green()
        )
    else:
        fine = random.randint(2000, 10000)
        robber_eco["wallet"] = max(0, robber_eco["wallet"] - fine)
        save_data(data)
        embed = discord.Embed(
            title="🚔 Robbery Failed!",
            description=f"You got caught and fined **{CURRENCY_NAME}{fine:,}**!",
            color=discord.Color.red()
        )
    embed.set_footer(text="Cooldown: 15 minutes | 40% success rate")
    await ctx.send(embed=embed)


@bot.command(name="deposit", aliases=["dep"])
async def deposit_cmd(ctx, amount: str = None):
    """Deposit money into your bank."""
    if not amount:
        await ctx.send("❌ Usage: `!deposit <amount>` or `!deposit all`")
        return
    data = load_data()
    eco = get_economy(data, str(ctx.author.id))
    if amount.lower() == "all":
        amt = eco["wallet"]
    elif amount.isdigit():
        amt = int(amount)
    else:
        await ctx.send("❌ Enter a valid amount.")
        return
    if amt < 1 or eco["wallet"] < amt:
        await ctx.send(f"❌ You only have **{CURRENCY_NAME}{eco['wallet']:,}** in your wallet.")
        return
    eco["wallet"] -= amt
    eco["bank"] += amt
    save_data(data)
    await ctx.send(f"🏦 Deposited **{CURRENCY_NAME}{amt:,}** into your bank.\n💰 Wallet: **{CURRENCY_NAME}{eco['wallet']:,}** | 🏦 Bank: **{CURRENCY_NAME}{eco['bank']:,}**")


@bot.command(name="withdraw", aliases=["with"])
async def withdraw_cmd(ctx, amount: str = None):
    """Withdraw money from your bank."""
    if not amount:
        await ctx.send("❌ Usage: `!withdraw <amount>` or `!withdraw all`")
        return
    data = load_data()
    eco = get_economy(data, str(ctx.author.id))
    if amount.lower() == "all":
        amt = eco["bank"]
    elif amount.isdigit():
        amt = int(amount)
    else:
        await ctx.send("❌ Enter a valid amount.")
        return
    if amt < 1 or eco["bank"] < amt:
        await ctx.send(f"❌ You only have **{CURRENCY_NAME}{eco['bank']:,}** in your bank.")
        return
    eco["bank"] -= amt
    eco["wallet"] += amt
    save_data(data)
    await ctx.send(f"🏦 Withdrew **{CURRENCY_NAME}{amt:,}** from your bank.\n💰 Wallet: **{CURRENCY_NAME}{eco['wallet']:,}** | 🏦 Bank: **{CURRENCY_NAME}{eco['bank']:,}**")


@bot.command(name="shop")
async def shop_cmd(ctx):
    """Browse the GTA shop."""
    embed = discord.Embed(
        title=f"🏪 GTA Online Shop",
        description="Buy items with `!buy <item_name>`",
        color=discord.Color.gold(), timestamp=datetime.utcnow()
    )
    for key, item in SHOP_ITEMS.items():
        embed.add_field(
            name=f"{item['name']} — {CURRENCY_NAME}{item['price']:,}",
            value=f"*{item['description']}*\n`!buy {key}`",
            inline=True
        )
    await ctx.send(embed=embed)


@bot.command(name="buy")
async def buy_cmd(ctx, *, item_name: str = None):
    """Buy an item from the shop."""
    if not item_name:
        await ctx.send("❌ Usage: `!buy <item_name>` — see `!shop`")
        return
    item_key = item_name.lower().replace(" ", "_")
    if item_key not in SHOP_ITEMS:
        await ctx.send(f"❌ Item `{item_name}` not found. Use `!shop` to browse.")
        return
    item = SHOP_ITEMS[item_key]
    data = load_data()
    eco = get_economy(data, str(ctx.author.id))
    if eco["wallet"] + eco["bank"] < item["price"]:
        await ctx.send(f"❌ You need **{CURRENCY_NAME}{item['price']:,}** but only have **{CURRENCY_NAME}{eco['wallet'] + eco['bank']:,}** total.")
        return
    # Take from wallet first, then bank
    if eco["wallet"] >= item["price"]:
        eco["wallet"] -= item["price"]
    else:
        remaining = item["price"] - eco["wallet"]
        eco["wallet"] = 0
        eco["bank"] -= remaining
    eco.setdefault("inventory", [])
    eco["inventory"].append({"item": item_key, "name": item["name"], "bought_at": datetime.utcnow().isoformat()})
    save_data(data)
    embed = discord.Embed(
        title="🛒 Purchase Complete!",
        description=f"You bought **{item['name']}** for **{CURRENCY_NAME}{item['price']:,}**!",
        color=discord.Color.green()
    )
    embed.add_field(name="💰 Remaining", value=f"Wallet: {CURRENCY_NAME}{eco['wallet']:,} | Bank: {CURRENCY_NAME}{eco['bank']:,}")
    await ctx.send(embed=embed)


@bot.command(name="inventory", aliases=["inv"])
async def inventory_cmd(ctx, target: str = None):
    """View your inventory."""
    if target:
        member = await resolve_member(ctx, target)
    else:
        member = ctx.author
    if not member:
        await ctx.send("❌ Member not found.")
        return
    data = load_data()
    eco = get_economy(data, str(member.id))
    items = eco.get("inventory", [])
    if not items:
        await ctx.send(f"🎒 **{member.display_name}** has no items. Use `!shop` to browse!")
        return
    embed = discord.Embed(title=f"🎒 {member.display_name}'s Inventory", color=discord.Color.blurple())
    item_counts = {}
    for item in items:
        name = item["name"]
        item_counts[name] = item_counts.get(name, 0) + 1
    embed.description = "\n".join([f"{name} x{count}" for name, count in item_counts.items()])
    embed.set_footer(text=f"{len(items)} item(s)")
    await ctx.send(embed=embed)


@bot.command(name="richest", aliases=["baltop", "ecotop"])
async def richest_cmd(ctx):
    """Show the richest members."""
    data = load_data()
    economy = data.get("economy", {})
    if not economy:
        await ctx.send("❌ No one has any money yet!")
        return
    sorted_users = sorted(
        economy.items(),
        key=lambda x: x[1].get("wallet", 0) + x[1].get("bank", 0),
        reverse=True
    )[:10]
    medals = ["🥇", "🥈", "🥉"]
    embed = discord.Embed(title=f"💰 Richest Players", color=discord.Color.gold(), timestamp=datetime.utcnow())
    lines = []
    for i, (uid, eco) in enumerate(sorted_users):
        total = eco.get("wallet", 0) + eco.get("bank", 0)
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{medal} <@{uid}> — **{CURRENCY_NAME}{total:,}**")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


# ══════════════════════════════════════════════
#  SUGGESTION SYSTEM
# ══════════════════════════════════════════════

@bot.command(name="suggest")
async def suggest_cmd(ctx, *, suggestion: str = None):
    """Submit a suggestion."""
    if not suggestion:
        await ctx.send("❌ Usage: `!suggest <your suggestion>`")
        return
    channel = discord.utils.get(ctx.guild.text_channels, name=SUGGESTION_CHANNEL)
    if not channel:
        channel = ctx.channel
    data = load_data()
    guild_id = str(ctx.guild.id)
    data.setdefault("suggestions", {})
    data.setdefault("suggestion_counter", {})
    data["suggestions"].setdefault(guild_id, [])
    data["suggestion_counter"].setdefault(guild_id, 0)
    data["suggestion_counter"][guild_id] += 1
    s_id = data["suggestion_counter"][guild_id]
    embed = discord.Embed(
        title=f"💡 Suggestion #{s_id}",
        description=suggestion,
        color=discord.Color.blurple(), timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"Status: ⏳ Pending | ID: {s_id}")
    msg = await channel.send(embed=embed)
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    data["suggestions"][guild_id].append({
        "id": s_id, "text": suggestion, "author_id": str(ctx.author.id),
        "message_id": msg.id, "channel_id": channel.id, "status": "pending",
        "timestamp": datetime.utcnow().isoformat()
    })
    save_data(data)
    if channel != ctx.channel:
        await ctx.send(f"✅ Suggestion #{s_id} submitted in {channel.mention}!")
    await ctx.message.delete()


@bot.command(name="approve")
async def approve_cmd(ctx, suggestion_id: str = None, *, reason: str = ""):
    """Approve a suggestion."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not suggestion_id or not suggestion_id.isdigit():
        await ctx.send("❌ Usage: `!approve <id> [reason]`")
        return
    data = load_data()
    guild_id = str(ctx.guild.id)
    suggestions = data.get("suggestions", {}).get(guild_id, [])
    s = next((s for s in suggestions if s["id"] == int(suggestion_id)), None)
    if not s:
        await ctx.send(f"❌ Suggestion #{suggestion_id} not found.")
        return
    s["status"] = "approved"
    s["reviewed_by"] = str(ctx.author.id)
    s["review_reason"] = reason
    save_data(data)
    try:
        ch = ctx.guild.get_channel(s["channel_id"])
        if ch:
            msg = await ch.fetch_message(s["message_id"])
            embed = msg.embeds[0]
            embed.color = discord.Color.green()
            embed.set_footer(text=f"Status: ✅ Approved by {ctx.author} | {reason}" if reason else f"Status: ✅ Approved by {ctx.author}")
            await msg.edit(embed=embed)
    except Exception:
        pass
    await ctx.send(f"✅ Suggestion #{suggestion_id} has been **approved**.")


@bot.command(name="deny")
async def deny_cmd(ctx, suggestion_id: str = None, *, reason: str = ""):
    """Deny a suggestion."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not suggestion_id or not suggestion_id.isdigit():
        await ctx.send("❌ Usage: `!deny <id> [reason]`")
        return
    data = load_data()
    guild_id = str(ctx.guild.id)
    suggestions = data.get("suggestions", {}).get(guild_id, [])
    s = next((s for s in suggestions if s["id"] == int(suggestion_id)), None)
    if not s:
        await ctx.send(f"❌ Suggestion #{suggestion_id} not found.")
        return
    s["status"] = "denied"
    s["reviewed_by"] = str(ctx.author.id)
    s["review_reason"] = reason
    save_data(data)
    try:
        ch = ctx.guild.get_channel(s["channel_id"])
        if ch:
            msg = await ch.fetch_message(s["message_id"])
            embed = msg.embeds[0]
            embed.color = discord.Color.red()
            embed.set_footer(text=f"Status: ❌ Denied by {ctx.author} | {reason}" if reason else f"Status: ❌ Denied by {ctx.author}")
            await msg.edit(embed=embed)
    except Exception:
        pass
    await ctx.send(f"❌ Suggestion #{suggestion_id} has been **denied**.")


# ══════════════════════════════════════════════
#  BIRTHDAY SYSTEM
# ══════════════════════════════════════════════

@bot.command(name="birthday", aliases=["bday"])
async def birthday_cmd(ctx, action: str = None, *, date_str: str = None):
    """Manage birthdays. Usage: !birthday set MM/DD, !birthday remove, !birthday check @user"""
    if not action:
        await ctx.send("❌ Usage: `!birthday set MM/DD` | `!birthday remove` | `!birthday check @user` | `!birthday list`")
        return
    data = load_data()
    data.setdefault("birthdays", {})

    if action.lower() == "set":
        if not date_str:
            await ctx.send("❌ Usage: `!birthday set MM/DD`")
            return
        try:
            month, day = date_str.strip().split("/")
            month, day = int(month), int(day)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                raise ValueError
            data["birthdays"][str(ctx.author.id)] = {"month": month, "day": day}
            save_data(data)
            await ctx.send(f"🎂 Birthday set to **{month}/{day}**!")
        except ValueError:
            await ctx.send("❌ Invalid format. Use `MM/DD` (e.g., `12/25`).")

    elif action.lower() == "remove":
        data["birthdays"].pop(str(ctx.author.id), None)
        save_data(data)
        await ctx.send("✅ Birthday removed.")

    elif action.lower() == "check":
        target = date_str
        if target:
            member = await resolve_member(ctx, target.split()[0])
        else:
            member = ctx.author
        if not member:
            await ctx.send("❌ Member not found.")
            return
        bday = data["birthdays"].get(str(member.id))
        if bday:
            await ctx.send(f"🎂 **{member.display_name}**'s birthday is **{bday['month']}/{bday['day']}**!")
        else:
            await ctx.send(f"❌ **{member.display_name}** hasn't set their birthday.")

    elif action.lower() == "list":
        if not data["birthdays"]:
            await ctx.send("❌ No birthdays registered yet!")
            return
        now = datetime.utcnow()
        sorted_bdays = sorted(
            data["birthdays"].items(),
            key=lambda x: (x[1]["month"], x[1]["day"])
        )
        embed = discord.Embed(title="🎂 Birthday List", color=discord.Color.magenta(), timestamp=datetime.utcnow())
        lines = []
        for uid, bday in sorted_bdays:
            marker = " 🎉" if bday["month"] == now.month and bday["day"] == now.day else ""
            lines.append(f"<@{uid}> — **{bday['month']}/{bday['day']}**{marker}")
        embed.description = "\n".join(lines[:20])
        embed.set_footer(text=f"{len(data['birthdays'])} birthday(s) registered")
        await ctx.send(embed=embed)


@tasks.loop(hours=1)
async def birthday_check():
    """Check for birthdays and announce them."""
    data = load_data()
    now = datetime.utcnow()
    for guild in bot.guilds:
        bday_channel = discord.utils.get(guild.text_channels, name=BIRTHDAY_CHANNEL)
        if not bday_channel:
            bday_channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)
        if not bday_channel:
            continue
        for uid, bday in data.get("birthdays", {}).items():
            if bday["month"] == now.month and bday["day"] == now.day and now.hour == 0:
                member = guild.get_member(int(uid))
                if member:
                    embed = discord.Embed(
                        title="🎂🎉 Happy Birthday! 🎉🎂",
                        description=f"It's **{member.mention}**'s birthday today!\nWish them a great day! 🎁🎈",
                        color=discord.Color.magenta()
                    )
                    embed.set_thumbnail(url=member.display_avatar.url)
                    await bday_channel.send(embed=embed)

@birthday_check.before_loop
async def before_birthday_check():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════
#  TEMP ROLES
# ══════════════════════════════════════════════

@bot.command(name="temprole")
async def temprole_cmd(ctx, target: str = None, duration: str = None, *, role_name: str = None):
    """Assign a temporary role. Usage: !temprole @user <minutes> <RoleName>"""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not target or not duration or not role_name:
        await ctx.send("❌ Usage: `!temprole @user/ID <minutes> <RoleName>`")
        return
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.send("❌ Member not found.")
        return
    try:
        mins = int(duration)
    except ValueError:
        await ctx.send("❌ Duration must be a number (minutes).")
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.add_roles(role, reason=f"Temp role by {ctx.author} ({mins}m)")
    expire_at = datetime.utcnow() + timedelta(minutes=mins)
    data = load_data()
    data.setdefault("temp_roles", [])
    data["temp_roles"].append({
        "guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id,
        "expire_at": expire_at.isoformat(), "assigned_by": str(ctx.author.id)
    })
    save_data(data)
    await ctx.send(f"✅ Gave **{role.name}** to {member.mention} for **{mins} minutes**.")


@tasks.loop(seconds=60)
async def temprole_check():
    """Remove expired temp roles."""
    data = load_data()
    temp_roles = data.get("temp_roles", [])
    now = datetime.utcnow()
    remaining = []
    for tr in temp_roles:
        expire_at = datetime.fromisoformat(tr["expire_at"])
        if now >= expire_at:
            try:
                guild = bot.get_guild(tr["guild_id"])
                if guild:
                    member = guild.get_member(tr["user_id"])
                    role = guild.get_role(tr["role_id"])
                    if member and role and role in member.roles:
                        await member.remove_roles(role, reason="Temp role expired")
            except Exception:
                pass
        else:
            remaining.append(tr)
    if len(remaining) != len(temp_roles):
        data["temp_roles"] = remaining
        save_data(data)

@temprole_check.before_loop
async def before_temprole_check():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════
#  AUTO-RESPONDER COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="autorespond")
async def autorespond_cmd(ctx, action: str = None, trigger: str = None, *, response: str = None):
    """Manage auto-responses. Usage: !autorespond add <trigger> <response>"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can manage auto-responses.")
        return
    data = load_data()
    guild_id = str(ctx.guild.id)
    data.setdefault("auto_responses", {})
    data["auto_responses"].setdefault(guild_id, {})

    if not action or action.lower() == "list":
        responses = data["auto_responses"][guild_id]
        if not responses:
            await ctx.send("📝 No auto-responses set. Use `!autorespond add <trigger> <response>`")
            return
        embed = discord.Embed(title="🤖 Auto-Responses", color=discord.Color.blurple())
        for trigger, resp_data in responses.items():
            embed.add_field(
                name=f"Trigger: `{trigger}`",
                value=f"Response: {resp_data['response'][:100]}",
                inline=False
            )
        embed.set_footer(text=f"{len(responses)} response(s)")
        await ctx.send(embed=embed)
        return

    if action.lower() == "add":
        if not trigger or not response:
            await ctx.send("❌ Usage: `!autorespond add <trigger> <response>`")
            return
        data["auto_responses"][guild_id][trigger.lower()] = {
            "response": response,
            "created_by": str(ctx.author.id),
            "created_at": datetime.utcnow().isoformat()
        }
        save_data(data)
        await ctx.send(f"✅ Auto-response set! When someone says `{trigger}`, I'll respond with: {response}")

    elif action.lower() in ["remove", "delete"]:
        if not trigger:
            await ctx.send("❌ Usage: `!autorespond remove <trigger>`")
            return
        if trigger.lower() in data["auto_responses"][guild_id]:
            del data["auto_responses"][guild_id][trigger.lower()]
            save_data(data)
            await ctx.send(f"✅ Removed auto-response for `{trigger}`.")
        else:
            await ctx.send(f"❌ No auto-response found for `{trigger}`.")

    elif action.lower() == "clear":
        data["auto_responses"][guild_id] = {}
        save_data(data)
        await ctx.send("✅ Cleared all auto-responses.")


# ══════════════════════════════════════════════
#  MODMAIL COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="reply")
async def modmail_reply(ctx, *, message: str = None):
    """Reply to a modmail thread."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not ctx.channel.name.startswith("mail-"):
        await ctx.send("❌ This command only works in modmail channels.")
        return
    if not message:
        await ctx.send("❌ Usage: `!reply <message>`")
        return
    # Extract user from channel name
    username = ctx.channel.name[5:]  # Remove "mail-" prefix
    target_member = None
    for member in ctx.guild.members:
        if member.name.lower().replace(' ', '-') == username:
            target_member = member
            break
    if not target_member:
        await ctx.send("❌ Could not find the user for this modmail thread.")
        return
    embed = discord.Embed(
        title=f"📬 Reply from {ctx.guild.name}",
        description=message,
        color=discord.Color.blue(), timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text="Reply to this DM to continue the conversation")
    try:
        await target_member.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ Could not DM the user (DMs may be disabled).")
        return
    # Log in the modmail channel
    staff_embed = discord.Embed(
        description=message,
        color=discord.Color.blue(), timestamp=datetime.utcnow()
    )
    staff_embed.set_author(name=f"Staff: {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=staff_embed)
    await ctx.message.delete()


@bot.command(name="closemail")
async def close_modmail(ctx):
    """Close a modmail thread."""
    if not is_host_or_admin(ctx):
        await ctx.send("❌ You don't have permission.")
        return
    if not ctx.channel.name.startswith("mail-"):
        await ctx.send("❌ This command only works in modmail channels.")
        return
    username = ctx.channel.name[5:]
    target_member = None
    for member in ctx.guild.members:
        if member.name.lower().replace(' ', '-') == username:
            target_member = member
            break
    if target_member:
        try:
            await target_member.send(f"📬 Your modmail thread in **{ctx.guild.name}** has been closed by staff.")
        except Exception:
            pass
    await ctx.send("🔒 Closing modmail thread in 5 seconds...")
    await asyncio.sleep(5)
    await ctx.channel.delete(reason=f"Modmail closed by {ctx.author}")


@bot.command(name="modmailsetup")
async def modmail_setup(ctx):
    """Create the Modmail category for DM forwarding."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can set up modmail.")
        return
    category = discord.utils.get(ctx.guild.categories, name=MODMAIL_CATEGORY)
    if category:
        await ctx.send(f"✅ Modmail category already exists: **{category.name}**")
        return
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    host_role = discord.utils.get(ctx.guild.roles, name=HOST_ROLE_NAME)
    if host_role:
        overwrites[host_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    await ctx.guild.create_category(MODMAIL_CATEGORY, overwrites=overwrites)
    await ctx.send(f"✅ Modmail category **{MODMAIL_CATEGORY}** created! Members can now DM the bot to contact staff.")


# ══════════════════════════════════════════════
#  VOICE STATE LOGGING (listener)
# ══════════════════════════════════════════════

@bot.listen('on_voice_state_update')
async def voice_logger(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Log voice channel joins, leaves, and moves."""
    log_ch = discord.utils.get(member.guild.text_channels, name=LOG_CHANNEL)
    if not log_ch:
        return
    if before.channel is None and after.channel is not None:
        embed = discord.Embed(
            description=f"🔊 {member.mention} joined **{after.channel.name}**",
            color=discord.Color.green(), timestamp=datetime.utcnow()
        )
        await log_ch.send(embed=embed)
    elif before.channel is not None and after.channel is None:
        embed = discord.Embed(
            description=f"🔇 {member.mention} left **{before.channel.name}**",
            color=discord.Color.red(), timestamp=datetime.utcnow()
        )
        await log_ch.send(embed=embed)
    elif before.channel != after.channel:
        embed = discord.Embed(
            description=f"🔀 {member.mention} moved from **{before.channel.name}** to **{after.channel.name}**",
            color=discord.Color.orange(), timestamp=datetime.utcnow()
        )
        await log_ch.send(embed=embed)
    # Server mute/deafen logging
    if before.self_mute != after.self_mute:
        status = "muted" if after.self_mute else "unmuted"
        await log_ch.send(embed=discord.Embed(
            description=f"🎤 {member.mention} {status} themselves", color=discord.Color.greyple(), timestamp=datetime.utcnow()
        ))


# ══════════════════════════════════════════════
#  ROLE PERSISTENCE (save/restore on leave/join)
# ══════════════════════════════════════════════

@bot.listen('on_member_remove')
async def save_member_roles(member: discord.Member):
    """Save member roles when they leave for later restoration."""
    data = load_data()
    data.setdefault("role_persist", {})
    guild_id = str(member.guild.id)
    data["role_persist"].setdefault(guild_id, {})
    role_ids = [r.id for r in member.roles if r != member.guild.default_role and not r.managed]
    if role_ids:
        data["role_persist"][guild_id][str(member.id)] = {
            "roles": role_ids, "left_at": datetime.utcnow().isoformat()
        }
        save_data(data)


@bot.listen('on_member_join')
async def restore_member_roles(member: discord.Member):
    """Restore saved roles when a member rejoins."""
    data = load_data()
    guild_id = str(member.guild.id)
    saved = data.get("role_persist", {}).get(guild_id, {}).get(str(member.id))
    if saved:
        roles_to_add = []
        for role_id in saved["roles"]:
            role = member.guild.get_role(role_id)
            if role and not role.managed:
                roles_to_add.append(role)
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Role persistence: restoring saved roles")
                log_ch = discord.utils.get(member.guild.text_channels, name=LOG_CHANNEL)
                if log_ch:
                    role_names = ", ".join(r.name for r in roles_to_add)
                    await log_ch.send(f"🔄 Restored roles for {member.mention}: {role_names}")
            except Exception:
                pass
        # Remove from persist data
        del data["role_persist"][guild_id][str(member.id)]
        save_data(data)


# ══════════════════════════════════════════════
#  ANTI-RAID DETECTION (listener)
# ══════════════════════════════════════════════

@bot.listen('on_member_join')
async def anti_raid_check(member: discord.Member):
    """Detect mass joins and trigger anti-raid lockdown."""
    guild_id = member.guild.id
    now = datetime.utcnow()
    raid_tracker[guild_id] = [t for t in raid_tracker[guild_id] if (now - t).total_seconds() < ANTI_RAID_WINDOW]
    raid_tracker[guild_id].append(now)
    if len(raid_tracker[guild_id]) >= ANTI_RAID_THRESHOLD:
        raid_tracker[guild_id] = []
        log_ch = discord.utils.get(member.guild.text_channels, name=LOG_CHANNEL)
        # Enable verification level
        try:
            await member.guild.edit(verification_level=discord.VerificationLevel.highest)
            if log_ch:
                embed = discord.Embed(
                    title="🚨 ANTI-RAID ACTIVATED",
                    description=(
                        f"**{ANTI_RAID_THRESHOLD}+ members joined in {ANTI_RAID_WINDOW} seconds!**\n\n"
                        f"Verification level has been set to **HIGHEST**.\n"
                        f"Use `!raidmode off` to disable."
                    ),
                    color=discord.Color.dark_red(), timestamp=datetime.utcnow()
                )
                await log_ch.send("@everyone", embed=embed)
        except Exception:
            if log_ch:
                await log_ch.send("🚨 **RAID DETECTED** but I couldn't change verification level!")


@bot.command(name="raidmode")
async def raidmode_cmd(ctx, mode: str = None):
    """Toggle raid protection. Usage: !raidmode on/off"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only admins can manage raid mode.")
        return
    if not mode or mode.lower() not in ["on", "off"]:
        await ctx.send("❌ Usage: `!raidmode on/off`")
        return
    if mode.lower() == "on":
        await ctx.guild.edit(verification_level=discord.VerificationLevel.highest)
        await ctx.send("🚨 **Raid mode ON** — Verification set to HIGHEST.")
    else:
        await ctx.guild.edit(verification_level=discord.VerificationLevel.medium)
        await ctx.send("✅ **Raid mode OFF** — Verification set to MEDIUM.")


# ══════════════════════════════════════════════
#  AUTO-PUBLISH (announcement channels)
# ══════════════════════════════════════════════

@bot.listen('on_message')
async def auto_publish(message: discord.Message):
    """Automatically publish messages in announcement channels."""
    if message.author.bot:
        return
    if message.channel.type == discord.ChannelType.news:
        try:
            await message.publish()
        except Exception:
            pass


# ══════════════════════════════════════════════
#  SERVER STATS / ANALYTICS
# ══════════════════════════════════════════════

@bot.command(name="stats")
async def stats_cmd(ctx):
    """Show detailed server statistics."""
    guild = ctx.guild
    embed = discord.Embed(title=f"📊 {guild.name} Statistics", color=discord.Color.blurple(), timestamp=datetime.utcnow())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    # Members
    total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots
    online = sum(1 for m in guild.members if m.status == discord.Status.online)
    idle = sum(1 for m in guild.members if m.status == discord.Status.idle)
    dnd = sum(1 for m in guild.members if m.status == discord.Status.dnd)
    offline = sum(1 for m in guild.members if m.status == discord.Status.offline)

    embed.add_field(name="👥 Members", value=(
        f"Total: **{total}** (Humans: {humans}, Bots: {bots})\n"
        f"🟢 {online} 🟡 {idle} 🔴 {dnd} ⚫ {offline}"
    ), inline=False)

    # Channels
    text = len(guild.text_channels)
    voice = len(guild.voice_channels)
    categories = len(guild.categories)
    forums = len([c for c in guild.channels if isinstance(c, discord.ForumChannel)])
    embed.add_field(name="💬 Channels", value=(
        f"Text: {text} | Voice: {voice} | Categories: {categories} | Forums: {forums}"
    ), inline=False)

    # Roles and emojis
    embed.add_field(name="🎭 Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="😀 Emojis", value=f"{len(guild.emojis)}/{guild.emoji_limit}", inline=True)
    embed.add_field(name="🎨 Stickers", value=f"{len(guild.stickers)}/{guild.sticker_limit}", inline=True)

    # Boost info
    embed.add_field(name="🚀 Boosts", value=(
        f"Level {guild.premium_tier} ({guild.premium_subscription_count} boosts)"
    ), inline=True)

    # Bot stats
    data = load_data()
    guild_id = str(guild.id)
    total_cases = len(data.get("cases", {}).get(guild_id, []))
    total_suggestions = len(data.get("suggestions", {}).get(guild_id, []))
    total_economy_users = len(data.get("economy", {}))
    total_verified = len(data.get("verified", {}))

    embed.add_field(name="📋 Bot Data", value=(
        f"Mod Cases: {total_cases} | Suggestions: {total_suggestions}\n"
        f"Economy Users: {total_economy_users} | Verified: {total_verified}"
    ), inline=False)

    # Server age
    embed.add_field(name="📅 Created", value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="🆔 Server ID", value=guild.id, inline=True)

    await ctx.send(embed=embed)


@bot.command(name="channelstats", aliases=["chstats"])
async def channelstats(ctx):
    """Show channel statistics."""
    guild = ctx.guild
    text = len(guild.text_channels)
    voice = len(guild.voice_channels)
    total = len(guild.channels)
    embed = discord.Embed(title="📊 Channel Statistics", color=discord.Color.blurple())
    embed.add_field(name="💬 Text Channels", value=text, inline=True)
    embed.add_field(name="🔊 Voice Channels", value=voice, inline=True)
    embed.add_field(name="📁 Categories", value=len(guild.categories), inline=True)
    embed.add_field(name="📊 Total", value=total, inline=True)
    # Find most popular voice channel
    most_members = 0
    popular_vc = None
    for vc in guild.voice_channels:
        if len(vc.members) > most_members:
            most_members = len(vc.members)
            popular_vc = vc
    if popular_vc:
        embed.add_field(name="🔥 Most Active VC", value=f"{popular_vc.name} ({most_members} members)", inline=False)
    await ctx.send(embed=embed)


# ══════════════════════════════════════════════
#  INTERACTIVE HELP MENU (dropdown)
# ══════════════════════════════════════════════

class HelpDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="🎮 Heist Queue", value="heist", description="Queue management for GTA heists"),
            discord.SelectOption(label="✅ Verification", value="verify", description="Social Club verification"),
            discord.SelectOption(label="🛡️ Moderation", value="mod", description="Ban, kick, mute, warn & more"),
            discord.SelectOption(label="🤖 Auto-Mod", value="automod", description="Auto-moderation filters"),
            discord.SelectOption(label="📋 Mod Logs", value="modlogs", description="Case system & mod logging"),
            discord.SelectOption(label="💰 Economy", value="economy", description="GTA-themed economy system"),
            discord.SelectOption(label="📊 Leveling", value="leveling", description="XP and level system"),
            discord.SelectOption(label="⭐ Starboard", value="starboard", description="Star messages system"),
            discord.SelectOption(label="💡 Suggestions", value="suggest", description="Suggestion system"),
            discord.SelectOption(label="🎂 Birthday", value="birthday", description="Birthday tracking"),
            discord.SelectOption(label="🎉 Fun & Utility", value="fun", description="Games, tools & more"),
            discord.SelectOption(label="⚙️ Management", value="manage", description="Server management tools"),
            discord.SelectOption(label="📬 Modmail", value="modmail", description="DM-based modmail system"),
            discord.SelectOption(label="ℹ️ Info Commands", value="info", description="Server and user info"),
        ]
        super().__init__(placeholder="📖 Choose a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        embeds = {
            "heist": discord.Embed(
                title="🎮 Heist Queue Commands",
                description=(
                    "`/queue open` — Open a heist queue with join button\n"
                    "`/queue start` — Start the heist with current players\n"
                    "`/queue view` — View the current queue\n"
                    "`/queue clear` — Clear the queue\n"
                    "`/queue kick @user` — Remove a player\n\n"
                    "*Players join via the interactive button!*"
                ), color=discord.Color.blue()
            ),
            "verify": discord.Embed(
                title="✅ Verification Commands",
                description=(
                    "`/verify <social_club>` — Verify with Social Club\n"
                    "`/forceverify @user <sc>` — [Host] Manual verify\n"
                    "`/unverify @user` — [Host] Remove verification\n"
                    "`/whois @user` — Look up Social Club name"
                ), color=discord.Color.green()
            ),
            "mod": discord.Embed(
                title="🛡️ Moderation Commands",
                description=(
                    "`!ban @user [reason]` — Ban (sends DM)\n"
                    "`!unban ID [reason]` — Unban a user\n"
                    "`!tempban @user <mins> [reason]` — ⏱️ Temporary ban\n"
                    "`!softban @user [reason]` — Ban+unban (clear msgs)\n"
                    "`!massban id1 id2 ...` — Ban multiple users\n"
                    "`!kick @user [reason]` — Kick (sends DM)\n"
                    "`!mute / !unmute @user` — Mute/unmute\n"
                    "`!timeout @user <mins>` — Timeout\n"
                    "`!untimeout @user` — Remove timeout\n"
                    "`!warn @user [reason]` — Issue a warning\n"
                    "`!warnings @user` — View warnings\n"
                    "`!clearwarnings @user` — Clear warnings\n"
                    "`!clear <amount>` — Delete messages\n"
                    "`!lock / !unlock` — Lock/unlock channel\n"
                    "`!slowmode <secs>` — Set slowmode\n"
                    "`!nuke` — Reset channel\n"
                    "`!raidmode on/off` — Anti-raid toggle\n"
                    "`!temprole @user <mins> <Role>` — ⏱️ Temp role"
                ), color=discord.Color.red()
            ),
            "automod": discord.Embed(
                title="🤖 Auto-Mod Commands",
                description=(
                    "`!automod` — View current settings\n"
                    "`!automod word_filter on/off`\n"
                    "`!automod invite_filter on/off`\n"
                    "`!automod link_filter on/off`\n"
                    "`!automod caps_filter on/off`\n"
                    "`!automod mention_filter on/off`\n"
                    "`!automod emoji_filter on/off`\n"
                    "`!automod max_mentions <n>`\n"
                    "`!automod max_emojis <n>`\n"
                    "`!blockedwords add/remove/list/clear`\n\n"
                    "*Admins are exempt from all filters.*"
                ), color=discord.Color.orange()
            ),
            "modlogs": discord.Embed(
                title="📋 Mod Case Log Commands",
                description=(
                    "`!case <number>` — View a specific case\n"
                    "`!modlogs @user` — View all cases for a user\n\n"
                    "*Cases are auto-created for bans, kicks, warns, etc.*\n"
                    f"*Logged to `#{MODLOG_CHANNEL}` channel.*"
                ), color=discord.Color.purple()
            ),
            "economy": discord.Embed(
                title="💰 Economy Commands",
                description=(
                    "`!balance` / `!bal` — Check balance\n"
                    "`!daily` — Daily reward (5K-25K)\n"
                    "`!work` — Work a GTA job (5min CD)\n"
                    "`!crime` — Risky crime (10min CD, 55%)\n"
                    "`!gamble <amount>` — Casino slots 🎰\n"
                    "`!rob @user` — Rob someone (15min CD, 40%)\n"
                    "`!pay @user <amount>` — Transfer money\n"
                    "`!deposit / !withdraw <amount>` — Banking\n"
                    "`!shop` — Browse GTA shop\n"
                    "`!buy <item>` — Buy from shop\n"
                    "`!inventory` — View your items\n"
                    "`!richest` — Money leaderboard"
                ), color=discord.Color.gold()
            ),
            "leveling": discord.Embed(
                title="📊 Leveling Commands",
                description=(
                    "`!level` / `!rank` — Check your level\n"
                    "`!leaderboard` / `!lb` — XP leaderboard\n"
                    "`!setlevel @user <level>` — [Admin] Set level\n\n"
                    "*Earn 15-25 XP per message (60s cooldown)*"
                ), color=discord.Color.blurple()
            ),
            "starboard": discord.Embed(
                title="⭐ Starboard",
                description=(
                    f"React with {STARBOARD_EMOJI} on any message.\n"
                    f"When it reaches **{STARBOARD_THRESHOLD}** stars, it's pinned to `#{STARBOARD_CHANNEL}`!\n\n"
                    "`!starboard threshold <n>` — Change threshold"
                ), color=discord.Color.gold()
            ),
            "suggest": discord.Embed(
                title="💡 Suggestion Commands",
                description=(
                    "`!suggest <idea>` — Submit a suggestion\n"
                    "`!approve <id> [reason]` — Approve suggestion\n"
                    "`!deny <id> [reason]` — Deny suggestion\n\n"
                    f"*Posted to `#{SUGGESTION_CHANNEL}` channel.*"
                ), color=discord.Color.blurple()
            ),
            "birthday": discord.Embed(
                title="🎂 Birthday Commands",
                description=(
                    "`!birthday set MM/DD` — Set your birthday\n"
                    "`!birthday remove` — Remove your birthday\n"
                    "`!birthday check @user` — Check someone's\n"
                    "`!birthday list` — List all birthdays\n\n"
                    f"*Announced daily in `#{BIRTHDAY_CHANNEL}`*"
                ), color=discord.Color.magenta()
            ),
            "fun": discord.Embed(
                title="🎉 Fun & Utility Commands",
                description=(
                    "`!poll <question>` / `!poll Q | A | B` — Create polls\n"
                    "`!giveaway <mins> <winners> <prize>` — Giveaways\n"
                    "`!8ball <question>` — Magic 8-ball\n"
                    "`!roll [sides]` — Roll dice\n"
                    "`!coinflip` — Flip a coin\n"
                    "`!choose opt1 | opt2` — Random choice\n"
                    "`!calc <expr>` — Calculator\n"
                    "`!countdown <secs>` — Countdown timer\n"
                    "`!remind <time> <msg>` — Set reminder\n"
                    "`!snipe` / `!editsnipe` — Deleted/edited msgs"
                ), color=discord.Color.purple()
            ),
            "manage": discord.Embed(
                title="⚙️ Management Commands",
                description=(
                    "`!afk [reason]` — Set AFK status\n"
                    "`!nick @user <name>` — Change nickname\n"
                    "`!addrole / !removerole @user <Role>` — Roles\n"
                    "`!roleinfo <Role>` — Role information\n"
                    "`!reactionrole <msgID> <emoji> <Role>`\n"
                    "`!ticketpanel` — Create ticket system\n"
                    "`!addcmd / !delcmd / !listcmds` — Custom commands\n"
                    "`!autorespond add/remove/list` — Auto-responses\n"
                    "`!announce #ch <msg>` — Announcements\n"
                    "`!say / !embed` — Bot messages\n"
                    "`!note add/list/clear @user` — Staff notes"
                ), color=discord.Color.dark_grey()
            ),
            "modmail": discord.Embed(
                title="📬 Modmail Commands",
                description=(
                    "**Members:** DM the bot to contact staff\n\n"
                    "`!modmailsetup` — Create modmail category\n"
                    "`!reply <message>` — Reply to modmail thread\n"
                    "`!closemail` — Close modmail thread\n\n"
                    "*Messages are forwarded to a private staff channel.*"
                ), color=discord.Color.blue()
            ),
            "info": discord.Embed(
                title="ℹ️ Info Commands",
                description=(
                    "`!serverinfo` — Server information\n"
                    "`!userinfo @user` — User information\n"
                    "`!avatar @user` — Show avatar\n"
                    "`!banner @user` — Show banner\n"
                    "`!roleinfo <Role>` — Role info\n"
                    "`!membercount` — Member count\n"
                    "`!stats` — Detailed server stats\n"
                    "`!channelstats` — Channel statistics\n"
                    "`!ping` — Bot latency\n"
                    "`!uptime` — Bot uptime\n"
                    "`!invite` — Bot invite link"
                ), color=discord.Color.blurple()
            ),
        }
        embed = embeds.get(v, discord.Embed(title="Unknown", description="Category not found."))
        embed.set_footer(text="Use ! or ? prefix | Slash commands also available")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpDropdown())


@bot.command(name="help")
async def help_cmd(ctx):
    """Interactive help menu with all bot features."""
    embed = discord.Embed(
        title="📖 GTA Heist Bot — Command Center",
        description=(
            "**Your all-in-one Discord server manager!**\n\n"
            "🎮 Heist queue management with Social Club verification\n"
            "🛡️ Full moderation suite with case logging\n"
            "🤖 Advanced auto-mod with word/link/invite/caps/emoji filters\n"
            "💰 GTA-themed economy with casino, jobs & shop\n"
            "📊 XP leveling system with leaderboards\n"
            "⭐ Starboard, 💡 Suggestions, 🎂 Birthdays\n"
            "📬 Modmail DM system, 🎫 Tickets\n"
            "🚨 Anti-raid protection & role persistence\n\n"
            "**Select a category below to explore commands! ▼**"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Total: 85+ commands | Prefix: ! or ? | Made with ❤️")
    if ctx.guild and ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    await ctx.send(embed=embed, view=HelpView())


@bot.command(name="modhelp")
async def modhelp(ctx):
    """Quick mod command reference."""
    embed = discord.Embed(title="🛡️ Quick Mod Reference", color=discord.Color.red(), timestamp=datetime.utcnow())
    embed.add_field(name="🔨 Bans", value="`!ban` `!unban` `!tempban` `!softban` `!massban`", inline=False)
    embed.add_field(name="🦵 Kick", value="`!kick`", inline=False)
    embed.add_field(name="🔇 Mute", value="`!mute` `!unmute` `!timeout` `!untimeout`", inline=False)
    embed.add_field(name="⚠️ Warnings", value="`!warn` `!warnings` `!clearwarnings`", inline=False)
    embed.add_field(name="📋 Cases", value="`!case <#>` `!modlogs @user`", inline=False)
    embed.add_field(name="🧹 Messages", value="`!clear` `!nuke`", inline=False)
    embed.add_field(name="🔒 Channel", value="`!lock` `!unlock` `!slowmode`", inline=False)
    embed.add_field(name="🛡️ Auto-Mod", value="`!automod` `!blockedwords`", inline=False)
    embed.add_field(name="🚨 Raid", value="`!raidmode on/off`", inline=False)
    embed.add_field(name="⏱️ Temp Role", value="`!temprole @user <mins> <Role>`", inline=False)
    embed.add_field(name="👤 Member", value="`!nick` `!addrole` `!removerole` `!note`", inline=False)
    embed.set_footer(text="All commands work with ! and ? prefixes")
    await ctx.send(embed=embed)


# ══════════════════════════════════════════════
#  BOT READY (set uptime tracker)
# ══════════════════════════════════════════════

@bot.event
async def on_ready():
    bot.ready_time = datetime.utcnow()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Cache invites
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except Exception:
            pass

    # Start all background tasks
    for task in [reminder_check, tempban_check, birthday_check, temprole_check]:
        if not task.is_running():
            task.start()

    # Register persistent views
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())

    print("──────────────────────────────────────")
    print(f"  Servers: {len(bot.guilds)}")
    print(f"  Users:   {sum(g.member_count for g in bot.guilds)}")
    print(f"  Commands: {len(bot.commands)}+")
    print("──────────────────────────────────────")

# ── RUN ───────────────────────────────────────

async def main():
    await start_web_server()
    bot.tree.add_command(queue_group)
    await bot.start(BOT_TOKEN)

asyncio.run(main())

