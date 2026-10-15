"""
QC & Fault Tracker Cog for Discord.py 2.x.
Provides:
- Minimal and focused slash command: /fault log
- Dynamic Autocomplete for active project codes
- Role-mapped automatic team and leader resolution
- Discord CDN permanent screenshot re-uploading and URL extraction
- Color-coded alert embeds with strict Mention isolation
- DynamicItem persistent [✅ Acknowledge / Fixed] button across bot restarts
- Bridge helper for web dashboard fault alert dispatch
"""

import os
import re
import io
import time
import json
import logging
from typing import Optional, List, Dict, Any, Union, Tuple
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.qc_faults_db import QCFaultsDB

logger = logging.getLogger("QCFaultsCog")

SEVERITY_COLORS = {
    "minor": discord.Color.green(),     # 0x57F287
    "major": discord.Color.gold(),      # 0xFEE75C
    "critical": discord.Color.red(),    # 0xED4245
}

SEVERITY_ICONS = {
    "minor": "🟢 Minor Fault Alert",
    "major": "🟡 Major Fault Alert",
    "critical": "🔴 Critical Fault Alert",
}


# =============================================================================
# Persistent Dynamic Button: [✅ Acknowledge / Fixed]
# =============================================================================

class DynamicFaultAckButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fault:ack:(?P<fault_id>\d+)"):
    """
    Persistent Dynamic Button for acknowledging a QC fault.
    Custom ID pattern: fault:ack:<fault_id>
    Works permanently across bot and VPS restarts.
    """
    def __init__(
        self,
        fault_id: int,
        label: str = "✅ Acknowledge / Fixed",
        style: discord.ButtonStyle = discord.ButtonStyle.success,
        disabled: bool = False
    ):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                custom_id=f"fault:ack:{fault_id}"
            )
        )
        self.fault_id = fault_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        fault_id = int(match.group("fault_id"))
        return cls(fault_id, label=item.label or "✅ Acknowledge / Fixed", style=item.style, disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ This action can only be performed in a server.", ephemeral=True)
            return

        db = QCFaultsDB.get_instance()
        fault = await db.get_fault_log(self.fault_id, guild_id)
        if not fault:
            await interaction.followup.send(f"❌ Fault record #{self.fault_id} not found.", ephemeral=True)
            return

        if fault.get("status") == "acknowledged":
            await interaction.followup.send(f"ℹ️ Fault #{self.fault_id} has already been acknowledged.", ephemeral=True)
            return

        # Authorization: Assigned labeler, Admin, Checker role, or Team Leader
        user = interaction.user
        is_assigned_labeler = (user.id == fault.get("labeler_id"))
        is_admin = bool(
            getattr(user.guild_permissions, "administrator", False) or
            getattr(user.guild_permissions, "manage_guild", False)
        )
        is_team_leader = (fault.get("leader_id") is not None and user.id == fault.get("leader_id"))

        # Check checker roles from settings
        settings = await db.get_settings(guild_id)
        checker_roles = set(settings.get("checker_role_ids") or [])
        user_role_ids = {r.id for r in getattr(user, "roles", [])}
        is_checker = bool(checker_roles.intersection(user_role_ids))

        if not (is_assigned_labeler or is_admin or is_team_leader or is_checker):
            await interaction.followup.send(
                "❌ You do not have permission to acknowledge this fault.\n"
                "Only the assigned labeler, team leader, or staff may acknowledge it.",
                ephemeral=True
            )
            return

        # Mark as acknowledged in database
        await db.acknowledge_fault(self.fault_id, guild_id)

        # Update the original message embed and disable button
        try:
            msg = interaction.message
            if msg and msg.embeds:
                embed = msg.embeds[0]
                now_ts = int(time.time())

                # Update Status field or add Acknowledged field
                status_updated = False
                for idx, field in enumerate(embed.fields):
                    if field.name and "status" in field.name.lower():
                        embed.set_field_at(
                            idx,
                            name="Status",
                            value=f"✅ **Acknowledged** by {user.mention} (<t:{now_ts}:R>)",
                            inline=field.inline
                        )
                        status_updated = True
                        break

                if not status_updated:
                    embed.add_field(
                        name="Status",
                        value=f"✅ **Acknowledged** by {user.mention} (<t:{now_ts}:R>)",
                        inline=False
                    )

                embed.set_footer(text=f"Acknowledged by {user.display_name} • Fault #{self.fault_id}")

                # Create updated view with disabled button
                updated_view = discord.ui.View(timeout=None)
                updated_view.add_item(
                    discord.ui.Button(
                        label="✅ Acknowledged / Fixed",
                        style=discord.ButtonStyle.secondary,
                        disabled=True,
                        custom_id=f"fault:ack:{self.fault_id}"
                    )
                )

                await msg.edit(embed=embed, view=updated_view)
        except Exception as e:
            logger.warning(f"[!] Could not update alert message on Discord: {e}")

        await interaction.followup.send(
            f"✅ Fault **#{self.fault_id}** has been marked as **Acknowledged / Fixed**.",
            ephemeral=True
        )


# =============================================================================
# Helper: Build Embed & View
# =============================================================================

def build_fault_alert_embed(
    fault_id: int,
    project_code: str,
    labeler: Union[discord.Member, discord.User],
    team_name: str,
    leader_user_id: Optional[int],
    logged_by: Union[discord.Member, discord.User],
    severity: str,
    description: str,
    fault_type: Optional[str] = None,
    image_url: Optional[str] = None
) -> Tuple[str, discord.Embed, discord.ui.View, discord.AllowedMentions]:
    """
    Constructs the notification string, color-coded embed, persistent view, and allowed mentions.
    """
    sev_key = severity.lower()
    color = SEVERITY_COLORS.get(sev_key, discord.Color.green())
    title_icon = SEVERITY_ICONS.get(sev_key, f"⚠️ Fault Alert — {severity.upper()}")

    embed = discord.Embed(
        title=title_icon,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="📁 Project Code", value=f"`{project_code}`", inline=True)
    embed.add_field(name="👤 Labeler", value=f"{labeler.mention} (`{labeler.display_name}`)", inline=True)
    embed.add_field(name="👥 Team", value=f"**{team_name}**", inline=True)

    leader_str = f"<@{leader_user_id}>" if leader_user_id else "*Unassigned*"
    embed.add_field(name="👑 Team Leader", value=leader_str, inline=True)
    embed.add_field(name="⚡ Severity", value=f"**{severity.upper()}**", inline=True)

    if fault_type and fault_type.strip():
        embed.add_field(name="🏷️ Fault Type", value=f"`{fault_type.strip()}`", inline=True)

    embed.add_field(name="📝 Logged By", value=f"{logged_by.mention}", inline=True)
    embed.add_field(name="Status", value="🔴 **Open** — Awaiting fix/acknowledgement", inline=True)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=f"Fault #{fault_id} • Click the button below once fixed")

    # Persistent View with Ack Button
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicFaultAckButton(fault_id=fault_id))

    # Pings in content
    pings = [labeler.mention]
    mention_users = [labeler]

    if leader_user_id:
        pings.append(f"<@{leader_user_id}>")

    content = f"⚠️ **QC Fault Alert:** {labeler.mention}"
    if leader_user_id:
        content += f" (CC: <@{leader_user_id}>)"

    allowed_mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)

    return content, embed, view, allowed_mentions


