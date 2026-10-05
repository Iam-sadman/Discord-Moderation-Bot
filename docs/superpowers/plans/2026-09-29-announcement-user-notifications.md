# Announcement User Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send normal Discord notifications to members selected in the announcement destination flow or mentioned inline, and support explicit `@everyone`/`@here` pings when the bot has permission.

**Architecture:** Extend the existing announcement draft with selected users and add a UserSelect beside the current role selector. At publish time resolve inline Discord mention tokens and unique `@username` references against the guild, combine them with selected users, and include explicit mention tokens in message content with an AllowedMentions allow-list. Broadcast mentions are copied into content only when present in announcement text and require Mention Everyone permission.

**Tech Stack:** Python, discord.py 2.7.1.

## Global Constraints

- Keep user notifications limited to explicitly selected or resolved members.
- Resolve inline `@username` only when it identifies exactly one current guild member.
- Unknown or ambiguous inline names stop publication with actionable feedback.
- Explicit `@everyone` and `@here` trigger notifications only when present in the authored text and when the bot has Mention Everyone permission.
- Preserve role notification behavior and disable implicit mention parsing.

---

### Task 1: Model and resolve notification recipients

**Files:**
- Modify: `cogs/announcement.py`

**Interfaces:**
- Produces: `AnnouncementDraft.notify_users: list[discord.User]`.
- Produces: `_users_mentioned_in_text(guild, texts) -> tuple[list[discord.Member], Optional[str]]`.
- Produces: `_broadcast_mentions_in_text(texts) -> list[str]` returning canonical `@everyone` and/or `@here` tokens.

- [x] Add selected users to the draft with an empty-list default.
- [x] Resolve pasted `<@id>` / `<@!id>` references through the current guild and reject IDs outside the guild.
- [x] Resolve single-token `@username` and unique one-token display/global names case-insensitively; ignore names matching a role; return a clear error for unknown or ambiguous candidates.
- [x] Detect explicit `@everyone` and `@here` in the authored header, body, and footer.

### Task 2: Add the separate user picker

**Files:**
- Modify: `cogs/announcement.py`

**Interfaces:**
- Consumes: `AnnouncementDraft.notify_users`.
- Produces: an optional `discord.ui.UserSelect` in `AnnouncementDeliveryView`.

- [x] Add a multi-user selector with a maximum of 25 selected members.
- [x] Store its values in the draft and show selected user names in the ephemeral destination status.
- [x] Place destination, role selector, user selector, and action buttons on distinct component rows.

### Task 3: Publish native notifications safely

**Files:**
- Modify: `cogs/announcement.py`

- [x] Resolve inline user recipients before deferring or publishing; stop with an ephemeral actionable error for unknown or ambiguous mentions.
- [x] Deduplicate selected and inline recipients by user ID.
- [x] Include actual `<@id>` tokens for selected and inline users, role mention tokens, explicit broadcast tokens, and existing native preview URLs in message content without silently dropping mentions at the 2,000-character limit.
- [x] Configure AllowedMentions with explicit user and role allow-lists; enable everyone parsing only when `@everyone` or `@here` appears in authored text.
- [x] Check destination `mention_everyone` permission when a broadcast token is present and stop with a clear error if missing.
- [x] Move Publish, Back, and Cancel buttons to a row that does not conflict with the new user selector.

### Task 4: Validate the module

**Files:**
- Validate: `cogs/announcement.py`

- [x] Run `python -m py_compile cogs/announcement.py`.
- [x] Review the final code to confirm no broad user/everyone mention parsing was enabled and existing selected-role notifications remain intact.
