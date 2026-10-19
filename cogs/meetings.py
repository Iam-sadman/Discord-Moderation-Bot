"""
Meeting & Training Recording Cog for MeetingAssistantBot.
Features:
- Per-user voice capture via discord-ext-voice-recv (VoiceRecvClient)
- Real-time voice attendance tracking (joins/leaves/durations)
- DynamicItem persistent control panel buttons across VPS restarts
- Gemini AI multimodal transcription pipeline
- File upload pathway for pre-recorded audio
- Formal English Meeting Minutes delivery with Discord embeds
- Mandatory cleanup of all temporary audio files
"""

import io
import os
import re
import json
import time
import shutil
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from services.meetings_db import MeetingsDB
from services.meetings_ai import (
    generate_meeting_minutes,
    compress_audio_ffmpeg,
    merge_audio_files_ffmpeg,
)

logger = logging.getLogger("MeetingsCog")

# Base directory for temporary meeting audio files
TEMP_MEETINGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_meetings")


def fmt_duration(seconds: int) -> str:
    """Formats seconds into H:MM:SS or M:SS."""
    if not seconds or seconds <= 0:
        return "0:00"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# =============================================================================
# Dynamic Persistent Items — Survive VPS Restarts
# =============================================================================

class DynamicMeetingStopButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"meeting:stop:(?P<meeting_id>\d+)"
):
    """Persistent Stop & Generate Minutes button."""

    def __init__(self, meeting_id: int):
        super().__init__(
            discord.ui.Button(
                label="⏹️ Stop & Generate Minutes",
                style=discord.ButtonStyle.danger,
                custom_id=f"meeting:stop:{meeting_id}"
            )
        )
        self.meeting_id = meeting_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        meeting_id = int(match["meeting_id"])
        return cls(meeting_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("MeetingsCog")
        if not cog:
            await interaction.followup.send("❌ Meeting system is not available.", ephemeral=True)
            return
        await cog._stop_meeting(interaction, self.meeting_id)


class DynamicMeetingNoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"meeting:note:(?P<meeting_id>\d+)"
):
    """Persistent Add Manual Note button."""

    def __init__(self, meeting_id: int):
        super().__init__(
            discord.ui.Button(
                label="📌 Add Manual Note",
                style=discord.ButtonStyle.secondary,
                custom_id=f"meeting:note:{meeting_id}"
            )
        )
        self.meeting_id = meeting_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        meeting_id = int(match["meeting_id"])
        return cls(meeting_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def callback(self, interaction: discord.Interaction):
        modal = MeetingNoteModal(self.meeting_id)
        await interaction.response.send_modal(modal)


class MeetingNoteModal(discord.ui.Modal, title="📌 Add Meeting Note"):
    """Modal for adding a manual note to an active meeting."""

    note_input = discord.ui.TextInput(
        label="Note Content",
        style=discord.TextStyle.paragraph,
        placeholder="Type your note here...",
        required=True,
        max_length=2000
    )

    def __init__(self, meeting_id: int):
        super().__init__()
        self.meeting_id = meeting_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Store the note as a message in the output channel
        db = MeetingsDB.get_instance()
        meeting = await db.get_meeting(self.meeting_id)
        if not meeting or meeting["status"] != "recording":
            await interaction.followup.send("❌ This meeting is no longer recording.", ephemeral=True)
            return

        note_text = self.note_input.value
        user_name = interaction.user.display_name

        # Post note in output channel
        try:
            channel = interaction.client.get_channel(int(meeting["output_channel_id"]))
            if channel:
                embed = discord.Embed(
                    title="📌 Meeting Note Added",
                    description=note_text,
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(text=f"By {user_name} • Meeting: {meeting['title']}")
                await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"[MEETING] Failed to post note: {e}")

        await interaction.followup.send(f"✅ Note added to meeting **{meeting['title']}**.", ephemeral=True)


# =============================================================================
# Audio Sink — Per-User Voice Capture
# =============================================================================

class MeetingAudioSink:
    """
    Captures incoming audio per user using discord-ext-voice-recv.
    Stores raw PCM data per speaker, then writes to files on stop.
    """

    def __init__(self, meeting_id: int):
        self.meeting_id = meeting_id
        self.temp_dir = os.path.join(TEMP_MEETINGS_DIR, f"meeting_{meeting_id}")
        os.makedirs(self.temp_dir, exist_ok=True)
        self.user_buffers: Dict[int, bytearray] = {}
        self.user_names: Dict[int, str] = {}
        self._stopped = False

    def write(self, user: Optional[discord.User], data: bytes):
        """Called by voice_recv for each audio packet."""
        if self._stopped or user is None:
            return
        uid = user.id
        if uid not in self.user_buffers:
            self.user_buffers[uid] = bytearray()
            self.user_names[uid] = user.display_name
        self.user_buffers[uid].extend(data)

    def stop(self):
        """Marks the sink as stopped."""
        self._stopped = True

    async def save_audio_files(self) -> List[str]:
        """
        Saves captured per-user PCM buffers to WAV files
        and compresses them. Returns list of compressed file paths.
        """
        self._stopped = True
        saved_files = []

        for uid, buffer in self.user_buffers.items():
            if not buffer:
                continue

            pcm_path = os.path.join(self.temp_dir, f"user_{uid}.pcm")
            wav_path = os.path.join(self.temp_dir, f"user_{uid}.wav")

            # Write raw PCM
            await asyncio.to_thread(self._write_pcm, pcm_path, buffer)

            # Convert PCM to WAV using Python built-in wave module (reliable, no external ffmpeg needed)
            try:
                def _write_wav(pcm_bytes, out_wav):
                    import wave
                    with wave.open(out_wav, "wb") as wf:
                        wf.setnchannels(2)      # Stereo
                        wf.setsampwidth(2)      # 16-bit
                        wf.setframerate(48000)  # 48kHz Discord voice rate
                        wf.writeframes(pcm_bytes)

                await asyncio.to_thread(_write_wav, buffer, wav_path)
                if os.path.exists(wav_path):
                    saved_files.append(wav_path)
            except Exception as e:
                logger.warning(f"[MEETING] Failed to convert PCM to WAV for user {uid}: {e}")

        return saved_files

    @staticmethod
    def _write_pcm(path: str, data: bytearray):
        with open(path, "wb") as f:
            f.write(data)

    def get_speaker_names(self) -> List[str]:
        """Returns list of captured speaker display names."""
        return list(set(self.user_names.values()))


# =============================================================================
# Meetings Cog
# =============================================================================

class MeetingsCog(commands.Cog):
    """Meeting & Training Recording System for MeetingAssistantBot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = MeetingsDB.get_instance()
        # Active meeting state: guild_id -> {meeting_id, sink, ...}
        self._active_meetings: Dict[int, Dict[str, Any]] = {}

    async def cog_load(self):
        """Register persistent dynamic items on cog load."""
        self.bot.add_dynamic_items(DynamicMeetingStopButton, DynamicMeetingNoteButton)
        await self.db.init_db()
        logger.info("[OK] MeetingsCog loaded with DynamicItem support")

    async def cog_unload(self):
        """Cleanup on unload."""
        # Stop any active recordings
        for guild_id in list(self._active_meetings.keys()):
            try:
                state = self._active_meetings.get(guild_id)
                if state and state.get("sink"):
                    state["sink"].stop()
            except Exception:
                pass

    # =========================================================================
    # Permission Check
    # =========================================================================

    async def _check_meeting_permission(self, interaction: discord.Interaction) -> bool:
        """Checks if the user has permission to manage meetings."""
        member = interaction.user
        if not member or not interaction.guild:
            return False

        # Server owner & Administrator always have access
        if member.id == interaction.guild.owner_id:
            return True
        if member.guild_permissions.administrator:
            return True

        # Check allowed roles from meeting settings
        settings = await self.db.get_settings(interaction.guild.id)
        allowed_roles = settings.get("allowed_role_ids", [])
        if allowed_roles:
            member_role_ids = {r.id for r in member.roles}
            if member_role_ids & set(allowed_roles):
                return True

        return False

    # =========================================================================
    # Slash Commands
    # =========================================================================

    meeting_group = app_commands.Group(
        name="meeting",
        description="Meeting & Training Recording Management"
    )

    @meeting_group.command(name="start", description="Start recording a meeting/training in your voice channel")
    @app_commands.describe(
        title="Meeting title or topic",
        output_channel="Text channel for minutes output (optional, uses default)",
        project_code="Optional project reference code"
    )
    async def meeting_start(
        self,
        interaction: discord.Interaction,
        title: str,
        output_channel: Optional[discord.TextChannel] = None,
        project_code: Optional[str] = None
    ):
        await interaction.response.defer(ephemeral=True)

        if not await self._check_meeting_permission(interaction):
            await interaction.followup.send(
                "❌ You don't have permission to start meetings. Ask an admin to configure allowed roles.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user

        # Check if user is in a voice channel
        if not member.voice or not member.voice.channel:
            await interaction.followup.send(
                "❌ You must be in a voice channel to start a meeting!",
                ephemeral=True
            )
            return

        voice_channel = member.voice.channel

        # Check if already recording in this guild
        if guild.id in self._active_meetings:
            active = self._active_meetings[guild.id]
            await interaction.followup.send(
                f"❌ A meeting is already recording: **{active.get('title', 'Unknown')}**\n"
                f"Stop it first with `/meeting stop` or the ⏹️ button.",
                ephemeral=True
            )
            return

        # Resolve output channel
        if not output_channel:
            settings = await self.db.get_settings(guild.id)
            default_ch_id = settings.get("default_output_channel_id")
            if default_ch_id:
                output_channel = guild.get_channel(int(default_ch_id))
            if not output_channel:
                output_channel = interaction.channel

        # Create meeting record
        meeting_id = await self.db.create_meeting(
            guild_id=guild.id,
            title=title,
            voice_channel_id=voice_channel.id,
            voice_channel_name=voice_channel.name,
            output_channel_id=output_channel.id,
            host_id=member.id,
            host_name=member.display_name,
            project_code=project_code
        )

        # Track initial participants
        for m in voice_channel.members:
            if not m.bot:
                await self.db.add_participant(meeting_id, m.id, m.display_name)

        # Connect to voice channel and start recording
        sink = MeetingAudioSink(meeting_id)

        try:
            # Try to use VoiceRecvClient for per-user audio capture
            try:
                from discord.ext import voice_recv
                vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
                vc.listen(voice_recv.BasicSink(sink.write))
                use_voice_recv = True
            except (ImportError, Exception) as e:
                logger.warning(f"[MEETING] voice_recv not available ({e}), using basic voice client")
                vc = await voice_channel.connect()
                use_voice_recv = False

        except Exception as e:
            await self.db.update_meeting_status(
                meeting_id, "failed", error_message=f"Failed to connect to voice: {e}"
            )
            await interaction.followup.send(
                f"❌ Failed to connect to **{voice_channel.name}**: `{e}`",
                ephemeral=True
            )
            return

        # Store active meeting state
        self._active_meetings[guild.id] = {
            "meeting_id": meeting_id,
            "title": title,
            "voice_channel": voice_channel,
            "voice_client": vc,
            "sink": sink,
            "use_voice_recv": use_voice_recv,
            "started_at": time.time()
        }

        # Post recording control panel embed
        embed = discord.Embed(
            title="🔴 Meeting Recording Active",
            description=(
                f"**{title}**\n\n"
                f"🎙️ Voice Channel: **{voice_channel.name}**\n"
                f"👤 Host: **{member.display_name}**\n"
                f"📁 Project: **{project_code or 'N/A'}**\n"
                f"📤 Output: {output_channel.mention}\n\n"
                f"*Recording in progress... Use the buttons below to control.*"
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Meeting ID: {meeting_id}")

        view = discord.ui.View(timeout=None)
        view.add_item(DynamicMeetingStopButton(meeting_id))
        view.add_item(DynamicMeetingNoteButton(meeting_id))

        ctrl_msg = await output_channel.send(embed=embed, view=view)

        # Store control message ID for later update
        self._active_meetings[guild.id]["control_message_id"] = ctrl_msg.id
        self._active_meetings[guild.id]["output_channel_id"] = output_channel.id

        await interaction.followup.send(
            f"✅ Meeting **{title}** started! Recording in **{voice_channel.name}**.\n"
            f"Minutes will be posted in {output_channel.mention} when stopped.",
            ephemeral=True
        )

    @meeting_group.command(name="stop", description="Stop the current meeting recording and generate minutes")
    async def meeting_stop_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not await self._check_meeting_permission(interaction):
            await interaction.followup.send("❌ You don't have permission to stop meetings.", ephemeral=True)
            return

        guild = interaction.guild
        state = self._active_meetings.get(guild.id)
        if not state:
            await interaction.followup.send("❌ No active meeting recording in this server.", ephemeral=True)
            return

        await self._stop_meeting(interaction, state["meeting_id"])

    @meeting_group.command(name="upload_audio", description="Upload a pre-recorded audio file to generate meeting minutes")
    @app_commands.describe(
        audio_file="Audio file (.mp3, .wav, .ogg, .m4a)",
        title="Meeting title",
        output_channel="Text channel for minutes output",
        project_code="Optional project reference code"
    )
    async def meeting_upload(
        self,
        interaction: discord.Interaction,
        audio_file: discord.Attachment,
        title: str,
        output_channel: Optional[discord.TextChannel] = None,
        project_code: Optional[str] = None
    ):
        await interaction.response.defer(ephemeral=True)

        if not await self._check_meeting_permission(interaction):
            await interaction.followup.send("❌ You don't have permission.", ephemeral=True)
            return

        # Validate file type
        valid_exts = (".mp3", ".wav", ".ogg", ".m4a", ".opus", ".flac", ".webm")
        filename_lower = audio_file.filename.lower()
        if not any(filename_lower.endswith(ext) for ext in valid_exts):
            await interaction.followup.send(
                f"❌ Unsupported file type. Accepted: {', '.join(valid_exts)}", ephemeral=True
            )
            return

        # Size check (25MB limit for Gemini free tier)
        if audio_file.size and audio_file.size > 100 * 1024 * 1024:
            await interaction.followup.send("❌ File too large. Maximum 100MB.", ephemeral=True)
            return

        guild = interaction.guild
        member = interaction.user

        if not output_channel:
            settings = await self.db.get_settings(guild.id)
            default_ch_id = settings.get("default_output_channel_id")
            if default_ch_id:
                output_channel = guild.get_channel(int(default_ch_id))
            if not output_channel:
                output_channel = interaction.channel

        # Create meeting record
        meeting_id = await self.db.create_meeting(
            guild_id=guild.id,
            title=title,
            voice_channel_id=0,
            voice_channel_name="Uploaded Audio",
            output_channel_id=output_channel.id,
            host_id=member.id,
            host_name=member.display_name,
            project_code=project_code
        )

        # Download and process in background
        await interaction.followup.send(
            f"✅ Audio file received! Processing **{title}**...\n"
            f"Minutes will be posted in {output_channel.mention} when ready.",
            ephemeral=True
        )

        asyncio.create_task(
            self._process_uploaded_audio(meeting_id, audio_file, output_channel, guild)
        )

    @meeting_group.command(name="status", description="Check the current meeting recording status")
    async def meeting_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        state = self._active_meetings.get(guild.id)

        if state:
            elapsed = int(time.time() - state["started_at"])
            embed = discord.Embed(
                title="🔴 Active Recording",
                description=(
                    f"**{state['title']}**\n\n"
                    f"🎙️ Channel: **{state['voice_channel'].name}**\n"
                    f"⏱️ Duration: **{fmt_duration(elapsed)}**\n"
                    f"📡 Voice Recv: **{'Active' if state.get('use_voice_recv') else 'Basic'}**"
                ),
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ No active meeting recording in this server.", ephemeral=True)

    # =========================================================================
    # Core Processing Logic
    # =========================================================================

    async def _stop_meeting(self, interaction: discord.Interaction, meeting_id: int):
        """Stops recording, disconnects, and triggers AI processing."""
        guild = interaction.guild
        state = self._active_meetings.get(guild.id)

        if not state or state["meeting_id"] != meeting_id:
            # Check if it's a stale button press
            db_meeting = await self.db.get_meeting(meeting_id)
            if db_meeting and db_meeting["status"] == "recording":
                await self.db.update_meeting_status(
                    meeting_id, "failed",
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    error_message="Meeting state lost (bot may have restarted)"
                )
            await interaction.followup.send(
                "⚠️ This meeting recording is no longer active.", ephemeral=True
            )
            return

        title = state["title"]
        sink = state["sink"]
        vc = state["voice_client"]
        started_at = state["started_at"]
        duration = int(time.time() - started_at)

        # Stop recording
        sink.stop()

        # Disconnect from voice
        try:
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        except Exception as e:
            logger.warning(f"[MEETING] Error disconnecting VC: {e}")

        # Finalize attendance
        await self.db.finalize_all_participants(meeting_id)

        # Update status to processing
        ended_at = datetime.now(timezone.utc).isoformat()
        await self.db.update_meeting_status(
            meeting_id, "processing",
            ended_at=ended_at,
            duration_seconds=duration
        )

        # Remove from active meetings
        self._active_meetings.pop(guild.id, None)

        # Update control panel embed
        try:
            output_ch_id = state.get("output_channel_id")
            ctrl_msg_id = state.get("control_message_id")
            if output_ch_id and ctrl_msg_id:
                channel = self.bot.get_channel(output_ch_id)
                if channel:
                    try:
                        ctrl_msg = await channel.fetch_message(ctrl_msg_id)
                        processing_embed = discord.Embed(
                            title="⏳ Meeting Processing",
                            description=(
                                f"**{title}**\n\n"
                                f"⏱️ Duration: **{fmt_duration(duration)}**\n"
                                f"🤖 Generating AI Meeting Minutes...\n\n"
                                f"*This may take a few minutes depending on meeting length.*"
                            ),
                            color=discord.Color.gold(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        await ctrl_msg.edit(embed=processing_embed, view=None)
                    except Exception:
                        pass
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ Meeting **{title}** stopped ({fmt_duration(duration)}).\n"
            f"🤖 AI is generating formal meeting minutes... Please wait.",
            ephemeral=True
        )

        # Process audio in background
        asyncio.create_task(
            self._process_recording(meeting_id, sink, guild, state)
        )

    async def _process_recording(self, meeting_id: int, sink: MeetingAudioSink,
                                  guild: discord.Guild, state: Dict[str, Any]):
        """Background task: saves audio, compresses, runs Gemini AI, posts results."""
        meeting_temp_dir = sink.temp_dir
        compressed_path = os.path.join(meeting_temp_dir, f"meeting_{meeting_id}_compressed.ogg")

        try:
            # Save per-user audio files from memory buffers
            audio_files = await sink.save_audio_files()

            if not audio_files:
                # No audio captured — update status and notify
                await self.db.update_meeting_status(
                    meeting_id, "failed",
                    error_message="No audio was captured. Ensure members were speaking and "
                                  "discord-ext-voice-recv is properly installed."
                )
                await self._post_error_embed(meeting_id, guild, state,
                                              "No audio was captured during this meeting.")
                return

            # Merge and compress audio files
            await merge_audio_files_ffmpeg(audio_files, compressed_path)

            if not os.path.exists(compressed_path):
                await self.db.update_meeting_status(
                    meeting_id, "failed", error_message="Audio compression failed."
                )
                await self._post_error_embed(meeting_id, guild, state, "Audio compression failed.")
                return

            # Run Gemini AI transcription
            speaker_names = sink.get_speaker_names()
            result = await generate_meeting_minutes(
                audio_path=compressed_path,
                meeting_temp_dir=meeting_temp_dir,
                speakers_hint=speaker_names
            )

            # Save results to database
            meeting = await self.db.get_meeting(meeting_id)
            output_channel = self.bot.get_channel(int(meeting["output_channel_id"])) if meeting else None

            output_msg_id = None
            if output_channel:
                output_msg_id = await self._post_minutes_embed(
                    meeting_id, meeting, result, output_channel, guild
                )

            await self.db.save_meeting_results(
                meeting_id=meeting_id,
                executive_summary=result.get("executive_summary", ""),
                key_points=result.get("key_points", []),
                action_items=result.get("action_items", []),
                qa_decisions=result.get("qa_and_decisions", []),
                full_transcript=result.get("full_transcript", ""),
                output_message_id=output_msg_id
            )

            logger.info(f"[MEETING] Meeting {meeting_id} processed successfully")

        except Exception as e:
            logger.error(f"[MEETING] Processing failed for meeting {meeting_id}: {e}", exc_info=True)
            await self.db.update_meeting_status(
                meeting_id, "failed", error_message=str(e)[:500]
            )
            await self._post_error_embed(meeting_id, guild, state, str(e))

        finally:
            # MANDATORY CLEANUP of local temp files
            # (Gemini cloud cleanup is handled inside generate_meeting_minutes)
            if os.path.exists(meeting_temp_dir):
                try:
                    shutil.rmtree(meeting_temp_dir, ignore_errors=True)
                    logger.info(f"[MEETING] Cleaned up temp dir: {meeting_temp_dir}")
                except Exception as cleanup_err:
                    logger.warning(f"[MEETING] Failed to clean temp dir: {cleanup_err}")

    async def _process_uploaded_audio(self, meeting_id: int, attachment: discord.Attachment,
                                       output_channel: discord.TextChannel, guild: discord.Guild):
        """Background task: downloads uploaded audio, compresses, runs Gemini AI."""
        meeting_temp_dir = os.path.join(TEMP_MEETINGS_DIR, f"meeting_{meeting_id}")
        os.makedirs(meeting_temp_dir, exist_ok=True)

        ext = os.path.splitext(attachment.filename)[1].lower() or ".mp3"
        raw_path = os.path.join(meeting_temp_dir, f"uploaded{ext}")
        compressed_path = os.path.join(meeting_temp_dir, f"meeting_{meeting_id}_compressed.ogg")

        try:
            # Download the file
            await attachment.save(raw_path)

            # Compress
            await compress_audio_ffmpeg(raw_path, compressed_path)

            if not os.path.exists(compressed_path):
                await self.db.update_meeting_status(
                    meeting_id, "failed", error_message="Audio compression failed."
                )
                return

            # Update status
            await self.db.update_meeting_status(meeting_id, "processing")

            # Run Gemini AI
            result = await generate_meeting_minutes(
                audio_path=compressed_path,
                meeting_temp_dir=meeting_temp_dir
            )

            meeting = await self.db.get_meeting(meeting_id)

            output_msg_id = None
            if output_channel:
                output_msg_id = await self._post_minutes_embed(
                    meeting_id, meeting, result, output_channel, guild
                )

            ended_at = datetime.now(timezone.utc).isoformat()
            await self.db.update_meeting_status(
                meeting_id, "processing", ended_at=ended_at
            )

            await self.db.save_meeting_results(
                meeting_id=meeting_id,
                executive_summary=result.get("executive_summary", ""),
                key_points=result.get("key_points", []),
                action_items=result.get("action_items", []),
                qa_decisions=result.get("qa_and_decisions", []),
                full_transcript=result.get("full_transcript", ""),
                output_message_id=output_msg_id
            )

            logger.info(f"[MEETING] Uploaded meeting {meeting_id} processed successfully")

        except Exception as e:
            logger.error(f"[MEETING] Upload processing failed for meeting {meeting_id}: {e}", exc_info=True)
            await self.db.update_meeting_status(
                meeting_id, "failed", error_message=str(e)[:500]
            )

        finally:
            # MANDATORY CLEANUP
            if os.path.exists(meeting_temp_dir):
                try:
                    shutil.rmtree(meeting_temp_dir, ignore_errors=True)
                except Exception:
                    pass

    # =========================================================================
    # Discord Embed & File Delivery
    # =========================================================================

    async def _post_minutes_embed(self, meeting_id: int, meeting: Dict[str, Any],
                                   result: Dict[str, Any],
                                   channel: discord.TextChannel,
                                   guild: discord.Guild) -> Optional[int]:
        """Posts a formal Meeting Minutes embed with attachment files."""
        try:
            # Build executive summary embed
            summary = result.get("executive_summary", "No summary available.")
            key_points = result.get("key_points", [])
            action_items = result.get("action_items", [])
            qa_decisions = result.get("qa_and_decisions", [])
            transcript = result.get("full_transcript", "")

            # Get participants
            participants = await self.db.get_participants(meeting_id)

            embed = discord.Embed(
                title=f"📋 Meeting Minutes: {meeting['title']}",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )

            # Executive Summary
            if summary:
                embed.add_field(
                    name="📝 Executive Summary",
                    value=summary[:1024],
                    inline=False
                )

            # Key Points
            if key_points:
                kp_text = "\n".join(f"• {p}" for p in key_points[:10])
                embed.add_field(
                    name=f"🔑 Key Points ({len(key_points)})",
                    value=kp_text[:1024],
                    inline=False
                )

            # Action Items
            if action_items:
                ai_text = "\n".join(f"☑️ {a}" for a in action_items[:10])
                embed.add_field(
                    name=f"✅ Action Items ({len(action_items)})",
                    value=ai_text[:1024],
                    inline=False
                )

            # Q&A / Decisions
            if qa_decisions:
                qa_text = "\n".join(f"💬 {q}" for q in qa_decisions[:8])
                embed.add_field(
                    name=f"❓ Q&A & Decisions ({len(qa_decisions)})",
                    value=qa_text[:1024],
                    inline=False
                )

            # Meeting Info
            duration_str = fmt_duration(meeting.get("duration_seconds", 0))
            info_lines = [
                f"**Host:** {meeting['host_name']}",
                f"**Channel:** {meeting['voice_channel_name']}",
                f"**Duration:** {duration_str}",
                f"**Participants:** {len(participants)}",
            ]
            if meeting.get("project_code"):
                info_lines.append(f"**Project:** {meeting['project_code']}")
            embed.add_field(
                name="ℹ️ Meeting Details",
                value="\n".join(info_lines),
                inline=False
            )

            embed.set_footer(text=f"Meeting ID: {meeting_id} • Powered by Gemini AI")

            # Generate attachment files
            files = []

            # 1. Meeting Minutes .md file
            md_content = self._generate_minutes_md(meeting, result, participants)
            md_buf = io.BytesIO(md_content.encode("utf-8"))
            files.append(discord.File(md_buf, filename=f"Meeting_Minutes_{meeting_id}.md"))

            # 2. Full Transcript .txt file
            if transcript:
                txt_buf = io.BytesIO(transcript.encode("utf-8"))
                files.append(discord.File(txt_buf, filename=f"Full_Transcript_{meeting_id}.txt"))

            msg = await channel.send(embed=embed, files=files)
            return msg.id

        except Exception as e:
            logger.error(f"[MEETING] Failed to post minutes embed: {e}", exc_info=True)
            return None

    async def _post_error_embed(self, meeting_id: int, guild: discord.Guild,
                                 state: Dict[str, Any], error_msg: str):
        """Posts an error embed when meeting processing fails."""
        try:
            output_ch_id = state.get("output_channel_id")
            if not output_ch_id:
                meeting = await self.db.get_meeting(meeting_id)
                if meeting:
                    output_ch_id = int(meeting["output_channel_id"])

            if output_ch_id:
                channel = self.bot.get_channel(output_ch_id)
                if channel:
                    embed = discord.Embed(
                        title="❌ Meeting Minutes Generation Failed",
                        description=(
                            f"**Meeting ID:** {meeting_id}\n"
                            f"**Error:** {error_msg[:1500]}\n\n"
                            f"You can try uploading the audio manually with `/meeting upload_audio`."
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"[MEETING] Failed to post error embed: {e}")

    def _generate_minutes_md(self, meeting: Dict[str, Any], result: Dict[str, Any],
                              participants: List[Dict[str, Any]]) -> str:
        """Generates a formatted Markdown minutes document."""
        lines = [
            f"# Meeting Minutes: {meeting['title']}",
            "",
            f"**Date:** {meeting.get('started_at', 'N/A')}",
            f"**Host:** {meeting['host_name']}",
            f"**Voice Channel:** {meeting['voice_channel_name']}",
            f"**Duration:** {fmt_duration(meeting.get('duration_seconds', 0))}",
        ]
        if meeting.get("project_code"):
            lines.append(f"**Project Code:** {meeting['project_code']}")
        lines.append("")

        # Executive Summary
        lines.append("## Executive Summary")
        lines.append(result.get("executive_summary", "No summary available."))
        lines.append("")

        # Key Points
        key_points = result.get("key_points", [])
        if key_points:
            lines.append("## Key Discussion Points")
            for p in key_points:
                lines.append(f"- {p}")
            lines.append("")

        # Action Items
        action_items = result.get("action_items", [])
        if action_items:
            lines.append("## Action Items")
            for a in action_items:
                lines.append(f"- [ ] {a}")
            lines.append("")

        # Q&A / Decisions
        qa_decisions = result.get("qa_and_decisions", [])
        if qa_decisions:
            lines.append("## Q&A & Decisions")
            for q in qa_decisions:
                lines.append(f"- {q}")
            lines.append("")

        # Attendance Report
        if participants:
            lines.append("## Attendance Report")
            lines.append("")
            lines.append("| Participant | Joined | Left | Duration |")
            lines.append("|-------------|--------|------|----------|")
            for p in participants:
                joined = p.get("joined_at", "N/A")
                if joined and len(joined) > 19:
                    joined = joined[:19]
                left = p.get("left_at", "Still Present")
                if left and len(left) > 19:
                    left = left[:19]
                dur = fmt_duration(p.get("total_seconds", 0))
                lines.append(f"| {p['user_name']} | {joined} | {left} | {dur} |")
            lines.append("")

        # Full Transcript
        transcript = result.get("full_transcript", "")
        if transcript:
            lines.append("## Full Transcript")
            lines.append("")
            lines.append(transcript)
            lines.append("")

        lines.append("---")
        lines.append("*Generated by Meeting Assistant Bot — Powered by Gemini AI*")

        return "\n".join(lines)

    # =========================================================================
    # Voice State Tracking (Attendance)
    # =========================================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        """Tracks participant joins/leaves for active meetings."""
        if member.bot:
            return

        guild_id = member.guild.id
        state = self._active_meetings.get(guild_id)
        if not state:
            return

        meeting_id = state["meeting_id"]
        target_vc = state["voice_channel"]

        # Member joined the meeting voice channel
        if after.channel and after.channel.id == target_vc.id:
            if not before.channel or before.channel.id != target_vc.id:
                await self.db.add_participant(meeting_id, member.id, member.display_name)

        # Member left the meeting voice channel
        if before.channel and before.channel.id == target_vc.id:
            if not after.channel or after.channel.id != target_vc.id:
                await self.db.mark_participant_left(meeting_id, member.id)

    # =========================================================================
    # Public API for Web Dashboard
    # =========================================================================

    def get_active_meetings(self) -> Dict[int, Dict[str, Any]]:
        """Returns active meeting states for all guilds (for dashboard)."""
        result = {}
        for guild_id, state in self._active_meetings.items():
            result[guild_id] = {
                "meeting_id": state["meeting_id"],
                "title": state["title"],
                "voice_channel_name": state["voice_channel"].name if state.get("voice_channel") else "Unknown",
                "voice_channel_id": str(state["voice_channel"].id) if state.get("voice_channel") else None,
                "started_at": state.get("started_at", 0),
                "elapsed_seconds": int(time.time() - state.get("started_at", time.time())),
                "use_voice_recv": state.get("use_voice_recv", False)
            }
        return result

    async def stop_meeting_from_dashboard(self, guild_id: int, meeting_id: int) -> Dict[str, Any]:
        """Stops a meeting from the web dashboard without a Discord interaction."""
        state = self._active_meetings.get(guild_id)
        if not state or state["meeting_id"] != meeting_id:
            return {"success": False, "error": "No active recording found for this meeting."}

        title = state["title"]
        sink = state["sink"]
        vc = state["voice_client"]
        started_at = state["started_at"]
        duration = int(time.time() - started_at)

        # Stop recording
        sink.stop()

        # Disconnect
        try:
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        except Exception:
            pass

        # Finalize attendance
        await self.db.finalize_all_participants(meeting_id)

        ended_at = datetime.now(timezone.utc).isoformat()
        await self.db.update_meeting_status(
            meeting_id, "processing", ended_at=ended_at, duration_seconds=duration
        )

        self._active_meetings.pop(guild_id, None)

        guild = self.bot.get_guild(guild_id)

        # Process in background
        asyncio.create_task(
            self._process_recording(meeting_id, sink, guild, state)
        )

        return {"success": True, "title": title, "duration": duration}

    async def start_meeting_from_dashboard(self, guild_id: int, voice_channel_id: int,
                                            title: str, output_channel_id: int,
                                            host_name: str = "Dashboard",
                                            project_code: Optional[str] = None) -> Dict[str, Any]:
        """Starts a meeting from the web dashboard."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found."}

        if guild_id in self._active_meetings:
            return {"success": False, "error": "A meeting is already recording in this server."}

        voice_channel = guild.get_channel(voice_channel_id)
        if not voice_channel:
            return {"success": False, "error": "Voice channel not found."}

        output_channel = guild.get_channel(output_channel_id)
        if not output_channel:
            return {"success": False, "error": "Output channel not found."}

        # Create meeting record
        meeting_id = await self.db.create_meeting(
            guild_id=guild_id,
            title=title,
            voice_channel_id=voice_channel_id,
            voice_channel_name=voice_channel.name,
            output_channel_id=output_channel_id,
            host_id=0,
            host_name=host_name,
            project_code=project_code
        )

        # Track initial participants
        for m in voice_channel.members:
            if not m.bot:
                await self.db.add_participant(meeting_id, m.id, m.display_name)

        # Connect to voice
        sink = MeetingAudioSink(meeting_id)
        try:
            try:
                from discord.ext import voice_recv
                vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
                vc.listen(voice_recv.BasicSink(sink.write))
                use_voice_recv = True
            except (ImportError, Exception):
                vc = await voice_channel.connect()
                use_voice_recv = False
        except Exception as e:
            await self.db.update_meeting_status(
                meeting_id, "failed", error_message=f"Failed to connect: {e}"
            )
            return {"success": False, "error": f"Failed to connect to voice: {e}"}

        self._active_meetings[guild_id] = {
            "meeting_id": meeting_id,
            "title": title,
            "voice_channel": voice_channel,
            "voice_client": vc,
            "sink": sink,
            "use_voice_recv": use_voice_recv,
            "started_at": time.time(),
            "output_channel_id": output_channel_id
        }

        # Post control embed
        embed = discord.Embed(
            title="🔴 Meeting Recording Active",
            description=(
                f"**{title}**\n\n"
                f"🎙️ Voice Channel: **{voice_channel.name}**\n"
                f"👤 Host: **{host_name}** (via Dashboard)\n"
                f"📁 Project: **{project_code or 'N/A'}**\n"
                f"📤 Output: {output_channel.mention}\n\n"
                f"*Recording in progress...*"
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Meeting ID: {meeting_id}")

        view = discord.ui.View(timeout=None)
        view.add_item(DynamicMeetingStopButton(meeting_id))
        view.add_item(DynamicMeetingNoteButton(meeting_id))

        ctrl_msg = await output_channel.send(embed=embed, view=view)
        self._active_meetings[guild_id]["control_message_id"] = ctrl_msg.id

        return {"success": True, "meeting_id": meeting_id}


async def setup(bot: commands.Bot):
    """Cog setup hook for MeetingAssistantBot."""
    await bot.add_cog(MeetingsCog(bot))
