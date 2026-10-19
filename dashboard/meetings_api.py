"""
Meetings API Blueprint for Quart Web Dashboard.
Provides RESTful endpoints for:
- Meeting bot status & guild connection state
- Guild meeting settings & channel configuration
- Live recording controls (Start/Stop from dashboard)
- Meeting history & minutes viewer
- Exporting markdown minutes and full transcripts
- Participant attendance tracking
"""

import io
import json
import logging
from typing import Optional, Dict, Any, List

import discord
from quart import Blueprint, request, jsonify, session, send_file, current_app

from services.meetings_db import MeetingsDB

logger = logging.getLogger("MeetingsAPI")

meetings_bp = Blueprint("meetings_api", __name__, url_prefix="/api/meetings")


@meetings_bp.before_request
async def enforce_meetings_operator_access():
    """Restricts modifying endpoints to Administrator, Master Admin, and Moderator."""
    if request.path.startswith("/api/internal/"):
        return
    if request.method in ("POST", "PUT", "DELETE"):
        role = session.get("role", "Viewer")
        if str(role).strip().lower() not in ("administrator", "master admin", "moderator"):
            return jsonify({
                "error": "Forbidden",
                "message": "Operator privileges (Moderator or Administrator) required to perform this action."
            }), 403


def get_meeting_bot():
    """Retrieves the dedicated MeetingAssistantBot instance, or falls back to discord_bot."""
    bot = getattr(current_app, "meeting_bot", None)
    if bot:
        return bot
    return getattr(current_app, "discord_bot", None)


def get_primary_bot():
    """Retrieves the primary Discord bot instance."""
    return getattr(current_app, "discord_bot", None)


def get_meetings_cog():
    """Retrieves MeetingsCog from the meeting bot instance."""
    bot = get_meeting_bot()
    if bot:
        cog = bot.get_cog("MeetingsCog")
        if cog:
            return cog
    # Fallback to primary bot
    pbot = get_primary_bot()
    if pbot:
        return pbot.get_cog("MeetingsCog")
    return None


def _format_duration(seconds: int) -> str:
    """Formats seconds into human-readable duration."""
    if not seconds:
        return "0s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# =============================================================================
# Bot Status & Guild State
# =============================================================================

@meetings_bp.route("/guilds/<guild_id>/status", methods=["GET"])
async def get_guild_meeting_status(guild_id: str):
    """Returns the status of the Meeting Assistant Bot for a guild."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    bot = get_meeting_bot()
    cog = get_meetings_cog()
    db = MeetingsDB.get_instance()

    bot_online = False
    bot_user = None
    bot_id = None
    guild_connected = False

    if bot and bot.is_ready():
        bot_online = True
        bot_user = str(bot.user)
        bot_id = str(bot.user.id)
        guild = bot.get_guild(gid)
        guild_connected = guild is not None
    else:
        # Check if primary bot is connected as a fallback reference
        pbot = get_primary_bot()
        if pbot and pbot.get_guild(gid):
            # Guild exists in primary bot
            guild_connected = False

    # Check for active recording in cog memory
    active_state = None
    if cog:
        active_map = cog.get_active_meetings()
        if gid in active_map:
            active_state = active_map[gid]

    # Get DB stats
    stats = await db.get_guild_stats(gid)

    return jsonify({
        "success": True,
        "bot_online": bot_online,
        "bot_user": bot_user,
        "bot_id": bot_id,
        "guild_connected": guild_connected,
        "active_meeting": active_state,
        "stats": stats
    })


# =============================================================================
# Guild Channels & Settings
# =============================================================================

@meetings_bp.route("/guilds/<guild_id>/channels", methods=["GET"])
async def get_guild_channels(guild_id: str):
    """Returns text channels, voice channels, and roles for meeting configuration."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    # Look up guild in meeting bot or primary bot
    bot = get_meeting_bot()
    guild = bot.get_guild(gid) if bot and bot.is_ready() else None
    if not guild:
        pbot = get_primary_bot()
        if pbot and pbot.is_ready():
            guild = pbot.get_guild(gid)

    if not guild:
        return jsonify({
            "success": False,
            "error": "Guild not found or bot is not in this server",
            "voice_channels": [],
            "text_channels": [],
            "roles": []
        }), 404

    voice_channels = [
        {"id": str(c.id), "name": c.name, "member_count": len(c.members)}
        for c in guild.voice_channels
    ]
    text_channels = [
        {"id": str(c.id), "name": c.name, "label": f"#{c.name}"}
        for c in guild.text_channels
    ]
    roles = [
        {"id": str(r.id), "name": r.name, "color": str(r.color)}
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if not r.managed and r.name != "@everyone"
    ]

    return jsonify({
        "success": True,
        "voice_channels": voice_channels,
        "text_channels": text_channels,
        "roles": roles
    })


