import discord
from discord.ext import commands
from config import Config


def is_administrator(ctx: commands.Context) -> bool:
    """Checks if the invoking member has Administrator permissions, is the server owner, or bot owner."""
    if not ctx.guild:
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    bot_owner_id = getattr(ctx.bot, "owner_id", None)
    if bot_owner_id and ctx.author.id == bot_owner_id:
        return True
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(perms and perms.administrator)


class Settings(commands.Cog):
    """Server settings and module management via Discord prefix commands (!command). Restricted to Administrators."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return True
        allowed = await Config.check_member_cog_permission(ctx.author, "settings")
        if not allowed:
            await ctx.send("❌ You do not have permission to use the **Settings** module on this server.")
            return False
        return True

    # ─── Settings Overview (!settings) ─────────────────────────────

    @commands.group(name="settings", invoke_without_command=True)
    async def cmd_settings(self, ctx: commands.Context):
        """View all active settings and module configurations for this server."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        guild = ctx.guild
        guild_id = ctx.guild.id if ctx.guild else 0

        embed = discord.Embed(
            title=f"⚙️ Server Settings — {guild.name if guild else 'Direct Messages'}",
            color=discord.Color.blue()
        )

        # 1. Bot Prefix
        prefix = await Config.get_prefix(guild_id) if guild_id else "!"
        embed.add_field(name="⌨️ Command Prefix", value=f"`{prefix}`", inline=True)

        # 2. Module Statuses
        modules = ["music", "logger", "loa", "announcement"]
        mod_status = []
        for mod in modules:
            enabled = await Config.is_module_enabled(guild_id, mod) if guild_id else True
            status_emoji = "🟢" if enabled else "🔴"
            display_name = "LOA" if mod == "loa" else mod.capitalize()
            mod_status.append(f"{status_emoji} **{display_name}**: {'Enabled' if enabled else 'Disabled'}")
        embed.add_field(name="📦 Modules", value="\n".join(mod_status), inline=False)

        # 3. Announcement Allowed Roles
        if guild:
            ann_roles = await Config.get_announcement_roles(guild_id)
            if ann_roles:
                role_str = ", ".join(f"<@&{r}>" for r in ann_roles)
            else:
                role_str = "*Administrators only (Use `/announcement_roles add`)*"
            embed.add_field(name="📢 Announcement Roles", value=role_str, inline=False)

        # 4. Logger Channel
        if guild:
            log_channel_id = await Config.get_guild_setting(guild_id, "log_channel_id")
            log_ch = guild.get_channel(log_channel_id) if log_channel_id else None
            embed.add_field(
                name="📋 Server Logger Channel",
                value=log_ch.mention if log_ch else "*Not set (Use `/log setup`)*",
                inline=True
            )

        # 5. LOA System Status (from loa cog if available)
        loa_cog = self.bot.get_cog("Loa")
        if loa_cog and hasattr(loa_cog, "get_settings") and guild:
            loa_data = await loa_cog.get_settings(guild_id)
            if loa_data:
                dash_ch = guild.get_channel(loa_data.get("dashboard_channel_id", 0))
                rev_ch = guild.get_channel(loa_data.get("review_channel_id", 0))
                tz_off = loa_data.get("timezone_offset", 6)
                loa_summary = (
                    f"• Dashboard: {dash_ch.mention if dash_ch else '*Not set*'}\n"
                    f"• Review Channel: {rev_ch.mention if rev_ch else '*Not set*'}\n"
                    f"• Timezone: UTC+{tz_off} (Use `/loa_config` to edit)"
                )
            else:
                loa_summary = "*Not set up (Run `/loa_setup`)*"
            embed.add_field(name="🌴 LOA System", value=loa_summary, inline=False)

        # 6. Additional Custom Settings
        if guild:
            all_settings = await Config.get_all_guild_settings(guild_id)
            known_keys = {
                "prefix", "announcement_roles", "log_channel_id", "loa_timezone_offset",
                "module_music_enabled", "module_logger_enabled", "module_loa_enabled", "module_announcement_enabled"
            }
            extra_keys = {k: v for k, v in all_settings.items() if k not in known_keys}
            if extra_keys:
                extra_lines = [f"• `{k}`: `{v}`" for k, v in extra_keys.items()]
                embed.add_field(name="🔧 Custom Configs", value="\n".join(extra_lines), inline=False)

        embed.set_footer(text=f"Commands: {prefix}prefix <new> | {prefix}cogs | {prefix}sync | {prefix}module <enable|disable> <name>")
        try:
            await ctx.send(embed=embed)
        except Exception:
            plain_lines = [
                f"⚙️ **Server Settings — {guild.name if guild else 'DM'}**",
                f"Prefix: `{prefix}`",
                "Modules: " + ", ".join(mod_status),
                f"Use {prefix}cogs to view extensions."
            ]
            await ctx.send("\n".join(plain_lines))

    @cmd_settings.command(name="view")
    async def settings_view_subcmd(self, ctx: commands.Context):
        """View server settings."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return
        await self.cmd_settings(ctx)

    # ─── Prefix Command (!prefix) ──────────────────────────────────

    @commands.command(name="prefix")
    async def cmd_prefix(self, ctx: commands.Context, new_prefix: str = None):
        """View or change bot command prefix (!prefix [new_prefix]). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        if not ctx.guild:
            await ctx.send("❌ Prefix cannot be changed in DMs.")
            return

        curr = await Config.get_prefix(ctx.guild.id)
        if not new_prefix:
            await ctx.send(f"Current bot prefix for **{ctx.guild.name}** is `{curr}`. Use `{curr}prefix <new>` to change.")
            return

        clean = new_prefix.strip()
        if len(clean) > 5:
            await ctx.send("❌ Prefix must be between 1 and 5 characters.")
            return
        await Config.set_prefix(ctx.guild.id, clean)
        await ctx.send(f"✅ Bot prefix updated to `{clean}` for **{ctx.guild.name}**.")

    # ─── Modules Management (!modules & !module) ───────────────────

    @commands.command(name="modules")
    async def cmd_modules(self, ctx: commands.Context):
        """List active modules and status (!modules). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        prefix = await Config.get_prefix(ctx.guild.id) if ctx.guild else "!"
        available_modules = ["music", "logger", "loa", "announcement"]
        embed = discord.Embed(title="🧩 Bot Modules Status", color=discord.Color.green())
        for mod_name in available_modules:
            enabled = await Config.is_module_enabled(ctx.guild.id, mod_name) if ctx.guild else True
            status = "🟢 Enabled" if enabled else "🔴 Disabled"
            display_name = "LOA (Leave of Absence)" if mod_name == "loa" else mod_name.capitalize()
            embed.add_field(name=display_name, value=status, inline=True)
        embed.set_footer(text=f"Toggle modules: {prefix}module enable <name> or {prefix}module disable <name>")
        try:
            await ctx.send(embed=embed)
        except Exception:
            lines = [f"• {m.capitalize()}: {'Enabled' if (await Config.is_module_enabled(ctx.guild.id, m) if ctx.guild else True) else 'Disabled'}" for m in available_modules]
            await ctx.send("🧩 **Bot Modules Status:**\n" + "\n".join(lines))

    @commands.group(name="module", invoke_without_command=True)
    async def cmd_module(self, ctx: commands.Context):
        """Manage modules: !module list, !module enable <name>, !module disable <name>"""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return
        await self.cmd_modules(ctx)

    @cmd_module.command(name="list")
    async def module_list_sub(self, ctx: commands.Context):
        """List all modules and status."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return
        await self.cmd_modules(ctx)

    @cmd_module.command(name="enable")
    async def module_enable_sub(self, ctx: commands.Context, module_name: str):
        """Enable a module (!module enable <music|logger|loa|announcement>)."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        valid_mods = {"music": "Music", "logger": "Logger", "loa": "LOA", "announcement": "Announcement"}
        key = module_name.lower().strip()
        if key not in valid_mods:
            await ctx.send(f"❌ Invalid module `{module_name}`. Available: `{'`, `'.join(valid_mods.keys())}`")
            return
        await Config.set_module_enabled(ctx.guild.id, key, True)
        await ctx.send(f"✅ Module `{valid_mods[key]}` is now **enabled** for **{ctx.guild.name}**.")

    @cmd_module.command(name="disable")
    async def module_disable_sub(self, ctx: commands.Context, module_name: str):
        """Disable a module (!module disable <music|logger|loa|announcement>)."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        valid_mods = {"music": "Music", "logger": "Logger", "loa": "LOA", "announcement": "Announcement"}
        key = module_name.lower().strip()
        if key not in valid_mods:
            await ctx.send(f"❌ Invalid module `{module_name}`. Available: `{'`, `'.join(valid_mods.keys())}`")
            return
        await Config.set_module_enabled(ctx.guild.id, key, False)
        await ctx.send(f"⚠️ Module `{valid_mods[key]}` is now **disabled** for **{ctx.guild.name}**.")

    # ─── Cogs & Extensions Commands (!cogs & !reload) ──────────────

    @commands.command(name="cogs")
    async def cmd_cogs(self, ctx: commands.Context):
        """Lists all loaded extensions and cogs (!cogs). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        print(f"[COGS_CMD] !cogs invoked by {ctx.author} in #{getattr(ctx.channel, 'name', 'DM')}")
        loaded = sorted(list(self.bot.extensions.keys()))
        lines = []
        for ext in loaded:
            lines.append(f"🟢 `{ext}`")
        prefix = await Config.get_prefix(ctx.guild.id) if ctx.guild else "!"
        embed = discord.Embed(
            title="⚙️ Loaded Bot Extensions & Cogs",
            description="\n".join(lines) or "No extensions loaded.",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Total: {len(loaded)} extensions active | Use {prefix}reload <name> to refresh")
        try:
            await ctx.send(embed=embed)
        except Exception:
            plain_text = f"⚙️ **Loaded Bot Extensions & Cogs ({len(loaded)} active):**\n" + "\n".join(lines)
            await ctx.send(plain_text)

    @commands.command(name="reload")
    async def cmd_reload(self, ctx: commands.Context, cog_name: str):
        """Reload a cog on the fly (e.g. !reload settings, !reload music). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        target = cog_name.strip()
        if not target.startswith("cogs."):
            target = f"cogs.{target}"
        try:
            await self.bot.reload_extension(target)
            await ctx.send(f"✅ Successfully reloaded `{target}`!")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{target}`: `{e}`")

    # ─── Instant Guild Command Syncing (!sync) ─────────────────────

    @commands.command(name="sync")
    async def cmd_sync(self, ctx: commands.Context):
        """Instantly syncs slash commands directly to this server guild (!sync). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        if not ctx.guild:
            await ctx.send("❌ Slash commands cannot be synced to DMs.")
            return

        async with ctx.typing():
            try:
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(
                    f"✅ **Instantly synced {len(synced)} slash commands directly to {ctx.guild.name}!**\n"
                    f"Slash commands (/announcement, /loa_*, /log, /play, /arc, etc.) are now live."
                )
            except Exception as e:
                await ctx.send(f"❌ Failed to sync slash commands: `{e}`")

    # ─── Advanced Key-Value Settings (!set & !reset) ───────────────

    @commands.command(name="set")
    async def cmd_set(self, ctx: commands.Context, key: str, value: str):
        """Update an advanced setting key-value pair (!set <key> <value>). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        await Config.set_guild_setting(ctx.guild.id, key.strip(), value.strip())
        await ctx.send(f"✅ Setting `{key}` updated to `{value}`.")

    @commands.command(name="reset")
    async def cmd_reset(self, ctx: commands.Context, key: str):
        """Reset a setting to default (!reset <key>). Restricted to Administrators."""
        if not is_administrator(ctx):
            await ctx.send("❌ You need **Administrator** permission to use Settings commands.")
            return

        await Config.delete_guild_setting(ctx.guild.id, key.strip())
        await ctx.send(f"✅ Setting `{key}` reset to default.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
