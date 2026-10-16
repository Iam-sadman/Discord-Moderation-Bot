"""
Dashboard Support Tickets Management API Blueprint.
Provides RESTful endpoints for:
- Guild ticket settings & channel configuration
- Interactive category button builder & per-category role/member routing
- Live Discord panel publishing & synchronization
- Active & closed tickets management
- Dynamic in-thread role/member assignment from the web
- As-is conversation transcripts and HTML/TXT downloads
"""

import io
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List

import discord
from quart import Blueprint, request, jsonify, session, send_file, current_app

from services.tickets_db import TicketsDB
from services.tickets_exporter import generate_html_transcript, generate_txt_transcript

logger = logging.getLogger("TicketsAPI")

tickets_bp = Blueprint("tickets_api", __name__)


@tickets_bp.before_request
async def enforce_tickets_operator_access():
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


def get_tickets_cog():
    """Retrieves the TicketsCog instance from the running Discord bot."""
    bot = get_bot()
    if bot:
        return bot.get_cog("TicketsCog")
    return None


def _get_server_channels_and_roles(guild: Optional[discord.Guild]):
    """Extracts text channels, roles, and members with strict 64-bit string serialization."""
    if not guild:
        return [], [], []

    channels = [
        {"id": str(c.id), "name": c.name, "label": f"#{c.name}"}
        for c in guild.text_channels
    ]
    roles = [
        {"id": str(r.id), "name": r.name, "color": str(r.color)}
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if not r.managed and r.name != "@everyone"
    ]
    members = [
        {"id": str(m.id), "name": str(m), "display_name": m.display_name}
        for m in guild.members
        if not m.bot
    ][:1000]
    return channels, roles, members


# =============================================================================
# 1. Ticket Panels Management & Discord Panel Publishing
# =============================================================================

