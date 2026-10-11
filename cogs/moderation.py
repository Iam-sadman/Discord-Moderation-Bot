"""
Moderation and Role Management Cog for ModularBot.
Provides persistent self-service role panels, dynamic button views,
reaction role event listeners, and moderator slash commands.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from services.moderation_db import ModerationDB
from services.moderation_engine import ModerationEngine

logger = logging.getLogger("ModerationCog")


class RolePanelButton(discord.ui.Button):
    def __init__(self, panel_id: int, item_id: int, role_id: int, label: str,
                 style: discord.ButtonStyle, emoji: Optional[str] = None, toggle: bool = True):
        super().__init__(
            label=label or None,
            style=style,
            emoji=emoji or None,
            custom_id=f"role_panel:{panel_id}:{item_id}",
            row=None
        )
        self.panel_id = panel_id
        self.item_id = item_id
        self.role_id = role_id
        self.toggle = toggle

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ This button can only be used within a server.", ephemeral=True)
            return

        db = ModerationDB.get_instance()
        # Verify self-service allowlist
        allowed = await db.is_role_self_assign_allowed(guild.id, self.role_id)
        if not allowed:
            await interaction.response.send_message("❌ This role is not permitted for self-service assignment.", ephemeral=True)
            return

        role = guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ The assigned role no longer exists on this server.", ephemeral=True)
            return

        # Bot hierarchy check
        bot_top_role = getattr(guild.me, "top_role", None)
        if getattr(role, "position", 0) >= getattr(bot_top_role, "position", 0):
            await interaction.response.send_message("❌ Bot configuration error: Bot cannot assign a role higher than or equal to its own top role.", ephemeral=True)
            return

        try:
            if role in member.roles:
                if self.toggle:
                    await member.remove_roles(role, reason="Self-service role panel toggle")
                    await interaction.response.send_message(f"✅ Removed the **{role.name}** role from your profile.", ephemeral=True)
                else:
                    await interaction.response.send_message(f"ℹ️ You already possess the **{role.name}** role.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Self-service role panel assignment")
                await interaction.response.send_message(f"✅ Assigned the **{role.name}** role to your profile!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot lacks permission to manage this role.", ephemeral=True)
        except Exception as e:
            logger.error("Error handling role panel button interaction: %s", e)
            await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)


class PersistentRolePanelView(discord.ui.View):
    """Persistent View with timeout=None for published role panels."""
    def __init__(self, panel_data: Dict[str, Any]):
        super().__init__(timeout=None)
        self.panel_id = panel_data["id"]

        style_map = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger
        }

        # Add buttons from panel items
        for item in panel_data.get("items", []):
            if item.get("item_type") == "button":
                btn_style = style_map.get(item.get("style", "secondary"), discord.ButtonStyle.secondary)
                self.add_item(RolePanelButton(
                    panel_id=self.panel_id,
                    item_id=item["id"],
                    role_id=item["role_id"],
                    label=item.get("label", ""),
                    style=btn_style,
                    emoji=item.get("emoji") or None,
                    toggle=bool(item.get("toggle", True))
                ))


class Moderation(commands.Cog):
    """Cog for Role Management, Self-Service Panels, and Server Moderation."""

    mod_group = app_commands.Group(
        name="mod",
        description="Moderation and role management commands"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = ModerationDB.get_instance()
        self.engine = ModerationEngine.get_instance(bot)

    async def cog_load(self):
        """Initializes moderation engine and registers all published persistent views."""
        await self.engine.start()
        await self._register_all_published_views()

    async def cog_unload(self):
        await self.engine.stop()

    async def _register_all_published_views(self):
        """Restores persistent buttons for all published panels across all guilds."""
        try:
            for guild in self.bot.guilds:
                panels = await self.db.list_role_panels(guild.id)
                for p in panels:
                    if p.get("message_id") and p.get("is_enabled"):
                        view = PersistentRolePanelView(p)
                        self.bot.add_view(view, message_id=p["message_id"])
                        logger.info("Restored persistent view for role panel #%d (Msg ID: %s)", p["id"], p["message_id"])
        except Exception as e:
            logger.error("Error registering persistent role panel views: %s", e)

    # =========================================================================
    # Reaction Role Gateway Listeners
    # =========================================================================

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or (self.bot.user and payload.user_id == self.bot.user.id):
            return

        panel = await self.db.get_role_panel_by_message_id(payload.message_id)
        if not panel or not panel.get("is_enabled"):
            return

        # Find matching reaction item
        emoji_str = str(payload.emoji)
        matching_item = None
        for item in panel.get("items", []):
            if item.get("item_type") == "reaction":
                item_emoji = item.get("emoji", "").strip()
                if item_emoji == emoji_str or (payload.emoji.name and item_emoji == payload.emoji.name):
                    matching_item = item
                    break

        if not matching_item:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = payload.member or guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        role_id = matching_item["role_id"]
        allowed = await self.db.is_role_self_assign_allowed(guild.id, role_id)
        if not allowed:
            logger.warning("Reaction role #%d clicked on panel #%d but role is not in allowlist", role_id, panel["id"])
            return

        role = guild.get_role(role_id)
        bot_top_role = getattr(guild.me, "top_role", None)
        if not role or getattr(role, "position", 0) >= getattr(bot_top_role, "position", 0):
            return

        try:
            if role not in member.roles:
                await member.add_roles(role, reason=f"Self-service reaction role panel #{panel['id']}")
        except Exception as e:
            logger.error("Error adding reaction role: %s", e)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or (self.bot.user and payload.user_id == self.bot.user.id):
            return

        panel = await self.db.get_role_panel_by_message_id(payload.message_id)
        if not panel or not panel.get("is_enabled"):
            return

        emoji_str = str(payload.emoji)
        matching_item = None
        for item in panel.get("items", []):
            if item.get("item_type") == "reaction":
                item_emoji = item.get("emoji", "").strip()
                if item_emoji == emoji_str or (payload.emoji.name and item_emoji == payload.emoji.name):
                    matching_item = item
                    break

        if not matching_item or not matching_item.get("toggle", True):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        role_id = matching_item["role_id"]
        allowed = await self.db.is_role_self_assign_allowed(guild.id, role_id)
        if not allowed:
            return

        role = guild.get_role(role_id)
        bot_top_role = getattr(guild.me, "top_role", None)
        if not role or getattr(role, "position", 0) >= getattr(bot_top_role, "position", 0):
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"Self-service reaction role removal panel #{panel['id']}")
        except Exception as e:
            logger.error("Error removing reaction role: %s", e)

    # =========================================================================
    # Slash Commands
    # =========================================================================

    @mod_group.command(name="status", description="Displays moderation and role management system status")
    @app_commands.default_permissions(manage_guild=True)
    async def mod_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        is_locked = await self.db.is_emergency_locked(guild_id)
        panels = await self.db.list_role_panels(guild_id)
        allowed_roles = await self.db.get_allowed_self_roles(guild_id)
        pending_tempbans = await self.db.get_all_pending_scheduled_actions(guild_id)

        embed = discord.Embed(
            title="🛡️ Moderation & Role Manager Telemetry",
            color=discord.Color.red() if is_locked else discord.Color.green()
        )
        embed.add_field(
            name="Circuit Breaker Status",
            value="🚨 **LOCKED** (Destructive operations disabled)" if is_locked else "🟢 **OPERATIONAL**",
            inline=True
        )
        embed.add_field(name="Published Panels", value=str(len(panels)), inline=True)
        embed.add_field(name="Whitelisted Self-Roles", value=str(len(allowed_roles)), inline=True)
        embed.add_field(name="Pending Temp-Bans", value=str(len(pending_tempbans)), inline=True)
        embed.set_footer(text="Manage via the Web Dashboard for full controls")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
    logger.info("[OK] Cog loaded: cogs.moderation")
