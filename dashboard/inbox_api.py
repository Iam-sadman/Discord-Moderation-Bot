"""
Dashboard Direct Messages Inbox API Blueprint.
Provides RESTful endpoints for:
- Browsing conversation threads with search and filter
- Fetching full message history and marking threads as read
- Dispatching staff DM replies via Discord bot
- Updating conversation status (open, closed, archived)
- Starting new DM threads with server members
- Configurable auto-responder and Discord relay channel settings
- Exporting full conversation transcripts (HTML / TXT)
"""

import io
import html
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import discord
from quart import Blueprint, request, jsonify, session, send_file, current_app

from services.inbox_db import InboxDB, DEFAULT_RELAY_CHANNEL_ID

logger = logging.getLogger("InboxAPI")

inbox_bp = Blueprint("inbox_api", __name__)


@inbox_bp.before_request
async def enforce_inbox_operator_access():
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


def get_bot():
    """Retrieves the running Discord bot instance attached to Quart."""
    return getattr(current_app, "discord_bot", None)


def get_inbox_cog():
    """Retrieves the InboxCog instance from the running Discord bot."""
    bot = get_bot()
    if bot:
        return bot.get_cog("InboxCog")
    return None


async def record_audit_event(action: str, target: str, details: str):
    """Safely records dashboard audit log if dashboard.security is available."""
    try:
        from dashboard.security import log_dashboard_event
        user_id = session.get("user_id", "admin")
        await log_dashboard_event(action=action, target=target, details=details, user_id=user_id)
    except Exception:
        pass


# =============================================================================
# Conversations Endpoints
# =============================================================================

