"""
Production-Ready Forms Builder & Management Cog for Discord.py 2.x.
Features:
- Dynamic, fully controllable form questions (short & paragraph styles, limits, order)
- Discord 5-field modal limit handling: 1-5 direct modal, 6+ chained multi-page modals with in-memory draft cache
- Optional review system (Approve, Reject, DM submitter) with role hierarchy guards
- DynamicItem persistent buttons working permanently across bot and VPS restarts
- Timing, manual start/end scheduling, live countdown timestamps (<t:TIMESTAMP:R>)
- 30-second background lifecycle supervisor with auto CSV export upon expiration
- Complete slash commands suite under /form
"""

import io
import re
import time
import json
import logging
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.forms_db import FormsDB
from services.forms_exporter import generate_csv_bytes, generate_xlsx_bytes, OPENPYXL_AVAILABLE

logger = logging.getLogger("FormsCog")


# =============================================================================
# Helper: Parse Relative Durations (e.g., '30m', '5h', '2d')
# =============================================================================

def parse_relative_duration(duration_str: str) -> Optional[int]:
    """Parses strings like '30m', '2h', '1d', '5 days' into total seconds."""
    if not duration_str:
        return None
    s = duration_str.strip().lower()
    match = re.match(r"^(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?|w|weeks?)$", s)
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2)
    if unit.startswith("s"):
        return val
    elif unit.startswith("m"):
        return val * 60
    elif unit.startswith("h"):
        return val * 3600
    elif unit.startswith("d"):
        return val * 86400
    elif unit.startswith("w"):
        return val * 604800
    return None


# =============================================================================
# Dynamic Persistent Items across VPS Restarts
# =============================================================================

class DynamicFormOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"form:open:(?P<form_id>\d+)"):
    """
    Persistent Dynamic Button for opening a form.
    Pattern: form:open:<form_id>
    """
    def __init__(
        self,
        form_id: int,
        label: str = "📝 Fill Form",
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        disabled: bool = False
    ):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                custom_id=f"form:open:{form_id}"
            )
        )
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        form_id = int(match.group("form_id"))
        return cls(form_id, label=item.label or "📝 Fill Form", style=item.style, disabled=item.disabled)

    @property
    def disabled(self) -> bool:
        return getattr(self.item, "disabled", False)

    @disabled.setter
    def disabled(self, value: bool) -> None:
        if hasattr(self.item, "disabled"):
            self.item.disabled = value

    @property
    def label(self) -> Optional[str]:
        return getattr(self.item, "label", None)

    @property
    def style(self) -> discord.ButtonStyle:
        return getattr(self.item, "style", discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        cog: Optional[FormsCog] = interaction.client.get_cog("FormsCog")  # type: ignore
        if not cog:
            await interaction.response.send_message("❌ Forms service is currently unavailable.", ephemeral=True)
            return
        await cog.handle_form_open_interaction(interaction, self.form_id)


class DynamicFormReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"form:review:(?P<action>approve|reject|dm):(?P<sub_id>\d+)"):
    """
    Persistent Dynamic Button for reviewing a submission.
    Pattern: form:review:<action>:<sub_id>
    """
    def __init__(
        self,
        action: str,
        sub_id: int,
        label: str = "Review",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
        disabled: bool = False
    ):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                emoji=emoji,
                disabled=disabled,
                custom_id=f"form:review:{action}:{sub_id}"
            )
        )
        self.action = action
        self.sub_id = sub_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        action = match.group("action")
        sub_id = int(match.group("sub_id"))
        return cls(action, sub_id, label=item.label or action.capitalize(), style=item.style, emoji=str(item.emoji) if item.emoji else None, disabled=item.disabled)

    @property
    def disabled(self) -> bool:
        return getattr(self.item, "disabled", False)

    @disabled.setter
    def disabled(self, value: bool) -> None:
        if hasattr(self.item, "disabled"):
            self.item.disabled = value

    @property
    def label(self) -> Optional[str]:
        return getattr(self.item, "label", None)

    @property
    def style(self) -> discord.ButtonStyle:
        return getattr(self.item, "style", discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        cog: Optional[FormsCog] = interaction.client.get_cog("FormsCog")  # type: ignore
        if not cog:
            await interaction.response.send_message("❌ Forms service is currently unavailable.", ephemeral=True)
            return
        await cog.handle_form_review_interaction(interaction, self.action, self.sub_id)


# =============================================================================
# Form Modals & Multi-Page View Components
# =============================================================================

class FormPageModal(discord.ui.Modal):
    """
    Dynamically generates modal fields (1 to 5) for a specific slice/page of questions.
    """
    def __init__(
        self,
        form: Dict[str, Any],
        questions_chunk: List[Dict[str, Any]],
        page: int,
        total_pages: int,
        cog: "FormsCog",
        draft_key: str
    ):
        title = f"{form['title'][:35]} (Page {page}/{total_pages})" if total_pages > 1 else form['title'][:45]
        super().__init__(title=title)
        self.form = form
        self.questions_chunk = questions_chunk
        self.page = page
        self.total_pages = total_pages
        self.cog = cog
        self.draft_key = draft_key

        self.inputs: Dict[int, discord.ui.TextInput] = {}

        for q in self.questions_chunk:
            is_paragraph = q.get("style", "short").lower() == "paragraph"
            text_style = discord.TextStyle.paragraph if is_paragraph else discord.TextStyle.short
            min_l = max(0, min(q.get("min_length", 0), 1000))
            max_l = max(1, min(q.get("max_length", 1000), 1000))
            if min_l > max_l:
                min_l = max_l

            inp = discord.ui.TextInput(
                label=q["label"][:45],
                style=text_style,
                placeholder=q.get("placeholder")[:100] if q.get("placeholder") else None,
                required=bool(q.get("required", True)),
                min_length=min_l if min_l > 0 else None,
                max_length=max_l
            )
            self.inputs[q["id"]] = inp
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction):
        # Extract answers from inputs
        chunk_answers = {}
        for qid, inp in self.inputs.items():
            chunk_answers[str(qid)] = inp.value.strip()

        # Update draft
        existing_draft = self.cog.drafts.get(self.draft_key, {
            "answers": {},
            "timestamp": time.time(),
            "form_id": self.form["id"],
            "user_id": interaction.user.id
        })
        existing_draft["answers"].update(chunk_answers)
        existing_draft["timestamp"] = time.time()
        self.cog.drafts[self.draft_key] = existing_draft

        # Check if there are more pages
        if self.page < self.total_pages:
            view = FormMultiPageContinueView(
                form=self.form,
                next_page=self.page + 1,
                total_pages=self.total_pages,
                draft_key=self.draft_key,
                cog=self.cog
            )
            await interaction.response.send_message(
                f"✅ **Page {self.page} of {self.total_pages} saved!**\n"
                f"Click **Continue to Next Page ➡️** to proceed, or **Start Over** to reset.",
                view=view,
                ephemeral=True
            )
        else:
            # Final page submitted!
            final_answers = existing_draft["answers"]
            self.cog.drafts.pop(self.draft_key, None)
            await self.cog.finalize_submission(interaction, self.form, final_answers)


