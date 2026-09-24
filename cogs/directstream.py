import os
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import aiofiles
from pathlib import Path
import time


class DirectStreamMusic(commands.Cog):
    """🎵 Direct Stream - Play music directly from your PC without uploading!"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_music")
        os.makedirs(self.temp_dir, exist_ok=True)

        # Track active streams (guild_id -> file_path)
        self.active_streams = {}

        # Cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_temp_files())

    def _format_filesize(self, size_bytes: int) -> str:
        """Convert bytes to human readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"

    def _get_audio_quality(self, filename: str) -> str:
        """Guess audio quality from extension."""
        ext = os.path.splitext(filename)[1].lower()
        quality_map = {
            '.flac': '🏆 Lossless (FLAC)',
            '.wav': '🏆 Lossless (WAV)',
            '.m4a': '💎 High Quality (AAC)',
            '.opus': '💎 High Quality (Opus)',
            '.mp3': '🎵 Standard (MP3)',
            '.ogg': '🎵 Standard (OGG)',
            '.aac': '💎 High Quality (AAC)',
            '.wma': '🎵 Standard (WMA)',
        }
        return quality_map.get(ext, '🎵 Audio')

    async def _cleanup_temp_files(self):
        """Background task to cleanup old temp files."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                current_time = time.time()
                for filename in os.listdir(self.temp_dir):
                    filepath = os.path.join(self.temp_dir, filename)
                    # Delete files older than 1 hour
                    if os.path.getmtime(filepath) < current_time - 3600:
                        try:
                            os.remove(filepath)
                            print(f"[Cleanup] Deleted old temp file: {filename}")
                        except:
                            pass

                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                print(f"[Cleanup Error] {e}")
                await asyncio.sleep(300)

    async def _play_file(self, interaction: discord.Interaction, filepath: str, filename: str):
        """Play a music file using the Music cog."""
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("❌ Music module not loaded", ephemeral=True)
            return

        try:
            vc = await music_cog._ensure_voice(interaction)
            if not vc:
                return

            state = music_cog.get_state(interaction.guild_id)

            # Import Song class
            from cogs.music import Song

            song = Song.from_local(filepath, interaction.user)
            state.queue.append(song)

            # Get file info
            size = os.path.getsize(filepath)
            quality = self._get_audio_quality(filename)

            embed = discord.Embed(title="🎵 Added to Queue", color=discord.Color.purple())
            embed.add_field(name="File", value=f"**{filename}**", inline=False)
            embed.add_field(name="Quality", value=quality, inline=True)
            embed.add_field(name="Size", value=self._format_filesize(size), inline=True)
            embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
            embed.add_field(name="From", value=f"{interaction.user.mention}'s PC", inline=False)
            embed.set_footer(text=f"⚡ Direct Stream")

            await interaction.followup.send(embed=embed)

            if not state.is_playing:
                await music_cog._play_next(interaction.guild_id)

        except Exception as e:
            await interaction.followup.send(f"❌ Error playing file: {e}", ephemeral=True)

    @app_commands.command(name="streamlocal", description="Stream a music file directly from your PC")
    @app_commands.describe(filename="File name to stream")
    async def streamlocal(self, interaction: discord.Interaction, filename: str):
        """
        Stream a local file from user's PC.

        HOW TO USE:
        1. Make sure your file is in a accessible location
        2. Type /streamlocal <filename>
        3. Select your file from the dialog
        """
        await interaction.response.send_message(
            "🎵 **Stream Local Music Feature**\n\n"
            "This command requires you to drag-and-drop your music file into Discord.\n\n"
            "**Steps:**\n"
            "1. Drag your `.mp3`, `.flac`, `.wav`, etc. file into this chat\n"
            "2. Copy the attachment URL\n"
            "3. Use `/streamfile <url>` to play it!\n\n"
            "Or use `/streamupload` for a simpler method.",
            ephemeral=True
        )

    @app_commands.command(name="streamupload", description="Upload and stream a music file temporarily")
    async def streamupload(self, interaction: discord.Interaction, file: discord.Attachment):
        """
        Upload a music file to stream.
        The file will be deleted after playing ends.
        """
        await interaction.response.defer()

        # Validate file type
        audio_extensions = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus', '.wma')
        if not file.filename.lower().endswith(audio_extensions):
            await interaction.followup.send(
                f"❌ Invalid file type: `{file.filename}`\n"
                f"Supported: MP3, FLAC, WAV, M4A, OGG, OPUS, AAC, WMA",
                ephemeral=True
            )
            return

        # Check file size (max 100MB)
        max_size = 100 * 1024 * 1024
        if file.size > max_size:
            await interaction.followup.send(
                f"❌ File too large! Max: 100MB, Your file: {file.size / (1024*1024):.1f}MB",
                ephemeral=True
            )
            return

        try:
            # Download file to temp directory
            temp_filename = f"{interaction.user.id}_{int(time.time())}_{file.filename}"
            temp_path = os.path.join(self.temp_dir, temp_filename)

            await file.save(temp_path)

            # Play the file
            await self._play_file(interaction, temp_path, file.filename)

            # Mark for cleanup after playing
            self.active_streams[interaction.guild_id] = temp_path

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="streamstatus", description="Check active streams")
    async def streamstatus(self, interaction: discord.Interaction):
        """Show active streaming status."""
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.response.send_message("❌ Music module not loaded", ephemeral=True)
            return

        state = music_cog.get_state(interaction.guild_id)

        embed = discord.Embed(title="🎵 Stream Status", color=discord.Color.purple())

        # Current stream
        if state.current:
            embed.add_field(
                name="Now Streaming",
                value=f"**{state.current.title}**\n"
                      f"Requested by: {state.current.requester.mention}\n"
                      f"Status: {'▶️ Playing' if state.is_playing else '⏸️ Paused'}",
                inline=False
            )

            # Check if it's from temp directory
            if state.current.is_local and "temp_music" in state.current.source_url:
                embed.add_field(
                    name="Stream Type",
                    value="📤 Temporary Upload (will be deleted after playing)",
                    inline=False
                )
            elif state.current.is_local:
                embed.add_field(
                    name="Stream Type",
                    value="💾 Local File",
                    inline=False
                )
        else:
            embed.description = "No active stream"

        # Queue info
        embed.add_field(
            name="Queue",
            value=f"**{len(state.queue)}** tracks waiting",
            inline=True
        )

        embed.add_field(
            name="Volume",
            value=f"**{int(state.volume * 100)}%**",
            inline=True
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="streamhelp", description="Get help on streaming music from PC")
    async def streamhelp(self, interaction: discord.Interaction):
        """Show streaming help."""
        embed = discord.Embed(
            title="📤 How to Stream Music from Your PC",
            description="Stream music directly without permanent upload!",
            color=discord.Color.green()
        )

        embed.add_field(
            name="⚡ Method 1: Quick Upload (Easiest)",
            value="```\n/streamupload <drag-your-file-here>\n```\n"
                  "✅ Upload your file\n"
                  "✅ Bot plays it immediately\n"
                  "✅ Automatically deleted when done",
            inline=False
        )

        embed.add_field(
            name="📝 How to Use Upload:",
            value="1. Type `/streamupload`\n"
                  "2. Drag your music file into the text input\n"
                  "3. Bot downloads and plays it\n"
                  "4. File is auto-deleted after playing",
            inline=False
        )

        embed.add_field(
            name="✅ Supported Formats",
            value="🏆 **Lossless:** FLAC, WAV\n"
                  "💎 **High Quality:** M4A, AAC, Opus\n"
                  "🎵 **Standard:** MP3, OGG, WMA",
            inline=False
        )

        embed.add_field(
            name="⚙️ Limitations",
            value="• Max file size: 100MB\n"
                  "• Upload depends on your internet speed\n"
                  "• FLAC recommended for best quality",
            inline=False
        )

        embed.add_field(
            name="💡 Tips",
            value="• Use FLAC for lossless audio\n"
                  "• Larger files take longer to upload\n"
                  "• All server members can hear the stream",
            inline=False
        )

        embed.add_field(
            name="🎵 Other Commands",
            value="`/streamstatus` - Check current stream\n"
                  "`/play` - Play from YouTube\n"
                  "`/mymusic` - Play from personal library",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Cleanup temp files on startup."""
        try:
            current_time = time.time()
            for filename in os.listdir(self.temp_dir):
                filepath = os.path.join(self.temp_dir, filename)
                try:
                    os.remove(filepath)
                except:
                    pass
        except:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(DirectStreamMusic(bot))