@meetings_bp.route("/guilds/<guild_id>/settings", methods=["GET"])
async def get_settings(guild_id: str):
    """Retrieves meeting settings for a guild."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    db = MeetingsDB.get_instance()
    settings = await db.get_settings(gid)

    # Convert snowflake IDs to strings for JS safety
    serialized_settings = {
        "guild_id": str(settings["guild_id"]),
        "default_output_channel_id": str(settings["default_output_channel_id"]) if settings["default_output_channel_id"] else None,
        "allowed_role_ids": [str(r) for r in settings.get("allowed_role_ids", [])]
    }

    return jsonify({
        "success": True,
        "settings": serialized_settings
    })


@meetings_bp.route("/guilds/<guild_id>/settings", methods=["POST"])
async def save_settings(guild_id: str):
    """Updates meeting settings for a guild."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    data = await request.get_json()
    if not data:
        return jsonify({"error": "No JSON payload provided"}), 400

    output_channel_id = None
    if data.get("default_output_channel_id"):
        try:
            output_channel_id = int(data["default_output_channel_id"])
        except ValueError:
            return jsonify({"error": "Invalid output_channel_id"}), 400

    allowed_role_ids = []
    if "allowed_role_ids" in data and isinstance(data["allowed_role_ids"], list):
        for r in data["allowed_role_ids"]:
            try:
                allowed_role_ids.append(int(r))
            except (ValueError, TypeError):
                continue

    db = MeetingsDB.get_instance()
    await db.save_settings(
        guild_id=gid,
        output_channel_id=output_channel_id,
        allowed_role_ids=allowed_role_ids
    )

    logger.info(f"[MEETINGS-API] Updated settings for guild {gid} by user {session.get('user', {}).get('username')}")
    return jsonify({
        "success": True,
        "message": "Settings updated successfully."
    })


# =============================================================================
# Live Recording Controls
# =============================================================================

