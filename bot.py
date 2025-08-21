# bot.py
# Central London Roleplay — Single-file “Pure Gold” Bot (discord.py 2.4)
# Commands included:
#   Slash: /ping, /promote, /infractions, /poll, /closepoll, /ticketpanel,
#          /test, /welcometest, /update, /stafffeedback
#   Prefix: clr!esd, clr!stu, clr!codedelete (with confirmation), master phrase reply
# Systems:
#   - Anti-ping enforcement for SLT/ALT with escalating timeouts
#   - Ticket system with dropdown panel, claim rules, close/close-with-reason, transcripts
#   - Polls with buttons (live updating, role-gated voting optional, manual close)
#   - Welcome message on member join
#   - Cooldowns for /test and /welcometest
#   - Uptime pinger task
# Notes:
#   - You must invite the bot with sufficient intents and permissions:
#       Read/Send Messages, Manage Channels, Manage Roles, Timeout Members,
#       Manage Messages, Attach Files, Read Message History, Use Slash Commands.
#   - Set your token via environment variable DISCORD_TOKEN before running.

from __future__ import annotations

import asyncio
import os
import io
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================
# CONFIG / CONSTANTS
# ============================

# Role IDs (as requested)
INFRACTION_ROLE_ID = 1377701384555593748
PROMOTION_PERMS_ROLE_ID = 1377701385759494264
ALT_ROLE_ID = 1377701319053283379
SLT_ROLE_ID = 1377701315576201308

# Claim-role matrix for tickets
ROLE_INTERNAL_AFFAIRS = 1377701329996349540  # can claim IA, General Support, Department Inquiries
ROLE_MGMT = 1377701319841808405              # claim all
ROLE_DIRECTORSHIP = 1377701315576201308      # claim all
ROLE_ALT = 1377701319053283379               # claim all

# Channels / Categories
WELCOME_CHANNEL_ID = 1377724833881653288
ANNOUNCEMENT_CHANNEL_ID = 1377728485811347968
TICKET_TRANSCRIPTS_CATEGORY_ID = 1406935230505418802

# Assets
THUMBNAIL_URL = "https://media.discordapp.net/attachments/1389640286485086349/1404110742113751121/CLR_BG_LOGO.webp?ex=68a3e2c8&is=68a29148&hm=3c363e5daaca7d96dd7e362fded1f6eb09a442595b1631c088be0532f606ed79&=&format=webp&width=454&height=454"
TICKETS_BANNER_URL = "https://media.discordapp.net/attachments/1389640286485086349/1404761316316414073/CLR_TICKETS.png?ex=68a39dad&is=68a24c2d&hm=ba289955e39cb8bad9e01326aa3bcd56cde15c573f363e57b40452b600668309&=&format=webp&quality=lossless&width=1152&height=364"
POLL_GIF_URL = "https://media.discordapp.net/attachments/1377729047701753956/1399782056527138847/CLR_SMALLER_BANNER.gif?ex=68a1fb20&is=68a0a9a0&hm=30f5db71023558287e021eb95c41f398641f7d3eaa102e7eb71699bde34346e5&=&width=1152&height=180"

# Ticket panel configuration (exact format you provided)
TICKET_OPTIONS = [
    {
        "key": "management",
        "label": "Management Ticket",
        "emoji": "<:MANAGEMENT:1396581197689258125>",
        "category_id": 1404745383414071326,
        "description": "Speak with Management",
        "allowed_claim_roles": ["ALL"],  # everyone listed in claim-all
    },
    {
        "key": "department",
        "label": "Department Inquiries",
        "emoji": "<:MOD:1396580738366832650>",
        "category_id": 1404745860063170600,
        "description": "Department Questions",
        "allowed_claim_roles": ["IA", "ALL"],
    },
    {
        "key": "developers",
        "label": "Developers Entry Request",
        "emoji": "<:DIRECTORSHIP:1396581332854898740>",
        "category_id": 1404744566556459040,
        "description": "Livery, Uniform & Graphic Designers",
        "allowed_claim_roles": ["ALL"],
    },
    {
        "key": "ia",
        "label": "Internal Affairs",
        "emoji": "<:IA:1396581171839631490>",
        "category_id": 1383824203714920478,
        "description": "Internal Reporting",
        "allowed_claim_roles": ["IA", "ALL"],
    },
    {
        "key": "partnership",
        "label": "Partnership Request",
        "emoji": "<:OWNERSHIP:1396581359635402994>",
        "category_id": 1383826166992867379,
        "description": "Reuqest To Partner With Our Server",
        "allowed_claim_roles": ["ALL"],
    },
    {
        "key": "support",
        "label": "General Support",
        "emoji": "<:ADMIN:1396581140781072504>",
        "category_id": 1383550688063127664,
        "description": "General Support.",
        "allowed_claim_roles": ["IA", "ALL"],
    },
]

