# Labeler Tracker Cog (`cogs/labeler_tracker.py`)

## 1. Overview & Purpose
The `LabelerTracker` cog ([cogs/labeler_tracker.py](file:///d:/Projects/DiscordBot/cogs/labeler_tracker.py)) provides automated, real-time telemetry polling and an interactive leaderboard dashboard for the ARC annotation and labeling team. It continuously scrapes Google Apps Script (GAS) web application feeds, aggregates performance metrics (tasks completed, work hours, comments, resubmissions, pace), and maintains a live paginated status board in Discord with change detection.

---

## 2. Connected Files & Architecture

```mermaid
graph TD
    GAS["Google Apps Script Web App<br/>(exec / callback endpoint)"] -->|Async HTTP POST / httpx| Scraper["services/labeler_scraper.py"]
    Scraper -->|get_live_arc_telemetry()| Cog["cogs/labeler_tracker.py"]
    Cog -->|Persist state| StateJSON["data/arc_live_state.json"]
    Cog -->|Check permissions| ConfigPy["config.py"]
    Cog -->|Post & Update Dashboard| DiscordChannel["Designated Discord Channel"]
    Standalone["standalone_scraper.py"] -->|CLI Test Script| Scraper
```

| Connected File | Relationship & Usage |
| :--- | :--- |
| [services/labeler_scraper.py](file:///d:/Projects/DiscordBot/services/labeler_scraper.py) | Pure async HTTP engine using `httpx`. Connects to Google Apps Script, strips XSSI protection, decodes `op.exec` payloads, filters `ARC_*` members, and calculates team KPIs. |
| [standalone_scraper.py](file:///d:/Projects/DiscordBot/standalone_scraper.py) | Independent command-line diagnostic script for verifying sheet connectivity and record mapping outside Discord. |
| [utils/arc_test_fetch.py](file:///d:/Projects/DiscordBot/utils/arc_test_fetch.py) | Low-level test utility for verifying raw GAS callback payloads. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Supplies `SCRAPE_INTERVAL` (default: 300s) and RBAC permission checks via `Config.check_member_cog_permission`. |
| [data/arc_live_state.json](file:///d:/Projects/DiscordBot/data/arc_live_state.json) | Local persistence storage tracking target channel ID, live message ID, and payload hash. |

---

## 3. Google Apps Script Data Mapping
The scraper connects to:
`https://script.google.com/macros/s/AKfycbwcETeVpiC_308tb3R7J6vtd4ftYuL6JN_wxldVIURBdcSFBG7HbpFtYmev909WrPq_/callback`

### Record Field Layout:
| Column Index | Variable | Description |
| :---: | :--- | :--- |
| `a[0]` | `hourIdx` | Hour index mapping into the sheet's `hours[]` array |
| `a[1]` | `owner` | Labeler full name (e.g. `ARC_Abdullah Al Mamun`) |
| `a[2]` | `proj` | Project ID (e.g. `5566`) |
| `a[3]` | `role` | Role (e.g. `labeler`) |
| `a[4]` | `op` | Task operation name |
| `a[5]` | `state` | Stage/state flag |
| `a[6]` | `wt` | Work duration in hours (decimal float) |
| `a[7]` | `tn` | Submitted task count (`task_num`) |
| `a[8]` | `fe` | Features count |
| `a[9]` | `pr` | Predicted features count |
| `a[10]` | `cm` | Comments count |
| `a[11]` | `area` | Task area / geometry metric |
| `a[12]` | `sub` | Submission attempts (Resubmissions = `max(0, sub - tn)`) |

---

## 4. Background Polling & Change Detection
The cog runs an automated background task `arc_live_loop`:
1. **Interval**: Executes every `Config.SCRAPE_INTERVAL` seconds (default: 300s / 5 minutes).
2. **MD5 Hash Comparison**: The cog generates an MD5 hash of the normalized telemetry payload.
   ```python
   def compute_data_hash(self, telemetry: Dict[str, Any]) -> str:
       serialized = json.dumps(telemetry, sort_keys=True, default=str)
       return hashlib.md5(serialized.encode("utf-8")).hexdigest()
   ```
3. **Rate-Limit Prevention**: If `current_hash == self.last_payload_hash`, the loop skips editing Discord messages, preventing unnecessary API calls.
4. **Self-Healing Messages**: If the pinned message was deleted (`discord.NotFound`), the cog automatically recreates it and updates `data/arc_live_state.json`.

---

## 5. User Interface (`LabelerDashboardView`)
The live dashboard is an interactive Discord view:
- **Row 0: Telemetry Badges** (Disabled Discord buttons for clean metric indicators):
  - `⚡ Active Now (N)`: Number of labelers with submitted tasks in the current active hour.
  - `👥 Active Today (N)`: Total distinct labelers who logged work today.
  - `🕒 <Hour Label>`: Timestamp label of the latest sheet cycle.
- **Row 1: Pagination & Controls**:
  - `◀️ Previous`: Navigate to previous page of labelers.
  - `Page X of Y`: Current pagination indicator.
  - `Next ▶️`: Navigate to next page.
  - `🔄 Refresh`: Triggers an immediate scrape and message update (restricted to Administrators).

---

## 6. Slash Commands Reference

### `/arc_setup [channel]`
- **Permission**: Server Administrators (`default_permissions(administrator=True)`).
- **Arguments**:
  - `channel` *(Optional)*: Target Discord text channel (defaults to current channel).
- **Functionality**: Binds the automated live dashboard to the channel, immediately fetches live data, creates the pinned message, and persists state in `data/arc_live_state.json`.

### `/arc`
- **Permission**: Server Administrators.
- **Functionality**: Generates an instant, private ephemeral snapshot embed of ARC metrics without altering the persistent dashboard channel.

### `/arc_status`
- **Permission**: Server Administrators.
- **Functionality**: Displays health diagnostics, including:
  - Bound Channel ID & Live Message ID
  - Polling interval (seconds and minutes)
  - Last scrape timestamp (UTC)
  - Total active labelers today and tasks submitted
