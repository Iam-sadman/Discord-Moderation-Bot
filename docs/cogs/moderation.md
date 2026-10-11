# Moderation & Role Management Cog (`cogs/moderation.py`)

## 1. Overview & Purpose
The `Moderation` cog ([cogs/moderation.py](file:///d:/Projects/DiscordBot/cogs/moderation.py)) and its backing services provide enterprise-grade server moderation, persistent role assignment panels, and bulk moderation. It is fully integrated with the web management dashboard and enforces strict Discord role hierarchy, rate limiting, and durable scheduling across bot restarts.

---

## 2. Connected Files & Architecture

| Connected File | Relationship & Usage |
| :--- | :--- |
| [bot.py](file:///d:/Projects/DiscordBot/bot.py) | Automatically loads `cogs.moderation` in `setup_hook` and syncs slash commands (`/mod status`). |
| [services/moderation_engine.py](file:///d:/Projects/DiscordBot/services/moderation_engine.py) | Moderation execution engine handling hierarchy checks, sequential token-bucket rate limiting (1.2s delay), safe pre-action DMs, temporary ban scheduling, and bulk workers. |
| [services/moderation_db.py](file:///d:/Projects/DiscordBot/services/moderation_db.py) | Independent SQLite WAL database service managing `data/moderation.db`. Completely isolated from `data/logs.db`. |
| [dashboard/moderation_api.py](file:///d:/Projects/DiscordBot/dashboard/moderation_api.py) | Quart REST API Blueprint providing 20+ endpoints for role panels, individual actions, bulk jobs, and audit logs. |
| [dashboard/templates/guild.html](file:///d:/Projects/DiscordBot/dashboard/templates/guild.html) | Web frontend rendering 6 dedicated tabs: Role Catalog, Role Panels, Moderation Hub, Bulk Moderation, Audit Log, and Config. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Enforces RBAC cog permissions and module toggles for Moderation. |

---

## 3. Storage & Schema
All records are durably saved inside `data/moderation.db` using SQLite WAL mode with busy timeout handling:

- `role_panels`: Embed metadata, target channel, published message ID, and status.
- `role_panel_items`: Individual buttons or reaction bindings linked to role IDs with toggle rules.
- `allowed_self_roles`: Whitelist of roles permitted for self-assignment via public panels.
- `message_templates`: Customizable DM notice templates with placeholder sanitization.
- `moderation_actions`: History of all individual actions with idempotency keys.
- `scheduled_actions`: Durable temporary ban records reconciled by an asynchronous watchdog.
- `bulk_jobs` & `bulk_job_targets`: Tracked mass moderation batches with status, filters, and target results.
- `moderation_audit_records`: Queryable, paginated audit log with outcomes and reason details.
- `dashboard_action_permissions`: Granular capability matrix for dashboard roles (Viewer, Moderator, Admin).
- `guild_emergency_controls`: Circuit breaker flag halting moderation in emergency lockouts.

---

## 4. Permission Model & Hierarchy
All actions are validated server-side:

1. **Role Hierarchy**:
   - The bot can never target members whose top role is higher than or equal to the bot's highest role.
   - The bot can never assign or remove roles higher than or equal to its own top role.
2. **Protected Entities**:
   - The Server Owner can never be targeted for moderation.
   - The Bot itself can never be targeted.
   - Server Owner, Bots, and Administrators are strictly excluded from bulk operations.
3. **Emergency Circuit Breaker**:
   - When a server is emergency-locked via dashboard or API, all mutating actions are immediately halted.

---

## 5. In-Discord Commands

### `/mod status`
- **Permissions**: `manage_guild`
- **Description**: Displays the active operational status of the moderation system:
  - Active published role panels count
  - Whitelisted self-assignable roles count
  - Pending scheduled actions (tempbans)
  - Emergency circuit breaker lock state
  - Active rate limit delays

---

## 6. Web Dashboard Features

1. **Roles Catalog & Member Inspector**:
   - Live badge list of server roles with position and manageability indicators.
   - Searchable, paginated member inspector modal displaying user roles.
2. **Role Panel Builder**:
   - Visual embed builder with color picker, image URL, and thumbnail.
   - Add/remove buttons or reactions with custom labels, emojis, and toggle settings.
   - Publish directly to any selected Discord text channel.
3. **Moderation Hub**:
   - Perform Timeout, Kick, Ban, Tempban, or Unban on any member.
   - Configurable duration presets (5m, 1h, 1d, 1w) or custom datepicker for tempbans.
   - Optional pre-action direct message notification.
4. **Bulk Moderation**:
   - Filter members by role, server join date, account creation age, or username pattern.
   - Live dry-run preview displaying matched count vs. protected count.
   - High-risk destructive actions (bulk kick/ban) require typing exact confirmation phrases (`CONFIRM KICK` / `CONFIRM BAN`).
   - Real-time job progress bar with on-demand cancellation.
5. **Moderation Audit Log**:
   - Filterable by action type and status (SUCCESS / FAILED).
   - Shows actor attribution, target details, timestamp, and DM delivery status.
6. **Moderation Settings**:
   - Granular capability checkboxes per dashboard role.
   - Customizable DM notification templates with `{user}`, `{server}`, `{action}`, `{reason}`, and `{duration}` placeholders.
   - Emergency circuit breaker toggle switch.
