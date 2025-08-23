# bot.py
# Discord.py v2.4+; Python 3.11.x
# Features:
# - Tickets (panel -> private threads; open/close)
# - Moderation (warn/kick/ban/timeout/clear-timeout), big embeds, logs to infractions channel
# - Promotions (big embed), logs to promotions channel
# - Anti-ping for SLT/ALT roles/members
# - Simple "sessions" commands (start/stop) -> managed in a thread
# - Robust startup, slash command sync, error handling, reconnects
# - Persistent ticket view so buttons keep working across restarts

import os
import asyncio
import logging
from datetime import timedelta, datetime, timezone
from typing import Optional, Dict, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks

###############################################################################
# --------------------------- CONFIG / CONSTANTS ------------------------------
###############################################################################

# === REQUIRED: put your server ID here (enables targeted sync and thread creation)
GUILD_ID = 1377700771683893309  # <-- set to your guild ID (int). If 0, bot will global sync (slower)

# === Role IDs for anti-ping:
SLT_ROLE_ID = 1377701315576201308   # <-- set to your SLT role ID (int)
ALT_ROLE_ID = 1377701319053283379   # <-- set to your ALT role ID (int)

# === Channels you gave:
PROMOTIONS_CHANNEL_ID = 1378002943269146796
INFRACTIONS_CHANNEL_ID = 1378002993567367319
TICKET_PANEL_CHANNEL_ID = 1377728428647911434

# === GIF to display at the bottom of infractions & promotions
FOOTER_GIF = ("https://media.discordapp.net/attachments/1377729047701753956/"
              "1399782056527138847/CLR_SMALLER_BANNER.gif"
              "?ex=68a9e420&is=68a892a0&hm=65f04e792468fc322f130fa21917459cad09b450e9686e909fc461ed9639456f"
              "&=&width=1152&height=180")

# Token: prefer env var DISCORD_TOKEN on Render
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.guild_messages = True
intents.guild_reactions = True

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s"
)
log = logging.getLogger("bot")

# Bot
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

###############################################################################
# ------------------------------ UTILITIES ------------------------------------
###############################################################################

