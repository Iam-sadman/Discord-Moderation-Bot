import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from config import Config
from utils.embed_builder import EmbedBuilder


class Logger(commands.Cog):
    """📋 Server Logger — tracks all server activity with detailed audit information."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Get the configured log channel for a guild."""
        channel_id = await Config.get_guild_setting(guild.id, "log_channel_id")
        if channel_id:
            return guild.get_channel(int(channel_id))
        return None

    async def _is_module_enabled(self, guild_id: int) -> bool:
        """Check if logger module is enabled."""
        enabled = await Config.get_guild_setting(guild_id, "module_logger_enabled", True)
        return enabled

    async def _get_filtered_users(self, guild_id: int) -> list[int]:
        """Get list of user IDs being tracked."""
        return await Config.get_guild_setting(guild_id, "log_filtered_users", [])

    async def _should_log_user(self, guild_id: int, user_id: int) -> bool:
        """Check if a specific user is being tracked. If no filters set, log everyone."""
        filters = await self._get_filtered_users(guild_id)
        if not filters:  # No filter = log everything
            return True
        return user_id in filters

    async def _get_audit_entry(self, guild: discord.Guild, action: discord.AuditLogAction,
                                target=None, retry=True) -> discord.AuditLogEntry | None:
        """Try to find a recent audit log entry for an action."""
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                # Only consider entries from the last 10 seconds
                if entry.created_at.timestamp() > (datetime.utcnow().timestamp() - 10):
                    if target is None or (entry.target and entry.target.id == target.id):
                        return entry
        except discord.Forbidden:
            pass
        return None

    async def _send_log(self, guild: discord.Guild, embed: discord.Embed):
        """Send an embed to the log channel."""
        channel = await self._get_log_channel(guild)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    # ──── Slash Commands ────

    log_group = app_commands.Group(name="log", description="Configure server logging")
    log_filter = app_commands.Group(name="logfilter", description="Manage user-based log filtering")

    @log_group.command(name="setup", description="Set the channel for log messages")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="The channel to send logs to")
    async def log_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await Config.set_guild_setting(interaction.guild_id, "log_channel_id", channel.id)
        await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

    @log_group.command(name="disable", description="Disable logging")
    @app_commands.checks.has_permissions(administrator=True)
    async def log_disable(self, interaction: discord.Interaction):
        await Config.set_guild_setting(interaction.guild_id, "log_channel_id", None)
        await interaction.response.send_message("✅ Logging disabled", ephemeral=True)

    @log_group.command(name="status", description="View current logging configuration")
    @app_commands.checks.has_permissions(administrator=True)
    async def log_status(self, interaction: discord.Interaction):
        channel = await self._get_log_channel(interaction.guild)
        filters = await self._get_filtered_users(interaction.guild_id)

        embed = discord.Embed(title="📋 Logger Status", color=discord.Color.blue())
        embed.add_field(name="Log Channel", value=channel.mention if channel else "Not configured", inline=False)
        embed.add_field(name="Module Enabled", value="✅ Yes" if await self._is_module_enabled(interaction.guild_id) else "❌ No", inline=True)

        if filters:
            user_mentions = []
            for uid in filters:
                member = interaction.guild.get_member(uid)
                user_mentions.append(member.mention if member else f"<@{uid}>")
            embed.add_field(name="Tracked Users", value=", ".join(user_mentions), inline=False)
        else:
            embed.add_field(name="Tracked Users", value="All users (no filter)", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @log_filter.command(name="add", description="Add a user to the tracking filter")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="User to track")
    async def filter_add(self, interaction: discord.Interaction, user: discord.Member):
        filters = await self._get_filtered_users(interaction.guild_id)
        if user.id not in filters:
            filters.append(user.id)
            await Config.set_guild_setting(interaction.guild_id, "log_filtered_users", filters)
            await interaction.response.send_message(f"✅ Now tracking {user.mention}", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {user.mention} is already being tracked", ephemeral=True)

    @log_filter.command(name="remove", description="Remove a user from the tracking filter")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="User to stop tracking")
    async def filter_remove(self, interaction: discord.Interaction, user: discord.Member):
        filters = await self._get_filtered_users(interaction.guild_id)
        if user.id in filters:
            filters.remove(user.id)
            await Config.set_guild_setting(interaction.guild_id, "log_filtered_users", filters)
            await interaction.response.send_message(f"✅ Stopped tracking {user.mention}", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {user.mention} is not being tracked", ephemeral=True)

    @log_filter.command(name="list", description="List all tracked users")
    @app_commands.checks.has_permissions(administrator=True)
    async def filter_list(self, interaction: discord.Interaction):
        filters = await self._get_filtered_users(interaction.guild_id)
        if not filters:
            await interaction.response.send_message("ℹ️ No user filter active — logging all users", ephemeral=True)
            return

        user_mentions = []
        for uid in filters:
            member = interaction.guild.get_member(uid)
            user_mentions.append(member.mention if member else f"<@{uid}>")

        embed = discord.Embed(title="👤 Tracked Users", description="\n".join(user_mentions), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @log_filter.command(name="clear", description="Clear all user filters (log everyone)")
    @app_commands.checks.has_permissions(administrator=True)
    async def filter_clear(self, interaction: discord.Interaction):
        await Config.set_guild_setting(interaction.guild_id, "log_filtered_users", [])
        await interaction.response.send_message("✅ All filters cleared — logging all users", ephemeral=True)

    # ──── Message Events ────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not await self._is_module_enabled(message.guild.id):
            return
        if not await self._should_log_user(message.guild.id, message.author.id):
            return

        # Find who deleted the message
        deleter = None
        entry = await self._get_audit_entry(message.guild, discord.AuditLogAction.message_delete, message.author)
        if entry and entry.user.id != message.author.id:
            deleter = entry.user

        embed = EmbedBuilder.create(
            title="🗑️ Message Deleted",
            color_key="log_message_delete",
            author=message.author,
            footer=f"Channel: #{message.channel.name} | Message ID: {message.id}"
        )
        embed.add_field(name="Author", value=message.author.mention, inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)

        if deleter:
            embed.add_field(name="Deleted By", value=deleter.mention, inline=True)
        else:
            embed.add_field(name="Deleted By", value="Self or unknown", inline=True)

        content = message.content or "*No text content*"
        if len(content) > 1024:
            content = content[:1021] + "..."
        embed.add_field(name="Content", value=content, inline=False)

        if message.attachments:
            att_text = "\n".join(a.filename for a in message.attachments)
            embed.add_field(name="Attachments", value=att_text, inline=False)

        await self._send_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild:
            return
        if before.content == after.content:
            return
        if not await self._is_module_enabled(before.guild.id):
            return
        if not await self._should_log_user(before.guild.id, before.author.id):
            return

        embed = EmbedBuilder.create(
            title="✏️ Message Edited",
            color_key="log_message_edit",
            author=before.author,
            footer=f"Channel: #{before.channel.name} | Message ID: {before.id}"
        )
        embed.add_field(name="Author", value=before.author.mention, inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({after.jump_url})", inline=True)

        old_content = before.content or "*Empty*"
        new_content = after.content or "*Empty*"
        if len(old_content) > 1024:
            old_content = old_content[:1021] + "..."
        if len(new_content) > 1024:
            new_content = new_content[:1021] + "..."

        embed.add_field(name="Before", value=old_content, inline=False)
        embed.add_field(name="After", value=new_content, inline=False)

        await self._send_log(before.guild, embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        if not await self._is_module_enabled(guild.id):
            return

        # Try to find who performed the bulk delete
        entry = await self._get_audit_entry(guild, discord.AuditLogAction.message_bulk_delete)
        performer = entry.user if entry else None

        embed = EmbedBuilder.create(
            title="🗑️ Bulk Message Delete",
            color_key="log_message_delete",
            footer=f"Channel: #{messages[0].channel.name}"
        )
        embed.add_field(name="Messages Deleted", value=str(len(messages)), inline=True)
        embed.add_field(name="Channel", value=messages[0].channel.mention, inline=True)
        if performer:
            embed.add_field(name="Deleted By", value=performer.mention, inline=True)

        # Log first few message authors
        authors = set(m.author.name for m in messages if not m.author.bot)
        if authors:
            embed.add_field(name="Authors", value=", ".join(list(authors)[:10]), inline=False)

        await self._send_log(guild, embed)

    # ──── Voice Events ────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or not member.guild:
            return
        if not await self._is_module_enabled(member.guild.id):
            return
        if not await self._should_log_user(member.guild.id, member.id):
            return

        guild = member.guild

        # Join voice channel
        if before.channel is None and after.channel is not None:
            embed = EmbedBuilder.create(
                title="🔊 Voice Channel Join",
                color_key="log_voice",
                author=member
            )
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Channel", value=after.channel.mention, inline=True)
            await self._send_log(guild, embed)

        # Leave voice channel
        elif before.channel is not None and after.channel is None:
            embed = EmbedBuilder.create(
                title="🔇 Voice Channel Leave",
                color_key="log_voice",
                author=member
            )
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Channel", value=before.channel.mention, inline=True)

            # Check if disconnected by someone else
            entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_disconnect, member)
            if entry:
                embed.add_field(name="Disconnected By", value=entry.user.mention, inline=True)

            await self._send_log(guild, embed)

        # Move between channels
        elif before.channel != after.channel:
            embed = EmbedBuilder.create(
                title="🔀 Voice Channel Move",
                color_key="log_voice",
                author=member
            )
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="From", value=before.channel.mention if before.channel else "Unknown", inline=True)
            embed.add_field(name="To", value=after.channel.mention if after.channel else "Unknown", inline=True)

            entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_move, member)
            if entry:
                embed.add_field(name="Moved By", value=entry.user.mention, inline=True)

            await self._send_log(guild, embed)

        # Server mute/deafen changes
        if before.self_mute != after.self_mute or before.mute != after.mute:
            is_server_action = before.mute != after.mute
            muted = after.mute if is_server_action else after.self_mute

            embed = EmbedBuilder.create(
                title=f"{'🔇' if muted else '🔊'} {'Server' if is_server_action else 'Self'} {'Muted' if muted else 'Unmuted'}",
                color_key="log_voice",
                author=member
            )
            embed.add_field(name="User", value=member.mention, inline=True)

            if is_server_action:
                entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_update, member)
                if entry:
                    embed.add_field(name="Action By", value=entry.user.mention, inline=True)

            await self._send_log(guild, embed)

        if before.self_deaf != after.self_deaf or before.deaf != after.deaf:
            is_server_action = before.deaf != after.deaf
            deafened = after.deaf if is_server_action else after.self_deaf

            embed = EmbedBuilder.create(
                title=f"{'🔇' if deafened else '🔊'} {'Server' if is_server_action else 'Self'} {'Deafened' if deafened else 'Undeafened'}",
                color_key="log_voice",
                author=member
            )
            embed.add_field(name="User", value=member.mention, inline=True)

            if is_server_action:
                entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_update, member)
                if entry:
                    embed.add_field(name="Action By", value=entry.user.mention, inline=True)

            await self._send_log(guild, embed)

    # ──── Member Events ────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not await self._is_module_enabled(member.guild.id):
            return
        if not await self._should_log_user(member.guild.id, member.id):
            return

        embed = EmbedBuilder.create(
            title="📥 Member Joined",
            color_key="log_member_join",
            author=member,
            thumbnail=member.display_avatar.url if member.display_avatar else None
        )
        embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)

        # Try to find which invite was used
        # This requires tracking invites before/after, which is complex
        # For now just log the join

        await self._send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not await self._is_module_enabled(member.guild.id):
            return
        if not await self._should_log_user(member.guild.id, member.id):
            return

        guild = member.guild

        # Check if it was a kick
        entry = await self._get_audit_entry(guild, discord.AuditLogAction.kick, member)
        if entry:
            embed = EmbedBuilder.create(
                title="👢 Member Kicked",
                color_key="log_member_leave",
                author=member,
                thumbnail=member.display_avatar.url if member.display_avatar else None
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Kicked By", value=entry.user.mention, inline=True)
            if entry.reason:
                embed.add_field(name="Reason", value=entry.reason, inline=False)
        else:
            embed = EmbedBuilder.create(
                title="📤 Member Left",
                color_key="log_member_leave",
                author=member,
                thumbnail=member.display_avatar.url if member.display_avatar else None
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
            embed.add_field(name="Roles", value=", ".join(r.mention for r in member.roles[1:]) or "None", inline=False)

        embed.add_field(name="Member Count", value=str(guild.member_count), inline=True)
        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not await self._is_module_enabled(guild.id):
            return

        entry = await self._get_audit_entry(guild, discord.AuditLogAction.ban, user)

        embed = EmbedBuilder.create(
            title="🔨 Member Banned",
            color_key="log_member_ban",
            thumbnail=user.display_avatar.url if user.display_avatar else None
        )
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=True)

        if entry:
            embed.add_field(name="Banned By", value=entry.user.mention, inline=True)
            if entry.reason:
                embed.add_field(name="Reason", value=entry.reason, inline=False)

        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not await self._is_module_enabled(guild.id):
            return

        entry = await self._get_audit_entry(guild, discord.AuditLogAction.unban, user)

        embed = EmbedBuilder.create(
            title="🔓 Member Unbanned",
            color_key="log_member_join",
            thumbnail=user.display_avatar.url if user.display_avatar else None
        )
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=True)

        if entry:
            embed.add_field(name="Unbanned By", value=entry.user.mention, inline=True)

        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.bot or not before.guild:
            return
        if not await self._is_module_enabled(before.guild.id):
            return
        if not await self._should_log_user(before.guild.id, before.id):
            return

        guild = before.guild

        # Nickname change
        if before.nick != after.nick:
            entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_update, before)

            embed = EmbedBuilder.create(
                title="📝 Nickname Changed",
                color_key="log_channel",
                author=after
            )
            embed.add_field(name="User", value=after.mention, inline=True)
            embed.add_field(name="Before", value=before.nick or "*None*", inline=True)
            embed.add_field(name="After", value=after.nick or "*None*", inline=True)

            if entry and entry.user.id != after.id:
                embed.add_field(name="Changed By", value=entry.user.mention, inline=True)
            else:
                embed.add_field(name="Changed By", value="Self", inline=True)

            await self._send_log(guild, embed)

        # Role changes
        if before.roles != after.roles:
            added_roles = set(after.roles) - set(before.roles)
            removed_roles = set(before.roles) - set(after.roles)

            entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_role_update, before)

            if added_roles:
                embed = EmbedBuilder.create(
                    title="➕ Role Added",
                    color_key="log_role",
                    author=after
                )
                embed.add_field(name="User", value=after.mention, inline=True)
                embed.add_field(name="Roles Added", value=", ".join(r.mention for r in added_roles), inline=True)
                if entry:
                    embed.add_field(name="Added By", value=entry.user.mention, inline=True)
                await self._send_log(guild, embed)

            if removed_roles:
                embed = EmbedBuilder.create(
                    title="➖ Role Removed",
                    color_key="log_role",
                    author=after
                )
                embed.add_field(name="User", value=after.mention, inline=True)
                embed.add_field(name="Roles Removed", value=", ".join(r.mention for r in removed_roles), inline=True)
                if entry:
                    embed.add_field(name="Removed By", value=entry.user.mention, inline=True)
                await self._send_log(guild, embed)

        # Timeout changes
        if before.timed_out_until != after.timed_out_until:
            entry = await self._get_audit_entry(guild, discord.AuditLogAction.member_update, before)

            if after.timed_out_until and after.timed_out_until > datetime.utcnow().astimezone():
                embed = EmbedBuilder.create(
                    title="⏰ Member Timed Out",
                    color_key="log_member_ban",
                    author=after
                )
                embed.add_field(name="User", value=after.mention, inline=True)
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "F"), inline=True)
            else:
                embed = EmbedBuilder.create(
                    title="✅ Timeout Removed",
                    color_key="log_member_join",
                    author=after
                )
                embed.add_field(name="User", value=after.mention, inline=True)

            if entry:
                embed.add_field(name="Action By", value=entry.user.mention, inline=True)
                if entry.reason:
                    embed.add_field(name="Reason", value=entry.reason, inline=False)

            await self._send_log(guild, embed)

    # ──── Channel Events ────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not await self._is_module_enabled(channel.guild.id):
            return

        entry = await self._get_audit_entry(channel.guild, discord.AuditLogAction.channel_create, channel)

        embed = EmbedBuilder.create(
            title="📁 Channel Created",
            color_key="log_channel"
        )
        embed.add_field(name="Channel", value=f"{channel.mention} ({channel.name})", inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)

        if entry:
            embed.add_field(name="Created By", value=entry.user.mention, inline=True)

        await self._send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not await self._is_module_enabled(channel.guild.id):
            return

        entry = await self._get_audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel)

        embed = EmbedBuilder.create(
            title="🗑️ Channel Deleted",
            color_key="log_message_delete"
        )
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)

        if entry:
            embed.add_field(name="Deleted By", value=entry.user.mention, inline=True)

        await self._send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if not await self._is_module_enabled(before.guild.id):
            return

        entry = await self._get_audit_entry(before.guild, discord.AuditLogAction.channel_update, before)

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")

        if hasattr(before, 'topic') and hasattr(after, 'topic'):
            if before.topic != after.topic:
                changes.append(f"**Topic:** {before.topic or '*None*'} → {after.topic or '*None*'}")

        if hasattr(before, 'slowmode_delay') and hasattr(after, 'slowmode_delay'):
            if before.slowmode_delay != after.slowmode_delay:
                changes.append(f"**Slowmode:** {before.slowmode_delay}s → {after.slowmode_delay}s")

        if hasattr(before, 'nsfw') and hasattr(after, 'nsfw'):
            if before.nsfw != after.nsfw:
                changes.append(f"**NSFW:** {before.nsfw} → {after.nsfw}")

        if not changes:
            return  # Permission-only changes or other non-visible changes

        embed = EmbedBuilder.create(
            title="✏️ Channel Updated",
            color_key="log_channel"
        )
        embed.add_field(name="Channel", value=after.mention, inline=True)
        if entry:
            embed.add_field(name="Updated By", value=entry.user.mention, inline=True)
        embed.add_field(name="Changes", value="\n".join(changes), inline=False)

        await self._send_log(before.guild, embed)

    # ──── Role Events ────

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if not await self._is_module_enabled(role.guild.id):
            return

        entry = await self._get_audit_entry(role.guild, discord.AuditLogAction.role_create, role)

        embed = EmbedBuilder.create(
            title="🏷️ Role Created",
            color_key="log_role"
        )
        embed.add_field(name="Role", value=f"{role.mention} ({role.name})", inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)

        if entry:
            embed.add_field(name="Created By", value=entry.user.mention, inline=True)

        await self._send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if not await self._is_module_enabled(role.guild.id):
            return

        entry = await self._get_audit_entry(role.guild, discord.AuditLogAction.role_delete, role)

        embed = EmbedBuilder.create(
            title="🗑️ Role Deleted",
            color_key="log_message_delete"
        )
        embed.add_field(name="Role", value=role.name, inline=True)

        if entry:
            embed.add_field(name="Deleted By", value=entry.user.mention, inline=True)

        await self._send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if not await self._is_module_enabled(before.guild.id):
            return

        entry = await self._get_audit_entry(before.guild, discord.AuditLogAction.role_update, before)

        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.color != after.color:
            changes.append(f"**Color:** {before.color} → {after.color}")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
        if before.permissions != after.permissions:
            added = after.permissions.value & ~before.permissions.value
            removed = before.permissions.value & ~after.permissions.value
            if added:
                perms = discord.Permissions(added)
                changes.append(f"**Permissions Added:** {', '.join(p for p, v in perms if v)}")
            if removed:
                perms = discord.Permissions(removed)
                changes.append(f"**Permissions Removed:** {', '.join(p for p, v in perms if v)}")

        if not changes:
            return

        embed = EmbedBuilder.create(
            title="✏️ Role Updated",
            color_key="log_role"
        )
        embed.add_field(name="Role", value=after.mention, inline=True)
        if entry:
            embed.add_field(name="Updated By", value=entry.user.mention, inline=True)
        embed.add_field(name="Changes", value="\n".join(changes), inline=False)

        await self._send_log(before.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logger(bot))
