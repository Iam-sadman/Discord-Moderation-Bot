import json
from quart import Quart, render_template_string, request, redirect, url_for, session, jsonify
from quart_cors import cors
from config import Config

def create_dashboard(bot):
    app = Quart(__name__)
    app.secret_key = Config.WEB_SECRET
    app = cors(app)

    # ──── HTML Templates (inline for portability) ────

    # Store templates inline as a dict
    templates = {}

    # ──── Routes ────

    @app.route("/")
    async def index():
        guilds = []
        for guild in bot.guilds:
            guilds.append({
                "id": guild.id,
                "name": guild.name,
                "icon": str(guild.icon.url) if guild.icon else None,
                "member_count": guild.member_count,
            })
        return await render_template_string(INDEX_TEMPLATE, guilds=guilds, bot_name=str(bot.user) if bot.user else "Bot")

    @app.route("/guild/<int:guild_id>")
    async def guild_dashboard(guild_id):
        guild = bot.get_guild(guild_id)
        if not guild:
            return "Guild not found", 404

        settings = await Config.get_all_guild_settings(guild_id)

        # Get module states
        modules = {
            "music": settings.get("module_music_enabled", True),
            "logger": settings.get("module_logger_enabled", True),
        }

        # Get log settings
        log_channel_id = settings.get("log_channel_id")
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None
        filtered_users = settings.get("log_filtered_users", [])

        # Get text channels for dropdown
        text_channels = [
            {"id": c.id, "name": c.name}
            for c in guild.text_channels
        ]

        # Get members for filter dropdown
        members = [
            {"id": m.id, "name": str(m), "avatar": str(m.display_avatar.url) if m.display_avatar else None}
            for m in guild.members if not m.bot
        ][:100]  # Limit to 100 for performance

        # Music state
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

        return await render_template_string(
            GUILD_TEMPLATE,
            guild=guild,
            modules=modules,
            log_channel=log_channel,
            filtered_users=filtered_users,
            text_channels=text_channels,
            members=members,
            music_state=music_state,
            settings=settings,
        )

    # ──── API Routes ────

    @app.route("/api/guild/<int:guild_id>/module", methods=["POST"])
    async def toggle_module(guild_id):
        data = await request.get_json()
        module_name = data.get("module")
        enabled = data.get("enabled", True)
        await Config.set_guild_setting(guild_id, f"module_{module_name}_enabled", enabled)
        return jsonify({"success": True, "module": module_name, "enabled": enabled})

    @app.route("/api/guild/<int:guild_id>/log/channel", methods=["POST"])
    async def set_log_channel(guild_id):
        data = await request.get_json()
        channel_id = data.get("channel_id")
        await Config.set_guild_setting(guild_id, "log_channel_id", channel_id)
        return jsonify({"success": True})

    @app.route("/api/guild/<int:guild_id>/log/filter", methods=["POST"])
    async def update_log_filter(guild_id):
        data = await request.get_json()
        action = data.get("action")  # "add" or "remove"
        user_id = data.get("user_id")

        filters = await Config.get_guild_setting(guild_id, "log_filtered_users", [])

        if action == "add" and user_id not in filters:
            filters.append(user_id)
        elif action == "remove" and user_id in filters:
            filters.remove(user_id)
        elif action == "clear":
            filters = []

        await Config.set_guild_setting(guild_id, "log_filtered_users", filters)
        return jsonify({"success": True, "filters": filters})

    @app.route("/api/guild/<int:guild_id>/settings", methods=["POST"])
    async def update_settings(guild_id):
        data = await request.get_json()
        key = data.get("key")
        value = data.get("value")
        await Config.set_guild_setting(guild_id, key, value)
        return jsonify({"success": True})

    @app.route("/api/guild/<int:guild_id>/music/skip", methods=["POST"])
    async def music_skip(guild_id):
        music_cog = bot.get_cog("Music")
        if not music_cog:
            return jsonify({"error": "Music module not loaded"}), 400
        state = music_cog.get_state(guild_id)
        if state.voice_client and state.is_playing:
            state._skip_flag = True
            state.voice_client.stop()
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
            if hasattr(state.voice_client.source, 'volume'):
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
            return jsonify({"success": True, "paused": True})
        elif state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
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

    return app