# Anti-ping escalation
PUNISH_ROLES = {ALT_ROLE_ID, SLT_ROLE_ID}
WARN_MESSAGE = (
    "Heya {user}, please avoid pinging the SLT and ALT.. If you do so one more time, you will be timed out. Thanks!"
)
TIMEOUT_BASE_MINUTES = 5
TIMEOUT_MAX_MINUTES = 120
STRIKE_RESET_HOURS = 24

# Cooldowns (per user)
TEST_COOLDOWN_SECONDS = 5 * 60

# Master controls
MASTER_USER_ID = 1299998109543301172
TRIGGER_PHRASE = "pulled an all nighter js for the bot @CLR | Staff Utilities#5388 you better thank me"

# Uptime pinger
UPTIME_URL = os.getenv("UPTIME_URL") or "http://localhost:3000/"

# ============================
# BOT SETUP
# ============================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.messages = True
intents.guild_messages = True
intents.guild_reactions = True

bot = commands.Bot(command_prefix="clr!", intents=intents)
tree = bot.tree

# State stores
is_paused = False
test_cooldowns: Dict[int, float] = {}  # user_id -> timestamp
mention_strikes: Dict[int, Dict[str, float | int]] = {}  # user_id -> {"count": int, "reset_at": ts}
active_polls: Dict[str, dict] = {}  # poll_id -> poll data

def now_ts() -> float:
    return datetime.now(tz=timezone.utc).timestamp()

def human_minutes(m: int) -> str:
    return f"{m} minute(s)"

# ============================
# UPTIME PINGER
# ============================

@tasks.loop(minutes=5)
async def ping_uptime():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(UPTIME_URL, timeout=10) as resp:
                print(f"Pinged uptime URL: {resp.status}")
    except Exception as e:
        print(f"Ping error: {e}")

# ============================
# EVENTS
# ============================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await tree.sync()
        print("Synced application commands.")
    except Exception as e:
        print("Slash sync failed:", e)
    if not ping_uptime.is_running():
        ping_uptime.start()

