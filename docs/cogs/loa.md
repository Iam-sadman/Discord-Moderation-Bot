# Leave of Absence Cog (`cogs/loa.py`)

## 1. Overview & Purpose
The `Loa` cog ([cogs/loa.py](file:///d:/Projects/DiscordBot/cogs/loa.py)) is an enterprise-grade staff Leave of Absence (LOA) management system. It provides self-service leave applications via Discord modals, review workflows for supervisors/checkers, an interactive pinned dashboard, early return and extension capabilities, automatic expiration scheduling, and CSV audit exports.

---

## 2. Connected Files & Architecture

```mermaid
graph TD
    Member["Team Member"] -->|Clicks 'Apply for LOA' or /loa_status| LoaModal["LoaApplyModal / LoaExtendModal"]
    LoaModal -->|Inserts pending request| LoaDB["data/loa.db"]
    LoaDB -->|Posts Review Card| ReviewChan["Review Channel (Checkers)"]
    ReviewChan -->|Approve / Reject Modal| ReviewFlow["LoaReviewView"]
    ReviewFlow -->|Updates status to approved/rejected| LoaDB
    LoaDB -->|Refreshes Live Board| DashChan["Dashboard Channel (Pinned Embed)"]
    MidnightTask["Midnight Cron Task (check_expired_loas_loop)"] -->|Expires outdated LOAs| LoaDB
    SettingsCog["cogs/settings.py"] -->|Queries Loa.get_settings()| LoaDB
    ConfigPy["config.py"] -->|Provides RBAC checks & timezone| LoaDB
```

| Connected File | Relationship & Usage |
| :--- | :--- |
| [bot.py](file:///d:/Projects/DiscordBot/bot.py) | Loaded dynamically into bot runtime. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Supplies `Config.get_loa_timezone(guild_id)` and RBAC checks via `Config.check_member_cog_permission`. |
| [cogs/settings.py](file:///d:/Projects/DiscordBot/cogs/settings.py) | Displays current LOA channels and timezone offset in `!settings`. |
| [dashboard/app.py](file:///d:/Projects/DiscordBot/dashboard/app.py) | Updates LOA configuration via `/api/guild/<guild_id>/loa_config`. |
| [data/loa.db](file:///d:/Projects/DiscordBot/data/loa.db) | Dedicated SQLite database for storing LOA settings and historical request records. |

---

## 3. Database Schema (`data/loa.db`)

### Table: `loa_settings`
Stores guild-specific channel bindings and role assignments:
```sql
CREATE TABLE IF NOT EXISTS loa_settings (
    guild_id INTEGER PRIMARY KEY,
    dashboard_channel_id INTEGER,
    dashboard_message_id INTEGER,
    review_channel_id INTEGER,
    labeler_role_id INTEGER,
    checker_role_id INTEGER,
    timezone_offset INTEGER DEFAULT 6,
    report_role_id INTEGER
);
```

### Table: `loa_requests`
Maintains the complete audit trail of all leave requests:
```sql
CREATE TABLE IF NOT EXISTS loa_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    team_name TEXT NOT NULL,          -- Stores Site Name (e.g. ARC_abcdefg)
    reason TEXT NOT NULL,
    start_date TEXT NOT NULL,         -- 'YYYY-MM-DD'
    end_date TEXT NOT NULL,           -- 'YYYY-MM-DD'
    status TEXT NOT NULL,             -- 'pending', 'approved', 'rejected', 'expired', 'ended_early', 'pending_extension'
    applied_at TIMESTAMP NOT NULL,
    reviewed_by INTEGER,
    reviewed_by_name TEXT,
    reviewed_at TIMESTAMP,
    rejection_reason TEXT,
    approval_comment TEXT,
    review_channel_msg_id INTEGER,
    ended_early_at TIMESTAMP,
    extension_of_id INTEGER
);
```

---

## 4. Workflows & State Lifecycle

```
[New Application]
       │
       ▼
  (pending) ───[Rejected by Checker]───► (rejected)
       │
       ▼ [Approved by Checker]
  (approved)
       ├───► [End Early by Member] ─────► (ended_early)
       ├───► [Extended by Member] ──────► (pending_extension) ──► (approved)
       └───► [Midnight Expiration] ────► (expired)
```

### 1. Application Flow:
- Member clicks **"📝 Apply for LOA"** on the dashboard or uses slash command.
- Pop-up modal (`LoaApplyModal`) collects:
  - **Site Name** (e.g. `ARC_abcdefg`)
  - **Start Date** (`YYYY-MM-DD`)
  - **End Date** (`YYYY-MM-DD`)
  - **Reason for Leave**
- Applicant's identity is automatically obtained from their Discord profile.
- System validates dates (end date after start date) and checks for active overlapping leaves.
- Posts review card into the designated **Review Channel**.

### 2. Checker Review Flow:
- Checkers / Admins see the review card with buttons (`LoaReviewView`):
  - **`✅ Approve`**: Launches `LoaApproveModal` to optionally record an approval comment/note for the applicant. Updates status to `approved`, edits the review card with decision & optional note, sends an approval DM to the applicant, and refreshes the live dashboard.
  - **`❌ Deny`**: Launches `LoaRejectModal` to capture mandatory feedback, marks the record `rejected`, sends a denial DM with the reason to the applicant, and edits the review card.

### 3. Extension & Early Return Flows:
- **Extension (`LoaExtendModal`)**: Allows an approved member to request an extended end date. Posts an extension review card (`LoaExtensionReviewView`) for checkers with optional approval comment support.
- **End Early (`LoaEndEarlyConfirmView`)**: Allows a member to terminate leave ahead of schedule.

### 4. Midnight Expiration Scheduler:
- Background task `check_expired_loas_loop` runs daily at **12:00:05 AM** in the configured server timezone (default: UTC+6).
- Automatically marks leaves whose `end_date < today` as `expired` and refreshes the pinned dashboard.

---

## 5. Slash Commands Reference

### `/loa_setup`
- **Permission**: Server Administrators (`default_permissions(administrator=True)`).
- **Parameters**:
  - `dashboard_channel`: Channel where the public pinned dashboard will be maintained.
  - `review_channel`: Private channel where checkers review incoming requests.
  - `checker_role`: Role allowed to approve/deny requests.
  - `labeler_role` *(Optional)*: Member role.
  - `report_role` *(Optional)*: Role permitted to download CSV reports.
  - `timezone_offset` *(Optional)*: UTC hour offset (e.g. `6` for Bangladesh UTC+6, `0` for UTC).

### `/loa_config`
- **Permission**: Server Administrators.
- **Functionality**: Modifies channels, roles, or timezone without recreating the pinned dashboard embed.

### `/loa_status`
- **Permission**: Everyone.
- **Functionality**: Shows the member's current active LOA, pending requests, and complete past history with action buttons to extend or end early.

### `/loa_end`
- **Permission**: Everyone.
- **Functionality**: Initiates the early return workflow for the calling user's active leave.

### `/loa_report`
- **Permission**: Checkers, Report Role, or Server Administrators.
- **Parameters**:
  - `start_date`: Start of report range (`YYYY-MM-DD`).
  - `end_date`: End of report range (`YYYY-MM-DD`).
  - `status_filter` *(Optional)*: Filter by `all`, `approved`, `pending`, `rejected`, `expired`, or `ended_early`.
- **Output**: Detailed Discord summary embed plus an attached UTF-8 `.csv` spreadsheet file.
