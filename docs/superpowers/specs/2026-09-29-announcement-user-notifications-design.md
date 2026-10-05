# Announcement user notifications design

## Goal

Send normal Discord user notifications for people included inline in announcement text or selected separately, while retaining role notifications.

## Agreed behavior

- Keep the existing optional role selector.
- Add an optional user selector in the destination step so authors can choose individual members directly.
- Recognize inline `@username` and Discord mention forms in the header, body, and footer.
- Resolve inline names only when they map to one server member. If a name is unknown or ambiguous, stop publication and ask the author to use the user selector or a precise mention.
- Deduplicate users found inline and selected in the separate user selector.
- Put actual user mention tokens into the published message content and explicitly allow only the resolved users to be mentioned. Discord does not send notifications from mentions inside embed text.
- Keep role mentions, role selection, and the existing role notification permission checks intact.
- Recognize explicit `@everyone` and `@here` in the announcement text and preserve Discord's normal notifications for them.
- Before publishing text containing either broadcast mention, require the bot to have Mention Everyone permission in the selected destination channel. Otherwise, stop with an actionable error.

## Interaction and data flow

The private announcement preview and existing destination step remain. In the destination step, authors select a destination, optional roles, and optional users. At publish time, the bot parses user mentions in the announcement text, resolves them against the guild, deduplicates them with selected users, and adds both role and user mentions to the message content before sending the embeds. The embed text stays as authored; the native notification mention may appear in the message content above the embed.

## Constraints and error handling

- A user selected or resolved for inline notification must belong to the current guild.
- Enable user mentions only for resolved recipients. Allow `@everyone` and `@here` only when the author explicitly included them in the announcement text.
- Unknown or ambiguous inline names must not silently publish without notification.
- The Bot's Server Members Intent must be enabled in the Discord Developer Portal for reliable username resolution. The separate user selector remains available as the reliable fallback.
- Preserve the existing Discord message length cap when composing mention content and native preview URLs.

## Acceptance criteria

1. Selecting one or more users in the destination step causes those users to receive native Discord mention notifications.
2. A unique inline `@username` or valid Discord mention in header, body, or footer causes that user to receive a notification.
3. Inline and separately selected recipients are mentioned only once.
4. Unknown or ambiguous inline names stop publication with actionable feedback.
5. Existing selected-role and inline-role notifications continue to work.
6. Explicit `@everyone` and `@here` in announcement text trigger Discord notifications when the bot has Mention Everyone permission in the destination channel.
7. Without that permission, the bot stops publication and explains how to fix the issue.