# =============================================================================
# Main Cog Definition
# =============================================================================

class QCFaultsCog(commands.Cog, name="QCFaults"):
    """
    QC & Fault Tracker Cog. Handles logging faults, project autocomplete,
    and acknowledging fault alerts.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = QCFaultsDB.get_instance()

    async def cog_load(self):
        """Initializes database and registers the dynamic persistent acknowledge button."""
        await self.db.init_db()
        self.bot.add_dynamic_items(DynamicFaultAckButton)
        logger.info("[OK] QCFaultsCog loaded and DynamicFaultAckButton registered.")

    # =========================================================================
    # Permission Helper
    # =========================================================================

    async def is_qc_authorized(self, interaction: discord.Interaction) -> bool:
        """
        Validates if user is Server Admin, has a configured Checker Role,
        or is a registered Team Leader.
        """
        if not interaction.guild:
            return False

        user = interaction.user
        perms = interaction.permissions

        # 1. Server Admin
        if perms.administrator or perms.manage_guild:
            return True

        guild_id = interaction.guild_id
        # 2. Configured Checker Roles
        settings = await self.db.get_settings(guild_id)
        checker_roles = set(settings.get("checker_role_ids") or [])
        user_roles = {r.id for r in getattr(user, "roles", [])}
        if checker_roles.intersection(user_roles):
            return True

        # 3. Registered Team Leader
        teams = await self.db.list_teams(guild_id)
        for t in teams:
            if t.get("leader_user_id") and t["leader_user_id"] == user.id:
                return True
            if t.get("leader_role_id") and t["leader_role_id"] in user_roles:
                return True

        return False

    # =========================================================================
    # Autocomplete for Project Codes
    # =========================================================================

    async def project_code_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> List[app_commands.Choice[str]]:
        """Dynamically autocompletes active project codes for this guild."""
        if not interaction.guild_id:
            return []

        projects = await self.db.list_projects(interaction.guild_id, only_active=True)
        cur = current.strip().lower()
        choices = []

        for p in projects:
            code = p["project_code"]
            name = p["project_name"]
            display = f"{code} — {name}"
            if not cur or cur in code.lower() or cur in name.lower():
                choices.append(app_commands.Choice(name=display[:100], value=code))
            if len(choices) >= 25:
                break

        return choices

    # =========================================================================
    # Slash Command: /fault log
    # =========================================================================

    fault_group = app_commands.Group(
        name="fault",
        description="QC & Fault Tracker commands for quality checking"
    )

    @fault_group.command(name="log", description="Log a QC fault for a team labeler and dispatch a color-coded alert.")
    @app_commands.describe(
        labeler="The team member / labeler who made the error",
        project_code="Active project code (type to search)",
        severity="Severity of the fault (Minor, Major, or Critical)",
        description="Detailed explanation of the fault / Task ID reference",
        fault_type="Optional manual category or tag (e.g. Missed Object, Misclassification)",
        screenshot="Optional screenshot of the task / error (image file)",
        team_override="Optional team role override if labeler has multiple teams or unassigned"
    )
    @app_commands.choices(
        severity=[
            app_commands.Choice(name="🟢 Minor", value="minor"),
            app_commands.Choice(name="🟡 Major", value="major"),
            app_commands.Choice(name="🔴 Critical", value="critical"),
        ]
    )
    async def fault_log(
        self,
        interaction: discord.Interaction,
        labeler: discord.Member,
        project_code: str,
        severity: app_commands.Choice[str],
        description: str,
        fault_type: Optional[str] = None,
        screenshot: Optional[discord.Attachment] = None,
        team_override: Optional[discord.Role] = None
    ):
        # 1. Defer response immediately as ephemeral
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ This command must be used within a server.", ephemeral=True)
            return

        # 2. Permission Guard
        if not await self.is_qc_authorized(interaction):
            await interaction.followup.send(
                "❌ **Access Denied:** Only Server Administrators, Team Leaders, "
                "or configured QC Checkers have permission to log faults.",
                ephemeral=True
            )
            return

        # 3. Validate image attachment if provided
        if screenshot:
            content_type = getattr(screenshot, "content_type", "") or ""
            filename = (screenshot.filename or "").lower()
            valid_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
            if not content_type.startswith("image/") and not any(filename.endswith(ext) for ext in valid_exts):
                await interaction.followup.send(
                    "❌ The provided attachment is not a supported image file (.png, .jpg, .jpeg, .webp, .gif).",
                    ephemeral=True
                )
                return

        # 4. Resolve Labeler's Team & Leader
        team_id: Optional[int] = None
        team_name = "Unassigned"
        leader_user_id: Optional[int] = None
        team_channel_id: Optional[int] = None

        guild_teams = await self.db.list_teams(guild.id)

        if team_override:
            # Match team_override role against qc_teams
            matched_team = next((t for t in guild_teams if t["team_role_id"] == team_override.id), None)
            if matched_team:
                team_id = matched_team["id"]
                team_name = matched_team["team_name"]
                leader_user_id = matched_team.get("leader_user_id")
                team_channel_id = matched_team.get("team_channel_id")
            else:
                team_name = team_override.name
        else:
            # Auto-detect from labeler's roles
            labeler_role_ids = {r.id for r in labeler.roles}
            matched_team = next((t for t in guild_teams if t["team_role_id"] in labeler_role_ids), None)
            if matched_team:
                team_id = matched_team["id"]
                team_name = matched_team["team_name"]
                leader_user_id = matched_team.get("leader_user_id")
                team_channel_id = matched_team.get("team_channel_id")

        # 5. Resolve Target Discord Alert Channel
        settings = await self.db.get_settings(guild.id)
        target_channel: Optional[discord.TextChannel] = None

        if team_channel_id:
            target_channel = guild.get_channel(team_channel_id)
        if not target_channel and settings.get("alert_channel_id"):
            target_channel = guild.get_channel(settings["alert_channel_id"])
        if not target_channel and isinstance(interaction.channel, discord.TextChannel):
            target_channel = interaction.channel

        if not target_channel:
            await interaction.followup.send(
                "❌ Could not determine an alert channel. Please configure a default alert channel "
                "in the Web Dashboard Settings or set one for this team.",
                ephemeral=True
            )
            return

        # Check bot send permissions in target channel
        bot_perms = target_channel.permissions_for(guild.me)
        if not bot_perms.send_messages or not bot_perms.embed_links:
            await interaction.followup.send(
                f"❌ Bot lacks permission to send messages or embed links in {target_channel.mention}.",
                ephemeral=True
            )
            return

        # 6. Insert initial record in SQLite to obtain the unique fault ID
        try:
            fault_id = await self.db.create_fault_log(
                guild_id=guild.id,
                project_code=project_code.strip(),
                labeler_id=labeler.id,
                labeler_name=str(labeler),
                team_id=team_id,
                team_name=team_name,
                leader_id=leader_user_id,
                logged_by_id=interaction.user.id,
                logged_by_name=str(interaction.user),
                severity=severity.value,
                description=description.strip(),
                fault_type=fault_type.strip() if fault_type else None,
                image_url=None,
                alert_channel_id=target_channel.id,
                alert_message_id=None
            )
        except Exception as e:
            logger.error(f"[ERROR] Failed to insert fault log: {e}")
            await interaction.followup.send(f"❌ Failed to record fault in database: `{e}`", ephemeral=True)
            return

        # 7. Re-upload screenshot as discord.File if provided (ensuring persistent Discord CDN storage)
        image_url: Optional[str] = None
        file_to_send: Optional[discord.File] = None

        if screenshot:
            try:
                # Read bytes and construct fresh discord.File
                img_bytes = await screenshot.read()
                file_to_send = discord.File(io.BytesIO(img_bytes), filename=screenshot.filename)
            except Exception as e:
                logger.warning(f"[!] Could not read screenshot attachment: {e}")

        # 8. Build alert embed, dynamic ack view, and ping content
        content, embed, view, allowed_mentions = build_fault_alert_embed(
            fault_id=fault_id,
            project_code=project_code.strip(),
            labeler=labeler,
            team_name=team_name,
            leader_user_id=leader_user_id,
            logged_by=interaction.user,
            severity=severity.value,
            description=description.strip(),
            fault_type=fault_type.strip() if fault_type else None,
            image_url=None
        )

        if file_to_send:
            embed.set_image(url=f"attachment://{file_to_send.filename}")

        # 9. Send alert message to target channel
        try:
            if file_to_send:
                sent_msg = await target_channel.send(
                    content=content,
                    embed=embed,
                    file=file_to_send,
                    view=view,
                    allowed_mentions=allowed_mentions
                )
            else:
                sent_msg = await target_channel.send(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=allowed_mentions
                )

            # Extract permanent Discord CDN URL from sent message
            if sent_msg.embeds and sent_msg.embeds[0].image and sent_msg.embeds[0].image.url:
                image_url = sent_msg.embeds[0].image.url
            elif sent_msg.attachments:
                image_url = sent_msg.attachments[0].url

            # Update DB with alert message ID and permanent image URL string
            async with self.db.get_connection() as conn:
                await conn.execute("""
                    UPDATE qc_fault_logs
                    SET alert_message_id = ?, image_url = ?
                    WHERE id = ?;
                """, (sent_msg.id, image_url, fault_id))
                await conn.commit()

        except Exception as e:
            logger.error(f"[ERROR] Failed to post alert embed to channel: {e}")
            await interaction.followup.send(
                f"⚠️ Fault was saved to database (#{fault_id}), but failed to post alert in {target_channel.mention}: `{e}`",
                ephemeral=True
            )
            return

        # 10. Send successful confirmation to the caller
        await interaction.followup.send(
            f"✅ **Fault #{fault_id} Logged Successfully!**\n"
            f"• **Project:** `{project_code}`\n"
            f"• **Labeler:** {labeler.mention}\n"
            f"• **Team:** {team_name}\n"
            f"• **Severity:** {severity.name}\n"
            f"• **Channel:** {target_channel.mention}",
            ephemeral=True
        )

    # Register autocomplete for project_code
    @fault_log.autocomplete("project_code")
    async def fault_log_project_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.project_code_autocomplete(interaction, current)

    # =========================================================================
    # Web Bridge Dispatcher
    # =========================================================================

    async def dispatch_web_fault_alert(
        self,
        fault_id: int,
        guild_id: int,
        project_code: str,
        labeler_id: int,
        severity: str,
        description: str,
        fault_type: Optional[str] = None,
        image_url: Optional[str] = None,
        team_id: Optional[int] = None,
        logged_by_id: Optional[int] = None,
        logged_by_name: Optional[str] = None
    ) -> Optional[int]:
        """
        Dispatches a fault alert to Discord when logged via the Web Dashboard.
        Returns the alert_message_id if successfully sent.
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None

        # Resolve labeler
        labeler = guild.get_member(labeler_id)
        if not labeler:
            try:
                labeler = await guild.fetch_member(labeler_id)
            except Exception:
                labeler = None

        if not labeler:
            try:
                labeler = await self.bot.fetch_user(labeler_id)
            except Exception:
                pass

        if not labeler:
            labeler = discord.Object(id=labeler_id)
            labeler.display_name = f"User #{labeler_id}"
            labeler.mention = f"<@{labeler_id}>"

        # Resolve team
        team_name = "Unassigned"
        leader_user_id = None
        team_channel_id = None

        if team_id:
            team = await self.db.get_team(team_id, guild_id)
            if team:
                team_name = team["team_name"]
                leader_user_id = team.get("leader_user_id")
                team_channel_id = team.get("team_channel_id")
        else:
            # Check labeler roles
            if hasattr(labeler, "roles"):
                role_ids = {r.id for r in labeler.roles}
                guild_teams = await self.db.list_teams(guild_id)
                matched = next((t for t in guild_teams if t["team_role_id"] in role_ids), None)
                if matched:
                    team_name = matched["team_name"]
                    leader_user_id = matched.get("leader_user_id")
                    team_channel_id = matched.get("team_channel_id")

        # Resolve target channel
        settings = await self.db.get_settings(guild_id)
        target_channel: Optional[discord.TextChannel] = None
        if team_channel_id:
            target_channel = guild.get_channel(team_channel_id)
        if not target_channel and settings.get("alert_channel_id"):
            target_channel = guild.get_channel(settings["alert_channel_id"])
        if not target_channel:
            target_channel = guild.system_channel

        if not target_channel:
            return None

        # Resolve logged_by
        logged_by = None
        if logged_by_id:
            logged_by = guild.get_member(logged_by_id)
        if not logged_by:
            logged_by = discord.Object(id=logged_by_id or 0)
            logged_by.display_name = logged_by_name or "Web Dashboard Admin"
            logged_by.mention = f"**{logged_by_name or 'Web Dashboard'}**"

        content, embed, view, allowed_mentions = build_fault_alert_embed(
            fault_id=fault_id,
            project_code=project_code,
            labeler=labeler,
            team_name=team_name,
            leader_user_id=leader_user_id,
            logged_by=logged_by,
            severity=severity,
            description=description,
            fault_type=fault_type,
            image_url=image_url
        )

        try:
            msg = await target_channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=allowed_mentions
            )
            # Update database
            await self.db.update_fault_alert_message(fault_id, target_channel.id, msg.id)
            return msg.id
        except Exception as e:
            logger.warning(f"[!] Could not dispatch web fault alert to Discord: {e}")
            return None


async def setup(bot: commands.Bot):
    cog = QCFaultsCog(bot)
    await bot.add_cog(cog)
