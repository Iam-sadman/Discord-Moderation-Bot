"""
QC & Fault Tracker REST API Blueprint.
Provides endpoints for overview analytics, labeler diagnostics, fault log filtering,
direct web fault logging with Discord alert dispatch, Excel/CSV exports,
project CRUD, role-mapped team configuration, and 64-bit snowflake ID safety.
"""

import re
import io
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import discord
from quart import Blueprint, request, jsonify, session, send_file, current_app

from config import Config
from services.qc_faults_db import QCFaultsDB
from services.qc_faults_exporter import generate_qc_csv_bytes, generate_qc_xlsx_bytes, OPENPYXL_AVAILABLE

logger = logging.getLogger("QCFaultsAPI")

qc_faults_bp = Blueprint("qc_faults_api", __name__)

SNOWFLAKE_REGEX = re.compile(r"^\d{17,20}$")


# =============================================================================
# Helper: Snowflake String Conversion & Sanitization (64-Bit Safety)
# =============================================================================

def stringify_snowflake(val: Any) -> Optional[str]:
    """Ensures Discord 64-bit Snowflake IDs are stringified for JavaScript JSON safety."""
    if val is None or val == "" or val == 0:
        return None
    return str(val)


def sanitize_fault_record_for_json(record: Dict[str, Any]) -> Dict[str, Any]:
    """Converts all 64-bit integer Discord IDs in a fault record to strings."""
    if not record:
        return record
    d = dict(record)
    snowflake_keys = [
        "guild_id", "labeler_id", "team_role_id", "leader_id", "leader_user_id",
        "leader_role_id", "team_channel_id", "alert_channel_id", "alert_message_id",
        "logged_by_id"
    ]
    for k in snowflake_keys:
        if k in d and d[k] is not None:
            d[k] = str(d[k])
    return d