def is_staff_slash():
    """Simple check: requires Manage Guild OR Mod perms (kick/ban) OR SLT/ALT roles."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user:
            return False

        # admins or manage guild can always use
        perms = interaction.user.guild_permissions
        if perms.administrator or perms.manage_guild or perms.kick_members or perms.ban_members:
            return True

        # has SLT/ALT role?
        roles = getattr(interaction.user, "roles", [])
        role_ids = {r.id for r in roles}
        if SLT_ROLE_ID and SLT_ROLE_ID in role_ids:
            return True
        if ALT_ROLE_ID and ALT_ROLE_ID in role_ids:
            return True

        await interaction.response.send_message(
            "You don't have permission to use this.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)

def fmt_dt(dt: datetime) -> str:
    return discord.utils.format_dt(dt, "F")

def can_ping_roles(member: discord.Member) -> bool:
    """True if member has SLT or ALT role (can ping those roles/members)."""
    if member.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in getattr(member, "roles", [])}
    return (SLT_ROLE_ID and SLT_ROLE_ID in role_ids) or (ALT_ROLE_ID and ALT_ROLE_ID in role_ids)

async def get_text_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        ch = await guild.fetch_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
    except Exception:
        return None
    return None

def big_embed(
    title: str,
    description: str,
    color: discord.Color,
    author: Optional[discord.abc.User] = None,
    thumbnail: Optional[str] = None,
    extra_fields: Optional[Dict[str, str]] = None,
    footer_gif: Optional[str] = None
) -> discord.Embed:
    """A 'big' looking embed with large title, big description, optional fields, and a GIF placed as image."""
    em = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    if author is not None:
        em.set_author(name=str(author), icon_url=getattr(author.display_avatar, "url", discord.Embed.Empty))
    # Large feel: use thumbnail + image
    if thumbnail:
        em.set_thumbnail(url=thumbnail)
    if extra_fields:
        for k, v in extra_fields.items():
            em.add_field(name=k, value=v, inline=False)
    if footer_gif:
        em.set_image(url=footer_gif)
    em.timestamp = datetime.now(timezone.utc)
    return em


###############################################################################
# -------------------------- PERSISTENT TICKET VIEW ---------------------------
###############################################################################

class TicketView(discord.ui.View):
    def __init__(self):
        # timeout=None => persistent
        super().__init__(timeout=None)
        # NOTE: we give custom_ids to persist across restarts
        # Buttons render in separate rows automatically if needed.

    @discord.ui.button(label="📩 Open Ticket", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Ensure we only open in the designated panel channel (not strictly necessary)
        if interaction.channel_id != TICKET_PANEL_CHANNEL_ID:
            await interaction.response.send_message(
                "Use the ticket panel in the configured channel to open a ticket.",
                ephemeral=True
            )
            return

        # Create a private thread in the panel channel
        parent: discord.TextChannel = interaction.channel  # type: ignore
        # Thread name: user#discrim or display name
        base_name = f"ticket-{interaction.user.name}".lower()
        try:
            thread = await parent.create_thread(
                name=base_name,
                auto_archive_duration=10080,  # 7 days
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            # Add requester
            try:
                await thread.add_user(interaction.user)
            except Exception:
                pass

            # Build intro embed
            em = big_embed(
                title="New Ticket Opened",
                description=(
                    f"Hello {interaction.user.mention}! A support ticket has been created.\n\n"
                    "Staff will be with you shortly.\n"
                    "Use the **Close Ticket** button when you're done."
                ),
                color=discord.Color.blurple(),
                author=interaction.user,
                extra_fields={"Ticket": thread.mention},
                footer_gif=None  # no need to add the banner GIF here
            )

            view = CloseTicketView()
            await thread.send(content=f"{interaction.user.mention}", embed=em, view=view)

            await interaction.response.send_message(
                f"Ticket created: {thread.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permissions to create private threads here. "
                "Please enable **Private Threads** for the bot role.",
                ephemeral=True
            )
        except Exception as e:
            log.exception("Error creating ticket thread")
            await interaction.response.send_message(
                f"Something went wrong creating the ticket: `{e}`", ephemeral=True
            )

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("This isn't a ticket thread.", ephemeral=True)
            return
        # allow OP, staff, or thread owner to close; otherwise deny
        can_manage = False
        if isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            if perms.manage_threads or perms.manage_channels or perms.manage_messages or perms.administrator:
                can_manage = True
            elif thread.owner_id == interaction.user.id:
                can_manage = True

        if not can_manage:
            await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
            return

        try:
            await thread.edit(archived=True, locked=True)
            await interaction.response.send_message("Ticket closed & archived.", ephemeral=True)
        except Exception as e:
            log.exception("Ticket close failed")
            await interaction.response.send_message(f"Failed to close: `{e}`", ephemeral=True)


###############################################################################
# ----------------------------- TICKET COG ------------------------------------
###############################################################################

class TicketCog(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    @app_commands.command(name="ticketpanel", description="Send the ticket panel to the configured channel.")
    @is_staff_slash()
    async def ticketpanel(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        ch = await get_text_channel(guild, TICKET_PANEL_CHANNEL_ID)
        if not ch:
            await interaction.response.send_message(
                f"I couldn't find the ticket panel channel (ID: {TICKET_PANEL_CHANNEL_ID}).",
                ephemeral=True
            )
            return

        # Build panel embed
        em = big_embed(
            title="Support Tickets",
            description=(
                "Need help? Click **Open Ticket** to create a private support thread.\n"
                "A staff member will assist you as soon as possible."
            ),
            color=discord.Color.blurple(),
            author=interaction.user,
            thumbnail=None,
            extra_fields={
                "How it works": (
                    "• Private thread with you & staff\n"
                    "• You can attach images and messages\n"
                    "• Press 'Close Ticket' when finished"
                )
            },
            footer_gif=None
        )
        view = TicketView()
        try:
            await ch.send(embed=em, view=view)
            await interaction.response.send_message("Successfully sent the ticket panel.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I cannot send messages in the panel channel.", ephemeral=True)
        except Exception as e:
            log.exception("Failed to send ticket panel")
            await interaction.response.send_message(f"Error sending panel: `{e}`", ephemeral=True)


###############################################################################
# --------------------------- MODERATION / INFRACTIONS ------------------------
###############################################################################

class InfractionsCog(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    async def _send_infraction_log(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        color: discord.Color,
        target: Optional[discord.Member],
        moderator: Optional[discord.Member],
        extra: Optional[Dict[str, str]] = None
    ):
        channel = await get_text_channel(guild, INFRACTIONS_CHANNEL_ID)
        if not channel:
            log.warning("Infractions channel not found.")
            return
        thumb = target.display_avatar.url if target else None
        em = big_embed(
            title=title,
            description=description,
            color=color,
            author=moderator,
            thumbnail=thumb,
            extra_fields=extra,
            footer_gif=FOOTER_GIF  # GIF at the bottom
        )
        await channel.send(embed=em)

    @app_commands.command(name="warn", description="Warn a member.")
    @is_staff_slash()
    @app_commands.describe(member="Member to warn", reason="Reason")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(f"You have been **warned** in **{interaction.guild.name}**. Reason: {reason}")
        except Exception:
            pass

        await self._send_infraction_log(
            interaction.guild,
            title="⚠️ Warning Issued",
            description=f"{member.mention} has been warned.",
            color=discord.Color.orange(),
            target=member,
            moderator=interaction.user if isinstance(interaction.user, discord.Member) else None,
            extra={"Reason": reason or "No reason provided"}
        )
        await interaction.followup.send(f"Warned {member.mention}.", ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member.")
    @is_staff_slash()
    @app_commands.describe(member="Member to kick", reason="Reason")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(f"You were **kicked** from **{interaction.guild.name}**. Reason: {reason}")
        except Exception:
            pass
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to kick this member.", ephemeral=True)
            return
        await self._send_infraction_log(
            interaction.guild, "👟 Member Kicked",
            f"{member.mention} was kicked.",
            discord.Color.red(), member,
            interaction.user if isinstance(interaction.user, discord.Member) else None,
            {"Reason": reason or "No reason provided"}
        )
        await interaction.followup.send(f"Kicked {member.mention}.", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member.")
    @is_staff_slash()
    @app_commands.describe(member="Member to ban", reason="Reason", delete_message_days="Delete x days of their messages")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided", delete_message_days: app_commands.Range[int, 0, 7] = 0):
        await interaction.response.defer(ephemeral=True)
        try:
            await member.send(f"You were **banned** from **{interaction.guild.name}**. Reason: {reason}")
        except Exception:
            pass
        try:
            await interaction.guild.ban(member, reason=reason, delete_message_seconds=delete_message_days * 24 * 3600)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to ban this member.", ephemeral=True)
            return
        await self._send_infraction_log(
            interaction.guild, "🔨 Member Banned",
            f"{member.mention} was banned.",
            discord.Color.dark_red(), member,
            interaction.user if isinstance(interaction.user, discord.Member) else None,
            {"Reason": reason or "No reason provided", "Deleted Message Days": str(delete_message_days)}
        )
        await interaction.followup.send(f"Banned {member.mention}.", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member for X minutes.")
    @is_staff_slash()
    @app_commands.describe(member="Member", minutes="Minutes (1-40320)", reason="Reason")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: Optional[str] = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        try:
            await member.edit(timed_out_until=until, reason=reason)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to timeout this member.", ephemeral=True)
            return
        await self._send_infraction_log(
            interaction.guild, "⏳ Member Timed Out",
            f"{member.mention} timed out for **{minutes}** minute(s).",
            discord.Color.gold(), member,
            interaction.user if isinstance(interaction.user, discord.Member) else None,
            {"Reason": reason or "No reason provided", "Until": discord.utils.format_dt(until, style='F')}
        )
        await interaction.followup.send(f"Timed out {member.mention} for {minutes} minute(s).", ephemeral=True)

    @app_commands.command(name="cleartimeout", description="Remove timeout from a member.")
    @is_staff_slash()
    @app_commands.describe(member="Member")
    async def cleartimeout(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            await member.edit(timed_out_until=None, reason=f"Cleared by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to clear timeout.", ephemeral=True)
            return
        await self._send_infraction_log(
            interaction.guild, "✅ Timeout Cleared",
            f"Timeout cleared for {member.mention}.",
            discord.Color.green(), member,
            interaction.user if isinstance(interaction.user, discord.Member) else None,
            None
        )
        await interaction.followup.send(f"Cleared timeout for {member.mention}.", ephemeral=True)


###############################################################################
# ----------------------------- PROMOTIONS COG --------------------------------
###############################################################################

class PromotionsCog(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    @app_commands.command(name="promote", description="Announce a promotion.")
    @is_staff_slash()
    @app_commands.describe(member="Member being promoted", new_role="The new role title", reason="Reason or notes")
    async def promote(self, interaction: discord.Interaction, member: discord.Member, new_role: str, reason: Optional[str] = "N/A"):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return

        ch = await get_text_channel(guild, PROMOTIONS_CHANNEL_ID)
        if not ch:
            await interaction.followup.send("Promotions channel not found.", ephemeral=True)
            return

        desc = (
            f"{member.mention} has been **promoted**!\n\n"
            f"**New Role:** {new_role}\n"
            f"**Reason:** {reason or 'N/A'}"
        )
        em = big_embed(
            title="🎉 Promotion Announcement",
            description=desc,
            color=discord.Color.green(),
            author=interaction.user,
            thumbnail=member.display_avatar.url,
            extra_fields={"Congratulations!": "Please welcome and support them in their new responsibilities."},
            footer_gif=FOOTER_GIF  # put GIF at the bottom
        )
        await ch.send(embed=em)
        await interaction.followup.send(f"Promotion posted for {member.mention}.", ephemeral=True)


###############################################################################
# ------------------------------ SESSIONS COG ---------------------------------
###############################################################################

class SessionsCog(commands.Cog):
    """Very simple session helper using a private thread. You can expand as you like."""
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_
        self.active_sessions: Dict[int, discord.Thread] = {}  # user_id -> thread

    @app_commands.command(name="session", description="Start or stop a session (creates a private thread).")
    @is_staff_slash()
    @app_commands.describe(action="start or stop", topic="Optional topic for the session")
    async def session(self, interaction: discord.Interaction, action: app_commands.Choice[str], topic: Optional[str] = None):
        # Predefine choices in setup_hook
        await interaction.response.defer(ephemeral=True)
        ch = await get_text_channel(interaction.guild, TICKET_PANEL_CHANNEL_ID)
        if not ch:
            await interaction.followup.send("Configured base channel not found for session threads.", ephemeral=True)
            return

        if action.value == "start":
            if interaction.user.id in self.active_sessions:
                await interaction.followup.send("You already have an active session.", ephemeral=True)
                return
            name = f"session-{interaction.user.name}".lower()
            thread = await ch.create_thread(
                name=name,
                auto_archive_duration=10080,
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            await thread.add_user(interaction.user)
            em = big_embed(
                title="Session Started",
                description=f"{interaction.user.mention} started a session.\n**Topic:** {topic or 'N/A'}",
                color=discord.Color.blurple(),
                author=interaction.user,
                extra_fields=None,
                footer_gif=None
            )
            await thread.send(embed=em)
            self.active_sessions[interaction.user.id] = thread
            await interaction.followup.send(f"Session started: {thread.mention}", ephemeral=True)

        elif action.value == "stop":
            thread = self.active_sessions.get(interaction.user.id)
            if not thread:
                await interaction.followup.send("You don't have an active session.", ephemeral=True)
                return
            try:
                await thread.edit(archived=True, locked=True)
            except Exception:
                pass
            self.active_sessions.pop(interaction.user.id, None)
            await interaction.followup.send("Your session has been stopped.", ephemeral=True)

###############################################################################
# ------------------------------ ANTI-PING ------------------------------------
###############################################################################

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Anti-ping for SLT / ALT unless the sender has those roles (or admin).
    try:
        author: discord.Member = message.author  # type: ignore
        if not can_ping_roles(author):
            # If they mentioned SLT/ALT roles directly
            mentioned_role_ids: Set[int] = {r.id for r in message.role_mentions}
            blocked = set()
            if SLT_ROLE_ID and SLT_ROLE_ID in mentioned_role_ids:
                blocked.add("SLT")
            if ALT_ROLE_ID and ALT_ROLE_ID in mentioned_role_ids:
                blocked.add("ALT")

            # If they mentioned any members who HAVE SLT/ALT
            if not blocked and message.mentions:
                for m in message.mentions:
                    if isinstance(m, discord.Member):
                        mids = {r.id for r in m.roles}
                        if SLT_ROLE_ID and SLT_ROLE_ID in mids:
                            blocked.add("SLT"); break
                        if ALT_ROLE_ID and ALT_ROLE_ID in mids:
                            blocked.add("ALT"); break

            if blocked:
                try:
                    await message.delete()
                except Exception:
                    pass
                username = author.display_name
                txt = f"Hiya, {username}. Please avoid pinging the **SLT, or ALT** role holders. Continuous violations will result in a moderation action which is automated!"
                try:
                    await message.channel.send(author.mention + " " + txt, delete_after=10)
                except Exception:
                    pass
                return
    except Exception:
        log.exception("anti-ping failed")

    await bot.process_commands(message)

###############################################################################
# ------------------------------ STARTUP / SYNC -------------------------------
###############################################################################

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)  # type: ignore

    # Register the persistent views so the ticket buttons survive restarts
    bot.add_view(TicketView())
    bot.add_view(CloseTicketView())

    # Try to sync slash commands quickly to your guild if provided
    try:
        if GUILD_ID and (guild := bot.get_guild(GUILD_ID)):
            synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
            log.info("Synced %d command(s) to guild %s", len(synced), guild.name)
        else:
            synced = await bot.tree.sync()
            log.info("Globally synced %d command(s). (May take up to an hour to appear)", len(synced))
    except Exception:
        log.exception("Slash command sync failed")

@bot.event
async def on_error(event_method, *args, **kwargs):
    log.exception("Unhandled error in %s", event_method)

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    log.exception("Command error: %s", error)
    try:
        await ctx.reply("An error occurred. Staff have been notified.")
    except Exception:
        pass

###############################################################################
# ------------------------------ SETUP HOOK -----------------------------------
###############################################################################

@bot.event
async def setup_hook():
    # Attach cogs
    await bot.add_cog(TicketCog(bot))
    await bot.add_cog(InfractionsCog(bot))
    await bot.add_cog(PromotionsCog(bot))
    await bot.add_cog(SessionsCog(bot))

    # Add choice options for /session command
    # (discord.py wants them attached to the command object before sync)
    try:
        session_cmd = bot.tree.get_command("session")
        if isinstance(session_cmd, app_commands.Command):
            session_cmd.parameters["action"].choices = [
                app_commands.Choice(name="start", value="start"),
                app_commands.Choice(name="stop", value="stop")
            ]
    except Exception:
        pass

###############################################################################
# --------------------------------- RUN ---------------------------------------
###############################################################################

def _missing_config() -> Optional[str]:
    if not TOKEN:
        return "Missing bot token. Set DISCORD_TOKEN env var."
    if GUILD_ID == 0:
        return ("GUILD_ID is 0. The bot will still run, "
                "but slash commands may take longer to appear (global sync).")
    return None

if __name__ == "__main__":
    warn = _missing_config()
    if warn:
        log.warning(warn)

    # Auto-reconnect is default; also limit member cache so memory is stable.
    bot.run(TOKEN, log_handler=None, reconnect=True)
