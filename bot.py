import asyncio
import logging
import os
import sys
import traceback
import discord
from discord import app_commands
from discord.ext import commands
from config import Config

# Ensure UTF-8 output encoding where supported
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Configure dual logging: console + file (data/bot.log)
log_dir = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8")
    ]
)
logger = logging.getLogger("ModularBot")


async def get_prefix(bot: commands.Bot, message: discord.Message):
    """
    Dynamic per-guild command prefix with fallback.
    Safely resolves prefix even if Config.get_prefix is missing on older configs.
    """
    guild_id = message.guild.id if message.guild else None
    guild_prefix = "!"
    if hasattr(Config, "get_prefix") and callable(getattr(Config, "get_prefix")):
        try:
            guild_prefix = await Config.get_prefix(guild_id)
        except Exception:
            guild_prefix = getattr(Config, "PREFIX", "!")
    else:
        guild_prefix = getattr(Config, "PREFIX", "!")

    if not guild_prefix:
        guild_prefix = "!"

    # Multi-prefix fallback guarantees '!' always works
    prefixes = [guild_prefix]
    if "!" not in prefixes:
        prefixes.append("!")

    if bot.user:
        return commands.when_mentioned_or(*prefixes)(bot, message)
    return prefixes


class ModularBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=commands.DefaultHelpCommand()
        )

    async def setup_hook(self):
        if hasattr(Config, "init_db"):
            try:
                await Config.init_db()
            except Exception as e:
                logger.warning(f"[!] Config.init_db notice: {e}")

        # Set up global slash command error handler
        async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CommandOnCooldown):
                msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
            elif isinstance(error, app_commands.CheckFailure):
                msg = "❌ You do not have permission to execute this command."
            else:
                msg = f"❌ An error occurred while executing the command: `{error}`"

            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        self.tree.on_error = on_tree_error

        # Load all cogs from the cogs directory
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        legacy_music_cogs = {"directstream", "localmusic", "usermusic"}
        if os.path.exists(cogs_dir):
            for filename in sorted(os.listdir(cogs_dir)):
                if filename.endswith(".py") and not filename.startswith("_"):
                    if filename[:-3] in legacy_music_cogs:
                        continue
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(cog_name)
                        logger.info(f"[OK] Cog loaded: {cog_name}")
                    except Exception as e:
                        logger.error(f"[ERROR] Failed to load cog {cog_name}: {e}\n{traceback.format_exc()}")

        # Sync slash commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"[OK] Synced {len(synced)} slash commands globally")
        except Exception as e:
            logger.error(f"[ERROR] Failed to sync commands: {e}")

    async def on_message(self, message: discord.Message):
        """Processes prefix commands with diagnostic telemetry."""
        if message.author.bot:
            return

        ch_name = getattr(message.channel, "name", "DM")
        g_name = getattr(message.guild, "name", "DM")

        if message.content:
            if message.content.startswith(("!", "?", "/", "$", ".", "<@")):
                logger.info(f"[MSG] Potential command from {message.author} in #{ch_name} ({g_name}): '{message.content}'")
        else:
            logger.warning(f"[WARN] Received message with EMPTY content from {message.author} in #{ch_name} ({g_name}). "
                           f"Ensure 'Message Content Intent' is enabled in Discord Developer Portal!")

        await self.process_commands(message)

    async def on_command(self, ctx: commands.Context):
        """Fires when a command is recognized and starts executing."""
        logger.info(f"[CMD] Executing '{ctx.prefix}{ctx.command.name}' requested by {ctx.author} in #{getattr(ctx.channel, 'name', 'DM')}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Global handler for prefix command errors with console and file telemetry."""
        logger.warning(f"[CMD_ERROR] '{ctx.message.content}' by {ctx.author}: {type(error).__name__} - {error}")

        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(f"❌ You do not have permission to use `{ctx.prefix}{ctx.command.name}`.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("❌ This command is restricted to the bot owner.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("❌ You do not meet the requirements to run this command here.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing required argument: `{error.param.name}`. Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
        else:
            await ctx.send(f"❌ Error: `{error}`")

    async def on_ready(self):
        logger.info("=" * 60)
        logger.info("Bot is ready and connected to Discord Gateway!")
        logger.info(f"Logged in as: {self.user} (ID: {self.user.id})")
        logger.info(f"Connected Guilds: {len(self.guilds)}")
        logger.info(f"Loaded Extensions: {list(self.extensions.keys())}")
        logger.info(f"Active Prefix Commands: {[c.name for c in self.commands]}")
        logger.info(f"Message Content Intent: {'ENABLED' if self.intents.message_content else 'DISABLED'}")

        # Print prefix per guild and sync slash commands
        for guild in self.guilds:
            p = "!"
            if hasattr(Config, "get_prefix") and callable(getattr(Config, "get_prefix")):
                try:
                    p = await Config.get_prefix(guild.id)
                except Exception:
                    p = getattr(Config, "PREFIX", "!")
            else:
                p = getattr(Config, "PREFIX", "!")

            logger.info(f" -> Guild: '{guild.name}' ({guild.id}) | Prefix: '{p}' (Fallback '!' also active)")
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"    [OK] Synced {len(synced)} slash commands directly to: {guild.name}")
            except Exception as e:
                logger.warning(f"    [!] Notice: Guild sync for {guild.name}: {e}")

        logger.info("=" * 60)


async def main():
    bot = ModularBot()

    # Start web dashboard in background
    try:
        from dashboard.app import create_dashboard
        dashboard = create_dashboard(bot)

        async def run_dashboard():
            await dashboard.run_task(
                host=Config.WEB_HOST,
                port=Config.WEB_PORT,
                shutdown_trigger=lambda: asyncio.Future()
            )

        asyncio.ensure_future(run_dashboard())
        logger.info(f"[OK] Web dashboard starting on {Config.WEB_HOST}:{Config.WEB_PORT}")
    except Exception as e:
        logger.warning(f"[!] Dashboard not started: {e}")

    await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
