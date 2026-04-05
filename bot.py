import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime

# ──────────────────────────────────────────────
#  CONFIG — change these values
# ──────────────────────────────────────────────
BOT_TOKEN = ""
VERIFIED_ROLE_NAME = "Verified"          # Role given after verification
HOST_ROLE_NAME = "Heist Host"            # Role allowed to manage queue/sessions
VERIFY_LOG_CHANNEL = "verify-log"        # Channel where verify requests appear
QUEUE_CHANNEL = "heist-queue"            # Channel where queue updates are posted
MAX_QUEUE_SIZE = 3                        # Max players per heist (excluding host)
DATA_FILE = "bot_data.json"
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Data helpers ──────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"verified": {}, "queue": [], "session_active": False, "pending": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

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
#  VERIFICATION SYSTEM
# ══════════════════════════════════════════════

@bot.tree.command(name="verify", description="Verify yourself with your Social Club name")
@app_commands.describe(social_club_name="Your Rockstar Social Club username")
async def verify(interaction: discord.Interaction, social_club_name: str):
    data = load_data()
    user_id = str(interaction.user.id)

    # Already verified?
    if user_id in data["verified"]:
        sc = data["verified"][user_id]["social_club"]
        await interaction.response.send_message(
            f"✅ You're already verified as **{sc}**.", ephemeral=True
        )
        return

    # Pending request?
    if user_id in data["pending"]:
        await interaction.response.send_message(
            "⏳ You already have a pending verification request. Please wait for a host to approve it.",
            ephemeral=True
        )
        return

    # Save pending
    data["pending"][user_id] = {
        "social_club": social_club_name,
        "discord_tag": str(interaction.user),
        "requested_at": datetime.utcnow().isoformat()
    }
    save_data(data)

    await interaction.response.send_message(
        f"📨 Your verification request for Social Club name **{social_club_name}** has been submitted!\n"
        "A host will approve it shortly.",
        ephemeral=True
    )

    # Post to verify-log channel
    log_channel = discord.utils.get(interaction.guild.text_channels, name=VERIFY_LOG_CHANNEL)
    if log_channel:
        embed = discord.Embed(
            title="🔐 New Verification Request",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="Social Club", value=social_club_name, inline=True)
        embed.set_footer(text=f"User ID: {interaction.user.id}")
        await log_channel.send(
            embed=embed,
            view=VerifyButtons(user_id=user_id, social_club=social_club_name)
        )


class VerifyButtons(discord.ui.View):
    def __init__(self, user_id: str, social_club: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.social_club = social_club

    def is_host(self, interaction: discord.Interaction):
        return discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME) is not None

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_host(interaction):
            await interaction.response.send_message("❌ Only hosts can approve verifications.", ephemeral=True)
            return

        data = load_data()
        guild = interaction.guild
        member = guild.get_member(int(self.user_id))

        if not member:
            await interaction.response.send_message("❌ Member not found in server.", ephemeral=True)
            return

        # Move from pending to verified
        data["verified"][self.user_id] = {
            "social_club": self.social_club,
            "discord_tag": str(member),
            "verified_by": str(interaction.user),
            "verified_at": datetime.utcnow().isoformat()
        }
        data["pending"].pop(self.user_id, None)
        save_data(data)

        # Assign role
        role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if role:
            await member.add_roles(role)

        await member.send(
            f"✅ You've been **verified** in **{guild.name}**!\n"
            f"Social Club: **{self.social_club}**\n"
            f"You can now join heist queues. Use `/queue join` in the server."
        )

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Verified",
                description=f"{member.mention} approved as **{self.social_club}** by {interaction.user.mention}",
                color=discord.Color.green()
            ),
            view=None
        )

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_host(interaction):
            await interaction.response.send_message("❌ Only hosts can reject verifications.", ephemeral=True)
            return

        data = load_data()
        data["pending"].pop(self.user_id, None)
        save_data(data)

        guild = interaction.guild
        member = guild.get_member(int(self.user_id))
        if member:
            await member.send(
                f"❌ Your verification request for **{self.social_club}** was rejected in **{guild.name}**.\n"
                "Please make sure the name matches your Rockstar Social Club account exactly and try again."
            )

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Rejected",
                description=f"Verification for **{self.social_club}** rejected by {interaction.user.mention}",
                color=discord.Color.red()
            ),
            view=None
        )


# ══════════════════════════════════════════════
#  QUEUE SYSTEM
# ══════════════════════════════════════════════

def is_verified(user_id: str) -> bool:
    data = load_data()
    return str(user_id) in data["verified"]

def get_sc(user_id: str) -> str:
    data = load_data()
    return data["verified"].get(str(user_id), {}).get("social_club", "Unknown")

