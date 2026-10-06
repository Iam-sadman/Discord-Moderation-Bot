# Server Audit Logger Cog (`cogs/logger.py`)

## 1. Overview & Purpose
The `Logger` cog ([cogs/logger.py](file:///d:/Projects/DiscordBot/cogs/logger.py)) provides enterprise server audit logging, security tracking, and member activity telemetry. It listens to gateway events, caches deleted message content, resolves audit log actors, writes records to an asynchronous SQLite WAL pipeline, broadcasts real-time color-coded embeds to a designated channel, and provides searchable in-Discord member history.

---

## 2. Connected Files & Architecture

```mermaid
graph TD
    Gateway["Discord Gateway Events"] -->|Voice, Members, Roles, Channels, Messages| Listeners["Event Listeners (cogs/logger.py)"]
    Listeners -->|Cache messages| MsgCache["MessageCache (In-Memory)"]
    Listeners -->|Enqueue log record| WriteQueue["Async Queue & Batch Writer"]
    WriteQueue -->|Persist (WAL mode)| LogsDB["data/logs.db (table: audit_history)"]
    WriteQueue -->|Format Embed| EmbedBuilder["utils/embed_builder.py"]
    EmbedBuilder -->|Send Embed| LogChannel["Configured Discord Log Channel"]
    Dashboard["dashboard/app.py & server_logs.py"] -->|Read-only queries| LogsDB
    ConfigPy["config.py"] -->|Stores log_channel_id| SettingsDB["data/settings.db"]
```

| Connected File | Relationship & Usage |
| :--- | :--- |
| [bot.py](file:///d:/Projects/DiscordBot/bot.py) | Loaded dynamically into bot runtime. |
| [utils/embed_builder.py](file:///d:/Projects/DiscordBot/utils/embed_builder.py) | Standardizes embed colors and formats for each distinct log action type. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Reads and saves `log_channel_id` in `data/settings.db`. |
| [dashboard/server_logs.py](file:///d:/Projects/DiscordBot/dashboard/server_logs.py) | Reads from `data/logs.db` to feed the web dashboard's real-time audit logs viewer. |
| [data/logs.db](file:///d:/Projects/DiscordBot/data/logs.db) | Dedicated high-throughput SQLite database in WAL mode. |

---

## 3. Database Schema & Pipeline (`data/logs.db`)

### Configuration & WAL Mode:
To support concurrent reads from the web dashboard while the Discord bot continuously writes event streams, `logs.db` runs with:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

### Table: `audit_history`
```sql
CREATE TABLE IF NOT EXISTS audit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER,                      -- Target member ID
    actor_id INTEGER,                     -- Responsible moderator/actor ID
    action_type TEXT NOT NULL,            -- e.g., 'message_delete', 'voice_join', 'role_update'
    details TEXT,                         -- Detailed change description
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Index Optimization:
- `idx_audit_history_guild_created ON audit_history(guild_id, created_at)`
- `idx_audit_history_guild_user ON audit_history(guild_id, user_id, created_at)`
- `idx_audit_history_guild_actor ON audit_history(guild_id, actor_id, created_at)`
- `idx_audit_history_guild_action ON audit_history(guild_id, action_type, created_at)`

---

## 4. Tracked Events & Listeners

| Gateway Listener | Captured Actions & Telemetry |
| :--- | :--- |
| `on_voice_state_update` | Voice join, leave, move between channels, server mute/deafen, self mute/deafen, screenshare stream start/stop, video camera on/off. |
| `on_member_update` | Nickname changes (before/after), roles added or removed, communication timeouts (duration & reason). |
| `on_guild_role_create` | Role creation, default color, permissions granted, actor identification from audit log. |
| `on_guild_role_delete` | Role removal and actor identification. |
| `on_guild_role_update` | Name changes, color adjustments, hoisted status, permission changes diff. |
| `on_guild_channel_update` | Channel name, topic, slowmode, bitrate, and NSFW flag changes. |
| `on_message` | Buffers messages into in-memory LRU `MessageCache` to support message delete auditing. |
| `on_message_delete` | Deleted message text, attachments, channel, author, and actor (moderator vs self). |
| `on_message_edit` | Edited messages with before/after comparison diff and jump links. |

---

## 5. In-Memory Message Caching (`MessageCache`)
Discord's API only provides deleted message contents if the message was cached in the client's memory. The cog maintains a custom `MessageCache` with an in-memory ring buffer:
- Stores message author, content, channel, and attachments.
- Enables complete recovery of deleted content for audit embeds even when standard bots log `[Message not cached]`.

---

## 6. Commands Reference

### `/log setup <channel>`
- **Permission**: Manage Server (`manage_guild=True`) or Server Administrators.
- **Arguments**:
  - `channel`: The text channel where live embeds should be delivered.
- **Functionality**: Saves `log_channel_id` to `data/settings.db` and immediately activates real-time event forwarding.

### `/log history <user> [days]`
- **Permission**: Manage Server.
- **Arguments**:
  - `user`: Target member.
  - `days` *(Optional, 1–90, default: 7)*: Lookback duration.
- **Functionality**: Fetches all logged activities involving the member (as target or actor) and renders an interactive paginated view (`HistoryPaginationView`).

### `/logsearch <user> [days]`
- **Permission**: Manage Server.
- **Functionality**: Quick alias for member log search with indexed SQL queries.
