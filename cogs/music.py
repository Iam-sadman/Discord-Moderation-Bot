import asyncio
import os
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# yt-dlp options
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch5',
    'source_address': '0.0.0.0',
    'extract_flat': False,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -af "volume=1.0"',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Song:
    """Represents a song in the queue."""
    def __init__(self, source_url: str, title: str, duration: int, thumbnail: str,
                 webpage_url: str, requester: discord.Member, is_local: bool = False):
        self.source_url = source_url  # Direct audio URL or local path
        self.title = title
        self.duration = duration
        self.thumbnail = thumbnail
        self.webpage_url = webpage_url
        self.requester = requester
        self.is_local = is_local

    @classmethod
    async def from_youtube(cls, query: str, requester: discord.Member, loop=None):
        """Create Song(s) from a YouTube URL or search query."""
        loop = loop or asyncio.get_event_loop()
        # Check if it's a playlist
        if 'list=' in query:
            return await cls._from_playlist(query, requester, loop)

        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))

        if data is None:
            raise ValueError("Could not extract info from the provided URL/query")

        # If search results, return list of options
        if 'entries' in data:
            entries = [e for e in data['entries'] if e is not None]
            if not entries:
                raise ValueError("No results found")
            # For search, return the entries for selection
            return entries  # Will be handled by the search command

        return [cls(
            source_url=data.get('url', data.get('webpage_url')),
            title=data.get('title', 'Unknown'),
            duration=data.get('duration', 0),
            thumbnail=data.get('thumbnail', ''),
            webpage_url=data.get('webpage_url', query),
            requester=requester
        )]

    @classmethod
    async def _from_playlist(cls, url: str, requester: discord.Member, loop):
        """Extract all songs from a YouTube playlist."""
        playlist_ytdl = yt_dlp.YoutubeDL({
            **YTDL_OPTIONS,
            'extract_flat': True,
        })
        data = await loop.run_in_executor(None, lambda: playlist_ytdl.extract_info(url, download=False))
        if data is None or 'entries' not in data:
            raise ValueError("Could not extract playlist")

        songs = []
        for entry in data['entries']:
            if entry is None:
                continue
            songs.append(cls(
                source_url=entry.get('url', ''),
                title=entry.get('title', 'Unknown'),
                duration=entry.get('duration', 0),
                thumbnail=entry.get('thumbnail', ''),
                webpage_url=entry.get('url', ''),
                requester=requester
            ))
        return songs

    @classmethod
    def from_local(cls, filepath: str, requester: discord.Member):
        """Create a Song from a local audio file."""
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        title = os.path.splitext(os.path.basename(filepath))[0]
        return cls(
            source_url=filepath,
            title=title,
            duration=0,
            thumbnail='',
            webpage_url=filepath,
            requester=requester,
            is_local=True
        )


class GuildMusicState:
    """Manages per-guild music state."""
    def __init__(self):
        self.queue: list[Song] = []
        self.current: Song | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 1.0  # 0.0 to 2.0
        self.loop_mode: str = "off"  # off, single, queue
        self.is_playing: bool = False
        self._skip_flag: bool = False
        self._inactivity_task: asyncio.Task | None = None

    def reset(self):
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        self.is_playing = False
        self._skip_flag = False