# ──── HTML Templates ────

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Discord Bot Dashboard</title>
    <style>
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --accent: #e94560;
            --accent-hover: #ff6b81;
            --text-primary: #eee;
            --text-secondary: #aaa;
            --success: #2ecc71;
            --warning: #f39c12;
            --border: rgba(255,255,255,0.1);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }
        .header {
            background: var(--bg-secondary);
            padding: 20px 40px;
            border-bottom: 2px solid var(--accent);
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .header h1 { font-size: 24px; }
        .header .badge {
            background: var(--accent);
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }
        .container {
            max-width: 1200px;
            margin: 40px auto;
            padding: 0 20px;
        }
        .section-title {
            font-size: 20px;
            margin-bottom: 20px;
            color: var(--text-secondary);
        }
        .guild-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 20px;
        }
        .guild-card {
            background: var(--bg-card);
            border-radius: 12px;
            padding: 24px;
            cursor: pointer;
            transition: all 0.2s;
            border: 1px solid var(--border);
            text-decoration: none;
            color: inherit;
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .guild-card:hover {
            transform: translateY(-2px);
            border-color: var(--accent);
            box-shadow: 0 8px 24px rgba(233, 69, 96, 0.15);
        }
        .guild-icon {
            width: 56px; height: 56px;
            border-radius: 50%;
            background: var(--accent);
            display: flex; align-items: center; justify-content: center;
            font-size: 24px; font-weight: bold;
            flex-shrink: 0;
        }
        .guild-icon img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; }
        .guild-info h3 { font-size: 18px; margin-bottom: 4px; }
        .guild-info p { font-size: 14px; color: var(--text-secondary); }
    </style>
</head>
<body>
    <div class="header">
        <h1>{{ bot_name }}</h1>
        <span class="badge">DASHBOARD</span>
    </div>
    <div class="container">
        <h2 class="section-title">Your Servers</h2>
        <div class="guild-grid">
            {% for guild in guilds %}
            <a href="/guild/{{ guild.id }}" class="guild-card">
                <div class="guild-icon">
                    {% if guild.icon %}
                    <img src="{{ guild.icon }}" alt="{{ guild.name }}">
                    {% else %}
                    {{ guild.name[0] }}
                    {% endif %}
                </div>
                <div class="guild-info">
                    <h3>{{ guild.name }}</h3>
                    <p>{{ guild.member_count }} members</p>
                </div>
            </a>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

