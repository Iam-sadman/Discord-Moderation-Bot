# 🤖 Discord Modular Operations Bot

A robust, enterprise-grade, multi-purpose Discord bot built with **discord.py 2.x**, featuring a fully asynchronous architecture, persistent SQLite storage, dynamic per-server configuration via Discord slash commands, and a modular system design.

---

## 🌟 Key Features & Modules

### 📢 Announcement System
- **Interactive Modal Composer:** Compose clean announcements with custom headers, bodies, footers, custom links, and image attachments.
- **Private Interactive Preview:** Real-time preview with action buttons to edit text, change formatting styles (bold/italic/underline), select destination channels, and pick notification roles/users.
- **In-Discord Role Permissions:** Manage which server roles can create announcements with `/announcement_roles add|remove|list`. Server Administrators automatically have full access. No `.env` restarts required!

### 🌴 Leave of Absence (LOA) System
- **Self-Service Application Flow:** Interactive modal for team members to submit leave requests (dates, reason, team name).
- **Two-Tier Review Pipeline:** Staff/Checkers review requests in a private review channel with one-click Approve / Deny buttons (with denial reason prompts).
- **Live Telemetry Dashboard:** Auto-updating, pinned embed displaying active leaves with paginated navigation and status badges.
- **Dynamic Timezone Configuration:** Configure UTC timezone offset per server using `/loa_config timezone_offset` without editing configuration files.
- **Reporting & Early End:** Members can end active leaves early with `/loa_end`, and Checkers/Admins can generate CSV audit reports with `/loa_report`.

### 🛡️ Comprehensive Server Logger
- **Automated Event Auditing:** Tracks message edits/deletions, member joins/leaves/bans, role updates, nickname changes, and voice channel activity.
- **Member History & Search:** Search any user's audit log history over the last 90 days with `/log history` or `/logsearch`.
- **Easy In-Discord Setup:** Link the server logging channel dynamically with `/log setup <#channel>`.

### 🎵 High-Performance Music System
- **Stream Support:** Powered by `yt-dlp` and `PyNaCl` for audio streaming with queueing, pause/resume, skip, and volume control.
- **Auto-Disconnect & Cleanup:** Automated idle voice disconnection and cache management.

### ⚙️ Dynamic Server Settings & Module Management
- **In-Discord Configuration:** Change bot prefixes, toggle entire modules on or off, and view all server configurations with `/settings view`.
- **Per-Guild Command Prefix:** Change prefix per guild with `/settings prefix <new_prefix>` (default: `!`).
- **Secret Separation:** Sensitive tokens and secrets stay strictly isolated in `.env`, while all operational settings are managed inside Discord.

---

## 📂 Project Architecture

```
DiscordBot/
├── bot.py                  # Main entry point: Bot initialization & dynamic prefix loader
├── config.py               # Core config manager & SQLite guild_settings data access layer
├── requirements.txt        # Production dependencies
├── .env.example            # Environment variables template (Secrets only)
├── .gitignore              # Configured for privacy (secrets, caches, internal scrapers excluded)
├── INSTALL.md              # Ubuntu VPS deployment guide with systemd & PM2
├── README.md               # Project documentation & command reference
│
├── cogs/                   # Modular feature extensions (Cogs)
│   ├── announcement.py     # Modal composer, preview workflow & role access management
│   ├── loa.py              # Leave of Absence tracking, review pipeline & dashboard
│   ├── logger.py           # Server audit logger & member log search
│   ├── music.py            # Audio streaming & queue management
│   └── settings.py         # Dynamic server settings, prefix setter & module toggles
│
├── dashboard/              # Async Web Management Dashboard
│   └── app.py              # Quart web application backend
│
├── data/                   # Persistent runtime databases (SQLite)
│   ├── settings.db         # Server-specific configurations & module toggles
│   ├── loa.db              # LOA applications, reviews & status state
│   └── logs.db             # Audit logs storage
│
└── utils/                  # Shared helper functions and UI utilities
    └── embed_builder.py    # Common Discord Embed generators
```

---

## 🚀 Getting Started

### Prerequisites
- Python **3.11** or higher
- [FFmpeg](https://ffmpeg.org/) installed and available in system PATH (required for music streaming)
- A Discord Bot Application & Token from the [Discord Developer Portal](https://discord.com/developers/applications)

### Installation

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd DiscordBot
   ```

2. **Create and activate a virtual environment:**
   - **Linux / macOS:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - **Windows:**
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your credentials:
   ```dotenv
   DISCORD_TOKEN=your_bot_token_here
   WEB_SECRET_KEY=generate_a_random_secret_string
   WEB_DASHBOARD_PORT=5000
   WEB_DASHBOARD_HOST=0.0.0.0
   ```
   > 🔒 **Security Notice:** Only sensitive secrets (bot token, OAuth2 secrets, web secret) belong in `.env`. Operational settings like prefixes, roles, and channels are managed entirely through Discord slash commands!

5. **Start the bot:**
   ```bash
   python bot.py
   ```

---

## 📜 Discord Slash Commands Reference

### ⚙️ Server Configuration (`/settings` & `/module`)
| Command | Permission | Description |
|---|---|---|
| `/settings view` | Administrator | View all current settings, prefixes, roles, and module states |
| `/settings prefix <char>` | Administrator | Change the bot command prefix for this server |
| `/settings set <key> <val>` | Administrator | Set an advanced custom guild setting |
| `/settings reset <key>` | Administrator | Reset a guild setting to default |
| `/module list` | Administrator | List all bot modules and their active/disabled status |
| `/module enable <name>` | Administrator | Enable a module (Music, Logger, LOA, Announcement) |
| `/module disable <name>` | Administrator | Disable a module for this server |

### 📢 Announcements (`/announcement` & `/announcement_roles`)
| Command | Permission | Description |
|---|---|---|
| `/announcement` | Allowed Roles / Admin | Open the guided rich announcement modal & interactive preview |
| `/announcement_roles add <role>` | Administrator | Grant an announcement creation permission to a role |
| `/announcement_roles remove <role>` | Administrator | Remove announcement creation permission from a role |
| `/announcement_roles list` | Administrator | List all roles permitted to create announcements |

### 🌴 Leave of Absence (`/loa_*`)
| Command | Permission | Description |
|---|---|---|
| `/loa_setup` | Administrator | Initial setup for public dashboard channel, review channel & roles |
| `/loa_config` | Administrator | Update review channels, roles, or server timezone (`timezone_offset`) |
| `/loa_status` | Everyone | View your active and past LOA requests |
| `/loa_end` | Everyone | Return to duty early and close your active approved leave |
| `/loa_report` | Checkers / Admin | Export date-wise LOA history with optional CSV download |

### 🛡️ Audit Logger (`/log*`)
| Command | Permission | Description |
|---|---|---|
| `/log setup <#channel>` | Manage Server | Bind a text channel to receive server audit log feeds |
| `/log history <user> [days]`| Manage Server | View recent infractions and logged events for a member |
| `/logsearch <user> [days]` | Manage Server | Search specific member action history in logs |

---

## 🛡️ Security & Privacy Architecture

- **Private Scraper Isolation:** All internal scraping engines, network telemetry captures, and reverse engineering scratch artifacts are strictly excluded via `.gitignore`.
- **Graceful Fallbacks:** Cogs safely detect missing optional services without throwing unhandled exceptions or halting execution.
- **Admin Privilege Safeguard:** Server Administrators always retain access to setup and configuration commands even prior to role definitions.
