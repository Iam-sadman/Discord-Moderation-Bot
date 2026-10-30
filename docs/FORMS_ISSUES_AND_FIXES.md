# Forms System: Issues, Root Causes, and Architectural Fixes
> **Incident & Troubleshooting Reference Document**  
> *Last Updated: 2026-10-31*

---

## 1. Executive Summary & Context

Server administrators reported that after building a custom form via the Web Dashboard and publishing it to Discord:
1. **Form Edit Issue:** The form and its questions could not be edited or updated.
2. **Premature Cooldown:** On clicking the "📝 Fill Form" button for the first time, the bot responded with:  
   `⏳ Please wait 30s before opening this form again.`
3. **Gateway Timeout:** After waiting for the cooldown and trying again, Discord reported:  
   `ARC Bot didn't respond in time` (Interaction Failed).
4. **Concurrency Concern:** What happens when multiple members attempt to open and submit the form simultaneously?

This document outlines the root cause analysis, architectural fixes applied across the codebase, concurrency safeguards, and a troubleshooting checklist for future reference.

---

## 2. Issues Breakdown & Root Cause Analysis

### Issue 1: Unable to Edit Form or Questions ("Edit kora jacche na")

#### Root Causes:
1. **Missing Question Edit UI in Web Dashboard:**
   - In [dashboard/templates/guild.html](file:///d:/Projects/DiscordBot/dashboard/templates/guild.html), the question manager only rendered `▲ Move Up`, `▼ Move Down`, and `🗑️ Delete` buttons.
   - There was **no edit button or modal** for existing questions. Users with typos or needing to modify character limits had to delete and re-add the question.
2. **Duplicate Published Panels Instead of In-Place Edit:**
   - When administrators clicked "Sync Panel" after modifying form settings, the backend endpoint `/api/guild/<id>/forms/<id>/publish` in [dashboard/forms_api.py](file:///d:/Projects/DiscordBot/dashboard/forms_api.py) sent a **brand-new Discord message** (`channel.send()`) rather than editing the existing message in-place (`message.edit()`).
   - The original published message remained abandoned and outdated in the Discord channel.
3. **Null-Value Type Errors in Question APIs:**
   - When inputs like `min_length` or `max_length` were blank, JavaScript serialized them as `null` (`{"min_length": null}`). In Python, calling `int(data.get("min_length", 0))` evaluated `int(None)`, throwing an unhandled `TypeError`.

#### Applied Fixes:
- **Added Edit Question Modal & Actions:**
  - Added an `✏️` Edit button to each question card in `guild.html`.
  - Added modal [`modal-edit-question`](file:///d:/Projects/DiscordBot/dashboard/templates/guild.html#L2752-L2805) with JavaScript handlers `openEditQuestionModal()`, `closeEditQuestionModal()`, and `handleEditQuestionSubmit()`.
- **In-Place Discord Panel Update:**
  - Updated `api_publish_form` in `dashboard/forms_api.py`: if `panel_message_id` and `panel_channel_id` are already set, it attempts `await existing_msg.edit(embed=embed, view=view)` before creating a new message.
- **Safe Integer Casting:**
  - Changed API parsing to `int(data.get("min_length") or 0)` and `int(data.get("max_length") or 1000)`.

---

### Issue 2: Premature "Please wait 30s" Warning on Open

#### Root Causes:
- In [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py), cooldown tracking was recorded during **Form Open** rather than on **Form Submission**:
  ```python
  # Old flawed logic in handle_form_open_interaction:
  self.cooldowns[cd_key] = now  # Recorded BEFORE user fills or submits anything!
  ```
- **Consequences:**
  1. A user clicking the button was immediately placed on a 30s or 60s cooldown.
  2. If a user double-clicked (due to network latency) or closed the modal by mistake, their next click triggered:  
     `⏳ Please wait 30s before opening this form again.`
  3. The cooldown was never recorded in `finalize_submission`, meaning actual submissions had no rate limiting.

#### Applied Fixes:
- **Decoupled Debounce from Submission Cooldown:**
  1. **Open Debounce (3 Seconds):** Added `self.open_debounce[(form_id, user.id)] = now`. If a user clicks rapidly within 3 seconds, the duplicate click is ignored safely.
  2. **Submission Cooldown:** `self.cooldowns[(form_id, user.id)] = now` is recorded **only** in `finalize_submission` when the form is fully submitted.
  3. Opening the form for the first time will **never** trigger a cooldown message.

---

### Issue 3: "ARC Bot Didn't Respond in Time" (Discord 3-Second Timeout)

Discord Gateway enforces a strict **3.0-second timeout** for interaction responses. If the bot does not send an initial HTTP response within 3 seconds, Discord displays:  
`ARC Bot didn't respond in time` (or `This interaction failed`).

#### Root Causes:
1. **`TypeError` in `min_length` / `max_length`:**
   - In `FormPageModal.__init__`:
     ```python
     # If min_length in sqlite was NULL, q.get("min_length", 0) returned None:
     min_l = max(0, min(q.get("min_length", 0), 1000))  # Python throws: TypeError: '<' not supported between instances of 'int' and 'NoneType'
     ```
   - The modal constructor crashed before `await interaction.response.send_modal(modal)` was called.
2. **Discord 45-Character Modal Title Overflow:**
   - Discord API returns `400 Bad Request: In title: Must be 45 or fewer in length` if modal title exceeds 45 characters.
   - For multi-page chained forms (6+ questions), the code computed:  
     `f"{form['title'][:35]} (Page {page}/{total_pages})"` → `35 + len(" (Page 1/2)")` = **46 characters** (exceeds 45).
   - Discord rejected the request, throwing `discord.HTTPException`.
3. **Unhandled Exceptions in DynamicItem Callbacks:**
   - `DynamicFormOpenButton.callback` had no `try...except` wrapper.
   - Any exception resulted in an unacknowledged interaction, leading to a Discord gateway timeout.
4. **Synchronous Database Bottlenecks:**
   - Every button click opened three separate SQLite connections (`get_form_any`, `count_user_submissions`, `get_questions`), each running 4 PRAGMA queries. If a write lock occurred, the database wait breached the 3-second window.

#### Applied Fixes:
- **Safe NoneType Handling:**
  ```python
  raw_min = q.get("min_length")
  raw_max = q.get("max_length")
  min_l = max(0, min(int(raw_min if raw_min is not None else 0), 1000))
  max_l = max(1, min(int(raw_max if raw_max is not None else 1000), 1000))
  ```
- **Strict 45-Character Title Clamping:**
  ```python
  raw_title = str(form.get("title") or "Server Form")
  if total_pages > 1:
      suffix = f" (P.{page}/{total_pages})"
      max_prefix = max(10, 45 - len(suffix))
      modal_title = f"{raw_title[:max_prefix]}{suffix}"
  else:
      modal_title = raw_title[:45]
  super().__init__(title=modal_title[:45])
  ```
- **Global `try...except` in Interaction Callbacks:**
  - Wrapped `DynamicFormOpenButton.callback`, `handle_form_open_interaction`, `FormPageModal.on_submit`, and `FormMultiPageContinueView.continue_page` in `try...except`.
  - On error, an ephemeral message is sent to the user so Discord never times out.
- **High-Concurrency In-Memory Form Cache:**
  - Implemented `self._form_cache: Dict[int, Dict[str, Any]]` in `FormsCog`.
  - Button clicks fetch form metadata and questions from memory (<1ms) instead of repeatedly hitting SQLite.

---

### Issue 4: Concurrency & Simultaneous Submissions ("Multiple Person Submit")

#### Concurrency Assessment:
- **Draft Isolation:** Each in-memory draft is keyed by `draft_key = f"{form_id}:{user.id}"`. Users cannot overwrite or corrupt each other's multi-page drafts.
- **Lock Free Form Opens:** With `_form_cache`, 50+ members clicking "Fill Form" simultaneously are served from memory in <1ms without database read locks.
- **Submission Deferral:** In `finalize_submission`, `await interaction.response.defer(ephemeral=True)` is called immediately. This grants the bot up to **15 minutes** to write to SQLite, post to destination channels, and respect Discord channel rate limits (5 messages / 5s).
- **Auto-Increment Isolation:** SQLite WAL mode safely handles concurrent write transactions without ID collision.

---

## 3. Quick Reference: Key Code Locations

| Component | File Path | Key Functions / Classes |
| :--- | :--- | :--- |
| **Persistent Open Button** | [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py) | `DynamicFormOpenButton`, `callback()` |
| **Modal Construction** | [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py) | `FormPageModal`, `on_submit()` |
| **Multi-Page Pagination** | [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py) | `FormMultiPageContinueView`, `continue_page()` |
| **Form Open & Cache** | [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py) | `handle_form_open_interaction()`, `_form_cache`, `open_debounce` |
| **Final Submission** | [cogs/forms.py](file:///d:/Projects/DiscordBot/cogs/forms.py) | `finalize_submission()`, `self.cooldowns` |
| **Dashboard Forms API** | [dashboard/forms_api.py](file:///d:/Projects/DiscordBot/dashboard/forms_api.py) | `api_publish_form()`, `api_update_question()`, `invalidate_cache()` |
| **Web Dashboard UI** | [dashboard/templates/guild.html](file:///d:/Projects/DiscordBot/dashboard/templates/guild.html) | `renderQuestionsList()`, `openEditQuestionModal()`, `handleEditQuestionSubmit()` |
| **Database Service** | [services/forms_db.py](file:///d:/Projects/DiscordBot/services/forms_db.py) | `FormsDB`, `get_connection()`, `update_question()` |

---

## 4. Troubleshooting Checklist for Future Incidents

When diagnosing future issues with the Form Builder system, follow these steps:

- [ ] **Check Bot Logs for Interaction Errors:**  
  Inspect `data/bot.log` for `[ERROR]` or `Traceback` lines matching `FormsCog` or `DynamicFormOpenButton`.
- [ ] **Verify Discord Modal Limits:**  
  - Is `title` length `<= 45` characters?
  - Is `label` non-empty and `<= 45` characters?
  - Is `placeholder` `<= 100` characters?
  - Does the modal contain `<= 5` text inputs per page?
- [ ] **Verify Dynamic Items Registration:**  
  Confirm `self.bot.add_dynamic_items(DynamicFormOpenButton, DynamicFormReviewButton)` ran in `cog_load()`.
- [ ] **Verify Database Locks & Permissions:**  
  Ensure `data/forms.db` is writable and WAL mode is active (`PRAGMA journal_mode=WAL`).
- [ ] **Check Cache Invalidation:**  
  If questions are changed via the dashboard but Discord still shows old fields, call `forms_cog.invalidate_cache(form_id)`.
- [ ] **Check Role Hierarchy (Approval System):**  
  If approving a submission fails to grant a role, ensure the role is lower than the bot's top role and does not have Administrator permissions.

---

## 5. Verification & Test Suite

The system includes automated tests in [tests/test_forms_system.py](file:///d:/Projects/DiscordBot/tests/test_forms_system.py). Run them with:

```bash
.venv/Scripts/python.exe -m unittest tests/test_forms_system.py
```

### Verified Test Coverage:
1. `test_01_create_and_get_form_with_guild_isolation` (Anti-IDOR)
2. `test_02_update_and_delete_form_cascade`
3. `test_03_questions_crud_and_limits_capping` (Caps max_length at 1000)
4. `test_04_questions_reorder_and_delete_reindex`
5. `test_05_submissions_status_filtering_and_stats`
6. `test_06_user_max_submissions_and_cooldown`
7. `test_07_csv_formula_injection_sanitization` (CWE-1236)
8. `test_08_csv_generation_with_utf8_bom`
9. `test_09_csv_omits_review_columns_when_review_disabled`
10. `test_10_xlsx_generation`
11. `test_11_parse_relative_duration`
12. `test_12_panel_embed_and_view_states`
13. `test_13_api_forms_crud_and_validation`
14. `test_14_internal_bridge_secret_authentication`
15. `test_15_modal_safety_none_limits_and_title_length` *(Added)*
16. `test_16_open_debounce_vs_submission_cooldown` *(Added)*
17. `test_17_concurrency_multiple_simultaneous_submissions` *(Added)*