GUILD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ guild.name }} — Dashboard</title>
    <style>
        :root {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-card: #0f3460;
            --accent: #e94560;
            --accent-hover: #ff6b81;
            --text-primary: #eee;
            --text-secondary: #aaa;
            --success: #2ecc71;
            --warning: #f39c12;
            --danger: #e74c3c;
            --border: rgba(255,255,255,0.1);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }
        .header {
            background: var(--bg-secondary);
            padding: 16px 40px;
            border-bottom: 2px solid var(--accent);
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .header a { color: var(--text-secondary); text-decoration: none; font-size: 14px; }
        .header a:hover { color: var(--accent); }
        .header h1 { font-size: 22px; flex: 1; }
        .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }

        .tabs {
            display: flex; gap: 0; margin-bottom: 30px;
            border-bottom: 2px solid var(--border);
        }
        .tab {
            padding: 12px 24px;
            cursor: pointer;
            border: none;
            background: none;
            color: var(--text-secondary);
            font-size: 15px;
            font-weight: 500;
            border-bottom: 2px solid transparent;
            margin-bottom: -2px;
            transition: all 0.2s;
        }
        .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
        .tab:hover { color: var(--text-primary); }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        .card {
            background: var(--bg-card);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid var(--border);
        }
        .card h3 {
            font-size: 17px;
            margin-bottom: 16px;
            display: flex; align-items: center; gap: 8px;
        }

        .toggle-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid var(--border);
        }
        .toggle-row:last-child { border-bottom: none; }
        .toggle-label { font-size: 15px; }
        .toggle-desc { font-size: 13px; color: var(--text-secondary); }

        .switch {
            position: relative; width: 48px; height: 26px;
        }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute; inset: 0;
            background: #555;
            border-radius: 13px;
            cursor: pointer;
            transition: 0.3s;
        }
        .slider:before {
            content: "";
            position: absolute;
            width: 20px; height: 20px;
            left: 3px; bottom: 3px;
            background: white;
            border-radius: 50%;
            transition: 0.3s;
        }
        .switch input:checked + .slider { background: var(--success); }
        .switch input:checked + .slider:before { transform: translateX(22px); }

        select, input[type="text"], input[type="number"] {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            color: var(--text-primary);
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
            outline: none;
        }
        select:focus, input:focus { border-color: var(--accent); }

        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn-primary { background: var(--accent); color: white; }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-success { background: var(--success); color: white; }
        .btn-danger { background: var(--danger); color: white; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }

        .inline-form { display: flex; gap: 8px; align-items: center; margin-top: 12px; }

        .tag {
            display: inline-flex; align-items: center; gap: 6px;
            background: var(--bg-secondary);
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 13px;
            margin: 4px;
        }
        .tag .remove {
            cursor: pointer; color: var(--danger);
            font-weight: bold; font-size: 16px;
        }

        /* Music Player UI */
        .player {
            display: flex; align-items: center; gap: 16px;
            padding: 16px;
            background: var(--bg-secondary);
            border-radius: 8px;
            margin-bottom: 16px;
        }
        .player-thumb {
            width: 64px; height: 64px;
            border-radius: 8px;
            background: #333;
            flex-shrink: 0;
        }
        .player-thumb img { width: 100%; height: 100%; object-fit: cover; border-radius: 8px; }
        .player-info { flex: 1; }
        .player-info h4 { font-size: 15px; margin-bottom: 4px; }
        .player-info p { font-size: 13px; color: var(--text-secondary); }
        .player-controls { display: flex; gap: 8px; align-items: center; }
        .player-btn {
            width: 36px; height: 36px;
            border-radius: 50%;
            border: none;
            background: var(--accent);
            color: white;
            cursor: pointer;
            font-size: 16px;
            display: flex; align-items: center; justify-content: center;
            transition: 0.2s;
        }
        .player-btn:hover { background: var(--accent-hover); transform: scale(1.1); }

        .volume-slider { width: 120px; accent-color: var(--accent); }

        .queue-item {
            display: flex; align-items: center; gap: 12px;
            padding: 8px 0;
            border-bottom: 1px solid var(--border);
            font-size: 14px;
        }
        .queue-item:last-child { border: none; }
        .queue-num { color: var(--text-secondary); width: 24px; text-align: center; }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        .empty-state .icon { font-size: 40px; margin-bottom: 12px; }

        .toast {
            position: fixed; bottom: 20px; right: 20px;
            padding: 12px 20px;
            background: var(--success);
            color: white;
            border-radius: 8px;
            font-size: 14px;
            opacity: 0;
            transform: translateY(10px);
            transition: all 0.3s;
            z-index: 1000;
        }
        .toast.show { opacity: 1; transform: translateY(0); }
        .toast.error { background: var(--danger); }
    </style>
