"""
Dashboard Forms Builder & Management API Blueprint.
Provides RESTful endpoints for forms CRUD, dynamic question management,
reordering, Discord panel publishing, submission review actions, and spreadsheet exports.
"""

import asyncio
import os
import io
import time
import logging
from typing import Optional, Dict, Any, List

import discord
from quart import Blueprint, request, jsonify, session, send_file, current_app

from config import Config
from services.forms_db import FormsDB
from services.forms_exporter import generate_csv_bytes, generate_xlsx_bytes, OPENPYXL_AVAILABLE

logger = logging.getLogger("FormsAPI")

forms_bp = Blueprint("forms_api", __name__)


@forms_bp.before_request
async def enforce_forms_operator_access():
    """Restricts create, edit, delete, publish, and review actions to Administrator and Moderator roles."""
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
    """Retrieves the Discord bot instance attached to the Quart app."""
    return getattr(current_app, "discord_bot", None)


def get_forms_cog():
    """Retrieves the FormsCog instance from the running Discord bot."""
    bot = get_bot()
    if bot:
        return bot.get_cog("FormsCog")
    return None


def verify_internal_secret(req) -> bool:
    """Validates X-Internal-Secret header against Config.INTERNAL_SECRET."""
    secret = req.headers.get("X-Internal-Secret", "")
    expected = getattr(Config, "INTERNAL_SECRET", "") or getattr(Config, "WEB_SECRET", "")
    return bool(secret and expected and secret == expected)



# =============================================================================
# 1. Forms CRUD Endpoints
# =============================================================================

@forms_bp.route("/api/guild/<int:guild_id>/forms", methods=["GET"])
async def api_list_forms(guild_id: int):
    """Lists all forms for a guild with submission counts and question counts."""
    db = FormsDB.get_instance()
    forms = await db.list_forms(guild_id)
    return jsonify({"success": True, "forms": forms})


@forms_bp.route("/api/guild/<int:guild_id>/forms", methods=["POST"])
async def api_create_form(guild_id: int):
    """Creates a new form."""
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found on bot instance"}), 404

    data = await request.get_json()
    if not data or not data.get("title") or not data.get("dest_channel_id"):
        return jsonify({"error": "Missing required fields: title and dest_channel_id"}), 400

    title = str(data["title"]).strip()
    dest_ch_id = int(data["dest_channel_id"])
    description = str(data.get("description", "")).strip() or None
    review_enabled = bool(data.get("review_enabled", False))
    approved_role_id = int(data["approved_role_id"]) if data.get("approved_role_id") else None
    panel_ch_id = int(data["panel_channel_id"]) if data.get("panel_channel_id") else None
    max_subs = max(0, int(data.get("max_submissions_per_user", 0)))
    cooldown = max(0, int(data.get("cooldown_seconds", 60)))
    opens_at = int(data["opens_at"]) if data.get("opens_at") else None
    closes_at = int(data["closes_at"]) if data.get("closes_at") else None

    # Validate destination channel belongs to target guild (Anti-IDOR)
    dest_channel = guild.get_channel(dest_ch_id)
    if not dest_channel:
        return jsonify({"error": "Destination channel must belong to this Discord server"}), 400

    # Role Hierarchy & Privilege Escalation Guard
    if approved_role_id:
        role = guild.get_role(approved_role_id)
        if not role:
            return jsonify({"error": "Specified approved role does not exist on this server"}), 400
        bot_top = guild.me.top_role if guild.me else None
        if bot_top and role.position >= bot_top.position:
            return jsonify({"error": "Security check: Approved role must be lower than the bot's highest role"}), 400
        if role.permissions.administrator or role.permissions.manage_guild:
            return jsonify({"error": "Security check: Approved role cannot possess Administrator or Manage Server permissions"}), 400

    creator = session.get("user_id") or "Web Dashboard Admin"

    db = FormsDB.get_instance()
    form_id = await db.create_form(
        guild_id=guild_id,
        title=title,
        dest_channel_id=dest_ch_id,
        description=description,
        panel_channel_id=panel_ch_id,
        review_enabled=review_enabled,
        approved_role_id=approved_role_id,
        opens_at=opens_at,
        closes_at=closes_at,
        max_submissions_per_user=max_subs,
        cooldown_seconds=cooldown,
        created_by=creator
    )

    return jsonify({"success": True, "form_id": form_id, "message": "Form created successfully."})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>", methods=["GET"])