@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels", methods=["GET"])
async def api_get_ticket_panels(guild_id: int):
    """Retrieves all ticket panels for a guild, channels, roles, members, and counts."""
    db = TicketsDB.get_instance()
    panels = await db.get_panels(guild_id)

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    channels, roles, members = _get_server_channels_and_roles(guild)

    counts = await db.get_ticket_counts(guild_id)

    return jsonify({
        "success": True,
        "panels": panels,
        "channels": channels,
        "roles": roles,
        "members": members,
        "counts": counts
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels", methods=["POST"])
async def api_create_ticket_panel(guild_id: int):
    """Creates a new ticket panel for a guild."""
    data = await request.get_json() or {}
    name = str(data.get("name", "")).strip() or "Support Desk"
    ch_id = int(data["channel_id"]) if data.get("channel_id") else None
    tr_id = int(data["transcript_channel_id"]) if data.get("transcript_channel_id") else None
    s_roles = [str(r).strip() for r in data.get("support_role_ids", []) if str(r).strip()]
    s_members = [str(m).strip() for m in data.get("support_member_ids", []) if str(m).strip()]
    max_open = max(1, min(int(data.get("max_open_per_user", 2)), 20))
    title = str(data.get("panel_title", "")).strip() or "🛡️ Official Support & Issue Desk"
    desc = str(data.get("panel_description", "")).strip() or "Click a category button below to open a secure Private Thread."
    color = str(data.get("panel_color", "")).strip() or "#5865F2"

    db = TicketsDB.get_instance()
    panel_id = await db.create_panel(
        guild_id=guild_id,
        name=name,
        channel_id=ch_id,
        transcript_channel_id=tr_id,
        support_role_ids=s_roles,
        support_member_ids=s_members,
        max_open_per_user=max_open,
        panel_title=title,
        panel_description=desc,
        panel_color=color
    )

    created = await db.get_panel(panel_id)
    return jsonify({
        "success": True,
        "message": f"Panel '{name}' created successfully.",
        "panel": created
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels/<int:panel_id>", methods=["GET"])
async def api_get_ticket_panel(guild_id: int, panel_id: int):
    """Retrieves a single ticket panel."""
    db = TicketsDB.get_instance()
    panel = await db.get_panel(panel_id)
    if not panel or panel["guild_id"] != str(guild_id):
        return jsonify({"error": "Panel not found."}), 404
    return jsonify({"success": True, "panel": panel})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels/<int:panel_id>", methods=["PUT"])
async def api_update_ticket_panel(guild_id: int, panel_id: int):
    """Updates a ticket panel's configuration."""
    data = await request.get_json() or {}
    db = TicketsDB.get_instance()
    panel = await db.get_panel(panel_id)
    if not panel or panel["guild_id"] != str(guild_id):
        return jsonify({"error": "Panel not found."}), 404

    kwargs_to_update = {}
    for field in (
        "name", "channel_id", "transcript_channel_id", "support_role_ids",
        "support_member_ids", "max_open_per_user", "panel_title",
        "panel_description", "panel_color"
    ):
        if field in data:
            kwargs_to_update[field] = data[field]

    updated = await db.update_panel(panel_id=panel_id, **kwargs_to_update)

    return jsonify({
        "success": True,
        "message": "Panel updated successfully.",
        "panel": updated
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels/<int:panel_id>", methods=["DELETE"])
async def api_delete_ticket_panel(guild_id: int, panel_id: int):
    """Deletes a ticket panel and removes its Discord message if published."""
    db = TicketsDB.get_instance()
    panel = await db.get_panel(panel_id)
    if not panel or panel["guild_id"] != str(guild_id):
        return jsonify({"error": "Panel not found."}), 404

    cog = get_tickets_cog()
    ch_id = int(panel["channel_id"]) if panel.get("channel_id") else None
    msg_id = int(panel["message_id"]) if panel.get("message_id") else None
    if cog and ch_id and msg_id:
        try:
            await cog.delete_panel_message(guild_id, ch_id, msg_id)
        except Exception as e:
            logger.warning(f"Could not delete message for panel {panel_id}: {e}")

    ok, _, _ = await db.delete_panel(panel_id)
    if not ok:
        return jsonify({"error": "Failed to delete panel."}), 500

    return jsonify({"success": True, "message": f"Panel '{panel['name']}' deleted successfully."})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/panels/<int:panel_id>/publish", methods=["POST"])
async def api_publish_specific_panel(guild_id: int, panel_id: int):
    """Publishes or updates a specific ticket panel on Discord."""
    cog = get_tickets_cog()
    if not cog:
        return jsonify({
            "success": False,
            "error": "Tickets cog is not loaded on the bot."
        }), 503

    success, message, panel_msg_id = await cog.publish_or_update_panel(guild_id, panel_id=panel_id)
    if not success:
        return jsonify({"success": False, "error": message}), 400

    return jsonify({
        "success": True,
        "message": message,
        "panel_message_id": panel_msg_id
    })


# =============================================================================
# Legacy Settings Endpoints (Maintains Backward Compatibility)
# =============================================================================

@tickets_bp.route("/api/guild/<int:guild_id>/tickets/settings", methods=["GET"])
async def api_get_ticket_settings(guild_id: int):
    """Retrieves guild ticket configuration, dropdown channel/role lists, and stats."""
    db = TicketsDB.get_instance()
    settings = await db.get_settings(guild_id)

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    channels, roles, members = _get_server_channels_and_roles(guild)

    counts = await db.get_ticket_counts(guild_id)

    return jsonify({
        "success": True,
        "settings": settings,
        "channels": channels,
        "roles": roles,
        "members": members,
        "counts": counts
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/settings", methods=["POST"])
async def api_save_ticket_settings(guild_id: int):
    """Updates global ticket settings for a guild."""
    data = await request.get_json() or {}
    db = TicketsDB.get_instance()

    panel_ch = int(data["panel_channel_id"]) if data.get("panel_channel_id") else None
    trans_ch = int(data["transcript_channel_id"]) if data.get("transcript_channel_id") else None
    s_roles = [str(r).strip() for r in data.get("support_role_ids", []) if str(r).strip()]
    s_members = [str(m).strip() for m in data.get("support_member_ids", []) if str(m).strip()]
    max_open = max(1, min(int(data.get("max_open_per_user", 2)), 20))
    title = str(data.get("panel_title", "")).strip() or "🛡️ Official Support & Issue Desk"
    desc = str(data.get("panel_description", "")).strip() or "Click a category button below to open a secure Private Thread."
    color = str(data.get("panel_color", "")).strip() or "#5865F2"

    updated = await db.upsert_settings(
        guild_id=guild_id,
        panel_channel_id=panel_ch,
        transcript_channel_id=trans_ch,
        support_role_ids=s_roles,
        support_member_ids=s_members,
        max_open_per_user=max_open,
        panel_title=title,
        panel_description=desc,
        panel_color=color
    )

    return jsonify({
        "success": True,
        "message": "Ticket settings saved successfully.",
        "settings": updated
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/publish", methods=["POST"])
async def api_publish_ticket_panel(guild_id: int):
    """Publishes or updates the live ticket embed and buttons on Discord."""
    data = await request.get_json() or {}
    panel_id = data.get("panel_id") or request.args.get("panel_id")
    p_id = int(panel_id) if panel_id and str(panel_id).isdigit() else None

    cog = get_tickets_cog()
    if not cog:
        return jsonify({
            "success": False,
            "error": "Tickets cog is not loaded on the bot."
        }), 503

    success, message, panel_msg_id = await cog.publish_or_update_panel(guild_id, panel_id=p_id)
    if not success:
        return jsonify({"success": False, "error": message}), 400

    return jsonify({
        "success": True,
        "message": message,
        "panel_message_id": panel_msg_id
    })


# =============================================================================
# 2. Category Builder & Routing Endpoints
# =============================================================================

@tickets_bp.route("/api/guild/<int:guild_id>/tickets/categories", methods=["GET"])
async def api_list_categories(guild_id: int):
    """Lists categories for a guild, optionally filtered by panel_id."""
    panel_id = request.args.get("panel_id")
    p_id = int(panel_id) if panel_id and str(panel_id).isdigit() else None

    db = TicketsDB.get_instance()
    categories = await db.get_categories(guild_id, panel_id=p_id)
    return jsonify({"success": True, "categories": categories})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/categories", methods=["POST"])
async def api_create_category(guild_id: int):
    """Creates a new ticket category / button."""
    data = await request.get_json() or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Category name is required."}), 400

    emoji = str(data.get("emoji", "🎫")).strip() or "🎫"
    button_style = str(data.get("button_style", "primary")).strip().lower()
    description = str(data.get("description", "")).strip()
    assigned_roles = [str(r).strip() for r in data.get("assigned_role_ids", []) if str(r).strip()]
    assigned_members = [str(m).strip() for m in data.get("assigned_member_ids", []) if str(m).strip()]
    panel_id = int(data["panel_id"]) if data.get("panel_id") and str(data["panel_id"]).isdigit() else None

    db = TicketsDB.get_instance()
    cat_id = await db.create_category(
        guild_id=guild_id,
        name=name,
        emoji=emoji,
        button_style=button_style,
        description=description,
        assigned_role_ids=assigned_roles,
        assigned_member_ids=assigned_members,
        panel_id=panel_id
    )

    created = await db.get_category(cat_id)
    return jsonify({
        "success": True,
        "message": "Category button created successfully.",
        "category": created
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/categories/<int:cat_id>", methods=["PUT"])
async def api_update_category(guild_id: int, cat_id: int):
    """Updates an existing ticket category."""
    data = await request.get_json() or {}
    name = str(data.get("name", "")).strip() if "name" in data else None
    emoji = str(data.get("emoji", "")).strip() if "emoji" in data else None
    button_style = str(data.get("button_style", "")).strip().lower() if "button_style" in data else None
    description = str(data.get("description", "")).strip() if "description" in data else None
    assigned_roles = [str(r).strip() for r in data.get("assigned_role_ids", []) if str(r).strip()] if "assigned_role_ids" in data else None
    assigned_members = [str(m).strip() for m in data.get("assigned_member_ids", []) if str(m).strip()] if "assigned_member_ids" in data else None
    panel_id = int(data["panel_id"]) if "panel_id" in data and str(data["panel_id"]).isdigit() else None

    db = TicketsDB.get_instance()
    ok = await db.update_category(
        cat_id=cat_id,
        name=name,
        emoji=emoji,
        button_style=button_style,
        description=description,
        assigned_role_ids=assigned_roles,
        assigned_member_ids=assigned_members,
        panel_id=panel_id
    )
    if not ok:
        return jsonify({"error": "Category not found."}), 404

    updated = await db.get_category(cat_id)
    return jsonify({
        "success": True,
        "message": "Category updated successfully.",
        "category": updated
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/categories/<int:cat_id>", methods=["DELETE"])
async def api_delete_category(guild_id: int, cat_id: int):
    """Deletes a ticket category button."""
    db = TicketsDB.get_instance()
    ok = await db.delete_category(cat_id)
    if not ok:
        return jsonify({"error": "Category not found or already deleted."}), 404
    return jsonify({"success": True, "message": "Category button deleted."})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/categories/reorder", methods=["POST"])
async def api_reorder_categories(guild_id: int):
    """Reorders categories based on provided ID list."""
    data = await request.get_json() or {}
    category_ids = data.get("category_ids", [])
    if not isinstance(category_ids, list):
        return jsonify({"error": "category_ids must be a list of IDs."}), 400

    db = TicketsDB.get_instance()
    await db.reorder_categories(guild_id, [int(cid) for cid in category_ids if str(cid).isdigit()])
    categories = await db.get_categories(guild_id)
    return jsonify({"success": True, "categories": categories})


# =============================================================================
# 3. Tickets Management & Actions
# =============================================================================

@tickets_bp.route("/api/guild/<int:guild_id>/tickets", methods=["GET"])
async def api_list_tickets(guild_id: int):
    """Lists tickets for a guild with status, search, panel, and category filters."""
    status = request.args.get("status", "all")
    search = request.args.get("search", "")
    category_id = int(request.args.get("category_id")) if request.args.get("category_id") else None
    panel_id = int(request.args.get("panel_id")) if request.args.get("panel_id") and str(request.args.get("panel_id")).isdigit() else None

    db = TicketsDB.get_instance()
    tickets = await db.list_tickets(
        guild_id=guild_id,
        status=status,
        search=search,
        category_id=category_id,
        panel_id=panel_id,
        limit=200
    )
    counts = await db.get_ticket_counts(guild_id, panel_id=panel_id)

    return jsonify({
        "success": True,
        "tickets": tickets,
        "counts": counts
    })




@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>", methods=["GET"])
async def api_get_ticket(guild_id: int, ticket_id: int):
    """Retrieves details of a single ticket."""
    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != str(guild_id):
        return jsonify({"error": "Ticket not found."}), 404
    return jsonify({"success": True, "ticket": ticket})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>/claim", methods=["POST"])
async def api_claim_ticket(guild_id: int, ticket_id: int):
    """Allows staff/operator to claim a ticket from the web dashboard."""
    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != str(guild_id):
        return jsonify({"error": "Ticket not found."}), 404

    staff_name = session.get("user_id") or "Dashboard Admin"
    staff_id = 0

    ok = await db.claim_ticket(ticket_id, staff_id=staff_id, staff_name=staff_name)
    if not ok:
        return jsonify({"error": "Unable to claim closed ticket."}), 400

    # Post notice to Discord thread if active
    bot = get_bot()
    if bot:
        thread = bot.get_channel(int(ticket["thread_id"]))
        if thread and isinstance(thread, (discord.Thread, discord.TextChannel)):
            try:
                await thread.send(f"🙋‍♂️ **{staff_name}** claimed this ticket via Web Dashboard.")
            except Exception:
                pass

    updated = await db.get_ticket(ticket_id)
    return jsonify({"success": True, "message": "Ticket claimed.", "ticket": updated})


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>/assign", methods=["POST"])
async def api_assign_to_ticket(guild_id: int, ticket_id: int):
    """Assigns a Discord Role or Member to the ticket thread from the web dashboard."""
    data = await request.get_json() or {}
    role_id = str(data.get("role_id", "")).strip() if data.get("role_id") else None
    member_id = str(data.get("member_id", "")).strip() if data.get("member_id") else None

    if not role_id and not member_id:
        return jsonify({"error": "Must specify either role_id or member_id."}), 400

    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != str(guild_id):
        return jsonify({"error": "Ticket not found."}), 404

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    thread = bot.get_channel(int(ticket["thread_id"])) if bot else None

    operator_name = session.get("user_id") or "Web Dashboard Admin"

    if role_id:
        await db.assign_role_to_ticket(ticket_id, role_id)
        if thread and isinstance(thread, (discord.Thread, discord.TextChannel)):
            try:
                role = guild.get_role(int(role_id)) if guild else None
                role_label = role.mention if role else f"<@&{role_id}>"
                await thread.send(f"👥 {role_label} has been assigned to this ticket by {operator_name} via Dashboard.")
            except Exception as e:
                logger.warning(f"Failed to post role assignment to thread: {e}")

    if member_id:
        await db.assign_user_to_ticket(ticket_id, member_id)
        if thread and isinstance(thread, discord.Thread):
            try:
                member = guild.get_member(int(member_id)) if guild else None
                if member:
                    await thread.add_user(member)
                await thread.send(f"👤 <@{member_id}> has been added to this private thread by {operator_name} via Dashboard.")
            except Exception as e:
                logger.warning(f"Failed to add member to thread: {e}")

    updated = await db.get_ticket(ticket_id)
    return jsonify({
        "success": True,
        "message": "Role/Member assigned successfully.",
        "ticket": updated
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>/close", methods=["POST"])
async def api_close_ticket(guild_id: int, ticket_id: int):
    """Closes ticket remotely from dashboard, archiving full transcript and locking thread."""
    data = await request.get_json() or {}
    reason = str(data.get("reason", "")).strip() or "Closed via Web Dashboard"

    cog = get_tickets_cog()
    if not cog:
        return jsonify({"error": "Tickets cog unavailable on bot."}), 503

    operator = session.get("user_id") or "Web Dashboard Admin"

    ok = await cog.execute_close_ticket(
        ticket_id=ticket_id,
        closed_by=operator,
        reason=reason
    )
    if not ok:
        return jsonify({"error": "Ticket could not be closed."}), 400

    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    return jsonify({
        "success": True,
        "message": f"Ticket #{ticket_id:04d} closed and archived.",
        "ticket": ticket
    })


# =============================================================================
# 4. Transcripts Viewing & As-Is Downloads
# =============================================================================

@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>/transcript", methods=["GET"])
async def api_view_transcript(guild_id: int, ticket_id: int):
    """Returns stored transcript messages or harvests live messages if ticket is open."""
    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != str(guild_id):
        return jsonify({"error": "Ticket not found."}), 404

    messages = await db.get_ticket_messages(ticket_id)

    # If ticket is open or stored messages are empty, try harvesting live messages from active thread
    if not messages:
        bot = get_bot()
        thread = bot.get_channel(int(ticket["thread_id"])) if bot else None
        if thread and isinstance(thread, discord.Thread):
            try:
                live_msgs = []
                async for m in thread.history(limit=500, oldest_first=True):
                    att_urls = [a.url for a in m.attachments]
                    live_msgs.append({
                        "id": 0,
                        "ticket_id": ticket_id,
                        "message_id": str(m.id),
                        "author_id": str(m.author.id),
                        "author_name": str(m.author),
                        "author_avatar": m.author.display_avatar.url if m.author.display_avatar else None,
                        "is_staff": bool(m.author.guild_permissions.administrator or m.author.id == bot.user.id) if isinstance(m.author, discord.Member) else False,
                        "content": m.content,
                        "attachments": att_urls,
                        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    })
                messages = live_msgs
            except Exception as e:
                logger.warning(f"Could not read live thread history for ticket {ticket_id}: {e}")

    return jsonify({
        "success": True,
        "ticket": ticket,
        "messages": messages
    })


@tickets_bp.route("/api/guild/<int:guild_id>/tickets/<int:ticket_id>/transcript/download", methods=["GET"])
async def api_download_transcript(guild_id: int, ticket_id: int):
    """Direct browser download of the complete formatted transcript (.html or .txt)."""
    fmt = request.args.get("format", "html").lower()
    db = TicketsDB.get_instance()
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != str(guild_id):
        return jsonify({"error": "Ticket not found."}), 404

    messages = await db.get_ticket_messages(ticket_id)
    if not messages:
        # Harvest live if open
        bot = get_bot()
        thread = bot.get_channel(int(ticket["thread_id"])) if bot else None
        if thread and isinstance(thread, discord.Thread):
            try:
                live_msgs = []
                async for m in thread.history(limit=500, oldest_first=True):
                    att_urls = [a.url for a in m.attachments]
                    live_msgs.append({
                        "message_id": str(m.id),
                        "author_id": str(m.author.id),
                        "author_name": str(m.author),
                        "author_avatar": m.author.display_avatar.url if m.author.display_avatar else None,
                        "is_staff": bool(m.author.guild_permissions.administrator or m.author.id == bot.user.id) if isinstance(m.author, discord.Member) else False,
                        "content": m.content,
                        "attachments": att_urls,
                        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    })
                messages = live_msgs
            except Exception:
                pass

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    guild_name = guild.name if guild else "Discord Server"

    clean_user = "".join(c for c in ticket.get("user_name", "user") if c.isalnum() or c in ("-", "_"))[:14]
    filename_base = f"ticket-{ticket_id:04d}-{clean_user}-transcript"

    if fmt == "txt":
        content = await asyncio.to_thread(generate_txt_transcript, ticket, messages)
        file_bytes = io.BytesIO(content.encode("utf-8"))
        return await send_file(
            file_bytes,
            mimetype="text/plain; charset=utf-8",
            as_attachment=True,
            attachment_filename=f"{filename_base}.txt"
        )
    else:
        content = await asyncio.to_thread(generate_html_transcript, ticket, messages, guild_name)
        file_bytes = io.BytesIO(content.encode("utf-8"))
        return await send_file(
            file_bytes,
            mimetype="text/html; charset=utf-8",
            as_attachment=True,
            attachment_filename=f"{filename_base}.html"
        )