</head>
<body>
    <div class="header">
        <a href="/">← Back</a>
        <h1>{{ guild.name }}</h1>
    </div>

    <div class="container">
        <div class="tabs">
            <button class="tab active" onclick="switchTab('modules')">Modules</button>
            <button class="tab" onclick="switchTab('logger')">Logger</button>
            <button class="tab" onclick="switchTab('music')">Music</button>
            <button class="tab" onclick="switchTab('settings')">Settings</button>
        </div>

        <!-- MODULES TAB -->
        <div id="tab-modules" class="tab-content active">
            <div class="card">
                <h3>🧩 Modules</h3>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">🎵 Music Player</div>
                        <div class="toggle-desc">Play YouTube and local audio in voice channels</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" {{ "checked" if modules.music else "" }}
                               onchange="toggleModule('music', this.checked)">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">📋 Server Logger</div>
                        <div class="toggle-desc">Track server events and log them to a channel</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" {{ "checked" if modules.logger else "" }}
                               onchange="toggleModule('logger', this.checked)">
                        <span class="slider"></span>
                    </label>
                </div>
            </div>
        </div>

        <!-- LOGGER TAB -->
        <div id="tab-logger" class="tab-content">
            <div class="card">
                <h3>📋 Log Channel</h3>
                <div class="inline-form">
                    <select id="log-channel-select">
                        <option value="">-- Select Channel --</option>
                        {% for ch in text_channels %}
                        <option value="{{ ch.id }}" {{ "selected" if log_channel and log_channel.id == ch.id else "" }}>
                            #{{ ch.name }}
                        </option>
                        {% endfor %}
                    </select>
                    <button class="btn btn-primary" onclick="setLogChannel()">Save</button>
                    <button class="btn btn-danger" onclick="disableLog()">Disable</button>
                </div>
            </div>

            <div class="card">
                <h3>👤 User Filter</h3>
                <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 12px;">
                    Filter logs to specific users. If empty, all users are logged.
                </p>

                <div id="filter-tags">
                    {% for uid in filtered_users %}
                    <span class="tag" data-user-id="{{ uid }}">
                        User #{{ uid }}
                        <span class="remove" onclick="removeFilter({{ uid }})">&times;</span>
                    </span>
                    {% endfor %}
                </div>

                <div class="inline-form">
                    <select id="filter-user-select">
                        <option value="">-- Add User --</option>
                        {% for m in members %}
                        <option value="{{ m.id }}">{{ m.name }}</option>
                        {% endfor %}
                    </select>
                    <button class="btn btn-primary" onclick="addFilter()">Add</button>
                    <button class="btn btn-danger btn-sm" onclick="clearFilters()">Clear All</button>
                </div>
            </div>
        </div>

        <!-- MUSIC TAB -->
        <div id="tab-music" class="tab-content">
            {% if music_state %}
            <div class="card">
                <h3>🎵 Now Playing</h3>
                {% if music_state.current %}
                <div class="player">
                    <div class="player-thumb">
                        {% if music_state.current.thumbnail %}
                        <img src="{{ music_state.current.thumbnail }}" alt="thumb">
                        {% endif %}
                    </div>
                    <div class="player-info">
                        <h4 id="np-title">{{ music_state.current.title }}</h4>
                        <p>Requested by {{ music_state.current.requester }}</p>
                    </div>
                    <div class="player-controls">
                        <button class="player-btn" onclick="musicAction('pause')" title="Pause/Resume">⏯</button>
                        <button class="player-btn" onclick="musicAction('skip')" title="Skip">⏭</button>
                        <button class="player-btn" onclick="musicAction('stop')" title="Stop">⏹</button>
                    </div>
                </div>
                {% else %}
                <div class="empty-state">
                    <div class="icon">🎵</div>
                    <p>Nothing is playing right now</p>
                </div>
                {% endif %}

                <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px;">
                    <span style="font-size: 13px;">🔊 Volume:</span>
                    <input type="range" class="volume-slider" min="0" max="200"
                           value="{{ music_state.volume }}" id="volume-slider"
                           oninput="document.getElementById('vol-val').textContent = this.value + '%'"
                           onchange="setVolume(this.value)">
                    <span id="vol-val" style="font-size: 13px; width: 40px;">{{ music_state.volume }}%</span>
                </div>

                <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px;">
                    <span style="font-size: 13px;">🔁 Loop:</span>
                    <select onchange="setLoop(this.value)" style="font-size: 13px;">
                        <option value="off" {{ "selected" if music_state.loop_mode == "off" else "" }}>Off</option>
                        <option value="single" {{ "selected" if music_state.loop_mode == "single" else "" }}>Single</option>
                        <option value="queue" {{ "selected" if music_state.loop_mode == "queue" else "" }}>Queue</option>
                    </select>
                </div>
            </div>

            <div class="card">
                <h3>📜 Queue ({{ music_state.queue_length }} tracks)</h3>
                {% if music_state.queue %}
                {% for song in music_state.queue %}
                <div class="queue-item">
                    <span class="queue-num">{{ loop.index }}</span>
                    <span style="flex:1">{{ song.title }}</span>
                    <span style="color: var(--text-secondary); font-size: 12px;">{{ song.requester }}</span>
                </div>
                {% endfor %}
                {% else %}
                <div class="empty-state">
                    <p>Queue is empty</p>
                </div>
                {% endif %}
            </div>
            {% else %}
            <div class="card">
                <div class="empty-state">
                    <div class="icon">🎵</div>
                    <p>Music module not loaded</p>
                </div>
            </div>
            {% endif %}
        </div>

        <!-- SETTINGS TAB -->
        <div id="tab-settings" class="tab-content">
            <div class="card">
                <h3>⚙️ All Settings</h3>
                {% for key, value in settings.items() %}
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">{{ key }}</div>
                        <div class="toggle-desc">{{ value }}</div>
                    </div>
                </div>
                {% endfor %}
                {% if not settings %}
                <div class="empty-state">
                    <p>No custom settings configured</p>
                </div>
                {% endif %}
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        const guildId = {{ guild.id }};

        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
        }

        function showToast(msg, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast show' + (isError ? ' error' : '');
            setTimeout(() => toast.className = 'toast', 3000);
        }

        async function api(path, data = null) {
            const opts = data ? {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            } : {};
            const res = await fetch('/api/guild/' + guildId + path, opts);
            return res.json();
        }

        async function toggleModule(name, enabled) {
            await api('/module', {module: name, enabled});
            showToast(name + ' ' + (enabled ? 'enabled' : 'disabled'));
        }

        async function setLogChannel() {
            const chId = document.getElementById('log-channel-select').value;
            if (!chId) return showToast('Select a channel first', true);
            await api('/log/channel', {channel_id: parseInt(chId)});
            showToast('Log channel updated');
        }

        async function disableLog() {
            await api('/log/channel', {channel_id: null});
            showToast('Logging disabled');
        }

        async function addFilter() {
            const userId = document.getElementById('filter-user-select').value;
            if (!userId) return;
            const res = await api('/log/filter', {action: 'add', user_id: parseInt(userId)});
            if (res.success) {
                showToast('Filter added');
                location.reload();
            }
        }

        async function removeFilter(userId) {
            await api('/log/filter', {action: 'remove', user_id: userId});
            showToast('Filter removed');
            location.reload();
        }

        async function clearFilters() {
            await api('/log/filter', {action: 'clear'});
            showToast('All filters cleared');
            location.reload();
        }

        async function musicAction(action) {
            await api('/music/' + action, {});
            showToast(action.charAt(0).toUpperCase() + action.slice(1) + ' done');
            setTimeout(() => location.reload(), 500);
        }

        async function setVolume(level) {
            await api('/music/volume', {volume: parseInt(level)});
        }

        async function setLoop(mode) {
            await api('/music/loop', {mode});
            showToast('Loop: ' + mode);
        }

        // Auto-refresh music state every 10s
        {% if music_state %}
        setInterval(async () => {
            try {
                const state = await api('/music/state');
                if (state.current) {
                    const el = document.getElementById('np-title');
                    if (el) el.textContent = state.current.title;
                }
            } catch(e) {}
        }, 10000);
        {% endif %}
    </script>
</body>
</html>
"""