@bot.event
async def on_member_join(member: discord.Member):
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel and isinstance(channel, discord.TextChannel):
        embed = discord.Embed(
            description=f"WELCOME!\nWelcome {member.name} to {member.guild.name}! We hope you enjoy your stay here! Check our <#1377719482352402523> for more information!",
            color=discord.Color.from_str("#ADD8E6"),
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.set_image(url="https://media.discordapp.net/attachments/1377729047701753956/1399782056527138847/CLR_SMALLER_BANNER.gif")
        embed.set_footer(text=f"Member Count: {member.guild.member_count}")
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

def _reset_or_increment_strikes(author_id: int) -> int:
    ts = now_ts()
    entry = mention_strikes.get(author_id)
    if not entry or ts >= entry.get("reset_at", 0):
        mention_strikes[author_id] = {"count": 1, "reset_at": ts + STRIKE_RESET_HOURS * 3600}
        return 1
    # increment
    entry["count"] = int(entry.get("count", 0)) + 1
    return int(entry["count"])

def _timeout_minutes_for_count(count: int) -> int:
    # 1st offense -> warn only
    # 2nd offense -> 5 minutes, then 10, 20, 40, 80, capped at 120
    if count <= 1:
        return 0
    minutes = TIMEOUT_BASE_MINUTES * (2 ** (count - 2))
    return min(minutes, TIMEOUT_MAX_MINUTES)

@bot.event
async def on_message(message: discord.Message):
    # Ignore bots and DMs
    if message.author.bot or not message.guild:
        return

    # Master phrase
    if TRIGGER_PHRASE.lower() in message.content.lower():
        if message.author.id == MASTER_USER_ID:
            await message.channel.send("thank you master")
        else:
            await message.channel.send(f"you are not my master, <@{MASTER_USER_ID}>")
        return

    # Pause-state: respect prefix commands when paused for non-master
    if is_paused and not message.content.startswith("clr!"):
        if message.author.id != MASTER_USER_ID:
            return

    # Anti-ping enforcement for SLT / ALT
    if message.mentions:
        guild = message.guild
        slt_role = guild.get_role(SLT_ROLE_ID)
        alt_role = guild.get_role(ALT_ROLE_ID)
        protected_ids = set()
        if slt_role:
            protected_ids.update(m.id for m in message.mentions if slt_role in m.roles)
        if alt_role:
            protected_ids.update(m.id for m in message.mentions if alt_role in m.roles)

        if protected_ids:
            count = _reset_or_increment_strikes(message.author.id)
            if count == 1:
                try:
                    await message.reply(WARN_MESSAGE.format(user=message.author.mention))
                except Exception:
                    pass
            else:
                minutes = _timeout_minutes_for_count(count)
                if minutes > 0:
                    try:
                        dur = timedelta(minutes=minutes)
                        await message.author.timeout(dur, reason="Repeatedly pinging SLT/ALT")
                        try:
                            await message.reply(
                                f"{message.author.mention} has been timed out for {human_minutes(minutes)} for pinging SLT/ALT again."
                            )
                        except Exception:
                            pass
                    except Exception:
                        # Missing permissions or hierarchy issues; still warn
                        try:
                            await message.reply(
                                f"Timeout escalation would be {human_minutes(minutes)}, but I lack permission."
                            )
                        except Exception:
                            pass

    # Allow commands extension to process prefix commands
    await bot.process_commands(message)

# ============================
# PREFIX COMMANDS (clr!)
# ============================

@bot.command(name="esd")
async def pause_bot(ctx: commands.Context):
    global is_paused
    if ctx.author.id != MASTER_USER_ID:
        await ctx.reply("You do not have permission to pause the bot.")
        return
    if is_paused:
        await ctx.reply("Bot is already paused.")
        return
    is_paused = True
    await ctx.reply("Bot is now paused. Slash commands and interactions will be ignored for non-master users.")

@bot.command(name="stu")
async def resume_bot(ctx: commands.Context):
    global is_paused
    if ctx.author.id != MASTER_USER_ID:
        await ctx.reply("You do not have permission to resume the bot.")
        return
    if not is_paused:
        await ctx.reply("Bot is not paused.")
        return
    is_paused = False
    await ctx.reply("Bot has resumed normal operation.")

# clr!codedelete with confirmation phrase
@bot.command(name="codedelete")
async def code_delete(ctx: commands.Context):
    if ctx.author.id != MASTER_USER_ID:
        await ctx.reply("You do not have permission to run this.")
        return
    await ctx.reply("Type `CONFIRM DELETE` within 15 seconds to shut down the bot.")
    try:
        def check(m: discord.Message):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and m.content.strip() == "CONFIRM DELETE"
        msg = await bot.wait_for("message", timeout=15, check=check)
        await ctx.reply("Confirmed. Shutting down now.")
        await bot.close()
    except asyncio.TimeoutError:
        await ctx.reply("Confirmation not received. Aborted.")

# ============================
# SLASH COMMANDS
# ============================

# /ping
@tree.command(name="ping", description="Check bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong: {round(bot.latency * 1000)} ms")

# /promote (requires promotion perms role)
@tree.command(name="promote", description="Promote a user from old role to new role.")
@app_commands.describe(
    user="User to promote",
    old_role="Role to remove",
    new_role="Role to add",
    reason="Reason for the promotion (will be shown in the embed)",
)
async def promote_cmd(interaction: discord.Interaction, user: discord.User, old_role: discord.Role, new_role: discord.Role, reason: str):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or PROMOTION_PERMS_ROLE_ID not in [r.id for r in member.roles]:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    try:
        target = await interaction.guild.fetch_member(user.id)
    except Exception:
        await interaction.followup.send("Could not fetch that member.")
        return

    if old_role.id not in [r.id for r in target.roles]:
        await interaction.followup.send(f"The user does not have the old role <@&{old_role.id}>.")
        return

    try:
        await target.remove_roles(old_role, reason=f"Promotion by {interaction.user} — {reason}")
        await target.add_roles(new_role, reason=f"Promotion by {interaction.user} — {reason}")
    except Exception:
        await interaction.followup.send("Error: Bot lacks permission to manage these roles. Please check bot permissions.")
        return

    embed = discord.Embed(color=discord.Color.from_str("#ADD8E6"))
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.description = (
        "STAFF PROMOTION\n"
        "The Community Standards team has decided to award you a promotion. Congratulations!\n\n"
        f"Staff Member: <@{user.id}>\n\n"
        f"Old Rank: <@&{old_role.id}>\n\n"
        f"New Rank: <@&{new_role.id}>\n\n"
        f"Reason: {reason}\n\n"
        f"Issued by {interaction.user}"
    )
    await interaction.followup.send(embed=embed)

# /infractions (requires infraction role)
@tree.command(name="infractions", description="Issue an infraction to a user.")
@app_commands.describe(
    user="User receiving the infraction",
    points="Number of points or severity",
    reason="Infraction reason",
)
async def infractions_cmd(interaction: discord.Interaction, user: discord.User, points: int, reason: str):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or INFRACTION_ROLE_ID not in [r.id for r in member.roles]:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    embed = discord.Embed(title="Infraction Issued", color=discord.Color.red())
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(name="User", value=f"<@{user.id}>", inline=True)
    embed.add_field(name="Points", value=str(points), inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text=f"Issued by {interaction.user}")
    await interaction.followup.send(embed=embed)
    try:
        dm = await user.create_dm()
        await dm.send(f"You have received an infraction in {interaction.guild.name}.\nPoints: {points}\nReason: {reason}")
    except Exception:
        pass

# /test and /welcometest with cooldown
def _on_cooldown(user_id: int) -> int:
    now = now_ts()
    exp = test_cooldowns.get(user_id, 0.0)
    if now < exp:
        return int(exp - now)
    return 0

def _set_cooldown(user_id: int):
    test_cooldowns[user_id] = now_ts() + TEST_COOLDOWN_SECONDS

@tree.command(name="test", description="Send a test welcome embed to the test channel.")
async def test_cmd(interaction: discord.Interaction):
    remaining = _on_cooldown(interaction.user.id)
    if remaining > 0:
        await interaction.response.send_message(f"Please wait {remaining} more seconds before using this command again.", ephemeral=True)
        return
    _set_cooldown(interaction.user.id)

    ch = interaction.guild.get_channel(WELCOME_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("Test channel not found.", ephemeral=True)
        return

    embed = discord.Embed(
        description=f"WELCOME!\nWelcome {interaction.user.name} to {interaction.guild.name}! We hope you enjoy your stay here! Check our <#1377719482352402523> for more information!",
        color=discord.Color.from_str("#ADD8E6"),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_image(url="https://media.discordapp.net/attachments/1377729047701753956/1399782056527138847/CLR_SMALLER_BANNER.gif")
    embed.set_footer(text=f"Member Count: {interaction.guild.member_count}")
    await ch.send(embed=embed)
    await interaction.response.send_message("Test welcome message sent successfully!", ephemeral=True)

@tree.command(name="welcometest", description="Send a welcome test embed to the welcome channel.")
async def welcometest_cmd(interaction: discord.Interaction):
    remaining = _on_cooldown(interaction.user.id)
    if remaining > 0:
        await interaction.response.send_message(f"Please wait {remaining} more seconds before using this command again.", ephemeral=True)
        return
    _set_cooldown(interaction.user.id)

    ch = interaction.guild.get_channel(WELCOME_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("Welcome channel not found.", ephemeral=True)
        return

    embed = discord.Embed(
        description=f"WELCOME!\nWelcome {interaction.user.name} to {interaction.guild.name}! We hope you enjoy your stay here! Check our <#1377719482352402523> for more information!",
        color=discord.Color.from_str("#ADD8E6"),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_image(url="https://media.discordapp.net/attachments/1377729047701753956/1399782056527138847/CLR_SMALLER_BANNER.gif")
    embed.set_footer(text=f"Member Count: {interaction.guild.member_count}")
    await ch.send(embed=embed)
    await interaction.response.send_message("Welcome test message sent successfully!", ephemeral=True)

# /update
@tree.command(name="update", description="Post an update announcement.")
@app_commands.describe(
    update_number="The update number label",
    update_description="Description/details of the update",
    image1="Primary image URL",
    image2="Additional image URL",
    image3="Additional image URL",
)
async def update_cmd(interaction: discord.Interaction, update_number: str, update_description: str, image1: Optional[str] = None, image2: Optional[str] = None, image3: Optional[str] = None):
    await interaction.response.defer(ephemeral=False)
    ch = interaction.guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        await interaction.followup.send("Announcement channel not found.")
        return

    embed = discord.Embed(title=f"Update #{update_number}", description=update_description, color=discord.Color.from_str("#ADD8E6"))
    embed.set_thumbnail(url=THUMBNAIL_URL)
    if image1:
        embed.set_image(url=image1)
    # Preserve your spacing pattern for additional images as text fields
    if image2:
        embed.add_field(name="\u200B", value="\u200B", inline=False)
        embed.add_field(name="Additional Image", value=f"[Image 2]({image2})", inline=False)
    if image3:
        embed.add_field(name="Additional Image", value=f"[Image 3]({image3})", inline=False)

    await ch.send(embed=embed)
    await interaction.followup.send("Update announcement sent successfully!")

# /stafffeedback
@tree.command(name="stafffeedback", description="Privately submit feedback about a staff member.")
@app_commands.describe(
    staff="Staff user receiving the feedback",
    review="Your feedback text",
    rating="Rating value (e.g., 1-5)"
)
async def stafffeedback_cmd(interaction: discord.Interaction, staff: discord.User, review: str, rating: str):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="Staff Feedback", color=discord.Color.from_str("#ADD8E6"))
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(name="Staff Member", value=f"<@{staff.id}>", inline=True)
    embed.add_field(name="Rating", value=rating, inline=True)
    embed.add_field(name="Review", value=review, inline=False)
    embed.set_footer(text=f"Feedback submitted by {interaction.user}")
    await interaction.followup.send("Thank you for your feedback!", ephemeral=True)
    # If you want to forward to a specific channel, set its ID and send there.
    # This implementation keeps it ephemeral-only as you didn't specify a feedback channel here.

# ============================
# POLL SYSTEM
# ============================

def build_poll_embed(poll: dict, closed: bool = False) -> discord.Embed:
    desc_lines = []
    for i, opt in enumerate(poll["options"]):
        votes = poll["votes"].get(i, 0)
        desc_lines.append(f"{i+1}. {opt} — {votes} votes")
    embed = discord.Embed(
        title=poll.get("title") or ("Poll (Closed)" if closed else "Poll"),
        description=f"**{poll['question']}**\n\n" + "\n\n".join(desc_lines),
        color=discord.Color.from_str("#ADD8E6"),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_image(url=POLL_GIF_URL)
    if closed:
        footer = poll.get("footer") or "Poll ended"
    else:
        remaining_ms = max(0, int(poll["end_ts"] - now_ts() * 1000))
        remain_hours = (remaining_ms + 3600_000 - 1) // 3600_000
        footer = poll.get("footer") or f"Poll closes in {remain_hours} hour(s)"
    embed.set_footer(text=footer)
    return embed

def build_poll_buttons(poll_id: str, options: List[str], disabled: bool = False) -> discord.ui.ActionRow:
    row = discord.ui.ActionRow()
    for i, label in enumerate(options):
        label_trim = (label[:77] + "...") if len(label) > 80 else label
        style = discord.ButtonStyle.primary
        custom_id = f"poll_{poll_id}_{i}"
        btn = discord.ui.Button(label=f"{i+1}. {label_trim}", style=style, custom_id=custom_id, disabled=disabled)
        row.append_item(btn)
    return row

@tree.command(name="poll", description="Create a button poll (2-10 options).")
@app_commands.describe(
    question="Poll question",
    duration="Duration in hours (min 1)",
    title="Optional title",
    footer="Optional footer text",
    voter_role="Optional role allowed to vote (others blocked)",
    option1="Option 1",
    option2="Option 2",
    option3="Option 3",
    option4="Option 4",
    option5="Option 5",
    option6="Option 6",
    option7="Option 7",
    option8="Option 8",
    option9="Option 9",
    option10="Option 10",
)
async def poll_cmd(
    interaction: discord.Interaction,
    question: str,
    duration: Optional[int] = 1,
    title: Optional[str] = None,
    footer: Optional[str] = None,
    voter_role: Optional[discord.Role] = None,
    option1: str = None, option2: str = None, option3: str = None, option4: str = None, option5: str = None,
    option6: str = None, option7: str = None, option8: str = None, option9: str = None, option10: str = None,
):
    if duration is None or duration < 1:
        duration = 1
    options = [o for o in [option1, option2, option3, option4, option5, option6, option7, option8, option9, option10] if o]
    if len(options) < 2:
        await interaction.response.send_message("You must provide at least 2 options.", ephemeral=True)
        return
    if len(options) > 10:
        await interaction.response.send_message("Maximum 10 options allowed.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    poll_id = str(interaction.id)
    end_ts = now_ts() * 1000 + duration * 3600_000
    poll = {
        "id": poll_id,
        "question": question,
        "title": title,
        "footer": footer,
        "options": options,
        "votes": {},          # index -> count
        "voters": {},         # user_id -> option index
        "message_id": None,
        "channel_id": interaction.channel_id,
        "ended": False,
        "end_ts": end_ts,
        "voter_role_id": voter_role.id if voter_role else None,
        "issuer_id": interaction.user.id,
    }
    embed = build_poll_embed(poll)
    view_row = build_poll_buttons(poll_id, options, disabled=False)
    msg = await interaction.followup.send(embed=embed, view=discord.ui.View.from_components(view_row))
    poll["message_id"] = msg.id
    active_polls[poll_id] = poll

    # Schedule auto close
    async def auto_close():
        await asyncio.sleep(duration * 3600)
        p = active_polls.get(poll_id)
        if not p or p["ended"]:
            return
        await close_poll_internal(interaction.guild, p, title_override=None, footer_override=None)
    bot.loop.create_task(auto_close())

    await interaction.followup.send(f"Poll created — live results will update as people vote. Poll length: {duration} hour(s). Poll ID: {poll_id}", ephemeral=True)

async def close_poll_internal(guild: discord.Guild, p: dict, title_override: Optional[str], footer_override: Optional[str]):
    p["ended"] = True
    # Disable buttons and edit message with final embed
    ch = guild.get_channel(p["channel_id"])
    if not isinstance(ch, discord.TextChannel):
        active_polls.pop(p["id"], None)
        return
    try:
        msg = await ch.fetch_message(p["message_id"])
    except Exception:
        active_polls.pop(p["id"], None)
        return
    final = build_poll_embed({**p, "title": title_override or p.get("title"), "footer": footer_override or p.get("footer")}, closed=True)
    row = build_poll_buttons(p["id"], p["options"], disabled=True)
    try:
        await msg.edit(embed=final, view=discord.ui.View.from_components(row))
    except Exception:
        pass
    active_polls.pop(p["id"], None)

@tree.command(name="closepoll", description="Close an active poll by Poll ID.")
@app_commands.describe(
    pollid="The poll ID shown when the poll was created",
    title="Optional new title for the closed poll",
    footer="Optional new footer for the closed poll",
    voterrole="Optionally set/override the voter role at close",
)
async def closepoll_cmd(interaction: discord.Interaction, pollid: str, title: Optional[str] = None, footer: Optional[str] = None, voterrole: Optional[discord.Role] = None):
    p = active_polls.get(pollid)
    if not p:
        await interaction.response.send_message(f"Poll with ID {pollid} not found or already closed.", ephemeral=True)
        return
    # allow issuer or anyone to close (you didn't restrict this in the last request)
    if voterrole:
        p["voter_role_id"] = voterrole.id
    await interaction.response.defer(ephemeral=True)
    await close_poll_internal(interaction.guild, p, title_override=title, footer_override=footer)
    await interaction.followup.send(f"Poll {pollid} closed successfully.", ephemeral=True)

# Buttons handling for poll votes
@bot.event
async def on_interaction(interaction: discord.Interaction):
    # respect paused state for non-master users
    if is_paused and interaction.user.id != MASTER_USER_ID:
        # Let master still use everything; others get rejection for component interactions
        if interaction.type == discord.InteractionType.component:
            try:
                await interaction.response.send_message("Bot is currently paused and not accepting votes.", ephemeral=True)
            except Exception:
                pass
        return

    if interaction.type == discord.InteractionType.component and interaction.data and isinstance(interaction.data, dict):
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("poll_"):
            # Format: poll_<pollId>_<optionIndex>
            _, poll_id, idx_str = custom_id.split("_")
            p = active_polls.get(poll_id)
            if not p or p.get("ended"):
                await interaction.response.send_message("This poll has ended or does not exist.", ephemeral=True)
                return

            # voter role gate
            if p.get("voter_role_id"):
                role_id = p["voter_role_id"]
                mem = interaction.guild.get_member(interaction.user.id)
                if not mem or role_id not in [r.id for r in mem.roles]:
                    await interaction.response.send_message("You are not allowed to vote in this poll.", ephemeral=True)
                    return

            try:
                option_index = int(idx_str)
            except Exception:
                return

            user_id = interaction.user.id
            prev = p["voters"].get(user_id)
            if prev is not None and prev == option_index:
                await interaction.response.send_message("You have already voted for that option.", ephemeral=True)
                return
            # adjust tallies
            if prev is not None:
                p["votes"][prev] = max(0, p["votes"].get(prev, 1) - 1)
            p["votes"][option_index] = p["votes"].get(option_index, 0) + 1
            p["voters"][user_id] = option_index

            # live update embed
            try:
                ch = interaction.guild.get_channel(p["channel_id"])
                msg = await ch.fetch_message(p["message_id"])
                live = build_poll_embed(p, closed=False)
                row = build_poll_buttons(p["id"], p["options"], disabled=False)
                await msg.edit(embed=live, view=discord.ui.View.from_components(row))
            except Exception:
                pass

            try:
                await interaction.response.send_message(f"You voted for option {option_index + 1}.", ephemeral=True)
            except Exception:
                pass

# ============================
# TICKET SYSTEM
# ============================

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Dropdown
        options = []
        for item in TICKET_OPTIONS:
            # Use emoji parameter; discord.py supports PartialEmoji.from_str
            options.append(discord.SelectOption(
                label=item["label"],
                description=item["description"][:100],
                value=item["key"],
                emoji=discord.PartialEmoji.from_str(item["emoji"])
            ))
        self.add_item(TicketDropdown(options=options))

class TicketDropdown(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(placeholder="Select a ticket type...", min_values=1, max_values=1, options=options, custom_id="ticket_dropdown")

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        conf = next((x for x in TICKET_OPTIONS if x["key"] == key), None)
        if not conf:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        category = interaction.guild.get_channel(conf["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("Ticket category not found or misconfigured.", ephemeral=True)
            return

        # Channel name short tag
        short = key[:6]
        ch_name = f"ticket-{interaction.user.name[:20]}-{short}"

        # Create ticket channel
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        # Allow staff roles to view
        staff_role_ids = {ROLE_MGMT, ROLE_DIRECTORSHIP, ROLE_ALT, ROLE_INTERNAL_AFFAIRS}
        for rid in staff_role_ids:
            r = interaction.guild.get_role(rid)
            if r:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        try:
            ch = await interaction.guild.create_text_channel(ch_name, category=category, overwrites=overwrites, reason=f"Ticket opened by {interaction.user}")
        except Exception as e:
            await interaction.followup.send(f"Failed to create ticket: {e}", ephemeral=True)
            return

        # Post the ticket greeting + buttons
        greet = (
            f"Thank you {interaction.user.mention} for opening a {conf['label']}! Our Staff Representatives will be here to assist you when they are free! "
            "Please refrain from pinging any staff unless they have requested."
        )

        embed = discord.Embed(description=greet, color=discord.Color.from_str("#ADD8E6"))
        embed.set_thumbnail(url=THUMBNAIL_URL)

        ticket_view = TicketControls(ticket_key=key, opener_id=interaction.user.id)
        await ch.send(embed=embed, view=ticket_view)
        await interaction.followup.send(f"Your ticket has been opened: {ch.mention}", ephemeral=True)

class TicketControls(discord.ui.View):
    def __init__(self, ticket_key: str, opener_id: int):
        super().__init__(timeout=None)
        self.ticket_key = ticket_key
        self.opener_id = opener_id
        self.claimed_by: Optional[int] = None

    def _can_claim(self, member: discord.Member) -> bool:
        # Claim rules
        has_all = any(r.id in {ROLE_MGMT, ROLE_DIRECTORSHIP, ROLE_ALT} for r in member.roles)
        if has_all:
            return True
        if self.ticket_key in ("ia", "support", "department"):
            return any(r.id == ROLE_INTERNAL_AFFAIRS for r in member.roles)
        if self.ticket_key == "management":
            return any(r.id == ROLE_MGMT for r in member.roles) or any(r.id == ROLE_DIRECTORSHIP for r in member.roles) or any(r.id == ROLE_ALT for r in member.roles)
        if self.ticket_key in ("developers", "partnership"):
            return has_all
        return False

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self._can_claim(member):
            await interaction.response.send_message("You cannot claim this ticket.", ephemeral=True)
            return
        if self.claimed_by:
            await interaction.response.send_message(f"Already claimed by <@{self.claimed_by}>.", ephemeral=True)
            return
        self.claimed_by = interaction.user.id
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=False)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Close silently and transcript
        await interaction.response.defer(ephemeral=True)
        await close_ticket_with_transcript(interaction.channel, closed_by=interaction.user, reason=None)

    @discord.ui.button(label="Close with Reason", style=discord.ButtonStyle.secondary, custom_id="ticket_close_reason")
    async def close_with_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CloseReasonModal(channel=interaction.channel)
        await interaction.response.send_modal(modal)

class CloseReasonModal(discord.ui.Modal, title="Close Ticket With Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.long, required=True, max_length=1000)

    def __init__(self, channel: discord.abc.MessageableChannel):
        super().__init__(timeout=None)
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await close_ticket_with_transcript(self.channel, closed_by=interaction.user, reason=str(self.reason))

async def close_ticket_with_transcript(channel: discord.abc.MessageableChannel, closed_by: discord.User, reason: Optional[str]):
    if not isinstance(channel, discord.TextChannel):
        try:
            await closed_by.send("Cannot close this ticket: channel type not supported.")
        except Exception:
            pass
        return

    guild = channel.guild
    # Fetch messages for transcript
    buff = io.StringIO()
    try:
        async for msg in channel.history(limit=1000, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content.replace("\n", "\\n")
            buff.write(f"[{ts}] {author}: {content}\n")
    except Exception:
        pass

    transcript_text = buff.getvalue().encode("utf-8")
    transcript_file = discord.File(io.BytesIO(transcript_text), filename=f"{channel.name}-transcript.txt")

    # Create transcript channel in configured category and post file
    cat = guild.get_channel(TICKET_TRANSCRIPTS_CATEGORY_ID)
    transcript_ch: Optional[discord.TextChannel] = None
    if isinstance(cat, discord.CategoryChannel):
        try:
            transcript_ch = await guild.create_text_channel(f"transcript-{channel.name}", category=cat)
        except Exception:
            transcript_ch = None

    if transcript_ch:
        try:
            await transcript_ch.send(
                content=f"Transcript for {channel.mention} — Closed by {closed_by.mention}" + (f"\nReason: {reason}" if reason else ""),
                file=transcript_file
            )
        except Exception:
            pass

    # DM opener if we can find them from channel name pattern or first message
    opener: Optional[discord.Member] = None
    try:
        async for msg in channel.history(limit=50, oldest_first=True):
            if msg.type == discord.MessageType.default and msg.author != guild.me:
                opener = guild.get_member(msg.author.id)
                break
    except Exception:
        opener = None

    if opener:
        try:
            if reason:
                await opener.send(f"Your ticket '{channel.name}' has been closed by {closed_by}. Reason: {reason}")
            else:
                await opener.send(f"Your ticket '{channel.name}' has been closed by {closed_by}.")
        except Exception:
            pass

    # Delete the ticket channel
    try:
        await channel.delete(reason=f"Ticket closed by {closed_by} — {reason or 'No reason provided'}")
    except Exception:
        pass

# /ticketpanel — posts the panel embed with dropdown
@tree.command(name="ticketpanel", description="Post the ticket creation panel in the current channel.")
async def ticketpanel_cmd(interaction: discord.Interaction):
    # No role lock as requested
    embed = discord.Embed(
        description="Create a Support Ticket\nSelect the reason for your ticket from the dropdown below.",
        color=discord.Color.from_str("#ADD8E6"),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_image(url=TICKETS_BANNER_URL)

    view = TicketPanel()
    await interaction.response.send_message(embed=embed, view=view)

# ============================
# RUN
# ============================

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: Discord token not set. Set DISCORD_TOKEN environment variable.")
        return
    try:
        bot.run(token)
    except Exception as e:
        print("Bot failed to start:", e)

if __name__ == "__main__":
    main()
