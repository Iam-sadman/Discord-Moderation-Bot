"""
Dashboard Moderation and Role Management API Blueprint.
Provides RESTful endpoints for individual moderation, role panels,
bulk actions, DM templates, and audit logs.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List

import discord
from quart import Blueprint, request, jsonify, session

from services.moderation_db import ModerationDB
from services.moderation_engine import ModerationEngine
from cogs.moderation import PersistentRolePanelView

logger = logging.getLogger("ModerationAPI")

moderation_bp = Blueprint("moderation_api", __name__)


def get_bot():
    """Retrieves the bot instance attached to the Quart app."""
    from quart import current_app
    return getattr(current_app, "discord_bot", None)


async def check_operator_permission(guild_id: int, capability_key: str) -> tuple[bool, str]:
    """Validates if the currently authenticated dashboard user has the required capability."""
    user_id = session.get("user_id")
    user_role = session.get("role", "Viewer")
    if not user_id:
        return False, "Unauthorized: Valid session required"

    # Master Administrator accounts always bypass capability restrictions
    if user_role.lower() in ("administrator", "master admin"):
        return True, ""

    db = ModerationDB.get_instance()
    perms = await db.get_dashboard_permissions(guild_id)
    role_perms = perms.get(user_role, {})
    if not role_perms.get(capability_key, False):
        return False, f"Permission Denied: Your dashboard role '{user_role}' lacks '{capability_key}' permission."

    return True, ""


# =============================================================================
# 1. Role Catalog & Member Role Management
# =============================================================================

@moderation_bp.route("/api/guild/<int:guild_id>/roles/catalog", methods=["GET"])
async def api_roles_catalog(guild_id):
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    bot_top_role = guild.me.top_role if guild.me else None
    bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
    roles_data = []

    for r in sorted(guild.roles, key=lambda x: getattr(x, "position", 0), reverse=True):
        is_def = r.is_default() if callable(getattr(r, "is_default", None)) else getattr(r, "is_default", False)
        r_pos = getattr(r, "position", 0)
        can_bot = bool(bot_top_role and r_pos < bot_pos and not getattr(r, "managed", False) and not is_def)
        roles_data.append({
            "id": r.id,
            "name": r.name,
            "color": str(r.color),
            "position": r_pos,
            "member_count": len(r.members) if hasattr(r, "members") and r.members is not None else 0,
            "is_default": is_def,
            "is_managed": getattr(r, "managed", False),
            "can_bot_manage": can_bot,
            "permissions": {
                "administrator": getattr(r.permissions, "administrator", False),
                "manage_guild": getattr(r.permissions, "manage_guild", False),
                "ban_members": getattr(r.permissions, "ban_members", False),
                "kick_members": getattr(r.permissions, "kick_members", False),
                "moderate_members": getattr(r.permissions, "moderate_members", False),
                "manage_roles": getattr(r.permissions, "manage_roles", False)
            }
        })

    return jsonify({"success": True, "roles": roles_data})


@moderation_bp.route("/api/guild/<int:guild_id>/roles/members", methods=["GET"])
async def api_roles_members(guild_id):
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    search = (request.args.get("search") or "").strip().lower()
    role_id_raw = request.args.get("role_id")
    limit = min(max(1, int(request.args.get("limit", 50))), 100)
    offset = max(0, int(request.args.get("offset", 0)))

    target_role_id = int(role_id_raw) if role_id_raw and role_id_raw.isdigit() else None

    members = []
    for m in guild.members:
        if m.bot:
            continue
        if target_role_id and not any(r.id == target_role_id for r in m.roles):
            continue
        if search:
            m_name = m.name.lower()
            m_disp = m.display_name.lower()
            m_id = str(m.id)
            if search not in m_name and search not in m_disp and search != m_id:
                continue

        is_timed_out = m.timed_out_until is not None and m.timed_out_until > discord.utils.utcnow()
        members.append({
            "id": m.id,
            "name": str(m),
            "display_name": m.display_name,
            "avatar": str(m.display_avatar.url),
            "joined_at": m.joined_at.strftime("%Y-%m-%d %H:%M") if m.joined_at else "Unknown",
            "created_at": m.created_at.strftime("%Y-%m-%d %H:%M"),
            "top_role": m.top_role.name,
            "roles": [{"id": r.id, "name": r.name, "color": str(r.color)} for r in m.roles if not r.is_default()],
            "is_timed_out": is_timed_out,
            "timed_out_until": m.timed_out_until.isoformat() if is_timed_out else None,
            "is_protected": bool(m.id == guild.owner_id or m.guild_permissions.administrator)
        })

    total = len(members)
    paginated = members[offset:offset+limit]

    return jsonify({"success": True, "total": total, "limit": limit, "offset": offset, "members": paginated})


@moderation_bp.route("/api/guild/<int:guild_id>/roles/assign", methods=["POST"])
async def api_roles_assign(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_manage_roles")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    role_id = data.get("role_id")
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    idempotency_key = data.get("idempotency_key")

    if not user_id or not role_id:
        return jsonify({"error": "user_id and role_id are required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_role_assign(
        guild_id=guild_id, target_id=int(user_id), role_id=int(role_id),
        actor_id=actor_id, actor_name=actor_name, reason=reason,
        notify_dm=notify_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/roles/remove", methods=["POST"])
async def api_roles_remove(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_manage_roles")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    role_id = data.get("role_id")
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    idempotency_key = data.get("idempotency_key")

    if not user_id or not role_id:
        return jsonify({"error": "user_id and role_id are required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_role_remove(
        guild_id=guild_id, target_id=int(user_id), role_id=int(role_id),
        actor_id=actor_id, actor_name=actor_name, reason=reason,
        notify_dm=notify_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/roles/allowed_self", methods=["GET"])
async def api_get_allowed_self_roles(guild_id):
    db = ModerationDB.get_instance()
    allowed_ids = await db.get_allowed_self_roles(guild_id)
    return jsonify({"success": True, "allowed_roles": allowed_ids})


@moderation_bp.route("/api/guild/<int:guild_id>/roles/allowed_self", methods=["POST"])
async def api_set_allowed_self_roles(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_create_panels")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    role_id = data.get("role_id")
    action = data.get("action", "add")  # "add" or "remove"

    if not role_id:
        return jsonify({"error": "role_id is required"}), 400

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if guild:
        role = guild.get_role(int(role_id))
        if role and action == "add":
            # Guard against privilege escalation
            if role.permissions.administrator or role.permissions.manage_guild or role.permissions.ban_members or role.permissions.kick_members:
                return jsonify({"error": "Security violation: Privileged roles with Administrator/Moderator permissions cannot be added to self-service allowlists."}), 400

    db = ModerationDB.get_instance()
    actor_id = session.get("user_id", "admin")

    if action == "add":
        await db.add_allowed_self_role(guild_id, int(role_id), added_by=actor_id)
    else:
        await db.remove_allowed_self_role(guild_id, int(role_id))

    allowed_ids = await db.get_allowed_self_roles(guild_id)
    return jsonify({"success": True, "allowed_roles": allowed_ids})


# =============================================================================
# 2. Self-Service Role Panel Builder
# =============================================================================

@moderation_bp.route("/api/guild/<int:guild_id>/role_panels", methods=["GET"])
async def api_list_role_panels(guild_id):
    db = ModerationDB.get_instance()
    panels = await db.list_role_panels(guild_id)
    return jsonify({"success": True, "panels": panels})


@moderation_bp.route("/api/guild/<int:guild_id>/role_panels/create", methods=["POST"])
async def api_create_role_panel(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_create_panels")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    channel_id = data.get("channel_id")
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    color = (data.get("color") or "#5865F2").strip()
    image_url = (data.get("image_url") or "").strip()
    thumbnail_url = (data.get("thumbnail_url") or "").strip()
    items = data.get("items", [])

    if not channel_id or not title:
        return jsonify({"error": "channel_id and title are required"}), 400

    db = ModerationDB.get_instance()
    actor_id = session.get("user_id", "admin")

    panel_id = await db.create_role_panel(
        guild_id=guild_id, channel_id=int(channel_id), title=title,
        description=description, color=color, created_by=actor_id,
        image_url=image_url, thumbnail_url=thumbnail_url
    )
    if items:
        await db.set_panel_items(panel_id, items)

    panel = await db.get_role_panel(panel_id)
    return jsonify({"success": True, "panel": panel})


@moderation_bp.route("/api/guild/<int:guild_id>/role_panels/<int:panel_id>/edit", methods=["POST"])
async def api_edit_role_panel(guild_id, panel_id):
    allowed, err = await check_operator_permission(guild_id, "can_create_panels")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    color = (data.get("color") or "#5865F2").strip()
    image_url = (data.get("image_url") or "").strip()
    thumbnail_url = (data.get("thumbnail_url") or "").strip()
    is_enabled = bool(data.get("is_enabled", True))
    items = data.get("items")

    db = ModerationDB.get_instance()
    await db.update_role_panel(
        panel_id=panel_id, title=title, description=description,
        color=color, image_url=image_url, thumbnail_url=thumbnail_url,
        is_enabled=is_enabled
    )
    if items is not None:
        await db.set_panel_items(panel_id, items)

    # If already published on Discord, update the live message embed & buttons
    panel = await db.get_role_panel(panel_id)
    if panel and panel.get("message_id"):
        bot = get_bot()
        guild = bot.get_guild(guild_id) if bot else None
        if guild:
            ch = guild.get_channel(panel["channel_id"])
            if ch:
                try:
                    msg = await ch.fetch_message(panel["message_id"])
                    color_int = int(panel["color"].lstrip("#"), 16) if panel["color"].startswith("#") else 0x5865F2
                    embed = discord.Embed(title=panel["title"], description=panel["description"] or None, color=color_int)
                    if panel["thumbnail_url"]: embed.set_thumbnail(url=panel["thumbnail_url"])
                    if panel["image_url"]: embed.set_image(url=panel["image_url"])
                    view = PersistentRolePanelView(panel)
                    await msg.edit(embed=embed, view=view)
                except Exception as e:
                    logger.warning("Failed to refresh Discord message for panel %d: %s", panel_id, e)

    return jsonify({"success": True, "panel": panel})


@moderation_bp.route("/api/guild/<int:guild_id>/role_panels/<int:panel_id>/publish", methods=["POST"])
async def api_publish_role_panel(guild_id, panel_id):
    allowed, err = await check_operator_permission(guild_id, "can_create_panels")
    if not allowed:
        return jsonify({"error": err}), 403

    db = ModerationDB.get_instance()
    panel = await db.get_role_panel(panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    channel = guild.get_channel(panel["channel_id"])
    if not channel:
        return jsonify({"error": "Destination channel not found in server"}), 404

    color_int = 0x5865F2
    try:
        color_int = int(panel["color"].lstrip("#"), 16)
    except Exception:
        pass

    embed = discord.Embed(title=panel["title"], description=panel["description"] or None, color=color_int)
    if panel.get("thumbnail_url"): embed.set_thumbnail(url=panel["thumbnail_url"])
    if panel.get("image_url"): embed.set_image(url=panel["image_url"])

    view = PersistentRolePanelView(panel)

    try:
        sent_msg = await channel.send(embed=embed, view=view)
        await db.set_panel_message_id(panel_id, sent_msg.id)
        # Register in bot persistent views
        bot.add_view(view, message_id=sent_msg.id)

        # Add reactions if reaction items configured
        for item in panel.get("items", []):
            if item.get("item_type") == "reaction" and item.get("emoji"):
                try:
                    await sent_msg.add_reaction(item["emoji"])
                except Exception:
                    pass

        return jsonify({"success": True, "message_id": str(sent_msg.id), "jump_url": sent_msg.jump_url})
    except discord.Forbidden:
        return jsonify({"error": "Bot lacks permission to send messages or embed links in the target channel"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@moderation_bp.route("/api/guild/<int:guild_id>/role_panels/<int:panel_id>", methods=["DELETE"])
async def api_delete_role_panel(guild_id, panel_id):
    allowed, err = await check_operator_permission(guild_id, "can_create_panels")
    if not allowed:
        return jsonify({"error": err}), 403

    db = ModerationDB.get_instance()
    deleted = await db.delete_role_panel(panel_id)
    if not deleted:
        return jsonify({"error": "Panel not found"}), 404

    return jsonify({"success": True, "deleted": deleted})


# =============================================================================
# 3. Individual Moderation Actions
# =============================================================================

@moderation_bp.route("/api/guild/<int:guild_id>/moderation/timeout", methods=["POST"])
async def api_mod_timeout(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_timeout")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    duration = int(data.get("duration_seconds", 300))
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    custom_dm = (data.get("custom_dm") or "").strip()
    idempotency_key = data.get("idempotency_key")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_timeout(
        guild_id=guild_id, target_id=int(user_id), duration_seconds=duration,
        actor_id=actor_id, actor_name=actor_name, reason=reason,
        notify_dm=notify_dm, custom_dm=custom_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/kick", methods=["POST"])
async def api_mod_kick(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_kick")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    custom_dm = (data.get("custom_dm") or "").strip()
    idempotency_key = data.get("idempotency_key")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_kick(
        guild_id=guild_id, target_id=int(user_id), actor_id=actor_id,
        actor_name=actor_name, reason=reason, notify_dm=notify_dm,
        custom_dm=custom_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/ban", methods=["POST"])
async def api_mod_ban(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_ban")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    delete_days = int(data.get("delete_message_days", 0))
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    custom_dm = (data.get("custom_dm") or "").strip()
    idempotency_key = data.get("idempotency_key")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_ban(
        guild_id=guild_id, target_id=int(user_id), actor_id=actor_id,
        actor_name=actor_name, reason=reason, delete_message_days=delete_days,
        notify_dm=notify_dm, custom_dm=custom_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/tempban", methods=["POST"])
async def api_mod_tempban(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_tempban")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    duration = int(data.get("duration_seconds", 3600))
    delete_days = int(data.get("delete_message_days", 0))
    reason = (data.get("reason") or "").strip()
    notify_dm = bool(data.get("notify_dm", False))
    custom_dm = (data.get("custom_dm") or "").strip()
    idempotency_key = data.get("idempotency_key")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_tempban(
        guild_id=guild_id, target_id=int(user_id), duration_seconds=duration,
        actor_id=actor_id, actor_name=actor_name, reason=reason,
        delete_message_days=delete_days, notify_dm=notify_dm,
        custom_dm=custom_dm, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/bans", methods=["GET"])
async def api_mod_get_bans(guild_id):
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    search = (request.args.get("search") or "").strip().lower()
    bans = []
    try:
        async for ban_entry in guild.bans(limit=1000):
            user = ban_entry.user
            if search:
                if search not in user.name.lower() and search != str(user.id):
                    continue
            bans.append({
                "user_id": user.id,
                "user_name": str(user),
                "avatar": str(user.display_avatar.url),
                "reason": ban_entry.reason or "No reason specified"
            })
        return jsonify({"success": True, "bans": bans[:100], "total": len(bans)})
    except discord.Forbidden:
        return jsonify({"error": "Bot lacks 'Ban Members' permission to view ban list"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/unban", methods=["POST"])
async def api_mod_unban(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_unban")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    user_id = data.get("user_id")
    reason = (data.get("reason") or "").strip()
    idempotency_key = data.get("idempotency_key")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.execute_unban(
        guild_id=guild_id, target_id=int(user_id), actor_id=actor_id,
        actor_name=actor_name, reason=reason, idempotency_key=idempotency_key
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


# =============================================================================
# 4. Bulk Moderation Endpoints
# =============================================================================

@moderation_bp.route("/api/guild/<int:guild_id>/bulk/preview", methods=["POST"])
async def api_bulk_preview(guild_id):
    data = await request.get_json() or {}
    filters = data.get("filters", {})

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    res = await engine.preview_bulk_targets(guild_id, filters)
    return jsonify(res)


@moderation_bp.route("/api/guild/<int:guild_id>/bulk/execute", methods=["POST"])
async def api_bulk_execute(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_bulk_mod")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    action_type = data.get("action_type")
    filters = data.get("filters", {})
    reason = (data.get("reason") or "").strip()
    role_id = data.get("role_id")
    duration = data.get("duration_seconds")
    notify_dm = bool(data.get("notify_dm", False))
    confirmation_phrase = (data.get("confirmation_phrase") or "").strip()

    if not action_type or action_type not in ("role_add", "role_remove", "timeout", "kick", "ban"):
        return jsonify({"error": f"Invalid action_type: {action_type}"}), 400

    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    actor_id = session.get("user_id", "admin")
    actor_name = session.get("name", actor_id)

    res = await engine.start_bulk_job(
        guild_id=guild_id, action_type=action_type, actor_id=actor_id,
        actor_name=actor_name, filters=filters, reason=reason,
        role_id=int(role_id) if role_id else None,
        duration_seconds=int(duration) if duration else None,
        notify_dm=notify_dm, confirmation_phrase=confirmation_phrase
    )
    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@moderation_bp.route("/api/guild/<int:guild_id>/bulk/jobs/<job_id>", methods=["GET"])
async def api_bulk_job_status(guild_id, job_id):
    db = ModerationDB.get_instance()
    job = await db.get_bulk_job(job_id)
    if not job or job["guild_id"] != guild_id:
        return jsonify({"error": "Bulk job not found"}), 404

    targets = await db.get_bulk_job_targets(job_id, limit=50)
    return jsonify({"success": True, "job": job, "targets_sample": targets})


@moderation_bp.route("/api/guild/<int:guild_id>/bulk/jobs/<job_id>/cancel", methods=["POST"])
async def api_bulk_job_cancel(guild_id, job_id):
    bot = get_bot()
    engine = ModerationEngine.get_instance(bot)
    success = await engine.cancel_bulk_job(job_id)
    return jsonify({"success": success})


@moderation_bp.route("/api/guild/<int:guild_id>/bulk/jobs", methods=["GET"])
async def api_bulk_jobs_list(guild_id):
    db = ModerationDB.get_instance()
    jobs = await db.list_bulk_jobs(guild_id, limit=15)
    return jsonify({"success": True, "jobs": jobs})


# =============================================================================
# 5. Templates, Audit Logs & Emergency Controls
# =============================================================================

@moderation_bp.route("/api/guild/<int:guild_id>/moderation/templates", methods=["GET"])
async def api_get_templates(guild_id):
    db = ModerationDB.get_instance()
    templates = await db.get_all_message_templates(guild_id)
    return jsonify({"success": True, "templates": templates})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/templates", methods=["POST"])
async def api_set_template(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_edit_templates")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    action_type = data.get("action_type")
    template_text = (data.get("template_text") or "").strip()
    is_enabled = bool(data.get("is_enabled", True))

    if not action_type or not template_text:
        return jsonify({"error": "action_type and template_text are required"}), 400

    db = ModerationDB.get_instance()
    actor_id = session.get("user_id", "admin")
    await db.set_message_template(
        guild_id=guild_id, action_type=action_type,
        template_text=template_text, is_enabled=is_enabled,
        updated_by=actor_id
    )
    return jsonify({"success": True, "action_type": action_type})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/audit_logs", methods=["GET"])
async def api_mod_audit_logs(guild_id):
    action_type = request.args.get("action_type")
    search = request.args.get("search")
    limit = int(request.args.get("limit", 25))
    offset = int(request.args.get("offset", 0))

    db = ModerationDB.get_instance()
    data = await db.get_moderation_audit_logs(
        guild_id=guild_id, action_type=action_type, search=search,
        limit=limit, offset=offset
    )
    return jsonify({"success": True, **data})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/permissions", methods=["GET"])
async def api_get_mod_permissions(guild_id):
    db = ModerationDB.get_instance()
    perms = await db.get_dashboard_permissions(guild_id)
    return jsonify({"success": True, "permissions": perms})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/permissions", methods=["POST"])
async def api_set_mod_permissions(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_emergency_stop")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    role = data.get("role")
    capabilities = data.get("capabilities", {})

    if not role or role not in ("Administrator", "Moderator", "Viewer"):
        return jsonify({"error": "Valid role (Administrator, Moderator, Viewer) is required"}), 400

    db = ModerationDB.get_instance()
    await db.set_dashboard_permission_for_role(guild_id, role, capabilities)
    perms = await db.get_dashboard_permissions(guild_id)
    return jsonify({"success": True, "permissions": perms})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/emergency_status", methods=["GET"])
async def api_get_emergency_status(guild_id):
    db = ModerationDB.get_instance()
    is_locked = await db.is_emergency_locked(guild_id)
    return jsonify({"success": True, "is_locked": is_locked})


@moderation_bp.route("/api/guild/<int:guild_id>/moderation/emergency_stop", methods=["POST"])
async def api_toggle_emergency_stop(guild_id):
    allowed, err = await check_operator_permission(guild_id, "can_emergency_stop")
    if not allowed:
        return jsonify({"error": err}), 403

    data = await request.get_json() or {}
    is_locked = bool(data.get("is_locked", False))
    reason = (data.get("reason") or "Operator manual trigger").strip()

    db = ModerationDB.get_instance()
    actor_id = session.get("user_id", "admin")
    await db.set_emergency_lock(guild_id, is_locked, triggered_by=actor_id, reason=reason)

    await db.record_audit_log(
        guild_id=guild_id, actor_id=actor_id, actor_name=session.get("name", actor_id),
        action_type="EMERGENCY_LOCK_TOGGLE", outcome="SUCCESS",
        details=f"Emergency circuit breaker set to {'LOCKED' if is_locked else 'UNLOCKED'} ({reason})"
    )

    return jsonify({"success": True, "is_locked": is_locked})
