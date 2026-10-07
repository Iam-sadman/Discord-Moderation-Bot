"""
Leave of Absence (LOA) management cog for Discord.
Supports self-service applications via modals, checker review flows,
live telemetry dashboard with pagination, and automatic expiration.
"""

import asyncio
import csv
import io
import logging
import os
from datetime import datetime, time, timezone, timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from config import Config

logger = logging.getLogger("LoaCog")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "loa.db")
ITEMS_PER_PAGE = 10

# Timezone configuration (defaults to UTC+6 / Bangladesh Standard Time)
TZ_OFFSET_HOURS = int(os.getenv("LOA_TIMEZONE_OFFSET", "6"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# Schedule midnight run at 12:00:05 AM (00:00:05) local time
MIDNIGHT_RUN_TIME = time(hour=0, minute=0, second=5, tzinfo=LOCAL_TZ)

# Default Team options for the application dropdown
DEFAULT_TEAMS = [
    "Delta Force",
    "Nano Banana",
    "Golden Tshushima",
    "Rafael's Carten [1989]",
    "Night Owls",
    "Totoro",
    "Athena",
    "Flash Point",
    "Rising Horizon",
    "Pixel Hunter",
]


# ============================================================
# DATABASE UTILITIES
# ============================================================

async def init_loa_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loa_settings (
                guild_id INTEGER PRIMARY KEY,
                dashboard_channel_id INTEGER,
                dashboard_message_id INTEGER,
                review_channel_id INTEGER,
                labeler_role_id INTEGER,
                checker_role_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loa_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                site_name TEXT,
                team_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL, -- 'pending', 'approved', 'rejected', 'expired', 'ended_early', 'pending_extension'
                applied_at TIMESTAMP NOT NULL,
                reviewed_by INTEGER,
                reviewed_by_name TEXT,
                reviewed_at TIMESTAMP,
                rejection_reason TEXT,
                approval_comment TEXT,
                review_channel_msg_id INTEGER,
                ended_early_at TIMESTAMP,
                extension_of_id INTEGER
            )
        """)
        try:
            await db.execute("ALTER TABLE loa_requests ADD COLUMN ended_early_at TIMESTAMP")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE loa_requests ADD COLUMN extension_of_id INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE loa_requests ADD COLUMN approval_comment TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE loa_requests ADD COLUMN site_name TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE loa_settings ADD COLUMN timezone_offset INTEGER DEFAULT 6")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE loa_settings ADD COLUMN report_role_id INTEGER")
        except Exception:
            pass
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_loa_guild_status 
            ON loa_requests(guild_id, status)
        """)
        await db.commit()


# ============================================================
# UI MODALS
# ============================================================

class LoaApplyModal(discord.ui.Modal, title="Leave of Absence (LOA) Application"):
    """Pop-up modal for members applying for Leave of Absence."""

    def __init__(self, cog: "Loa", teams: Optional[list[str]] = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.teams = teams or DEFAULT_TEAMS

        self.site_name = discord.ui.TextInput(
            label="Site Name",
            placeholder="e.g. ARC_abcdefg",
            max_length=100,
            required=True,
        )
        self.team_select = discord.ui.Select(
            placeholder="Choose your team...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=t, value=t) for t in self.teams],
        )
        self.start_date = discord.ui.TextInput(
            label="Start Date (YYYY-MM-DD)",
            placeholder="e.g. 2026-10-05",
            min_length=10,
            max_length=10,
            required=True,
        )
        self.end_date = discord.ui.TextInput(
            label="End Date (YYYY-MM-DD)",
            placeholder="e.g. 2026-10-10",
            min_length=10,
            max_length=10,
            required=True,
        )
        self.reason = discord.ui.TextInput(
            label="Reason for Leave",
            placeholder="Briefly describe the reason for your absence...",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )

        self.add_item(self.site_name)
        self.add_item(discord.ui.Label(text="Team Name", description="Select your assigned team", component=self.team_select))
        self.add_item(self.start_date)
        self.add_item(self.end_date)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate date formats (YYYY-MM-DD)
        start_str = str(self.start_date.value).strip()
        end_str = str(self.end_date.value).strip()

        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
        except ValueError:
            await interaction.response.send_message(
                "❌ **Invalid Start Date!** Please use format: `YYYY-MM-DD` (e.g. `2026-10-05`).",
                ephemeral=True
            )
            return

        try:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            await interaction.response.send_message(
                "❌ **Invalid End Date!** Please use format: `YYYY-MM-DD` (e.g. `2026-10-10`).",
                ephemeral=True
            )
            return

        if end_dt < start_dt:
            await interaction.response.send_message(
                "❌ **Invalid Range!** End Date cannot be earlier than Start Date.",
                ephemeral=True
            )
            return

        total_days = (end_dt - start_dt).days + 1

        # Check guild settings
        settings = await self.cog.get_settings(interaction.guild_id)
        if not settings or not settings.get("review_channel_id"):
            await interaction.response.send_message(
                "❌ The LOA review channel has not been configured yet. Please ask an Administrator to run `/loa_setup`.",
                ephemeral=True
            )
            return

        labeler_role_id = settings.get("labeler_role_id")
        has_labeler_role = any(role.id == labeler_role_id for role in interaction.user.roles) if labeler_role_id else False
        if not has_labeler_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only members with the **Labeler** role can apply for a Leave of Absence (LOA).",
                ephemeral=True
            )
            return

        review_channel = interaction.guild.get_channel(settings["review_channel_id"])
        if not review_channel:
            await interaction.response.send_message(
                "❌ The configured LOA review channel could not be found. Please contact an admin.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        user_name_val = interaction.user.display_name
        site_name_val = str(self.site_name.value).strip()
        team_name_val = self.team_select.values[0] if self.team_select.values else "General"
        reason_val = str(self.reason.value).strip()

        # Insert record into DB
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """
                INSERT INTO loa_requests (
                    guild_id, user_id, user_name, site_name, team_name, reason,
                    start_date, end_date, status, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    interaction.guild_id,
                    interaction.user.id,
                    user_name_val,
                    site_name_val,
                    team_name_val,
                    reason_val,
                    start_str,
                    end_str,
                    now_iso
                )
            )
            request_id = cursor.lastrowid
            await db.commit()

        # Send card to the review channel
        review_embed = discord.Embed(
            title=f"📋 New LOA Application #{request_id}",
            description=f"A new Leave of Absence has been submitted by {interaction.user.mention}.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        review_embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{user_name_val}`)", inline=True)
        review_embed.add_field(name="Team", value=team_name_val, inline=True)
        review_embed.add_field(name="Site Name", value=site_name_val, inline=True)
        review_embed.add_field(name="Duration", value=f"**{start_str}** to **{end_str}** ({total_days} day{'s' if total_days > 1 else ''})", inline=False)
        review_embed.add_field(name="Reason", value=reason_val, inline=False)
        review_embed.set_thumbnail(url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        review_embed.set_footer(text=f"Request ID: {request_id} • Status: Waiting for Checker")

        review_view = LoaReviewView(request_id)
        review_msg = await review_channel.send(embed=review_embed, view=review_view)

        # Update msg_id in DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE loa_requests SET review_channel_msg_id = ? WHERE id = ?",
                (review_msg.id, request_id)
            )
            await db.commit()

        # Confirmation to applicant
        await interaction.followup.send(
            f"✅ **Your LOA request `#{request_id}` has been successfully submitted!**\n"
            f"👥 Team: **{team_name_val}** • 🏷️ Site: `{site_name_val}`\n"
            f"📅 Period: `{start_str}` to `{end_str}` ({total_days} days)\n"
            f"Checkers have been notified in the review channel.",
            ephemeral=True
        )

        # Refresh dashboard in public channel
        await self.cog.refresh_dashboard(interaction.guild_id)


class LoaApproveModal(discord.ui.Modal, title="Approve LOA Request"):
    """Modal for checkers to optionally provide a comment upon approval."""

    comment = discord.ui.TextInput(
        label="Comment / Note (Optional)",
        placeholder="Add an optional comment or note for the applicant...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, cog: "Loa", request_id: int, is_extension: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.request_id = request_id
        self.is_extension = is_extension
        if is_extension:
            self.title = "Approve LOA Extension"

    async def on_submit(self, interaction: discord.Interaction):
        approval_comment = str(self.comment.value).strip() or None

        if self.is_extension:
            await self.cog.process_extension_review(
                interaction=interaction,
                ext_id=self.request_id,
                action="approve",
                approval_comment=approval_comment
            )
        else:
            await self.cog.process_review(
                interaction=interaction,
                request_id=self.request_id,
                action="approve",
                approval_comment=approval_comment
            )


class LoaRejectModal(discord.ui.Modal, title="Deny LOA Request"):
    """Modal for checkers to optionally specify a rejection reason / note."""

    reason = discord.ui.TextInput(
        label="Reason for Denial (Optional)",
        placeholder="Explain why this request is being denied (optional)...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, cog: "Loa", request_id: int, is_extension: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.request_id = request_id
        self.is_extension = is_extension
        if is_extension:
            self.title = "Deny LOA Extension"

    async def on_submit(self, interaction: discord.Interaction):
        rejection_reason = str(self.reason.value).strip() or None

        if self.is_extension:
            await self.cog.process_extension_review(
                interaction=interaction,
                ext_id=self.request_id,
                action="reject",
                rejection_reason=rejection_reason
            )
        else:
            await self.cog.process_review(
                interaction=interaction,
                request_id=self.request_id,
                action="reject",
                rejection_reason=rejection_reason
            )


class LoaExtendModal(discord.ui.Modal, title="Extend Leave of Absence (LOA)"):
    """Modal for members wishing to extend their active approved LOA."""

    current_end_date = discord.ui.TextInput(
        label="Current Scheduled End Date",
        placeholder="YYYY-MM-DD",
        required=False,
    )
    new_end_date = discord.ui.TextInput(
        label="New Extended End Date (YYYY-MM-DD)",
        placeholder="e.g. 2026-10-15",
        min_length=10,
        max_length=10,
        required=True,
    )
    reason = discord.ui.TextInput(
        label="Reason for Extension",
        placeholder="Explain why you need to extend your leave...",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500,
        required=True,
    )

    def __init__(self, cog: "Loa", parent_loa: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.parent_loa = parent_loa
        curr_end = parent_loa.get("end_date", "")
        self.current_end_date.default = curr_end
        self.new_end_date.placeholder = f"Must be after {curr_end} (YYYY-MM-DD)"

    async def on_submit(self, interaction: discord.Interaction):
        new_end_str = str(self.new_end_date.value).strip()
        reason_val = str(self.reason.value).strip()
        current_end_str = self.parent_loa.get("end_date", "")

        try:
            new_end_dt = datetime.strptime(new_end_str, "%Y-%m-%d").date()
        except ValueError:
            await interaction.response.send_message(
                "❌ **Invalid New End Date!** Please use format: `YYYY-MM-DD` (e.g. `2026-10-15`).",
                ephemeral=True
            )
            return

        try:
            curr_end_dt = datetime.strptime(current_end_str, "%Y-%m-%d").date()
        except Exception:
            curr_end_dt = datetime.now(LOCAL_TZ).date()

        if new_end_dt <= curr_end_dt:
            await interaction.response.send_message(
                f"❌ **Invalid Date!** The new end date (`{new_end_str}`) must be later than your current scheduled end date (`{current_end_str}`).",
                ephemeral=True
            )
            return

        added_days = (new_end_dt - curr_end_dt).days

        # Check guild settings
        settings = await self.cog.get_settings(interaction.guild_id)
        if not settings or not settings.get("review_channel_id"):
            await interaction.response.send_message(
                "❌ The LOA review channel has not been configured yet. Please ask an Administrator to run `/loa_setup`.",
                ephemeral=True
            )
            return

        review_channel = interaction.guild.get_channel(settings["review_channel_id"])
        if not review_channel:
            await interaction.response.send_message(
                "❌ The configured LOA review channel could not be found. Please contact an admin.",
                ephemeral=True
            )
            return

        parent_id = self.parent_loa["id"]

        # Check if already pending extension
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT id FROM loa_requests
                WHERE guild_id = ? AND user_id = ? AND status = 'pending_extension' AND extension_of_id = ?
            """, (interaction.guild_id, interaction.user.id, parent_id))
            existing_ext = await cursor.fetchone()

        if existing_ext:
            await interaction.response.send_message(
                f"⚠️ You already have an extension request (`#{existing_ext['id']}`) awaiting checker approval.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        now_iso = datetime.now(timezone.utc).isoformat()

        # Insert extension request
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO loa_requests (
                    guild_id, user_id, user_name, site_name, team_name, reason,
                    start_date, end_date, status, applied_at, extension_of_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_extension', ?, ?)
            """, (
                interaction.guild_id,
                interaction.user.id,
                self.parent_loa.get("user_name", interaction.user.display_name),
                self.parent_loa.get("site_name", ""),
                self.parent_loa.get("team_name", "General"),
                reason_val,
                self.parent_loa.get("start_date", current_end_str),
                new_end_str,
                now_iso,
                parent_id
            ))
            ext_id = cursor.lastrowid
            await db.commit()

        # Send card to review channel
        review_embed = discord.Embed(
            title=f"⏳ LOA Extension Request #{ext_id} (Parent #{parent_id})",
            description=f"{interaction.user.mention} is requesting to **EXTEND** their current Leave of Absence.",
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc)
        )
        review_embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{self.parent_loa.get('user_name', interaction.user.display_name)}`)", inline=True)
        review_embed.add_field(name="Team", value=self.parent_loa.get("team_name", "General"), inline=True)
        if self.parent_loa.get("site_name"):
            review_embed.add_field(name="Site Name", value=self.parent_loa.get("site_name"), inline=True)
        review_embed.add_field(name="Current Period", value=f"`{self.parent_loa.get('start_date', 'N/A')}` to `{current_end_str}`", inline=False)
        review_embed.add_field(
            name="Requested Extension",
            value=f"Extend until **{new_end_str}** (+{added_days} extra day{'s' if added_days > 1 else ''})",
            inline=False
        )
        review_embed.add_field(name="Reason for Extension", value=reason_val, inline=False)
        review_embed.set_thumbnail(url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        review_embed.set_footer(text=f"Extension ID: {ext_id} • Requires Checker Review")

        review_view = LoaExtensionReviewView(ext_id)
        review_msg = await review_channel.send(embed=review_embed, view=review_view)

        # Update msg_id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE loa_requests SET review_channel_msg_id = ? WHERE id = ?",
                (review_msg.id, ext_id)
            )
            await db.commit()

        # Confirmation to applicant
        await interaction.followup.send(
            f"✅ **Your LOA extension request `#{ext_id}` has been successfully submitted!**\n"
            f"📅 Current End Date: `{current_end_str}` ➔ New End Date: `{new_end_str}` (+{added_days} days)\n"
            "Checkers will review your request, and you will receive a notification once a decision is made.",
            ephemeral=True
        )

        await self.cog.refresh_dashboard(interaction.guild_id)


class LoaReportModal(discord.ui.Modal, title="Download LOA Report"):
    """Pop-up modal for authorized staff to export LOA records for a date range."""

    def __init__(self, cog: "Loa"):
        super().__init__(timeout=300)
        self.cog = cog

        today = datetime.now(LOCAL_TZ).date()
        month_start = today.replace(day=1)

        self.start_input = discord.ui.TextInput(
            placeholder="e.g. 2026-10-01",
            default=month_start.strftime("%Y-%m-%d"),
            min_length=10,
            max_length=10,
            required=True,
        )
        self.end_input = discord.ui.TextInput(
            placeholder="e.g. 2026-10-31",
            default=today.strftime("%Y-%m-%d"),
            min_length=10,
            max_length=10,
            required=True,
        )
        self.status_select = discord.ui.Select(
            placeholder="Choose which records to include",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Approved Only",
                    value="approved",
                    description="Active, completed & ended-early leaves",
                    emoji="✅",
                    default=True,
                ),
                discord.SelectOption(
                    label="All Records",
                    value="all",
                    description="Includes pending, denied & extension requests",
                    emoji="📚",
                ),
            ],
        )

        self.add_item(discord.ui.Label(text="Start Date", description="Format: YYYY-MM-DD", component=self.start_input))
        self.add_item(discord.ui.Label(text="End Date", description="Format: YYYY-MM-DD", component=self.end_input))
        self.add_item(discord.ui.Label(text="Status Filter", component=self.status_select))

    async def on_submit(self, interaction: discord.Interaction):
        # Re-verify authorization in case roles changed while the modal was open
        if not await self.cog.can_download_report(interaction):
            await self.cog.send_report_unauthorized(interaction)
            return

        start_str = str(self.start_input.value).strip()
        end_str = str(self.end_input.value).strip()
        error = self.cog.validate_report_range(start_str, end_str)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        filter_val = self.status_select.values[0] if self.status_select.values else "approved"

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, csv_file = await self.cog.generate_loa_report(
            interaction.guild_id, start_str, end_str, filter_val, interaction.user.display_name
        )
        await interaction.followup.send(embed=embed, file=csv_file, ephemeral=True)


# ============================================================
# UI VIEWS
# ============================================================

class LoaReviewView(discord.ui.View):
    """Buttons on the review card in the checker channel."""

    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

        btn_approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"loa:approve:{request_id}"
        )
        btn_approve.callback = self.on_approve
        self.add_item(btn_approve)

        btn_reject = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"loa:reject:{request_id}"
        )
        btn_reject.callback = self.on_reject
        self.add_item(btn_reject)

    async def on_approve(self, interaction: discord.Interaction):
        cog: Optional[Loa] = interaction.client.get_cog("Loa")
        if not cog:
            await interaction.response.send_message("❌ LOA module is not available.", ephemeral=True)
            return

        if not await cog.can_review(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to review this LOA. Only Checkers or Admins can review.",
                ephemeral=True
            )
            return

        modal = LoaApproveModal(cog, self.request_id)
        await interaction.response.send_modal(modal)

    async def on_reject(self, interaction: discord.Interaction):
        cog: Optional[Loa] = interaction.client.get_cog("Loa")
        if not cog:
            await interaction.response.send_message("❌ LOA module is not available.", ephemeral=True)
            return

        # Check permissions before opening modal
        if not await cog.can_review(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to review this LOA. Only Checkers or Admins can review.",
                ephemeral=True
            )
            return

        modal = LoaRejectModal(cog, self.request_id)
        await interaction.response.send_modal(modal)


class LoaExtensionReviewView(discord.ui.View):
    """Buttons on an extension review card in the checker channel."""

    def __init__(self, ext_id: int):
        super().__init__(timeout=None)
        self.ext_id = ext_id

        btn_approve = discord.ui.Button(
            label="Approve Extension",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"loa:approve_ext:{ext_id}"
        )
        btn_approve.callback = self.on_approve
        self.add_item(btn_approve)

        btn_reject = discord.ui.Button(
            label="Deny Extension",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"loa:reject_ext:{ext_id}"
        )
        btn_reject.callback = self.on_reject
        self.add_item(btn_reject)

    async def on_approve(self, interaction: discord.Interaction):
        cog: Optional[Loa] = interaction.client.get_cog("Loa")
        if not cog:
            await interaction.response.send_message("❌ LOA module is not available.", ephemeral=True)
            return

        if not await cog.can_review(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to review this LOA extension. Only Checkers or Admins can review.",
                ephemeral=True
            )
            return

        modal = LoaApproveModal(cog, self.ext_id, is_extension=True)
        await interaction.response.send_modal(modal)

    async def on_reject(self, interaction: discord.Interaction):
        cog: Optional[Loa] = interaction.client.get_cog("Loa")
        if not cog:
            await interaction.response.send_message("❌ LOA module is not available.", ephemeral=True)
            return

        if not await cog.can_review(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to review this LOA extension. Only Checkers or Admins can review.",
                ephemeral=True
            )
            return

        modal = LoaRejectModal(cog, self.ext_id, is_extension=True)
        await interaction.response.send_modal(modal)


class LoaEndEarlyConfirmView(discord.ui.View):
    """Confirmation prompt to end an active approved LOA early."""

    def __init__(self, cog: "Loa", request_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.request_id = request_id

    @discord.ui.button(label="Yes, End Leave Early", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.end_loa_early(interaction, self.request_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Action cancelled. Your LOA remains unchanged.", embed=None, view=None)


class LoaActiveLoaActionView(discord.ui.View):
    """View shown when someone with an active approved LOA clicks 'Apply' or views their status."""

    def __init__(self, cog: "Loa", parent_loa: dict | int):
        super().__init__(timeout=180)
        self.cog = cog
        if isinstance(parent_loa, int):
            self.parent_loa = {"id": parent_loa, "start_date": "", "end_date": "", "site_name": "", "team_name": "", "user_name": ""}
            self.parent_id = parent_loa
        else:
            self.parent_loa = parent_loa
            self.parent_id = parent_loa["id"]

    @discord.ui.button(label="Extend Current LOA", style=discord.ButtonStyle.primary, emoji="⏳", row=0)
    async def extend_loa(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = self.parent_loa
        if not parent.get("end_date"):
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (self.parent_id,))
                row = await cursor.fetchone()
                if row:
                    parent = dict(row)
                    self.parent_loa = parent

        # Check if already pending extension
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT id FROM loa_requests
                WHERE guild_id = ? AND user_id = ? AND status = 'pending_extension' AND extension_of_id = ?
            """, (interaction.guild_id, interaction.user.id, self.parent_id))
            existing_ext = await cursor.fetchone()

        if existing_ext:
            await interaction.response.send_message(
                f"⚠️ You already have an extension request (`#{existing_ext['id']}`) awaiting checker approval.",
                ephemeral=True
            )
            return

        modal = LoaExtendModal(self.cog, parent)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="End LOA Early & Return", style=discord.ButtonStyle.danger, emoji="⚡", row=0)
    async def end_early(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = LoaEndEarlyConfirmView(self.cog, self.parent_id)
        await interaction.response.edit_message(
            content="⚠️ **Are you sure you want to end your Leave of Absence early?**\nYou will immediately be marked as active and removed from the LOA list.",
            embed=None,
            view=view
        )

    @discord.ui.button(label="Apply for Another LOA", style=discord.ButtonStyle.secondary, emoji="📝", row=1)
    async def apply_anyway(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = LoaApplyModal(self.cog)
        await interaction.response.send_modal(modal)


# Alias for backward compatibility
LoaEndEarlyPromptView = LoaActiveLoaActionView


class LoaCancelPendingView(discord.ui.View):
    """View to cancel a pending LOA application."""

    def __init__(self, cog: "Loa", request_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.request_id = request_id

    @discord.ui.button(label="Cancel Application", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.cancel_pending_loa(interaction, self.request_id)

    @discord.ui.button(label="Keep Application", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Application kept pending.", view=None)


class LoaDashboardView(discord.ui.View):
    """Persistent controls on the public LOA dashboard."""

    def __init__(self, cog: "Loa", page: int = 0, total_pages: int = 1):
        super().__init__(timeout=None)
        self.cog = cog
        self.page = page
        self.total_pages = max(1, total_pages)
        self.setup_buttons()

    def setup_buttons(self):
        self.clear_items()

        # Row 0: Primary actions
        btn_apply = discord.ui.Button(
            label="📝 Apply for LOA",
            style=discord.ButtonStyle.success,
            custom_id="loa:dashboard:apply",
            row=0
        )
        btn_apply.callback = self.on_click_apply
        self.add_item(btn_apply)

        btn_end_early = discord.ui.Button(
            label="⚡ End LOA Early",
            style=discord.ButtonStyle.secondary,
            custom_id="loa:dashboard:end_early",
            row=0
        )
        btn_end_early.callback = self.on_click_end_early
        self.add_item(btn_end_early)

        btn_report = discord.ui.Button(
            label="📥 Download Report",
            style=discord.ButtonStyle.primary,
            custom_id="loa:dashboard:report",
            row=0
        )
        btn_report.callback = self.on_click_report
        self.add_item(btn_report)

        # Row 1: Pagination for active LOA members & refresh
        btn_prev = discord.ui.Button(
            label="◀️ Previous",
            style=discord.ButtonStyle.primary,
            disabled=(self.page <= 0 or self.total_pages <= 1),
            custom_id="loa:dashboard:prev",
            row=1
        )
        btn_prev.callback = self.on_click_prev
        self.add_item(btn_prev)

        btn_indicator = discord.ui.Button(
            label=f"Page {self.page + 1} of {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="loa:dashboard:page_info",
            row=1
        )
        self.add_item(btn_indicator)

        btn_next = discord.ui.Button(
            label="Next ▶️",
            style=discord.ButtonStyle.primary,
            disabled=(self.page >= self.total_pages - 1 or self.total_pages <= 1),
            custom_id="loa:dashboard:next",
            row=1
        )
        btn_next.callback = self.on_click_next
        self.add_item(btn_next)

        btn_refresh = discord.ui.Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="loa:dashboard:refresh",
            row=1
        )
        btn_refresh.callback = self.on_click_refresh
        self.add_item(btn_refresh)

    async def on_click_apply(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This action can only be used in a server.", ephemeral=True)
            return

        settings = await self.cog.get_settings(interaction.guild_id)
        labeler_role_id = settings.get("labeler_role_id")
        if not labeler_role_id:
            await interaction.response.send_message(
                "❌ The LOA system is not configured yet. Please ask an Administrator to run `/loa_setup`.",
                ephemeral=True
            )
            return

        has_labeler_role = any(role.id == labeler_role_id for role in interaction.user.roles)
        if not has_labeler_role and not interaction.user.guild_permissions.administrator:
            role = interaction.guild.get_role(labeler_role_id)
            role_name = role.name if role else "Labeler"
            await interaction.response.send_message(
                f"❌ Only members with the **@{role_name}** role can apply for a Leave of Absence.",
                ephemeral=True
            )
            return

        # Check if user currently has an approved active LOA running
        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM loa_requests
                WHERE guild_id = ? AND user_id = ? AND status = 'approved' AND end_date >= ?
                ORDER BY id DESC LIMIT 1
            """, (interaction.guild_id, interaction.user.id, today_str))
            active_loa = await cursor.fetchone()

        if active_loa:
            # Check if user already submitted an extension that is awaiting review
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT id FROM loa_requests
                    WHERE guild_id = ? AND user_id = ? AND status = 'pending_extension' AND extension_of_id = ?
                """, (interaction.guild_id, interaction.user.id, active_loa["id"]))
                existing_ext = await cursor.fetchone()

            if existing_ext:
                await interaction.response.send_message(
                    f"⚠️ You currently have an active approved LOA (`{active_loa['start_date']}` to `{active_loa['end_date']}`).\n"
                    f"You have already submitted an extension request (`#{existing_ext['id']}`) awaiting checker approval. You can apply again after a decision has been made.",
                    ephemeral=True
                )
                return

            # Directly open the extension popup form
            modal = LoaExtendModal(self.cog, dict(active_loa))
            await interaction.response.send_modal(modal)
            return

        # No running LOA: directly open standard LOA application form
        modal = LoaApplyModal(self.cog)
        await interaction.response.send_modal(modal)

    async def on_click_end_early(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This action can only be used in a server.", ephemeral=True)
            return

        settings = await self.cog.get_settings(interaction.guild_id)
        labeler_role_id = settings.get("labeler_role_id")
        if not labeler_role_id:
            await interaction.response.send_message(
                "❌ The LOA system is not configured yet. Please ask an Administrator to run `/loa_setup`.",
                ephemeral=True
            )
            return

        has_labeler_role = any(role.id == labeler_role_id for role in interaction.user.roles)
        if not has_labeler_role and not interaction.user.guild_permissions.administrator:
            role = interaction.guild.get_role(labeler_role_id)
            role_name = role.name if role else "Labeler"
            await interaction.response.send_message(
                f"❌ Only members with the **@{role_name}** role can use this LOA option.",
                ephemeral=True
            )
            return

        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM loa_requests
                WHERE guild_id = ? AND user_id = ? AND status = 'approved' AND end_date >= ?
                ORDER BY id DESC LIMIT 1
            """, (interaction.guild_id, interaction.user.id, today_str))
            active_loa = await cursor.fetchone()

        if not active_loa:
            # Check if applicant has a pending request
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT * FROM loa_requests
                    WHERE guild_id = ? AND user_id = ? AND status IN ('pending', 'pending_extension')
                    ORDER BY id DESC LIMIT 1
                """, (interaction.guild_id, interaction.user.id))
                pending_loa = await cursor.fetchone()

            if pending_loa:
                view = LoaCancelPendingView(self.cog, pending_loa["id"])
                await interaction.response.send_message(
                    f"ℹ️ You do not have an active approved LOA, but you have a **pending application `#{pending_loa['id']}`** (`{pending_loa['start_date']}` to `{pending_loa['end_date']}`).\n"
                    "Would you like to cancel this pending application?",
                    view=view,
                    ephemeral=True
                )
                return

            await interaction.response.send_message(
                "❌ You do not have an active or upcoming approved Leave of Absence (LOA) to end.",
                ephemeral=True
            )
            return

        view = LoaEndEarlyConfirmView(self.cog, active_loa["id"])
        site_str = f"• **Site Name:** {active_loa['site_name']}\n" if ("site_name" in active_loa.keys() and active_loa["site_name"]) else ""
        embed = discord.Embed(
            title="⚡ End Leave of Absence Early?",
            description=(
                f"You currently have an active approved Leave of Absence:\n\n"
                f"• **Request ID:** `#{active_loa['id']}`\n"
                f"• **Team:** {active_loa['team_name']}\n"
                f"{site_str}"
                f"• **Scheduled Duration:** `{active_loa['start_date']}` to `{active_loa['end_date']}`\n"
                f"• **Reason:** {active_loa['reason']}\n\n"
                "**If you confirm returning to duty early:**\n"
                "1. Your leave will be concluded immediately.\n"
                "2. You will be removed from the **Active LOA Members** list on the dashboard.\n"
                "3. An automatic notification will be sent to the checkers channel."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_click_report(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This action can only be used in a server.", ephemeral=True)
            return

        if not await self.cog.can_download_report(interaction):
            await self.cog.send_report_unauthorized(interaction)
            return

        await interaction.response.send_modal(LoaReportModal(self.cog))

    async def on_click_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        embed, total_pages = await self.cog.build_dashboard_embed(interaction.guild, page=self.page)
        self.total_pages = total_pages
        self.setup_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_click_next(self, interaction: discord.Interaction):
        if self.page < self.total_pages - 1:
            self.page += 1
        embed, total_pages = await self.cog.build_dashboard_embed(interaction.guild, page=self.page)
        self.total_pages = total_pages
        self.setup_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_click_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed, total_pages = await self.cog.build_dashboard_embed(interaction.guild, page=self.page)
        self.total_pages = total_pages
        self.setup_buttons()
        await interaction.message.edit(embed=embed, view=self)


# ============================================================
# MAIN COG
# ============================================================

class Loa(commands.Cog):
    """Leave of Absence (LOA) management system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dashboard_pages: dict[int, int] = {}  # guild_id -> current page
        self.loa_check_loop.start()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        cmd_name = interaction.command.name if interaction.command else ""
        if cmd_name in ("loa_status",):
            return True
        allowed = await Config.check_member_cog_permission(interaction.user, "loa")
        if not allowed:
            await interaction.response.send_message(
                "❌ You do not have permission to use the **LOA** module on this server.",
                ephemeral=True
            )
            return False
        return True

    async def cog_load(self):
        await init_loa_db()
        # Register persistent dashboard view so global custom_ids are routed
        self.bot.add_view(LoaDashboardView(self, 0, 1))
        # Initial catch-up check in case bot was offline at midnight
        try:
            await self.check_and_expire_loas()
        except Exception as exc:
            logger.error(f"Failed initial LOA check on cog load: {exc}")

    def cog_unload(self):
        self.loa_check_loop.cancel()

    # --- Database Helpers ---

    async def get_settings(self, guild_id: int) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_settings WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if not row:
                return {}
            return dict(row)

    async def save_settings(
        self,
        guild_id: int,
        dashboard_channel_id: Optional[int] = None,
        dashboard_message_id: Optional[int] = None,
        review_channel_id: Optional[int] = None,
        labeler_role_id: Optional[int] = None,
        checker_role_id: Optional[int] = None,
        timezone_offset: Optional[int] = None,
        report_role_id: Optional[int] = None,
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO loa_settings (
                    guild_id, dashboard_channel_id, dashboard_message_id,
                    review_channel_id, labeler_role_id, checker_role_id, timezone_offset,
                    report_role_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    dashboard_channel_id = COALESCE(excluded.dashboard_channel_id, loa_settings.dashboard_channel_id),
                    dashboard_message_id = COALESCE(excluded.dashboard_message_id, loa_settings.dashboard_message_id),
                    review_channel_id = COALESCE(excluded.review_channel_id, loa_settings.review_channel_id),
                    labeler_role_id = COALESCE(excluded.labeler_role_id, loa_settings.labeler_role_id),
                    checker_role_id = COALESCE(excluded.checker_role_id, loa_settings.checker_role_id),
                    timezone_offset = COALESCE(excluded.timezone_offset, loa_settings.timezone_offset),
                    report_role_id = COALESCE(excluded.report_role_id, loa_settings.report_role_id)
            """, (
                guild_id,
                dashboard_channel_id,
                dashboard_message_id,
                review_channel_id,
                labeler_role_id,
                checker_role_id,
                timezone_offset,
                report_role_id
            ))
            await db.commit()

    # --- Permissions Check ---

    async def can_review(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True

        settings = await self.get_settings(interaction.guild_id)
        checker_role_id = settings.get("checker_role_id")
        if checker_role_id and any(role.id == checker_role_id for role in interaction.user.roles):
            return True

        return False

    @staticmethod
    def get_report_role_id(settings: dict) -> Optional[int]:
        """Role allowed to download LOA reports. Falls back to the Checker role if not configured."""
        return settings.get("report_role_id") or settings.get("checker_role_id")

    async def can_download_report(self, interaction: discord.Interaction) -> bool:
        """Only Server Admins/Owner and members holding the configured report role may export LOA reports."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id:
            return True

        settings = await self.get_settings(interaction.guild_id)
        report_role_id = self.get_report_role_id(settings)
        if report_role_id and any(role.id == report_role_id for role in interaction.user.roles):
            return True

        return False

    async def send_report_unauthorized(self, interaction: discord.Interaction):
        """Shows the 'not authorized' pop-up (ephemeral) to members who cannot download reports."""
        settings = await self.get_settings(interaction.guild_id) if interaction.guild_id else {}
        report_role_id = self.get_report_role_id(settings)
        role = interaction.guild.get_role(report_role_id) if (interaction.guild and report_role_id) else None
        role_text = f"members with the {role.mention} role" if role else "authorized staff"

        embed = discord.Embed(
            title="🔒 Not Authorized",
            description=(
                "You are **not authorized** to download the LOA report.\n\n"
                f"Only {role_text} and Server Administrators can export LOA records."
            ),
            color=discord.Color.red(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def validate_report_range(start_str: str, end_str: str) -> Optional[str]:
        """Returns an error message if the report date range is invalid, otherwise None."""
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
        except ValueError:
            return "❌ **Invalid Start Date!** Please use format: `YYYY-MM-DD` (e.g. `2026-10-01`)."
        try:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            return "❌ **Invalid End Date!** Please use format: `YYYY-MM-DD` (e.g. `2026-10-31`)."
        if end_dt < start_dt:
            return "❌ **Invalid Date Range!** End Date cannot be earlier than Start Date."
        return None

    # --- Review Handler ---

    async def process_review(
        self,
        interaction: discord.Interaction,
        request_id: int,
        action: str,
        rejection_reason: Optional[str] = None,
        approval_comment: Optional[str] = None
    ):
        if not await self.can_review(interaction):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ You do not have permission to review this LOA. Only Checkers or Admins can review.",
                    ephemeral=True
                )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (request_id,))
            req = await cursor.fetchone()

        if not req:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return

        if req["status"] != "pending":
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"⚠️ This request is already `{req['status']}` by {req['reviewed_by_name'] or 'a checker'}.",
                    ephemeral=True
                )
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        new_status = "approved" if action == "approve" else "rejected"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE loa_requests
                SET status = ?, reviewed_by = ?, reviewed_by_name = ?,
                    reviewed_at = ?, rejection_reason = ?, approval_comment = ?
                WHERE id = ?
            """, (
                new_status,
                interaction.user.id,
                interaction.user.display_name,
                now_iso,
                rejection_reason,
                approval_comment,
                request_id
            ))
            await db.commit()

        # Update the card in review channel
        guild = interaction.guild
        member = guild.get_member(req["user_id"])
        applicant_mention = member.mention if member else f"`{req['user_name']}`"

        color = discord.Color.green() if new_status == "approved" else discord.Color.red()
        badge = "✅ Approved" if new_status == "approved" else "❌ Rejected"

        updated_embed = discord.Embed(
            title=f"📋 LOA Application #{request_id} — {badge}",
            description=f"Reviewed by {interaction.user.mention}.",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        updated_embed.add_field(name="Applicant", value=f"{applicant_mention} (`{req['user_name']}`)", inline=True)
        updated_embed.add_field(name="Team", value=req["team_name"], inline=True)
        if "site_name" in req.keys() and req["site_name"]:
            updated_embed.add_field(name="Site Name", value=req["site_name"], inline=True)
        updated_embed.add_field(name="Duration", value=f"**{req['start_date']}** to **{req['end_date']}**", inline=False)
        updated_embed.add_field(name="Reason for Leave", value=req["reason"], inline=False)
        updated_embed.add_field(name="Decision", value=f"**{badge}** by {interaction.user.mention}", inline=True)
        if approval_comment:
            updated_embed.add_field(name="Approval Note", value=approval_comment, inline=False)
        if rejection_reason:
            updated_embed.add_field(name="Rejection Note", value=rejection_reason, inline=False)

        # Clear buttons on review message
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=updated_embed, view=None)
        else:
            await interaction.message.edit(embed=updated_embed, view=None)

        # Notify applicant via DM
        target_user = member
        if not target_user:
            try:
                target_user = await self.bot.fetch_user(req["user_id"])
            except Exception:
                pass

        if target_user:
            try:
                if new_status == "approved":
                    dm_embed = discord.Embed(
                        title="🎉 Leave of Absence (LOA) Approved!",
                        description=(
                            f"Hello **{req['user_name']}**,\n\n"
                            f"Your Leave of Absence request `#{request_id}` has been **APPROVED** by {interaction.user.mention} (`{interaction.user.display_name}`)."
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(name="📅 Leave Period", value=f"**{req['start_date']}** to **{req['end_date']}**", inline=True)
                    dm_embed.add_field(name="👥 Team", value=req["team_name"], inline=True)
                    if "site_name" in req.keys() and req["site_name"]:
                        dm_embed.add_field(name="🏷️ Site Name", value=req["site_name"], inline=True)
                    if approval_comment:
                        dm_embed.add_field(name="💬 Approval Note", value=f"```\n{approval_comment}\n```", inline=False)
                    dm_embed.set_footer(text=f"Server: {guild.name}")
                    await target_user.send(embed=dm_embed)
                else:
                    dm_embed = discord.Embed(
                        title="⚠️ Leave of Absence (LOA) Denied",
                        description=(
                            f"Hello **{req['user_name']}**,\n\n"
                            f"Your Leave of Absence request `#{request_id}` for **{req['start_date']}** to **{req['end_date']}** has been **DENIED** by {interaction.user.mention} (`{interaction.user.display_name}`)."
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(name="📅 Requested Period", value=f"`{req['start_date']}` to `{req['end_date']}`", inline=True)
                    dm_embed.add_field(name="👥 Team", value=req["team_name"], inline=True)
                    if "site_name" in req.keys() and req["site_name"]:
                        dm_embed.add_field(name="🏷️ Site Name", value=req["site_name"], inline=True)
                    if rejection_reason:
                        dm_embed.add_field(
                            name="❗ Reason / Note for Denial",
                            value=f"```\n{rejection_reason}\n```",
                            inline=False
                        )
                    dm_embed.set_footer(text=f"Server: {guild.name} • Contact your team checker if you have questions.")
                    await target_user.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(f"Could not send DM to user {req['user_id']} for LOA #{request_id} (DMs might be closed).")

        # Refresh public dashboard
        await self.refresh_dashboard(interaction.guild_id)

    async def process_extension_review(
        self,
        interaction: discord.Interaction,
        ext_id: int,
        action: str,
        rejection_reason: Optional[str] = None,
        approval_comment: Optional[str] = None
    ):
        if not await self.can_review(interaction):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ You do not have permission to review this LOA extension. Only Checkers or Admins can review.",
                    ephemeral=True
                )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (ext_id,))
            ext_req = await cursor.fetchone()

        if not ext_req:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Extension request not found.", ephemeral=True)
            return

        if ext_req["status"] != "pending_extension":
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"⚠️ This extension request is already `{ext_req['status']}` by {ext_req['reviewed_by_name'] or 'a checker'}.",
                    ephemeral=True
                )
            return

        parent_id = ext_req["extension_of_id"]
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (parent_id,))
            parent_req = await cursor.fetchone()

        now_iso = datetime.now(timezone.utc).isoformat()
        new_status = "approved" if action == "approve" else "rejected"

        async with aiosqlite.connect(DB_PATH) as db:
            # Update extension record
            await db.execute("""
                UPDATE loa_requests
                SET status = ?, reviewed_by = ?, reviewed_by_name = ?,
                    reviewed_at = ?, rejection_reason = ?, approval_comment = ?
                WHERE id = ?
            """, (
                new_status,
                interaction.user.id,
                interaction.user.display_name,
                now_iso,
                rejection_reason,
                approval_comment,
                ext_id
            ))

            # If approved, update parent LOA's end_date
            if action == "approve" and parent_req:
                await db.execute("""
                    UPDATE loa_requests
                    SET end_date = ?
                    WHERE id = ?
                """, (ext_req["end_date"], parent_id))

            await db.commit()

        # Update review card in checker channel
        guild = interaction.guild
        member = guild.get_member(ext_req["user_id"])
        applicant_mention = member.mention if member else f"`{ext_req['user_name']}`"

        color = discord.Color.green() if action == "approve" else discord.Color.red()
        badge = "✅ Approved" if action == "approve" else "❌ Rejected"

        updated_embed = discord.Embed(
            title=f"⏳ LOA Extension #{ext_id} (Ref: #{parent_id}) — {badge}",
            description=f"Extension decision by {interaction.user.mention}.",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        updated_embed.add_field(name="Applicant", value=f"{applicant_mention} (`{ext_req['user_name']}`)", inline=True)
        updated_embed.add_field(name="Team", value=ext_req["team_name"], inline=True)
        if "site_name" in ext_req.keys() and ext_req["site_name"]:
            updated_embed.add_field(name="Site Name", value=ext_req["site_name"], inline=True)
        old_end = parent_req["end_date"] if parent_req else "N/A"
        updated_embed.add_field(
            name="Extension Period",
            value=f"Original End: `{old_end}` ➔ New End: **`{ext_req['end_date']}`**",
            inline=False
        )
        updated_embed.add_field(name="Reason for Extension", value=ext_req["reason"], inline=False)
        updated_embed.add_field(name="Decision", value=f"**{badge}** by {interaction.user.mention}", inline=True)
        if approval_comment:
            updated_embed.add_field(name="Approval Note", value=approval_comment, inline=False)
        if rejection_reason:
            updated_embed.add_field(name="Denial Reason", value=rejection_reason, inline=False)

        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=updated_embed, view=None)
        else:
            await interaction.message.edit(embed=updated_embed, view=None)

        # Notify applicant via DM
        target_user = member
        if not target_user:
            try:
                target_user = await self.bot.fetch_user(ext_req["user_id"])
            except Exception:
                pass

        if target_user:
            try:
                if action == "approve":
                    dm_embed = discord.Embed(
                        title="🎉 LOA Extension Approved!",
                        description=(
                            f"Hello **{ext_req['user_name']}**,\n\n"
                            f"Your request to extend your Leave of Absence (`#{ext_id}`) has been **APPROVED** by {interaction.user.mention} (`{interaction.user.display_name}`)."
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(name="📅 Updated Leave End Date", value=f"**{ext_req['end_date']}**", inline=True)
                    dm_embed.add_field(name="👥 Team", value=ext_req["team_name"], inline=True)
                    if "site_name" in ext_req.keys() and ext_req["site_name"]:
                        dm_embed.add_field(name="🏷️ Site Name", value=ext_req["site_name"], inline=True)
                    if approval_comment:
                        dm_embed.add_field(name="💬 Approval Note", value=f"```\n{approval_comment}\n```", inline=False)
                    dm_embed.set_footer(text=f"Server: {guild.name}")
                    await target_user.send(embed=dm_embed)
                else:
                    dm_embed = discord.Embed(
                        title="⚠️ LOA Extension Denied",
                        description=(
                            f"Hello **{ext_req['user_name']}**,\n\n"
                            f"Your request to extend your Leave of Absence to `{ext_req['end_date']}` (`#{ext_id}`) has been **DENIED** by {interaction.user.mention} (`{interaction.user.display_name}`).\n"
                            f"*Note: Your current approved leave (until `{old_end}`) remains active.*"
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(name="📅 Requested End Date", value=f"`{ext_req['end_date']}`", inline=True)
                    dm_embed.add_field(name="👥 Team", value=ext_req["team_name"], inline=True)
                    if "site_name" in ext_req.keys() and ext_req["site_name"]:
                        dm_embed.add_field(name="🏷️ Site Name", value=ext_req["site_name"], inline=True)
                    if rejection_reason:
                        dm_embed.add_field(
                            name="❗ Reason for Denial",
                            value=f"```\n{rejection_reason}\n```",
                            inline=False
                        )
                    dm_embed.set_footer(text=f"Server: {guild.name} • Contact your checker for clarification.")
                    await target_user.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(f"Could not send DM to user {ext_req['user_id']} for extension #{ext_id}.")

        # Refresh public dashboard
        await self.refresh_dashboard(interaction.guild_id)

    # --- Dashboard Construction ---

    async def build_dashboard_embed(self, guild: discord.Guild, page: int = 0) -> tuple[discord.Embed, int]:
        """Constructs the LOA dashboard listing only members who are currently on approved leave."""
        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

        # 1. Fetch currently approved active/future LOAs (exclude extension sub-records so they aren't duplicated)
        # 2. Fetch pending LOAs (including pending_extension)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            # Approved leaves that are either currently active today or in the future
            cursor = await db.execute("""
                SELECT * FROM loa_requests 
                WHERE guild_id = ? AND status = 'approved' AND end_date >= ? AND extension_of_id IS NULL
                ORDER BY start_date ASC
            """, (guild.id, today_str))
            approved_rows = await cursor.fetchall()

            # Pending applications & extensions
            cursor = await db.execute("""
                SELECT * FROM loa_requests 
                WHERE guild_id = ? AND status IN ('pending', 'pending_extension')
                ORDER BY id DESC
            """, (guild.id,))
            pending_rows = await cursor.fetchall()

        # Members currently on approved leave today (one entry per member, soonest return first)
        on_leave_rows = []
        seen_uids = set()
        for r in sorted(approved_rows, key=lambda x: (x["end_date"], x["id"])):
            if r["start_date"] <= today_str <= r["end_date"] and r["user_id"] not in seen_uids:
                seen_uids.add(r["user_id"])
                on_leave_rows.append(r)

        upcoming_count = sum(1 for r in approved_rows if r["start_date"] > today_str)

        total_on_leave = len(on_leave_rows)
        total_pages = max(1, (total_on_leave + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0

        start_idx = page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, total_on_leave)
        page_rows = on_leave_rows[start_idx:end_idx]

        # Assemble embed
        embed = discord.Embed(
            title="📋 Leave of Absence (LOA) Dashboard",
            description=(
                f"**Date Today:** `{today_str}` (UTC{TZ_OFFSET_HOURS:+d})\n"
                "To submit a leave of absence, click the **📝 Apply for LOA** button below.\n"
                "To end your leave early, click **⚡ End LOA Early**.\n"
                "Authorized staff can export records with **📥 Download Report**.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(52, 152, 219),  # Crisp Blue
            timestamp=datetime.now(timezone.utc)
        )

        # KPI Badges
        embed.add_field(name="🏖️ On Leave Today", value=f"**{total_on_leave} member{'s' if total_on_leave != 1 else ''}**", inline=True)
        embed.add_field(name="📅 Upcoming Leaves", value=f"**{upcoming_count} scheduled**", inline=True)
        embed.add_field(name="⏳ Pending Review", value=f"**{len(pending_rows)} request{'s' if len(pending_rows) != 1 else ''}**", inline=True)

        # Active LOA Members list (names of members on leave today only)
        if page_rows:
            today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
            lines = []
            for rank_offset, r in enumerate(page_rows):
                actual_rank = start_idx + rank_offset + 1
                try:
                    days_left = (datetime.strptime(r["end_date"], "%Y-%m-%d").date() - today_date).days + 1
                    left_txt = f" • *{days_left} day{'s' if days_left != 1 else ''} left*"
                except ValueError:
                    left_txt = ""
                # Resolve server display name (avoids raw un-cached user ID mentions in Discord embeds)
                member = guild.get_member(r["user_id"])
                raw_name = member.display_name if member else (r["user_name"] or "Member")
                name_display = discord.utils.escape_markdown(raw_name)

                site_txt = f"{r['site_name']} • " if ("site_name" in r.keys() and r["site_name"]) else ""
                lines.append(
                    f"`#{actual_rank:02d}` **{name_display}** ({site_txt}{r['team_name']}) — until `{r['end_date']}`{left_txt}"
                )
            loa_content = "\n".join(lines)
        else:
            loa_content = "*No members are on leave today.*"

        page_str = f"Showing {start_idx + 1}–{end_idx} of {total_on_leave}" if total_on_leave else "0 on leave"
        embed.add_field(
            name=f"🏖️ Active LOA Members ({page_str})",
            value=loa_content,
            inline=False
        )

        embed.set_footer(text=f"Page {page + 1}/{total_pages} • Auto-expires daily at 12:00 AM Midnight (UTC+{TZ_OFFSET_HOURS})")
        return embed, total_pages

    async def refresh_dashboard(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        settings = await self.get_settings(guild_id)
        channel_id = settings.get("dashboard_channel_id")
        message_id = settings.get("dashboard_message_id")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        page = self.dashboard_pages.get(guild_id, 0)
        embed, total_pages = await self.build_dashboard_embed(guild, page=page)
        view = LoaDashboardView(self, page=page, total_pages=total_pages)

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                logger.warning(f"Dashboard message {message_id} in guild {guild_id} was deleted. Creating new.")
            except discord.HTTPException as exc:
                logger.error(f"Failed to edit dashboard message: {exc}")

        # Send new message and pin it
        try:
            new_msg = await channel.send(embed=embed, view=view)
            try:
                await new_msg.pin(reason="LOA Live Dashboard")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self.save_settings(guild_id, dashboard_message_id=new_msg.id)
        except discord.HTTPException as exc:
            logger.error(f"Failed to send LOA dashboard message: {exc}")

    # --- Background Loop for Expiration (Fires at 12:00 AM Midnight) ---

    async def check_and_expire_loas(self):
        """Checks for approved LOAs whose end date has passed and marks them expired."""
        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE loa_requests
                SET status = 'expired'
                WHERE status = 'approved' AND end_date < ?
            """, (today_str,))
            await db.commit()

        # Refresh all configured guild dashboards
        for guild in self.bot.guilds:
            settings = await self.get_settings(guild.id)
            if settings.get("dashboard_channel_id"):
                await self.refresh_dashboard(guild.id)

    @tasks.loop(time=MIDNIGHT_RUN_TIME)
    async def loa_check_loop(self):
        logger.info("Executing daily midnight LOA expiration check...")
        try:
            await self.check_and_expire_loas()
            logger.info("Midnight LOA expiration check completed.")
        except Exception as exc:
            logger.error(f"Error in midnight loa_check_loop: {exc}")

    @loa_check_loop.before_loop
    async def before_loa_check_loop(self):
        await self.bot.wait_until_ready()

    # --- Interaction Fallback Listener for Buttons ---

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")

        # Route review actions across bot restarts
        if custom_id.startswith("loa:approve:"):
            try:
                req_id = int(custom_id.split(":")[-1])
                if not await self.can_review(interaction):
                    await interaction.response.send_message(
                        "❌ You do not have permission to review this LOA.",
                        ephemeral=True
                    )
                    return
                modal = LoaApproveModal(self, req_id)
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"Error handling loa approve interaction: {e}")
        elif custom_id.startswith("loa:reject:"):
            try:
                req_id = int(custom_id.split(":")[-1])
                if not await self.can_review(interaction):
                    await interaction.response.send_message(
                        "❌ You do not have permission to review this LOA.",
                        ephemeral=True
                    )
                    return
                modal = LoaRejectModal(self, req_id)
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"Error handling loa reject interaction: {e}")
        elif custom_id.startswith("loa:approve_ext:"):
            try:
                ext_id = int(custom_id.split(":")[-1])
                if not await self.can_review(interaction):
                    await interaction.response.send_message(
                        "❌ You do not have permission to review this LOA extension.",
                        ephemeral=True
                    )
                    return
                modal = LoaApproveModal(self, ext_id, is_extension=True)
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"Error handling loa approve extension interaction: {e}")
        elif custom_id.startswith("loa:reject_ext:"):
            try:
                ext_id = int(custom_id.split(":")[-1])
                if not await self.can_review(interaction):
                    await interaction.response.send_message(
                        "❌ You do not have permission to review this LOA extension.",
                        ephemeral=True
                    )
                    return
                modal = LoaRejectModal(self, ext_id, is_extension=True)
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"Error handling loa reject extension interaction: {e}")
        elif custom_id == "loa:dashboard:end_early":
            try:
                view = LoaDashboardView(self, 0, 1)
                await view.on_click_end_early(interaction)
            except Exception as e:
                logger.error(f"Error handling dashboard end early interaction: {e}")

    # --- Slash Commands ---

    @app_commands.command(name="loa_setup", description="Configure channels and roles for the Leave of Absence (LOA) system.")
    @app_commands.describe(
        dashboard_channel="Public channel where the live LOA dashboard and apply button are placed",
        review_channel="Private channel where Checkers and Admins review applications",
        labeler_role="The server role assigned to regular Labelers",
        checker_role="The server role assigned to Checkers who approve/deny leaves",
        report_role="Role allowed to download LOA reports (defaults to the Checker role if not set)"
    )
    @app_commands.default_permissions(administrator=True)
    async def slash_loa_setup(
        self,
        interaction: discord.Interaction,
        dashboard_channel: discord.TextChannel,
        review_channel: discord.TextChannel,
        labeler_role: discord.Role,
        checker_role: discord.Role,
        report_role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)

        await self.save_settings(
            guild_id=interaction.guild_id,
            dashboard_channel_id=dashboard_channel.id,
            review_channel_id=review_channel.id,
            labeler_role_id=labeler_role.id,
            checker_role_id=checker_role.id,
            report_role_id=report_role.id if report_role else None
        )

        # Create live dashboard message
        embed, total_pages = await self.build_dashboard_embed(interaction.guild, page=0)
        view = LoaDashboardView(self, page=0, total_pages=total_pages)
        msg = await dashboard_channel.send(embed=embed, view=view)

        try:
            await msg.pin(reason="LOA Dashboard")
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self.save_settings(interaction.guild_id, dashboard_message_id=msg.id)

        settings = await self.get_settings(interaction.guild_id)
        rpt_role = interaction.guild.get_role(settings.get("report_role_id") or 0)
        rpt_text = rpt_role.mention if rpt_role else f"{checker_role.mention} *(defaults to Checker role)*"

        await interaction.followup.send(
            f"✅ **LOA System successfully configured!**\n\n"
            f"• **Public Dashboard:** {dashboard_channel.mention} (Message pinned)\n"
            f"• **Review Channel:** {review_channel.mention} (Only Checkers/Admins should view)\n"
            f"• **Labeler Role:** {labeler_role.mention}\n"
            f"• **Checker Role:** {checker_role.mention}\n"
            f"• **Report Download Role:** {rpt_text}",
            ephemeral=True
        )

    @app_commands.command(name="loa_config", description="View or update LOA configuration (channels, roles, timezone) without recreating the dashboard.")
    @app_commands.describe(
        review_channel="Update the private review channel for Checkers",
        dashboard_channel="Move the live dashboard to a new channel",
        checker_role="Update the Checker role",
        labeler_role="Update the Labeler role",
        report_role="Update the role allowed to download LOA reports",
        timezone_offset="Timezone offset in hours from UTC (e.g. 6 for Bangladesh Standard Time UTC+6)"
    )
    @app_commands.default_permissions(administrator=True)
    async def slash_loa_config(
        self,
        interaction: discord.Interaction,
        review_channel: Optional[discord.TextChannel] = None,
        dashboard_channel: Optional[discord.TextChannel] = None,
        checker_role: Optional[discord.Role] = None,
        labeler_role: Optional[discord.Role] = None,
        report_role: Optional[discord.Role] = None,
        timezone_offset: Optional[int] = None
    ):
        """Allows server admins to change review channels, roles, or timezone without spamming new dashboards."""
        await interaction.response.defer(ephemeral=True)

        current_settings = await self.get_settings(interaction.guild_id)
        if not current_settings and not any([review_channel, dashboard_channel, checker_role, labeler_role, report_role, timezone_offset is not None]):
            await interaction.followup.send(
                "❌ LOA system has not been set up yet. Please run `/loa_setup` first.",
                ephemeral=True
            )
            return

        updates = []
        new_rev_id = review_channel.id if review_channel else None
        new_dash_id = dashboard_channel.id if dashboard_channel else None
        new_chk_id = checker_role.id if checker_role else None
        new_lbl_id = labeler_role.id if labeler_role else None
        new_rpt_id = report_role.id if report_role else None

        if timezone_offset is not None:
            tz_str = f"UTC+{timezone_offset}" if timezone_offset >= 0 else f"UTC{timezone_offset}"
            updates.append(f"• **Timezone Offset:** {tz_str} (hours)")
            await self.save_settings(interaction.guild_id, timezone_offset=timezone_offset)

        if review_channel:
            updates.append(f"• **Checker Review Channel:** {review_channel.mention}")
        if checker_role:
            updates.append(f"• **Checker Role:** {checker_role.mention}")
        if labeler_role:
            updates.append(f"• **Labeler Role:** {labeler_role.mention}")
        if report_role:
            updates.append(f"• **Report Download Role:** {report_role.mention}")

        if dashboard_channel:
            # Recreate dashboard in new channel
            embed, total_pages = await self.build_dashboard_embed(interaction.guild, page=0)
            view = LoaDashboardView(self, page=0, total_pages=total_pages)
            msg = await dashboard_channel.send(embed=embed, view=view)
            try:
                await msg.pin(reason="LOA Dashboard Moved")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self.save_settings(
                interaction.guild_id,
                dashboard_channel_id=dashboard_channel.id,
                dashboard_message_id=msg.id,
                review_channel_id=new_rev_id,
                checker_role_id=new_chk_id,
                labeler_role_id=new_lbl_id,
                report_role_id=new_rpt_id
            )
            updates.append(f"• **Public Dashboard Channel:** {dashboard_channel.mention} (New message pinned)")
        elif any([review_channel, checker_role, labeler_role, report_role]):
            await self.save_settings(
                interaction.guild_id,
                review_channel_id=new_rev_id,
                checker_role_id=new_chk_id,
                labeler_role_id=new_lbl_id,
                report_role_id=new_rpt_id
            )
            if labeler_role:
                await self.refresh_dashboard(interaction.guild_id)

        # Fetch updated settings to display
        updated_settings = await self.get_settings(interaction.guild_id)
        dash_ch = interaction.guild.get_channel(updated_settings.get("dashboard_channel_id", 0))
        rev_ch = interaction.guild.get_channel(updated_settings.get("review_channel_id", 0))
        lbl_role = interaction.guild.get_role(updated_settings.get("labeler_role_id", 0))
        chk_role = interaction.guild.get_role(updated_settings.get("checker_role_id") or 0)
        rpt_role = interaction.guild.get_role(updated_settings.get("report_role_id") or 0)

        if updates:
            desc = "✅ **The following LOA settings were successfully updated:**\n" + "\n".join(updates)
        else:
            desc = "📋 **Current LOA Configuration:**"

        embed = discord.Embed(
            title="⚙️ LOA Configuration",
            description=desc,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Public Dashboard", value=dash_ch.mention if dash_ch else "*Not set*", inline=True)
        embed.add_field(name="Checker Review Channel", value=rev_ch.mention if rev_ch else "*Not set*", inline=True)
        embed.add_field(name="Labeler Role", value=lbl_role.mention if lbl_role else "*Not set*", inline=True)
        embed.add_field(name="Checker Role", value=chk_role.mention if chk_role else "*Not set*", inline=True)
        if rpt_role:
            rpt_display = rpt_role.mention
        elif chk_role:
            rpt_display = f"{chk_role.mention} *(Checker fallback)*"
        else:
            rpt_display = "*Admins only*"
        embed.add_field(name="Report Download Role", value=rpt_display, inline=True)
        tz_offset = updated_settings.get("timezone_offset")
        if tz_offset is None:
            tz_offset = TZ_OFFSET_HOURS
        tz_display = f"UTC+{tz_offset} (BST UTC+6)" if tz_offset >= 0 else f"UTC{tz_offset}"
        embed.add_field(name="Timezone", value=tz_display, inline=True)
        embed.set_footer(text="To change any setting, run: /loa_config [option]")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="loa_status", description="Check your current LOA applications and history.")
    async def slash_loa_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM loa_requests
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT 5
            """, (interaction.guild_id, interaction.user.id))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("ℹ️ You have not submitted any Leave of Absence requests.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Your Leave of Absence History",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )

        # Check if there is an active approved LOA to offer early return
        active_approved_id = None
        for r in rows:
            status_emoji = {
                "pending": "⏳ Waiting for Approval",
                "pending_extension": "⏳ Extension Pending",
                "approved": "✅ Approved",
                "rejected": "❌ Rejected",
                "expired": "⌛ Finished/Expired",
                "ended_early": "⚡ Ended Early"
            }.get(r["status"], r["status"])

            details = [
                f"**Team:** {r['team_name']}",
            ]
            if "site_name" in r.keys() and r["site_name"]:
                details.append(f"**Site:** {r['site_name']}")
            details.extend([
                f"**Period:** `{r['start_date']}` to `{r['end_date']}`",
                f"**Status:** {status_emoji}",
                f"**Reason:** {r['reason']}"
            ])
            if "approval_comment" in r.keys() and r["approval_comment"]:
                details.append(f"**Approval Note:** {r['approval_comment']}")
            if r["rejection_reason"]:
                details.append(f"**Rejection Note:** {r['rejection_reason']}")
            if r["reviewed_by_name"]:
                details.append(f"**Reviewed By:** {r['reviewed_by_name']}")

            embed.add_field(
                name=f"Request #{r['id']} ({r['start_date']})",
                value="\n".join(details),
                inline=False
            )

            today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
            if r["status"] == "approved" and r["start_date"] <= today_str <= r["end_date"]:
                active_approved_id = r["id"]

        view = LoaEndEarlyPromptView(self, active_approved_id) if active_approved_id else None
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="loa_end", description="End your active Leave of Absence (LOA) early and return to work.")
    async def slash_loa_end(self, interaction: discord.Interaction):
        """Allows a member to terminate their active approved LOA early and return to active status."""
        await interaction.response.defer(ephemeral=True)

        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM loa_requests
                WHERE guild_id = ? AND user_id = ? AND status = 'approved' AND end_date >= ?
                ORDER BY id DESC LIMIT 1
            """, (interaction.guild_id, interaction.user.id, today_str))
            active_loa = await cursor.fetchone()

        if not active_loa:
            # Check if applicant has a pending request
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT * FROM loa_requests
                    WHERE guild_id = ? AND user_id = ? AND status IN ('pending', 'pending_extension')
                    ORDER BY id DESC LIMIT 1
                """, (interaction.guild_id, interaction.user.id))
                pending_loa = await cursor.fetchone()

            if pending_loa:
                view = LoaCancelPendingView(self, pending_loa["id"])
                await interaction.followup.send(
                    f"ℹ️ You do not have an active approved LOA, but you have a **pending request `#{pending_loa['id']}`** (`{pending_loa['start_date']}` to `{pending_loa['end_date']}`).\n"
                    "Would you like to cancel this pending application?",
                    view=view,
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                "❌ You do not have an active or upcoming approved Leave of Absence to end.",
                ephemeral=True
            )
            return

        view = LoaEndEarlyConfirmView(self, active_loa["id"])
        site_info = f"• **Site Name:** {active_loa['site_name']}\n" if ("site_name" in active_loa.keys() and active_loa["site_name"]) else ""
        embed = discord.Embed(
            title="🔄 End Leave of Absence Early?",
            description=(
                f"You currently have an approved Leave of Absence:\n\n"
                f"• **Request ID:** `#{active_loa['id']}`\n"
                f"• **Team:** {active_loa['team_name']}\n"
                f"{site_info}"
                f"• **Scheduled Period:** `{active_loa['start_date']}` to `{active_loa['end_date']}`\n"
                f"• **Reason:** {active_loa['reason']}\n\n"
                "**If you confirm early return:**\n"
                "1. Your LOA will be marked as **Ended Early**.\n"
                "2. You will immediately be removed from the **Active LOA Members** list on the dashboard.\n"
                "3. Your team checkers will be notified."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def end_loa_early(self, interaction: discord.Interaction, request_id: int):
        now_iso = datetime.now(timezone.utc).isoformat()
        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (request_id,))
            req = await cursor.fetchone()

        if not req:
            await interaction.response.edit_message(content="❌ Request not found.", embed=None, view=None)
            return

        if req["user_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.response.edit_message(content="❌ You cannot end someone else's LOA.", embed=None, view=None)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE loa_requests
                SET status = 'ended_early', ended_early_at = ?
                WHERE id = ?
            """, (now_iso, request_id))
            await db.commit()

        # Confirmation embed to member
        embed = discord.Embed(
            title="🎉 Welcome Back!",
            description=(
                f"Your Leave of Absence `#{request_id}` has been successfully **ended early**.\n"
                f"You are now marked as **Active** and have been removed from the LOA list."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Scheduled Period", value=f"`{req['start_date']}` to `{req['end_date']}`", inline=True)
        embed.add_field(name="Returned On", value=f"`{today_str}`", inline=True)
        await interaction.response.edit_message(content=None, embed=embed, view=None)

        # Notify review channel
        settings = await self.get_settings(interaction.guild_id)
        review_channel_id = settings.get("review_channel_id")
        if review_channel_id:
            review_channel = interaction.guild.get_channel(review_channel_id)
            if review_channel:
                notice = discord.Embed(
                    title="ℹ️ Labeler Returned from LOA Early",
                    description=f"{interaction.user.mention} (`{req['user_name']}`) has ended their Leave of Absence early and returned to active duty.",
                    color=discord.Color.blue(),
                    timestamp=datetime.now(timezone.utc)
                )
                notice.add_field(name="Team", value=req["team_name"], inline=True)
                if "site_name" in req.keys() and req["site_name"]:
                    notice.add_field(name="Site Name", value=req["site_name"], inline=True)
                notice.add_field(name="Original Schedule", value=f"`{req['start_date']}` to `{req['end_date']}`", inline=True)
                notice.add_field(name="Returned At", value=f"`{today_str}`", inline=True)
                try:
                    await review_channel.send(embed=notice)
                except Exception as e:
                    logger.warning(f"Could not send early return notice to review channel: {e}")

        # Refresh public dashboard immediately
        await self.refresh_dashboard(interaction.guild_id)

    async def cancel_pending_loa(self, interaction: discord.Interaction, request_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM loa_requests WHERE id = ?", (request_id,))
            req = await cursor.fetchone()

        if not req or req["user_id"] != interaction.user.id:
            await interaction.response.edit_message(content="❌ Could not cancel application.", view=None)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM loa_requests WHERE id = ? AND status IN ('pending', 'pending_extension')", (request_id,))
            await db.commit()

        if req["review_channel_msg_id"]:
            settings = await self.get_settings(interaction.guild_id)
            review_ch_id = settings.get("review_channel_id")
            if review_ch_id:
                ch = interaction.guild.get_channel(review_ch_id)
                if ch:
                    try:
                        rev_msg = await ch.fetch_message(req["review_channel_msg_id"])
                        cancel_embed = discord.Embed(
                            title=f"📋 LOA Application #{request_id} — 🚫 Cancelled by Applicant",
                            description=f"Applicant {interaction.user.mention} cancelled this request.",
                            color=discord.Color.dark_grey(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        await rev_msg.edit(embed=cancel_embed, view=None)
                    except Exception:
                        pass

        await interaction.response.edit_message(content=f"✅ Your pending LOA application `#{request_id}` has been cancelled.", view=None)
        await self.refresh_dashboard(interaction.guild_id)

    @app_commands.command(name="loa_report", description="Generate a date-wise LOA report with CSV export (authorized report role only).")
    @app_commands.describe(
        start_date="Start date of report window (YYYY-MM-DD)",
        end_date="End date of report window (YYYY-MM-DD)",
        status_filter="Filter by leave status (default: Approved leaves only)",
        ephemeral="Set to True if you want the report visible only to you (default: False)"
    )
    @app_commands.choices(status_filter=[
        app_commands.Choice(name="Approved Only (Active & Completed leaves)", value="approved"),
        app_commands.Choice(name="All Records (Approved, Pending, Denied)", value="all"),
    ])
    async def slash_loa_report(
        self,
        interaction: discord.Interaction,
        start_date: str,
        end_date: str,
        status_filter: Optional[app_commands.Choice[str]] = None,
        ephemeral: bool = False
    ):
        # 1. Check permissions (Admin or configured report role only)
        if not await self.can_download_report(interaction):
            await self.send_report_unauthorized(interaction)
            return

        # 2. Validate dates
        start_str = start_date.strip()
        end_str = end_date.strip()
        error = self.validate_report_range(start_str, end_str)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=ephemeral)

        filter_val = status_filter.value if status_filter else "approved"
        embed, csv_file = await self.generate_loa_report(
            interaction.guild_id, start_str, end_str, filter_val, interaction.user.display_name
        )
        await interaction.followup.send(embed=embed, file=csv_file, ephemeral=ephemeral)

    async def generate_loa_report(
        self,
        guild_id: int,
        start_str: str,
        end_str: str,
        filter_val: str,
        generated_by: str
    ) -> tuple[discord.Embed, discord.File]:
        """Builds the LOA report embed and CSV attachment for leaves overlapping the given date range."""

        # 3. Query Database for leaves active during window
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if filter_val == "approved":
                cursor = await db.execute("""
                    SELECT * FROM loa_requests
                    WHERE guild_id = ?
                      AND status IN ('approved', 'expired', 'ended_early')
                      AND extension_of_id IS NULL
                      AND start_date <= ? AND end_date >= ?
                    ORDER BY start_date ASC, id ASC
                """, (guild_id, end_str, start_str))
            else:
                cursor = await db.execute("""
                    SELECT * FROM loa_requests
                    WHERE guild_id = ?
                      AND start_date <= ? AND end_date >= ?
                    ORDER BY start_date ASC, id ASC
                """, (guild_id, end_str, start_str))
            rows = await cursor.fetchall()

        # 4. Generate CSV export in-memory
        csv_buffer = io.StringIO()
        csv_writer = csv.writer(csv_buffer)
        csv_writer.writerow([
            "Request ID",
            "User ID",
            "User Name",
            "Team Name",
            "Site Name",
            "Start Date",
            "End Date",
            "Total Days",
            "Status",
            "Reason",
            "Reviewed By",
            "Reviewed At",
            "Approval Note",
            "Denial Reason",
            "Applied At"
        ])

        team_counts = {}
        for r in rows:
            try:
                r_s = datetime.strptime(r["start_date"], "%Y-%m-%d").date()
                r_e = datetime.strptime(r["end_date"], "%Y-%m-%d").date()
                days_num = (r_e - r_s).days + 1
            except Exception:
                days_num = ""

            t_name = r["team_name"] or "Unknown"
            team_counts[t_name] = team_counts.get(t_name, 0) + 1

            site_val = (r["site_name"] if "site_name" in r.keys() else "") or ""
            appr_note = (r["approval_comment"] if "approval_comment" in r.keys() else "") or ""
            csv_writer.writerow([
                r["id"],
                r["user_id"],
                r["user_name"],
                r["team_name"],
                site_val,
                r["start_date"],
                r["end_date"],
                days_num,
                r["status"].capitalize(),
                r["reason"],
                r["reviewed_by_name"] or "",
                r["reviewed_at"] or "",
                appr_note,
                r["rejection_reason"] or "",
                r["applied_at"]
            ])

        csv_buffer.seek(0)
        csv_bytes = io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig"))
        filename = f"loa_report_{start_str}_to_{end_str}.csv"
        csv_file = discord.File(fp=csv_bytes, filename=filename)

        # 5. Build Report Embed
        filter_text = "Approved Leaves" if filter_val == "approved" else "All Statuses"
        embed = discord.Embed(
            title="📊 Leave of Absence (LOA) Date-Wise Report",
            description=(
                f"📅 **Date Range:** `{start_str}` to `{end_str}`\n"
                f"🔍 **Filter:** {filter_text}\n"
                f"📁 **Total Leaves in Period:** `{len(rows)}` record{'s' if len(rows) != 1 else ''}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(46, 204, 113) if rows else discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        unique_members = len(set(r["user_id"] for r in rows))
        embed.add_field(name="👥 Total Members", value=f"**{unique_members}** unique labeler{'s' if unique_members != 1 else ''}", inline=True)

        if team_counts:
            team_summary = ", ".join(f"**{t}**: {c}" for t, c in sorted(team_counts.items()))
            if len(team_summary) > 250:
                team_summary = team_summary[:245] + "…"
            embed.add_field(name="👥 Team Breakdown", value=team_summary, inline=True)

        if not rows:
            embed.add_field(
                name="📋 Results",
                value=f"No LOA records found overlapping with `{start_str}` to `{end_str}`.",
                inline=False
            )
        else:
            preview_lines = []
            for r in rows[:12]:
                status_badge = {
                    "approved": "✅ Approved",
                    "expired": "⌛ Completed",
                    "ended_early": "⚡ Ended Early",
                    "pending": "⏳ Pending",
                    "rejected": "❌ Denied"
                }.get(r["status"], r["status"])

                site_str = f" • `{r['site_name']}`" if ("site_name" in r.keys() and r["site_name"]) else ""
                preview_lines.append(
                    f"`#{r['id']:02d}` **{r['user_name']}** ({r['team_name']}{site_str}) • `{r['start_date']}` to `{r['end_date']}` • {status_badge}"
                )

            if len(rows) > 12:
                preview_lines.append(f"*…and {len(rows) - 12} more records (see attached CSV)*")

            embed.add_field(
                name=f"📋 Leaves List ({min(12, len(rows))} of {len(rows)})",
                value="\n".join(preview_lines),
                inline=False
            )

        embed.set_footer(text=f"Generated by {generated_by} • Full export attached as CSV")

        return embed, csv_file


async def setup(bot: commands.Bot):
    await bot.add_cog(Loa(bot))
