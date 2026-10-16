"""
Production-Ready Private Thread Support Ticket System Cog for Discord.py 2.x.
Features:
- DynamicItem persistent category buttons on the public Discord panel
- Private Thread spawning per ticket with automatic role & member routing
- In-thread persistent staff control buttons (Claim, Assign Role/Member, Close)
- Full as-is transcript preservation, HTML/TXT exports, and archival to SQLite
- Complete remote management via Web Dashboard API
"""

import io
import re
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from services.tickets_db import TicketsDB
from services.tickets_exporter import generate_html_transcript, generate_txt_transcript

logger = logging.getLogger("TicketsCog")


def parse_hex_color(hex_str: Optional[str], default: int = 0x5865F2) -> discord.Color:
    """Parses a hex color string into a discord.Color."""
    if not hex_str:
        return discord.Color(default)
    s = hex_str.strip().lstrip("#")
    try:
        return discord.Color(int(s, 16))
    except Exception:
        return discord.Color(default)


def button_style_from_str(style_str: Optional[str]) -> discord.ButtonStyle:
    """Maps string representation to discord.ButtonStyle."""
    s = (style_str or "").strip().lower()
    if s == "success":
        return discord.ButtonStyle.success
    elif s == "danger":
        return discord.ButtonStyle.danger
    elif s == "secondary":
        return discord.ButtonStyle.secondary
    return discord.ButtonStyle.primary


# =============================================================================
# Dynamic Persistent Items across Bot & VPS Restarts
# =============================================================================

class DynamicTicketCategoryButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:open:(?P<cat_id>\d+)"):
    """
    Persistent Dynamic Button for category buttons on the public Discord Ticket Panel.
    Pattern: ticket:open:<cat_id>
    """
    def __init__(
        self,
        cat_id: int,
        label: str = "Open Ticket",
        emoji: Optional[str] = "🎫",
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        disabled: bool = False
    ):
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                emoji=emoji if emoji else None,
                style=style,
                disabled=disabled,
                custom_id=f"ticket:open:{cat_id}"
            )
        )
        self.cat_id = cat_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        cat_id = int(match.group("cat_id"))
        return cls(
            cat_id=cat_id,
            label=item.label or "Open Ticket",
            emoji=str(item.emoji) if item.emoji else None,
            style=item.style,
            disabled=item.disabled
        )

    async def callback(self, interaction: discord.Interaction):
        cog: Optional["TicketsCog"] = interaction.client.get_cog("TicketsCog")  # type: ignore
        if not cog:
            await interaction.response.send_message("❌ Ticket system is temporarily unavailable.", ephemeral=True)
            return
        await cog.handle_category_click(interaction, self.cat_id)


class DynamicTicketActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:action:(?P<action>claim|assign|close):(?P<ticket_id>\d+)"):
    """
    Persistent Dynamic Button for action buttons inside the private ticket thread.
    Pattern: ticket:action:<action>:<ticket_id>
    """
    def __init__(
        self,
        action: str,
        ticket_id: int,
        label: str = "Action",
        emoji: Optional[str] = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False
    ):
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                emoji=emoji if emoji else None,
                style=style,
                disabled=disabled,
                custom_id=f"ticket:action:{action}:{ticket_id}"
            )
        )
        self.action = action
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        action = match.group("action")
        ticket_id = int(match.group("ticket_id"))
        return cls(
            action=action,
            ticket_id=ticket_id,
            label=item.label or action.capitalize(),
            emoji=str(item.emoji) if item.emoji else None,
            style=item.style,
            disabled=item.disabled
        )

    async def callback(self, interaction: discord.Interaction):
        cog: Optional["TicketsCog"] = interaction.client.get_cog("TicketsCog")  # type: ignore
        if not cog:
            await interaction.response.send_message("❌ Ticket system is temporarily unavailable.", ephemeral=True)
            return
        await cog.handle_ticket_action(interaction, self.action, self.ticket_id)


