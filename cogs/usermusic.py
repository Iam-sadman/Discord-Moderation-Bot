import os
import discord
from discord import app_commands
from discord.ext import commands
from typing import Literal


class UserMusic(commands.Cog):
    """🎵 User Music Library - Upload and play your own high-quality music collection."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Root music directory
        self.music_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "music")
        self.public_dir = os.path.join(self.music_root, "public")
        self.users_dir = os.path.join(self.music_root, "users")

        # Create directories
        os.makedirs(self.public_dir, exist_ok=True)
        os.makedirs(self.users_dir, exist_ok=True)

    def _get_user_dir(self, user_id: int) -> str:
        """Get or create user's personal music directory."""
        user_path = os.path.join(self.users_dir, str(user_id))
        os.makedirs(user_path, exist_ok=True)
        return user_path

    def _get_audio_files(self, directory: str) -> list[tuple[str, str]]:
        """Get all audio files from directory. Returns (filename, full_path)."""
        if not os.path.exists(directory):
            return []

        audio_extensions = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus', '.wma')
        files = []

        for filename in os.listdir(directory):
            if filename.lower().endswith(audio_extensions):
                full_path = os.path.join(directory, filename)
                files.append((filename, full_path))

        return sorted(files, key=lambda x: x[0].lower())

    def _search_files(self, query: str, search_in: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Search files by name (case insensitive)."""
        query = query.lower()
        results = []

        # Exact match first
        for filename, path in search_in:
            name_without_ext = os.path.splitext(filename)[0].lower()
            if name_without_ext == query or filename.lower() == query:
                return [(filename, path)]

        # Partial match
        for filename, path in search_in:
            if query in filename.lower():
                results.append((filename, path))

        return results

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

    async def _play_file(self, interaction: discord.Interaction, filepath: str, filename: str, owner: str = None):
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

            if owner:
                embed.add_field(name="Uploaded by", value=owner, inline=False)

            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.followup.send(embed=embed)

            if not state.is_playing:
                await music_cog._play_next(interaction.guild_id)

        except Exception as e:
            await interaction.followup.send(f"❌ Error playing file: {e}", ephemeral=True)

    # ──── Commands ────

    @app_commands.command(name="mymusic", description="Play a song from your personal library")
    @app_commands.describe(query="Song name (can be partial)")
    async def mymusic(self, interaction: discord.Interaction, query: str):
        """Play from user's personal music library."""
        await interaction.response.defer()

        user_dir = self._get_user_dir(interaction.user.id)
        files = self._get_audio_files(user_dir)

        if not files:
            await interaction.followup.send(
                f"📁 Your music library is empty!\n\n"
                f"**Upload your music files to:**\n"
                f"```\n{user_dir}\n```\n"
                f"**Supported formats:** MP3, FLAC, WAV, M4A, OGG, OPUS, AAC, WMA\n\n"
                f"Use `/musicupload` to get upload instructions.",
                ephemeral=True
            )
            return

        # Search for the file
        results = self._search_files(query, files)

        if not results:
            file_list = "\n".join(f"• `{f[0]}`" for f in files[:10])
            if len(files) > 10:
                file_list += f"\n... and {len(files) - 10} more"

            await interaction.followup.send(
                f"❌ No match found for: `{query}`\n\n**Your files:**\n{file_list}",
                ephemeral=True
            )
            return

        # If multiple matches, show options
        if len(results) > 1:
            file_list = "\n".join(f"• `{f[0]}`" for f in results[:5])
            await interaction.followup.send(
                f"🔍 Multiple matches found for `{query}`:\n{file_list}\n\n"
                f"Please be more specific!",
                ephemeral=True
            )
            return

        # Play the file
        filename, filepath = results[0]
        await self._play_file(interaction, filepath, filename, owner=interaction.user.mention)

    @app_commands.command(name="publicmusic", description="Play a song from public library")
    @app_commands.describe(query="Song name (can be partial)")
    async def publicmusic(self, interaction: discord.Interaction, query: str):
        """Play from public music library (shared by all users)."""
        await interaction.response.defer()

        files = self._get_audio_files(self.public_dir)

        if not files:
            await interaction.followup.send(
                f"📁 Public library is empty!\n\n"
                f"Admins can upload to: `{self.public_dir}`",
                ephemeral=True
            )
            return

        # Search for the file
        results = self._search_files(query, files)

        if not results:
            file_list = "\n".join(f"• `{f[0]}`" for f in files[:10])
            if len(files) > 10:
                file_list += f"\n... and {len(files) - 10} more"

            await interaction.followup.send(
                f"❌ No match found for: `{query}`\n\n**Available:**\n{file_list}",
                ephemeral=True
            )
            return

        if len(results) > 1:
            file_list = "\n".join(f"• `{f[0]}`" for f in results[:5])
            await interaction.followup.send(
                f"🔍 Multiple matches:\n{file_list}\n\nBe more specific!",
                ephemeral=True
            )
            return

        filename, filepath = results[0]
        await self._play_file(interaction, filepath, filename, owner="Public Library")

    @app_commands.command(name="musiclibrary", description="Browse music libraries")
    @app_commands.describe(
        library="Which library to browse",
        user="Browse another user's library (optional)"
    )
    @app_commands.choices(library=[
        app_commands.Choice(name="My Music", value="my"),
        app_commands.Choice(name="Public Library", value="public"),
        app_commands.Choice(name="Other User", value="user"),
    ])
    async def musiclibrary(
        self,
        interaction: discord.Interaction,
        library: app_commands.Choice[str],
        user: discord.Member = None
    ):
        """Browse music libraries."""
        await interaction.response.defer(ephemeral=True)

        if library.value == "my":
            directory = self._get_user_dir(interaction.user.id)
            title = f"🎵 {interaction.user.display_name}'s Music Library"
        elif library.value == "public":
            directory = self.public_dir
            title = "🎵 Public Music Library"
        elif library.value == "user" and user:
            directory = self._get_user_dir(user.id)
            title = f"🎵 {user.display_name}'s Music Library"
        else:
            await interaction.followup.send("❌ Please specify a user!", ephemeral=True)
            return

        files = self._get_audio_files(directory)

        if not files:
            await interaction.followup.send(f"📁 {title}\n\nNo files found.", ephemeral=True)
            return

        # Calculate total size
        total_size = sum(os.path.getsize(path) for _, path in files)

        embed = discord.Embed(
            title=title,
            description=f"**Total files:** {len(files)} • **Total size:** {self._format_filesize(total_size)}",
            color=discord.Color.purple()
        )

        # Group by quality
        flac_files = [f for f in files if f[0].lower().endswith(('.flac', '.wav'))]
        hq_files = [f for f in files if f[0].lower().endswith(('.m4a', '.aac', '.opus'))]
        standard_files = [f for f in files if f[0].lower().endswith(('.mp3', '.ogg', '.wma'))]

        if flac_files:
            file_list = "\n".join(f"• `{f[0]}`" for f in flac_files[:5])
            if len(flac_files) > 5:
                file_list += f"\n*... and {len(flac_files) - 5} more*"
            embed.add_field(name="🏆 Lossless Quality", value=file_list, inline=False)

        if hq_files:
            file_list = "\n".join(f"• `{f[0]}`" for f in hq_files[:5])
            if len(hq_files) > 5:
                file_list += f"\n*... and {len(hq_files) - 5} more*"
            embed.add_field(name="💎 High Quality", value=file_list, inline=False)

        if standard_files:
            file_list = "\n".join(f"• `{f[0]}`" for f in standard_files[:5])
            if len(standard_files) > 5:
                file_list += f"\n*... and {len(standard_files) - 5} more*"
            embed.add_field(name="🎵 Standard Quality", value=file_list, inline=False)

        command_hint = "/mymusic" if library.value == "my" else "/publicmusic"
        embed.set_footer(text=f"Use {command_hint} <song name> to play")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="musicupload", description="Get instructions to upload your music")
    async def musicupload(self, interaction: discord.Interaction):
        """Show upload instructions."""
        user_dir = self._get_user_dir(interaction.user.id)

        embed = discord.Embed(
            title="📤 Upload Your Music",
            description="Upload your personal music collection to the server!",
            color=discord.Color.green()
        )

        embed.add_field(
            name="📁 Your Upload Directory",
            value=f"```\n{user_dir}\n```",
            inline=False
        )

        embed.add_field(
            name="🖥️ Using SCP (Recommended)",
            value=f"```bash\n"
                  f"# Single file\n"
                  f"scp 'your-song.flac' user@server:{user_dir}/\n\n"
                  f"# Multiple files\n"
                  f"scp *.flac user@server:{user_dir}/\n\n"
                  f"# Entire folder\n"
                  f"scp -r 'My Music Folder' user@server:{user_dir}/\n"
                  f"```",
            inline=False
        )

        embed.add_field(
            name="🌐 Using SFTP (FileZilla, WinSCP)",
            value=f"1. Connect to server via SFTP\n"
                  f"2. Navigate to: `{user_dir}`\n"
                  f"3. Drag and drop your files",
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
            name="🎵 After Uploading",
            value="Use `/mymusic <song name>` to play!",
            inline=False
        )

        embed.set_footer(text=f"Your User ID: {interaction.user.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="musicstats", description="View music library statistics")
    async def musicstats(self, interaction: discord.Interaction):
        """Show overall music library statistics."""
        await interaction.response.defer(ephemeral=True)

        # Get user's files
        user_files = self._get_audio_files(self._get_user_dir(interaction.user.id))
        user_size = sum(os.path.getsize(path) for _, path in user_files)

        # Get public files
        public_files = self._get_audio_files(self.public_dir)
        public_size = sum(os.path.getsize(path) for _, path in public_files)

        # Count total users with music
        users_with_music = 0
        total_user_files = 0
        total_user_size = 0

        if os.path.exists(self.users_dir):
            for user_id in os.listdir(self.users_dir):
                user_path = os.path.join(self.users_dir, user_id)
                if os.path.isdir(user_path):
                    files = self._get_audio_files(user_path)
                    if files:
                        users_with_music += 1
                        total_user_files += len(files)
                        total_user_size += sum(os.path.getsize(path) for _, path in files)

        embed = discord.Embed(title="📊 Music Library Statistics", color=discord.Color.blue())

        embed.add_field(
            name="👤 Your Library",
            value=f"**Files:** {len(user_files)}\n**Size:** {self._format_filesize(user_size)}",
            inline=True
        )

        embed.add_field(
            name="🌐 Public Library",
            value=f"**Files:** {len(public_files)}\n**Size:** {self._format_filesize(public_size)}",
            inline=True
        )

        embed.add_field(
            name="📈 Server Total",
            value=f"**Users:** {users_with_music}\n"
                  f"**Files:** {total_user_files + len(public_files)}\n"
                  f"**Size:** {self._format_filesize(total_user_size + public_size)}",
            inline=False
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sharesong", description="Share your song to public library")
    @app_commands.describe(filename="Your song to share")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sharesong(self, interaction: discord.Interaction, filename: str):
        """Share a personal song to public library (requires Manage Messages permission)."""
        await interaction.response.defer(ephemeral=True)

        user_dir = self._get_user_dir(interaction.user.id)
        user_files = self._get_audio_files(user_dir)

        results = self._search_files(filename, user_files)

        if not results:
            await interaction.followup.send(f"❌ File not found in your library: `{filename}`", ephemeral=True)
            return

        if len(results) > 1:
            file_list = "\n".join(f"• `{f[0]}`" for f in results)
            await interaction.followup.send(
                f"🔍 Multiple matches:\n{file_list}\n\nBe more specific!",
                ephemeral=True
            )
            return

        # Copy to public
        import shutil
        src_filename, src_path = results[0]
        dst_path = os.path.join(self.public_dir, src_filename)

        try:
            shutil.copy2(src_path, dst_path)
            size = os.path.getsize(src_path)
            quality = self._get_audio_quality(src_filename)

            embed = discord.Embed(title="✅ Song Shared to Public Library", color=discord.Color.green())
            embed.add_field(name="File", value=f"**{src_filename}**", inline=False)
            embed.add_field(name="Quality", value=quality, inline=True)
            embed.add_field(name="Size", value=self._format_filesize(size), inline=True)
            embed.set_footer(text=f"Shared by {interaction.user}")

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error sharing file: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(UserMusic(bot))