class Music(commands.Cog):
    """🎵 Music Player — play YouTube and local audio in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    def _format_duration(self, seconds: int) -> str:
        if seconds <= 0:
            return "Live/Unknown"
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}:{mins:02d}:{secs:02d}"
        return f"{mins}:{secs:02d}"

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Ensure the bot is in the user's voice channel."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)
            return None

        state = self.get_state(interaction.guild_id)
        channel = interaction.user.voice.channel

        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel.id != channel.id:
                await state.voice_client.move_to(channel)
            return state.voice_client

        state.voice_client = await channel.connect(self_deaf=True)
        return state.voice_client

    async def _play_next(self, guild_id: int):
        """Play the next song in the queue."""
        state = self.get_state(guild_id)

        if not state.voice_client or not state.voice_client.is_connected():
            state.reset()
            return

        # Handle loop modes
        if state._skip_flag:
            state._skip_flag = False
        elif state.loop_mode == "single" and state.current:
            pass  # Don't change current song
        elif state.loop_mode == "queue" and state.current:
            state.queue.append(state.current)

        if state.loop_mode != "single" or state._skip_flag:
            if not state.queue:
                state.current = None
                state.is_playing = False
                self._start_inactivity_timer(guild_id)
                return
            state.current = state.queue.pop(0)

        song = state.current

        try:
            if song.is_local:
                source = discord.FFmpegPCMAudio(song.source_url)
            else:
                # For playlist entries that only have video URLs, we need to re-extract
                if not song.source_url.startswith('http') or 'googlevideo' not in song.source_url:
                    try:
                        loop = asyncio.get_event_loop()
                        data = await loop.run_in_executor(
                            None, lambda: ytdl.extract_info(song.webpage_url or song.source_url, download=False)
                        )
                        if data:
                            song.source_url = data.get('url', song.source_url)
                            song.title = data.get('title', song.title)
                            song.duration = data.get('duration', song.duration)
                            song.thumbnail = data.get('thumbnail', song.thumbnail)
                    except Exception:
                        pass

                source = discord.FFmpegPCMAudio(
                    song.source_url,
                    **{
                        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                        'options': f'-vn -af "volume={state.volume}"',
                    }
                )

            # Apply volume transform for local files
            if song.is_local:
                source = discord.PCMVolumeTransformer(source, volume=state.volume)

            state.is_playing = True
            self._cancel_inactivity_timer(guild_id)

            def after_playing(error):
                if error:
                    print(f"Player error: {error}")
                asyncio.run_coroutine_threadsafe(self._play_next(guild_id), self.bot.loop)

            state.voice_client.play(source, after=after_playing)
        except Exception as e:
            print(f"Error playing {song.title}: {e}")
            state.is_playing = False
            await self._play_next(guild_id)

    def _start_inactivity_timer(self, guild_id: int):
        """Start a timer to disconnect after 3 minutes of inactivity."""
        self._cancel_inactivity_timer(guild_id)
        state = self.get_state(guild_id)

        async def disconnect_after_timeout():
            await asyncio.sleep(180)  # 3 minutes
            if state.voice_client and state.voice_client.is_connected() and not state.is_playing:
                await state.voice_client.disconnect()
                state.reset()

        state._inactivity_task = asyncio.ensure_future(disconnect_after_timeout())

    def _cancel_inactivity_timer(self, guild_id: int):
        state = self.get_state(guild_id)
        if state._inactivity_task and not state._inactivity_task.done():
            state._inactivity_task.cancel()
            state._inactivity_task = None

    # ──── Slash Commands ────

    @app_commands.command(name="play", description="Play a YouTube URL, search query, or local file path")
    @app_commands.describe(query="YouTube URL, search keywords, or local file path")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        state = self.get_state(interaction.guild_id)

        # Check if it's a local file
        if os.path.isfile(query):
            try:
                song = Song.from_local(query, interaction.user)
                state.queue.append(song)

                embed = discord.Embed(title="🎵 Added Local File", color=discord.Color.purple())
                embed.add_field(name="Title", value=song.title, inline=False)
                embed.set_footer(text=f"Requested by {interaction.user}")
                await interaction.followup.send(embed=embed)

                if not state.is_playing:
                    await self._play_next(interaction.guild_id)
                return
            except FileNotFoundError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return

        # YouTube URL or search
        try:
            # If not a URL, prepend ytsearch:
            if not query.startswith(('http://', 'https://')):
                query = f"ytsearch:{query}"

            songs = await Song.from_youtube(query, interaction.user, self.bot.loop)

            if isinstance(songs, list) and len(songs) > 0 and isinstance(songs[0], Song):
                for song in songs:
                    state.queue.append(song)

                if len(songs) == 1:
                    embed = discord.Embed(title="🎵 Added to Queue", color=discord.Color.purple())
                    embed.add_field(name="Title", value=songs[0].title, inline=False)
                    embed.add_field(name="Duration", value=self._format_duration(songs[0].duration), inline=True)
                    embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
                    if songs[0].thumbnail:
                        embed.set_thumbnail(url=songs[0].thumbnail)
                    embed.set_footer(text=f"Requested by {interaction.user}")
                    await interaction.followup.send(embed=embed)
                else:
                    embed = discord.Embed(
                        title="🎵 Added Playlist to Queue",
                        description=f"Added **{len(songs)}** tracks to the queue",
                        color=discord.Color.purple()
                    )
                    embed.set_footer(text=f"Requested by {interaction.user}")
                    await interaction.followup.send(embed=embed)

                if not state.is_playing:
                    await self._play_next(interaction.guild_id)
            else:
                await interaction.followup.send("❌ Could not find any playable tracks.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="search", description="Search YouTube and pick a track")
    @app_commands.describe(query="Search keywords")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        try:
            loop = asyncio.get_event_loop()
            search_ytdl = yt_dlp.YoutubeDL({**YTDL_OPTIONS, 'default_search': 'ytsearch5'})
            data = await loop.run_in_executor(
                None, lambda: search_ytdl.extract_info(f"ytsearch5:{query}", download=False)
            )

            if not data or 'entries' not in data:
                await interaction.followup.send("❌ No results found.", ephemeral=True)
                return

            entries = [e for e in data['entries'] if e][:5]
            if not entries:
                await interaction.followup.send("❌ No results found.", ephemeral=True)
                return

            embed = discord.Embed(title=f"🔍 Search Results for: {query}", color=discord.Color.purple())

            options = []
            for i, entry in enumerate(entries, 1):
                duration = entry.get('duration', 0)
                title = entry.get('title', 'Unknown')
                embed.add_field(
                    name=f"{i}. {title}",
                    value=f"Duration: {self._format_duration(duration)}",
                    inline=False
                )
                options.append(discord.SelectOption(
                    label=f"{i}. {title[:95]}",
                    value=str(i - 1),
                    description=f"Duration: {self._format_duration(duration)}"
                ))

            select = discord.ui.Select(
                placeholder="Pick a track to play...",
                options=options,
                custom_id="search_select"
            )

            async def select_callback(select_interaction: discord.Interaction):
                idx = int(select.values[0])
                chosen = entries[idx]

                vc = await self._ensure_voice(select_interaction)
                if not vc:
                    return

                state = self.get_state(select_interaction.guild_id)
                song = Song(
                    source_url=chosen.get('url', chosen.get('webpage_url', '')),
                    title=chosen.get('title', 'Unknown'),
                    duration=chosen.get('duration', 0),
                    thumbnail=chosen.get('thumbnail', ''),
                    webpage_url=chosen.get('webpage_url', ''),
                    requester=select_interaction.user
                )
                state.queue.append(song)

                await select_interaction.response.send_message(
                    f"🎵 Added **{song.title}** to the queue (#{len(state.queue)})"
                )

                if not state.is_playing:
                    await self._play_next(select_interaction.guild_id)

            select.callback = select_callback
            view = discord.ui.View(timeout=30)
            view.add_item(select)

            await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            await interaction.followup.send(f"❌ Search error: {e}", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not state.is_playing:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return

        state._skip_flag = True
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped!")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed")
        else:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        state.loop_mode = "off"
        if state.voice_client:
            state.voice_client.stop()
        state.is_playing = False
        state.current = None
        await interaction.response.send_message("⏹️ Stopped and queue cleared")

    @app_commands.command(name="queue", description="View the current queue")
    async def queue_view(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)

        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())

        if state.current:
            embed.add_field(
                name="Now Playing",
                value=f"**{state.current.title}** [{self._format_duration(state.current.duration)}]\n"
                      f"Requested by {state.current.requester.mention}",
                inline=False
            )

        if state.queue:
            queue_text = ""
            for i, song in enumerate(state.queue[:10], 1):
                queue_text += f"`{i}.` **{song.title}** [{self._format_duration(song.duration)}]\n"

            if len(state.queue) > 10:
                queue_text += f"\n*... and {len(state.queue) - 10} more*"

            embed.add_field(name="Up Next", value=queue_text, inline=False)
        elif not state.current:
            embed.description = "The queue is empty. Use `/play` to add songs!"

        embed.set_footer(text=f"Loop: {state.loop_mode.capitalize()} | Volume: {int(state.volume * 100)}%")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild_id)
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(f"❌ Invalid position. Queue has {len(state.queue)} tracks.", ephemeral=True)
            return

        removed = state.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue")

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)
            return

        song = state.current
        embed = discord.Embed(title="🎵 Now Playing", color=discord.Color.purple())
        embed.add_field(name="Title", value=song.title, inline=False)
        embed.add_field(name="Duration", value=self._format_duration(song.duration), inline=True)
        embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
        embed.add_field(name="Loop", value=state.loop_mode.capitalize(), inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"Requested by {song.requester}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Set playback volume (0-200)")
    @app_commands.describe(level="Volume level (0-200)")
    async def volume(self, interaction: discord.Interaction, level: int):
        if level < 0 or level > 200:
            await interaction.response.send_message("❌ Volume must be between 0 and 200.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        state.volume = level / 100.0

        # Update volume live if playing local file with PCMVolumeTransformer
        if state.voice_client and state.voice_client.source:
            if isinstance(state.voice_client.source, discord.PCMVolumeTransformer):
                state.voice_client.source.volume = state.volume

        await interaction.response.send_message(f"🔊 Volume set to **{level}%**")

    @app_commands.command(name="loop", description="Set loop mode")
    @app_commands.describe(mode="Loop mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Single Track", value="single"),
        app_commands.Choice(name="Entire Queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = self.get_state(interaction.guild_id)
        state.loop_mode = mode.value

        icons = {"off": "➡️", "single": "🔂", "queue": "🔁"}
        await interaction.response.send_message(f"{icons.get(mode.value, '🔁')} Loop mode: **{mode.name}**")

    @app_commands.command(name="disconnect", description="Disconnect the bot from voice")
    async def disconnect(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect()
            state.reset()
            await interaction.response.send_message("👋 Disconnected!")
        else:
            await interaction.response.send_message("❌ Not connected to any voice channel.", ephemeral=True)

    @app_commands.command(name="clearqueue", description="Clear the queue without stopping current track")
    async def clearqueue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        count = len(state.queue)
        state.queue.clear()
        await interaction.response.send_message(f"🗑️ Cleared **{count}** tracks from the queue")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