@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations", methods=["GET"])
async def api_list_conversations(guild_id: int):
    """Lists conversations with optional status filtering and search query."""
    status = request.args.get("status", "all")
    search = request.args.get("search", "")
    limit = min(int(request.args.get("limit", 50)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)

    db = InboxDB.get_instance()
    convs, total = await db.list_conversations(
        guild_id=guild_id,
        status=status,
        search=search,
        limit=limit,
        offset=offset
    )

    unread_total = await db.get_total_unread_count(guild_id)

    return jsonify({
        "success": True,
        "conversations": convs,
        "total": total,
        "unread_total": unread_total
    })


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations/<int:conv_id>", methods=["GET"])
async def api_get_conversation(guild_id: int, conv_id: int):
    """Fetches conversation metadata and chronological message history, marking thread read."""
    db = InboxDB.get_instance()
    conv = await db.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    # Automatically mark conversation read
    await db.mark_as_read(conv_id)
    conv["unread_count"] = 0

    messages = await db.get_messages(conv_id, limit=150)

    return jsonify({
        "success": True,
        "conversation": conv,
        "messages": messages
    })


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations/<int:conv_id>/reply", methods=["POST"])
async def api_reply_conversation(guild_id: int, conv_id: int):
    """Sends a direct message reply to the member via Discord bot."""
    data = await request.get_json() or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"error": "Message content cannot be empty"}), 400

    db = InboxDB.get_instance()
    conv = await db.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    bot = get_bot()
    cog = get_inbox_cog()
    if not bot:
        return jsonify({"error": "Discord bot is not running"}), 503

    target_user_id = conv["user_id"]
    staff_name = session.get("user_id") or session.get("username") or "Web Dashboard Admin"
    staff_id = 0

    if cog:
        success, err = await cog.send_staff_reply(
            target_user_id=target_user_id,
            content=content,
            staff_name=staff_name,
            staff_id=staff_id,
            origin="staff_dashboard"
        )
    else:
        # Fallback if cog not yet loaded
        try:
            user = bot.get_user(target_user_id) or await bot.fetch_user(target_user_id)
            embed = discord.Embed(
                title="💬 Official Server Staff Message",
                description=content,
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Sent by Staff: {staff_name}")
            sent_msg = await user.send(embed=embed)
            await db.add_message(
                conversation_id=conv_id,
                sender_type="staff_dashboard",
                author_id=staff_id,
                author_name=staff_name,
                content=content,
                discord_message_id=sent_msg.id,
                is_read=1
            )
            success, err = True, None
        except Exception as e:
            success, err = False, str(e)

    if not success:
        return jsonify({"error": f"Failed to send DM to member: {err}"}), 400

    # Also log a note to the staff channel if configured
    settings = await db.get_settings(guild_id)
    channel_id = settings.get("notify_channel_id") or DEFAULT_RELAY_CHANNEL_ID
    if channel_id and bot:
        channel = bot.get_channel(channel_id)
        if channel and hasattr(channel, "send"):
            try:
                await channel.send(
                    content=f"💬 **[Dashboard] {staff_name}** replied to <@{target_user_id}> via DM:\n> {content[:250]}",
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception:
                pass

    await record_audit_event(
        action="SEND_INBOX_DM",
        target=f"User {target_user_id} ({conv.get('username')})",
        details=f"Staff DM sent from dashboard by {staff_name}"
    )

    # Return refreshed messages
    refreshed_messages = await db.get_messages(conv_id, limit=50)

    return jsonify({
        "success": True,
        "message": "Direct message sent successfully",
        "messages": refreshed_messages
    })


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations/<int:conv_id>/status", methods=["POST"])
async def api_update_status(guild_id: int, conv_id: int):
    """Updates thread status (open, closed, archived)."""
    data = await request.get_json() or {}
    status = str(data.get("status", "open")).lower().strip()

    db = InboxDB.get_instance()
    await db.update_conversation_status(conv_id, status)
    return jsonify({"success": True, "status": status})


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations/<int:conv_id>/read", methods=["POST"])
async def api_mark_read(guild_id: int, conv_id: int):
    """Marks conversation read."""
    db = InboxDB.get_instance()
    await db.mark_as_read(conv_id)
    return jsonify({"success": True})


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/send_new", methods=["POST"])
async def api_send_new(guild_id: int):
    """Starts a new DM conversation with any server member."""
    data = await request.get_json() or {}
    user_id_raw = data.get("user_id")
    content = str(data.get("content", "")).strip()

    if not user_id_raw or not content:
        return jsonify({"error": "Member ID and message content are required"}), 400

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid Discord User ID"}), 400

    bot = get_bot()
    if not bot:
        return jsonify({"error": "Discord bot is not running"}), 503

    db = InboxDB.get_instance()
    cog = get_inbox_cog()
    staff_name = session.get("user_id") or "Web Dashboard Admin"

    if cog:
        success, err = await cog.send_staff_reply(
            target_user_id=user_id,
            content=content,
            staff_name=staff_name,
            staff_id=0,
            origin="staff_dashboard"
        )
    else:
        try:
            target_user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            embed = discord.Embed(
                title="💬 Official Server Staff Message",
                description=content,
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Sent by Staff: {staff_name}")
            sent_msg = await target_user.send(embed=embed)
            conv = await db.get_or_create_conversation(
                user_id=user_id,
                username=target_user.name,
                display_name=getattr(target_user, "display_name", target_user.name),
                avatar_url=target_user.display_avatar.url if target_user.display_avatar else "",
                guild_id=guild_id
            )
            await db.add_message(
                conversation_id=conv["id"],
                sender_type="staff_dashboard",
                author_id=0,
                author_name=staff_name,
                content=content,
                discord_message_id=sent_msg.id,
                is_read=1
            )
            success, err = True, None
        except Exception as e:
            success, err = False, str(e)

    if not success:
        return jsonify({"error": f"Failed to send DM: {err}"}), 400

    conv = await db.get_conversation_by_user(user_id)
    return jsonify({
        "success": True,
        "message": "Direct message initiated successfully",
        "conversation_id": conv["id"] if conv else None
    })


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/unread_count", methods=["GET"])
async def api_unread_count(guild_id: int):
    """Lightweight endpoint for polling unread message count."""
    db = InboxDB.get_instance()
    count = await db.get_total_unread_count(guild_id)
    return jsonify({"success": True, "unread_count": count})


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/settings", methods=["GET", "POST"])
async def api_inbox_settings(guild_id: int):
    """Gets or updates inbox settings (auto-reply, notify channel)."""
    db = InboxDB.get_instance()
    if request.method == "POST":
        data = await request.get_json() or {}
        await db.update_settings(guild_id, data)
        return jsonify({"success": True, "message": "Settings saved successfully"})

    settings = await db.get_settings(guild_id)
    return jsonify({"success": True, "settings": settings})


@inbox_bp.route("/api/guild/<int:guild_id>/inbox/conversations/<int:conv_id>/export", methods=["GET"])
async def api_export_conversation(guild_id: int, conv_id: int):
    """Generates an HTML or TXT transcript download."""
    export_format = request.args.get("format", "html").lower()
    db = InboxDB.get_instance()
    conv = await db.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    messages = await db.get_messages(conv_id, limit=500)
    username = conv.get("username", "user")

    if export_format == "txt":
        txt_lines = [
            "=" * 70,
            f"DISCORD INBOX DIRECT MESSAGE TRANSCRIPT",
            "=" * 70,
            f"Member:     {conv.get('display_name')} (@{username})",
            f"User ID:    {conv.get('user_id')}",
            f"Status:     {conv.get('status', 'open').upper()}",
            f"Exported:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "=" * 70,
            "CONVERSATION LOG:",
            ""
        ]
        for m in messages:
            sender_tag = f" [{m.get('sender_type', '').upper()}]"
            ts = m.get("sent_at", "")
            author = m.get("author_name", "Unknown")
            txt_lines.append(f"[{ts}] {author}{sender_tag}:")
            if m.get("content"):
                txt_lines.append(f"  {m.get('content')}")
            for att in (m.get("attachments") or []):
                txt_lines.append(f"  [Attachment: {att.get('filename')} - {att.get('url')}]")
            txt_lines.append("")
        txt_lines.append("=" * 70)
        content_bytes = "\n".join(txt_lines).encode("utf-8")
        filename = f"inbox_{username}_{conv_id}.txt"
        mimetype = "text/plain"
    else:
        # HTML Export
        msg_html_parts = []
        for m in messages:
            is_staff = m.get("sender_type") in ("staff_dashboard", "staff_discord")
            bubble_class = "msg-staff" if is_staff else "msg-user"
            author_title = f"{html.escape(m.get('author_name', ''))} ({'Staff' if is_staff else 'Member'})"
            body = html.escape(m.get("content", "")).replace("\n", "<br>")
            att_html = ""
            for a in (m.get("attachments") or []):
                a_url = html.escape(a.get("url", ""))
                a_name = html.escape(a.get("filename", "File"))
                att_html += f'<div style="margin-top:6px;"><a href="{a_url}" target="_blank" style="color:#5865F2;">📎 {a_name}</a></div>'

            msg_html_parts.append(f"""
            <div class="msg {bubble_class}">
                <div class="msg-header"><strong>{author_title}</strong> • <small>{html.escape(str(m.get('sent_at', '')))}</small></div>
                <div class="msg-body">{body}</div>
                {att_html}
            </div>
            """)

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Inbox Transcript - @{username}</title>
    <style>
        body {{ background:#1e1f22; color:#dbdee1; font-family:'Segoe UI',sans-serif; padding:20px; }}
        .header {{ background:#2b2d31; padding:16px; border-radius:8px; margin-bottom:20px; border-left:4px solid #5865F2; }}
        .msg {{ padding:10px 14px; border-radius:8px; margin-bottom:10px; max-width:80%; }}
        .msg-user {{ background:#313338; margin-right:auto; }}
        .msg-staff {{ background:#2b2d31; border:1px solid #5865F2; margin-left:auto; }}
        .msg-header {{ font-size:12px; color:#949ba4; margin-bottom:4px; }}
        .msg-body {{ line-height:1.4; }}
    </style>
</head>
<body>
    <div class="header">
        <h2>📥 Direct Message Transcript: @{html.escape(username)}</h2>
        <p>User ID: <code>{conv.get('user_id')}</code> • Status: <strong>{conv.get('status')}</strong></p>
    </div>
    {"".join(msg_html_parts)}
</body>
</html>"""
        content_bytes = html_content.encode("utf-8")
        filename = f"inbox_{username}_{conv_id}.html"
        mimetype = "text/html"

    bio = io.BytesIO(content_bytes)
    return await send_file(bio, as_attachment=True, attachment_filename=filename, mimetype=mimetype)