@meetings_bp.route("/guilds/<guild_id>/active", methods=["GET"])
async def get_active_meeting(guild_id: str):
    """Returns currently active meeting recording for a guild."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    cog = get_meetings_cog()
    if not cog:
        return jsonify({"success": True, "active_meeting": None})

    active_map = cog.get_active_meetings()
    active_meeting = active_map.get(gid)

    return jsonify({
        "success": True,
        "active_meeting": active_meeting
    })


@meetings_bp.route("/guilds/<guild_id>/start", methods=["POST"])
async def start_recording(guild_id: str):
    """Starts a meeting recording from the web dashboard."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    cog = get_meetings_cog()
    if not cog:
        return jsonify({"error": "Meeting Assistant Bot is not loaded or online."}), 503

    data = await request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Meeting title is required."}), 400

    voice_channel_id = data.get("voice_channel_id")
    if not voice_channel_id:
        return jsonify({"error": "Voice channel must be selected."}), 400
    try:
        vc_id = int(voice_channel_id)
    except ValueError:
        return jsonify({"error": "Invalid voice_channel_id"}), 400

    # Output channel: from request or guild settings
    output_channel_id = data.get("output_channel_id")
    db = MeetingsDB.get_instance()
    if not output_channel_id:
        settings = await db.get_settings(gid)
        output_channel_id = settings.get("default_output_channel_id")

    if not output_channel_id:
        return jsonify({
            "error": "No output channel specified and no default output channel is configured in settings."
        }), 400

    try:
        out_id = int(output_channel_id)
    except ValueError:
        return jsonify({"error": "Invalid output_channel_id"}), 400

    host_name = session.get("user", {}).get("username", "Dashboard Operator")
    project_code = data.get("project_code", "").strip() or None

    result = await cog.start_meeting_from_dashboard(
        guild_id=gid,
        voice_channel_id=vc_id,
        title=title,
        output_channel_id=out_id,
        host_name=host_name,
        project_code=project_code
    )

    if not result.get("success"):
        return jsonify({"error": result.get("error", "Failed to start meeting.")}), 400

    return jsonify({
        "success": True,
        "meeting_id": result.get("meeting_id"),
        "message": f"Meeting '{title}' started recording."
    })


