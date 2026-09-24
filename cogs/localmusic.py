import os
import discord
from discord import app_commands
from discord.ext import commands


class LocalMusic(commands.Cog):
    """🎵 Local Music Player - Play audio files from server's music folder."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Music folder path - server e eta create korte hobe
        self.music_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "music")

        # Create music directory if it doesn't exist
        os.makedirs(self.music_dir, exist_ok=True)

    def _get_audio_files(self) -> list[str]:
        """Get all audio files from the music directory."""
        if not os.path.exists(self.music_dir):
            return []

        audio_extensions = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus')
        files = []

        for filename in os.listdir(self.music_dir):
            if filename.lower().endswith(audio_extensions):
                files.append(filename)

        return sorted(files)

    def _find_file(self, query: str) -> str | None:
        """Find a file by partial name match."""
        query = query.lower()
        files = self._get_audio_files()

        # Exact match first
        for f in files:
            if f.lower() == query or f.lower() == f"{query}.mp3":
                return os.path.join(self.music_dir, f)

        # Partial match
        for f in files:
            if query in f.lower():
                return os.path.join(self.music_dir, f)

        return None

    @app_commands.command(name="playlocal", description="Play a local audio file from server")
    @app_commands.describe(filename="File name (can be partial)")
    async def playlocal(self, interaction: discord.Interaction, filename: str):
        """Play a local file from the music directory."""
        filepath = self._find_file(filename)

        if not filepath:
            available = self._get_audio_files()
            if available:
                file_list = "\n".join(f"• `{f}`" for f in available[:10])
                if len(available) > 10:
                    file_list += f"\n... and {len(available) - 10} more"
                await interaction.response.send_message(
                    f"❌ File not found: `{filename}`\n\n**Available files:**\n{file_list}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ No audio files found in music folder.\n"
                    f"Upload files to: `{self.music_dir}`",
                    ephemeral=True
                )
            return

        # Use the music cog's play command with the full path
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.response.send_message("❌ Music module not loaded", ephemeral=True)
            return

        # Call the original play command with full path
        await interaction.response.defer()

        try:
            vc = await music_cog._ensure_voice(interaction)
            if not vc:
                return

            state = music_cog.get_state(interaction.guild_id)

            # Import Song class from music cog
            from cogs.music import Song

            song = Song.from_local(filepath, interaction.user)
            state.queue.append(song)

            embed = discord.Embed(title="🎵 Added Local File", color=discord.Color.purple())
            embed.add_field(name="File", value=os.path.basename(filepath), inline=False)
            embed.add_field(name="Path", value=f"`{filepath}`", inline=False)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.followup.send(embed=embed)

            if not state.is_playing:
                await music_cog._play_next(interaction.guild_id)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="musiclist", description="List all available local music files")
    async def musiclist(self, interaction: discord.Interaction):
        """List all audio files in the music directory."""
        files = self._get_audio_files()

        if not files:
            await interaction.response.send_message(
                f"📁 No audio files found.\n\n"
                f"**Upload location:** `{self.music_dir}`\n"
                f"**Supported formats:** MP3, WAV, OGG, M4A, FLAC, AAC, OPUS",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎵 Available Local Music Files",
            description=f"**Total files:** {len(files)}",
            color=discord.Color.purple()
        )

        # Show files in chunks
        file_list = []
        for i, filename in enumerate(files[:20], 1):
            size = os.path.getsize(os.path.join(self.music_dir, filename))
            size_mb = size / (1024 * 1024)
            file_list.append(f"`{i}.` **{filename}** ({size_mb:.1f} MB)")

        embed.add_field(
            name="Files",
            value="\n".join(file_list) if file_list else "No files",
            inline=False
        )

        if len(files) > 20:
            embed.add_field(
                name="Note",
                value=f"... and {len(files) - 20} more files",
                inline=False
            )

        embed.set_footer(text=f"Use /playlocal <filename> to play")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="musicinfo", description="Get information about the music folder")
    @app_commands.checks.has_permissions(administrator=True)
    async def musicinfo(self, interaction: discord.Interaction):
        """Show music folder information (admin only)."""
        files = self._get_audio_files()

        total_size = 0
        for filename in files:
            filepath = os.path.join(self.music_dir, filename)
            total_size += os.path.getsize(filepath)

        total_size_mb = total_size / (1024 * 1024)
        total_size_gb = total_size / (1024 * 1024 * 1024)

        embed = discord.Embed(title="📁 Music Folder Info", color=discord.Color.blue())
        embed.add_field(name="Location", value=f"`{self.music_dir}`", inline=False)
        embed.add_field(name="Total Files", value=str(len(files)), inline=True)

        if total_size_gb >= 1:
            embed.add_field(name="Total Size", value=f"{total_size_gb:.2f} GB", inline=True)
        else:
            embed.add_field(name="Total Size", value=f"{total_size_mb:.2f} MB", inline=True)

        embed.add_field(
            name="Upload Instructions (SSH)",
            value=f"```bash\nscp your-file.mp3 user@server:{self.music_dir}/\n```",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LocalMusic(bot))
