"""High-performance, full-fledged music engine for Discord voice channels.
Supports YouTube, Spotify, SoundCloud, server-local audio, personal & public user libraries,
temporary direct uploads, persistent SQLite playlists, dedicated interactive music channel,
and real-time web dashboard integration.
"""

import asyncio
import json
import math
import os
import random
import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

from config import Config

# --- Engine Configuration ---
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

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".wma")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
TEMP_STALE_AFTER_SECONDS = 3600  # 1 hour purge for temporary uploads
SPOTIFY_EMBED_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def format_duration(seconds: int, is_elapsed: bool = False) -> str:
    """Formats an integer seconds count to M:SS or H:MM:SS."""
    if seconds is None or (seconds <= 0 and not is_elapsed):
        return "Live/Unknown"
    if seconds <= 0 and is_elapsed:
        return "0:00"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def make_progress_bar(elapsed: int, duration: int, length: int = 14) -> str:
    """Renders a sleek Spotify-style timeline progress bar."""
    if duration <= 0:
        return "🔘" + "▬" * (length - 1) + " `[Live Stream]`"
    progress = min(1.0, max(0.0, elapsed / duration))
    dot_pos = min(length - 1, int(progress * length))
    bar = ["▬"] * length
    bar[dot_pos] = "🔘"
    return f"{''.join(bar)} `{format_duration(elapsed, is_elapsed=True)} / {format_duration(duration)}`"


# =========================================================================
# Song & Audio Item Representation
# =========================================================================

class Song:
    """A single playable track from YouTube, Spotify, SoundCloud, or Local storage."""

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
        source_type: str = "youtube",
    ):
        self.source_url = source_url
        self.title = title
        self.duration = duration or 0
        self.thumbnail = thumbnail or ""
        self.webpage_url = webpage_url
        self.requester = requester
        self.is_local = is_local
        self.temporary = temporary
        self.source_type = source_type  # youtube, spotify, soundcloud, local, upload

        # Live playback progress tracking
        self.start_playback_time: Optional[float] = None
        self.pause_playback_time: Optional[float] = None
        self.total_paused_duration: float = 0.0

    def elapsed(self) -> int:
        """Returns the current elapsed playback time in seconds."""
        if self.start_playback_time is None:
            return 0
        if self.pause_playback_time is not None:
            el = self.pause_playback_time - self.start_playback_time - self.total_paused_duration
        else:
            el = time.time() - self.start_playback_time - self.total_paused_duration
        if self.duration > 0:
            return max(0, min(self.duration, int(el)))
        return max(0, int(el))

    def progress_bar(self, length: int = 14) -> str:
        return make_progress_bar(self.elapsed(), self.duration, length)

    @classmethod
    async def from_youtube(cls, query: str, requester: discord.abc.User, loop=None) -> List["Song"]:
        loop = loop or asyncio.get_running_loop()
        if "list=" in query and ("youtube.com" in query or "youtu.be" in query):
            return await cls._from_playlist(query, requester, loop)

        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if not data:
            raise ValueError("Could not extract audio from that YouTube URL or search.")

        if "entries" in data:
            entries = [entry for entry in data["entries"] if entry]
            if not entries:
                raise ValueError("No playable results found.")
            data = entries[0]

        return [cls(
            source_url=data.get("url") or data.get("webpage_url", query),
            title=data.get("title", "Unknown"),
            duration=data.get("duration") or 0,
            thumbnail=data.get("thumbnail", ""),
            webpage_url=data.get("webpage_url", query),
            requester=requester,
            source_type="youtube"
        )]

    @classmethod
    async def _from_playlist(cls, url: str, requester: discord.abc.User, loop) -> List["Song"]:
        playlist_ytdl = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "extract_flat": True})
        data = await loop.run_in_executor(None, lambda: playlist_ytdl.extract_info(url, download=False))
        if not data or "entries" not in data:
            raise ValueError("Could not read that playlist.")

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
                source_type="youtube"
            ))
        return songs

    @classmethod
    async def from_soundcloud(cls, url: str, requester: discord.abc.User, loop=None) -> List["Song"]:
        loop = loop or asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        if not data:
            raise ValueError("Could not read that SoundCloud track or set.")

        if "entries" in data:
            songs = []
            for entry in data["entries"]:
                if not entry:
                    continue
                songs.append(cls(
                    source_url=entry.get("url") or entry.get("webpage_url", url),
                    title=entry.get("title", "Unknown"),
                    duration=entry.get("duration") or 0,
                    thumbnail=entry.get("thumbnail", ""),
                    webpage_url=entry.get("webpage_url", url),
                    requester=requester,
                    source_type="soundcloud"
                ))
            return songs

        return [cls(
            source_url=data.get("url") or data.get("webpage_url", url),
            title=data.get("title", "Unknown"),
            duration=data.get("duration") or 0,
            thumbnail=data.get("thumbnail", ""),
            webpage_url=data.get("webpage_url", url),
            requester=requester,
            source_type="soundcloud"
        )]

    @classmethod
    async def from_spotify(cls, url: str, requester: discord.abc.User) -> List["Song"]:
        """Resolves Spotify track, playlist, or album links tokenlessly and maps them to YouTube."""
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            raise ValueError("Invalid Spotify link.")

        media_type = parts[0].lower()  # track, playlist, album
        media_id = parts[1].split("?")[0]

        async with aiohttp.ClientSession(headers=SPOTIFY_EMBED_HEADERS) as session:
            if media_type == "track":
                # Fetch oEmbed JSON
                oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
                async with session.get(oembed_url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "Unknown Spotify Track")
                        thumb = data.get("thumbnail_url", "")
                        return [cls(
                            source_url=f"ytsearch1:{title}",
                            title=title,
                            duration=0,
                            thumbnail=thumb,
                            webpage_url=url,
                            requester=requester,
                            source_type="spotify"
                        )]

            # For playlist or album: scrape the public embed HTML to extract track list
            embed_url = f"https://open.spotify.com/embed/{media_type}/{media_id}"
            async with session.get(embed_url, timeout=15) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
                    if match:
                        try:
                            payload = json.loads(match.group(1))
                            entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
                            raw_tracks = entity.get("trackList", [])
                            songs = []
                            for t in raw_tracks:
                                t_title = t.get("title", "").strip()
                                t_sub = t.get("subtitle", "").strip()
                                full_title = f"{t_title} - {t_sub}" if t_sub else t_title
                                dur = (t.get("duration") or 0) // 1000
                                songs.append(cls(
                                    source_url=f"ytsearch1:{full_title}",
                                    title=full_title,
                                    duration=dur,
                                    thumbnail="",
                                    webpage_url=url,
                                    requester=requester,
                                    source_type="spotify"
                                ))
                            if songs:
                                return songs
                        except Exception:
                            pass

        # Fallback to general YouTube search of URL
        return await cls.from_youtube(url, requester)

    @classmethod
    def from_local(cls, filepath: str, requester: discord.abc.User, temporary: bool = False) -> "Song":
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
            source_type="upload" if temporary else "local"
        )


# =========================================================================
# Guild State Representation
# =========================================================================

class GuildMusicState:
    """Complete audio, queue, history, and channel state for a single guild."""

    def __init__(self):
        self.queue: List[Song] = []
        self.history: List[Song] = []
        self.current: Optional[Song] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = 1.0
        self.loop_mode: str = "off"  # off, single, queue
        self.is_shuffled: bool = False
        self.is_playing: bool = False

        self._skip_flag: bool = False
        self._stop_flag: bool = False
        self._previous_flag: bool = False
        self._ending_session: bool = False
        self._inactivity_task: Optional[asyncio.Task] = None

        self.temp_files: set[str] = set()
        self.controller_message: Optional[discord.Message] = None
        self.dedicated_channel_id: Optional[int] = None
        self.dedicated_player_message: Optional[discord.Message] = None

    def reset(self):
        self.queue.clear()
        self.history.clear()
        self.current = None
        self.loop_mode = "off"
        self.is_shuffled = False
        self.is_playing = False
        self._skip_flag = False
        self._stop_flag = False
        self._previous_flag = False
        self.voice_client = None


# =========================================================================
# Interactive Discord Controller View
# =========================================================================