async def api_get_form(guild_id: int, form_id: int):
    """Retrieves form details including its questions."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    questions = await db.get_questions(form_id)
    stats = await db.get_submission_stats(guild_id, form_id)
    return jsonify({
        "success": True,
        "form": form,
        "questions": questions,
        "stats": stats
    })


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>", methods=["PUT"])
async def api_update_form(guild_id: int, form_id: int):
    """Updates form properties and refreshes public panel if published."""
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found on bot instance"}), 404

    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    data = await request.get_json()
    if not data:
        return jsonify({"error": "No update payload provided"}), 400

    update_fields: Dict[str, Any] = {}
    if "title" in data:
        update_fields["title"] = str(data["title"]).strip()
    if "description" in data:
        update_fields["description"] = str(data["description"]).strip() or None
    if "dest_channel_id" in data and data["dest_channel_id"]:
        dest_id = int(data["dest_channel_id"])
        ch = guild.get_channel(dest_id)
        if not ch:
            return jsonify({"error": "Destination channel must belong to this server"}), 400
        update_fields["dest_channel_id"] = dest_id
    if "panel_channel_id" in data:
        update_fields["panel_channel_id"] = int(data["panel_channel_id"]) if data["panel_channel_id"] else None
    if "review_enabled" in data:
        update_fields["review_enabled"] = bool(data["review_enabled"])
    if "approved_role_id" in data:
        r_id = int(data["approved_role_id"]) if data["approved_role_id"] else None
        if r_id:
            role = guild.get_role(r_id)
            if not role:
                return jsonify({"error": "Specified approved role does not exist"}), 400
            bot_top = guild.me.top_role if guild.me else None
            if bot_top and role.position >= bot_top.position:
                return jsonify({"error": "Approved role must be lower than the bot's highest role"}), 400
            if role.permissions.administrator or role.permissions.manage_guild:
                return jsonify({"error": "Approved role cannot possess Administrator or Manage Server permissions"}), 400
        update_fields["approved_role_id"] = r_id
    if "is_enabled" in data:
        update_fields["is_enabled"] = bool(data["is_enabled"])
    if "is_closed" in data:
        update_fields["is_closed"] = bool(data["is_closed"])
    if "max_submissions_per_user" in data:
        update_fields["max_submissions_per_user"] = max(0, int(data["max_submissions_per_user"]))
    if "cooldown_seconds" in data:
        update_fields["cooldown_seconds"] = max(0, int(data["cooldown_seconds"]))
    if "opens_at" in data:
        update_fields["opens_at"] = int(data["opens_at"]) if data["opens_at"] else None
    if "closes_at" in data:
        update_fields["closes_at"] = int(data["closes_at"]) if data["closes_at"] else None

    success = await db.update_form(guild_id, form_id, **update_fields)
    if not success:
        return jsonify({"error": "Failed to update form"}), 500

    # Sync updated embed on Discord if published
    updated_form = await db.get_form(guild_id, form_id)
    forms_cog = get_forms_cog()
    if forms_cog and updated_form and updated_form.get("panel_message_id"):
        await forms_cog.refresh_panel_message(updated_form)

    return jsonify({"success": True, "message": "Form updated successfully."})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>", methods=["DELETE"])
async def api_delete_form(guild_id: int, form_id: int):
    """Deletes a form and cascades questions and submissions."""
    db = FormsDB.get_instance()
    success = await db.delete_form(guild_id, form_id)
    if not success:
        return jsonify({"error": "Form not found or could not be deleted"}), 404
    return jsonify({"success": True, "message": "Form deleted successfully."})


# =============================================================================
# 2. Discord Panel Publishing & Lifecycle Sync
# =============================================================================

@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/publish", methods=["POST"])
async def api_publish_form(guild_id: int, form_id: int):
    """Publishes or updates the interactive public panel in Discord."""
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not connected"}), 404

    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    data = await request.get_json() or {}
    channel_id = int(data.get("channel_id") or form.get("panel_channel_id") or form["dest_channel_id"])
    target_ch = guild.get_channel(channel_id)
    if not target_ch or not hasattr(target_ch, "send"):
        return jsonify({"error": "Target channel not found in this guild"}), 400

    questions = await db.get_questions(form_id)
    if not questions:
        return jsonify({"error": "Cannot publish form without any questions. Add questions first."}), 400

    from cogs.forms import build_panel_embed_and_view
    embed, view = build_panel_embed_and_view(form)

    try:
        msg = await target_ch.send(embed=embed, view=view)
        await db.update_panel_message(form_id, target_ch.id, msg.id)
        return jsonify({
            "success": True,
            "message": f"Form panel published to #{target_ch.name}.",
            "message_id": str(msg.id),
            "jump_url": msg.jump_url
        })
    except Exception as e:
        logger.error("Error publishing form panel: %s", e)
        return jsonify({"error": f"Failed to send panel message: {e}"}), 500


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/toggle_status", methods=["POST"])
async def api_toggle_form_status(guild_id: int, form_id: int):
    """Toggles form closed/open state and refreshes live panel."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    new_closed = not bool(form.get("is_closed", False))
    await db.set_form_closed(guild_id, form_id, new_closed)
    form["is_closed"] = 1 if new_closed else 0

    forms_cog = get_forms_cog()
    if forms_cog and form.get("panel_message_id"):
        await forms_cog.refresh_panel_message(form)

    status_str = "closed" if new_closed else "reopened"
    return jsonify({"success": True, "is_closed": new_closed, "message": f"Form {status_str} successfully."})


