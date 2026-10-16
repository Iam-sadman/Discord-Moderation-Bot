"""
Ticket Transcript Exporters (HTML & TXT).
Renders comprehensive, exact as-is conversation transcripts
with Discord dark-mode aesthetics, author badges, timestamps, and attachment previews.
"""

import html
import re
from typing import Dict, Any, List


def generate_txt_transcript(ticket: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    """Generates a clean, readable plain-text transcript."""
    lines = [
        "=" * 70,
        f"DISCORD TICKET TRANSCRIPT — TICKET #{ticket.get('id', 0):04d}",
        "=" * 70,
        f"Subject:        {ticket.get('subject', 'N/A')}",
        f"Category:       {ticket.get('category_name', 'General')}",
        f"Status:         {ticket.get('status', 'open').upper()}",
        f"Created By:     {ticket.get('user_name', 'Unknown')} (ID: {ticket.get('user_id', 'N/A')})",
        f"Created At:     {ticket.get('created_at', 'N/A')}",
        f"Closed By:      {ticket.get('closed_by_name', 'N/A')} (ID: {ticket.get('closed_by_id', 'N/A')})",
        f"Closed At:      {ticket.get('closed_at', 'N/A')}",
        f"Close Reason:   {ticket.get('close_reason', 'N/A')}",
        "=" * 70,
        "INITIAL ISSUE DESCRIPTION:",
        ticket.get('initial_details', 'N/A'),
        "=" * 70,
        "CONVERSATION LOG:",
        ""
    ]

    for m in messages:
        staff_tag = " [STAFF]" if m.get("is_staff") else ""
        ts = m.get("created_at", "")
        author = m.get("author_name", "Unknown")
        content = m.get("content", "")
        lines.append(f"[{ts}] {author}{staff_tag}:")
        if content:
            lines.append(f"  {content}")
        attachments = m.get("attachments") or []
        if attachments:
            lines.append(f"  [Attachments: {', '.join(attachments)}]")
        lines.append("")

    lines.append("=" * 70)
    lines.append("END OF TRANSCRIPT")
    lines.append("=" * 70)
    return "\n".join(lines)


def _format_message_content_html(content: str) -> str:
    """Safely escapes HTML and formats Discord markdown like code blocks, inline code, and line breaks."""
    if not content:
        return ""
    # HTML escape first to prevent XSS
    escaped = html.escape(content)

    # Multi-line codeblocks: ```lang\ncode\n``` or ```code```
    def replace_codeblock(match):
        code_text = match.group(1)
        return f'<pre class="chat-codeblock"><code>{code_text}</code></pre>'

    escaped = re.sub(r'```(?:[a-zA-Z0-9_-]+)?\n?([\s\S]*?)```', replace_codeblock, escaped)

    # Inline code: `code`
    escaped = re.sub(r'`([^`\n]+)`', r'<code class="chat-inline-code">\1</code>', escaped)

    # Bold: **bold**
    escaped = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', escaped)

    # Italic: *italic*
    escaped = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', escaped)

    # Underline: __underline__
    escaped = re.sub(r'__([^_]+)__', r'<u>\1</u>', escaped)

    # Replace newlines with <br> (outside pre tags)
    parts = escaped.split('<pre class="chat-codeblock">')
    reconstructed = [parts[0].replace('\n', '<br>')]
    for part in parts[1:]:
        subparts = part.split('</pre>', 1)
        if len(subparts) == 2:
            code_part = subparts[0]
            rest = subparts[1].replace('\n', '<br>')
            reconstructed.append(f'<pre class="chat-codeblock">{code_part}</pre>{rest}')
        else:
            reconstructed.append(f'<pre class="chat-codeblock">{part}')

    return ''.join(reconstructed)


def generate_html_transcript(ticket: Dict[str, Any], messages: List[Dict[str, Any]], guild_name: str = "Discord Server") -> str:
    """Generates a standalone, dark-themed HTML transcript."""
    ticket_id = ticket.get("id", 0)
    subject = html.escape(str(ticket.get("subject", "Support Ticket")))
    category_name = html.escape(str(ticket.get("category_name", "General")))
    user_name = html.escape(str(ticket.get("user_name", "Unknown")))
    user_id = html.escape(str(ticket.get("user_id", "")))
    status = html.escape(str(ticket.get("status", "open")).upper())
    created_at = html.escape(str(ticket.get("created_at", "N/A")))
    closed_at = html.escape(str(ticket.get("closed_at", "N/A")))
    closed_by = html.escape(str(ticket.get("closed_by_name", "N/A")))
    close_reason = html.escape(str(ticket.get("close_reason", "None provided")))
    initial_details_html = _format_message_content_html(ticket.get("initial_details", ""))

    status_color = "#23a55a" if status == "OPEN" else ("#f0b232" if status == "CLAIMED" else "#80848e")

    messages_html = []
    default_avatar = "https://cdn.discordapp.com/embed/avatars/0.png"

    # Opening initial issue message
    messages_html.append(f"""
    <div class="message initial-message">
        <div class="msg-avatar-col">
            <img class="avatar" src="{default_avatar}" alt="Opener" />
        </div>
        <div class="msg-content-col">
            <div class="msg-header">
                <span class="author-name">{user_name}</span>
                <span class="badge badge-opener">TICKET CREATOR</span>
                <span class="timestamp">{created_at}</span>
            </div>
            <div class="msg-body">
                <div class="initial-card">
                    <div class="initial-title">📋 <strong>Subject:</strong> {subject}</div>
                    <div class="initial-desc">{initial_details_html}</div>
                </div>
            </div>
        </div>
    </div>
    """)

    for m in messages:
        m_author = html.escape(str(m.get("author_name", "Unknown")))
        m_avatar = m.get("author_avatar") or default_avatar
        m_ts = html.escape(str(m.get("created_at", "")))
        m_content_html = _format_message_content_html(m.get("content", ""))
        is_staff = m.get("is_staff", False)
        staff_badge = '<span class="badge badge-staff">STAFF</span>' if is_staff else ""

        attachments_html = []
        for att in (m.get("attachments") or []):
            att_url = html.escape(att)
            is_img = any(att.lower().split("?")[0].endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])
            if is_img:
                attachments_html.append(f"""
                <div class="attachment-item">
                    <a href="{att_url}" target="_blank" rel="noopener noreferrer">
                        <img class="attachment-img" src="{att_url}" alt="Attachment preview" loading="lazy" />
                    </a>
                </div>
                """)
            else:
                filename = att.split("/")[-1].split("?")[0]
                attachments_html.append(f"""
                <div class="attachment-item file-item">
                    <a href="{att_url}" target="_blank" rel="noopener noreferrer" class="attachment-link">
                        📎 {html.escape(filename)}
                    </a>
                </div>
                """)

        att_container = f'<div class="attachments-grid">{"".join(attachments_html)}</div>' if attachments_html else ""

        messages_html.append(f"""
        <div class="message">
            <div class="msg-avatar-col">
                <img class="avatar" src="{m_avatar}" alt="{m_author}" onerror="this.src='{default_avatar}'" />
            </div>
            <div class="msg-content-col">
                <div class="msg-header">
                    <span class="author-name">{m_author}</span>
                    {staff_badge}
                    <span class="timestamp">{m_ts}</span>
                </div>
                <div class="msg-body">{m_content_html}</div>
                {att_container}
            </div>
        </div>
        """)

    rendered_messages = "\n".join(messages_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket #{ticket_id:04d} Transcript — {subject}</title>
    <style>
        :root {{
            --bg-body: #1e1f22;
            --bg-chat: #313338;
            --bg-card: #2b2d31;
            --bg-hover: #35373c;
            --text-normal: #dbdee1;
            --text-muted: #949ba4;
            --text-heading: #f2f3f5;
            --accent: #5865f2;
            --accent-green: #23a55a;
            --border: rgba(255, 255, 255, 0.08);
            --code-bg: #1e1f22;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            background-color: var(--bg-body);
            color: var(--text-normal);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            font-size: 15px;
            line-height: 1.5;
            padding: 24px;
        }}
        .transcript-container {{
            max-width: 900px;
            margin: 0 auto;
            background-color: var(--bg-chat);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        }}
        .ticket-header {{
            background-color: var(--bg-card);
            border-bottom: 1px solid var(--border);
            padding: 24px;
        }}
        .ticket-header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .ticket-title {{
            font-size: 22px;
            font-weight: 700;
            color: var(--text-heading);
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .status-badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            color: #fff;
            background-color: {status_color};
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .ticket-metadata-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
            margin-top: 16px;
            font-size: 13px;
        }}
        .meta-item {{
            background: rgba(0, 0, 0, 0.2);
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid var(--border);
        }}
        .meta-label {{
            color: var(--text-muted);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 2px;
        }}
        .meta-value {{
            color: var(--text-heading);
            font-weight: 600;
            word-break: break-all;
        }}
        .messages-list {{
            padding: 20px 24px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .message {{
            display: flex;
            gap: 14px;
            padding: 8px 12px;
            border-radius: 8px;
            transition: background 0.15s ease;
        }}
        .message:hover {{
            background-color: var(--bg-hover);
        }}
        .msg-avatar-col {{
            flex-shrink: 0;
        }}
        .avatar {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            object-fit: cover;
            background-color: #5865f2;
        }}
        .msg-content-col {{
            flex: 1;
            min-width: 0;
        }}
        .msg-header {{
            display: flex;
            align-items: baseline;
            gap: 8px;
            margin-bottom: 4px;
        }}
        .author-name {{
            font-weight: 600;
            color: var(--text-heading);
            font-size: 14px;
        }}
        .badge {{
            font-size: 10px;
            font-weight: 700;
            padding: 1px 6px;
            border-radius: 4px;
            text-transform: uppercase;
        }}
        .badge-staff {{
            background-color: #5865f2;
            color: #fff;
        }}
        .badge-opener {{
            background-color: #4e5058;
            color: #f2f3f5;
        }}
        .timestamp {{
            font-size: 12px;
            color: var(--text-muted);
        }}
        .msg-body {{
            color: var(--text-normal);
            word-break: break-word;
        }}
        .initial-card {{
            background: rgba(88, 101, 242, 0.1);
            border-left: 4px solid var(--accent);
            padding: 12px 16px;
            border-radius: 6px;
            margin-top: 4px;
        }}
        .initial-title {{
            font-size: 14px;
            margin-bottom: 6px;
            color: var(--text-heading);
        }}
        .initial-desc {{
            font-size: 14px;
            color: var(--text-normal);
        }}
        .chat-codeblock {{
            background-color: var(--code-bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 10px 14px;
            margin: 8px 0;
            overflow-x: auto;
            font-family: Consolas, monospace;
            font-size: 13px;
            color: #e0e1e5;
        }}
        .chat-inline-code {{
            background-color: var(--code-bg);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 13px;
        }}
        .attachments-grid {{
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .attachment-img {{
            max-width: 400px;
            max-height: 300px;
            border-radius: 8px;
            border: 1px solid var(--border);
            cursor: pointer;
            transition: transform 0.2s ease;
        }}
        .attachment-img:hover {{
            transform: scale(1.02);
        }}
        .attachment-link {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--bg-card);
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid var(--border);
            color: #00a8fc;
            text-decoration: none;
            font-size: 13px;
            font-weight: 500;
        }}
        .attachment-link:hover {{
            text-decoration: underline;
        }}
        .transcript-footer {{
            background-color: var(--bg-card);
            border-top: 1px solid var(--border);
            padding: 16px 24px;
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>
    <div class="transcript-container">
        <header class="ticket-header">
            <div class="ticket-header-top">
                <div class="ticket-title">
                    <span>🎫</span> Ticket #{ticket_id:04d} — {subject}
                </div>
                <div class="status-badge">{status}</div>
            </div>
            <div class="ticket-metadata-grid">
                <div class="meta-item">
                    <div class="meta-label">Category</div>
                    <div class="meta-value">{category_name}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Ticket Opener</div>
                    <div class="meta-value">{user_name} ({user_id})</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Created At</div>
                    <div class="meta-value">{created_at}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Closed By</div>
                    <div class="meta-value">{closed_by}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Closed At</div>
                    <div class="meta-value">{closed_at}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Close Reason</div>
                    <div class="meta-value">{close_reason}</div>
                </div>
            </div>
        </header>

        <main class="messages-list">
            {rendered_messages}
        </main>

        <footer class="transcript-footer">
            Discord Private Thread Ticket System — Archival Record for {html.escape(guild_name)}
        </footer>
    </div>
</body>
</html>
"""