# =============================================================================
# Modals & Ephemeral Assignment Views
# =============================================================================

class TicketOpenModal(discord.ui.Modal):
    """Modal prompted when a user clicks a ticket category button."""
    def __init__(self, cat_id: int, category_name: str, cog: "TicketsCog"):
        super().__init__(title=f"Ticket: {category_name[:30]}")
        self.cat_id = cat_id
        self.cog = cog

        self.subject_input = discord.ui.TextInput(
            label="Subject / Issue Title",
            placeholder="Brief summary of your issue...",
            max_length=100,
            required=True
        )
        self.details_input = discord.ui.TextInput(
            label="Details / Description",
            style=discord.TextStyle.paragraph,
            placeholder="Describe your issue in detail (credentials, error logs, context)...",
            max_length=2000,
            required=True
        )
        self.add_item(self.subject_input)
        self.add_item(self.details_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.process_ticket_creation(
            interaction=interaction,
            cat_id=self.cat_id,
            subject=self.subject_input.value.strip(),
            initial_details=self.details_input.value.strip()
        )


class TicketCloseModal(discord.ui.Modal):
    """Modal prompted when staff or creator clicks Close Ticket."""
    def __init__(self, ticket_id: int, cog: "TicketsCog"):
        super().__init__(title=f"Close Ticket #{ticket_id:04d}")
        self.ticket_id = ticket_id
        self.cog = cog

        self.reason_input = discord.ui.TextInput(
            label="Resolution Note / Close Reason",
            style=discord.TextStyle.paragraph,
            placeholder="Optional summary of resolution or closing note...",
            max_length=1000,
            required=False
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason = self.reason_input.value.strip() or "No resolution reason provided."
        await self.cog.execute_close_ticket(
            ticket_id=self.ticket_id,
            closed_by=interaction.user,
            reason=reason,
            interaction=interaction
        )


class TicketAssignView(discord.ui.View):
    """Ephemeral view allowing staff to assign a Role or Member on the fly."""
    def __init__(self, ticket_id: int, cog: "TicketsCog"):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.cog = cog

        self.role_select = discord.ui.RoleSelect(
            placeholder="👥 Select a Role to assign & grant thread access...",
            min_values=1,
            max_values=1,
            row=0
        )
        self.role_select.callback = self.on_role_select
        self.add_item(self.role_select)

        self.user_select = discord.ui.UserSelect(
            placeholder="👤 Select a Member to add to this private thread...",
            min_values=1,
            max_values=1,
            row=1
        )
        self.user_select.callback = self.on_user_select
        self.add_item(self.user_select)

    async def on_role_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.role_select.values:
            return
        selected_role = self.role_select.values[0]
        await self.cog.execute_assign_role(self.ticket_id, selected_role, interaction)

    async def on_user_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.user_select.values:
            return
        selected_user = self.user_select.values[0]
        await self.cog.execute_assign_user(self.ticket_id, selected_user, interaction)


# =============================================================================
# Tickets Cog Class
# =============================================================================

class TicketsCog(commands.Cog):
    """Handles Ticket Panel interactions, Private Threads, and Transcripts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = TicketsDB.get_instance()

    async def cog_load(self):
        """Registers persistent dynamic items and initializes database."""
        await self.db.init_db()
        self.bot.add_dynamic_items(DynamicTicketCategoryButton, DynamicTicketActionButton)
        logger.info("[OK] TicketsCog loaded and dynamic items registered.")

    # =========================================================================
    # Permission & Staff Verification Helpers
    # =========================================================================

    def is_staff_or_admin(self, member: discord.Member, ticket: Dict[str, Any], settings: Dict[str, Any]) -> bool:
        """Determines if a member has ticket management authority."""
        if not isinstance(member, discord.Member):
            return False
        # Server Administrator or Manage Guild / Manage Channels
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_channels:
            return True

        member_id_str = str(member.id)
        # Check global support member list
        if member_id_str in settings.get("support_member_ids", []):
            return True
        # Check ticket assigned users
        if member_id_str in ticket.get("assigned_users", []):
            return True

        # Check global support roles and ticket assigned roles
        member_role_ids = {str(r.id) for r in member.roles}
        global_support_roles = set(settings.get("support_role_ids", []))
        if member_role_ids & global_support_roles:
            return True

        ticket_roles = set(ticket.get("assigned_roles", []))
        if member_role_ids & ticket_roles:
            return True

        return False

    # =========================================================================
    # Category Button Click & Modal Prompt
    # =========================================================================

    async def handle_category_click(self, interaction: discord.Interaction, cat_id: int):
        """Verifies rate limits and opens the issue submission modal."""
        if not interaction.guild:
            await interaction.response.send_message("❌ Tickets can only be created within a server.", ephemeral=True)
            return

        settings = await self.db.get_settings(interaction.guild.id)
        max_open = settings.get("max_open_per_user", 2)
        open_count = await self.db.count_open_tickets_for_user(interaction.guild.id, interaction.user.id)

        if open_count >= max_open:
            await interaction.response.send_message(
                f"❌ You currently have **{open_count}** open ticket(s). "
                f"The maximum allowed per member is **{max_open}**. "
                f"Please close an existing ticket before opening a new one.",
                ephemeral=True
            )
            return

        category = await self.db.get_category(cat_id)
        cat_name = category["name"] if category else "General Support"
        modal = TicketOpenModal(cat_id=cat_id, category_name=cat_name, cog=self)
        await interaction.response.send_modal(modal)

    # =========================================================================
    # Ticket Creation & Private Thread Spawning
    # =========================================================================

    async def process_ticket_creation(
        self,
        interaction: discord.Interaction,
        cat_id: int,
        subject: str,
        initial_details: str
    ):
        """Creates the private thread, adds routing, and posts the control message."""
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Discord server not found.", ephemeral=True)
            return

        settings = await self.db.get_settings(guild.id)
        panel_channel_id = int(settings.get("panel_channel_id") or 0)
        panel_channel = guild.get_channel(panel_channel_id) if panel_channel_id else None

        if not panel_channel or not isinstance(panel_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Ticket panel channel is not configured or is not a valid text channel. "
                "Please contact server staff.",
                ephemeral=True
            )
            return

        category = await self.db.get_category(cat_id)
        cat_name = category["name"] if category else "General Support"

        # Combine global and per-category role/member routing
        assigned_roles = list(set(
            settings.get("support_role_ids", []) + (category.get("assigned_role_ids", []) if category else [])
        ))
        assigned_members = list(set(
            settings.get("support_member_ids", []) + (category.get("assigned_member_ids", []) if category else [])
        ))

        # Insert placeholder ticket to obtain unique auto-increment ticket_id
        ticket_id = await self.db.create_ticket(
            guild_id=guild.id,
            thread_id=0,
            panel_channel_id=panel_channel.id,
            category_id=category["id"] if category else None,
            category_name=cat_name,
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            subject=subject,
            initial_details=initial_details,
            assigned_roles=assigned_roles,
            assigned_users=assigned_members
        )

        clean_user_name = re.sub(r"[^a-zA-Z0-9_-]", "", interaction.user.name)[:14] or "user"
        thread_name = f"ticket-{ticket_id:04d}-{clean_user_name}"

        try:
            thread = await panel_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason=f"Ticket #{ticket_id} ({cat_name}) opened by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot lacks permission to create private threads in the designated ticket channel. "
                "Please grant 'Create Private Threads' and 'Send Messages in Threads'.",
                ephemeral=True
            )
            return
        except Exception as e:
            logger.error(f"Failed to create private thread: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to spawn private thread: {e}", ephemeral=True)
            return

        # Update actual thread ID in database
        await self.db.update_ticket_thread_id(ticket_id, thread.id)

        # 1. Add ticket creator to thread
        try:
            await thread.add_user(interaction.user)
        except Exception as e:
            logger.warning(f"Could not add creator {interaction.user} to thread {thread.id}: {e}")

        # 2. Add configured assigned support members
        for uid in assigned_members:
            try:
                m = guild.get_member(int(uid))
                if m and m.id != interaction.user.id:
                    await thread.add_user(m)
            except Exception as e:
                logger.warning(f"Failed to add assigned member {uid} to thread {thread.id}: {e}")

        # 3. Mention roles in opening message (grants private thread access in Discord)
        role_pings = " ".join([f"<@&{rid}>" for rid in assigned_roles if guild.get_role(int(rid))])

        # 4. Construct Opening Control Embed & Buttons
        panel_color = parse_hex_color(settings.get("panel_color"))
        embed = discord.Embed(
            title=f"🎫 Ticket #{ticket_id:04d} — {cat_name}",
            color=panel_color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.description = f"**Subject:** {subject}\n\n**Initial Details:**\n{initial_details}"
        embed.add_field(name="Ticket Creator", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=True)
        embed.add_field(name="Status", value="🟢 `Open`", inline=True)
        embed.add_field(name="Category", value=f"{category.get('emoji', '🎫') if category else '🎫'} {cat_name}", inline=True)
        embed.set_footer(text="Support staff will respond shortly. Please do not share passwords publicly.")

        action_view = discord.ui.View(timeout=None)
        action_view.add_item(DynamicTicketActionButton("claim", ticket_id, label="Claim Ticket", emoji="🙋‍♂️", style=discord.ButtonStyle.primary))
        action_view.add_item(DynamicTicketActionButton("assign", ticket_id, label="Assign Role / Member", emoji="👥", style=discord.ButtonStyle.secondary))
        action_view.add_item(DynamicTicketActionButton("close", ticket_id, label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger))

        content_parts = [interaction.user.mention]
        if role_pings:
            content_parts.append(role_pings)
        content_msg = " ".join(content_parts)

        await thread.send(content=content_msg, embed=embed, view=action_view)

        await interaction.followup.send(
            f"✅ Your secure private ticket thread has been created: {thread.mention}",
            ephemeral=True
        )

    # =========================================================================
    # In-Thread Action Button Handlers (Claim, Assign, Close)
    # =========================================================================

    async def handle_ticket_action(self, interaction: discord.Interaction, action: str, ticket_id: int):
        """Routes actions from persistent buttons in the ticket thread."""
        ticket = await self.db.get_ticket(ticket_id)
        if not ticket:
            await interaction.response.send_message("❌ Ticket record not found in database.", ephemeral=True)
            return

        settings = await self.db.get_settings(int(ticket["guild_id"]))
        is_staff = self.is_staff_or_admin(interaction.user, ticket, settings)
        is_creator = (str(interaction.user.id) == ticket["user_id"])

        if action == "claim":
            if not is_staff:
                await interaction.response.send_message("❌ Only support staff or administrators can claim tickets.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await self.db.claim_ticket(ticket_id, interaction.user.id, str(interaction.user))
            await interaction.channel.send(f"🙋‍♂️ **{interaction.user.mention}** has claimed Ticket #{ticket_id:04d}.")
            await interaction.followup.send(f"✅ You have successfully claimed Ticket #{ticket_id:04d}.", ephemeral=True)

        elif action == "assign":
            if not is_staff:
                await interaction.response.send_message("❌ Only support staff or administrators can assign roles or members.", ephemeral=True)
                return
            view = TicketAssignView(ticket_id=ticket_id, cog=self)
            await interaction.response.send_message(
                "👥 **Assign Role or Member:**\nSelect a role or member below to grant them access to this private thread.",
                view=view,
                ephemeral=True
            )

        elif action == "close":
            if not (is_staff or is_creator):
                await interaction.response.send_message("❌ You do not have permission to close this ticket.", ephemeral=True)
                return
            modal = TicketCloseModal(ticket_id=ticket_id, cog=self)
            await interaction.response.send_modal(modal)

    # =========================================================================
    # Assign Role / Member Implementation
    # =========================================================================

    async def execute_assign_role(self, ticket_id: int, role: discord.Role, interaction: discord.Interaction):
        """Assigns a role to a ticket, updating DB and mentioning in thread for access."""
        thread = interaction.channel
        await self.db.assign_role_to_ticket(ticket_id, str(role.id))
        if isinstance(thread, (discord.Thread, discord.TextChannel)):
            await thread.send(f"👥 {role.mention} has been assigned to this ticket by {interaction.user.mention}.")
        await interaction.followup.send(f"✅ Role **@{role.name}** assigned and granted thread access.", ephemeral=True)

    async def execute_assign_user(self, ticket_id: int, user: discord.Member, interaction: discord.Interaction):
        """Assigns a user to a ticket, adding them to the private thread and updating DB."""
        thread = interaction.channel
        await self.db.assign_user_to_ticket(ticket_id, str(user.id))
        if isinstance(thread, discord.Thread):
            try:
                await thread.add_user(user)
            except Exception as e:
                logger.warning(f"Could not add user {user.id} to thread: {e}")
            await thread.send(f"👤 {user.mention} has been added to this ticket by {interaction.user.mention}.")
        await interaction.followup.send(f"✅ Member **{user}** added to this private thread.", ephemeral=True)

    # =========================================================================
    # Ticket Closing, As-Is Transcript Archiving & Locking
    # =========================================================================

    async def execute_close_ticket(
        self,
        ticket_id: int,
        closed_by: Any,
        reason: str,
        interaction: Optional[discord.Interaction] = None
    ) -> bool:
        """
        Executes complete ticket closure:
        1. Archives all thread messages as-is into ticket_messages in SQLite.
        2. Generates HTML & TXT transcripts asynchronously.
        3. Posts transcript files to transcript_channel_id if configured.
        4. Locks and archives the private thread on Discord.
        """
        ticket = await self.db.get_ticket(ticket_id)
        if not ticket:
            if interaction:
                await interaction.followup.send("❌ Ticket not found.", ephemeral=True)
            return False

        if ticket.get("status") == "closed":
            if interaction:
                await interaction.followup.send("⚠️ This ticket is already closed.", ephemeral=True)
            return True

        guild_id = int(ticket["guild_id"])
        guild = self.bot.get_guild(guild_id)
        settings = await self.db.get_settings(guild_id)

        # Locate Discord Thread
        thread_id = int(ticket["thread_id"])
        thread = self.bot.get_channel(thread_id)
        if not thread and guild:
            try:
                thread = await guild.fetch_channel(thread_id)
            except Exception:
                thread = None

        # 1. Harvest complete message history from Discord Thread
        msg_records = []
        if thread and isinstance(thread, discord.Thread):
            try:
                async for m in thread.history(limit=1000, oldest_first=True):
                    # Check if author was staff
                    is_staff_msg = False
                    if isinstance(m.author, discord.Member):
                        is_staff_msg = self.is_staff_or_admin(m.author, ticket, settings)
                    elif m.author.id == self.bot.user.id:
                        is_staff_msg = True

                    att_urls = [a.url for a in m.attachments]
                    msg_records.append({
                        "message_id": m.id,
                        "author_id": m.author.id,
                        "author_name": str(m.author),
                        "author_avatar": m.author.display_avatar.url if m.author.display_avatar else None,
                        "is_staff": is_staff_msg,
                        "content": m.content,  # Exact as-is content!
                        "attachments": att_urls,
                        "attachments_json": json.dumps(att_urls),
                        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    })
            except Exception as e:
                logger.error(f"Error harvesting messages from thread {thread_id}: {e}")

        # 2. Batch insert into ticket_messages
        if msg_records:
            await self.db.save_messages_batch(ticket_id, msg_records)

        # 3. Mark ticket closed in database
        closed_by_id = getattr(closed_by, "id", None)
        closed_by_name = str(closed_by)
        await self.db.close_ticket(
            ticket_id=ticket_id,
            closed_by_id=closed_by_id,
            closed_by_name=closed_by_name,
            close_reason=reason
        )

        # 4. Generate formatted HTML and TXT transcripts in background threads
        ticket_for_export = dict(ticket)
        ticket_for_export["status"] = "closed"
        ticket_for_export["closed_by_name"] = closed_by_name
        ticket_for_export["closed_by_id"] = str(closed_by_id) if closed_by_id else "0"
        ticket_for_export["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        ticket_for_export["close_reason"] = reason

        guild_name = guild.name if guild else "Discord Server"
        html_content = await asyncio.to_thread(generate_html_transcript, ticket_for_export, msg_records, guild_name)
        txt_content = await asyncio.to_thread(generate_txt_transcript, ticket_for_export, msg_records)

        # 5. Post to Transcript Channel if configured
        transcript_ch_id = int(settings.get("transcript_channel_id") or 0)
        if transcript_ch_id and guild:
            transcript_channel = guild.get_channel(transcript_ch_id)
            if transcript_channel and isinstance(transcript_channel, discord.TextChannel):
                try:
                    embed = discord.Embed(
                        title=f"📜 Ticket #{ticket_id:04d} Closed Transcript",
                        color=discord.Color.dark_grey(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    embed.add_field(name="Subject", value=ticket["subject"], inline=False)
                    embed.add_field(name="Opener", value=f"{ticket['user_name']} (`{ticket['user_id']}`)", inline=True)
                    embed.add_field(name="Category", value=ticket["category_name"], inline=True)
                    embed.add_field(name="Closed By", value=closed_by_name, inline=True)
                    embed.add_field(name="Resolution Note", value=reason, inline=False)

                    html_file = discord.File(io.BytesIO(html_content.encode("utf-8")), filename=f"ticket-{ticket_id:04d}-transcript.html")
                    txt_file = discord.File(io.BytesIO(txt_content.encode("utf-8")), filename=f"ticket-{ticket_id:04d}-transcript.txt")

                    await transcript_channel.send(embed=embed, files=[html_file, txt_file])
                except Exception as e:
                    logger.error(f"Failed to post transcript to transcript channel: {e}")

        # 5b. Send .txt transcript via DM to the ticket creator
        try:
            creator_id = int(ticket["user_id"])
            creator = self.bot.get_user(creator_id)
            if not creator:
                try:
                    creator = await self.bot.fetch_user(creator_id)
                except Exception as e:
                    logger.warning(f"Could not fetch ticket creator {creator_id}: {e}")
                    creator = None

            if creator:
                dm_embed = discord.Embed(
                    title=f"🎫 Ticket #{ticket_id:04d} Closed — {ticket.get('category_name', 'Support')}",
                    color=discord.Color.blue(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.description = (
                    f"Hello {creator.mention}, your support ticket in **{guild_name}** has been closed.\n\n"
                    f"📄 **A complete text transcript (.txt) of your conversation is attached below for your records.**"
                )
                dm_embed.add_field(name="Subject", value=ticket.get("subject", "Support Ticket"), inline=False)
                dm_embed.add_field(name="Closed By", value=str(closed_by_name), inline=True)
                dm_embed.add_field(name="Resolution / Reason", value=str(reason) if reason else "No resolution reason provided.", inline=False)

                if guild and guild.icon:
                    dm_embed.set_footer(text=f"{guild_name} Support Desk", icon_url=guild.icon.url)
                else:
                    dm_embed.set_footer(text=f"{guild_name} Support Desk")

                dm_txt_file = discord.File(
                    io.BytesIO(txt_content.encode("utf-8")),
                    filename=f"ticket-{ticket_id:04d}-transcript.txt"
                )
                await creator.send(embed=dm_embed, file=dm_txt_file)
                logger.info(f"Successfully sent .txt transcript DM to user {creator_id} for ticket #{ticket_id:04d}")
        except discord.Forbidden:
            logger.warning(f"Could not DM user {ticket.get('user_id')} for ticket #{ticket_id:04d}: Direct messages disabled by user.")
        except Exception as e:
            logger.error(f"Error sending DM transcript to user {ticket.get('user_id')}: {e}", exc_info=True)

        # 6. Post closing message, send interaction response, and delete thread
        if interaction:
            try:
                await interaction.followup.send(
                    f"✅ Ticket #{ticket_id:04d} has been closed. A text transcript has been sent to the ticket creator's DMs.",
                    ephemeral=True
                )
            except Exception as e:
                logger.warning(f"Could not send close interaction followup: {e}")

        if thread and isinstance(thread, discord.Thread):
            try:
                await thread.send(
                    f"🔒 **Ticket #{ticket_id:04d} was closed by {closed_by_name}**.\n"
                    f"**Resolution Note:** {reason}\n"
                    f"*A complete transcript has been saved and sent to the ticket creator.*\n"
                    f"🗑️ *Deleting thread...*"
                )
            except Exception as e:
                logger.warning(f"Could not send close notice to thread {thread.id}: {e}")

            try:
                await thread.edit(locked=True, archived=True)
            except Exception as e:
                logger.warning(f"Could not lock/archive thread {thread.id}: {e}")

            try:
                await thread.delete()
                logger.info(f"Deleted thread {thread.id} for ticket #{ticket_id:04d}")
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"Could not delete thread {thread.id}: {e}")

        return True

    # =========================================================================
    # Publish & Sync Public Ticket Panel
    # =========================================================================

    async def publish_or_update_panel(self, guild_id: int) -> Tuple[bool, str, Optional[str]]:
        """
        Publishes or updates the live ticket panel embed and category buttons on Discord.
        Returns: (success: bool, message: str, panel_message_id: Optional[str])
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return False, "Guild not found on bot instance.", None

        settings = await self.db.get_settings(guild_id)
        panel_ch_id = int(settings.get("panel_channel_id") or 0)
        if not panel_ch_id:
            return False, "Ticket panel channel is not configured.", None

        channel = guild.get_channel(panel_ch_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return False, "Target panel channel is not a valid text channel.", None

        categories = await self.db.get_categories(guild_id)
        if not categories:
            return False, "No ticket categories configured.", None

        # Build Discord Embed
        panel_color = parse_hex_color(settings.get("panel_color"))
        embed = discord.Embed(
            title=settings.get("panel_title") or "🛡️ Official Support & Issue Desk",
            description=settings.get("panel_description") or "Click a category button below to open a secure Private Thread.",
            color=panel_color
        )
        embed.set_footer(text="Private Threads • Only assigned staff & ticket creator have access")

        # Build Dynamic Action View
        view = discord.ui.View(timeout=None)
        for cat in categories[:25]:  # Discord 25 buttons limit per view
            style = button_style_from_str(cat.get("button_style"))
            btn = DynamicTicketCategoryButton(
                cat_id=cat["id"],
                label=cat["name"][:80],
                emoji=cat.get("emoji") or "🎫",
                style=style
            )
            view.add_item(btn)

        # Edit existing or send new message
        existing_msg_id = int(settings.get("panel_message_id") or 0)
        sent_msg: Optional[discord.Message] = None

        if existing_msg_id:
            try:
                msg = await channel.fetch_message(existing_msg_id)
                await msg.edit(embed=embed, view=view)
                sent_msg = msg
            except Exception:
                sent_msg = None

        if not sent_msg:
            try:
                sent_msg = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                return False, "Bot lacks permission to send messages in the panel channel.", None
            except Exception as e:
                return False, f"Failed to send panel message: {e}", None

        await self.db.update_panel_message_id(guild_id, sent_msg.id)
        return True, "Ticket panel successfully published to Discord.", str(sent_msg.id)


async def setup(bot: commands.Bot):
    """Cog setup hook for ModularBot."""
    await bot.add_cog(TicketsCog(bot))