class FormMultiPageContinueView(discord.ui.View):
    """Ephemeral view shown between modal pages with Continue and Start Over buttons."""
    def __init__(
        self,
        form: Dict[str, Any],
        next_page: int,
        total_pages: int,
        draft_key: str,
        cog: "FormsCog"
    ):
        super().__init__(timeout=600)  # 10 minute timeout
        self.form = form
        self.next_page = next_page
        self.total_pages = total_pages
        self.draft_key = draft_key
        self.cog = cog

    @discord.ui.button(label="Continue to Next Page ➡️", style=discord.ButtonStyle.primary)
    async def continue_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Retrieve all questions for this form
        questions = await self.cog.db.get_questions(self.form["id"])
        chunk_size = 5
        start_idx = (self.next_page - 1) * chunk_size
        chunk = questions[start_idx:start_idx + chunk_size]

        if not chunk:
            await interaction.response.send_message("❌ Form questions could not be retrieved.", ephemeral=True)
            return

        modal = FormPageModal(
            form=self.form,
            questions_chunk=chunk,
            page=self.next_page,
            total_pages=self.total_pages,
            cog=self.cog,
            draft_key=self.draft_key
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔄 Start Over", style=discord.ButtonStyle.danger)
    async def start_over(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.drafts.pop(self.draft_key, None)
        await interaction.response.edit_message(
            content="🗑️ Your draft has been cleared. You can click **Fill Form** on the public panel to start again.",
            view=None
        )


# =============================================================================
# Review Modals (Approve, Reject, DM User)
# =============================================================================

class ApproveModal(discord.ui.Modal, title="Approve Submission"):
    note = discord.ui.TextInput(
        label="Reviewer Note (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Enter an optional note or comment for the submitter...",
        required=False,
        max_length=500
    )

    def __init__(self, sub_id: int, form: Dict[str, Any], cog: "FormsCog"):
        super().__init__()
        self.sub_id = sub_id
        self.form = form
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_approval(interaction, self.sub_id, self.form, self.note.value.strip())


class RejectModal(discord.ui.Modal, title="Reject Submission"):
    reason = discord.ui.TextInput(
        label="Rejection Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this submission is being rejected...",
        required=True,
        max_length=500
    )

    def __init__(self, sub_id: int, form: Dict[str, Any], cog: "FormsCog"):
        super().__init__()
        self.sub_id = sub_id
        self.form = form
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_rejection(interaction, self.sub_id, self.form, self.reason.value.strip())


class StaffDMModal(discord.ui.Modal, title="Send Official Staff DM"):
    message_content = discord.ui.TextInput(
        label="Direct Message Content",
        style=discord.TextStyle.paragraph,
        placeholder="Type the message you want the bot to DM to the submitter...",
        required=True,
        max_length=1000
    )

    def __init__(self, sub_id: int, form: Dict[str, Any], cog: "FormsCog"):
        super().__init__()
        self.sub_id = sub_id
        self.form = form
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_staff_dm(interaction, self.sub_id, self.form, self.message_content.value.strip())


# =============================================================================
# Static/Dynamic Action View Helpers
# =============================================================================

def build_review_view(sub_id: int, disabled: bool = False) -> discord.ui.View:
    """Builds the 3 persistent review action buttons for a destination submission message."""
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicFormReviewButton("approve", sub_id, label="Approve", style=discord.ButtonStyle.success, emoji="✅", disabled=disabled))
    view.add_item(DynamicFormReviewButton("reject", sub_id, label="Reject", style=discord.ButtonStyle.danger, emoji="❌", disabled=disabled))
    view.add_item(DynamicFormReviewButton("dm", sub_id, label="DM User", style=discord.ButtonStyle.secondary, emoji="💬", disabled=False))
    return view


def build_panel_embed_and_view(form: Dict[str, Any]) -> Tuple[discord.Embed, discord.ui.View]:
    """
    Builds the public Discord embed and action button representing the form's lifecycle state.
    States:
      1. Scheduled (Before opens_at): Yellow embed, opening countdown, disabled button ('🔒 Opens Soon').
      2. Open (Active window): Green embed, live closing countdown, enabled button ('📝 Fill Form').
      3. Closed (After closes_at or is_closed): Red embed, closed notice, disabled button ('🔒 Form Closed').
    """
    now = int(time.time())
    opens_at = form.get("opens_at")
    closes_at = form.get("closes_at")
    is_closed = bool(form.get("is_closed", False)) or not bool(form.get("is_enabled", True))

    title = form.get("title", "Server Form")
    description = form.get("description") or "Please fill out the form by clicking the button below."

    # Determine state
    if is_closed:
        state = "closed"
    elif opens_at and now < opens_at:
        state = "scheduled"
    elif closes_at and now >= closes_at:
        state = "closed"
    else:
        state = "open"

    view = discord.ui.View(timeout=None)

    if state == "scheduled":
        embed = discord.Embed(
            title=f"⏳ [Scheduled] {title}",
            description=f"{description}\n\n**Opens:** <t:{opens_at}:R> (<t:{opens_at}:F>)",
            color=0xF59E0B  # Amber/Yellow
        )
        embed.set_footer(text=f"Form ID: #{form['id']} • Opens Soon")
        view.add_item(DynamicFormOpenButton(form["id"], label="🔒 Opens Soon", style=discord.ButtonStyle.secondary, disabled=True))

    elif state == "open":
        timing_info = ""
        if closes_at:
            timing_info = f"\n\n**Closes:** <t:{closes_at}:R> (<t:{closes_at}:F>)"

        embed = discord.Embed(
            title=f"📝 {title}",
            description=f"{description}{timing_info}",
            color=0x10B981  # Emerald/Green
        )
        embed.set_footer(text=f"Form ID: #{form['id']} • Active")
        view.add_item(DynamicFormOpenButton(form["id"], label="📝 Fill Form", style=discord.ButtonStyle.primary, disabled=False))

    else:  # closed
        embed = discord.Embed(
            title=f"🔒 [Closed] {title}",
            description=f"{description}\n\n*This form is currently closed to new submissions.*",
            color=0xEF4444  # Red
        )
        embed.set_footer(text=f"Form ID: #{form['id']} • Closed")
        view.add_item(DynamicFormOpenButton(form["id"], label="🔒 Form Closed", style=discord.ButtonStyle.secondary, disabled=True))

    return embed, view


# =============================================================================
# FormsCog Implementation
# =============================================================================

class FormsCog(commands.Cog):
    """Complete Form Builder & Management Cog with DynamicItems and Web Dashboard Sync."""

    form_group = app_commands.Group(
        name="form",
        description="Form builder and submission management commands"
    )
    question_group = app_commands.Group(
        name="question",
        description="Manage custom form fields and questions",
        parent=form_group
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = FormsDB.get_instance()
        # In-memory drafts for multi-page forms: {draft_key: {"answers": dict, "timestamp": float, "form_id": int, "user_id": int}}
        self.drafts: Dict[str, Dict[str, Any]] = {}
        # User cooldown tracking: {(form_id, user_id): float_timestamp}
        self.cooldowns: Dict[Tuple[int, int], float] = {}

    async def cog_load(self):
        """Initializes database, registers persistent dynamic items, and starts lifecycle task."""
        await self.db.init_db()
        self.bot.add_dynamic_items(DynamicFormOpenButton, DynamicFormReviewButton)
        if not self.forms_lifecycle_task.is_running():
            self.forms_lifecycle_task.start()
        logger.info("[OK] FormsCog loaded, dynamic items registered, and 30s lifecycle task started.")

    async def cog_unload(self):
        if self.forms_lifecycle_task.is_running():
            self.forms_lifecycle_task.cancel()
        logger.info("[OK] FormsCog unloaded.")

    # =========================================================================
    # Lifecycle Supervisor: @tasks.loop(seconds=30)
    # =========================================================================

    @tasks.loop(seconds=30.0)
    async def forms_lifecycle_task(self):
        """
        Background supervisor that:
        1. Purges stale multi-page in-memory drafts older than 15 minutes.
        2. Updates scheduled forms to 'Open' when opens_at arrives.
        3. Updates active forms to 'Closed' when closes_at arrives and automatically delivers the final CSV report.
        """
        now = time.time()
        now_int = int(now)

        # 1. Purge stale in-memory drafts (> 15 minutes)
        stale_keys = [k for k, v in self.drafts.items() if now - v.get("timestamp", 0) > 900]
        for k in stale_keys:
            self.drafts.pop(k, None)

        # 2. Check scheduled forms that are due to open
        try:
            due_opens = await self.db.get_due_open_forms(now_int)
            for form in due_opens:
                await self.refresh_panel_message(form)
        except Exception as e:
            logger.error("Error refreshing scheduled forms: %s", e)

        # 3. Check active forms that are due to close
        try:
            due_closes = await self.db.get_due_close_forms(now_int)
            for form in due_closes:
                # Mark form closed in DB
                await self.db.set_form_closed(form["guild_id"], form["id"], is_closed=True)
                form["is_closed"] = 1
                await self.refresh_panel_message(form)

                # Send automatic final report to dest_channel_id if not yet sent
                if not form.get("closed_report_sent"):
                    await self.send_closed_report(form)
        except Exception as e:
            logger.error("Error managing expired forms: %s", e)

    @forms_lifecycle_task.before_loop
    async def before_lifecycle_task(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            pass

    async def refresh_panel_message(self, form: Dict[str, Any]) -> bool:
        """Edits the published Discord panel message with updated lifecycle embed & button."""
        channel_id = form.get("panel_channel_id")
        message_id = form.get("panel_message_id")
        if not channel_id or not message_id:
            return False

        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return False

        if not hasattr(channel, "fetch_message"):
            return False

        try:
            msg = await channel.fetch_message(message_id)
            embed, view = build_panel_embed_and_view(form)
            await msg.edit(embed=embed, view=view)
            return True
        except discord.NotFound:
            # Message was deleted
            return False
        except Exception as e:
            logger.warning("Could not refresh panel message #%s: %s", message_id, e)
            return False

    async def send_closed_report(self, form: Dict[str, Any]):
        """Generates and delivers the final CSV report to dest_channel_id when a timed form expires."""
        dest_ch_id = form.get("dest_channel_id")
        if not dest_ch_id:
            return

        channel = self.bot.get_channel(dest_ch_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(dest_ch_id)
            except Exception:
                return

        try:
            questions = await self.db.get_questions(form["id"])
            submissions = await self.db.list_submissions(form["guild_id"], form["id"], limit=5000)
            csv_bytes = generate_csv_bytes(form, questions, submissions)
            file_name = f"form_{form['id']}_final_report.csv"
            discord_file = discord.File(fp=csv_bytes, filename=file_name)

            await channel.send(
                content=f"📊 **Form Closed:** The submission window for **{form['title']}** has ended. Attached is the final submissions report ({len(submissions)} entries).",
                file=discord_file,
                allowed_mentions=discord.AllowedMentions.none()
            )
            await self.db.mark_closed_report_sent(form["id"])
            logger.info("Delivered final CSV report for Form #%d to channel #%d", form["id"], dest_ch_id)
        except Exception as e:
            logger.error("Failed to send closed form report for Form #%d: %s", form["id"], e)

    # =========================================================================
    # User Interaction Handlers (Modal Launch & Multi-Page)
    # =========================================================================

    async def handle_form_open_interaction(self, interaction: discord.Interaction, form_id: int):
        """Triggered when a user clicks 'Fill Form' on any published Discord panel."""
        guild = interaction.guild
        user = interaction.user
        if not guild:
            await interaction.response.send_message("❌ Forms can only be completed within a server.", ephemeral=True)
            return

        form = await self.db.get_form_any(form_id)
        if not form:
            await interaction.response.send_message("❌ This form no longer exists.", ephemeral=True)
            return

        # Guild isolation guard
        if form["guild_id"] != guild.id:
            await interaction.response.send_message("❌ Access denied: Form does not belong to this server.", ephemeral=True)
            return

        now = int(time.time())

        # 1. State & Timing Guards
        if bool(form.get("is_closed", False)) or not bool(form.get("is_enabled", True)):
            await interaction.response.send_message("🔒 This form is currently closed to new submissions.", ephemeral=True)
            return

        opens_at = form.get("opens_at")
        if opens_at and now < opens_at:
            await interaction.response.send_message(f"⏳ This form is not open yet. It opens <t:{opens_at}:R> (<t:{opens_at}:F>).", ephemeral=True)
            return

        closes_at = form.get("closes_at")
        if closes_at and now >= closes_at:
            await interaction.response.send_message(f"🔒 This form closed <t:{closes_at}:R>.", ephemeral=True)
            return

        # 2. Cooldown Guard
        cooldown = form.get("cooldown_seconds", 60)
        cd_key = (form_id, user.id)
        last_click = self.cooldowns.get(cd_key, 0)
        if cooldown > 0 and (now - last_click) < cooldown:
            remaining = int(cooldown - (now - last_click))
            await interaction.response.send_message(f"⏳ Please wait **{remaining}s** before opening this form again.", ephemeral=True)
            return

        # 3. Max Submissions Per User Guard
        max_subs = form.get("max_submissions_per_user", 0)
        if max_subs > 0:
            count = await self.db.count_user_submissions(form_id, user.id)
            if count >= max_subs:
                await interaction.response.send_message(f"❌ You have reached the maximum allowed submissions for this form ({max_subs}).", ephemeral=True)
                return

        # Fetch questions
        questions = await self.db.get_questions(form_id)
        if not questions:
            await interaction.response.send_message("⚠️ This form has no questions configured yet. Please notify server staff.", ephemeral=True)
            return

        # Update cooldown timestamp
        self.cooldowns[cd_key] = now

        # 4. Modal chunking (1-5 questions vs 6+ chained multi-page)
        chunk_size = 5
        total_questions = len(questions)
        total_pages = (total_questions + chunk_size - 1) // chunk_size

        draft_key = f"{form_id}:{user.id}"
        # Start new or resumed draft
        first_chunk = questions[0:chunk_size]
        modal = FormPageModal(
            form=form,
            questions_chunk=first_chunk,
            page=1,
            total_pages=total_pages,
            cog=self,
            draft_key=draft_key
        )
        await interaction.response.send_modal(modal)

    async def finalize_submission(self, interaction: discord.Interaction, form: Dict[str, Any], answers: Dict[str, Any]):
        """Processes final submission, inserts DB record, and posts destination embed."""
        guild = interaction.guild
        user = interaction.user
        if not guild:
            return

        review_enabled = bool(form.get("review_enabled", False))
        status = "pending" if review_enabled else "submitted"

        # Insert submission record into database
        sub_id = await self.db.create_submission(
            form_id=form["id"],
            guild_id=guild.id,
            user_id=user.id,
            user_name=user.name,
            user_discriminator=getattr(user, "discriminator", "0"),
            user_avatar_url=user.display_avatar.url,
            answers=answers,
            status=status
        )

        # Build destination channel embed
        dest_ch_id = form.get("dest_channel_id")
        dest_channel = guild.get_channel(dest_ch_id)
        if not dest_channel:
            try:
                dest_channel = await guild.fetch_channel(dest_ch_id)
            except Exception:
                dest_channel = None

        if dest_channel and hasattr(dest_channel, "send"):
            questions = await self.db.get_questions(form["id"])

            embed_color = 0xF59E0B if review_enabled else 0x10B981  # Amber if pending review, Green if direct
            status_text = "⏳ Pending Review" if review_enabled else "✅ Submitted"

            dest_embed = discord.Embed(
                title=f"📋 New Submission: {form['title']} (ID: #{sub_id})",
                color=embed_color,
                timestamp=datetime.now(timezone.utc)
            )
            dest_embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)
            dest_embed.add_field(name="Status", value=f"**{status_text}**", inline=True)
            dest_embed.add_field(name="Submitter", value=f"{user.mention}", inline=True)

            # Add question & answer fields
            for idx, q in enumerate(questions, start=1):
                ans = answers.get(str(q["id"])) or answers.get(q["id"]) or "*[No answer provided]*"
                # Discord embed field value limit is 1024 chars
                safe_ans = str(ans)[:1000] if ans else "*[Empty]*"
                dest_embed.add_field(
                    name=f"Q{idx}: {q['label'][:45]}",
                    value=safe_ans,
                    inline=False
                )

            dest_embed.set_footer(text=f"Form #{form['id']} • Submission #{sub_id}")

            # Attach review view if review system enabled, otherwise view=None
            review_view = build_review_view(sub_id) if review_enabled else None

            try:
                dest_msg = await dest_channel.send(
                    embed=dest_embed,
                    view=review_view,
                    allowed_mentions=discord.AllowedMentions.none()
                )
                await self.db.update_submission_dest_msg(sub_id, dest_msg.id)
            except Exception as e:
                logger.error("Failed to post submission to dest_channel #%s: %s", dest_ch_id, e)

        # Reply to user
        if review_enabled:
            reply_msg = f"🎉 **Thank you!** Your submission for **{form['title']}** has been received and is pending staff review."
        else:
            reply_msg = f"🎉 **Thank you!** Your submission for **{form['title']}** has been recorded successfully."

        if not interaction.response.is_done():
            await interaction.response.send_message(reply_msg, ephemeral=True)
        else:
            await interaction.followup.send(reply_msg, ephemeral=True)

    # =========================================================================
    # Review System Callbacks & Security Checks
    # =========================================================================

    async def _check_staff_reviewer(self, interaction: discord.Interaction) -> bool:
        """Verifies that the user has staff/moderation authority to review submissions."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_messages:
            return True
        return False

    async def handle_form_review_interaction(self, interaction: discord.Interaction, action: str, sub_id: int):
        """Handles clicks on persistent review buttons (Approve, Reject, DM)."""
        if not await self._check_staff_reviewer(interaction):
            await interaction.response.send_message("❌ You do not have permission to review form submissions.", ephemeral=True)
            return

        sub = await self.db.get_submission(sub_id)
        if not sub:
            await interaction.response.send_message("❌ Submission record could not be found.", ephemeral=True)
            return

        form = await self.db.get_form(sub["guild_id"], sub["form_id"])
        if not form:
            await interaction.response.send_message("❌ Associated form could not be found.", ephemeral=True)
            return

        # Check existing review status for approve/reject
        if action in ("approve", "reject") and sub.get("status") in ("approved", "rejected"):
            await interaction.response.send_message(
                f"ℹ️ This submission has already been reviewed as **{sub['status'].upper()}** by {sub.get('reviewer_name') or 'staff'}.",
                ephemeral=True
            )
            return

        if action == "approve":
            await interaction.response.send_modal(ApproveModal(sub_id, form, self))
        elif action == "reject":
            await interaction.response.send_modal(RejectModal(sub_id, form, self))
        elif action == "dm":
            await interaction.response.send_modal(StaffDMModal(sub_id, form, self))

    async def process_approval(
        self,
        interaction: discord.Interaction,
        sub_id: int,
        form: Dict[str, Any],
        note: str,
        reviewer_user: Optional[discord.User | discord.Member] = None
    ) -> bool:
        """
        Processes submission approval: updates DB, updates destination message embed,
        disables approve/reject buttons, assigns approved_role_id with role hierarchy checks, and DMs submitter.
        """
        reviewer = reviewer_user or interaction.user
        guild = interaction.guild if interaction else self.bot.get_guild(form["guild_id"])
        if not guild:
            if interaction:
                await interaction.response.send_message("❌ Server context unavailable.", ephemeral=True)
            return False

        # 1. Update DB
        await self.db.update_submission_status(
            sub_id=sub_id,
            status="approved",
            reviewer_id=reviewer.id,
            reviewer_name=reviewer.name,
            review_note=note or None
        )

        sub = await self.db.get_submission(sub_id)
        if not sub:
            return False

        # 2. Role Hierarchy & Privilege Escalation Guard
        role_assigned = False
        approved_role_id = form.get("approved_role_id")
        if approved_role_id:
            role = guild.get_role(approved_role_id)
            if role:
                bot_top = guild.me.top_role if guild.me else None
                # Verify bot role hierarchy and danger flags
                if bot_top and role.position < bot_top.position and not role.permissions.administrator and not role.permissions.manage_guild:
                    member = guild.get_member(sub["user_id"])
                    if member:
                        try:
                            await member.add_roles(role, reason=f"Form #{form['id']} submission #{sub_id} approved by {reviewer.name}")
                            role_assigned = True
                        except Exception as e:
                            logger.error("Could not assign approved role to %s: %s", member.id, e)

        # 3. Update Destination Message Embed
        dest_msg_id = sub.get("dest_message_id")
        dest_ch_id = form.get("dest_channel_id")
        if dest_ch_id and dest_msg_id:
            ch = guild.get_channel(dest_ch_id)
            if ch and hasattr(ch, "fetch_message"):
                try:
                    msg = await ch.fetch_message(dest_msg_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.color = 0x10B981  # Green
                        # Update status field
                        for i, f in enumerate(embed.fields):
                            if f.name.lower() == "status":
                                embed.set_field_at(i, name="Status", value=f"**✅ Approved by {reviewer.name}**", inline=True)
                        if note:
                            embed.add_field(name="Reviewer Note", value=note[:1000], inline=False)
                        # Disable approve and reject buttons, keep DM enabled
                        new_view = build_review_view(sub_id, disabled=True)
                        await msg.edit(embed=embed, view=new_view)
                except Exception as e:
                    logger.warning("Could not update destination embed #%s: %s", dest_msg_id, e)

        # 4. DM Submitter
        try:
            submitter = await self.bot.fetch_user(sub["user_id"])
            if submitter:
                dm_text = f"🎉 **Good news!** Your submission for **{form['title']}** in **{guild.name}** has been **Approved**!"
                if note:
                    dm_text += f"\n**Staff Note:** {note}"
                if role_assigned:
                    dm_text += "\n✨ The corresponding server role has been granted to your account."
                await submitter.send(dm_text)
        except Exception:
            pass  # User has DMs disabled

        if interaction and not interaction.response.is_done():
            await interaction.response.send_message(f"✅ Submission **#{sub_id}** approved successfully.", ephemeral=True)
        return True

    async def process_rejection(
        self,
        interaction: discord.Interaction,
        sub_id: int,
        form: Dict[str, Any],
        reason: str,
        reviewer_user: Optional[discord.User | discord.Member] = None
    ) -> bool:
        """
        Processes submission rejection: updates DB, updates destination message embed,
        disables approve/reject buttons, and sends DM with rejection reason.
        """
        reviewer = reviewer_user or interaction.user
        guild = interaction.guild if interaction else self.bot.get_guild(form["guild_id"])
        if not guild:
            if interaction:
                await interaction.response.send_message("❌ Server context unavailable.", ephemeral=True)
            return False

        # 1. Update DB
        await self.db.update_submission_status(
            sub_id=sub_id,
            status="rejected",
            reviewer_id=reviewer.id,
            reviewer_name=reviewer.name,
            review_note=reason
        )

        sub = await self.db.get_submission(sub_id)
        if not sub:
            return False

        # 2. Update Destination Message Embed
        dest_msg_id = sub.get("dest_message_id")
        dest_ch_id = form.get("dest_channel_id")
        if dest_ch_id and dest_msg_id:
            ch = guild.get_channel(dest_ch_id)
            if ch and hasattr(ch, "fetch_message"):
                try:
                    msg = await ch.fetch_message(dest_msg_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.color = 0xEF4444  # Red
                        for i, f in enumerate(embed.fields):
                            if f.name.lower() == "status":
                                embed.set_field_at(i, name="Status", value=f"**❌ Rejected by {reviewer.name}**", inline=True)
                        embed.add_field(name="Rejection Reason", value=reason[:1000], inline=False)
                        new_view = build_review_view(sub_id, disabled=True)
                        await msg.edit(embed=embed, view=new_view)
                except Exception as e:
                    logger.warning("Could not update destination embed #%s: %s", dest_msg_id, e)

        # 3. DM Submitter
        try:
            submitter = await self.bot.fetch_user(sub["user_id"])
            if submitter:
                dm_text = f"❌ Your submission for **{form['title']}** in **{guild.name}** was **Rejected**.\n**Reason:** {reason}"
                await submitter.send(dm_text)
        except Exception:
            pass

        if interaction and not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Submission **#{sub_id}** has been rejected.", ephemeral=True)
        return True

    async def process_staff_dm(
        self,
        interaction: discord.Interaction,
        sub_id: int,
        form: Dict[str, Any],
        dm_message: str,
        reviewer_user: Optional[discord.User | discord.Member] = None
    ) -> bool:
        """Sends an official staff message to the submitter and notes it on the destination message."""
        reviewer = reviewer_user or interaction.user
        guild = interaction.guild if interaction else self.bot.get_guild(form["guild_id"])
        sub = await self.db.get_submission(sub_id)
        if not sub or not guild:
            if interaction:
                await interaction.response.send_message("❌ Submission or server context unavailable.", ephemeral=True)
            return False

        dm_delivered = False
        try:
            submitter = await self.bot.fetch_user(sub["user_id"])
            if submitter:
                embed = discord.Embed(
                    title=f"💬 Staff Message regarding '{form['title']}'",
                    description=dm_message,
                    color=0x6366F1,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(text=f"Server: {guild.name} • Sent by {reviewer.name}")
                await submitter.send(embed=embed)
                dm_delivered = True
        except Exception as e:
            logger.warning("Could not DM user %s: %s", sub["user_id"], e)

        # Log on destination channel
        dest_ch_id = form.get("dest_channel_id")
        if dest_ch_id:
            ch = guild.get_channel(dest_ch_id)
            if ch and hasattr(ch, "send"):
                try:
                    await ch.send(
                        content=f"💬 Staff {reviewer.mention} sent a DM to submitter <@{sub['user_id']}> regarding submission **#{sub_id}**:\n> {dm_message[:200]}",
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                except Exception:
                    pass

        if interaction and not interaction.response.is_done():
            if dm_delivered:
                await interaction.response.send_message(f"✅ Staff DM sent to <@{sub['user_id']}> successfully.", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ Could not deliver DM to user (they may have DMs closed), but message was logged to destination channel.", ephemeral=True)
        return dm_delivered

    # =========================================================================
    # Discord Slash Commands (/form ...)
    # =========================================================================

    @form_group.command(name="create", description="Create a new custom form")
    @app_commands.describe(
        title="Title of the form (e.g., Staff Application)",
        dest_channel="Channel where new submissions will be delivered",
        description="Optional description or instructions for the form",
        review_enabled="Require staff approval/rejection for submissions",
        approved_role="Role to grant automatically upon approval (optional)",
        max_submissions="Max submissions per user (0 for unlimited)",
        cooldown="Cooldown between submissions in seconds (default: 60)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_create_form(
        self,
        interaction: discord.Interaction,
        title: str,
        dest_channel: discord.TextChannel,
        description: Optional[str] = None,
        review_enabled: bool = False,
        approved_role: Optional[discord.Role] = None,
        max_submissions: int = 0,
        cooldown: int = 60
    ):
        guild = interaction.guild
        if not guild:
            return

        # Role Hierarchy Security Check
        if approved_role:
            bot_top = guild.me.top_role
            user_top = interaction.user.top_role if isinstance(interaction.user, discord.Member) else None
            if (user_top and approved_role.position >= user_top.position) or (approved_role.position >= bot_top.position):
                await interaction.response.send_message(
                    "❌ **Role Hierarchy Error:** The approved role must be strictly lower than both your highest role and the bot's highest role.",
                    ephemeral=True
                )
                return
            if approved_role.permissions.administrator or approved_role.permissions.manage_guild:
                await interaction.response.send_message(
                    "❌ **Security Violation:** The approved role cannot possess Administrator or Manage Server permissions.",
                    ephemeral=True
                )
                return

        form_id = await self.db.create_form(
            guild_id=guild.id,
            title=title,
            dest_channel_id=dest_channel.id,
            description=description,
            review_enabled=review_enabled,
            approved_role_id=approved_role.id if approved_role else None,
            max_submissions_per_user=max_submissions,
            cooldown_seconds=cooldown,
            created_by=f"{interaction.user.name} ({interaction.user.id})"
        )

        embed = discord.Embed(
            title="✅ Form Created Successfully",
            description=f"**Form ID:** `{form_id}`\n**Title:** {title}\n**Destination:** {dest_channel.mention}\n**Review Required:** {'Yes' if review_enabled else 'No'}",
            color=0x10B981
        )
        embed.add_field(name="Next Steps", value=f"• Add questions: `/form question add form_id:{form_id} ...`\n• Publish embed: `/form publish form_id:{form_id}`\n• Or configure in Web Dashboard!", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @form_group.command(name="list", description="List all forms created on this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_list_forms(self, interaction: discord.Interaction):
        forms = await self.db.list_forms(interaction.guild_id)  # type: ignore
        if not forms:
            await interaction.response.send_message("ℹ️ No forms have been created on this server yet. Create one with `/form create`.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 Server Forms ({len(forms)})",
            color=0x6366F1
        )
        for f in forms:
            status_emoji = "🔴 Closed" if (f.get("is_closed") or not f.get("is_enabled")) else "🟢 Open"
            embed.add_field(
                name=f"#{f['id']}: {f['title']}",
                value=f"• Status: {status_emoji}\n• Questions: **{f.get('question_count', 0)}**\n• Submissions: **{f.get('total_submissions', 0)}**\n• Review: {'Enabled' if f.get('review_enabled') else 'Disabled'}",
                inline=True
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @form_group.command(name="publish", description="Publish the public interactive form panel to a channel")
    @app_commands.describe(
        form_id="ID of the form to publish",
        channel="Channel to publish the form embed to (defaults to current channel)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_publish_form(self, interaction: discord.Interaction, form_id: int, channel: Optional[discord.TextChannel] = None):
        target_ch = channel or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Target channel must be a text channel.", ephemeral=True)
            return

        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found on this server.", ephemeral=True)
            return

        questions = await self.db.get_questions(form_id)
        if not questions:
            await interaction.response.send_message("⚠️ Please add at least one question to the form before publishing (`/form question add`).", ephemeral=True)
            return

        embed, view = build_panel_embed_and_view(form)
        msg = await target_ch.send(embed=embed, view=view)
        await self.db.update_panel_message(form_id, target_ch.id, msg.id)

        await interaction.response.send_message(
            f"✅ Form **#{form_id}** panel has been published to {target_ch.mention}! [Jump to Panel]({msg.jump_url})",
            ephemeral=True
        )

    @form_group.command(name="schedule", description="Configure open and close schedule for a form")
    @app_commands.describe(
        form_id="ID of the form to schedule",
        duration="Relative duration from now (e.g., '5h', '30m', '2d')",
        opens_at="Manual Unix timestamp when form should open (optional)",
        closes_at="Manual Unix timestamp when form should close (optional)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_schedule_form(
        self,
        interaction: discord.Interaction,
        form_id: int,
        duration: Optional[str] = None,
        opens_at: Optional[int] = None,
        closes_at: Optional[int] = None
    ):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        now = int(time.time())
        target_open = opens_at
        target_close = closes_at

        if duration:
            sec = parse_relative_duration(duration)
            if not sec:
                await interaction.response.send_message("❌ Invalid duration format. Examples: `30m`, `5h`, `2d`, `1w`.", ephemeral=True)
                return
            target_open = now
            target_close = now + sec

        await self.db.update_form(
            interaction.guild_id,  # type: ignore
            form_id,
            opens_at=target_open,
            closes_at=target_close,
            is_closed=False
        )
        form["opens_at"] = target_open
        form["closes_at"] = target_close
        form["is_closed"] = 0

        await self.refresh_panel_message(form)

        info = f"✅ Form **#{form_id}** schedule updated!"
        if target_open:
            info += f"\n• **Opens:** <t:{target_open}:R> (<t:{target_open}:F>)"
        if target_close:
            info += f"\n• **Closes:** <t:{target_close}:R> (<t:{target_close}:F>)"
        await interaction.response.send_message(info, ephemeral=True)

    @form_group.command(name="close", description="Manually close an open form")
    @app_commands.describe(form_id="ID of the form to close")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_close_form(self, interaction: discord.Interaction, form_id: int):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        await self.db.set_form_closed(interaction.guild_id, form_id, is_closed=True)  # type: ignore
        form["is_closed"] = 1
        await self.refresh_panel_message(form)
        await interaction.response.send_message(f"🔒 Form **#{form_id}** is now closed.", ephemeral=True)

    @form_group.command(name="reopen", description="Reopen a closed form")
    @app_commands.describe(form_id="ID of the form to reopen")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_reopen_form(self, interaction: discord.Interaction, form_id: int):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        await self.db.update_form(interaction.guild_id, form_id, is_closed=False, closes_at=None)  # type: ignore
        form["is_closed"] = 0
        form["closes_at"] = None
        await self.refresh_panel_message(form)
        await interaction.response.send_message(f"🟢 Form **#{form_id}** has been reopened.", ephemeral=True)

    @form_group.command(name="export", description="Export form submissions to Excel (.xlsx) or CSV")
    @app_commands.describe(
        form_id="ID of the form to export",
        format="Export format: 'csv' or 'xlsx'",
        status="Filter submissions by status: all, pending, approved, rejected"
    )
    @app_commands.choices(
        format=[
            app_commands.Choice(name="CSV Spreadsheet (.csv)", value="csv"),
            app_commands.Choice(name="Microsoft Excel (.xlsx)", value="xlsx")
        ],
        status=[
            app_commands.Choice(name="All Submissions", value="all"),
            app_commands.Choice(name="Pending Review", value="pending"),
            app_commands.Choice(name="Approved Only", value="approved"),
            app_commands.Choice(name="Rejected Only", value="rejected")
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_export_form(
        self,
        interaction: discord.Interaction,
        form_id: int,
        format: str = "csv",
        status: str = "all"
    ):
        await interaction.response.defer(ephemeral=True)
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.followup.send("❌ Form not found.", ephemeral=True)
            return

        questions = await self.db.get_questions(form_id)
        submissions = await self.db.list_submissions(interaction.guild_id, form_id, status=status, limit=5000)  # type: ignore

        if format == "xlsx" and OPENPYXL_AVAILABLE:
            file_bytes = generate_xlsx_bytes(form, questions, submissions)
            file_name = f"form_{form_id}_submissions.xlsx"
        else:
            file_bytes = generate_csv_bytes(form, questions, submissions)
            file_name = f"form_{form_id}_submissions.csv"

        discord_file = discord.File(fp=file_bytes, filename=file_name)
        await interaction.followup.send(
            f"📊 Exported **{len(submissions)}** submissions for **{form['title']}** ({status.upper()}):",
            file=discord_file,
            ephemeral=True
        )

    # =========================================================================
    # Question Management Subcommands (/form question ...)
    # =========================================================================

    @question_group.command(name="add", description="Add a new custom question to a form")
    @app_commands.describe(
        form_id="ID of the target form",
        label="Question text or prompt (max 45 chars)",
        style="Input style: short single-line or paragraph multi-line",
        placeholder="Optional hint text displayed in the input (max 100 chars)",
        required="Whether an answer is mandatory",
        min_length="Minimum character length (0 to 1000)",
        max_length="Maximum character length (up to 1000)"
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="Short Single-Line", value="short"),
        app_commands.Choice(name="Paragraph Multi-Line", value="paragraph")
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_add_question(
        self,
        interaction: discord.Interaction,
        form_id: int,
        label: str,
        style: str = "short",
        placeholder: Optional[str] = None,
        required: bool = True,
        min_length: int = 0,
        max_length: int = 1000
    ):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        qid = await self.db.add_question(
            form_id=form_id,
            label=label,
            style=style,
            placeholder=placeholder,
            required=required,
            min_length=min_length,
            max_length=max_length
        )

        all_q = await self.db.get_questions(form_id)
        await interaction.response.send_message(
            f"✅ Question **#{qid}** added to Form **#{form_id}**!\n"
            f"• Label: **{label[:45]}**\n"
            f"• Style: **{style.capitalize()}**\n"
            f"• Total Questions: **{len(all_q)}** (Forms with 6+ questions will automatically use chained multi-page modals).",
            ephemeral=True
        )

    @question_group.command(name="list", description="List all questions for a form")
    @app_commands.describe(form_id="ID of the target form")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_list_questions(self, interaction: discord.Interaction, form_id: int):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        questions = await self.db.get_questions(form_id)
        if not questions:
            await interaction.response.send_message(f"ℹ️ Form **#{form_id}** currently has no questions.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"❓ Questions for Form #{form_id}: {form['title']}",
            description=f"Total: **{len(questions)}** questions",
            color=0x6366F1
        )
        for idx, q in enumerate(questions, start=1):
            req = "Required" if q.get("required") else "Optional"
            embed.add_field(
                name=f"#{idx} (ID: {q['id']}): {q['label']}",
                value=f"• Style: {q.get('style', 'short').capitalize()} | {req}\n• Limits: {q.get('min_length', 0)}-{q.get('max_length', 1000)} chars\n• Placeholder: `{q.get('placeholder') or 'None'}`",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @question_group.command(name="delete", description="Delete a question from a form")
    @app_commands.describe(form_id="ID of the form", question_id="ID of the question to delete")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_delete_question(self, interaction: discord.Interaction, form_id: int, question_id: int):
        form = await self.db.get_form(interaction.guild_id, form_id)  # type: ignore
        if not form:
            await interaction.response.send_message("❌ Form not found.", ephemeral=True)
            return

        success = await self.db.delete_question(form_id, question_id)
        if success:
            await interaction.response.send_message(f"🗑️ Question **#{question_id}** deleted from Form **#{form_id}**.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Question not found on this form.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(FormsCog(bot))