class MusicPlayerView(discord.ui.View):
    """Persistent, high-fidelity UI controller with full playback & library buttons."""

    def __init__(self, music: "Music"):
        super().__init__(timeout=None)
        self.music = music

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id or not interaction.guild:
            await interaction.response.send_message("❌ Music controls can only be used in a server.", ephemeral=True)
            return False
        allowed = await Config.check_member_cog_permission(interaction.user, "music")
        if not allowed:
            await interaction.response.send_message(
                "❌ You do not have permission to use the **Music** module.",
                ephemeral=True
            )
            return False
        return True

    # --- Row 1: Playback Navigation ---

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="music:prev", row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        if not state.history:
            await interaction.response.send_message("❌ No previous track in history.", ephemeral=True)
            return
        state._previous_flag = True
        prev_song = state.history.pop()
        if state.current:
            state.queue.insert(0, state.current)
        state.queue.insert(0, prev_song)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await interaction.response.send_message(f"⏮️ Playing previous track: **{prev_song.title}**", ephemeral=True)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music:toggle", row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        vc = state.voice_client
        if vc and vc.is_playing():
            vc.pause()
            state.is_playing = False
            if state.current:
                state.current.pause_playback_time = time.time()
            await interaction.response.send_message("⏸️ Playback paused.", ephemeral=True)
        elif vc and vc.is_paused():
            vc.resume()
            state.is_playing = True
            if state.current and state.current.pause_playback_time:
                state.current.total_paused_duration += time.time() - state.current.pause_playback_time
                state.current.pause_playback_time = None
            self.music._cancel_inactivity_timer(guild_id)
            await interaction.response.send_message("▶️ Playback resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)
            return
        await self.music.refresh_all_controllers(guild_id)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing to skip.", ephemeral=True)
            return
        state._skip_flag = True
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped current track.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        state.queue.clear()
        state.loop_mode = "off"
        state._stop_flag = True
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        state.current = None
        state.is_playing = False
        await interaction.response.send_message("⏹️ Playback stopped and queue cleared.", ephemeral=True)
        await self.music.refresh_all_controllers(guild_id)
        self.music._start_inactivity_timer(guild_id)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        if len(state.queue) < 2:
            await interaction.response.send_message("❌ Need at least 2 tracks in the queue to shuffle.", ephemeral=True)
            return
        random.shuffle(state.queue)
        state.is_shuffled = not state.is_shuffled
        await interaction.response.send_message(f"🔀 Shuffled **{len(state.queue)}** queued tracks.", ephemeral=True)
        await self.music.refresh_all_controllers(guild_id)

    # --- Row 2: Settings, Queue, & Library ---

    @discord.ui.button(emoji="🔁", label="Loop", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        modes = ["off", "single", "queue"]
        next_mode = modes[(modes.index(state.loop_mode) + 1) % len(modes)]
        state.loop_mode = next_mode
        labels = {"off": "Disabled (Off)", "single": "Repeat Current Track (Single)", "queue": "Repeat Entire Queue"}
        await interaction.response.send_message(f"🔁 Looping mode set to: **{labels[next_mode]}**", ephemeral=True)
        await self.music.refresh_all_controllers(guild_id)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="music:vol_down", row=1)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._change_volume(interaction, -10)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="music:vol_up", row=1)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._change_volume(interaction, 10)

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=1)
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        state = self.music.get_state(guild_id)
        embed = discord.Embed(title="📜 Active Audio Queue", color=discord.Color.purple())
        if state.current:
            embed.description = f"**Now Playing:** [{state.current.title}]({state.current.webpage_url})\n{state.current.progress_bar()}\n"
        else:
            embed.description = "*Nothing is currently playing.*\n"

        if not state.queue:
            embed.add_field(name="Up Next", value="Queue is empty.", inline=False)
        else:
            lines = []
            total_sec = sum(s.duration for s in state.queue)
            for idx, s in enumerate(state.queue[:15], 1):
                lines.append(f"`{idx}.` [{s.title[:80]}]({s.webpage_url}) (`{format_duration(s.duration)}`) • *{s.requester}*")
            if len(state.queue) > 15:
                lines.append(f"… and **{len(state.queue) - 15}** more")
            embed.add_field(name=f"Up Next ({len(state.queue)} tracks · ~{format_duration(total_sec)})", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="📚", label="Library", style=discord.ButtonStyle.secondary, custom_id="music:library", row=1)
    async def library_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📚 Music Library & Quick Actions",
            description=(
                "Access saved libraries or playlists directly with slash commands:\n\n"
                "• `/mymusic <query>` — Play from your personal library\n"
                "• `/publicmusic <query>` — Play from the shared community library\n"
                "• `/playlocal <filename>` — Play from server music folder\n"
                "• `/playlist play <name>` — Load a custom saved playlist\n"
                "• `/playlist list` — View all server playlists"
            ),
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================================================================
# Interactive Playlist UI Components (Modal, Select Dropdowns, Actions View)
# =========================================================================

class AddSongModal(discord.ui.Modal):
    def __init__(self, cog, playlist_name: str):
        super().__init__(title=f"Add to {playlist_name[:30]}")
        self.cog = cog
        self.playlist_name = playlist_name
        self.query_input = discord.ui.TextInput(
            label="Song Title, YouTube, Spotify, or SoundCloud",
            placeholder="Paste song URL or search keywords...",
            min_length=2,
            max_length=400,
            required=True
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.query_input.value.strip()
        source_type = "url"
        if "spotify.com" in query:
            source_type = "spotify"
        elif "soundcloud.com" in query:
            source_type = "soundcloud"
        elif "youtube.com" in query or "youtu.be" in query:
            source_type = "youtube"

        added = await Config.add_track_to_playlist(
            interaction.guild_id, self.playlist_name, query, query, source_type
        )
        if added:
            embed = discord.Embed(
                title="✅ Track Added to Playlist",
                description=f"Added **{query}** to playlist **{self.playlist_name}**.",
                color=discord.Color.green()
            )
            embed.set_footer(text=f"Use /playlist view {self.playlist_name} to view all tracks")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Failed to add track to playlist **{self.playlist_name}**.",
                ephemeral=True
            )


class PlaylistSelect(discord.ui.Select):
    def __init__(self, cog, playlists: List[Dict[str, Any]], query: Optional[str] = None, mode: str = "add"):
        self.cog = cog
        self.query = query
        self.mode = mode
        options = []
        for p in playlists[:25]:
            count = p.get("track_count", 0)
            desc = f"{count} track{'s' if count != 1 else ''} • by {p.get('created_by_name', 'Unknown')}"
            options.append(discord.SelectOption(
                label=p["name"][:100],
                description=desc[:100],
                emoji="🎵",
                value=p["name"]
            ))

        placeholder = "📂 Select a playlist from this server..."
        if mode == "play":
            placeholder = "▶️ Select a playlist to play in voice..."
        elif mode == "view":
            placeholder = "👁️ Select a playlist to inspect..."
        elif mode == "add":
            placeholder = "➕ Select a playlist to add track to..."

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        playlist_name = self.values[0]

        if self.mode == "add":
            if self.query:
                await interaction.response.defer()
                source_type = "url"
                if "spotify.com" in self.query:
                    source_type = "spotify"
                elif "soundcloud.com" in self.query:
                    source_type = "soundcloud"
                elif "youtube.com" in self.query or "youtu.be" in self.query:
                    source_type = "youtube"

                added = await Config.add_track_to_playlist(
                    interaction.guild_id, playlist_name, self.query, self.query, source_type
                )
                if added:
                    embed = discord.Embed(
                        title="✅ Track Added to Playlist",
                        description=f"Added **{self.query}** to playlist **{playlist_name}**.",
                        color=discord.Color.green()
                    )
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send(f"❌ Playlist **{playlist_name}** was not found.", ephemeral=True)
            else:
                modal = AddSongModal(self.cog, playlist_name)
                await interaction.response.send_modal(modal)

        elif self.mode == "play":
            await self.cog._play_playlist_by_name(interaction, playlist_name)

        elif self.mode == "view":
            await self.cog._show_playlist_by_name(interaction, playlist_name)


class PlaylistSelectView(discord.ui.View):
    def __init__(self, cog, playlists: List[Dict[str, Any]], query: Optional[str] = None, mode: str = "add"):
        super().__init__(timeout=120)
        self.add_item(PlaylistSelect(cog, playlists, query=query, mode=mode))


