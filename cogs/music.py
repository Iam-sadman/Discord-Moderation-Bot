"""Unified YouTube and local-library music player for Discord voice channels."""

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from config import Config


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch5",
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".wma")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
TEMP_STALE_AFTER_SECONDS = 24 * 60 * 60


class Song:
    """A queued YouTube stream or local audio file."""

    def __init__(
        self,
        source_url: str,
        title: str,
        duration: int,
        thumbnail: str,
        webpage_url: str,
        requester: discord.abc.User,
        is_local: bool = False,
        temporary: bool = False,
    ):
        self.source_url = source_url
        self.title = title
        self.duration = duration or 0
        self.thumbnail = thumbnail or ""
        self.webpage_url = webpage_url
        self.requester = requester
        self.is_local = is_local
        self.temporary = temporary

    @classmethod
    async def from_youtube(cls, query: str, requester: discord.abc.User, loop=None):
        loop = loop or asyncio.get_running_loop()
        if "list=" in query:
            return await cls._from_playlist(query, requester, loop)

        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if not data:
            raise ValueError("Could not extract information from that YouTube URL or search.")

        if "entries" in data:
            entries = [entry for entry in data["entries"] if entry]
            if not entries:
                raise ValueError("No playable results were found.")
            return entries

        return [cls(
            source_url=data.get("url") or data.get("webpage_url", query),
            title=data.get("title", "Unknown"),
            duration=data.get("duration") or 0,
            thumbnail=data.get("thumbnail", ""),
            webpage_url=data.get("webpage_url", query),
            requester=requester,
        )]

    @classmethod
    async def _from_playlist(cls, url: str, requester: discord.abc.User, loop):
        playlist_ytdl = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "extract_flat": True})
        data = await loop.run_in_executor(None, lambda: playlist_ytdl.extract_info(url, download=False))
        if not data or "entries" not in data:
            raise ValueError("Could not read that YouTube playlist.")

        songs = []
        for entry in data["entries"]:
            if not entry:
                continue
            webpage_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url", "")
            if webpage_url and not webpage_url.startswith(("http://", "https://")):
                video_id = entry.get("id") or webpage_url
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            songs.append(cls(
                source_url=entry.get("url", ""),
                title=entry.get("title", "Unknown"),
                duration=entry.get("duration") or 0,
                thumbnail=entry.get("thumbnail", ""),
                webpage_url=webpage_url,
                requester=requester,
            ))
        return songs

    @classmethod
    def from_local(cls, filepath: str, requester: discord.abc.User, temporary: bool = False):
        path = Path(filepath)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path.name}")
        return cls(
            source_url=str(path),
            title=path.stem,
            duration=0,
            thumbnail="",
            webpage_url=str(path),
            requester=requester,
            is_local=True,
            temporary=temporary,
        )


class GuildMusicState:
    """Playback state and temporary upload ownership for one guild."""

    def __init__(self):
        self.queue: list[Song] = []
        self.current: Song | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.volume = 1.0
        self.loop_mode = "off"
        self.is_playing = False
        self._skip_flag = False
        self._stop_flag = False
        self._ending_session = False
        self._inactivity_task: asyncio.Task | None = None
        self.temp_files: set[str] = set()
        self.controller_message: discord.Message | None = None

    def reset(self):
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        self.is_playing = False
        self._skip_flag = False
        self._stop_flag = False
        self.voice_client = None


class MusicController(discord.ui.View):
    """Shared controls attached to the guild's Now Playing embed."""

    def __init__(self, music: "Music"):
        super().__init__(timeout=None)
        self.music = music

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await interaction.response.send_message("These controls only work in a server.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music:toggle")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        state.controller_message = interaction.message
        vc = state.voice_client
        if vc and vc.is_playing():
            vc.pause()
            state.is_playing = False
            await interaction.response.send_message("⏸️ Playback paused.", ephemeral=True)
        elif vc and vc.is_paused():
            vc.resume()
            state.is_playing = True
            await interaction.response.send_message("▶️ Playback resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        await self.music._refresh_controller(guild_id)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        state.controller_message = interaction.message
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        state._skip_flag = True
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        state.controller_message = interaction.message
        state.queue.clear()
        state.loop_mode = "off"
        has_active_source = bool(
            state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused())
        )
        state._stop_flag = has_active_source
        if has_active_source:
            state.voice_client.stop()
        state.current = None
        state.is_playing = False
        await interaction.response.send_message("⏹️ Playback stopped and queue cleared.", ephemeral=True)
        await self.music._refresh_controller(guild_id)

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, custom_id="music:volume_down")
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._change_volume(interaction, -10)

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, custom_id="music:volume_up")
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._change_volume(interaction, 10)