# =============================================================================
# 3. Dynamic Question Management Endpoints
# =============================================================================

@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/questions", methods=["GET"])
async def api_list_questions(guild_id: int, form_id: int):
    """Lists questions for a form ordered by position."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    questions = await db.get_questions(form_id)
    return jsonify({"success": True, "questions": questions})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/questions", methods=["POST"])
async def api_add_question(guild_id: int, form_id: int):
    """Adds a new question to the form."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    data = await request.get_json()
    if not data or not data.get("label"):
        return jsonify({"error": "Missing question label"}), 400

    label = str(data["label"]).strip()[:45]
    style = "paragraph" if str(data.get("style", "short")).lower() in ("paragraph", "long", "textarea") else "short"
    placeholder = str(data.get("placeholder", "")).strip()[:100] or None
    required = bool(data.get("required", True))
    min_len = max(0, min(int(data.get("min_length", 0)), 1000))
    max_len = max(1, min(int(data.get("max_length", 1000)), 1000))
    if min_len > max_len:
        min_len = max_len

    qid = await db.add_question(
        form_id=form_id,
        label=label,
        style=style,
        placeholder=placeholder,
        required=required,
        min_length=min_len,
        max_length=max_len
    )

    questions = await db.get_questions(form_id)
    return jsonify({"success": True, "question_id": qid, "questions": questions, "message": "Question added successfully."})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/questions/<int:question_id>", methods=["PUT"])
async def api_update_question(guild_id: int, form_id: int, question_id: int):
    """Updates an existing question."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    data = await request.get_json()
    if not data:
        return jsonify({"error": "No update fields provided"}), 400

    update_fields = {}
    if "label" in data:
        update_fields["label"] = str(data["label"]).strip()[:45]
    if "style" in data:
        update_fields["style"] = "paragraph" if str(data["style"]).lower() in ("paragraph", "long") else "short"
    if "placeholder" in data:
        update_fields["placeholder"] = str(data["placeholder"]).strip()[:100] or None
    if "required" in data:
        update_fields["required"] = bool(data["required"])
    if "min_length" in data:
        update_fields["min_length"] = max(0, min(int(data["min_length"]), 1000))
    if "max_length" in data:
        update_fields["max_length"] = max(1, min(int(data["max_length"]), 1000))

    success = await db.update_question(form_id, question_id, **update_fields)
    if not success:
        return jsonify({"error": "Question not found or update failed"}), 404

    questions = await db.get_questions(form_id)
    return jsonify({"success": True, "questions": questions, "message": "Question updated successfully."})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/questions/<int:question_id>", methods=["DELETE"])
async def api_delete_question(guild_id: int, form_id: int, question_id: int):
    """Deletes a question and re-indexes remaining positions."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    success = await db.delete_question(form_id, question_id)
    if not success:
        return jsonify({"error": "Question not found"}), 404

    questions = await db.get_questions(form_id)
    return jsonify({"success": True, "questions": questions, "message": "Question deleted."})


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/questions/reorder", methods=["POST"])
async def api_reorder_questions(guild_id: int, form_id: int):
    """Reorders questions based on a provided list of question IDs."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    data = await request.get_json()
    if not data or not isinstance(data.get("question_ids"), list):
        return jsonify({"error": "Expected question_ids array"}), 400

    q_ids = [int(x) for x in data["question_ids"]]
    await db.reorder_questions(form_id, q_ids)
    questions = await db.get_questions(form_id)
    return jsonify({"success": True, "questions": questions, "message": "Questions reordered successfully."})


# =============================================================================
# 4. Submissions & Review Workflows
# =============================================================================

@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/submissions", methods=["GET"])
async def api_list_submissions(guild_id: int, form_id: int):
    """Lists submissions for a form with filtering and pagination."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    status = request.args.get("status")
    limit = min(max(1, int(request.args.get("limit", 100))), 500)
    offset = max(0, int(request.args.get("offset", 0)))

    submissions = await db.list_submissions(guild_id, form_id, status=status, limit=limit, offset=offset)
    stats = await db.get_submission_stats(guild_id, form_id)
    questions = await db.get_questions(form_id)

    return jsonify({
        "success": True,
        "submissions": submissions,
        "stats": stats,
        "questions": questions
    })