@meetings_bp.route("/guilds/<guild_id>/stop", methods=["POST"])
async def stop_recording(guild_id: str):
    """Stops the active meeting recording for a guild from the dashboard."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    cog = get_meetings_cog()
    if not cog:
        return jsonify({"error": "Meeting Assistant Bot is not loaded or online."}), 503

    data = await request.get_json() or {}
    meeting_id = data.get("meeting_id")

    if not meeting_id:
        # Find active meeting for this guild
        active_map = cog.get_active_meetings()
        if gid in active_map:
            meeting_id = active_map[gid]["meeting_id"]

    if not meeting_id:
        return jsonify({"error": "No active meeting found for this server."}), 400

    try:
        m_id = int(meeting_id)
    except ValueError:
        return jsonify({"error": "Invalid meeting_id"}), 400

    result = await cog.stop_meeting_from_dashboard(guild_id=gid, meeting_id=m_id)
    if not result.get("success"):
        return jsonify({"error": result.get("error", "Failed to stop meeting.")}), 400

    return jsonify({
        "success": True,
        "message": f"Recording stopped. Duration: {_format_duration(result.get('duration', 0))}. AI minutes are processing in the background."
    })


# =============================================================================
# Meeting History & Details
# =============================================================================

@meetings_bp.route("/guilds/<guild_id>/list", methods=["GET"])
async def list_meetings(guild_id: str):
    """Returns paginated meeting history for a guild."""
    try:
        gid = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id"}), 400

    status = request.args.get("status", "").strip() or None
    page = max(1, int(request.args.get("page", 1)))
    limit = min(100, max(5, int(request.args.get("limit", 20))))
    offset = (page - 1) * limit

    db = MeetingsDB.get_instance()
    data = await db.list_meetings(guild_id=gid, status=status, limit=limit, offset=offset)

    return jsonify({
        "success": True,
        "meetings": data["meetings"],
        "total": data["total"],
        "page": page,
        "limit": limit
    })


@meetings_bp.route("/<int:meeting_id>", methods=["GET"])
async def get_meeting_detail(meeting_id: int):
    """Returns full details of a meeting including participants."""
    db = MeetingsDB.get_instance()
    meeting = await db.get_meeting(meeting_id)
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    participants = await db.get_participants(meeting_id)

    # Format durations
    meeting["duration_formatted"] = _format_duration(meeting.get("duration_seconds", 0))
    for p in participants:
        p["duration_formatted"] = _format_duration(p.get("total_seconds", 0))

    return jsonify({
        "success": True,
        "meeting": meeting,
        "participants": participants
    })


@meetings_bp.route("/<int:meeting_id>", methods=["DELETE"])
async def delete_meeting(meeting_id: int):
    """Deletes a meeting record."""
    db = MeetingsDB.get_instance()
    success = await db.delete_meeting(meeting_id)
    if not success:
        return jsonify({"error": "Meeting not found or could not be deleted."}), 404

    logger.info(f"[MEETINGS-API] Meeting {meeting_id} deleted by user {session.get('user', {}).get('username')}")
    return jsonify({
        "success": True,
        "message": f"Meeting {meeting_id} deleted successfully."
    })


# =============================================================================
# Exports: Markdown Minutes & Text Transcripts
# =============================================================================

@meetings_bp.route("/<int:meeting_id>/export/markdown", methods=["GET"])
async def export_markdown(meeting_id: int):
    """Downloads the Meeting Minutes as a formatted Markdown file."""
    db = MeetingsDB.get_instance()
    meeting = await db.get_meeting(meeting_id)
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    participants = await db.get_participants(meeting_id)

    lines = [
        f"# Meeting Minutes: {meeting['title']}",
        "",
        f"**Date:** {meeting.get('started_at', 'N/A')}",
        f"**Host:** {meeting['host_name']}",
        f"**Voice Channel:** {meeting['voice_channel_name']}",
        f"**Duration:** {_format_duration(meeting.get('duration_seconds', 0))}",
    ]
    if meeting.get("project_code"):
        lines.append(f"**Project Code:** {meeting['project_code']}")
    lines.append("")

    lines.append("## Executive Summary")
    lines.append(meeting.get("executive_summary") or "No summary available.")
    lines.append("")

    key_points = meeting.get("key_points", [])
    if key_points:
        lines.append("## Key Discussion Points")
        for p in key_points:
            lines.append(f"- {p}")
        lines.append("")

    action_items = meeting.get("action_items", [])
    if action_items:
        lines.append("## Action Items")
        for a in action_items:
            lines.append(f"- [ ] {a}")
        lines.append("")

    qa_decisions = meeting.get("qa_decisions", [])
    if qa_decisions:
        lines.append("## Q&A & Decisions")
        for q in qa_decisions:
            lines.append(f"- {q}")
        lines.append("")

    if participants:
        lines.append("## Attendance Report")
        lines.append("")
        lines.append("| Participant | Joined | Left | Duration |")
        lines.append("|-------------|--------|------|----------|")
        for p in participants:
            joined = (p.get("joined_at") or "N/A")[:19]
            left = (p.get("left_at") or "Still Present")[:19]
            dur = _format_duration(p.get("total_seconds", 0))
            lines.append(f"| {p['user_name']} | {joined} | {left} | {dur} |")
        lines.append("")

    transcript = meeting.get("full_transcript", "")
    if transcript:
        lines.append("## Full Transcript")
        lines.append("")
        lines.append(transcript)
        lines.append("")

    lines.append("---")
    lines.append("*Generated by Meeting Assistant Bot — Powered by Gemini AI*")

    md_content = "\n".join(lines)
    buf = io.BytesIO(md_content.encode("utf-8"))
    buf.seek(0)

    filename = f"Meeting_Minutes_{meeting_id}_{meeting['title'].replace(' ', '_')[:30]}.md"
    return await send_file(
        buf,
        mimetype="text/markdown",
        as_attachment=True,
        attachment_filename=filename
    )


@meetings_bp.route("/<int:meeting_id>/export/transcript", methods=["GET"])
async def export_transcript(meeting_id: int):
    """Downloads the full raw transcript as a text file."""
    db = MeetingsDB.get_instance()
    meeting = await db.get_meeting(meeting_id)
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    transcript = meeting.get("full_transcript") or "No transcript available."
    buf = io.BytesIO(transcript.encode("utf-8"))
    buf.seek(0)

    filename = f"Transcript_Meeting_{meeting_id}.txt"
    return await send_file(
        buf,
        mimetype="text/plain",
        as_attachment=True,
        attachment_filename=filename
    )
