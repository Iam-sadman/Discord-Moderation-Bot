# Settings Cog (`cogs/settings.py`)

## 1. Overview & Purpose
The `Settings` cog ([cogs/settings.py](file:///d:/Projects/DiscordBot/cogs/settings.py)) provides server administrators with in-Discord control over the bot's runtime behavior, configuration, module toggles, and slash command synchronization. It operates primarily through traditional prefix commands (`!command`) to ensure admins can configure the bot even when slash commands have not yet synced or registered in Discord's gateway.

---

## 2. Connected Files & Architecture

| Connected File | Relationship & Usage |
| :--- | :--- |
| [bot.py](file:///d:/Projects/DiscordBot/bot.py) | Loaded dynamically in `setup_hook` during bot startup. Uses `get_prefix` dynamically resolved from configuration. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Interacts with `Config.get_prefix`, `Config.set_prefix`, `Config.is_module_enabled`, `Config.set_module_enabled`, `Config.get_guild_setting`, `Config.set_guild_setting`, `Config.delete_guild_setting`, and `Config.check_member_cog_permission`. |
| [cogs/loa.py](file:///d:/Projects/DiscordBot/cogs/loa.py) | Safely queries `Loa.get_settings(guild_id)` to display current LOA dashboard channel, review channel, and timezone in `!settings`. |
| [cogs/logger.py](file:///d:/Projects/DiscordBot/cogs/logger.py) | Queries configured logger channel (`log_channel_id`) from `Config.get_guild_setting`. |
| [dashboard/app.py](file:///d:/Projects/DiscordBot/dashboard/app.py) | Reflects and updates the exact same SQLite key-value settings in `data/settings.db` that this cog controls. |

---

## 3. Storage & Schema
Configuration values are persisted inside `data/settings.db` via `Config` in the `guild_settings` table:

```sql
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (guild_id, key)
);
```

### Key Settings Managed:
- `prefix`: Per-guild custom prefix string (defaults to `!`).
- `module_music_enabled`: Boolean flag toggling the Music module.
- `module_logger_enabled`: Boolean flag toggling the Logger module.
- `module_loa_enabled`: Boolean flag toggling the LOA module.
- `module_announcement_enabled`: Boolean flag toggling the Announcement module.
- `announcement_roles`: List of integer role IDs permitted to create announcements.
- `log_channel_id`: Integer channel ID designated for server audit logs.
- `loa_timezone_offset`: Timezone offset in hours (default: `6` for UTC+6).
- Arbitrary key-value pairs stored via `!set <key> <value>`.

---

## 4. Permission Model & Access Control
All commands inside `Settings` are restricted to:
1. **Discord Server Administrators** (`guild_permissions.administrator`).
2. **Server Owner** (`ctx.author.id == ctx.guild.owner_id`).
3. **Bot Owner** (`ctx.author.id == ctx.bot.owner_id`).
4. **RBAC Cog Check**: Enforces `Config.check_member_cog_permission(ctx.author, "settings")`.

---

## 5. Command Reference

### `!settings` / `!settings view`
- **Description**: Displays a comprehensive server configuration summary embed.
- **Fields Displayed**:
  - Command Prefix
  - Module Statuses (`music`, `logger`, `loa`, `announcement` with 🟢/🔴 badges)
  - Announcement Allowed Roles
  - Server Logger Channel
  - LOA System status (dashboard channel, review channel, timezone)
  - Custom key-value pairs stored in the database
- **Fallback**: Automatically falls back to formatted plain text if embed sending fails or permissions are restricted.

### `!prefix [new_prefix]`
- **Description**: View or change the server-specific bot prefix.
- **Arguments**:
  - `new_prefix` *(Optional)*: New prefix string (1 to 5 characters).
- **Behavior**: If omitted, outputs current prefix and instructions.

### `!modules` / `!module list`
- **Description**: Displays the enabled/disabled status of all 4 core bot modules (`music`, `logger`, `loa`, `announcement`).

### `!module enable <module_name>`
- **Description**: Enables a specific module for the guild.
- **Allowed values**: `music`, `logger`, `loa`, `announcement`.

### `!module disable <module_name>`
- **Description**: Disables a specific module for the guild.
- **Allowed values**: `music`, `logger`, `loa`, `announcement`.

### `!cogs`
- **Description**: Displays all currently loaded extensions/cogs inside the bot runtime (`bot.extensions.keys()`).
- **Telemetry**: Logs diagnostic output to console and sends an interactive status embed.

### `!reload <cog_name>`
- **Description**: Performs on-the-fly hot reloading of a cog without taking down the Discord bot or dashboard process.
- **Syntax**: `!reload settings` or `!reload music` (automatically prepends `cogs.` if not present).

### `!sync`
- **Description**: Instantly copies and syncs all global application slash commands (`bot.tree.copy_global_to`) to the current guild (`bot.tree.sync`).
- **Use Case**: Eliminates Discord's 1-hour global slash command registration delay when updating commands in production.

### `!set <key> <value>`
- **Description**: Directly writes an arbitrary key-value configuration into `data/settings.db` for custom extension use.

### `!reset <key>`
- **Description**: Removes an arbitrary configuration key from `data/settings.db`, reverting it to default.
