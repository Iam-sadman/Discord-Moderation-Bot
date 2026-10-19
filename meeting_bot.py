"""
Meeting Assistant Bot — Dedicated Secondary Discord Bot Instance.
Runs concurrently with the primary ModularBot in the same process.
Handles voice meeting/training recording so music playback and
meeting recording NEVER conflict in the same server.
"""

import os
import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from config import Config

logger = logging.getLogger("MeetingBot")


class MeetingAssistantBot(commands.Bot):
    """
    Secondary Discord Bot instance dedicated to meeting/training recording.
    Uses a separate MEETING_BOT_TOKEN so it can independently join voice channels
    without conflicting with the primary bot's music playback.
    """

    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix="!mtg ",
            intents=intents,
            chunk_guilds_at_startup=True,
            help_command=None
        )

    async def setup_hook(self):
        # Initialize meetings database
        from services.meetings_db import MeetingsDB
        db = MeetingsDB.get_instance()
        await db.init_db()

        # Load meetings cog
        try:
            await self.load_extension("cogs.meetings")
            logger.info("[OK] MeetingBot: cogs.meetings loaded")
        except Exception as e:
            logger.error(f"[ERROR] MeetingBot: Failed to load cogs.meetings: {e}\n{traceback.format_exc()}")

        # Set up global slash command error handler
        async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CommandOnCooldown):
                msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
            elif isinstance(error, app_commands.CheckFailure):
                msg = "❌ You do not have permission to execute this command."
            else:
                msg = f"❌ An error occurred: `{error}`"

            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        self.tree.on_error = on_tree_error

        # Sync slash commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"[OK] MeetingBot: Synced {len(synced)} slash commands globally")
        except Exception as e:
            logger.error(f"[ERROR] MeetingBot: Failed to sync commands: {e}")

    async def on_ready(self):
        logger.info("=" * 60)
        logger.info("Meeting Assistant Bot is ready!")
        logger.info(f"Logged in as: {self.user} (ID: {self.user.id})")
        logger.info(f"Connected Guilds: {len(self.guilds)}")
        logger.info(f"Loaded Extensions: {list(self.extensions.keys())}")
        logger.info("=" * 60)
