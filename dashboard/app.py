"""
Web Management Dashboard for Modular Discord Bot.
Features:
- Excel-backed User ID & Access Key authentication (data/dashboard_users.xlsx)
- Security & device telemetry tracking (IP, physical/ARP MAC resolution, device signature)
- Permission & setting change auditing
- Live Discord server audit logs viewer (data/logs.db)
- Interactive bot cogs and slash commands catalog
- Music player remote control
"""

import os
import io
import re
import json
import discord
from quart import Quart, render_template, request, redirect, url_for, session, jsonify
from quart_cors import cors
from config import Config

from dashboard.auth import authenticate_user, load_all_users
from dashboard.security import (
    get_client_ip,
    get_client_mac,
    get_device_info,
    log_dashboard_event,
    get_dashboard_logs
)
from dashboard.cogs_catalog import get_cogs_and_commands_catalog
from dashboard.server_logs import fetch_server_audit_logs, get_available_action_types


COLOR_TO_ANSI = {
    'red': '31', 'green': '32', 'yellow': '33', 'gold': '33', 'orange': '33',
    'blue': '34', 'blurple': '34', 'pink': '35', 'magenta': '35', 'fuchsia': '35',
    'cyan': '36', 'aqua': '36', 'white': '37', 'gray': '30;1', 'grey': '30;1',
    'slate': '30;1', 'dark': '30'
}

HEX_TO_ANSI = {
    '#ed4245': '31', '#ef4444': '31', '#f87171': '31',
    '#57f287': '32', '#22c55e': '32', '#10b981': '32',
    '#fee75c': '33', '#eab308': '33', '#f59e0b': '33', '#f97316': '33',
    '#5865f2': '34', '#3b82f6': '34', '#6366f1': '34',
    '#eb459e': '35', '#ec4899': '35', '#d946ef': '35',
    '#00e5ff': '36', '#06b6d4': '36', '#14b8a6': '36',
    '#ffffff': '37', '#f8fafc': '37',
    '#949ba4': '30;1', '#64748b': '30;1', '#475569': '30;1'
}


def resolve_ansi_code(color_str: str) -> str:
    """Maps color names and hex values to Discord-supported ANSI 16-color codes."""
    if not color_str:
        return '37'
    c = color_str.strip().lower()
    if c in COLOR_TO_ANSI:
        return COLOR_TO_ANSI[c]
    if c in HEX_TO_ANSI:
        return HEX_TO_ANSI[c]
    if c.startswith('#') and len(c) == 7:
        try:
            r = int(c[1:3], 16)
            g = int(c[3:5], 16)
            b = int(c[5:7], 16)
            if r > 180 and g < 100 and b < 100:
                return '31'  # Red
            if g > 180 and r < 120:
                return '32'  # Green
            if r > 180 and g > 150 and b < 100:
                return '33'  # Yellow
            if b > 180 and r < 120:
                return '34'  # Blue
            if r > 180 and b > 150:
                return '35'  # Pink / Magenta
            if g > 180 and b > 180:
                return '36'  # Cyan
            if r > 200 and g > 200 and b > 200:
                return '37'  # White
            return '30;1'
        except Exception:
            pass
    return '37'


def convert_color_tags_to_discord(text: str) -> str:
    """
    Converts human-friendly [color=X]selected text[/color] tags
    into Discord's native ANSI color-highlighted codeblocks.
    """
    if not text or '[color=' not in text.lower():
        return text

    ansi_pattern = re.compile(r'\[color=([#a-zA-Z0-9]+)\]([\s\S]*?)\[/color\]', re.IGNORECASE)
    lines = text.split('\n')
    output_lines = []
    ansi_buffer = []

    def flush_ansi():
        if ansi_buffer:
            output_lines.append("```ansi\n" + "\n".join(ansi_buffer) + "\n```")
            ansi_buffer.clear()

    for line in lines:
        if ansi_pattern.search(line):
            def replacer(m):
                code = resolve_ansi_code(m.group(1))
                return f"\x1b[{code}m{m.group(2)}\x1b[0m"
            converted_line = ansi_pattern.sub(replacer, line)
            ansi_buffer.append(converted_line)
        else:
            flush_ansi()
            output_lines.append(line)

    flush_ansi()
    return "\n".join(output_lines)


