"""
Direct Messages Inbox Database Service.
Manages persistent storage in data/inbox.db with SQLite WAL mode,
transaction safety, indexing, and connection pooling.
Synchronizes messages between Discord direct messages, staff Discord relay channel, and Web Dashboard.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite

from config import Config

logger = logging.getLogger("InboxDB")

DEFAULT_DB_PATH = getattr(
    Config,
    "INBOX_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "inbox.db")
)
DEFAULT_RELAY_CHANNEL_ID = getattr(
    Config,
    "DEFAULT_INBOX_RELAY_CHANNEL_ID",
    1566408437603237898
)


def _safe_json_loads(val: Any, default: Any = None) -> Any:
    """Safely parse JSON or return default."""
    if default is None:
        default = []
    if not val:
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        res = json.loads(val)
        return res if res is not None else default
    except Exception:
        return default


class InboxDB:
    _instance: Optional["InboxDB"] = None

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "InboxDB":
        if cls._instance is None:
            cls._instance = cls(db_path or DEFAULT_DB_PATH)
        return cls._instance

    @asynccontextmanager
    async def get_connection(self):
        """Yields an aiosqlite connection with WAL mode, busy timeout, and foreign keys enabled."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            yield conn

    async def init_db(self):
        """Initializes tables, indices, and constraints."""
        if self._initialized:
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with self.get_connection() as db:
            # 1. Conversations Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS inbox_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    guild_id INTEGER,
                    username TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    avatar_url TEXT DEFAULT '',
                    status TEXT DEFAULT 'open',
                    unread_count INTEGER DEFAULT 0,
                    last_message_preview TEXT DEFAULT '',
                    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_relay_message_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Messages Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id) ON DELETE CASCADE,
                    discord_message_id INTEGER UNIQUE,
                    sender_type TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    author_avatar TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    attachments_json TEXT DEFAULT '[]',
                    embeds_json TEXT DEFAULT '[]',
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read INTEGER DEFAULT 0
                );
            """)

            # 3. Settings Table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS inbox_settings (
                    guild_id INTEGER PRIMARY KEY,
                    auto_reply_enabled INTEGER DEFAULT 1,
                    auto_reply_text TEXT DEFAULT 'Thank you for your message! Our team has received it on our dashboard and staff channel. We will get back to you shortly.',
                    notify_channel_id INTEGER DEFAULT 1566408437603237898,
                    allowed_roles_json TEXT DEFAULT '[]'
                );
            """)

            # 4. Indices for high performance
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_conv_user ON inbox_conversations(user_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_conv_last_msg ON inbox_conversations(last_message_at DESC);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_conv_status ON inbox_conversations(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_msg_conv ON inbox_messages(conversation_id, sent_at ASC);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inbox_msg_is_read ON inbox_messages(is_read);")

            await db.commit()

        self._initialized = True
        logger.info(f"[OK] InboxDB initialized at {self.db_path}")

    # =========================================================================
    # Conversation Methods
    # =========================================================================

    async def get_or_create_conversation(
        self,
        user_id: int,
        username: str,
        display_name: str = "",
        avatar_url: str = "",
        guild_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Gets existing conversation or inserts a new one with user details."""
        await self.init_db()
        user_id = int(user_id)
        username = str(username or f"User_{user_id}")
        display_name = str(display_name or username)
        avatar_url = str(avatar_url or "")
        guild_id = int(guild_id) if guild_id else None

        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM inbox_conversations WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                conv = dict(row)
                # Update username / display_name / avatar / guild if changed
                needs_update = False
                update_fields = []
                params = []

                if username and conv.get("username") != username:
                    update_fields.append("username = ?")
                    params.append(username)
                    needs_update = True
                if display_name and conv.get("display_name") != display_name:
                    update_fields.append("display_name = ?")
                    params.append(display_name)
                    needs_update = True
                if avatar_url and conv.get("avatar_url") != avatar_url:
                    update_fields.append("avatar_url = ?")
                    params.append(avatar_url)
                    needs_update = True
                if guild_id and not conv.get("guild_id"):
                    update_fields.append("guild_id = ?")
                    params.append(guild_id)
                    needs_update = True

                if needs_update:
                    update_fields.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(conv["id"])
                    query = f"UPDATE inbox_conversations SET {', '.join(update_fields)} WHERE id = ?"
                    await db.execute(query, tuple(params))
                    await db.commit()
                    conv["username"] = username or conv.get("username")
                    conv["display_name"] = display_name or conv.get("display_name")
                    conv["avatar_url"] = avatar_url or conv.get("avatar_url")
                    if guild_id:
                        conv["guild_id"] = guild_id
                return conv

            # Insert new conversation
            cursor = await db.execute(
                """
                INSERT INTO inbox_conversations (
                    user_id, guild_id, username, display_name, avatar_url,
                    status, unread_count, last_message_preview, last_message_at
                ) VALUES (?, ?, ?, ?, ?, 'open', 0, '', CURRENT_TIMESTAMP)
                """,
                (user_id, guild_id, username, display_name or username, avatar_url or "")
            )
            conv_id = cursor.lastrowid
            await db.commit()

            async with db.execute("SELECT * FROM inbox_conversations WHERE id = ?", (conv_id,)) as cur:
                new_row = await cur.fetchone()
                return dict(new_row) if new_row else {}

    async def get_conversation(self, conv_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single conversation by conversation ID."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM inbox_conversations WHERE id = ?", (conv_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_conversation_by_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a conversation by Discord user ID."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM inbox_conversations WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_conversations(
        self,
        guild_id: Optional[int] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Lists conversations with optional filtering by status and search terms.
        Returns (list_of_conversations, total_matching_count).
        """
        await self.init_db()
        conditions = []
        params: List[Any] = []

        if status and status.lower() not in ("all", ""):
            if status.lower() == "unread":
                conditions.append("unread_count > 0")
            else:
                conditions.append("status = ?")
                params.append(status.lower())

        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            conditions.append(
                "(LOWER(username) LIKE ? OR LOWER(display_name) LIKE ? OR CAST(user_id AS TEXT) LIKE ? OR LOWER(last_message_preview) LIKE ?)"
            )
            params.extend([term, term, term, term])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        count_sql = f"SELECT COUNT(*) FROM inbox_conversations {where_clause}"
        query_sql = f"""
            SELECT * FROM inbox_conversations
            {where_clause}
            ORDER BY last_message_at DESC
            LIMIT ? OFFSET ?
        """

        async with self.get_connection() as db:
            async with db.execute(count_sql, tuple(params)) as cur:
                total_row = await cur.fetchone()
                total = total_row[0] if total_row else 0

            query_params = list(params) + [limit, offset]
            async with db.execute(query_sql, tuple(query_params)) as cur:
                rows = await cur.fetchall()
                convs = [dict(r) for r in rows]

            return convs, total

    async def update_conversation_status(self, conv_id: int, status: str) -> bool:
        """Sets status to open, closed, or archived."""
        await self.init_db()
        status_clean = (status or "open").lower().strip()
        if status_clean not in ("open", "closed", "archived"):
            status_clean = "open"

        async with self.get_connection() as db:
            await db.execute(
                "UPDATE inbox_conversations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status_clean, conv_id)
            )
            await db.commit()
            return True

    async def update_last_relay_message_id(self, conv_id: int, message_id: int) -> bool:
        """Saves Discord message ID of the latest relay embed posted to staff channel."""
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE inbox_conversations SET last_relay_message_id = ? WHERE id = ?",
                (message_id, conv_id)
            )
            await db.commit()
            return True

    async def delete_conversation(self, conv_id: int) -> bool:
        """Deletes a conversation and all its messages."""
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("DELETE FROM inbox_conversations WHERE id = ?", (conv_id,))
            await db.commit()
            return True

    # =========================================================================
    # Message Methods
    # =========================================================================

    async def add_message(
        self,
        conversation_id: int,
        sender_type: str,
        author_id: int,
        author_name: str,
        author_avatar: str = "",
        content: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        discord_message_id: Optional[int] = None,
        is_read: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Inserts a message into the conversation thread, updates the preview snippet,
        refreshes last_message_at, and adjusts unread count.
        """
        await self.init_db()
        conversation_id = int(conversation_id)
        sender_type = str(sender_type)
        author_id = int(author_id)
        author_name = str(author_name)
        author_avatar = str(author_avatar or "")
        content = str(content or "")
        discord_message_id = int(discord_message_id) if discord_message_id else None

        attachments_json = json.dumps(attachments or [])
        embeds_json = json.dumps(embeds or [])

        # Default is_read: user incoming is unread (0), staff/bot outgoing is read (1)
        if is_read is None:
            is_read = 0 if sender_type == "user" else 1

        async with self.get_connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO inbox_messages (
                    conversation_id, discord_message_id, sender_type,
                    author_id, author_name, author_avatar, content,
                    attachments_json, embeds_json, sent_at, is_read
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    conversation_id,
                    discord_message_id,
                    sender_type,
                    author_id,
                    author_name,
                    author_avatar,
                    content or "",
                    attachments_json,
                    embeds_json,
                    is_read
                )
            )
            msg_id = cursor.lastrowid

            # Preview snippet
            preview = (content or "").strip()
            if not preview and attachments:
                preview = "📎 [Attachment]"
            if len(preview) > 100:
                preview = preview[:97] + "..."

            # Update conversation metadata
            if sender_type == "user":
                await db.execute(
                    """
                    UPDATE inbox_conversations
                    SET last_message_preview = ?,
                        last_message_at = CURRENT_TIMESTAMP,
                        unread_count = unread_count + 1,
                        status = 'open',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (preview, conversation_id)
                )
            else:
                await db.execute(
                    """
                    UPDATE inbox_conversations
                    SET last_message_preview = ?,
                        last_message_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (preview, conversation_id)
                )

            await db.commit()

            async with db.execute("SELECT * FROM inbox_messages WHERE id = ?", (msg_id,)) as cur:
                row = await cur.fetchone()
                msg_dict = dict(row)
                msg_dict["attachments"] = _safe_json_loads(msg_dict.get("attachments_json"), [])
                msg_dict["embeds"] = _safe_json_loads(msg_dict.get("embeds_json"), [])
                return msg_dict

    async def get_messages(
        self,
        conversation_id: int,
        limit: int = 100,
        before_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Retrieves chronological messages for a conversation thread."""
        await self.init_db()
        params: List[Any] = [conversation_id]
        before_sql = ""
        if before_id:
            before_sql = "AND id < ?"
            params.append(before_id)
        params.append(limit)

        sql = f"""
            SELECT * FROM (
                SELECT * FROM inbox_messages
                WHERE conversation_id = ? {before_sql}
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
        """

        async with self.get_connection() as db:
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    item["attachments"] = _safe_json_loads(item.get("attachments_json"), [])
                    item["embeds"] = _safe_json_loads(item.get("embeds_json"), [])
                    results.append(item)
                return results

    async def mark_as_read(self, conversation_id: int) -> bool:
        """Marks all messages in the conversation as read and resets unread_count."""
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE inbox_messages SET is_read = 1 WHERE conversation_id = ? AND is_read = 0",
                (conversation_id,)
            )
            await db.execute(
                "UPDATE inbox_conversations SET unread_count = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (conversation_id,)
            )
            await db.commit()
            return True

    async def get_total_unread_count(self, guild_id: Optional[int] = None) -> int:
        """Returns total unread conversations count for sidebar pill."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM inbox_conversations WHERE unread_count > 0"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0

    # =========================================================================
    # Settings Methods
    # =========================================================================

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Retrieves guild-specific inbox settings or default values."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM inbox_settings WHERE guild_id = ?",
                (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    data = dict(row)
                    data["allowed_roles"] = _safe_json_loads(data.get("allowed_roles_json"), [])
                    return data

        # Default settings
        return {
            "guild_id": guild_id,
            "auto_reply_enabled": 1,
            "auto_reply_text": "Thank you for your message! Our team has received it on our dashboard and staff channel. We will get back to you shortly.",
            "notify_channel_id": DEFAULT_RELAY_CHANNEL_ID,
            "allowed_roles": []
        }

    async def update_settings(self, guild_id: int, settings: Dict[str, Any]) -> bool:
        """Upserts settings for a guild."""
        await self.init_db()
        auto_reply_enabled = 1 if settings.get("auto_reply_enabled") else 0
        auto_reply_text = str(settings.get("auto_reply_text", "")).strip()
        notify_channel_id = int(settings.get("notify_channel_id") or DEFAULT_RELAY_CHANNEL_ID)
        allowed_roles_json = json.dumps(settings.get("allowed_roles", []))

        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO inbox_settings (
                    guild_id, auto_reply_enabled, auto_reply_text, notify_channel_id, allowed_roles_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    auto_reply_enabled = excluded.auto_reply_enabled,
                    auto_reply_text = excluded.auto_reply_text,
                    notify_channel_id = excluded.notify_channel_id,
                    allowed_roles_json = excluded.allowed_roles_json
                """,
                (guild_id, auto_reply_enabled, auto_reply_text, notify_channel_id, allowed_roles_json)
            )
            await db.commit()
            return True
