"""
Tickets Management Database Service.
Manages persistent storage in data/tickets.db with SQLite WAL mode,
transaction safety, indexing, and connection pooling.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite

from config import Config

logger = logging.getLogger("TicketsDB")

DEFAULT_DB_PATH = getattr(Config, "TICKETS_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tickets.db"))

DEFAULT_CATEGORIES = [
    {
        "name": "🔑 Credential / Account Issue",
        "emoji": "🔑",
        "button_style": "danger",
        "description": "Help with account access, passwords, or credentials.",
        "assigned_role_ids": [],
        "assigned_member_ids": [],
        "position": 1
    },
    {
        "name": "💳 Payment & Billing",
        "emoji": "💳",
        "button_style": "success",
        "description": "Inquiries regarding subscriptions, invoices, and payments.",
        "assigned_role_ids": [],
        "assigned_member_ids": [],
        "position": 2
    },
    {
        "name": "🛠️ Technical / Tool Bug",
        "emoji": "🛠️",
        "button_style": "primary",
        "description": "Report broken tools, error logs, or functional defects.",
        "assigned_role_ids": [],
        "assigned_member_ids": [],
        "position": 3
    },
    {
        "name": "💬 General Support",
        "emoji": "💬",
        "button_style": "secondary",
        "description": "General questions and server community inquiries.",
        "assigned_role_ids": [],
        "assigned_member_ids": [],
        "position": 4
    }
]


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


def _str_id_list(items: Any) -> List[str]:
    """Ensures all IDs in list are string serialized."""
    raw = _safe_json_loads(items, [])
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if x is not None and str(x).strip()]


class TicketsDB:
    _instance: Optional["TicketsDB"] = None

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "TicketsDB":
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
            # 1. Global Guild Ticket Settings
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_settings (
                    guild_id INTEGER PRIMARY KEY,
                    panel_channel_id INTEGER,
                    panel_message_id INTEGER,
                    transcript_channel_id INTEGER,
                    support_role_ids TEXT DEFAULT '[]',
                    support_member_ids TEXT DEFAULT '[]',
                    max_open_per_user INTEGER DEFAULT 2,
                    panel_title TEXT DEFAULT '🛡️ Official Support & Issue Desk',
                    panel_description TEXT DEFAULT 'Click a category button below to open a secure Private Thread.',
                    panel_color TEXT DEFAULT '#5865F2'
                );
            """)

            # 1b. Multiple Ticket Panels
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    channel_id INTEGER,
                    message_id INTEGER,
                    transcript_channel_id INTEGER,
                    support_role_ids TEXT DEFAULT '[]',
                    support_member_ids TEXT DEFAULT '[]',
                    max_open_per_user INTEGER DEFAULT 2,
                    panel_title TEXT DEFAULT '🛡️ Official Support & Issue Desk',
                    panel_description TEXT DEFAULT 'Click a category button below to open a secure Private Thread.',
                    panel_color TEXT DEFAULT '#5865F2',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_panels_guild ON ticket_panels(guild_id);")

            # 2. Ticket Categories
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    panel_id INTEGER,
                    name TEXT NOT NULL,
                    emoji TEXT DEFAULT '🎫',
                    button_style TEXT DEFAULT 'primary',
                    description TEXT,
                    assigned_role_ids TEXT DEFAULT '[]',
                    assigned_member_ids TEXT DEFAULT '[]',
                    position INTEGER DEFAULT 1
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_cat_guild_pos ON ticket_categories(guild_id, position);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_cat_panel ON ticket_categories(panel_id);")

            # Migration: Ensure panel_id exists in ticket_categories
            cur = await db.execute("PRAGMA table_info(ticket_categories);")
            cat_cols = [row["name"] for row in await cur.fetchall()]
            if "panel_id" not in cat_cols:
                await db.execute("ALTER TABLE ticket_categories ADD COLUMN panel_id INTEGER DEFAULT NULL;")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_cat_panel ON ticket_categories(panel_id);")

            # 3. Tickets
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL UNIQUE,
                    panel_channel_id INTEGER NOT NULL,
                    panel_id INTEGER,
                    category_id INTEGER,
                    category_name TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    initial_details TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    claimed_by_id INTEGER,
                    claimed_by_name TEXT,
                    assigned_roles_json TEXT DEFAULT '[]',
                    assigned_users_json TEXT DEFAULT '[]',
                    closed_by_id INTEGER,
                    closed_by_name TEXT,
                    close_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_thread ON tickets(thread_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_panel ON tickets(panel_id);")

            # Migration: Ensure panel_id exists in tickets (for existing databases)
            cur = await db.execute("PRAGMA table_info(tickets);")
            t_cols = [row["name"] for row in await cur.fetchall()]
            if "panel_id" not in t_cols:
                await db.execute("ALTER TABLE tickets ADD COLUMN panel_id INTEGER DEFAULT NULL;")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_panel ON tickets(panel_id);")

            # 4. Ticket Messages (Stores Full As-Is Transcript History)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    author_avatar TEXT,
                    is_staff BOOLEAN DEFAULT 0,
                    content TEXT,
                    attachments_json TEXT DEFAULT '[]',
                    created_at TIMESTAMP NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_msgs_ticket ON ticket_messages(ticket_id);")

            await db.commit()
            self._initialized = True
            logger.info(f"[OK] TicketsDB initialized at {self.db_path}")

    # =========================================================================
    # 1. Ticket Settings Management
    # =========================================================================

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Retrieves global ticket settings for a guild, returning safe defaults if not found."""
        await self.init_db()
        guild_id = int(guild_id)
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM ticket_settings WHERE guild_id = ?", (guild_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "guild_id": str(row["guild_id"]),
                        "panel_channel_id": str(row["panel_channel_id"]) if row["panel_channel_id"] else None,
                        "panel_message_id": str(row["panel_message_id"]) if row["panel_message_id"] else None,
                        "transcript_channel_id": str(row["transcript_channel_id"]) if row["transcript_channel_id"] else None,
                        "support_role_ids": _str_id_list(row["support_role_ids"]),
                        "support_member_ids": _str_id_list(row["support_member_ids"]),
                        "max_open_per_user": row["max_open_per_user"] or 2,
                        "panel_title": row["panel_title"] or "🛡️ Official Support & Issue Desk",
                        "panel_description": row["panel_description"] or "Click a category button below to open a secure Private Thread.",
                        "panel_color": row["panel_color"] or "#5865F2"
                    }

        # Return defaults for new guild
        return {
            "guild_id": str(guild_id),
            "panel_channel_id": None,
            "panel_message_id": None,
            "transcript_channel_id": None,
            "support_role_ids": [],
            "support_member_ids": [],
            "max_open_per_user": 2,
            "panel_title": "🛡️ Official Support & Issue Desk",
            "panel_description": "Click a category button below to open a secure Private Thread.",
            "panel_color": "#5865F2"
        }

    async def upsert_settings(
        self,
        guild_id: int,
        panel_channel_id: Optional[int] = None,
        panel_message_id: Optional[int] = None,
        transcript_channel_id: Optional[int] = None,
        support_role_ids: Optional[List[str]] = None,
        support_member_ids: Optional[List[str]] = None,
        max_open_per_user: Optional[int] = None,
        panel_title: Optional[str] = None,
        panel_description: Optional[str] = None,
        panel_color: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates or updates ticket settings for a guild."""
        await self.init_db()
        guild_id = int(guild_id)
        current = await self.get_settings(guild_id)

        p_channel = int(panel_channel_id) if panel_channel_id is not None else (int(current["panel_channel_id"]) if current["panel_channel_id"] else None)
        p_msg = int(panel_message_id) if panel_message_id is not None else (int(current["panel_message_id"]) if current["panel_message_id"] else None)
        t_channel = int(transcript_channel_id) if transcript_channel_id is not None else (int(current["transcript_channel_id"]) if current["transcript_channel_id"] else None)
        s_roles = [str(r).strip() for r in support_role_ids] if support_role_ids is not None else current["support_role_ids"]
        s_members = [str(m).strip() for m in support_member_ids] if support_member_ids is not None else current["support_member_ids"]
        max_open = max_open_per_user if max_open_per_user is not None else current["max_open_per_user"]
        title = panel_title if panel_title is not None else current["panel_title"]
        desc = panel_description if panel_description is not None else current["panel_description"]
        color = panel_color if panel_color is not None else current["panel_color"]

        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO ticket_settings (
                    guild_id, panel_channel_id, panel_message_id, transcript_channel_id,
                    support_role_ids, support_member_ids, max_open_per_user,
                    panel_title, panel_description, panel_color
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    panel_channel_id = excluded.panel_channel_id,
                    panel_message_id = COALESCE(excluded.panel_message_id, ticket_settings.panel_message_id),
                    transcript_channel_id = excluded.transcript_channel_id,
                    support_role_ids = excluded.support_role_ids,
                    support_member_ids = excluded.support_member_ids,
                    max_open_per_user = excluded.max_open_per_user,
                    panel_title = excluded.panel_title,
                    panel_description = excluded.panel_description,
                    panel_color = excluded.panel_color;
            """, (
                guild_id, p_channel, p_msg, t_channel,
                json.dumps(s_roles), json.dumps(s_members), max_open,
                title, desc, color
            ))
            await db.commit()

        return await self.get_settings(guild_id)

    async def update_panel_message_id(self, guild_id: int, message_id: Optional[int]):
        """Updates only the panel_message_id."""
        await self.init_db()
        guild_id = int(guild_id)
        msg_id_val = int(message_id) if message_id else None
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO ticket_settings (guild_id, panel_message_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET panel_message_id = excluded.panel_message_id;
            """, (guild_id, msg_id_val))
            await db.commit()

    # =========================================================================
    # 1b. Multiple Ticket Panels Management
    # =========================================================================

    async def get_panels(self, guild_id: int) -> List[Dict[str, Any]]:
        """Returns all ticket panels for a guild. If none exist, auto-seeds default panel."""
        await self.init_db()
        guild_id = int(guild_id)
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT p.*,
                    (SELECT COUNT(*) FROM ticket_categories c WHERE c.panel_id = p.id) AS category_count
                FROM ticket_panels p
                WHERE p.guild_id = ?
                ORDER BY p.id ASC;
            """, (guild_id,)) as cur:
                rows = await cur.fetchall()

            if not rows:
                # Check if ticket_settings exists for migration
                async with db.execute("SELECT * FROM ticket_settings WHERE guild_id = ?;", (guild_id,)) as cur_s:
                    s_row = await cur_s.fetchone()

                p_name = "Main Support Desk"
                p_ch = s_row["panel_channel_id"] if s_row else None
                p_msg = s_row["panel_message_id"] if s_row else None
                t_ch = s_row["transcript_channel_id"] if s_row else None
                s_roles = s_row["support_role_ids"] if s_row else "[]"
                s_members = s_row["support_member_ids"] if s_row else "[]"
                max_open = s_row["max_open_per_user"] if s_row else 2
                p_title = s_row["panel_title"] if s_row else "🛡️ Official Support & Issue Desk"
                p_desc = s_row["panel_description"] if s_row else "Click a category button below to open a secure Private Thread."
                p_color = s_row["panel_color"] if s_row else "#5865F2"

                ins = await db.execute("""
                    INSERT INTO ticket_panels (
                        guild_id, name, channel_id, message_id, transcript_channel_id,
                        support_role_ids, support_member_ids, max_open_per_user,
                        panel_title, panel_description, panel_color
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    guild_id, p_name, p_ch, p_msg, t_ch,
                    s_roles, s_members, max_open,
                    p_title, p_desc, p_color
                ))
                new_panel_id = ins.lastrowid

                # Link any orphan categories to this panel
                await db.execute("""
                    UPDATE ticket_categories SET panel_id = ?
                    WHERE guild_id = ? AND (panel_id IS NULL OR panel_id = 0);
                """, (new_panel_id, guild_id))

                # If still no categories, seed defaults for this panel
                async with db.execute("SELECT COUNT(*) as cnt FROM ticket_categories WHERE panel_id = ?;", (new_panel_id,)) as cur_c:
                    c_row = await cur_c.fetchone()
                    if not c_row or c_row["cnt"] == 0:
                        for cat in DEFAULT_CATEGORIES:
                            await db.execute("""
                                INSERT INTO ticket_categories (
                                    guild_id, panel_id, name, emoji, button_style, description,
                                    assigned_role_ids, assigned_member_ids, position
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """, (
                                guild_id, new_panel_id, cat["name"], cat["emoji"], cat["button_style"],
                                cat["description"], json.dumps(cat["assigned_role_ids"]),
                                json.dumps(cat["assigned_member_ids"]), cat["position"]
                            ))

                await db.commit()

                async with db.execute("""
                    SELECT p.*,
                        (SELECT COUNT(*) FROM ticket_categories c WHERE c.panel_id = p.id) AS category_count
                    FROM ticket_panels p
                    WHERE p.guild_id = ?
                    ORDER BY p.id ASC;
                """, (guild_id,)) as cur_refetch:
                    rows = await cur_refetch.fetchall()

            return [
                {
                    "id": r["id"],
                    "guild_id": str(r["guild_id"]),
                    "name": r["name"],
                    "channel_id": str(r["channel_id"]) if r["channel_id"] else None,
                    "message_id": str(r["message_id"]) if r["message_id"] else None,
                    "transcript_channel_id": str(r["transcript_channel_id"]) if r["transcript_channel_id"] else None,
                    "support_role_ids": _str_id_list(r["support_role_ids"]),
                    "support_member_ids": _str_id_list(r["support_member_ids"]),
                    "max_open_per_user": r["max_open_per_user"] or 2,
                    "panel_title": r["panel_title"] or "🛡️ Official Support & Issue Desk",
                    "panel_description": r["panel_description"] or "Click a category button below to open a secure Private Thread.",
                    "panel_color": r["panel_color"] or "#5865F2",
                    "category_count": r["category_count"] or 0,
                    "created_at": str(r["created_at"]) if r["created_at"] else ""
                }
                for r in rows
            ]

    async def get_panel(self, panel_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single ticket panel by ID."""
        await self.init_db()
        panel_id = int(panel_id)
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM ticket_panels WHERE id = ?;", (panel_id,)) as cur:
                r = await cur.fetchone()
                if not r:
                    return None
                return {
                    "id": r["id"],
                    "guild_id": str(r["guild_id"]),
                    "name": r["name"],
                    "channel_id": str(r["channel_id"]) if r["channel_id"] else None,
                    "message_id": str(r["message_id"]) if r["message_id"] else None,
                    "transcript_channel_id": str(r["transcript_channel_id"]) if r["transcript_channel_id"] else None,
                    "support_role_ids": _str_id_list(r["support_role_ids"]),
                    "support_member_ids": _str_id_list(r["support_member_ids"]),
                    "max_open_per_user": r["max_open_per_user"] or 2,
                    "panel_title": r["panel_title"] or "🛡️ Official Support & Issue Desk",
                    "panel_description": r["panel_description"] or "Click a category button below to open a secure Private Thread.",
                    "panel_color": r["panel_color"] or "#5865F2",
                    "created_at": str(r["created_at"]) if r["created_at"] else ""
                }

    async def create_panel(
        self,
        guild_id: int,
        name: str,
        channel_id: Optional[int] = None,
        transcript_channel_id: Optional[int] = None,
        support_role_ids: Optional[List[str]] = None,
        support_member_ids: Optional[List[str]] = None,
        max_open_per_user: int = 2,
        panel_title: str = "🛡️ Official Support & Issue Desk",
        panel_description: str = "Click a category button below to open a secure Private Thread.",
        panel_color: str = "#5865F2"
    ) -> int:
        """Creates a new ticket panel for a guild."""
        await self.init_db()
        guild_id = int(guild_id)
        ch_val = int(channel_id) if channel_id else None
        tr_val = int(transcript_channel_id) if transcript_channel_id else None
        s_roles = [str(r).strip() for r in support_role_ids] if support_role_ids else []
        s_members = [str(m).strip() for m in support_member_ids] if support_member_ids else []

        async with self.get_connection() as db:
            cur = await db.execute("""
                INSERT INTO ticket_panels (
                    guild_id, name, channel_id, message_id, transcript_channel_id,
                    support_role_ids, support_member_ids, max_open_per_user,
                    panel_title, panel_description, panel_color
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?);
            """, (
                guild_id, name.strip() or "Support Desk", ch_val, tr_val,
                json.dumps(s_roles), json.dumps(s_members), max_open_per_user or 2,
                panel_title.strip() or "🛡️ Official Support & Issue Desk",
                panel_description.strip() or "Click a category button below to open a secure Private Thread.",
                panel_color.strip() or "#5865F2"
            ))
            await db.commit()
            return cur.lastrowid

    async def update_panel(self, panel_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        """Updates fields on a ticket panel."""
        await self.init_db()
        panel_id = int(panel_id)
        panel = await self.get_panel(panel_id)
        if not panel:
            return None

        allowed = {
            "name", "channel_id", "message_id", "transcript_channel_id",
            "support_role_ids", "support_member_ids", "max_open_per_user",
            "panel_title", "panel_description", "panel_color"
        }
        updates = []
        params = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k in ("support_role_ids", "support_member_ids"):
                v = json.dumps([str(x).strip() for x in (v or [])])
            elif k in ("channel_id", "message_id", "transcript_channel_id"):
                v = int(v) if v else None
            elif k == "max_open_per_user":
                v = int(v) if v else 2
            updates.append(f"{k} = ?")
            params.append(v)

        if updates:
            params.append(panel_id)
            async with self.get_connection() as db:
                await db.execute(f"UPDATE ticket_panels SET {', '.join(updates)} WHERE id = ?;", params)
                await db.commit()

        return await self.get_panel(panel_id)

    async def delete_panel(self, panel_id: int) -> Tuple[bool, Optional[int], Optional[int]]:
        """Deletes a ticket panel and its associated category buttons. Returns (success, channel_id, message_id)."""
        await self.init_db()
        panel = await self.get_panel(panel_id)
        if not panel:
            return False, None, None

        ch_id = int(panel["channel_id"]) if panel["channel_id"] else None
        msg_id = int(panel["message_id"]) if panel["message_id"] else None

        async with self.get_connection() as db:
            await db.execute("DELETE FROM ticket_categories WHERE panel_id = ?;", (panel_id,))
            await db.execute("DELETE FROM ticket_panels WHERE id = ?;", (panel_id,))
            await db.commit()

        return True, ch_id, msg_id

    # =========================================================================
    # 2. Ticket Categories Management (Buttons & Role/Member Routing)
    # =========================================================================

    async def get_categories(self, guild_id: int, panel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns categories ordered by position. Optionally filtered by panel_id."""
        await self.init_db()
        guild_id = int(guild_id)
        async with self.get_connection() as db:
            if panel_id is not None:
                query = "SELECT * FROM ticket_categories WHERE guild_id = ? AND panel_id = ? ORDER BY position ASC, id ASC;"
                params = (guild_id, int(panel_id))
            else:
                query = "SELECT * FROM ticket_categories WHERE guild_id = ? ORDER BY position ASC, id ASC;"
                params = (guild_id,)

            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()

            if not rows and panel_id is None:
                # Seed defaults
                for cat in DEFAULT_CATEGORIES:
                    await db.execute("""
                        INSERT INTO ticket_categories (
                            guild_id, name, emoji, button_style, description,
                            assigned_role_ids, assigned_member_ids, position
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """, (
                        guild_id, cat["name"], cat["emoji"], cat["button_style"],
                        cat["description"], json.dumps(cat["assigned_role_ids"]),
                        json.dumps(cat["assigned_member_ids"]), cat["position"]
                    ))
                await db.commit()

                async with db.execute(query, params) as cur2:
                    rows = await cur2.fetchall()

            return [
                {
                    "id": r["id"],
                    "guild_id": str(r["guild_id"]),
                    "panel_id": r["panel_id"] if "panel_id" in r.keys() else None,
                    "name": r["name"],
                    "emoji": r["emoji"] or "🎫",
                    "button_style": r["button_style"] or "primary",
                    "description": r["description"] or "",
                    "assigned_role_ids": _str_id_list(r["assigned_role_ids"]),
                    "assigned_member_ids": _str_id_list(r["assigned_member_ids"]),
                    "position": r["position"]
                }
                for r in rows
            ]

    async def get_category(self, cat_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single category by ID."""
        await self.init_db()
        cat_id = int(cat_id)
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM ticket_categories WHERE id = ?;", (cat_id,)) as cur:
                r = await cur.fetchone()
                if not r:
                    return None
                return {
                    "id": r["id"],
                    "guild_id": str(r["guild_id"]),
                    "panel_id": r["panel_id"] if "panel_id" in r.keys() else None,
                    "name": r["name"],
                    "emoji": r["emoji"] or "🎫",
                    "button_style": r["button_style"] or "primary",
                    "description": r["description"] or "",
                    "assigned_role_ids": _str_id_list(r["assigned_role_ids"]),
                    "assigned_member_ids": _str_id_list(r["assigned_member_ids"]),
                    "position": r["position"]
                }

    async def create_category(
        self,
        guild_id: int,
        name: str,
        emoji: str = "🎫",
        button_style: str = "primary",
        description: str = "",
        assigned_role_ids: Optional[List[str]] = None,
        assigned_member_ids: Optional[List[str]] = None,
        position: Optional[int] = None,
        panel_id: Optional[int] = None
    ) -> int:
        """Creates a new category for a guild, optionally assigned to a panel."""
        await self.init_db()
        guild_id = int(guild_id)
        s_roles = [str(r).strip() for r in (assigned_role_ids or [])]
        s_members = [str(m).strip() for m in (assigned_member_ids or [])]
        p_id = int(panel_id) if panel_id else None

        if p_id is None:
            # Fallback to first panel if available
            panels = await self.get_panels(guild_id)
            if panels:
                p_id = panels[0]["id"]

        async with self.get_connection() as db:
            if position is None:
                if p_id is not None:
                    async with db.execute("SELECT MAX(position) as max_pos FROM ticket_categories WHERE guild_id = ? AND panel_id = ?;", (guild_id, p_id)) as cur:
                        row = await cur.fetchone()
                        position = (row["max_pos"] or 0) + 1 if row else 1
                else:
                    async with db.execute("SELECT MAX(position) as max_pos FROM ticket_categories WHERE guild_id = ?;", (guild_id,)) as cur:
                        row = await cur.fetchone()
                        position = (row["max_pos"] or 0) + 1 if row else 1

            cursor = await db.execute("""
                INSERT INTO ticket_categories (
                    guild_id, panel_id, name, emoji, button_style, description,
                    assigned_role_ids, assigned_member_ids, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                guild_id, p_id, name.strip(), emoji.strip() or "🎫", button_style.strip().lower() or "primary",
                description.strip() if description else "", json.dumps(s_roles),
                json.dumps(s_members), int(position)
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_category(
        self,
        cat_id: int,
        name: Optional[str] = None,
        emoji: Optional[str] = None,
        button_style: Optional[str] = None,
        description: Optional[str] = None,
        assigned_role_ids: Optional[List[str]] = None,
        assigned_member_ids: Optional[List[str]] = None,
        position: Optional[int] = None,
        panel_id: Optional[int] = None
    ) -> bool:
        """Updates category properties."""
        await self.init_db()
        cat = await self.get_category(cat_id)
        if not cat:
            return False

        new_name = name.strip() if name is not None else cat["name"]
        new_emoji = emoji.strip() if emoji is not None else cat["emoji"]
        new_style = button_style.strip().lower() if button_style is not None else cat["button_style"]
        new_desc = description.strip() if description is not None else cat["description"]
        new_roles = [str(r).strip() for r in assigned_role_ids] if assigned_role_ids is not None else cat["assigned_role_ids"]
        new_members = [str(m).strip() for m in assigned_member_ids] if assigned_member_ids is not None else cat["assigned_member_ids"]
        new_pos = int(position) if position is not None else cat["position"]
        new_panel = int(panel_id) if panel_id is not None else cat.get("panel_id")

        async with self.get_connection() as db:
            await db.execute("""
                UPDATE ticket_categories SET
                    name = ?,
                    emoji = ?,
                    button_style = ?,
                    description = ?,
                    assigned_role_ids = ?,
                    assigned_member_ids = ?,
                    position = ?,
                    panel_id = ?
                WHERE id = ?;
            """, (
                new_name, new_emoji, new_style, new_desc,
                json.dumps(new_roles), json.dumps(new_members),
                new_pos, new_panel, int(cat_id)
            ))
            await db.commit()
        return True

    async def delete_category(self, cat_id: int) -> bool:
        """Deletes a category."""
        await self.init_db()
        async with self.get_connection() as db:
            res = await db.execute("DELETE FROM ticket_categories WHERE id = ?;", (int(cat_id),))
            await db.commit()
            return res.rowcount > 0

    async def reorder_categories(self, guild_id: int, category_ids_in_order: List[int]) -> bool:
        """Reorders categories by setting position 1..N based on list order."""
        await self.init_db()
        guild_id = int(guild_id)
        async with self.get_connection() as db:
            for idx, cid in enumerate(category_ids_in_order, start=1):
                await db.execute("""
                    UPDATE ticket_categories SET position = ? WHERE id = ? AND guild_id = ?;
                """, (idx, int(cid), guild_id))
            await db.commit()
        return True

    # =========================================================================
    # 3. Tickets Management
    # =========================================================================

    async def create_ticket(
        self,
        guild_id: int,
        thread_id: int,
        panel_channel_id: int,
        category_id: Optional[int],
        category_name: str,
        user_id: int,
        user_name: str,
        subject: str,
        initial_details: str,
        assigned_roles: Optional[List[str]] = None,
        assigned_users: Optional[List[str]] = None,
        panel_id: Optional[int] = None
    ) -> int:
        """Creates an initial ticket record."""
        await self.init_db()
        roles = [str(r).strip() for r in (assigned_roles or [])]
        users = [str(u).strip() for u in (assigned_users or [])]
        p_id = int(panel_id) if panel_id else None

        async with self.get_connection() as db:
            cur = await db.execute("""
                INSERT INTO tickets (
                    guild_id, thread_id, panel_channel_id, category_id,
                    category_name, user_id, user_name, subject,
                    initial_details, status, assigned_roles_json, assigned_users_json, panel_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?);
            """, (
                int(guild_id), int(thread_id), int(panel_channel_id),
                int(category_id) if category_id else None,
                category_name.strip(), int(user_id), user_name.strip(),
                subject.strip(), initial_details.strip(),
                json.dumps(roles), json.dumps(users), p_id
            ))
            await db.commit()
            return cur.lastrowid

    async def update_ticket_thread_id(self, ticket_id: int, thread_id: int) -> bool:
        """Updates the thread_id of a newly created ticket."""
        await self.init_db()
        async with self.get_connection() as db:
            res = await db.execute(
                "UPDATE tickets SET thread_id = ? WHERE id = ?;",
                (int(thread_id), int(ticket_id))
            )
            await db.commit()
            return res.rowcount > 0

    async def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a ticket record by primary key."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM tickets WHERE id = ?;", (int(ticket_id),)) as cur:
                r = await cur.fetchone()
                if not r:
                    return None
                return self._format_ticket_row(r)

    async def get_ticket_by_thread(self, thread_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a ticket record by Discord private thread ID."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute("SELECT * FROM tickets WHERE thread_id = ?;", (int(thread_id),)) as cur:
                r = await cur.fetchone()
                if not r:
                    return None
                return self._format_ticket_row(r)

    async def count_open_tickets_for_user(self, guild_id: int, user_id: int, panel_id: Optional[int] = None) -> int:
        """Counts how many active (open or claimed) tickets a user has, optionally filtered by panel."""
        await self.init_db()
        async with self.get_connection() as db:
            if panel_id is not None:
                query = """
                    SELECT COUNT(*) as cnt FROM tickets
                    WHERE guild_id = ? AND user_id = ? AND panel_id = ? AND status IN ('open', 'claimed');
                """
                params = (int(guild_id), int(user_id), int(panel_id))
            else:
                query = """
                    SELECT COUNT(*) as cnt FROM tickets
                    WHERE guild_id = ? AND user_id = ? AND status IN ('open', 'claimed');
                """
                params = (int(guild_id), int(user_id))

            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return row["cnt"] if row else 0

    async def list_tickets(
        self,
        guild_id: int,
        status: Optional[str] = None,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
        panel_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Lists tickets for a guild with optional filtering by status, category, panel, or search query."""
        await self.init_db()
        guild_id = int(guild_id)
        query = "SELECT * FROM tickets WHERE guild_id = ?"
        params: List[Any] = [guild_id]

        if panel_id is not None:
            query += " AND panel_id = ?"
            params.append(int(panel_id))

        if status and status.lower() != "all":
            query += " AND status = ?"
            params.append(status.lower())

        if category_id:
            query += " AND category_id = ?"
            params.append(int(category_id))

        if search and search.strip():
            s = f"%{search.strip().lower()}%"
            query += " AND (LOWER(subject) LIKE ? OR LOWER(user_name) LIKE ? OR CAST(id AS TEXT) LIKE ?)"
            params.extend([s, s, s])

        query += " ORDER BY id DESC LIMIT ? OFFSET ?;"
        params.extend([limit, offset])

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cur:
                rows = await cur.fetchall()
                return [self._format_ticket_row(r) for r in rows]

    async def get_ticket_counts(self, guild_id: int, panel_id: Optional[int] = None) -> Dict[str, int]:
        """Returns ticket count statistics for a guild, optionally filtered by panel."""
        await self.init_db()
        guild_id = int(guild_id)
        query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_cnt,
                SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) as claimed_cnt,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_cnt
            FROM tickets WHERE guild_id = ?
        """
        params: List[Any] = [guild_id]
        if panel_id is not None:
            query += " AND panel_id = ?"
            params.append(int(panel_id))

        query += ";"
        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cur:
                r = await cur.fetchone()
                if not r:
                    return {"total": 0, "open": 0, "claimed": 0, "closed": 0}
                return {
                    "total": r["total"] or 0,
                    "open": r["open_cnt"] or 0,
                    "claimed": r["claimed_cnt"] or 0,
                    "closed": r["closed_cnt"] or 0
                }

    async def claim_ticket(self, ticket_id: int, staff_id: int, staff_name: str) -> bool:
        """Marks a ticket as claimed by a staff member."""
        await self.init_db()
        async with self.get_connection() as db:
            res = await db.execute("""
                UPDATE tickets SET
                    status = 'claimed',
                    claimed_by_id = ?,
                    claimed_by_name = ?
                WHERE id = ? AND status != 'closed';
            """, (int(staff_id), staff_name.strip(), int(ticket_id)))
            await db.commit()
            return res.rowcount > 0

    async def assign_role_to_ticket(self, ticket_id: int, role_id: str) -> bool:
        """Adds a role ID to the ticket's assigned roles list."""
        await self.init_db()
        t = await self.get_ticket(ticket_id)
        if not t:
            return False
        roles = set(t["assigned_roles"])
        roles.add(str(role_id).strip())
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE tickets SET assigned_roles_json = ? WHERE id = ?;",
                (json.dumps(list(roles)), int(ticket_id))
            )
            await db.commit()
        return True

    async def assign_user_to_ticket(self, ticket_id: int, user_id: str) -> bool:
        """Adds a user ID to the ticket's assigned users list."""
        await self.init_db()
        t = await self.get_ticket(ticket_id)
        if not t:
            return False
        users = set(t["assigned_users"])
        users.add(str(user_id).strip())
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE tickets SET assigned_users_json = ? WHERE id = ?;",
                (json.dumps(list(users)), int(ticket_id))
            )
            await db.commit()
        return True

    async def close_ticket(
        self,
        ticket_id: int,
        closed_by_id: Optional[int],
        closed_by_name: str,
        close_reason: Optional[str] = None
    ) -> bool:
        """Marks a ticket as closed."""
        await self.init_db()
        async with self.get_connection() as db:
            res = await db.execute("""
                UPDATE tickets SET
                    status = 'closed',
                    closed_by_id = ?,
                    closed_by_name = ?,
                    close_reason = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status != 'closed';
            """, (
                int(closed_by_id) if closed_by_id else None,
                closed_by_name.strip(),
                close_reason.strip() if close_reason else None,
                int(ticket_id)
            ))
            await db.commit()
            return res.rowcount > 0

    def _format_ticket_row(self, r: Any) -> Dict[str, Any]:
        """Converts database row to dictionary with strict 64-bit string serialization."""
        return {
            "id": r["id"],
            "guild_id": str(r["guild_id"]),
            "thread_id": str(r["thread_id"]),
            "panel_channel_id": str(r["panel_channel_id"]),
            "panel_id": r["panel_id"] if "panel_id" in r.keys() else None,
            "category_id": r["category_id"],
            "category_name": r["category_name"],
            "user_id": str(r["user_id"]),
            "user_name": r["user_name"],
            "subject": r["subject"],
            "initial_details": r["initial_details"],
            "status": r["status"] or "open",
            "claimed_by_id": str(r["claimed_by_id"]) if r["claimed_by_id"] else None,
            "claimed_by_name": r["claimed_by_name"],
            "assigned_roles": _str_id_list(r["assigned_roles_json"]),
            "assigned_users": _str_id_list(r["assigned_users_json"]),
            "closed_by_id": str(r["closed_by_id"]) if r["closed_by_id"] else None,
            "closed_by_name": r["closed_by_name"],
            "close_reason": r["close_reason"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"]
        }

    # =========================================================================
    # 4. Ticket Messages & Transcripts (Full As-Is History)
    # =========================================================================

    async def save_messages_batch(self, ticket_id: int, messages: List[Dict[str, Any]]) -> int:
        """Batch inserts messages into ticket_messages preserving exact as-is content."""
        await self.init_db()
        if not messages:
            return 0
        async with self.get_connection() as db:
            count = 0
            for m in messages:
                att = m.get("attachments_json")
                if isinstance(att, list):
                    att = json.dumps(att)
                elif not att:
                    att = "[]"

                await db.execute("""
                    INSERT INTO ticket_messages (
                        ticket_id, message_id, author_id, author_name,
                        author_avatar, is_staff, content, attachments_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    int(ticket_id),
                    int(m["message_id"]),
                    int(m["author_id"]),
                    str(m.get("author_name", "Unknown")),
                    str(m.get("author_avatar")) if m.get("author_avatar") is not None else None,
                    1 if m.get("is_staff") else 0,
                    m.get("content", ""),
                    att,
                    str(m.get("created_at", ""))
                ))
                count += 1
            await db.commit()
            return count

    async def get_ticket_messages(self, ticket_id: int) -> List[Dict[str, Any]]:
        """Retrieves stored messages for a ticket ordered by creation time."""
        await self.init_db()
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT * FROM ticket_messages
                WHERE ticket_id = ?
                ORDER BY id ASC;
            """, (int(ticket_id),)) as cur:
                rows = await cur.fetchall()
                return [
                    {
                        "id": r["id"],
                        "ticket_id": r["ticket_id"],
                        "message_id": str(r["message_id"]),
                        "author_id": str(r["author_id"]),
                        "author_name": r["author_name"],
                        "author_avatar": r["author_avatar"],
                        "is_staff": bool(r["is_staff"]),
                        "content": r["content"] or "",
                        "attachments": _safe_json_loads(r["attachments_json"], []),
                        "created_at": r["created_at"]
                    }
                    for r in rows
                ]