def resolve_author_profile(guild, user_id: str = "", access_key: str = "", name: str = "", custom_name: str = "") -> tuple:
    """
    Resolves author display name and avatar URL for announcements.
    Prioritizes showing the User ID (e.g. 'ARC_sadman') instead of generic 'Master Admin'.

    Priority for author name:
    1. custom_name (if explicitly typed in the composer input field)
    2. user_id (the logged-in User ID e.g. 'ARC_sadman', 'admin')
    3. Discord member display_name if found in the server
    4. session name (if not generic 'Master Admin' or 'Administrator')
    5. Fallback 'User'
    Never defaults to 'Master Admin'.

    Priority for avatar:
    1. Member lookup by numeric user_id
    2. Member lookup by numeric access_key (Discord Snowflake ID in Excel user sheet)
    3. Member lookup by username/nick in guild.members
    """
    user_id = (user_id or "").strip()
    access_key = (access_key or "").strip()
    name = (name or "").strip()
    custom_name = (custom_name or "").strip()

    # If access_key not in session, try lookup from Excel user registry
    if not access_key and user_id:
        try:
            for u in load_all_users():
                if u.get("user_id", "").strip().lower() == user_id.lower():
                    access_key = (u.get("access_key") or u.get("raw_access_key") or "").strip()
                    break
        except Exception:
            pass

    member = None
    if guild:
        try:
            # 1. By numeric user_id
            if user_id.isdigit():
                member = guild.get_member(int(user_id))
            # 2. By numeric access_key (Discord Snowflake ID)
            if not member and access_key.isdigit() and len(access_key) >= 15:
                member = guild.get_member(int(access_key))
            # 3. By matching username / nickname / display_name
            if not member and user_id:
                clean_uid = user_id.lower()
                for m in getattr(guild, "members", []):
                    m_name = (getattr(m, "name", "") or "").lower()
                    m_nick = (getattr(m, "nick", "") or "").lower()
                    m_disp = (getattr(m, "display_name", "") or "").lower()
                    if clean_uid in (m_name, m_nick, m_disp) or (clean_uid.startswith("arc_") and clean_uid[4:] in (m_name, m_nick, m_disp)):
                        member = m
                        break
        except Exception:
            pass

    avatar_url = ""
    if member and getattr(member, "display_avatar", None):
        avatar_url = str(member.display_avatar.url)

    # Resolve author display name
    if custom_name:
        author_name = custom_name
    elif user_id and user_id.lower() not in ("master admin", "administrator", "anonymous"):
        author_name = user_id
    elif member and getattr(member, "display_name", None):
        author_name = member.display_name
    elif name and name.lower() not in ("master admin", "administrator", "admin", "viewer"):
        author_name = name
    else:
        author_name = user_id or "User"

    return author_name, avatar_url