async def post_queue_update(guild: discord.Guild, data: dict):
    channel = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL)
    if not channel:
        return

    queue = data["queue"]
    active = data["session_active"]

    embed = discord.Embed(
        title="🎮 GTA Online Heist Queue",
        color=discord.Color.blue() if not active else discord.Color.green(),
        timestamp=datetime.utcnow()
    )

    if not queue:
        embed.description = "Queue is empty. Use `/queue join` to get in line!"
    else:
        lines = []
        for i, entry in enumerate(queue):
            lines.append(f"`{i+1}.` <@{entry['user_id']}> — **{entry['social_club']}**")
        embed.description = "\n".join(lines)

    embed.add_field(name="Status", value="🟢 Session Active" if active else "🔴 Waiting", inline=True)
    embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.set_footer(text="Join with /queue join • Leave with /queue leave")

    await channel.send(embed=embed)


queue_group = app_commands.Group(name="queue", description="Heist queue commands")

@queue_group.command(name="join", description="Join the heist queue")
async def queue_join(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    if not is_verified(user_id):
        await interaction.response.send_message(
            "❌ You need to verify first! Use `/verify <your_social_club_name>`.", ephemeral=True
        )
        return

    data = load_data()

    if any(e["user_id"] == user_id for e in data["queue"]):
        await interaction.response.send_message("⚠️ You're already in the queue!", ephemeral=True)
        return

    if len(data["queue"]) >= MAX_QUEUE_SIZE:
        await interaction.response.send_message(
            f"🚫 Queue is full ({MAX_QUEUE_SIZE}/{MAX_QUEUE_SIZE}). Try again later!", ephemeral=True
        )
        return

    sc = get_sc(user_id)
    data["queue"].append({"user_id": user_id, "social_club": sc, "joined_at": datetime.utcnow().isoformat()})
    save_data(data)

    await interaction.response.send_message(
        f"✅ You joined the queue as **{sc}**! Position: **{len(data['queue'])}**", ephemeral=True
    )
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

    if not queue:
        embed.description = "Queue is empty!"
    else:
        lines = [f"`{i+1}.` <@{e['user_id']}> — **{e['social_club']}**" for i, e in enumerate(queue)]
        embed.description = "\n".join(lines)

    embed.add_field(name="Spots", value=f"{len(queue)}/{MAX_QUEUE_SIZE}", inline=True)
    embed.add_field(name="Status", value="🟢 Active" if data["session_active"] else "🔴 Waiting", inline=True)

    await interaction.response.send_message(embed=embed)


@queue_group.command(name="start", description="[Host] Start the heist session with current queue")
async def queue_start(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME):
        await interaction.response.send_message("❌ Only hosts can start sessions.", ephemeral=True)
        return

    data = load_data()
    if not data["queue"]:
        await interaction.response.send_message("⚠️ Queue is empty.", ephemeral=True)
        return

    data["session_active"] = True
    save_data(data)

    mentions = " ".join(f"<@{e['user_id']}>" for e in data["queue"])
    await interaction.response.send_message(
        f"🚀 **Heist session started!**\nPlayers: {mentions}\n"
        f"Host: {interaction.user.mention}\n\nGet in the lobby! 🎮"
    )


@queue_group.command(name="clear", description="[Host] Clear the queue and end the session")
async def queue_clear(interaction: discord.Interaction):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME):
        await interaction.response.send_message("❌ Only hosts can clear the queue.", ephemeral=True)
        return

    data = load_data()
    data["queue"] = []
    data["session_active"] = False
    save_data(data)

    await interaction.response.send_message("🧹 Queue cleared and session ended.")
    await post_queue_update(interaction.guild, data)


@queue_group.command(name="kick", description="[Host] Remove a player from the queue")
@app_commands.describe(member="The member to remove from the queue")
async def queue_kick(interaction: discord.Interaction, member: discord.Member):
    if not discord.utils.get(interaction.user.roles, name=HOST_ROLE_NAME):
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


# ══════════════════════════════════════════════
#  VERIFIED LOOKUP
# ══════════════════════════════════════════════

@bot.tree.command(name="whois", description="Look up a member's Social Club name")
@app_commands.describe(member="The Discord member to look up")
async def whois(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    user_id = str(member.id)

    if user_id in data["verified"]:
        sc = data["verified"][user_id]["social_club"]
        verified_by = data["verified"][user_id].get("verified_by", "Unknown")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔎 Member Lookup",
                color=discord.Color.green(),
                description=f"**Discord:** {member.mention}\n**Social Club:** {sc}\n**Verified by:** {verified_by}"
            )
        )
    else:
        await interaction.response.send_message(f"❌ {member.mention} is not verified.", ephemeral=True)


# ══════════════════════════════════════════════
#  REGISTER & RUN
# ══════════════════════════════════════════════

bot.tree.add_command(queue_group)
bot.run(BOT_TOKEN)
