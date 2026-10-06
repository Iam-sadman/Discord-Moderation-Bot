# Discord Bot — Master Architecture & Documentation Index

This document is the central architectural index and operational blueprint for the modular Discord bot and its integrated web administration dashboard.

---

## 1. Quick Documentation Navigation

| Component | Path | Key Capabilities |
| :--- | :--- | :--- |
| **Settings Cog** | [docs/cogs/settings.md](file:///d:/Projects/DiscordBot/docs/cogs/settings.md) | In-Discord server settings, prefix management, module toggles, extension hot-reload, and instant command sync. |
| **Announcement Cog** | [docs/cogs/announcement.md](file:///d:/Projects/DiscordBot/docs/cogs/announcement.md) | Modal-based announcement composer, interactive live preview, formatting options, media attachments, and role whitelisting. |
| **Labeler Tracker Cog** | [docs/cogs/labeler_tracker.md](file:///d:/Projects/DiscordBot/docs/cogs/labeler_tracker.md) | Real-time ARC telemetry polling from Google Apps Script, MD5 change detection, and paginated leaderboard board. |
| **Leave of Absence (LOA)** | [docs/cogs/loa.md](file:///d:/Projects/DiscordBot/docs/cogs/loa.md) | Member leave applications, supervisor review flow, pinned dashboard, midnight auto-expiration, and CSV export. |
| **Server Audit Logger** | [docs/cogs/logger.md](file:///d:/Projects/DiscordBot/docs/cogs/logger.md) | Gateway event logging (voice, roles, channels, messages), message delete recovery cache, SQLite WAL pipeline, and member history. |
| **Unified Music System** | [docs/cogs/music.md](file:///d:/Projects/DiscordBot/docs/cogs/music.md) | YouTube playback, interactive search, server music folder, personal/public libraries, and direct stream uploads. |
| **Web Dashboard** | [docs/dashboard/web_dashboard.md](file:///d:/Projects/DiscordBot/docs/dashboard/web_dashboard.md) | Quart web app, Excel user registry (`dashboard_users.xlsx`), hardware/MAC tracking, remote music control, and live audit feed. |

---

## 2. Complete Project Directory Layout

```
d:/Projects/DiscordBot/
├── bot.py                     # Bot entrypoint, setup_hook, cog loader, and Quart background task
├── config.py                  # Environment configuration, SQLite settings.db helpers, and RBAC logic
├── standalone_scraper.py      # Standalone testing utility for ARC telemetry scraper
│
├── cogs/                      # Discord Bot Extension Modules
│   ├── settings.py            # Prefix commands (!settings, !prefix, !cogs, !sync, !reload)
│   ├── announcement.py        # Slash command /announcement with modal composer & interactive preview
│   ├── labeler_tracker.py     # ARC team telemetry scraper, background loop & live dashboard
│   ├── loa.py                 # Leave of Absence (LOA) system, review cards & midnight expiration
│   ├── logger.py              # Server audit logger, message caching & event listeners
│   ├── music.py               # Unified music player (YouTube, local, user libraries, uploads)
│   ├── directstream.py        # [Legacy] Preserved direct upload player (superseded by music.py)
│   ├── localmusic.py          # [Legacy] Preserved local folder player (superseded by music.py)
│   └── usermusic.py           # [Legacy] Preserved user library player (superseded by music.py)
│
├── dashboard/                 # Quart Web Management Dashboard
│   ├── app.py                 # Quart application factory, routing & REST APIs
│   ├── auth.py                # Excel authentication manager (data/dashboard_users.xlsx)
│   ├── security.py            # Hardware MAC resolution, IP extraction & audit logging
│   ├── server_logs.py         # Asynchronous provider querying data/logs.db
│   ├── cogs_catalog.py        # Live metadata introspection for bot cogs & commands
│   └── templates/             # HTML5 Jinja Templates
│       ├── base.html          # Core layout & navigation
│       ├── login.html         # Authentication interface
│       ├── index.html         # Server list & guild picker
│       └── guild.html         # Server management panel (toggles, RBAC, music, logs, composer)
│
├── services/                  # Business Logic Services
│   └── labeler_scraper.py     # Async HTTP scraper for Google Apps Script ARC telemetry
│
├── utils/                     # Shared Utilities
│   ├── embed_builder.py       # Standardized Discord embed color schemes & formatting
│   └── arc_test_fetch.py      # Low-level diagnostic script for raw GAS callback payloads
│
├── data/                      # Persistent Application Storage (Ignored by Git)
│   ├── settings.db            # SQLite: guild configurations, prefixes, and dashboard access logs
│   ├── logs.db                # SQLite: server audit history (WAL mode)
│   ├── loa.db                 # SQLite: LOA settings and historical request records
│   ├── dashboard_users.xlsx   # Excel: authorized web dashboard accounts & access keys
│   ├── arc_live_state.json    # JSON: pinned live message ID and payload hash for ARC dashboard
│   └── bot.log                # Primary application runtime log file
│
├── music/                     # Audio File Directories
│   ├── public/                # Public community audio files
│   └── users/                 # Personal user audio folders (<user_id>/)
│
└── docs/                      # Central Documentation Hub
    ├── INDEX.md               # This architecture overview and navigation index
    ├── cogs/                  # Detailed documentation for each cog
    └── dashboard/             # Detailed documentation for the web dashboard
```

---

## 3. Inter-File Connection Matrix

The table below maps which files depend on and connect to other files across the repository:

```
┌─────────────────────────────────┬────────────────────────────────────────────────────────┐
│ Source File                     │ Directly Connects To / Calls                          │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ bot.py                          │ • config.py (Config, credentials, prefix, init_db)     │
│                                 │ • cogs/*.py (Dynamically loads all active cogs)        │
│                                 │ • dashboard/app.py (create_dashboard -> run_task)      │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ config.py                       │ • data/settings.db (aiosqlite guild_settings table)   │
│                                 │ • .env (Loads environment tokens & configurations)     │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/settings.py                │ • config.py (Prefix, modules, RBAC, settings.db)       │
│                                 │ • cogs/loa.py (Queries Loa.get_settings)               │
│                                 │ • bot.py (Hot-reloads extensions, syncs tree)          │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/announcement.py            │ • config.py (Announcement roles, RBAC, module toggle)  │
│                                 │ • data/settings.db (Stores allowed role whitelist)     │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/labeler_tracker.py         │ • services/labeler_scraper.py (get_live_arc_telemetry) │
│                                 │ • data/arc_live_state.json (Persistence state)         │
│                                 │ • config.py (Scrape interval & permissions)            │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/loa.py                     │ • data/loa.db (aiosqlite loa_settings & requests)      │
│                                 │ • config.py (Timezone offset & permissions)            │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/logger.py                  │ • data/logs.db (aiosqlite audit_history WAL pipeline)  │
│                                 │ • utils/embed_builder.py (Standardized colors)         │
│                                 │ • config.py (log_channel_id from settings.db)          │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ cogs/music.py                   │ • music/ (Local, public, and user audio files)         │
│                                 │ • temp_music/ (Direct-stream uploads)                  │
│                                 │ • dashboard/app.py (Remote state & playback controls)  │
├─────────────────────────────────┼────────────────────────────────────────────────────────┤
│ dashboard/app.py                │ • bot.py (Direct bot instance for guild inspection)    │
│                                 │ • dashboard/auth.py (Excel user authentication)        │
│                                 │ • dashboard/security.py (IP/MAC/device audit logging)  │
│                                 │ • dashboard/server_logs.py (Reads data/logs.db)        │
│                                 │ • dashboard/cogs_catalog.py (Command introspection)    │
│                                 │ • config.py (Reads & writes guild settings)            │
│                                 │ • cogs/music.py (Remote player control)                │
└─────────────────────────────────┴────────────────────────────────────────────────────────┘
```

---

## 4. Lifecycle & Startup Execution Order

1. **Environment Setup & Logging**:
   - `bot.py` reconfigures standard I/O streams to UTF-8.
   - Dual logging is established: console output + `data/bot.log`.
2. **Database Initialization**:
   - `bot.setup_hook()` calls `Config.init_db()` to ensure `data/settings.db` exists.
   - `cogs/loa.py` initializes `data/loa.db` (`loa_settings`, `loa_requests`).
   - `cogs/logger.py` initializes `data/logs.db` (`audit_history` in WAL mode).
   - `dashboard/security.py` initializes `dashboard_access_logs` in `data/settings.db`.
   - `dashboard/auth.py` generates `data/dashboard_users.xlsx` if not present.
3. **Cog Loading**:
   - `bot.py` loops through `cogs/`, skipping legacy modules (`directstream`, `localmusic`, `usermusic`).
   - Loads: `settings`, `announcement`, `labeler_tracker`, `loa`, `logger`, `music`.
4. **Command Tree Synchronization**:
   - Global slash commands are synced (`bot.tree.sync()`).
   - In `on_ready()`, commands are copied and synced directly to each guild (`bot.tree.copy_global_to(guild=guild)`).
5. **Web Dashboard Launch**:
   - In `main()`, `dashboard = create_dashboard(bot)` is initialized.
   - Spawned as an asynchronous background task on `asyncio.ensure_future(run_dashboard())` bound to `Config.WEB_HOST` and `Config.WEB_PORT` (default: `0.0.0.0:5000`).
6. **Gateway Connection**:
   - `bot.start(Config.TOKEN)` connects to Discord Gateway with all intents enabled.

---

## 5. Databases & Persistent Files Summary

### 1. `data/settings.db` (SQLite)
- **`guild_settings`**: Guild prefixes, enabled modules (`music`, `logger`, `loa`, `announcement`), announcement role IDs, and logger channel bindings.
- **`dashboard_access_logs`**: Security telemetry of all dashboard logins, IP addresses, MAC addresses, device User-Agents, and settings modifications.

### 2. `data/logs.db` (SQLite - WAL Mode)
- **`audit_history`**: High-frequency stream of server events (voice state updates, role changes, channel edits, member timeouts/nicknames, and message edits/deletions).

### 3. `data/loa.db` (SQLite)
- **`loa_settings`**: Channel and role configurations for the Leave of Absence system.
- **`loa_requests`**: Complete historical database of all member leave applications, approval/denial timestamps, checker IDs, and extension records.

### 4. `data/dashboard_users.xlsx` (Excel Workbook)
- Off-Discord user registry for the web management panel with User ID, Access Key, Full Name, Role (`Administrator`, `Moderator`, `Viewer`), and Status (`Active`).

### 5. `data/arc_live_state.json` (JSON)
- Preserves the Discord channel ID, pinned message ID, and MD5 telemetry hash for the ARC live tracker.
