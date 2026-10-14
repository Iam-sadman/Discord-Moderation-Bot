# Forms Cog (`cogs/forms.py`)

## 1. Overview & Purpose
The `FormsCog` ([cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py)) and its backing services provide a production-ready, dynamic **Form Builder & Submission Management System** for the Discord bot and web management dashboard. It enables server administrators to construct customized application forms, handle multi-page Discord modals, run scheduled application windows with live countdown timestamps (`<t:TIMESTAMP:R>`), conduct review workflows (Approve, Reject, DM), and export submission data to UTF-8 BOM CSV or formatted Microsoft Excel (`.xlsx`) files.

---

## 2. Connected Files & Architecture

| Connected File | Relationship & Usage |
| :--- | :--- |
| [bot.py](file:///d:/Projects/DiscordBot/bot.py) | Automatically loads `cogs.forms` in `setup_hook` and synchronizes `/form` slash commands globally and to guilds. |
| [services/forms_db.py](file:///d:/Projects/DiscordBot/services/forms_db.py) | Shared SQLite WAL database service managing `data/forms.db` with connection pooling, 5000ms busy timeout, and anti-IDOR guild scoping. |
| [services/forms_exporter.py](file:///d:/Projects/DiscordBot/services/forms_exporter.py) | Generates in-memory CSV (`utf-8-sig`) and Excel (`.xlsx` via `openpyxl`) files with formula injection protection (CWE-1236) and dynamic headers. |
| [dashboard/forms_api.py](file:///d:/Projects/DiscordBot/dashboard/forms_api.py) | Quart REST API Blueprint providing endpoints for forms CRUD, question reordering, submissions review, and live panel publishing. |
| [dashboard/templates/guild.html](file:///d:/Projects/DiscordBot/dashboard/templates/guild.html) | Interactive web UI tab with form cards, question builder with reordering, submission inspection modal, review actions, and export buttons. |
| [config.py](file:///d:/Projects/DiscordBot/config.py) | Stores `FORMS_DB_PATH` and `INTERNAL_SECRET` configuration constants. |
| [tests/test_forms_system.py](file:///d:/Projects/DiscordBot/tests/test_forms_system.py) | Comprehensive 14-test integration test suite covering database operations, multi-page modals, exports, and security constraints. |

---

## 3. Storage & Schema (`data/forms.db`)
All records are saved inside `data/forms.db` using SQLite WAL mode with foreign key cascade integrity:

- `forms`: Master table storing form title, description, destination channel ID, public panel channel/message IDs, review toggles, approved role ID, timing boundaries (`opens_at`, `closes_at`), max submissions per user, and cooldown settings.
- `form_questions`: Custom questions linked to a form with label (max 45 chars), style (`short` or `paragraph`), placeholder hint (max 100 chars), required flag, character length limits (capped at 1000), and display position.
- `form_submissions`: Recorded user submissions containing submitter ID and avatar, review status (`submitted`, `pending`, `approved`, `rejected`), JSON response payload, destination message ID, and reviewer feedback.

---

## 4. Modal Limit Handling & Multi-Page Chaining
Discord modals strictly enforce a maximum of 5 `TextInput` components per modal:
- **1–5 Questions**: Opened directly in a single `FormPageModal` and submitted in one step.
- **6+ Questions**: Automatically sliced into 5-question pages. Completing Page 1 stores answers in an in-memory draft cache (`self.drafts`) and presents an ephemeral view with:
  - `[Continue to Next Page ➡️]`: Opens the next chunk of questions in a subsequent modal.
  - `[🔄 Start Over]`: Discards the current draft and resets progress.
- **Draft Cleanup**: An asynchronous `@tasks.loop(seconds=30)` task automatically purges drafts older than 15 minutes to prevent memory leaks on the VPS.

---

## 5. Security & Guardrails

1. **Anti-IDOR Guild Isolation**: All database operations and API endpoints require matching `guild_id` parameters.
2. **Formula Injection Sanitization (CWE-1236)**: Any cell beginning with `=`, `+`, `-`, `@`, `\t`, or `\r` is automatically prefixed with a single quote (`'`) to prevent formula execution in Excel or Google Sheets.
3. **Role Hierarchy & Escalation Guard**: Role assignment on approval is only permitted if the target role is strictly lower than both the configuring user's top role and the bot's top role, and possesses neither `administrator` nor `manage_guild` permissions.
4. **Discord DynamicItem Persistence**: Public panel buttons and submission review buttons use `discord.ui.DynamicItem` with regex templates (`form:open:<form_id>` and `form:review:<action>:<sub_id>`), operating permanently across bot and server restarts without re-instantiating message views.

---

## 6. Discord Slash Commands (`/form`)

| Command | Permission | Description |
| :--- | :--- | :--- |
| `/form create` | `Manage Server` | Creates a new custom form with destination channel, review toggles, and limits. |
| `/form list` | `Manage Server` | Displays an overview embed of all forms created on the server. |
| `/form publish` | `Manage Server` | Deploys or updates the public interactive form panel to a target channel. |
| `/form schedule` | `Manage Server` | Configures relative duration (e.g. `5h`, `2d`) or manual Unix opening/closing timestamps. |
| `/form close` | `Manage Server` | Manually closes an active form and disables submission buttons. |
| `/form reopen` | `Manage Server` | Reopens a closed form. |
| `/form export` | `Manage Server` | Exports form submissions to CSV (`utf-8-sig`) or Excel (`.xlsx`) directly to Discord. |
| `/form question add` | `Manage Server` | Adds a custom question with label, style, limits, and placeholder. |
| `/form question list` | `Manage Server` | Lists all questions and positions for a form. |
| `/form question delete` | `Manage Server` | Deletes a question and normalizes remaining positions. |