@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/submissions/<int:sub_id>/review", methods=["POST"])
async def api_review_submission(guild_id: int, form_id: int, sub_id: int):
    """
    Triggers Approve, Reject, or DM actions from the Web Dashboard.
    Syncs directly with the Discord bot (updates Discord embed, disables buttons, grants role, and DMs submitter).
    """
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    sub = await db.get_submission(sub_id, guild_id=guild_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404

    data = await request.get_json() or {}
    action = str(data.get("action", "")).strip().lower()
    note = str(data.get("note", "")).strip()

    if action not in ("approve", "reject", "dm"):
        return jsonify({"error": "Invalid action. Supported: 'approve', 'reject', 'dm'"}), 400

    forms_cog = get_forms_cog()
    reviewer_name = session.get("user_id") or "Dashboard Admin"

    # Construct mock user object representing reviewer for Discord sync
    class WebReviewer:
        id = 0
        name = reviewer_name
        mention = f"**{reviewer_name}**"

    reviewer = WebReviewer()

    if action == "approve":
        if forms_cog:
            await forms_cog.process_approval(None, sub_id, form, note, reviewer_user=reviewer)
        else:
            await db.update_submission_status(sub_id, "approved", 0, reviewer_name, note)
        return jsonify({"success": True, "status": "approved", "message": f"Submission #{sub_id} approved."})

    elif action == "reject":
        if not note:
            return jsonify({"error": "A rejection reason is required"}), 400
        if forms_cog:
            await forms_cog.process_rejection(None, sub_id, form, note, reviewer_user=reviewer)
        else:
            await db.update_submission_status(sub_id, "rejected", 0, reviewer_name, note)
        return jsonify({"success": True, "status": "rejected", "message": f"Submission #{sub_id} rejected."})

    elif action == "dm":
        if not note:
            return jsonify({"error": "DM message content is required"}), 400
        delivered = False
        if forms_cog:
            delivered = await forms_cog.process_staff_dm(None, sub_id, form, note, reviewer_user=reviewer)
        return jsonify({"success": True, "delivered": delivered, "message": "DM sent to submitter."})


# =============================================================================
# 5. Spreadsheet Export Endpoint (CSV / Excel)
# =============================================================================

@forms_bp.route("/api/guild/<int:guild_id>/forms/<int:form_id>/export", methods=["GET"])
async def api_export_submissions(guild_id: int, form_id: int):
    """Direct download for CSV (utf-8-sig) or Excel (.xlsx) report."""
    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    export_format = request.args.get("format", "csv").lower()
    status_filter = request.args.get("status", "all").lower()

    questions = await db.get_questions(form_id)
    submissions = await db.list_submissions(guild_id, form_id, status=status_filter, limit=10000)

    if export_format == "xlsx" and OPENPYXL_AVAILABLE:
        stream = await asyncio.to_thread(generate_xlsx_bytes, form, questions, submissions)
        filename = f"form_{form_id}_submissions.xlsx"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        stream = await asyncio.to_thread(generate_csv_bytes, form, questions, submissions)
        filename = f"form_{form_id}_submissions.csv"
        mimetype = "text/csv; charset=utf-8"

    return await send_file(
        stream,
        mimetype=mimetype,
        as_attachment=True,
        attachment_filename=filename
    )


# =============================================================================
# 6. Authenticated Internal Bridge Endpoint (Localhost IPC)
# =============================================================================

@forms_bp.route("/api/internal/forms/action", methods=["POST"])
async def api_internal_bridge():
    """
    Protected bridge endpoint for automated webhook / IPC communication.
    Binds to localhost and requires valid X-Internal-Secret.
    """
    client_ip = request.remote_addr or ""
    if client_ip not in ("127.0.0.1", "::1", "localhost", "<local>") and not getattr(current_app, "testing", False):
        return jsonify({"error": "Forbidden: internal bridge only accessible via loopback"}), 403

    if not verify_internal_secret(request):
        return jsonify({"error": "Unauthorized bridge access"}), 401

    data = await request.get_json() or {}
    action = data.get("action")
    form_id = data.get("form_id")
    guild_id = data.get("guild_id")

    if not action or not form_id or not guild_id:
        return jsonify({"error": "Missing action, form_id, or guild_id"}), 400

    db = FormsDB.get_instance()
    form = await db.get_form(guild_id, form_id)
    if not form:
        return jsonify({"error": "Form not found"}), 404

    forms_cog = get_forms_cog()
    if action == "refresh_panel" and forms_cog:
        success = await forms_cog.refresh_panel_message(form)
        return jsonify({"success": success})

    return jsonify({"error": f"Unhandled internal action '{action}'"}), 400