class PlaylistActionsView(discord.ui.View):
    def __init__(self, cog, playlist_name: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.playlist_name = playlist_name

    @discord.ui.button(label="Play Playlist", style=discord.ButtonStyle.success, emoji="▶️")
    async def play_playlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._play_playlist_by_name(interaction, self.playlist_name)

    @discord.ui.button(label="Add Song", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddSongModal(self.cog, self.playlist_name)
        await interaction.response.send_modal(modal)


# =========================================================================
# Main Music Cog
# =========================================================================

class Music(commands.Cog):
    """Unified high-fidelity music player with dedicated channel & multi-source support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: Dict[int, GuildMusicState] = {}

        # Directory layout
        self.music_root = Path(__file__).resolve().parent.parent / "music"
        self.local_dir = self.music_root
        self.public_dir = self.music_root / "public"
        self.users_dir = self.music_root / "users"
        self.temp_dir = Path(__file__).resolve().parent.parent / "temp_music"
        for d in (self.local_dir, self.public_dir, self.users_dir, self.temp_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Register persistent view
        self.bot.add_view(MusicPlayerView(self))
        self.cleanup_task = asyncio.create_task(self._cleanup_stale_temp_files())
        self.ensure_players_task = asyncio.create_task(self._ensure_dedicated_channel_players())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        allowed = await Config.check_member_cog_permission(interaction.user, "music")
        if not allowed:
            await interaction.response.send_message(
                "❌ You do not have permission to use the **Music** module.",
                ephemeral=True
            )
            return False
        return True

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    def _get_user_dir(self, user_id: int, create: bool = True) -> Path:
        d = self.users_dir / str(user_id)
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _audio_files(self, directory: Path) -> List[Tuple[str, str]]:
        if not directory.is_dir():
            return []
        items = []
        for path in directory.iterdir():
            try:
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                    items.append((path.name, str(path)))
            except OSError:
                continue
        return sorted(items, key=lambda x: x[0].lower())

    @staticmethod
    def _search_files(query: str, files: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        query = query.casefold().strip()
        if not query:
            return []
        for filename, path in files:
            if filename.casefold() == query or Path(filename).stem.casefold() == query:
                return [(filename, path)]
        return [(filename, path) for filename, path in files if query in filename.casefold()]

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    # =========================================================================
    # Bulletproof Voice Connection & In-Use Protection
    # =========================================================================

    async def _ensure_voice(
        self,
        interaction_or_msg: Any,
        force_move: bool = False
    ) -> Tuple[Optional[discord.VoiceClient], Optional[str]]:
        """
        Connects or retrieves voice client safely with In-Use protection.
        Fixes ClientException: Already connected to a voice channel.
        """
        guild = getattr(interaction_or_msg, "guild", None)
        author = getattr(interaction_or_msg, "user", None) or getattr(interaction_or_msg, "author", None)
        if not guild or not author:
            return None, "❌ This command can only be used in a server."

        voice_state = getattr(author, "voice", None)
        target_channel = voice_state.channel if voice_state else None
        if target_channel is None:
            return None, "❌ You must join a voice channel first!"

        state = self.get_state(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client

        # 1. Existing voice connection check
        if vc is not None:
            state.voice_client = vc
            if vc.is_connected():
                # In-Use Protection: is the bot in a DIFFERENT channel with active listeners?
                if vc.channel.id != target_channel.id:
                    active_listeners = [m for m in vc.channel.members if not m.bot]
                    if len(active_listeners) > 0 and state.is_playing and not force_move:
                        return None, (
                            f"❌ I am currently playing music in **{vc.channel.name}** with "
                            f"{len(active_listeners)} listener(s). Please join **{vc.channel.name}**!"
                        )
                    # Safe to move
                    try:
                        await vc.move_to(target_channel)
                    except Exception as exc:
                        print(f"[Music] Error moving voice channel: {exc}")
                return vc, None
            else:
                # Ghost/half-closed connection: clean up before reconnecting
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.3)

        # 2. Fresh connection with defensive ClientException trap
        try:
            vc = await target_channel.connect(self_deaf=True, timeout=15.0)
            state.voice_client = vc
            return vc, None
        except discord.ClientException:
            # Already connected in Discord's internal registry -> recover it
            recovered_vc = guild.voice_client
            if recovered_vc and recovered_vc.is_connected():
                state.voice_client = recovered_vc
                if recovered_vc.channel.id != target_channel.id:
                    try:
                        await recovered_vc.move_to(target_channel)
                    except Exception:
                        pass
                return recovered_vc, None
            # Force disconnect lingering state and retry
            if recovered_vc:
                try:
                    await recovered_vc.disconnect(force=True)
                    await asyncio.sleep(0.4)
                    vc = await target_channel.connect(self_deaf=True, timeout=15.0)
                    state.voice_client = vc
                    return vc, None
                except Exception as e2:
                    return None, f"❌ Voice connection error: {e2}"
            return None, "❌ Voice client reported already connected but socket was dead."
        except asyncio.TimeoutError:
            return None, "❌ Voice connection timed out. Please check bot permissions in the channel."
        except Exception as exc:
            return None, f"❌ Could not connect to voice channel: {exc}"

    # =========================================================================
    # High-Fidelity UI Controller Embed & Live Render
    # =========================================================================

    def _build_player_embed(self, guild_id: int) -> discord.Embed:
        state = self.get_state(guild_id)
        song = state.current

        source_icons = {
            "youtube": "🔴 YouTube",
            "spotify": "🟢 Spotify",
            "soundcloud": "🟠 SoundCloud",
            "local": "📁 Local Server",
            "upload": "📤 Direct Upload",
        }

        if song:
            embed = discord.Embed(
                title=f"🎵 {song.title[:240]}",
                url=song.webpage_url if song.webpage_url.startswith("http") else None,
                color=discord.Color.from_rgb(147, 51, 234)
            )
            embed.description = f"**Timeline**\n{song.progress_bar(15)}\n"

            status_str = "⏸️ Paused" if state.voice_client and state.voice_client.is_paused() else "▶️ Playing"
            loop_labels = {"off": "Off", "single": "🔂 Track", "queue": "🔁 Queue"}
            source_label = source_icons.get(song.source_type, "🎵 Audio")

            embed.add_field(name="Status", value=status_str, inline=True)
            embed.add_field(name="Volume", value=f"🔊 {int(state.volume * 100)}%", inline=True)
            embed.add_field(name="Looping", value=loop_labels.get(state.loop_mode, "Off"), inline=True)

            embed.add_field(name="Source", value=source_label, inline=True)
            embed.add_field(name="Requester", value=song.requester.mention, inline=True)
            embed.add_field(name="Mode", value="🔀 Shuffled" if state.is_shuffled else "➡️ Linear", inline=True)

            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)

            # Up next preview
            if state.queue:
                queue_lines = [
                    f"`{i}.` [{item.title[:65]}]({item.webpage_url}) (`{format_duration(item.duration)}`)"
                    for i, item in enumerate(state.queue[:3], 1)
                ]
                if len(state.queue) > 3:
                    queue_lines.append(f"*… and {len(state.queue) - 3} more*")
                embed.add_field(name=f"Up Next ({len(state.queue)})", value="\n".join(queue_lines), inline=False)
            else:
                embed.add_field(name="Up Next", value="*Queue is empty*", inline=False)

            embed.set_footer(text="ARC High-Fidelity Audio Engine • Use controls below")
            return embed

        # Idle / Stopped state
        embed = discord.Embed(
            title="🎵 ARC Music Player",
            description=(
                "**No audio is currently playing.**\n\n"
                "• Type a song name or paste a **YouTube**, **Spotify**, or **SoundCloud** link\n"
                "• Use `/play <query>` from any channel\n"
                "• Click **Library** below to browse saved tracks or playlists"
            ),
            color=discord.Color.dark_theme()
        )
        embed.set_footer(text="Dedicated Music Channel • Type here to play directly")
        return embed

    async def refresh_all_controllers(self, guild_id: int, channel=None):
        """Updates both the Now Playing command message and the Dedicated Music Channel message."""
        state = self.get_state(guild_id)
        embed = self._build_player_embed(guild_id)
        view = MusicPlayerView(self)

        # 1. Update ephemeral / command controller message if active
        if state.controller_message:
            try:
                await state.controller_message.edit(embed=embed, view=view)
            except discord.NotFound:
                state.controller_message = None
            except discord.HTTPException:
                pass

        # 2. Update dedicated music channel pinned player
        ded_channel_id = await Config.get_music_channel(guild_id)
        if ded_channel_id:
            ded_msg_id = await Config.get_music_player_message_id(guild_id)
            guild = self.bot.get_guild(guild_id)
            if guild:
                ded_channel = guild.get_channel(ded_channel_id)
                if ded_channel and isinstance(ded_channel, discord.TextChannel):
                    if ded_msg_id:
                        try:
                            msg = await ded_channel.fetch_message(ded_msg_id)
                            if not msg.pinned:
                                try:
                                    await msg.pin(reason="Pinned active ARC music player banner")
                                    await asyncio.sleep(0.4)
                                    async for sys_m in ded_channel.history(limit=5):
                                        if sys_m.type == discord.MessageType.pins_add:
                                            try:
                                                await sys_m.delete()
                                            except discord.HTTPException:
                                                pass
                                except discord.HTTPException:
                                    pass
                            await msg.edit(embed=embed, view=view)
                            return
                        except (discord.NotFound, discord.HTTPException):
                            pass
                    # If message not found, create new pinned message
                    try:
                        new_msg = await ded_channel.send(embed=embed, view=view)
                        try:
                            await new_msg.pin(reason="Pinned active ARC music player banner")
                            await asyncio.sleep(0.4)
                            async for sys_m in ded_channel.history(limit=5):
                                if sys_m.type == discord.MessageType.pins_add:
                                    try:
                                        await sys_m.delete()
                                    except discord.HTTPException:
                                        pass
                        except discord.HTTPException:
                            pass
                        await Config.set_music_player_message_id(guild_id, new_msg.id)
                    except discord.HTTPException:
                        pass

    async def _change_volume(self, interaction: discord.Interaction, delta: int):
        guild_id = interaction.guild_id
        state = self.get_state(guild_id)
        state.volume = max(0.0, min(2.0, state.volume + delta / 100))
        source = state.voice_client.source if state.voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to: **{int(state.volume * 100)}%**", ephemeral=True)
        await self.refresh_all_controllers(guild_id)

    # =========================================================================
    # Playback Pipeline
    # =========================================================================

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
            await self.refresh_all_controllers(guild_id)
            self._start_inactivity_timer(guild_id)
            return

        was_skipped = state._skip_flag
        state._skip_flag = False

        # Loop logic
        if not was_skipped and state.loop_mode == "single" and state.current:
            song = state.current
        else:
            if state.current and not state._previous_flag:
                state.history.append(state.current)
                if len(state.history) > 50:
                    state.history.pop(0)

            state._previous_flag = False

            if not was_skipped and state.loop_mode == "queue" and state.current:
                state.queue.append(state.current)

            if not state.queue:
                state.current = None
                state.is_playing = False
                await self.refresh_all_controllers(guild_id)
                self._start_inactivity_timer(guild_id)
                return

            song = state.queue.pop(0)
            state.current = song

        # Reset playback timer
        song.start_playback_time = time.time()
        song.pause_playback_time = None
        song.total_paused_duration = 0.0

        try:
            if song.is_local:
                source = discord.FFmpegPCMAudio(song.source_url)
                source = discord.PCMVolumeTransformer(source, volume=state.volume)
            else:
                # Resolve stream URL if search query or expired
                if not song.source_url.startswith("http") or "googlevideo" not in song.source_url:
                    query = song.source_url if song.source_url.startswith("ytsearch") else (song.webpage_url or song.source_url)
                    data = await self.bot.loop.run_in_executor(
                        None, lambda: ytdl.extract_info(query, download=False)
                    )
                    if not data:
                        raise ValueError("Could not resolve audio stream.")
                    if "entries" in data and data["entries"]:
                        data = data["entries"][0]
                    song.source_url = data.get("url", song.source_url)
                    song.title = data.get("title", song.title)
                    song.duration = data.get("duration") or song.duration
                    song.thumbnail = data.get("thumbnail", song.thumbnail)

                source = discord.FFmpegPCMAudio(
                    song.source_url,
                    before_options=FFMPEG_OPTIONS["before_options"],
                    options=FFMPEG_OPTIONS["options"]
                )
                source = discord.PCMVolumeTransformer(source, volume=state.volume)

            state.is_playing = True
            self._cancel_inactivity_timer(guild_id)
            await self.refresh_all_controllers(guild_id)

            def after_playing(error):
                if error:
                    print(f"[Music] Playback error in guild {guild_id}: {error}")
                if not self.bot.is_closed():
                    asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)

            vc.play(source, after=after_playing)
        except Exception as exc:
            print(f"[Music] Playback exception for '{song.title}': {exc}")
            state.is_playing = False
            await self._play_next(guild_id)

    # =========================================================================
    # Inactivity & Session Management
    # =========================================================================

    def _start_inactivity_timer(self, guild_id: int):
        self._cancel_inactivity_timer(guild_id)
        state = self.get_state(guild_id)

        async def disconnect_after_timeout():
            try:
                await asyncio.sleep(180)  # 3 minutes smart idle timeout
                if state.voice_client and state.voice_client.is_connected() and not state.is_playing and not state.queue:
                    await self._end_voice_session(guild_id)
            except asyncio.CancelledError:
                pass

        state._inactivity_task = asyncio.create_task(disconnect_after_timeout())

    def _cancel_inactivity_timer(self, guild_id: int):
        state = self.get_state(guild_id)
        if state._inactivity_task and not state._inactivity_task.done():
            state._inactivity_task.cancel()
        state._inactivity_task = None

    async def _cleanup_temp_files(self, guild_id: int):
        state = self.get_state(guild_id)
        paths = tuple(state.temp_files)
        for path in paths:
            try:
                await asyncio.to_thread(os.remove, path)
                state.temp_files.discard(path)
            except (FileNotFoundError, OSError):
                state.temp_files.discard(path)

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
                except Exception:
                    pass
            state.reset()
            await self._cleanup_temp_files(guild_id)
            await self.refresh_all_controllers(guild_id)
        finally:
            state._ending_session = False

    async def _cleanup_stale_temp_files(self):
        """Purges old uploaded audio files older than 1 hour in background."""
        while not self.bot.is_closed():
            try:
                cutoff = time.time() - TEMP_STALE_AFTER_SECONDS
                active_paths = {
                    os.path.abspath(p)
                    for s in self.states.values()
                    for p in s.temp_files
                }
                for path in self.temp_dir.iterdir():
                    try:
                        if (
                            path.is_file()
                            and os.path.abspath(path) not in active_paths
                            and path.stat().st_mtime < cutoff
                        ):
                            await asyncio.to_thread(path.unlink)
                    except (FileNotFoundError, OSError):
                        continue
            except Exception:
                pass
            await asyncio.sleep(300)

    async def _ensure_dedicated_channel_players(self):
        """Ensures that all configured dedicated music channels have an active pinned player embed on bot ready."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            while not self.bot.is_ready():
                if self.bot.is_closed():
                    return
                await asyncio.sleep(1)

        await asyncio.sleep(1.0)

        for guild in self.bot.guilds:
            try:
                chan_id = await Config.get_music_channel(guild.id)
                if not chan_id:
                    continue
                chan = guild.get_channel(chan_id)
                if not chan or not isinstance(chan, discord.TextChannel):
                    continue

                msg_id = await Config.get_music_player_message_id(guild.id)
                msg = None
                if msg_id:
                    try:
                        msg = await chan.fetch_message(msg_id)
                    except (discord.NotFound, discord.HTTPException):
                        msg = None

                embed = self._build_player_embed(guild.id)
                view = MusicPlayerView(self)

                if msg is None:
                    # Clean any non-pinned leftover bot/user messages to keep channel clean
                    try:
                        await chan.purge(limit=25, check=lambda m: not m.pinned)
                    except Exception:
                        pass
                    msg = await chan.send(embed=embed, view=view)
                    try:
                        await msg.pin(reason="Pinned active ARC music player banner")
                        await asyncio.sleep(0.5)
                        async for sys_m in chan.history(limit=5):
                            if sys_m.type == discord.MessageType.pins_add:
                                try:
                                    await sys_m.delete()
                                except discord.HTTPException:
                                    pass
                    except discord.HTTPException:
                        pass
                    await Config.set_music_player_message_id(guild.id, msg.id)
                else:
                    if not msg.pinned:
                        try:
                            await msg.pin(reason="Re-pinned active ARC music player banner")
                            await asyncio.sleep(0.5)
                            async for sys_m in chan.history(limit=5):
                                if sys_m.type == discord.MessageType.pins_add:
                                    try:
                                        await sys_m.delete()
                                    except discord.HTTPException:
                                        pass
                        except discord.HTTPException:
                            pass
                    await msg.edit(embed=embed, view=view)
            except Exception as exc:
                print(f"[Music] Error ensuring player in guild {guild.id}: {exc}")

    # =========================================================================
    # Auto-Play Text Channel Listener
    # =========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-Play listener for the dedicated music channel. Removes user links immediately upon detection."""
        if not message.guild:
            return

        ded_channel_id = await Config.get_music_channel(message.guild.id)
        if not ded_channel_id or message.channel.id != ded_channel_id:
            return

        # 1. Automatically delete Discord system pin notifications
        if message.type == discord.MessageType.pins_add:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

        # Ignore bot messages (like our player embed)
        if message.author.bot:
            return

        # 2. IMMEDIATELY delete user message as soon as detected so channel stays pristine
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        content = message.content.strip()

        # Strip any prefix commands typed as text, e.g. /play <url>, !play <url>, play <url>, p <url>
        for pfx in ("/play ", "!play ", "-play ", ";play ", ".play ", "?play ", "play ", "p "):
            if content.lower().startswith(pfx):
                content = content[len(pfx):].strip()
                break

        # Strip surrounding angle brackets if link was wrapped like <https://...>
        if content.startswith("<") and content.endswith(">"):
            content = content[1:-1].strip()

        # Check for direct audio attachment
        attachment = None
        if message.attachments:
            att = message.attachments[0]
            if Path(att.filename).suffix.lower() in AUDIO_EXTENSIONS:
                attachment = att

        if not content and not attachment:
            return

        # Check voice
        vc, err = await self._ensure_voice(message)
        if err or not vc:
            try:
                await message.channel.send(
                    f"{message.author.mention} {err or '❌ Join a voice channel first to play music!'}",
                    delete_after=4.0
                )
            except discord.HTTPException:
                pass
            return

        state = self.get_state(message.guild.id)

        # Handle file attachment
        if attachment:
            if attachment.size > MAX_UPLOAD_BYTES:
                try:
                    await message.channel.send(
                        f"{message.author.mention} ❌ File is too large (max 100 MB).",
                        delete_after=4.0
                    )
                except discord.HTTPException:
                    pass
                return

            safe_name = Path(attachment.filename).name
            temp_path = self.temp_dir / f"{message.guild.id}_{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
            try:
                await attachment.save(temp_path)
                song = Song.from_local(str(temp_path), message.author, temporary=True)
                state.temp_files.add(os.path.abspath(str(temp_path)))
                state.queue.append(song)
                try:
                    await message.channel.send(f"📤 Queued **{song.title}**!", delete_after=4.0)
                except discord.HTTPException:
                    pass
                await self.refresh_all_controllers(message.guild.id)
                if not state.is_playing and state.current is None:
                    await self._play_next(message.guild.id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                except Exception:
                    pass
                try:
                    await message.channel.send(f"{message.author.mention} ❌ Upload error: `{exc}`", delete_after=4.0)
                except discord.HTTPException:
                    pass
            return

        # Resolve query / links
        try:
            url_match = re.search(r"https?://[^\s<>]+", content)
            target_url = url_match.group(0) if url_match else content

            if "spotify.com" in target_url:
                songs = await Song.from_spotify(target_url, message.author)
            elif "soundcloud.com" in target_url:
                songs = await Song.from_soundcloud(target_url, message.author, self.bot.loop)
            elif target_url.startswith(("http://", "https://")):
                songs = await Song.from_youtube(target_url, message.author, self.bot.loop)
            else:
                songs = await Song.from_youtube(f"ytsearch:{content}", message.author, self.bot.loop)
        except Exception as exc:
            try:
                await message.channel.send(f"{message.author.mention} ❌ Could not load track: `{exc}`", delete_after=4.0)
            except discord.HTTPException:
                pass
            return

        if not songs:
            try:
                await message.channel.send(f"{message.author.mention} ❌ No playable audio found.", delete_after=4.0)
            except discord.HTTPException:
                pass
            return

        state.queue.extend(songs)
        try:
            if len(songs) == 1:
                await message.channel.send(f"🎵 Added **{songs[0].title}** to queue!", delete_after=4.0)
            else:
                await message.channel.send(f"🎵 Added **{len(songs)}** tracks to queue!", delete_after=4.0)
        except discord.HTTPException:
            pass

        await self.refresh_all_controllers(message.guild.id)
        if not state.is_playing and state.current is None:
            await self._play_next(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Monitors voice client and disconnects on inactivity or empty channel."""
        if not self.bot.user:
            return

        guild = member.guild
        state = self.get_state(guild.id)
        vc = state.voice_client or guild.voice_client

        # If bot itself was disconnected
        if member.id == self.bot.user.id and after.channel is None:
            if not state._ending_session:
                await self._end_voice_session(guild.id)
            return

        # If bot is connected, check if voice channel is now empty of humans
        if vc and vc.is_connected() and vc.channel:
            human_listeners = [m for m in vc.channel.members if not m.bot]
            if len(human_listeners) == 0:
                self._start_inactivity_timer(guild.id)
            else:
                if state.is_playing:
                    self._cancel_inactivity_timer(guild.id)

    # =========================================================================
    # Autocomplete Handlers
    # =========================================================================

    async def _autocomplete_local_files(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        files = self._audio_files(self.local_dir)
        cur = current.casefold()
        matches = [name for name, _ in files if cur in name.casefold()][:25]
        return [app_commands.Choice(name=m[:100], value=m) for m in matches]

    async def _autocomplete_my_files(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        files = self._audio_files(self._get_user_dir(interaction.user.id))
        cur = current.casefold()
        matches = [name for name, _ in files if cur in name.casefold()][:25]
        return [app_commands.Choice(name=m[:100], value=m) for m in matches]

    async def _autocomplete_public_files(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        files = self._audio_files(self.public_dir)
        cur = current.casefold()
        matches = [name for name, _ in files if cur in name.casefold()][:25]
        return [app_commands.Choice(name=m[:100], value=m) for m in matches]

    async def _autocomplete_playlists(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        playlists = await Config.list_playlists(interaction.guild_id)
        cur = current.casefold()
        matches = [p["name"] for p in playlists if cur in p["name"].casefold()][:25]
        return [app_commands.Choice(name=m[:100], value=m) for m in matches]

    # =========================================================================
    # Playback Slash Commands
    # =========================================================================

    @app_commands.command(name="play", description="Play a YouTube, Spotify, SoundCloud URL or search query")
    @app_commands.describe(query="Song title, YouTube URL, Spotify link, or SoundCloud link", force="Override in-use channel check")
    async def play(self, interaction: discord.Interaction, query: str, force: bool = False):
        await interaction.response.defer()
        vc, err = await self._ensure_voice(interaction, force_move=force)
        if err or not vc:
            await interaction.followup.send(err or "❌ Failed to connect to voice channel.", ephemeral=True)
            return

        try:
            if "spotify.com" in query:
                songs = await Song.from_spotify(query, interaction.user)
            elif "soundcloud.com" in query:
                songs = await Song.from_soundcloud(query, interaction.user, self.bot.loop)
            elif query.startswith(("http://", "https://")):
                songs = await Song.from_youtube(query, interaction.user, self.bot.loop)
            else:
                songs = await Song.from_youtube(f"ytsearch:{query}", interaction.user, self.bot.loop)
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not load audio: {exc}", ephemeral=True)
            return

        if not songs:
            await interaction.followup.send("❌ No playable audio found for that query.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        state.queue.extend(songs)

        if len(songs) == 1:
            song = songs[0]
            embed = discord.Embed(title="🎵 Added to Queue", description=f"**[{song.title}]({song.webpage_url})**", color=discord.Color.purple())
            embed.add_field(name="Duration", value=format_duration(song.duration), inline=True)
            embed.add_field(name="Queue Position", value=f"#{len(state.queue)}", inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
        else:
            embed = discord.Embed(
                title="🎵 Playlist Added to Queue",
                description=f"Enqueued **{len(songs)}** tracks into the playlist.",
                color=discord.Color.purple()
            )

        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed)
        await self.refresh_all_controllers(interaction.guild_id, interaction.channel)

        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    @app_commands.command(name="search", description="Search YouTube and select from top results")
    @app_commands.describe(query="Keywords to search")
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
                    description=f"Duration: {format_duration(entry.get('duration') or 0)}",
                )
                for index, entry in enumerate(entries, 1)
            ]
            embed = discord.Embed(title=f"🔍 Search Results for: {query[:200]}", color=discord.Color.purple())
            for index, entry in enumerate(entries, 1):
                embed.add_field(
                    name=f"{index}. {entry.get('title', 'Unknown')[:200]}",
                    value=f"Duration: {format_duration(entry.get('duration') or 0)}",
                    inline=False,
                )
            select = discord.ui.Select(placeholder="Choose a track to play", options=options)

            async def select_callback(select_interaction: discord.Interaction):
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
                vc, err = await self._ensure_voice(select_interaction)
                if err or not vc:
                    await select_interaction.followup.send(err or "Voice connection error.", ephemeral=True)
                    return
                state = self.get_state(select_interaction.guild_id)
                state.queue.append(song)
                await select_interaction.followup.send(f"🎵 Added **{song.title}** to the queue.")
                await self.refresh_all_controllers(select_interaction.guild_id)
                if not state.is_playing and state.current is None:
                    await self._play_next(select_interaction.guild_id)

            select.callback = select_callback
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            await interaction.followup.send(embed=embed, view=view)
        except Exception as exc:
            await interaction.followup.send(f"❌ Search error: {exc}", ephemeral=True)

    @app_commands.command(name="playlocal", description="Play a file from the server music directory")
    @app_commands.describe(filename="Audio file name")
    @app_commands.autocomplete(filename=_autocomplete_local_files)
    async def playlocal(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer()
        files = self._audio_files(self.local_dir)
        matches = self._search_files(filename, files)
        if not matches:
            await interaction.followup.send(f"❌ Could not find `{filename}` in server library.", ephemeral=True)
            return
        match = matches[0]
        vc, err = await self._ensure_voice(interaction)
        if err or not vc:
            await interaction.followup.send(err or "Voice error.", ephemeral=True)
            return
        state = self.get_state(interaction.guild_id)
        song = Song.from_local(match[1], interaction.user)
        state.queue.append(song)
        embed = discord.Embed(title="🎵 Added Local Track", description=f"**{song.title}**", color=discord.Color.purple())
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed)
        await self.refresh_all_controllers(interaction.guild_id)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    @app_commands.command(name="mymusic", description="Play a song from your personal user library")
    @app_commands.describe(song="File name in your library")
    @app_commands.autocomplete(song=_autocomplete_my_files)
    async def mymusic(self, interaction: discord.Interaction, song: str):
        await interaction.response.defer(ephemeral=True)
        files = self._audio_files(self._get_user_dir(interaction.user.id))
        matches = self._search_files(song, files)
        if not matches:
            await interaction.followup.send(f"❌ `{song}` not found in your personal library.", ephemeral=True)
            return
        match = matches[0]
        vc, err = await self._ensure_voice(interaction)
        if err or not vc:
            await interaction.followup.send(err or "Voice error.", ephemeral=True)
            return
        state = self.get_state(interaction.guild_id)
        item = Song.from_local(match[1], interaction.user)
        state.queue.append(item)
        await interaction.followup.send(f"🎵 Added **{item.title}** from your library to the queue.", ephemeral=True)
        await self.refresh_all_controllers(interaction.guild_id)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    @app_commands.command(name="publicmusic", description="Play a track from the community public library")
    @app_commands.describe(song="File name in the public library")
    @app_commands.autocomplete(song=_autocomplete_public_files)
    async def publicmusic(self, interaction: discord.Interaction, song: str):
        await interaction.response.defer()
        files = self._audio_files(self.public_dir)
        matches = self._search_files(song, files)
        if not matches:
            await interaction.followup.send(f"❌ `{song}` not found in public library.", ephemeral=True)
            return
        match = matches[0]
        vc, err = await self._ensure_voice(interaction)
        if err or not vc:
            await interaction.followup.send(err or "Voice error.", ephemeral=True)
            return
        state = self.get_state(interaction.guild_id)
        item = Song.from_local(match[1], interaction.user)
        state.queue.append(item)
        await interaction.followup.send(f"🎵 Added **{item.title}** from public library to queue.")
        await self.refresh_all_controllers(interaction.guild_id)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    @app_commands.command(name="streamupload", description="Directly upload and queue an audio file (up to 100 MB)")
    async def streamupload(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer()
        safe_name = Path(file.filename).name
        if Path(safe_name).suffix.lower() not in AUDIO_EXTENSIONS:
            await interaction.followup.send("❌ Unsupported audio type. Supported: MP3, WAV, OGG, M4A, FLAC, AAC, OPUS, WMA.", ephemeral=True)
            return
        if file.size > MAX_UPLOAD_BYTES:
            await interaction.followup.send(f"❌ File is {file.size / (1024 * 1024):.1f} MB (bot limit: 100 MB).", ephemeral=True)
            return

        temp_path = self.temp_dir / f"{interaction.guild_id}_{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
        try:
            await file.save(temp_path)
            vc, err = await self._ensure_voice(interaction)
            if err or not vc:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                await interaction.followup.send(err or "Voice connection error.", ephemeral=True)
                return

            state = self.get_state(interaction.guild_id)
            song = Song.from_local(str(temp_path), interaction.user, temporary=True)
            state.temp_files.add(os.path.abspath(str(temp_path)))
            state.queue.append(song)

            embed = discord.Embed(title="📤 Direct Audio Stream Queued", description=f"**{song.title}**", color=discord.Color.purple())
            embed.add_field(name="Size", value=self._format_size(file.size), inline=True)
            embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
            embed.set_footer(text="File will be cleaned up when voice session concludes.")
            await interaction.followup.send(embed=embed)
            await self.refresh_all_controllers(interaction.guild_id)

            if not state.is_playing and state.current is None:
                await self._play_next(interaction.guild_id)
        except Exception as exc:
            try:
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            except Exception:
                pass
            await interaction.followup.send(f"❌ Upload error: {exc}", ephemeral=True)

    # =========================================================================
    # Dedicated Music Channel Commands
    # =========================================================================

    musicchannel_group = app_commands.Group(name="musicchannel", description="Configure dedicated server music channel")

    @musicchannel_group.command(name="setup", description="Creates or designates a dedicated music channel with live player embed")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        channel = discord.utils.get(guild.text_channels, name="music-player")
        if not channel:
            channel = await guild.create_text_channel(
                name="music-player",
                topic="🎵 Dedicated Music Player • Type song titles or paste YouTube/Spotify/SoundCloud links to play!",
                reason="Created dedicated ARC music channel"
            )

        embed = self._build_player_embed(guild.id)
        view = MusicPlayerView(self)
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin(reason="Pinned active ARC music player banner")
            await asyncio.sleep(0.4)
            async for sys_m in channel.history(limit=5):
                if sys_m.type == discord.MessageType.pins_add:
                    try:
                        await sys_m.delete()
                    except discord.HTTPException:
                        pass
        except discord.HTTPException:
            pass

        await Config.set_music_channel(guild.id, channel.id)
        await Config.set_music_player_message_id(guild.id, msg.id)
        await interaction.followup.send(f"✅ Dedicated music channel configured: {channel.mention}\nMembers can now paste links or type song names directly into that channel!", ephemeral=True)

    @musicchannel_group.command(name="set", description="Assign an existing channel as the dedicated music channel")
    @app_commands.describe(channel="Text channel to use")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        embed = self._build_player_embed(interaction.guild_id)
        view = MusicPlayerView(self)
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin(reason="Pinned active ARC music player banner")
            await asyncio.sleep(0.4)
            async for sys_m in channel.history(limit=5):
                if sys_m.type == discord.MessageType.pins_add:
                    try:
                        await sys_m.delete()
                    except discord.HTTPException:
                        pass
        except discord.HTTPException:
            pass

        await Config.set_music_channel(interaction.guild_id, channel.id)
        await Config.set_music_player_message_id(interaction.guild_id, msg.id)
        await interaction.followup.send(f"✅ Set {channel.mention} as the dedicated music channel.", ephemeral=True)

    @musicchannel_group.command(name="clear", description="Remove dedicated music channel setting")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def channel_clear(self, interaction: discord.Interaction):
        await Config.set_music_channel(interaction.guild_id, None)
        await Config.set_music_player_message_id(interaction.guild_id, None)
        await interaction.response.send_message("✅ Dedicated music channel setting removed.", ephemeral=True)

    # =========================================================================
    # Persistent SQLite Playlists (/playlist)
    # =========================================================================

    playlist_group = app_commands.Group(name="playlist", description="Manage persistent server playlists")

    @playlist_group.command(name="create", description="Create a new persistent playlist")
    @app_commands.describe(name="Playlist name")
    async def pl_create(self, interaction: discord.Interaction, name: str):
        success = await Config.create_playlist(
            interaction.guild_id, name, interaction.user.id, interaction.user.display_name
        )
        if success:
            await interaction.response.send_message(f"✅ Created playlist **{name}**! Add songs with `/playlist add {name} <url>`.")
        else:
            await interaction.response.send_message(f"❌ A playlist named **{name}** already exists in this server.", ephemeral=True)

    async def _play_playlist_by_name(self, interaction: discord.Interaction, name: str):
        if not interaction.response.is_done():
            await interaction.response.defer()
        pl_data = await Config.get_playlist(interaction.guild_id, name)
        if not pl_data or not pl_data.get("tracks"):
            await interaction.followup.send(f"❌ Playlist **{name}** is empty or does not exist.", ephemeral=True)
            return

        vc, err = await self._ensure_voice(interaction)
        if err or not vc:
            await interaction.followup.send(err or "Voice error.", ephemeral=True)
            return

        tracks = pl_data["tracks"]
        state = self.get_state(interaction.guild_id)
        queued_count = 0

        for t in tracks:
            url = t["url"]
            state.queue.append(Song(
                source_url=url if url.startswith("http") else f"ytsearch1:{url}",
                title=t["title"],
                duration=0,
                thumbnail="",
                webpage_url=url,
                requester=interaction.user,
                source_type=t.get("source_type", "youtube")
            ))
            queued_count += 1

        embed = discord.Embed(
            title="🎵 Playlist Queued",
            description=f"Loaded **{queued_count}** tracks from playlist **{name}** into the queue.",
            color=discord.Color.purple()
        )
        await interaction.followup.send(embed=embed)
        await self.refresh_all_controllers(interaction.guild_id)
        if not state.is_playing and state.current is None:
            await self._play_next(interaction.guild_id)

    async def _show_playlist_by_name(self, interaction: discord.Interaction, name: str):
        pl_data = await Config.get_playlist(interaction.guild_id, name)
        if not pl_data:
            msg = f"❌ Playlist **{name}** not found."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        tracks = pl_data.get("tracks", [])
        embed = discord.Embed(
            title=f"📁 Playlist: {pl_data['name']}",
            description=f"Created by **{pl_data.get('created_by_name', 'Unknown')}** • **{len(tracks)}** tracks",
            color=discord.Color.purple()
        )
        if not tracks:
            embed.add_field(name="Tracks", value="*This playlist has no songs yet. Click 'Add Song' below or use `/playlist add`.*", inline=False)
        else:
            lines = [f"`{t['position']}.` **{t['title'][:80]}**" for t in tracks[:25]]
            if len(tracks) > 25:
                lines.append(f"… and {len(tracks) - 25} more")
            embed.add_field(name="Track List", value="\n".join(lines), inline=False)

        view = PlaylistActionsView(self, name)
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view)

    @playlist_group.command(name="add", description="Add a track to a playlist (choose from dropdown or specify name)")
    @app_commands.describe(name="Playlist name (leave empty to select from dropdown menu)", query="Track title or link (leave empty to type in popup)")
    @app_commands.autocomplete(name=_autocomplete_playlists)
    async def pl_add(self, interaction: discord.Interaction, name: Optional[str] = None, query: Optional[str] = None):
        if not name:
            playlists = await Config.list_playlists(interaction.guild_id)
            if not playlists:
                await interaction.response.send_message(
                    "❌ No playlists found on this server yet. Create one first with `/playlist create <name>`!",
                    ephemeral=True
                )
                return
            view = PlaylistSelectView(self, playlists, query=query, mode="add")
            hint = f" Select which playlist to add **{query}** to:" if query else " Select a playlist below to add a track:"
            embed = discord.Embed(
                title="📂 Select Playlist",
                description=f"Choose a playlist from the dropdown menu below.{hint}",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        if not query:
            modal = AddSongModal(self, name)
            await interaction.response.send_modal(modal)
            return

        await interaction.response.defer()
        source_type = "url"
        if "spotify.com" in query:
            source_type = "spotify"
        elif "soundcloud.com" in query:
            source_type = "soundcloud"
        elif "youtube.com" in query or "youtu.be" in query:
            source_type = "youtube"

        added = await Config.add_track_to_playlist(interaction.guild_id, name, query, query, source_type)
        if added:
            embed = discord.Embed(
                title="✅ Track Added to Playlist",
                description=f"Added **{query}** to playlist **{name}**.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Playlist **{name}** was not found.", ephemeral=True)

    @playlist_group.command(name="play", description="Load and play all tracks from a playlist")
    @app_commands.describe(name="Playlist name (leave empty to select from dropdown)")
    @app_commands.autocomplete(name=_autocomplete_playlists)
    async def pl_play(self, interaction: discord.Interaction, name: Optional[str] = None):
        if not name:
            playlists = await Config.list_playlists(interaction.guild_id)
            if not playlists:
                await interaction.response.send_message("❌ No playlists found on this server. Create one first with `/playlist create <name>`!", ephemeral=True)
                return
            view = PlaylistSelectView(self, playlists, mode="play")
            embed = discord.Embed(
                title="▶️ Select Playlist to Play",
                description="Choose a playlist from the dropdown menu below to play in voice:",
                color=discord.Color.purple()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        await self._play_playlist_by_name(interaction, name)

    @playlist_group.command(name="view", description="View tracks inside a saved playlist")
    @app_commands.describe(name="Playlist name (leave empty to select from dropdown)")
    @app_commands.autocomplete(name=_autocomplete_playlists)
    async def pl_view(self, interaction: discord.Interaction, name: Optional[str] = None):
        if not name:
            playlists = await Config.list_playlists(interaction.guild_id)
            if not playlists:
                await interaction.response.send_message("❌ No playlists found on this server. Create one first with `/playlist create <name>`!", ephemeral=True)
                return
            view = PlaylistSelectView(self, playlists, mode="view")
            embed = discord.Embed(
                title="👁️ Select Playlist to View",
                description="Choose a playlist from the dropdown menu below to view its tracks:",
                color=discord.Color.purple()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        await self._show_playlist_by_name(interaction, name)

    @playlist_group.command(name="list", description="List all saved playlists in this server")
    async def pl_list(self, interaction: discord.Interaction):
        playlists = await Config.list_playlists(interaction.guild_id)
        if not playlists:
            await interaction.response.send_message("📁 No playlists created yet in this server. Use `/playlist create <name>` to create one!", ephemeral=True)
            return

        embed = discord.Embed(title="📚 Server Saved Playlists", color=discord.Color.blue())
        for p in playlists:
            embed.add_field(
                name=f"🎵 {p['name']}",
                value=f"{p['track_count']} tracks • by {p.get('created_by_name', 'Unknown')}",
                inline=True
            )
        view = PlaylistSelectView(self, playlists, mode="view")
        await interaction.response.send_message(embed=embed, view=view)

    @playlist_group.command(name="remove", description="Remove a track from a playlist by position number")
    @app_commands.describe(name="Playlist name", position="Position number from /playlist view (e.g. 1)")
    @app_commands.autocomplete(name=_autocomplete_playlists)
    async def pl_remove(self, interaction: discord.Interaction, name: str, position: int):
        pl_data = await Config.get_playlist(interaction.guild_id, name)
        if not pl_data:
            await interaction.response.send_message(f"❌ Playlist **{name}** not found.", ephemeral=True)
            return

        if pl_data["created_by"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only the playlist creator or an Administrator can edit this playlist.", ephemeral=True)
            return

        removed_title = await Config.remove_track_from_playlist(interaction.guild_id, name, position)
        if removed_title:
            await interaction.response.send_message(f"✅ Removed track `#{position}` (**{removed_title}**) from playlist **{name}**.")
        else:
            await interaction.response.send_message(f"❌ Invalid track position `#{position}`. Use `/playlist view {name}` to see track numbers.", ephemeral=True)

    @playlist_group.command(name="delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=_autocomplete_playlists)
    async def pl_delete(self, interaction: discord.Interaction, name: str):
        pl_data = await Config.get_playlist(interaction.guild_id, name)
        if not pl_data:
            await interaction.response.send_message(f"❌ Playlist **{name}** not found.", ephemeral=True)
            return

        # Check permissions: creator or administrator
        if pl_data["created_by"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only the playlist creator or an Administrator can delete this playlist.", ephemeral=True)
            return

        await Config.delete_playlist(interaction.guild_id, name)
        await interaction.response.send_message(f"🗑️ Playlist **{name}** deleted.")

    # =========================================================================
    # Playback Controls (/skip, /pause, /resume, /stop, /queue, etc.)
    # =========================================================================

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        state._skip_flag = True
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped current track.")

    @app_commands.command(name="previous", description="Play the previous track from history")
    async def previous(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.history:
            await interaction.response.send_message("❌ No previous track in history.", ephemeral=True)
            return
        state._previous_flag = True
        prev = state.history.pop()
        if state.current:
            state.queue.insert(0, state.current)
        state.queue.insert(0, prev)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await interaction.response.send_message(f"⏮️ Playing previous track: **{prev.title}**")

    @app_commands.command(name="pause", description="Pause audio playback")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            state.is_playing = False
            if state.current:
                state.current.pause_playback_time = time.time()
            await interaction.response.send_message("⏸️ Paused.")
            await self.refresh_all_controllers(interaction.guild_id)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume paused playback")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            state.is_playing = True
            if state.current and state.current.pause_playback_time:
                state.current.total_paused_duration += time.time() - state.current.pause_playback_time
                state.current.pause_playback_time = None
            self._cancel_inactivity_timer(interaction.guild_id)
            await interaction.response.send_message("▶️ Resumed.")
            await self.refresh_all_controllers(interaction.guild_id)
        else:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop music and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        state.loop_mode = "off"
        state.current = None
        state.is_playing = False
        state._stop_flag = True
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()
        await interaction.response.send_message("⏹️ Playback stopped and queue cleared.")
        await self.refresh_all_controllers(interaction.guild_id)
        self._start_inactivity_timer(interaction.guild_id)

    @app_commands.command(name="queue", description="View upcoming tracks in queue")
    async def queue_view(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        embed = self._build_player_embed(interaction.guild_id)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show detailed player card with timeline")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._build_player_embed(interaction.guild_id))

    @app_commands.command(name="volume", description="Set audio volume level (0% - 200%)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
        state = self.get_state(interaction.guild_id)
        state.volume = level / 100
        source = state.voice_client.source if state.voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
        await self.refresh_all_controllers(interaction.guild_id)

    @app_commands.command(name="loop", description="Set looping mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Disabled (Off)", value="off"),
        app_commands.Choice(name="Repeat Single Track", value="single"),
        app_commands.Choice(name="Repeat Entire Queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = self.get_state(interaction.guild_id)
        state.loop_mode = mode.value
        await interaction.response.send_message(f"🔁 Looping mode: **{mode.name}**.")
        await self.refresh_all_controllers(interaction.guild_id)

    @app_commands.command(name="shuffle", description="Shuffle all queued tracks")
    async def shuffle_cmd(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if len(state.queue) < 2:
            await interaction.response.send_message("❌ Need at least 2 tracks in queue to shuffle.", ephemeral=True)
            return
        random.shuffle(state.queue)
        state.is_shuffled = True
        await interaction.response.send_message(f"🔀 Shuffled **{len(state.queue)}** tracks in queue.")
        await self.refresh_all_controllers(interaction.guild_id)

    @app_commands.command(name="remove", description="Remove a queued track by its position")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild_id)
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(f"❌ Invalid position; queue currently has {len(state.queue)} tracks.", ephemeral=True)
            return
        removed = state.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from queue.")
        await self.refresh_all_controllers(interaction.guild_id)

    @app_commands.command(name="clearqueue", description="Clear upcoming queue without interrupting current track")
    async def clearqueue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        count = len(state.queue)
        state.queue.clear()
        await interaction.response.send_message(f"🗑️ Cleared **{count}** queued tracks.")
        await self.refresh_all_controllers(interaction.guild_id)

    @app_commands.command(name="disconnect", description="Safely disconnect from voice channel")
    async def disconnect(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message("Bot is not currently connected to voice.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._end_voice_session(interaction.guild_id)
        await interaction.followup.send("👋 Disconnected cleanly from voice channel.")

    # =========================================================================
    # Library Inspection & File Commands
    # =========================================================================

    @app_commands.command(name="musiclist", description="List files in the server music folder")
    async def musiclist(self, interaction: discord.Interaction):
        files = self._audio_files(self.local_dir)
        if not files:
            await interaction.response.send_message(f"No server music files found. Add audio to `{self.local_dir}`.", ephemeral=True)
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
        embed = discord.Embed(title="🎵 Server Music Library", description="\n".join(lines), color=discord.Color.purple())
        embed.set_footer(text=f"{len(files)} files • Use /playlocal <filename>")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="musicinfo", description="Show audio directory metrics")
    @app_commands.checks.has_permissions(administrator=True)
    async def musicinfo(self, interaction: discord.Interaction):
        files = self._audio_files(self.local_dir)
        total_size = sum(os.path.getsize(p) for _, p in files if os.path.exists(p))
        embed = discord.Embed(title="📁 Server Music Directory", color=discord.Color.blue())
        embed.add_field(name="Location", value=f"`{self.local_dir}`", inline=False)
        embed.add_field(name="Total Files", value=str(len(files)), inline=True)
        embed.add_field(name="Disk Usage", value=self._format_size(total_size), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="sharesong", description="Copy a track from your personal library into public library")
    @app_commands.describe(filename="Track in your library")
    @app_commands.autocomplete(filename=_autocomplete_my_files)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sharesong(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer(ephemeral=True)
        matches = self._search_files(filename, self._audio_files(self._get_user_dir(interaction.user.id)))
        if not matches:
            await interaction.followup.send("❌ Track not found in your library.", ephemeral=True)
            return
        src_name, src_path = matches[0]
        dst = self.public_dir / Path(src_name).name
        try:
            await asyncio.to_thread(shutil.copy2, src_path, dst)
            await interaction.followup.send(f"✅ Shared `{src_name}` with the public library.", ephemeral=True)
        except OSError as exc:
            await interaction.followup.send(f"❌ Copy error: {exc}", ephemeral=True)

    @app_commands.command(name="streamstatus", description="Diagnostics of active voice buffers")
    async def streamstatus(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        embed = self._build_player_embed(interaction.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="streamhelp", description="Music command guide and supported links")
    async def streamhelp(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎵 ARC Music Engine Manual",
            description=(
                "**Supported Streaming Sources:**\n"
                "• **YouTube**: Videos, shorts, and playlist URLs\n"
                "• **Spotify**: Tracks, playlists, and albums\n"
                "• **SoundCloud**: Single tracks and playlist sets\n"
                "• **Local Libraries**: Server files (`/playlocal`), Personal (`/mymusic`), Public (`/publicmusic`)\n"
                "• **Direct File Uploads**: Up to 100 MB via `/streamupload`\n\n"
                "**Dedicated Channel:**\n"
                "• Admins can set up a `#music-player` channel using `/musicchannel setup`.\n"
                "• Once configured, members can simply paste links or type song names directly into chat!"
            ),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_unload(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
        if hasattr(self, "ensure_players_task") and self.ensure_players_task and not self.ensure_players_task.done():
            self.ensure_players_task.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