def create_dashboard(bot):
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    app = Quart(__name__, template_folder=template_dir)
    app.secret_key = Config.WEB_SECRET
    app = cors(app)

    # ──── Helper: Record Security Event ────
    async def record_audit_event(action: str, target: str, details: str):
        user_id = session.get("user_id", "anonymous")
        ip = get_client_ip(request)
        mac = get_client_mac(ip)
        device = get_device_info(request)
        await log_dashboard_event(
            user_id=user_id,
            action=action,
            target=target,
            details=details,
            ip_address=ip,
            mac_address=mac,
            device_info=device
        )

    # ──── Authentication Middleware ────
    @app.before_request
    async def require_authentication():
        # Allow login route and static assets without session
        if request.path.startswith("/login"):
            return
        if request.path.startswith("/favicon.ico"):
            return

        # Check for active session
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Unauthorized",
                    "message": "Access Key authentication required to access this endpoint."
                }), 401
            return redirect(url_for("login"))

    # ──── Authentication Routes ────
    @app.route("/login", methods=["GET", "POST"])
    async def login():
        # If already logged in, redirect to main hub
        if session.get("user_id"):
            return redirect(url_for("index"))

        error = None

        if request.method == "POST":
            # Support both JSON (AJAX) and traditional Form POST
            if request.is_json:
                data = await request.get_json()
            else:
                data = await request.form

            user_id = (data.get("user_id") or "").strip()
            access_key = (data.get("access_key") or "").strip()

            ip = get_client_ip(request)
            mac = get_client_mac(ip)
            device = get_device_info(request)

            user = authenticate_user(user_id, access_key)

            if user:
                # Login Success
                session["user_id"] = user["user_id"]
                session["role"] = user["role"]
                # Avoid setting generic 'Master Admin' as session name; prefer user_id
                raw_name = (user.get("name") or "").strip()
                if not raw_name or raw_name.lower() in ("master admin", "administrator"):
                    session["name"] = user["user_id"]
                else:
                    session["name"] = raw_name
                session["access_key"] = user.get("access_key", "")
                session["ip"] = ip
                session["mac"] = mac
                session["device"] = device

                await log_dashboard_event(
                    user_id=user["user_id"],
                    action="LOGIN_SUCCESS",
                    target="Dashboard Portal",
                    details=f"Authorized operator ({user['role']}) from {ip}",
                    ip_address=ip,
                    mac_address=mac,
                    device_info=device
                )

                if request.is_json:
                    return jsonify({"success": True, "redirect": "/"})
                return redirect(url_for("index"))
            else:
                # Login Failed
                await log_dashboard_event(
                    user_id=user_id or "unknown",
                    action="LOGIN_FAILED",
                    target="Dashboard Portal",
                    details="Invalid user ID or access key",
                    ip_address=ip,
                    mac_address=mac,
                    device_info=device
                )

                error = "Invalid User ID or Access Key. Please check the Excel sheet (data/dashboard_users.xlsx) credentials."
                if request.is_json:
                    return jsonify({"success": False, "error": error}), 401

        return await render_template("login.html", error=error)

    @app.route("/logout")
    async def logout():
        user_id = session.get("user_id", "unknown")
        ip = get_client_ip(request)
        mac = get_client_mac(ip)
        device = get_device_info(request)

        await log_dashboard_event(
            user_id=user_id,
            action="LOGOUT",
            target="Dashboard Portal",
            details="Operator logged out",
            ip_address=ip,
            mac_address=mac,
            device_info=device
        )

        session.clear()
        return redirect(url_for("login"))

    # ──── Main Views ────
    @app.route("/")
    async def index():
        guilds = []
        total_members = 0

        for guild in bot.guilds:
            guilds.append({
                "id": guild.id,
                "name": guild.name,
                "icon": str(guild.icon.url) if guild.icon else None,
                "member_count": guild.member_count or 0,
            })
            total_members += (guild.member_count or 0)

        latency = round((bot.latency or 0) * 1000)

        return await render_template(
            "index.html",
            guilds=guilds,
            total_members=total_members,
            latency=latency,
            bot=bot
        )

    @app.route("/guild/<int:guild_id>")
    async def guild_dashboard(guild_id):
        guild = bot.get_guild(guild_id)
        if not guild:
            return "Guild not found", 404

        settings = await Config.get_all_guild_settings(guild_id)

        prefix = settings.get("prefix", "!")
        announcement_roles = settings.get("announcement_roles", [])
        loa_timezone = settings.get("loa_timezone", "UTC")

        # Module toggles
        modules = {
            "music": settings.get("module_music_enabled", True),
            "logger": settings.get("module_logger_enabled", True),
            "loa": settings.get("module_loa_enabled", True),
            "labeler_tracker": settings.get("module_labeler_tracker_enabled", True)
        }

        # Log channel and filtered users
        log_channel_id = settings.get("log_channel_id")
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None
        filtered_users = settings.get("log_filtered_users", [])

        # Channels and roles
        text_channels = [{"id": c.id, "name": c.name} for c in guild.text_channels]
        voice_channels = [{"id": c.id, "name": c.name} for c in guild.voice_channels]
        roles = [{"id": r.id, "name": r.name, "color": str(r.color)} for r in guild.roles if not r.managed]
        members = [{"id": m.id, "name": str(m)} for m in guild.members if not m.bot][:100]

        # Music player state
        music_cog = bot.get_cog("Music")
        music_state = None
        if music_cog:
            state = music_cog.get_state(guild_id)
            music_state = {
                "is_playing": state.is_playing,
                "current": {
                    "title": state.current.title,
                    "duration": state.current.duration,
                    "requester": str(state.current.requester),
                    "thumbnail": state.current.thumbnail,
                } if state.current else None,
                "queue_length": len(state.queue),
                "volume": int(state.volume * 100),
                "loop_mode": state.loop_mode,
                "queue": [
                    {"title": s.title, "duration": s.duration, "requester": str(s.requester)}
                    for s in state.queue[:20]
                ],
            }

        # Cogs catalog and available action types for server logs
        cogs_catalog = get_cogs_and_commands_catalog(bot)
        action_types = await get_available_action_types(guild_id)
        cog_permissions = await Config.get_all_cog_permissions(guild_id)
        latency = round((bot.latency or 0) * 1000)

        # Resolve logged-in dashboard user for announcement author (showing User ID instead of Master Admin)
        raw_uid = session.get("user_id", "")
        raw_key = session.get("access_key", "")
        raw_name = session.get("name", "")
        dashboard_author_name, dashboard_author_avatar = resolve_author_profile(
            guild, user_id=raw_uid, access_key=raw_key, name=raw_name
        )

        return await render_template(
            "guild.html",
            guild=guild,
            prefix=prefix,
            modules=modules,
            log_channel=log_channel,
            filtered_users=filtered_users,
            announcement_roles=announcement_roles,
            loa_timezone=loa_timezone,
            text_channels=text_channels,
            voice_channels=voice_channels,
            roles=roles,
            members=members,
            music_state=music_state,
            settings=settings,
            cogs_catalog=cogs_catalog,
            cog_permissions=cog_permissions,
            action_types=action_types,
            latency=latency,
            bot=bot,
            dashboard_author_name=dashboard_author_name,
            dashboard_author_avatar=dashboard_author_avatar
        )

    # ──── API Routes: Logs & Catalog ────
    @app.route("/api/guild/<int:guild_id>/server_logs", methods=["GET"])
    async def api_server_logs(guild_id):
        action_type = request.args.get("action_type")
        search = request.args.get("search")
        limit = int(request.args.get("limit", 25))
        offset = int(request.args.get("offset", 0))

        data = await fetch_server_audit_logs(
            guild_id=guild_id,
            action_type=action_type,
            search=search,
            limit=limit,
            offset=offset
        )
        return jsonify(data)

    @app.route("/api/security_logs", methods=["GET"])
    async def api_security_logs():
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        logs = await get_dashboard_logs(limit=limit, offset=offset)
        return jsonify({"success": True, "logs": logs})

    @app.route("/api/cogs_catalog", methods=["GET"])
    async def api_cogs_catalog():
        catalog = get_cogs_and_commands_catalog(bot)
        return jsonify({"success": True, "catalog": catalog})

    # ──── API Routes: Settings & Permissions (Audited) ────
    @app.route("/api/guild/<int:guild_id>/module", methods=["POST"])
    async def toggle_module(guild_id):
        data = await request.get_json()
        module_name = data.get("module")
        enabled = bool(data.get("enabled", True))
        
        await Config.set_guild_setting(guild_id, f"module_{module_name}_enabled", enabled)
        await record_audit_event(
            action="TOGGLE_MODULE",
            target=f"Guild {guild_id}",
            details=f"Module '{module_name}' set to {enabled}"
        )
        return jsonify({"success": True, "module": module_name, "enabled": enabled})

    @app.route("/api/guild/<int:guild_id>/settings/prefix", methods=["POST"])
    async def update_prefix(guild_id):
        data = await request.get_json()
        new_prefix = (data.get("prefix") or "!").strip()
        
        await Config.set_guild_setting(guild_id, "prefix", new_prefix)
        await record_audit_event(
            action="UPDATE_PREFIX",
            target=f"Guild {guild_id}",
            details=f"Command prefix updated to '{new_prefix}'"
        )
        return jsonify({"success": True, "prefix": new_prefix})

    @app.route("/api/guild/<int:guild_id>/log/channel", methods=["POST"])
    async def set_log_channel(guild_id):
        data = await request.get_json()
        raw_cid = data.get("channel_id")
        channel_id = None
        if raw_cid is not None and str(raw_cid).strip() != "":
            try:
                channel_id = int(raw_cid)
            except (ValueError, TypeError):
                channel_id = None
        
        await Config.set_guild_setting(guild_id, "log_channel_id", channel_id)
        await record_audit_event(
            action="SET_LOG_CHANNEL",
            target=f"Guild {guild_id}",
            details=f"Audit logging channel set to {channel_id or 'Disabled'}"
        )
        return jsonify({"success": True})

    @app.route("/api/guild/<int:guild_id>/log/filter", methods=["POST"])
    async def update_log_filter(guild_id):
        data = await request.get_json()
        action = data.get("action")  # "add", "remove", "clear"
        raw_uid = data.get("user_id")
        user_id = None
        if raw_uid is not None:
            try:
                user_id = int(raw_uid)
            except (ValueError, TypeError):
                user_id = raw_uid

        raw_filters = await Config.get_guild_setting(guild_id, "log_filtered_users", [])
        filters = []
        for u in raw_filters:
            try:
                filters.append(int(u))
            except (ValueError, TypeError):
                filters.append(u)

        if action == "add" and user_id is not None and user_id not in filters:
            filters.append(user_id)
        elif action == "remove" and user_id is not None:
            filters = [u for u in filters if u != user_id]
        elif action == "clear":
            filters = []

        await Config.set_guild_setting(guild_id, "log_filtered_users", filters)
        await record_audit_event(
            action="UPDATE_LOG_FILTER",
            target=f"Guild {guild_id}",
            details=f"Log filter action: {action}, user: {user_id}"
        )
        return jsonify({"success": True, "filters": filters})

    @app.route("/api/guild/<int:guild_id>/announcement_roles", methods=["POST"])
    async def update_announcement_roles(guild_id):
        data = await request.get_json()
        action = data.get("action")
        raw_rid = data.get("role_id")
        role_id = None
        if raw_rid is not None:
            try:
                role_id = int(raw_rid)
            except (ValueError, TypeError):
                role_id = raw_rid

        raw_roles = await Config.get_guild_setting(guild_id, "announcement_roles", [])
        roles = []
        for r in raw_roles:
            try:
                roles.append(int(r))
            except (ValueError, TypeError):
                roles.append(r)

        if action == "add" and role_id is not None and role_id not in roles:
            roles.append(role_id)
        elif action == "remove" and role_id is not None:
            roles = [r for r in roles if r != role_id]
        elif action == "clear":
            roles = []

        await Config.set_guild_setting(guild_id, "announcement_roles", roles)
        await record_audit_event(
            action="UPDATE_ANNOUNCEMENT_ROLES",
            target=f"Guild {guild_id}",
            details=f"Announcement roles action: {action}, role: {role_id}"
        )
        return jsonify({"success": True, "roles": roles})

    @app.route("/api/guild/<int:guild_id>/loa_config", methods=["POST"])
    async def update_loa_config(guild_id):
        data = await request.get_json()
        timezone = data.get("timezone")

        if timezone:
            await Config.set_guild_setting(guild_id, "loa_timezone", timezone)

        await record_audit_event(
            action="UPDATE_LOA_CONFIG",
            target=f"Guild {guild_id}",
            details=f"Updated LOA configuration: {data}"
        )
        return jsonify({"success": True})

    @app.route("/api/guild/<int:guild_id>/settings", methods=["POST"])
    async def update_settings(guild_id):
        data = await request.get_json()
        key = data.get("key")
        value = data.get("value")
        
        await Config.set_guild_setting(guild_id, key, value)
        await record_audit_event(
            action="UPDATE_SETTING",
            target=f"Guild {guild_id}",
            details=f"Setting '{key}' updated to '{value}'"
        )
        return jsonify({"success": True})

    # ──── Music Remote Control API ────
    @app.route("/api/guild/<int:guild_id>/music/skip", methods=["POST"])
    async def music_skip(guild_id):
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        if state.voice_client and state.is_playing:
            state._skip_flag = True
            state.voice_client.stop()
            await record_audit_event("MUSIC_SKIP", f"Guild {guild_id}", "Skipped current track")
            return jsonify({"success": True})
        return jsonify({"error": "Nothing playing"}), 400

    @app.route("/api/guild/<int:guild_id>/music/stop", methods=["POST"])
    async def music_stop(guild_id):
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        state.queue.clear()
        state.loop_mode = "off"
        if state.voice_client:
            state.voice_client.stop()
        state.is_playing = False
        state.current = None
        await record_audit_event("MUSIC_STOP", f"Guild {guild_id}", "Stopped playback and cleared queue")
        return jsonify({"success": True})

    @app.route("/api/guild/<int:guild_id>/music/volume", methods=["POST"])
    async def music_volume(guild_id):
        data = await request.get_json()
        level = data.get("volume", 100)
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        state.volume = level / 100.0
        if state.voice_client and state.voice_client.source:
            if hasattr(state.voice_client.source, "volume"):
                state.voice_client.source.volume = state.volume
        return jsonify({"success": True, "volume": level})

    @app.route("/api/guild/<int:guild_id>/music/pause", methods=["POST"])
    async def music_pause(guild_id):
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await record_audit_event("MUSIC_PAUSE", f"Guild {guild_id}", "Paused audio playback")
            return jsonify({"success": True, "paused": True})
        elif state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await record_audit_event("MUSIC_RESUME", f"Guild {guild_id}", "Resumed audio playback")
            return jsonify({"success": True, "paused": False})
        return jsonify({"error": "Nothing playing"}), 400

    @app.route("/api/guild/<int:guild_id>/music/loop", methods=["POST"])
    async def music_loop(guild_id):
        data = await request.get_json()
        mode = data.get("mode", "off")
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        state.loop_mode = mode
        await record_audit_event("MUSIC_LOOP", f"Guild {guild_id}", f"Looping mode set to {mode}")
        return jsonify({"success": True, "loop_mode": mode})

    @app.route("/api/guild/<int:guild_id>/music/state", methods=["GET"])
    async def music_state_api(guild_id):
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        return jsonify({
            "is_playing": state.is_playing,
            "current": {
                "title": state.current.title,
                "duration": state.current.duration,
                "requester": str(state.current.requester),
                "thumbnail": state.current.thumbnail,
            } if state.current else None,
            "queue_length": len(state.queue),
            "volume": int(state.volume * 100),
            "loop_mode": state.loop_mode,
            "queue": [
                {"title": s.title, "duration": s.duration, "requester": str(s.requester)}
                for s in state.queue[:20]
            ],
        })

    # ──── Cog Permissions (RBAC) API ────
    @app.route("/api/guild/<int:guild_id>/cog_permissions", methods=["GET"])
    async def api_get_cog_permissions(guild_id):
        perms = await Config.get_all_cog_permissions(guild_id)
        return jsonify({"success": True, "permissions": perms, "cog_permissions": perms})

    @app.route("/api/guild/<int:guild_id>/cog_permissions", methods=["POST"])
    async def api_set_cog_permissions(guild_id):
        data = await request.get_json()
        cog_name = data.get("cog_name")
        access_level = data.get("access_level", "admin_only")
        allowed_roles = data.get("allowed_roles", [])

        if not cog_name:
            return jsonify({"error": "cog_name is required"}), 400

        await Config.set_cog_permission(guild_id, cog_name, access_level, allowed_roles)
        current_user = session.get("user_id", "admin")
        await record_audit_event(
            action="UPDATE_COG_PERMISSIONS",
            target=f"Guild {guild_id}",
            details=f"Updated permissions for cog '{cog_name}' to '{access_level}' with {len(allowed_roles)} allowed roles by {current_user}"
        )
        return jsonify({"success": True, "cog_name": cog_name, "access_level": access_level})

    # ──── Announcement Composer & Delivery API ────
    @app.route("/api/guild/<int:guild_id>/announcement/send", methods=["POST"])
    async def api_send_announcement(guild_id):
        guild = bot.get_guild(guild_id)
        if not guild:
            return jsonify({"error": "Guild not found or bot not in guild"}), 404

        # Parse form data (supporting multipart/form-data file uploads and JSON)
        is_multipart = request.content_type and "multipart/form-data" in request.content_type
        if is_multipart:
            form = await request.form
            req_files = await request.files
            channel_id_raw = form.get("channel_id")
            content = (form.get("content") or "").strip()
            title = (form.get("title") or "").strip()
            ann_type = form.get("format_type", form.get("type", "embed"))
            mention_type = form.get("mention_type", "none")
            mention_role_id = form.get("mention_role_id")
            color_hex = (form.get("embed_color", form.get("color", "#5865F2")) or "#5865F2").strip()
            footer_text = (form.get("footer_text") or "").strip()
            buttons_raw_str = form.get("buttons", "[]")
            try:
                buttons_raw = json.loads(buttons_raw_str) if isinstance(buttons_raw_str, str) else buttons_raw_str
            except Exception:
                buttons_raw = []
            uploaded_banner = req_files.get("image_file")
            uploaded_thumb = req_files.get("thumb_file")
            extra_files_raw = req_files.getlist("extra_files")
            heading_color = (form.get("heading_color") or "").strip()
            footer_color = (form.get("footer_color") or "").strip()
            show_author = form.get("show_author", "false").lower() in ("true", "1", "yes")
            author_name_field = (form.get("author_name") or "").strip()
        else:
            data = await request.get_json() or {}
            channel_id_raw = data.get("channel_id")
            content = (data.get("content") or "").strip()
            title = (data.get("title") or "").strip()
            ann_type = data.get("format_type", data.get("type", "embed"))
            mention_type = data.get("mention_type", "none")
            mention_role_id = data.get("mention_role_id")
            color_hex = (data.get("embed_color", data.get("color", "#5865F2")) or "#5865F2").strip()
            footer_text = (data.get("footer_text") or "").strip()
            buttons_raw = data.get("buttons", [])
            uploaded_banner = None
            uploaded_thumb = None
            extra_files_raw = []
            heading_color = (data.get("heading_color") or "").strip()
            footer_color = (data.get("footer_color") or "").strip()
            show_author = str(data.get("show_author", "false")).lower() in ("true", "1", "yes")
            author_name_field = (data.get("author_name") or "").strip()

        if not channel_id_raw:
            return jsonify({"error": "Target destination channel is required"}), 400

        try:
            channel_id = int(channel_id_raw)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid channel ID"}), 400

        channel = guild.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                return jsonify({"error": "Channel not found on Discord server"}), 404

        # Read uploaded files into discord.File objects
        discord_files = []
        banner_filename = None
        thumb_filename = None

        if uploaded_banner and getattr(uploaded_banner, "filename", None):
            file_bytes = uploaded_banner.read()
            if file_bytes:
                orig_ext = os.path.splitext(uploaded_banner.filename)[1].lower() or ".png"
                if orig_ext not in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                    orig_ext = ".png"
                banner_filename = f"announcement_banner{orig_ext}"
                discord_files.append(discord.File(io.BytesIO(file_bytes), filename=banner_filename))

        if uploaded_thumb and getattr(uploaded_thumb, "filename", None):
            thumb_bytes = uploaded_thumb.read()
            if thumb_bytes:
                orig_ext = os.path.splitext(uploaded_thumb.filename)[1].lower() or ".png"
                if orig_ext not in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                    orig_ext = ".png"
                thumb_filename = f"announcement_thumb{orig_ext}"
                discord_files.append(discord.File(io.BytesIO(thumb_bytes), filename=thumb_filename))

        # Extra attachments (images, documents, etc.)
        for extra in (extra_files_raw or []):
            if not getattr(extra, "filename", None):
                continue
            extra_bytes = extra.read()
            if extra_bytes:
                safe_name = os.path.basename(extra.filename) or "attachment"
                discord_files.append(discord.File(io.BytesIO(extra_bytes), filename=safe_name))

        if not content and not title and not discord_files:
            return jsonify({"error": "Announcement message content, title, or image is required"}), 400

        # Construct mention string
        mention_str = ""
        if mention_type == "everyone":
            mention_str = "@everyone"
        elif mention_type == "here":
            mention_str = "@here"
        elif mention_type == "role" and mention_role_id:
            try:
                mention_str = f"<@&{int(mention_role_id)}>"
            except (ValueError, TypeError):
                pass

        # Interactive Link Buttons
        view = None
        if isinstance(buttons_raw, list) and buttons_raw:
            view = discord.ui.View()
            for btn in buttons_raw[:5]:
                label = btn.get("label", "").strip() or "Link"
                url = btn.get("url", "").strip()
                emoji = btn.get("emoji", "").strip() or None
                if url and (url.startswith("http://") or url.startswith("https://")):
                    view.add_item(discord.ui.Button(label=label, url=url, emoji=emoji, style=discord.ButtonStyle.link))

        # Convert custom inline color tags into Discord ANSI syntax blocks
        discord_content = convert_color_tags_to_discord(content)

        # If heading color was customized and embed color was default, match embed color
        if color_hex.upper() == "#5865F2" and heading_color and heading_color.startswith("#") and heading_color.upper() != "#FFFFFF":
            color_hex = heading_color

        # Build AllowedMentions — Discord ONLY sends push notifications to roles
        # if the role object is listed explicitly in allowed_mentions.roles.
        # Using roles=True alone may suppress notifications on some servers.
        if mention_type == "everyone":
            allowed_mentions = discord.AllowedMentions(everyone=True, roles=False, users=False)
        elif mention_type == "here":
            allowed_mentions = discord.AllowedMentions(everyone=True, roles=False, users=False)
        elif mention_type == "role" and mention_role_id:
            role_obj = guild.get_role(int(mention_role_id))
            if role_obj:
                allowed_mentions = discord.AllowedMentions(everyone=False, roles=[role_obj], users=False)
            else:
                # Fallback: allow all roles if specific role not cached
                allowed_mentions = discord.AllowedMentions(everyone=False, roles=True, users=False)
        else:
            allowed_mentions = discord.AllowedMentions.none()

        try:
            if ann_type == "embed":
                # Parse embed color
                try:
                    c_int = int(color_hex.lstrip("#"), 16)
                except Exception:
                    c_int = 0x5865F2

                embed = discord.Embed(
                    title=title if title else None,
                    description=discord_content if discord_content else None,
                    color=discord.Color(c_int)
                )
                if thumb_filename:
                    embed.set_thumbnail(url=f"attachment://{thumb_filename}")
                if banner_filename:
                    embed.set_image(url=f"attachment://{banner_filename}")
                if footer_text:
                    embed.set_footer(text=footer_text)

                # Author attribution — resolve User ID / server member display name + avatar
                if show_author:
                    try:
                        raw_uid = session.get("user_id", "")
                        raw_key = session.get("access_key", "")
                        raw_name = session.get("name", "")
                        author_display, author_icon = resolve_author_profile(
                            guild, user_id=raw_uid, access_key=raw_key, name=raw_name, custom_name=author_name_field
                        )
                        embed.set_author(name=author_display, icon_url=author_icon if author_icon else None)
                    except Exception as e:
                        logger.error(f"Error setting embed author: {e}")

                sent_msg = await channel.send(
                    content=mention_str if mention_str else None,
                    embed=embed,
                    files=discord_files if discord_files else None,
                    view=view,
                    allowed_mentions=allowed_mentions
                )
            else:
                full_body = ""
                if mention_str:
                    full_body += f"{mention_str}\n\n"
                if title:
                    full_body += f"**{title}**\n\n"
                full_body += discord_content
                # Author attribution for plain text (appended as bold signature)
                if show_author:
                    try:
                        raw_uid = session.get("user_id", "")
                        raw_key = session.get("access_key", "")
                        raw_name = session.get("name", "")
                        author_display, _ = resolve_author_profile(
                            guild, user_id=raw_uid, access_key=raw_key, name=raw_name, custom_name=author_name_field
                        )
                        full_body += f"\n\n— **{author_display}**"
                    except Exception as e:
                        logger.error(f"Error setting plain text author: {e}")
                sent_msg = await channel.send(
                    content=full_body.strip() if full_body.strip() else None,
                    files=discord_files if discord_files else None,
                    view=view,
                    allowed_mentions=allowed_mentions
                )

            current_user = session.get("user_id", "admin")
            has_image = bool(discord_files)
            await record_audit_event(
                action="SEND_ANNOUNCEMENT",
                target=f"Guild {guild_id} #{channel.name}",
                details=f"Announcement sent to #{channel.name} by {current_user} (Type: {ann_type}, Image: {'Yes' if has_image else 'No'}, Mention: {mention_type})"
            )

            return jsonify({
                "success": True,
                "message_id": str(getattr(sent_msg, "id", "")),
                "jump_url": getattr(sent_msg, "jump_url", ""),
                "channel_name": channel.name
            })
        except Exception as e:
            return jsonify({"error": f"Discord API Error: {str(e)}"}), 500

    return app
