import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
from aiohttp import web
import asyncio
from datetime import datetime

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
VERIFIED_ROLE_NAME = "Verified"
HOST_ROLE_NAME = "Heist Host"
VERIFY_LOG_CHANNEL = "verify-log"
QUEUE_CHANNEL = "heist-queue"
MAX_QUEUE_SIZE = 3
DATA_FILE = "bot_data.json"
PORT = int(os.environ.get("PORT", 8080))
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=["!", "?"], intents=intents)

# ── Web server for UptimeRobot ────────────────

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
    return {"verified": {}, "queue": [], "session_active": False}

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

# ── Helper: resolve member by mention or ID ───

async def resolve_member(ctx, target: str):
    # Try mention
    if ctx.message.mentions:
        return ctx.message.mentions[0]
    # Try user ID
    try:
        uid = int(target.strip())
        member = ctx.guild.get_member(uid)
        if member:
            return member
        # Try fetching if not in cache
        member = await ctx.guild.fetch_member(uid)
        return member
    except Exception:
        return None

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
#  MODERATION - PREFIX COMMANDS (! and ?)
# ══════════════════════════════════════════════

def is_host_or_admin(ctx):
    return (
        discord.utils.get(ctx.author.roles, name=HOST_ROLE_NAME) is not None
        or ctx.author.guild_permissions.administrator
    )

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
    if member == ctx.author:
        await ctx.send("❌ You can't ban yourself.")
        return
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
    if member == ctx.author:
        await ctx.send("❌ You can't kick yourself.")
        return
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
    # Find or create Muted role
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role:
        muted_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, speak=False)
    if muted_role in member.roles:
        await ctx.send(f"⚠️ **{member}** is already muted.")
        return
    await member.add_roles(muted_role, reason=f"{reason} | Muted by {ctx.author}")
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
    await ctx.send(f"🔊 **{member}** has been unmuted.")

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
    try:
        await member.send(f"⚠️ You have been warned in **{ctx.guild.name}**.\nReason: `{reason}`")
    except Exception:
        pass
    await ctx.send(f"⚠️ **{member}** has been warned. Reason: `{reason}`")

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

@bot.command(name="modhelp")
async def modhelp(ctx):
    embed = discord.Embed(title="🛡️ Mod Commands", color=discord.Color.blue())
    embed.add_field(name="!ban / ?ban @user/ID [reason]", value="Ban a member", inline=False)
    embed.add_field(name="!unban / ?unban USER_ID [reason]", value="Unban a member", inline=False)
    embed.add_field(name="!kick / ?kick @user/ID [reason]", value="Kick a member", inline=False)
    embed.add_field(name="!mute / ?mute @user/ID [reason]", value="Mute a member", inline=False)
    embed.add_field(name="!unmute / ?unmute @user/ID", value="Unmute a member", inline=False)
    embed.add_field(name="!warn / ?warn @user/ID [reason]", value="Warn a member", inline=False)
    embed.add_field(name="!clear / ?clear [amount]", value="Delete messages (max 100)", inline=False)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════
#  VERIFICATION SYSTEM
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
        f"✅ Verified! Welcome **{social_club_name}**!\nUse `/queue join` to join a heist queue.",
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


# ══════════════════════════════════════════════
#  QUEUE SYSTEM
# ══════════════════════════════════════════════

def is_verified_user(user_id):
    return str(user_id) in load_data()["verified"]

def get_sc(user_id):
    return load_data()["verified"].get(str(user_id), {}).get("social_club", "Unknown")

async def post_queue_update(guild, data):
    channel = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL)
    if not channel:
        return
    queue = data["queue"]
    embed = discord.Embed(
        title="🎮 GTA Online Heist Queue",
        color=discord.Color.green() if data["session_active"] else discord.Color.blue(),
        timestamp=datetime.utcnow()
    )
    embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "Queue is empty. Use `/queue join` to get in line!"
    embed.add_field(name="Status", value="🟢 Session Active" if data["session_active"] else "🔴 Waiting", inline=True)
    embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.set_footer(text="Join with /queue join • Leave with /queue leave")
    await channel.send(embed=embed)

queue_group = app_commands.Group(name="queue", description="Heist queue commands")

@queue_group.command(name="join", description="Join the heist queue")
async def queue_join(interaction: discord.Interaction):
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
    await interaction.response.send_message(f"✅ Joined queue as **{sc}**! Position: **{len(data['queue'])}**", ephemeral=True)
    await post_queue_update(interaction.guild, data)

@queue_group.command(name="leave", description="Leave the heist queue")
async def queue_leave(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_data()
    before = len(data["queue"])
    data["queue"] = [e for e in data["queue"] if e["user_id"] != user_id]
    if len(data["queue"]) == before:
        await interaction.response.send_message("⚠️ You're not in the queue.", ephemeral=True)
        return
    save_data(data)
    await interaction.response.send_message("👋 You left the queue.", ephemeral=True)
    await post_queue_update(interaction.guild, data)

@queue_group.command(name="view", description="View the current heist queue")
async def queue_view(interaction: discord.Interaction):
    data = load_data()
    queue = data["queue"]
    embed = discord.Embed(title="🎮 GTA Online Heist Queue", color=discord.Color.blue(), timestamp=datetime.utcnow())
    embed.description = "\n".join([f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]) if queue else "Queue is empty!"
    embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.add_field(name="Status", value="🟢 Active" if data["session_active"] else "🔴 Waiting", inline=True)
    await interaction.response.send_message(embed=embed)

@queue_group.command(name="start", description="[Host] Start the heist session")
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
    await interaction.response.send_message(f"🚀 **Heist session started!**\nPlayers: {mentions}\nHost: {interaction.user.mention}\n\nGet in the lobby!")

@queue_group.command(name="clear", description="[Host] Clear the queue and end session")
async def queue_clear(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only hosts can clear the queue.", ephemeral=True)
        return
    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)
    await interaction.response.send_message("🧹 Queue cleared and session ended.")
    await post_queue_update(interaction.guild, data)

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
    await interaction.response.send_message(f"🦵 {member.mention} was removed from the queue.")
    await post_queue_update(interaction.guild, data)

# ── WHOIS ─────────────────────────────────────

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

# ── RUN ───────────────────────────────────────

async def main():
    await start_web_server()
    bot.tree.add_command(queue_group)
    await bot.start(BOT_TOKEN)

asyncio.run(main())
