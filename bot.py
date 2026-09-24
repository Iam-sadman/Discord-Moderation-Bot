import asyncio
import os
import discord
from discord.ext import commands
from config import Config

class ModularBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix=commands.when_mentioned_or(Config.PREFIX),
            intents=intents,
            help_command=commands.DefaultHelpCommand()
        )

    async def setup_hook(self):
        await Config.init_db()
        # Load all cogs from the cogs directory
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        if os.path.exists(cogs_dir):
            for filename in os.listdir(cogs_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(cog_name)
                        print(f"[✓] Loaded cog: {cog_name}")
                    except Exception as e:
                        print(f"[✗] Failed to load cog {cog_name}: {e}")

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            print(f"[✓] Synced {len(synced)} slash commands")
        except Exception as e:
            print(f"[✗] Failed to sync commands: {e}")

    async def on_ready(self):
        print(f"{'='*50}")
        print(f"Bot is ready!")
        print(f"Logged in as: {self.user} (ID: {self.user.id})")
        print(f"Guilds: {len(self.guilds)}")
        print(f"{'='*50}")

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
        print(f"[✓] Web dashboard starting on {Config.WEB_HOST}:{Config.WEB_PORT}")
    except Exception as e:
        print(f"[!] Dashboard not started: {e}")

    await bot.start(Config.TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
