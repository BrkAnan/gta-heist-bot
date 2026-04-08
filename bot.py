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

# ── Bot ready ─────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Cache invites for invite tracking
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except Exception:
            pass

    # Start background tasks
    if not reminder_check.is_running():
        reminder_check.start()

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
#  AFK SYSTEM + XP + ANTI-SPAM
# ══════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    data = load_data()
    user_id = str(message.author.id)

    # ── Anti-spam check ──
    if message.guild:
        now = datetime.utcnow()
        spam_tracker[user_id] = [
            t for t in spam_tracker[user_id]
            if (now - t).total_seconds() < 5
        ]
        spam_tracker[user_id].append(now)
        if len(spam_tracker[user_id]) >= 5:
            # 5 messages in 5 seconds = spam
            try:
                muted_role = discord.utils.get(message.guild.roles, name="Muted")
                if muted_role and muted_role not in message.author.roles:
                    await message.author.add_roles(muted_role, reason="Auto-mute: spam detected")
                    await message.channel.send(
                        f"🤖 {message.author.mention} has been auto-muted for spamming. "
                        f"A moderator can unmute with `!unmute`."
                    )
                    spam_tracker[user_id] = []
                    # Auto-unmute after 60 seconds
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
#  HELP COMMANDS
# ══════════════════════════════════════════════

@bot.command(name="modhelp")
async def modhelp(ctx):
    embed = discord.Embed(title="🛡️ Mod Commands", color=discord.Color.blue())
    embed.add_field(name="!ban @user/ID [reason]", value="Ban a member (sends DM)", inline=False)
    embed.add_field(name="!unban ID [reason]", value="Unban a user", inline=False)
    embed.add_field(name="!kick @user/ID [reason]", value="Kick a member (sends DM)", inline=False)
    embed.add_field(name="!mute @user/ID [reason]", value="Mute a member (sends DM)", inline=False)
    embed.add_field(name="!unmute @user/ID", value="Unmute a member", inline=False)
    embed.add_field(name="!timeout @user/ID <mins> [reason]", value="Timeout a member (sends DM)", inline=False)
    embed.add_field(name="!untimeout @user/ID", value="Remove timeout", inline=False)
    embed.add_field(name="!warn @user/ID [reason]", value="Warn a member (sends DM, tracked)", inline=False)
    embed.add_field(name="!warnings @user/ID", value="View warnings", inline=False)
    embed.add_field(name="!clearwarnings @user/ID", value="Clear all warnings", inline=False)
    embed.add_field(name="!clear [amount]", value="Delete messages (max 100)", inline=False)
    embed.add_field(name="!lock [reason] / !unlock", value="Lock/unlock channel", inline=False)
    embed.add_field(name="!slowmode <seconds>", value="Set channel slowmode", inline=False)
    embed.add_field(name="!nuke", value="Clone & delete channel (admin only)", inline=False)
    embed.add_field(name="!nick @user nickname", value="Change nickname", inline=False)
    embed.add_field(name="!addrole / !removerole", value="Add/remove roles", inline=False)
    embed.add_field(name="!note add/list/clear @user", value="Manage staff notes", inline=False)
    embed.set_footer(text="All commands work with ! and ? prefixes")
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="📖 Bot Commands",
        description="Here's everything I can do!",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )

    embed.add_field(
        name="🎮 Heist Queue",
        value="`/queue open` `/queue start` `/queue view` `/queue clear` `/queue kick`\nJoin via button!",
        inline=False
    )
    embed.add_field(
        name="✅ Verification",
        value="`/verify` `/forceverify` `/unverify` `/whois`",
        inline=False
    )
    embed.add_field(
        name="🛡️ Moderation",
        value="`!modhelp` for full list\n`!ban` `!kick` `!mute` `!timeout` `!warn` `!clear` `!lock`",
        inline=False
    )
    embed.add_field(
        name="📊 Leveling",
        value="`!level` `!leaderboard` `!setlevel`",
        inline=False
    )
    embed.add_field(
        name="🎉 Fun & Utility",
        value="`!poll` `!giveaway` `!8ball` `!roll` `!coinflip` `!choose` `!calc` `!countdown`",
        inline=False
    )
    embed.add_field(
        name="ℹ️ Info",
        value="`!serverinfo` `!userinfo` `!avatar` `!banner` `!roleinfo` `!membercount` `!ping` `!uptime`",
        inline=False
    )
    embed.add_field(
        name="💬 Messages",
        value="`!snipe` `!editsnipe` `!announce` `!say` `!embed`",
        inline=False
    )
    embed.add_field(
        name="⚙️ Management",
        value="`!afk` `!remind` `!reactionrole` `!ticketpanel` `!addcmd` `!delcmd` `!listcmds`",
        inline=False
    )
    embed.set_footer(text="Use ! or ? prefix | Slash commands also available")
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

    if not reminder_check.is_running():
        reminder_check.start()

    print("──────────────────────────────────────")
    print(f"  Servers: {len(bot.guilds)}")
    print(f"  Users:   {sum(g.member_count for g in bot.guilds)}")
    print("──────────────────────────────────────")

# ── RUN ───────────────────────────────────────

async def main():
    await start_web_server()
    bot.tree.add_command(queue_group)
    await bot.start(BOT_TOKEN)

asyncio.run(main())