class Music(commands.Cog):
    """Single-cog YouTube and local music player with Discord message controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}
        self.music_root = Path(__file__).resolve().parent.parent / "music"
        self.local_dir = self.music_root
        self.public_dir = self.music_root / "public"
        self.users_dir = self.music_root / "users"
        self.temp_dir = Path(__file__).resolve().parent.parent / "temp_music"
        for directory in (self.local_dir, self.public_dir, self.users_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.bot.add_view(MusicController(self))
        self.cleanup_task = asyncio.create_task(self._cleanup_stale_temp_files())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        allowed = await Config.check_member_cog_permission(interaction.user, "music")
        if not allowed:
            await interaction.response.send_message(
                "❌ You do not have permission to use the **Music** module on this server.",
                ephemeral=True
            )
            return False
        return True

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    def _get_user_dir(self, user_id: int, create: bool = True) -> Path:
        directory = self.users_dir / str(user_id)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _audio_files(self, directory: Path) -> list[tuple[str, str]]:
        if not directory.is_dir():
            return []
        items = []
        for path in directory.iterdir():
            try:
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                    items.append((path.name, str(path)))
            except OSError:
                continue
        return sorted(items, key=lambda item: item[0].lower())

    @staticmethod
    def _search_files(query: str, files: list[tuple[str, str]]) -> list[tuple[str, str]]:
        query = query.casefold().strip()
        if not query:
            return []
        for filename, path in files:
            if filename.casefold() == query or Path(filename).stem.casefold() == query:
                return [(filename, path)]
        return [(filename, path) for filename, path in files if query in filename.casefold()]

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds <= 0:
            return "Live/Unknown"
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @staticmethod
    def _quality(filename: str) -> str:
        return {
            ".flac": "🏆 Lossless source (FLAC)",
            ".wav": "🏆 Lossless source (WAV)",
            ".m4a": "💎 AAC source",
            ".opus": "💎 Opus source",
            ".aac": "💎 AAC source",
            ".mp3": "🎵 MP3 source",
            ".ogg": "🎵 OGG source",
            ".wma": "🎵 WMA source",
        }.get(Path(filename).suffix.lower(), "🎵 Audio")

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        if not interaction.guild_id or not interaction.guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return None
        voice_state = getattr(interaction.user, "voice", None)
        channel = voice_state.channel if voice_state else None
        if channel is None:
            await interaction.followup.send("❌ Join a voice channel first.", ephemeral=True)
            return None

        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client
        state.voice_client = await channel.connect(self_deaf=True)
        return state.voice_client

    async def _ensure_voice_ctx(self, ctx: commands.Context) -> discord.VoiceClient | None:
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            return None
        voice_state = getattr(ctx.author, "voice", None)
        channel = voice_state.channel if voice_state else None
        if channel is None:
            await ctx.send("❌ Join a voice channel first.")
            return None

        state = self.get_state(ctx.guild.id)
        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client
        state.voice_client = await channel.connect(self_deaf=True)
        return state.voice_client

    def _controller_embed(self, state: GuildMusicState) -> discord.Embed:
        song = state.current
        embed = discord.Embed(title="🎵 Music Controller", color=discord.Color.purple())
        if song:
            embed.description = f"**{discord.utils.escape_markdown(song.title[:240])}**"
            embed.add_field(name="Requested by", value=song.requester.mention, inline=True)
            status = "⏸️ Paused" if state.voice_client and state.voice_client.is_paused() else "▶️ Playing"
            embed.add_field(name="Status", value=status, inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
        else:
            embed.description = "Nothing is playing. Add a track with `/play`, `/playlocal`, or `/streamupload`."
        queue_lines = [
            f"`{i}.` {discord.utils.escape_markdown(item.title[:160])}"
            for i, item in enumerate(state.queue[:5], 1)
        ]
        if len(state.queue) > 5:
            queue_lines.append(f"*and {len(state.queue) - 5} more*")
        embed.add_field(name=f"Up next ({len(state.queue)})", value="\n".join(queue_lines) or "Queue is empty", inline=False)
        embed.set_footer(text=f"Volume {int(state.volume * 100)}% · Loop {state.loop_mode}")
        return embed

    async def _refresh_controller(self, guild_id: int, channel=None):
        state = self.get_state(guild_id)
        message = state.controller_message
        view = MusicController(self)
        embed = self._controller_embed(state)
        if message:
            try:
                await message.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                state.controller_message = None
            except discord.HTTPException as exc:
                print(f"[Music] Could not update controller for guild {guild_id}: {exc}")
                return
        if channel is not None:
            try:
                state.controller_message = await channel.send(embed=embed, view=view)
            except discord.HTTPException as exc:
                print(f"[Music] Could not send controller for guild {guild_id}: {exc}")

    async def _change_volume(self, interaction: discord.Interaction, delta: int):
        guild_id = self.guild_id_for(interaction)
        state = self.get_state(guild_id)
        state.controller_message = interaction.message
        state.volume = max(0.0, min(2.0, state.volume + delta / 100))
        source = state.voice_client.source if state.voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume: {int(state.volume * 100)}%", ephemeral=True)
        await self._refresh_controller(guild_id)

    @staticmethod
    def guild_id_for(interaction: discord.Interaction) -> int:
        if interaction.guild_id is None:
            raise RuntimeError("Music controls can only be used in a server.")
        return interaction.guild_id

    async def _play_next(self, guild_id: int):
        state = self.get_state(guild_id)
        vc = state.voice_client
        if not vc or not vc.is_connected():
            if state.temp_files or vc:
                await self._end_voice_session(guild_id)
            else:
                state.current = None
                state.is_playing = False
            return

        if state._stop_flag:
            state._stop_flag = False
            state.current = None
            state.is_playing = False
            await self._refresh_controller(guild_id)
            return

        was_skipped = state._skip_flag
        state._skip_flag = False
        if not was_skipped and state.loop_mode == "single" and state.current:
            song = state.current
        else:
            if not was_skipped and state.loop_mode == "queue" and state.current:
                state.queue.append(state.current)
            if not state.queue:
                state.current = None
                state.is_playing = False
                await self._refresh_controller(guild_id)
                self._start_inactivity_timer(guild_id)
                return
            song = state.queue.pop(0)
            state.current = song

        try:
            if song.is_local:
                source = discord.FFmpegPCMAudio(song.source_url)
                source = discord.PCMVolumeTransformer(source, volume=state.volume)
            else:
                if not song.source_url.startswith("http") or "googlevideo" not in song.source_url:
                    data = await self.bot.loop.run_in_executor(
                        None,
                        lambda: ytdl.extract_info(song.webpage_url or song.source_url, download=False),
                    )
                    if not data:
                        raise ValueError("Could not resolve this YouTube track.")
                    song.source_url = data.get("url", song.source_url)
                    song.title = data.get("title", song.title)
                    song.duration = data.get("duration") or song.duration
                    song.thumbnail = data.get("thumbnail", song.thumbnail)
                source = discord.FFmpegPCMAudio(
                    song.source_url,
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn",
                )
                source = discord.PCMVolumeTransformer(source, volume=state.volume)

            state.is_playing = True
            self._cancel_inactivity_timer(guild_id)
            await self._refresh_controller(guild_id)

            def after_playing(error):
                if error:
                    print(f"[Music] Player error in guild {guild_id}: {error}")
                if not self.bot.is_closed():
                    asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)

            vc.play(source, after=after_playing)
        except Exception as exc:
            print(f"[Music] Could not play {song.title}: {exc}")
            state.is_playing = False
            await self._play_next(guild_id)

    def _start_inactivity_timer(self, guild_id: int):
        self._cancel_inactivity_timer(guild_id)
        state = self.get_state(guild_id)

        async def disconnect_after_timeout():
            try:
                await asyncio.sleep(180)
                if state.voice_client and state.voice_client.is_connected() and not state.is_playing and not state.queue:
                    await self._end_voice_session(guild_id)
            except asyncio.CancelledError:
                pass

        state._inactivity_task = asyncio.create_task(disconnect_after_timeout())

    def _cancel_inactivity_timer(self, guild_id: int):
        state = self.get_state(guild_id)
        task = state._inactivity_task
        if task and not task.done():
            task.cancel()
        state._inactivity_task = None

    async def _cleanup_temp_files(self, guild_id: int):
        state = self.get_state(guild_id)
        paths = tuple(state.temp_files)
        for path in paths:
            try:
                await asyncio.to_thread(os.remove, path)
                state.temp_files.discard(path)
            except FileNotFoundError:
                state.temp_files.discard(path)
            except OSError as exc:
                print(f"[Music] Could not remove temporary upload {path}: {exc}")

    async def _end_voice_session(self, guild_id: int):
        state = self.get_state(guild_id)
        if state._ending_session:
            return
        state._ending_session = True
        self._cancel_inactivity_timer(guild_id)
        vc = state.voice_client
        try:
            state.queue.clear()
            state.current = None
            state.is_playing = False
            state._skip_flag = False
            state._stop_flag = True
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                await asyncio.sleep(0.15)
            if vc and vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except discord.HTTPException as exc:
                    print(f"[Music] Could not disconnect voice client for guild {guild_id}: {exc}")
            state.reset()
            await self._cleanup_temp_files(guild_id)
            await self._refresh_controller(guild_id)
        finally:
            state._ending_session = False

    async def _cleanup_stale_temp_files(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            while not self.bot.is_ready():
                if self.bot.is_closed():
                    return
                await asyncio.sleep(1)
        while not self.bot.is_closed():
            cutoff = time.time() - TEMP_STALE_AFTER_SECONDS
            active_paths = {
                os.path.abspath(path)
                for state in self.states.values()
                for path in state.temp_files
            }
            try:
                for path in self.temp_dir.iterdir():
                    try:
                        if (
                            path.is_file()
                            and os.path.abspath(path) not in active_paths
                            and path.stat().st_mtime < cutoff
                        ):
                            await asyncio.to_thread(path.unlink)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        print(f"[Music] Could not remove stale upload {path}: {exc}")
            except OSError as exc:
                print(f"[Music] Temp directory cleanup failed: {exc}")
            await asyncio.sleep(300)

    def _find_local_file(self, query: str) -> str | None:
        found = self._search_files(query, self._audio_files(self.local_dir))
        return found[0][1] if found else None

    async def _queue_local_file(
        self,
        interaction: discord.Interaction,
        filepath: str,
        filename: str,
        owner: str | None = None,
        temporary: bool = False,
    ):
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        state = self.get_state(interaction.guild_id)
        try:
            song = Song.from_local(filepath, interaction.user, temporary=temporary)
        except (OSError, FileNotFoundError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        state.queue.append(song)
        if temporary:
            state.temp_files.add(os.path.abspath(filepath))
        embed = discord.Embed(title="🎵 Added to Queue", color=discord.Color.purple())
        embed.add_field(name="File", value=filename, inline=False)
        embed.add_field(name="Source", value=self._quality(filename), inline=True)
        try:
            embed.add_field(name="Size", value=self._format_size(os.path.getsize(filepath)), inline=True)
        except OSError:
            pass
        embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
        if owner:
            embed.add_field(name="Library", value=owner, inline=False)
        embed.set_footer(text=f"Requested by {interaction.user}")
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException as exc:
            # The audio is already queued; a failed acknowledgement must not delete it.
            print(f"[Music] Could not acknowledge queued file in guild {interaction.guild_id}: {exc}")
        await self._refresh_controller(interaction.guild_id, interaction.channel)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    async def _resolve_file_search(self, interaction, query, files, label):
        matches = self._search_files(query, files)
        if not matches:
            preview = "\n".join(f"• `{name[:100]}`" for name, _ in files[:10])
            suffix = f"\n… and {len(files) - 10} more" if len(files) > 10 else ""
            await interaction.followup.send(
                f"❌ No match for `{query}` in {label}." + (f"\n\n{preview}{suffix}" if preview else ""),
                ephemeral=True,
            )
            return None
        if len(matches) > 1:
            preview = "\n".join(f"• `{name[:100]}`" for name, _ in matches[:8])
            await interaction.followup.send(f"🔍 Multiple matches:\n{preview}\nUse a more specific name.", ephemeral=True)
            return None
        return matches[0]

    # ─── Playback commands ──────────────────────────────────────────

    @app_commands.command(name="play", description="Play a YouTube URL, search query, or playlist")
    @app_commands.describe(query="YouTube URL, playlist URL, or search keywords")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        if not query.startswith(("http://", "https://")):
            query = f"ytsearch:{query}"
        try:
            songs = await Song.from_youtube(query, interaction.user, self.bot.loop)
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not load that track: {exc}", ephemeral=True)
            return
        if not songs or not isinstance(songs[0], Song):
            await interaction.followup.send("❌ Could not find a playable track. Use `/search` to choose a result.", ephemeral=True)
            return
        state = self.get_state(interaction.guild_id)
        state.queue.extend(songs)
        if len(songs) == 1:
            song = songs[0]
            embed = discord.Embed(title="🎵 Added to Queue", description=f"**{song.title}**", color=discord.Color.purple())
            embed.add_field(name="Duration", value=self._format_duration(song.duration), inline=True)
            embed.add_field(name="Queue position", value=f"#{len(state.queue)}", inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
        else:
            embed = discord.Embed(
                title="🎵 Playlist Added",
                description=f"Added **{len(songs)}** tracks to the queue.",
                color=discord.Color.purple(),
            )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed)
        await self._refresh_controller(interaction.guild_id, interaction.channel)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    @app_commands.command(name="search", description="Search YouTube and select a track")
    @app_commands.describe(query="Search keywords")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        try:
            search_ytdl = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "default_search": "ytsearch5"})
            data = await self.bot.loop.run_in_executor(
                None, lambda: search_ytdl.extract_info(f"ytsearch5:{query}", download=False)
            )
            entries = [entry for entry in (data or {}).get("entries", []) if entry][:5]
            if not entries:
                await interaction.followup.send("❌ No YouTube results found.", ephemeral=True)
                return
            options = [
                discord.SelectOption(
                    label=f"{index}. {entry.get('title', 'Unknown')[:95]}",
                    value=str(index - 1),
                    description=f"Duration: {self._format_duration(entry.get('duration') or 0)}",
                )
                for index, entry in enumerate(entries, 1)
            ]
            embed = discord.Embed(title=f"🔍 YouTube results: {query[:200]}", color=discord.Color.purple())
            for index, entry in enumerate(entries, 1):
                embed.add_field(
                    name=f"{index}. {entry.get('title', 'Unknown')[:200]}",
                    value=f"Duration: {self._format_duration(entry.get('duration') or 0)}",
                    inline=False,
                )
            select = discord.ui.Select(placeholder="Choose a track", options=options)

            async def select_callback(select_interaction: discord.Interaction):
                if not select_interaction.guild_id:
                    await select_interaction.response.send_message("Use this in a server.", ephemeral=True)
                    return
                selected = entries[int(select.values[0])]
                webpage_url = selected.get("webpage_url") or selected.get("url", "")
                song = Song(
                    source_url=selected.get("url", webpage_url),
                    title=selected.get("title", "Unknown"),
                    duration=selected.get("duration") or 0,
                    thumbnail=selected.get("thumbnail", ""),
                    webpage_url=webpage_url,
                    requester=select_interaction.user,
                )
                await select_interaction.response.defer()
                vc = await self._ensure_voice(select_interaction)
                if not vc:
                    return
                state = self.get_state(select_interaction.guild_id)
                state.queue.append(song)
                await select_interaction.followup.send(f"🎵 Added **{song.title}** to the queue.")
                await self._refresh_controller(select_interaction.guild_id, select_interaction.channel)
                if not state.is_playing and state.current is None:
                    await self._play_next(select_interaction.guild_id)

            select.callback = select_callback
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            await interaction.followup.send(embed=embed, view=view)
        except Exception as exc:
            await interaction.followup.send(f"❌ Search error: {exc}", ephemeral=True)

    @app_commands.command(name="playlocal", description="Play a file from the server music folder")
    @app_commands.describe(filename="Full or partial file name")
    async def playlocal(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer()
        files = self._audio_files(self.local_dir)
        match = await self._resolve_file_search(interaction, filename, files, "the server music folder")
        if match:
            await self._queue_local_file(interaction, match[1], match[0], owner="Server library")

    @app_commands.command(name="mymusic", description="Play a song from your personal library")
    async def mymusic(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)
        files = self._audio_files(self._get_user_dir(interaction.user.id))
        if not files:
            await interaction.followup.send(
                f"Your library is empty. Upload files to `{self._get_user_dir(interaction.user.id)}`.", ephemeral=True
            )
            return
        match = await self._resolve_file_search(interaction, query, files, "your library")
        if match:
            await self._queue_local_file(interaction, match[1], match[0], owner=interaction.user.mention)

    @app_commands.command(name="publicmusic", description="Play a song from the public library")
    async def publicmusic(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        files = self._audio_files(self.public_dir)
        if not files:
            await interaction.followup.send(f"Public library is empty. Add files to `{self.public_dir}`.", ephemeral=True)
            return
        match = await self._resolve_file_search(interaction, query, files, "the public library")
        if match:
            await self._queue_local_file(interaction, match[1], match[0], owner="Public library")

    # ─── Upload and library commands ─────────────────────────────────

    @app_commands.command(name="streamupload", description="Upload and queue a temporary audio file (max 100 MB)")
    async def streamupload(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer()
        safe_name = Path(file.filename).name
        if Path(safe_name).suffix.lower() not in AUDIO_EXTENSIONS:
            await interaction.followup.send(
                "❌ Unsupported audio type. Supported: MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WMA.", ephemeral=True
            )
            return
        if file.size > MAX_UPLOAD_BYTES:
            await interaction.followup.send(
                f"❌ File is {file.size / (1024 * 1024):.1f} MB; the bot limit is 100 MB.", ephemeral=True
            )
            return
        if not interaction.guild_id:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        temp_path = self.temp_dir / f"{interaction.guild_id}_{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
        try:
            await file.save(temp_path)
            vc = await self._ensure_voice(interaction)
            if not vc:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                return
            await self._queue_local_file(
                interaction,
                str(temp_path),
                safe_name,
                owner=f"Temporary upload by {interaction.user.mention}",
                temporary=True,
            )
        except Exception as exc:
            try:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            except OSError:
                pass
            await interaction.followup.send(f"❌ Upload/playback error: {exc}", ephemeral=True)

    @app_commands.command(name="musiclist", description="List server-local music files")
    async def musiclist(self, interaction: discord.Interaction):
        files = self._audio_files(self.local_dir)
        if not files:
            await interaction.response.send_message(
                f"No server music files found. Add audio to `{self.local_dir}`.", ephemeral=True
            )
            return
        lines = []
        for index, (filename, path) in enumerate(files[:20], 1):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            lines.append(f"`{index}.` **{filename}** ({self._format_size(size)})")
        if len(files) > 20:
            lines.append(f"… and {len(files) - 20} more")
        embed = discord.Embed(
            title="🎵 Server Music Library",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{len(files)} files · Use /playlocal <filename>")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="musicinfo", description="Show server music folder information")
    @app_commands.checks.has_permissions(administrator=True)
    async def musicinfo(self, interaction: discord.Interaction):
        files = self._audio_files(self.local_dir)
        total_size = sum(os.path.getsize(path) for _, path in files if os.path.exists(path))
        embed = discord.Embed(title="📁 Music Folder Info", color=discord.Color.blue())
        embed.add_field(name="Location", value=f"`{self.local_dir}`", inline=False)
        embed.add_field(name="Files", value=str(len(files)), inline=True)
        embed.add_field(name="Total size", value=self._format_size(total_size), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="musiclibrary", description="Browse a music library")
    @app_commands.describe(library="Library to browse", user="Member whose library to browse")
    @app_commands.choices(library=[
        app_commands.Choice(name="My Music", value="my"),
        app_commands.Choice(name="Public Library", value="public"),
        app_commands.Choice(name="Other User", value="user"),
    ])
    async def musiclibrary(
        self,
        interaction: discord.Interaction,
        library: app_commands.Choice[str],
        user: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if library.value == "my":
            directory = self._get_user_dir(interaction.user.id)
            title = f"{interaction.user.display_name}'s Library"
        elif library.value == "public":
            directory, title = self.public_dir, "Public Library"
        elif library.value == "user" and user:
            directory = self._get_user_dir(user.id, create=False)
            title = f"{user.display_name}'s Library"
        else:
            await interaction.followup.send("Choose a member for the Other User library.", ephemeral=True)
            return
        files = self._audio_files(directory)
        if not files:
            await interaction.followup.send(f"📁 {title}: no files found.", ephemeral=True)
            return
        total_size = sum(os.path.getsize(path) for _, path in files if os.path.exists(path))
        embed = discord.Embed(
            title=f"🎵 {title}",
            description=f"**{len(files)} files · {self._format_size(total_size)}**",
            color=discord.Color.purple(),
        )
        groups = (
            ("🏆 Lossless source", (".flac", ".wav")),
            ("💎 Higher bitrate formats", (".m4a", ".aac", ".opus")),
            ("🎵 Other formats", (".mp3", ".ogg", ".wma")),
        )
        for label, extensions in groups:
            names = [name for name, _ in files if Path(name).suffix.lower() in extensions]
            if names:
                shown = "\n".join(f"• `{name[:100]}`" for name in names[:8])
                if len(names) > 8:
                    shown += f"\n…and {len(names) - 8} more"
                embed.add_field(name=label, value=shown, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="musicupload", description="Show how to upload music to your library")
    async def musicupload(self, interaction: discord.Interaction):
        user_dir = self._get_user_dir(interaction.user.id)
        embed = discord.Embed(
            title="📤 Upload Your Music",
            description=f"Upload supported audio files to:\n`{user_dir}`",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="How",
            value="Use SFTP/SCP to copy files into that directory, then play them with `/mymusic <name>`.",
            inline=False,
        )
        embed.add_field(name="Formats", value="MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WMA", inline=False)
        embed.set_footer(text="Discord voice playback is encoded and cannot guarantee bit-perfect lossless audio.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="musicstats", description="Show music library statistics")
    async def musicstats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        personal = self._audio_files(self._get_user_dir(interaction.user.id))
        public = self._audio_files(self.public_dir)
        user_count = file_count = user_bytes = 0
        for directory in self.users_dir.iterdir():
            if directory.is_dir():
                items = self._audio_files(directory)
                if items:
                    user_count += 1
                    file_count += len(items)
                    user_bytes += sum(os.path.getsize(path) for _, path in items if os.path.exists(path))
        public_bytes = sum(os.path.getsize(path) for _, path in public if os.path.exists(path))
        personal_bytes = sum(os.path.getsize(path) for _, path in personal if os.path.exists(path))
        embed = discord.Embed(title="📊 Music Library Statistics", color=discord.Color.blue())
        embed.add_field(name="Your Library", value=f"{len(personal)} files · {self._format_size(personal_bytes)}", inline=True)
        embed.add_field(name="Public Library", value=f"{len(public)} files · {self._format_size(public_bytes)}", inline=True)
        embed.add_field(
            name="Server Total",
            value=f"{user_count} users · {file_count + len(public)} files · {self._format_size(user_bytes + public_bytes)}",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sharesong", description="Copy a song from your library to public")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sharesong(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer(ephemeral=True)
        match = await self._resolve_file_search(
            interaction, filename, self._audio_files(self._get_user_dir(interaction.user.id)), "your library"
        )
        if not match:
            return
        src_name, src = match
        destination = self.public_dir / Path(src_name).name
        try:
            await asyncio.to_thread(shutil.copy2, src, destination)
            await interaction.followup.send(f"✅ Shared `{src_name}` with the public library.", ephemeral=True)
        except OSError as exc:
            await interaction.followup.send(f"❌ Could not share that file: {exc}", ephemeral=True)

    @app_commands.command(name="streamstatus", description="Show current playback and queue status")
    async def streamstatus(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        embed = self._controller_embed(state)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="streamhelp", description="Show music upload and playback help")
    async def streamhelp(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎵 Music Help",
            description=(
                "`/play <YouTube URL/search>` · `/search <keywords>`\n"
                "`/streamupload <audio attachment>` · `/playlocal <filename>`\n"
                "`/mymusic <name>` · `/publicmusic <name>` · `/queue`\n\n"
                "Temporary uploads are removed when the bot leaves voice."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="Upload limit", value="100 MB; MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WMA", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── Queue and playback controls ─────────────────────────────────

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        state._skip_flag = True
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            state.is_playing = False
            await interaction.response.send_message("⏸️ Paused.")
            await self._refresh_controller(interaction.guild_id)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            state.is_playing = True
            self._cancel_inactivity_timer(interaction.guild_id)
            await interaction.response.send_message("▶️ Resumed.")
            await self._refresh_controller(interaction.guild_id)
        else:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        state.loop_mode = "off"
        state.current = None
        state.is_playing = False
        has_active_source = bool(
            state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused())
        )
        state._stop_flag = has_active_source
        if has_active_source:
            state.voice_client.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")
        await self._refresh_controller(interaction.guild_id)

    @app_commands.command(name="queue", description="View the current queue")
    async def queue_view(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        embed = self._controller_embed(state)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Remove a queued track by position")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild_id)
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(f"Invalid position; queue has {len(state.queue)} tracks.", ephemeral=True)
            return
        song = state.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Removed **{song.title}**.")
        await self._refresh_controller(interaction.guild_id)

    @app_commands.command(name="nowplaying", description="Show the current track")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._controller_embed(state))

    @app_commands.command(name="volume", description="Set volume from 0 to 200 percent")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
        state = self.get_state(interaction.guild_id)
        state.volume = level / 100
        source = state.voice_client.source if state.voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
        await self._refresh_controller(interaction.guild_id)

    @app_commands.command(name="loop", description="Set loop mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Single Track", value="single"),
        app_commands.Choice(name="Entire Queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = self.get_state(interaction.guild_id)
        state.loop_mode = mode.value
        await interaction.response.send_message(f"Loop mode: **{mode.name}**.")
        await self._refresh_controller(interaction.guild_id)

    @app_commands.command(name="disconnect", description="Disconnect and clear this voice session")
    async def disconnect(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message("Bot is not connected to voice.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._end_voice_session(interaction.guild_id)
        await interaction.followup.send("👋 Disconnected. Temporary uploads from this session were removed.")

    @app_commands.command(name="clearqueue", description="Clear queued tracks without stopping current playback")
    async def clearqueue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        count = len(state.queue)
        state.queue.clear()
        await interaction.response.send_message(f"🗑️ Cleared **{count}** queued tracks.")
        await self._refresh_controller(interaction.guild_id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.bot.user or member.id != self.bot.user.id or before.channel is None:
            return
        if after.channel is None:
            state = self.get_state(member.guild.id)
            if not state._ending_session:
                await self._end_voice_session(member.guild.id)
    async def cog_unload(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))

