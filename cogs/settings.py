import discord
from discord import app_commands
from discord.ext import commands
from config import Config

class Settings(commands.Cog):
    """Server settings management via Discord commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    settings_group = app_commands.Group(name="settings", description="Manage bot settings")
    module_group = app_commands.Group(name="module", description="Manage bot modules")

    @settings_group.command(name="view", description="View all settings for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_view(self, interaction: discord.Interaction):
        settings = await Config.get_all_guild_settings(interaction.guild_id)
        if not settings:
            await interaction.response.send_message("No custom settings configured. Using defaults.", ephemeral=True)
            return

        embed = discord.Embed(title="Server Settings", color=discord.Color.blue())
        for key, value in settings.items():
            embed.add_field(name=key, value=f"`{value}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @settings_group.command(name="set", description="Update a setting")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(key="Setting name", value="Setting value")
    async def settings_set(self, interaction: discord.Interaction, key: str, value: str):
        await Config.set_guild_setting(interaction.guild_id, key, value)
        await interaction.response.send_message(f"Setting `{key}` updated to `{value}`", ephemeral=True)

    @settings_group.command(name="reset", description="Reset a setting to default")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(key="Setting name to reset")
    async def settings_reset(self, interaction: discord.Interaction, key: str):
        from aiosqlite import connect
        async with connect(Config.DB_PATH) as db:
            await db.execute(
                "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
                (interaction.guild_id, key)
            )
            await db.commit()
        await interaction.response.send_message(f"Setting `{key}` reset to default", ephemeral=True)

    @module_group.command(name="list", description="List all available modules")
    @app_commands.checks.has_permissions(administrator=True)
    async def module_list(self, interaction: discord.Interaction):
        available_modules = ["music", "logger"]
        embed = discord.Embed(title="Bot Modules", color=discord.Color.green())

        for mod_name in available_modules:
            enabled = await Config.get_guild_setting(interaction.guild_id, f"module_{mod_name}_enabled", True)
            status = "Enabled" if enabled else "Disabled"
            embed.add_field(name=mod_name.capitalize(), value=status, inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @module_group.command(name="enable", description="Enable a module")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(module_name="Module to enable")
    @app_commands.choices(module_name=[
        app_commands.Choice(name="Music", value="music"),
        app_commands.Choice(name="Logger", value="logger"),
    ])
    async def module_enable(self, interaction: discord.Interaction, module_name: app_commands.Choice[str]):
        await Config.set_guild_setting(interaction.guild_id, f"module_{module_name.value}_enabled", True)
        await interaction.response.send_message(f"Module `{module_name.name}` enabled", ephemeral=True)

    @module_group.command(name="disable", description="Disable a module")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(module_name="Module to disable")
    @app_commands.choices(module_name=[
        app_commands.Choice(name="Music", value="music"),
        app_commands.Choice(name="Logger", value="logger"),
    ])
    async def module_disable(self, interaction: discord.Interaction, module_name: app_commands.Choice[str]):
        await Config.set_guild_setting(interaction.guild_id, f"module_{module_name.value}_enabled", False)
        await interaction.response.send_message(f"Module `{module_name.name}` disabled", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