def sanitize_fault_list_for_json(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [sanitize_fault_record_for_json(r) for r in records]


def get_bot():
    """Retrieves the Discord bot instance attached to the Quart app."""
    return getattr(current_app, "discord_bot", None)


def get_qc_cog():
    """Retrieves the QCFaultsCog instance from the running Discord bot."""
    bot = get_bot()
    if bot:
        return bot.get_cog("QCFaults")
    return None


# =============================================================================
# Access Control Middleware
# =============================================================================

@qc_faults_bp.before_request
async def enforce_qc_operator_access():
    """
    Enforces that logged-in users have Administrator or Moderator privileges.
    Mutating actions require Administrator or Master Admin privileges.
    """
    if request.path.startswith("/api/internal/"):
        return

    role = str(session.get("role", "Viewer")).strip().lower()
    allowed_viewer_roles = ("administrator", "master admin", "moderator")

    if role not in allowed_viewer_roles:
        return jsonify({
            "error": "Forbidden",
            "message": "Operator privileges (Moderator or Administrator) required to view QC tracker."
        }), 403

    if request.method in ("POST", "PUT", "DELETE"):
        # For managing projects, teams, or settings: require Administrator
        path = request.path
        if "/projects" in path or "/teams" in path or "/settings" in path:
            if role not in ("administrator", "master admin"):
                return jsonify({
                    "error": "Forbidden",
                    "message": "Administrator privileges required to manage QC projects, teams, and settings."
                }), 403


# =============================================================================
# 1. Overview & Summary Analytics Endpoints
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/overview", methods=["GET"])
async def api_qc_overview(guild_id: int):
    """Returns overview KPIs, project-wise summaries, and team-wise summaries."""
    db = QCFaultsDB.get_instance()
    kpis = await db.get_overview_kpis(guild_id)
    projects = await db.get_project_summaries(guild_id)
    teams = await db.get_team_summaries(guild_id)

    # Sanitize 64-bit snowflake IDs
    sanitized_teams = sanitize_fault_list_for_json(teams)

    # Attach Leader Profile info to teams
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None

    if guild:
        for t in sanitized_teams:
            l_id_str = t.get("leader_user_id")
            if l_id_str and l_id_str.isdigit():
                l_id = int(l_id_str)
                member = guild.get_member(l_id)
                if not member:
                    try:
                        member = await guild.fetch_member(l_id)
                    except Exception:
                        member = None
                if member:
                    t["leader_name"] = str(member)
                    t["leader_display_name"] = member.display_name
                    t["leader_avatar"] = str(member.display_avatar.url)
                else:
                    t["leader_name"] = f"User #{l_id_str}"
                    t["leader_display_name"] = f"User #{l_id_str}"
                    t["leader_avatar"] = None
            else:
                t["leader_name"] = "Unassigned"
                t["leader_display_name"] = "Unassigned"
                t["leader_avatar"] = None

    return jsonify({
        "success": True,
        "kpis": kpis,
        "projects": projects,
        "teams": sanitized_teams
    })


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/labelers", methods=["GET"])
async def api_qc_labelers(guild_id: int):
    """Returns paginated summaries aggregated per labeler."""
    db = QCFaultsDB.get_instance()
    search = request.args.get("search", "")
    limit = min(max(1, int(request.args.get("limit", 50))), 100)
    offset = max(0, int(request.args.get("offset", 0)))

    total, labelers = await db.get_labeler_summaries(guild_id, search=search, limit=limit, offset=offset)
    sanitized_labelers = sanitize_fault_list_for_json(labelers)

    return jsonify({
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "labelers": sanitized_labelers
    })


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/labeler/<labeler_id_raw>", methods=["GET"])
async def api_qc_labeler_details(guild_id: int, labeler_id_raw: str):
    """Returns detailed diagnostics, project breakdown, recurring fault types, and history for a labeler."""
    if not SNOWFLAKE_REGEX.match(labeler_id_raw):
        return jsonify({"error": "Invalid Labeler Snowflake ID"}), 400

    labeler_id = int(labeler_id_raw)
    db = QCFaultsDB.get_instance()
    details = await db.get_labeler_details(guild_id, labeler_id)

    if not details:
        return jsonify({"error": "No fault records found for this labeler"}), 404

    # Sanitize 64-bit snowflake IDs
    details["summary"] = sanitize_fault_record_for_json(details["summary"])
    details["history"] = sanitize_fault_list_for_json(details["history"])

    # Resolve Discord member info
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if guild:
        m = guild.get_member(labeler_id)
        if not m:
            try:
                m = await guild.fetch_member(labeler_id)
            except Exception:
                m = None
        if m:
            details["member"] = {
                "id": str(m.id),
                "name": str(m),
                "display_name": m.display_name,
                "avatar": str(m.display_avatar.url)
            }
        else:
            details["member"] = {
                "id": str(labeler_id),
                "name": details["summary"].get("labeler_name", f"User #{labeler_id}"),
                "display_name": details["summary"].get("labeler_name", f"User #{labeler_id}"),
                "avatar": None
            }

    return jsonify({"success": True, "details": details})


# =============================================================================
# 2. Fault Logs Explorer & Web Logger Endpoints
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/faults", methods=["GET"])
async def api_qc_list_faults(guild_id: int):
    """Filterable, paginated list of fault logs."""
    db = QCFaultsDB.get_instance()
    project_code = request.args.get("project_code")
    team_name = request.args.get("team_name")
    severity = request.args.get("severity")
    status = request.args.get("status")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search")

    labeler_id_raw = request.args.get("labeler_id")
    labeler_id = int(labeler_id_raw) if labeler_id_raw and SNOWFLAKE_REGEX.match(labeler_id_raw) else None

    limit = min(max(1, int(request.args.get("limit", 50))), 200)
    offset = max(0, int(request.args.get("offset", 0)))

    total = await db.count_fault_logs(
        guild_id=guild_id,
        project_code=project_code,
        team_name=team_name,
        labeler_id=labeler_id,
        severity=severity,
        status=status,
        date_from=date_from,
        date_to=date_to,
        search=search
    )

    logs = await db.list_fault_logs(
        guild_id=guild_id,
        project_code=project_code,
        team_name=team_name,
        labeler_id=labeler_id,
        severity=severity,
        status=status,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=limit,
        offset=offset
    )

    sanitized_logs = sanitize_fault_list_for_json(logs)

    return jsonify({
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "faults": sanitized_logs
    })


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/faults", methods=["POST"])
async def api_qc_create_fault(guild_id: int):
    """Logs a fault directly from the Web Dashboard and optionally dispatches a Discord alert."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON payload"}), 400

    labeler_id_raw = str(data.get("labeler_id", "")).strip()
    if not SNOWFLAKE_REGEX.match(labeler_id_raw):
        return jsonify({"error": "A valid 18-19 digit Discord User ID is required for labeler"}), 400
    labeler_id = int(labeler_id_raw)

    project_code = str(data.get("project_code", "")).strip()
    if not project_code:
        return jsonify({"error": "Project code is required"}), 400

    severity = str(data.get("severity", "minor")).strip().lower()
    if severity not in ("minor", "major", "critical"):
        severity = "minor"

    description = str(data.get("description", "")).strip()
    if not description:
        return jsonify({"error": "Fault description is required"}), 400

    fault_type = str(data.get("fault_type", "")).strip() or None
    image_url = str(data.get("image_url", "")).strip() or None
    dispatch_discord_alert = bool(data.get("dispatch_discord_alert", True))

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Server guild not found on active bot instance"}), 404

    # Resolve labeler name
    labeler_name = f"User #{labeler_id}"
    labeler = guild.get_member(labeler_id)
    if not labeler:
        try:
            labeler = await guild.fetch_member(labeler_id)
        except Exception:
            labeler = None
    if labeler:
        labeler_name = str(labeler)

    # Resolve team
    team_id_raw = data.get("team_id")
    team_id = int(team_id_raw) if team_id_raw and str(team_id_raw).isdigit() else None
    team_name = "Unassigned"
    leader_user_id = None
    team_channel_id = None

    db = QCFaultsDB.get_instance()
    guild_teams = await db.list_teams(guild_id)

    if team_id:
        matched = next((t for t in guild_teams if t["id"] == team_id), None)
        if matched:
            team_name = matched["team_name"]
            leader_user_id = matched.get("leader_user_id")
            team_channel_id = matched.get("team_channel_id")
    elif labeler:
        role_ids = {r.id for r in labeler.roles}
        matched = next((t for t in guild_teams if t["team_role_id"] in role_ids), None)
        if matched:
            team_id = matched["id"]
            team_name = matched["team_name"]
            leader_user_id = matched.get("leader_user_id")
            team_channel_id = matched.get("team_channel_id")

    # Resolve logged_by from session
    logged_by_id = 0
    raw_uid = session.get("user_id", "")
    if SNOWFLAKE_REGEX.match(str(raw_uid)):
        logged_by_id = int(raw_uid)
    logged_by_name = session.get("name") or session.get("user_id") or "Dashboard Admin"

    # Insert into database
    try:
        fault_id = await db.create_fault_log(
            guild_id=guild_id,
            project_code=project_code,
            labeler_id=labeler_id,
            labeler_name=labeler_name,
            team_id=team_id,
            team_name=team_name,
            leader_id=leader_user_id,
            logged_by_id=logged_by_id,
            logged_by_name=logged_by_name,
            severity=severity,
            description=description,
            fault_type=fault_type,
            image_url=image_url,
            alert_channel_id=team_channel_id,
            alert_message_id=None
        )
    except Exception as e:
        logger.error(f"[ERROR] Failed to insert fault log from dashboard: {e}")
        return jsonify({"error": f"Failed to record fault in database: {e}"}), 500

    # Dispatch Discord Alert if requested
    alert_posted = False
    if dispatch_discord_alert:
        cog = get_qc_cog()
        if cog:
            msg_id = await cog.dispatch_web_fault_alert(
                fault_id=fault_id,
                guild_id=guild_id,
                project_code=project_code,
                labeler_id=labeler_id,
                severity=severity,
                description=description,
                fault_type=fault_type,
                image_url=image_url,
                team_id=team_id,
                logged_by_id=logged_by_id,
                logged_by_name=logged_by_name
            )
            alert_posted = bool(msg_id)

    return jsonify({
        "success": True,
        "fault_id": fault_id,
        "alert_posted": alert_posted,
        "message": f"Fault #{fault_id} logged successfully."
    })


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/faults/<int:fault_id>/ack", methods=["POST"])
async def api_qc_ack_fault(guild_id: int, fault_id: int):
    """Acknowledges a fault from the Web Dashboard."""
    db = QCFaultsDB.get_instance()
    fault = await db.get_fault_log(fault_id, guild_id)
    if not fault:
        return jsonify({"error": "Fault record not found"}), 404

    if fault.get("status") == "acknowledged":
        return jsonify({"success": True, "message": "Fault already acknowledged"})

    await db.acknowledge_fault(fault_id, guild_id)

    # Attempt to update Discord alert embed button
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if guild and fault.get("alert_channel_id") and fault.get("alert_message_id"):
        ch = guild.get_channel(fault["alert_channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(fault["alert_message_id"])
                if msg and msg.embeds:
                    embed = msg.embeds[0]
                    actor_name = session.get("name") or session.get("user_id") or "Dashboard Admin"
                    now_ts = int(datetime.now(timezone.utc).timestamp())

                    status_updated = False
                    for idx, field in enumerate(embed.fields):
                        if field.name and "status" in field.name.lower():
                            embed.set_field_at(
                                idx,
                                name="Status",
                                value=f"✅ **Acknowledged** via Dashboard by {actor_name} (<t:{now_ts}:R>)",
                                inline=field.inline
                            )
                            status_updated = True
                            break

                    if not status_updated:
                        embed.add_field(
                            name="Status",
                            value=f"✅ **Acknowledged** via Dashboard by {actor_name} (<t:{now_ts}:R>)",
                            inline=False
                        )

                    embed.set_footer(text=f"Acknowledged by {actor_name} • Fault #{fault_id}")

                    # Disable button
                    view = discord.ui.View(timeout=None)
                    view.add_item(
                        discord.ui.Button(
                            label="✅ Acknowledged / Fixed",
                            style=discord.ButtonStyle.secondary,
                            disabled=True,
                            custom_id=f"fault:ack:{fault_id}"
                        )
                    )
                    await msg.edit(embed=embed, view=view)
            except Exception as e:
                logger.warning(f"[!] Could not update Discord message for ack: {e}")

    return jsonify({"success": True, "message": f"Fault #{fault_id} acknowledged successfully"})


# =============================================================================
# 3. Export Endpoints (CSV / Excel .xlsx with formula injection protection)
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/export", methods=["GET"])
async def api_qc_export(guild_id: int):
    """Exports filtered fault logs to CSV or Microsoft Excel (.xlsx)."""
    db = QCFaultsDB.get_instance()
    export_format = request.args.get("format", "csv").lower()

    project_code = request.args.get("project_code")
    team_name = request.args.get("team_name")
    severity = request.args.get("severity")
    status = request.args.get("status")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search")

    labeler_id_raw = request.args.get("labeler_id")
    labeler_id = int(labeler_id_raw) if labeler_id_raw and SNOWFLAKE_REGEX.match(labeler_id_raw) else None

    # Fetch all matching logs up to 10,000 records
    logs = await db.list_fault_logs(
        guild_id=guild_id,
        project_code=project_code,
        team_name=team_name,
        labeler_id=labeler_id,
        severity=severity,
        status=status,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=10000,
        offset=0
    )

    date_str = datetime.now().strftime("%Y%m%d_%H%M")

    if export_format in ("xlsx", "excel"):
        xlsx_bytes = await asyncio.to_thread(generate_qc_xlsx_bytes, logs, "QC Fault Logs")
        return await send_file(
            io.BytesIO(xlsx_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            attachment_filename=f"qc_fault_logs_{guild_id}_{date_str}.xlsx"
        )
    else:
        csv_bytes = await asyncio.to_thread(generate_qc_csv_bytes, logs)
        return await send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            attachment_filename=f"qc_fault_logs_{guild_id}_{date_str}.csv"
        )


# =============================================================================
# 4. Projects Management Endpoints
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/projects", methods=["GET"])
async def api_qc_list_projects(guild_id: int):
    """Lists all configured QC projects for a guild."""
    db = QCFaultsDB.get_instance()
    projects = await db.list_projects(guild_id)
    return jsonify({"success": True, "projects": projects})


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/projects", methods=["POST"])
async def api_qc_create_project(guild_id: int):
    """Creates a new project code."""
    data = await request.get_json()
    if not data or not data.get("project_code") or not data.get("project_name"):
        return jsonify({"error": "Missing required fields: project_code and project_name"}), 400

    code = str(data["project_code"]).strip().upper()
    name = str(data["project_name"]).strip()
    desc = str(data.get("description", "")).strip() or None
    created_by = session.get("name") or session.get("user_id") or "Admin"

    db = QCFaultsDB.get_instance()
    existing = await db.get_project(guild_id, code)
    if existing:
        return jsonify({"error": f"Project code '{code}' already exists for this server"}), 409

    proj = await db.create_project(guild_id, code, name, desc, created_by)
    return jsonify({"success": True, "project": proj})


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/projects/<project_code>", methods=["PUT"])
async def api_qc_update_project(guild_id: int, project_code: str):
    """Updates project name, description, or active status."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    name = str(data["project_name"]).strip() if "project_name" in data else None
    desc = str(data["description"]).strip() if "description" in data else None
    is_active = bool(data["is_active"]) if "is_active" in data else None

    db = QCFaultsDB.get_instance()
    ok = await db.update_project(guild_id, project_code, name, desc, is_active)
    if not ok:
        return jsonify({"error": "Project not found or no changes made"}), 404

    return jsonify({"success": True, "message": "Project updated successfully"})


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/projects/<project_code>", methods=["DELETE"])
async def api_qc_delete_project(guild_id: int, project_code: str):
    """Deletes a project."""
    db = QCFaultsDB.get_instance()
    ok = await db.delete_project(guild_id, project_code)
    if not ok:
        return jsonify({"error": "Project not found"}), 404
    return jsonify({"success": True, "message": f"Project '{project_code}' deleted successfully"})


# =============================================================================
# 5. Teams & Role Mapping Configuration (10+ Teams)
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/teams", methods=["GET"])
async def api_qc_list_teams(guild_id: int):
    """Lists all configured teams with resolved leader names/avatars and stringified IDs."""
    db = QCFaultsDB.get_instance()
    teams = await db.list_teams(guild_id)
    sanitized = sanitize_fault_list_for_json(teams)

    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None

    if guild:
        for t in sanitized:
            l_id_str = t.get("leader_user_id")
            if l_id_str and l_id_str.isdigit():
                l_id = int(l_id_str)
                member = guild.get_member(l_id)
                if not member:
                    try:
                        member = await guild.fetch_member(l_id)
                    except Exception:
                        member = None
                if member:
                    t["leader_name"] = str(member)
                    t["leader_display_name"] = member.display_name
                    t["leader_avatar"] = str(member.display_avatar.url)
                else:
                    t["leader_name"] = f"User #{l_id_str}"
                    t["leader_display_name"] = f"User #{l_id_str}"
                    t["leader_avatar"] = None
            else:
                t["leader_name"] = "Unassigned"
                t["leader_display_name"] = "Unassigned"
                t["leader_avatar"] = None

            # Resolve role name
            r_id_str = t.get("team_role_id")
            if r_id_str and r_id_str.isdigit():
                role = guild.get_role(int(r_id_str))
                t["role_name"] = role.name if role else f"Role #{r_id_str}"
                t["role_color"] = str(role.color) if role else "#6366f1"
            else:
                t["role_name"] = "Unknown Role"
                t["role_color"] = "#6366f1"

    return jsonify({"success": True, "teams": sanitized})


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/teams", methods=["POST"])
async def api_qc_create_team(guild_id: int):
    """Creates a new role-mapped team."""
    data = await request.get_json()
    if not data or not data.get("team_name") or not data.get("team_role_id"):
        return jsonify({"error": "Missing required fields: team_name and team_role_id"}), 400

    team_name = str(data["team_name"]).strip()
    team_role_id_raw = str(data["team_role_id"]).strip()
    if not SNOWFLAKE_REGEX.match(team_role_id_raw):
        return jsonify({"error": "A valid 18-19 digit Discord Role ID is required"}), 400
    team_role_id = int(team_role_id_raw)

    leader_user_id = None
    leader_user_id_raw = str(data.get("leader_user_id") or "").strip()
    if leader_user_id_raw:
        if not SNOWFLAKE_REGEX.match(leader_user_id_raw):
            return jsonify({"error": "Team Leader ID must be a valid 18-19 digit Discord User ID"}), 400
        leader_user_id = int(leader_user_id_raw)

    leader_role_id = None
    leader_role_id_raw = str(data.get("leader_role_id") or "").strip()
    if leader_role_id_raw:
        if SNOWFLAKE_REGEX.match(leader_role_id_raw):
            leader_role_id = int(leader_role_id_raw)

    team_channel_id = None
    team_channel_id_raw = str(data.get("team_channel_id") or "").strip()
    if team_channel_id_raw:
        if SNOWFLAKE_REGEX.match(team_channel_id_raw):
            team_channel_id = int(team_channel_id_raw)

    db = QCFaultsDB.get_instance()
    # Check if role is already mapped
    existing = await db.get_team_by_role(guild_id, team_role_id)
    if existing:
        return jsonify({"error": f"Discord role is already mapped to team '{existing['team_name']}'"}), 409

    try:
        team = await db.create_team(
            guild_id=guild_id,
            team_name=team_name,
            team_role_id=team_role_id,
            leader_user_id=leader_user_id,
            leader_role_id=leader_role_id,
            team_channel_id=team_channel_id
        )
        return jsonify({"success": True, "team": sanitize_fault_record_for_json(team)})
    except Exception as e:
        logger.error(f"[ERROR] Failed to create team: {e}")
        return jsonify({"error": f"Failed to create team: {e}"}), 500


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/teams/<int:team_id>", methods=["PUT"])
async def api_qc_update_team(guild_id: int, team_id: int):
    """Updates team configuration."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    team_name = str(data["team_name"]).strip() if "team_name" in data else None

    team_role_id = None
    if "team_role_id" in data:
        raw = str(data["team_role_id"]).strip()
        if not SNOWFLAKE_REGEX.match(raw):
            return jsonify({"error": "Invalid Team Role ID"}), 400
        team_role_id = int(raw)

    leader_user_id = None
    if "leader_user_id" in data:
        raw = str(data["leader_user_id"] or "").strip()
        if raw and not SNOWFLAKE_REGEX.match(raw):
            return jsonify({"error": "Invalid Leader User ID"}), 400
        leader_user_id = int(raw) if raw else 0

    leader_role_id = None
    if "leader_role_id" in data:
        raw = str(data["leader_role_id"] or "").strip()
        if raw and SNOWFLAKE_REGEX.match(raw):
            leader_role_id = int(raw)
        else:
            leader_role_id = 0

    team_channel_id = None
    if "team_channel_id" in data:
        raw = str(data["team_channel_id"] or "").strip()
        if raw and SNOWFLAKE_REGEX.match(raw):
            team_channel_id = int(raw)
        else:
            team_channel_id = 0

    db = QCFaultsDB.get_instance()
    ok = await db.update_team(
        team_id=team_id,
        guild_id=guild_id,
        team_name=team_name,
        team_role_id=team_role_id,
        leader_user_id=leader_user_id,
        leader_role_id=leader_role_id,
        team_channel_id=team_channel_id
    )

    if not ok:
        return jsonify({"error": "Team not found or no changes made"}), 404

    return jsonify({"success": True, "message": "Team configuration updated successfully"})


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/teams/<int:team_id>", methods=["DELETE"])
async def api_qc_delete_team(guild_id: int, team_id: int):
    """Deletes a team configuration."""
    db = QCFaultsDB.get_instance()
    ok = await db.delete_team(team_id, guild_id)
    if not ok:
        return jsonify({"error": "Team not found"}), 404
    return jsonify({"success": True, "message": "Team deleted successfully"})


# =============================================================================
# 6. QC Settings Endpoints
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/settings", methods=["GET"])
async def api_qc_get_settings(guild_id: int):
    """Retrieves QC settings with stringified Snowflake IDs."""
    db = QCFaultsDB.get_instance()
    settings = await db.get_settings(guild_id)
    return jsonify({
        "success": True,
        "settings": {
            "guild_id": str(settings["guild_id"]),
            "alert_channel_id": stringify_snowflake(settings["alert_channel_id"]),
            "checker_role_ids": [str(rid) for rid in (settings.get("checker_role_ids") or [])]
        }
    })


@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/settings", methods=["POST"])
async def api_qc_update_settings(guild_id: int):
    """Updates default alert channel and checker role IDs."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    alert_channel_id = None
    raw_ch = str(data.get("alert_channel_id") or "").strip()
    if raw_ch and SNOWFLAKE_REGEX.match(raw_ch):
        alert_channel_id = int(raw_ch)

    checker_role_ids = []
    raw_roles = data.get("checker_role_ids") or []
    if isinstance(raw_roles, list):
        for rid in raw_roles:
            s_rid = str(rid).strip()
            if SNOWFLAKE_REGEX.match(s_rid):
                checker_role_ids.append(int(s_rid))

    db = QCFaultsDB.get_instance()
    await db.update_settings(guild_id, alert_channel_id, checker_role_ids)

    return jsonify({"success": True, "message": "QC Settings saved successfully"})


# =============================================================================
# 7. Server Member Search Helper (Autocomplete Member Picker)
# =============================================================================

@qc_faults_bp.route("/api/guild/<int:guild_id>/qc/members", methods=["GET"])
async def api_qc_search_members(guild_id: int):
    """Search server members to easily pick team leaders or labelers."""
    bot = get_bot()
    guild = bot.get_guild(guild_id) if bot else None
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    search = (request.args.get("search") or "").strip().lower()
    limit = min(max(1, int(request.args.get("limit", 25))), 50)

    results = []
    for m in guild.members:
        if m.bot:
            continue
        if search:
            m_name = m.name.lower()
            m_disp = m.display_name.lower()
            m_id = str(m.id)
            if search not in m_name and search not in m_disp and search != m_id:
                continue
        results.append({
            "id": str(m.id),
            "name": str(m),
            "display_name": m.display_name,
            "avatar": str(m.display_avatar.url)
        })
        if len(results) >= limit:
            break

    return jsonify({"success": True, "members": results})
