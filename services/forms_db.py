"""
Forms and Dynamic Survey Management Database Service.
Manages persistent storage in data/forms.db with SQLite WAL mode,
transaction safety, indexing, and connection pooling.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite

logger = logging.getLogger("FormsDB")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "forms.db")


class FormsDB:
    _instance: Optional["FormsDB"] = None

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "FormsDB":
        if cls._instance is None:
            cls._instance = cls(db_path or DB_PATH)
        return cls._instance

    @asynccontextmanager
    async def get_connection(self):
        """Yields a configured connection with WAL mode, busy timeout, and foreign keys."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            yield conn

    async def init_db(self):
        """Initializes tables, indices, and integrity constraints."""
        if self._initialized:
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with self.get_connection() as db:
            # 1. Forms master table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    panel_channel_id INTEGER,
                    panel_message_id INTEGER,
                    dest_channel_id INTEGER NOT NULL,
                    review_enabled BOOLEAN DEFAULT 0,
                    approved_role_id INTEGER,
                    is_enabled BOOLEAN DEFAULT 1,
                    opens_at INTEGER,
                    closes_at INTEGER,
                    is_closed BOOLEAN DEFAULT 0,
                    max_submissions_per_user INTEGER DEFAULT 0,
                    cooldown_seconds INTEGER DEFAULT 60,
                    created_by TEXT NOT NULL,
                    closed_report_sent BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_forms_guild ON forms(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_forms_panel_msg ON forms(panel_message_id);")

            # 2. Form custom questions table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS form_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    style TEXT DEFAULT 'short',
                    placeholder TEXT,
                    required BOOLEAN DEFAULT 1,
                    min_length INTEGER DEFAULT 0,
                    max_length INTEGER DEFAULT 1000,
                    position INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_form_questions_form ON form_questions(form_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_form_questions_pos ON form_questions(form_id, position);")

            # 3. Form submissions table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS form_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    user_discriminator TEXT DEFAULT '0',
                    user_avatar_url TEXT,
                    status TEXT DEFAULT 'submitted',
                    answers_json TEXT NOT NULL,
                    dest_message_id INTEGER,
                    reviewer_id INTEGER,
                    reviewer_name TEXT,
                    review_note TEXT,
                    reviewed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(form_id) REFERENCES forms(id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_form_subs_form ON form_submissions(form_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_form_subs_guild_user ON form_submissions(guild_id, user_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_form_subs_dest_msg ON form_submissions(dest_message_id);")

            await db.commit()

        self._initialized = True
        logger.info("FormsDB initialized successfully at %s", self.db_path)

    # =========================================================================
    # Forms CRUD Operations
    # =========================================================================

    async def create_form(
        self,
        guild_id: int,
        title: str,
        dest_channel_id: int,
        description: Optional[str] = None,
        panel_channel_id: Optional[int] = None,
        review_enabled: bool = False,
        approved_role_id: Optional[int] = None,
        is_enabled: bool = True,
        opens_at: Optional[int] = None,
        closes_at: Optional[int] = None,
        max_submissions_per_user: int = 0,
        cooldown_seconds: int = 60,
        created_by: str = "System"
    ) -> int:
        """Creates a new form and returns its generated form ID."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO forms (
                    guild_id, title, description, panel_channel_id, dest_channel_id,
                    review_enabled, approved_role_id, is_enabled, opens_at, closes_at,
                    max_submissions_per_user, cooldown_seconds, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                guild_id, title.strip(), description.strip() if description else None,
                panel_channel_id, dest_channel_id, 1 if review_enabled else 0,
                approved_role_id, 1 if is_enabled else 0, opens_at, closes_at,
                max(0, max_submissions_per_user), max(0, cooldown_seconds), created_by
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_form(self, guild_id: int, form_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single form enforcing strict guild scoping (Anti-IDOR)."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM forms WHERE id = ? AND guild_id = ?",
                (form_id, guild_id)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def get_form_any(self, form_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a form by ID without guild scoping (used by global DynamicItem callbacks)."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM forms WHERE id = ?", (form_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def get_form_by_panel_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a form matching a published Discord panel message ID."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM forms WHERE panel_message_id = ?", (message_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def list_forms(self, guild_id: int) -> List[Dict[str, Any]]:
        """Lists all forms for a guild along with question counts and submission stats."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT 
                    f.*,
                    (SELECT COUNT(*) FROM form_questions q WHERE q.form_id = f.id) AS question_count,
                    (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id) AS total_submissions,
                    (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id AND s.status = 'pending') AS pending_submissions,
                    (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id AND s.status = 'approved') AS approved_submissions,
                    (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id = f.id AND s.status = 'rejected') AS rejected_submissions
                FROM forms f
                WHERE f.guild_id = ?
                ORDER BY f.id DESC
            """, (guild_id,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_form(self, guild_id: int, form_id: int, **fields) -> bool:
        """Updates specific fields of a form under strict guild scoping."""
        await self.init_db()
        allowed_keys = {
            "title", "description", "panel_channel_id", "panel_message_id",
            "dest_channel_id", "review_enabled", "approved_role_id", "is_enabled",
            "opens_at", "closes_at", "is_closed", "max_submissions_per_user",
            "cooldown_seconds", "closed_report_sent"
        }
        updates = []
        params = []
        for key, val in fields.items():
            if key in allowed_keys:
                updates.append(f"{key} = ?")
                if isinstance(val, bool):
                    params.append(1 if val else 0)
                else:
                    params.append(val)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([form_id, guild_id])

        sql = f"UPDATE forms SET {', '.join(updates)} WHERE id = ? AND guild_id = ?"
        async with self.get_connection() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount > 0

    async def delete_form(self, guild_id: int, form_id: int) -> bool:
        """Deletes a form and cascades all its questions and submissions."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM forms WHERE id = ? AND guild_id = ?", (form_id, guild_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_panel_message(self, form_id: int, channel_id: int, message_id: int) -> bool:
        """Stores published Discord channel and message IDs for a form."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "UPDATE forms SET panel_channel_id = ?, panel_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (channel_id, message_id, form_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_form_closed(self, guild_id: int, form_id: int, is_closed: bool) -> bool:
        """Manually toggles the closed status of a form."""
        return await self.update_form(guild_id, form_id, is_closed=is_closed)

    async def mark_closed_report_sent(self, form_id: int) -> bool:
        """Marks that the automated expiration CSV report has been delivered."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "UPDATE forms SET closed_report_sent = 1 WHERE id = ?",
                (form_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_due_open_forms(self, current_time: int) -> List[Dict[str, Any]]:
        """Finds forms scheduled to open whose opens_at <= current_time < closes_at and are closed/scheduled."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM forms 
                WHERE is_enabled = 1 
                  AND is_closed = 0
                  AND opens_at IS NOT NULL 
                  AND opens_at <= ? 
                  AND (closes_at IS NULL OR closes_at > ?)
                  AND panel_message_id IS NOT NULL
            """, (current_time, current_time))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_due_close_forms(self, current_time: int) -> List[Dict[str, Any]]:
        """Finds active forms whose closes_at <= current_time but have not yet been marked closed."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM forms 
                WHERE is_enabled = 1 
                  AND is_closed = 0
                  AND closes_at IS NOT NULL 
                  AND closes_at <= ?
            """, (current_time,))
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # Questions CRUD Operations
    # =========================================================================

    async def add_question(
        self,
        form_id: int,
        label: str,
        style: str = "short",
        placeholder: Optional[str] = None,
        required: bool = True,
        min_length: int = 0,
        max_length: int = 1000,
        position: Optional[int] = None
    ) -> int:
        """Adds a question to a form. Automatically determines next position if unset."""
        await self.init_db()
        # Cap limits to ensure Discord embed safety (max_length <= 1000)
        safe_min = max(0, min(min_length, 1000))
        safe_max = max(1, min(max_length, 1000))
        if safe_min > safe_max:
            safe_min = safe_max

        safe_label = label.strip()[:45]
        safe_placeholder = (placeholder.strip()[:100]) if placeholder else None
        safe_style = "paragraph" if style.lower() in ("paragraph", "long", "textarea") else "short"

        async with self.get_connection() as db:
            if position is None:
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM form_questions WHERE form_id = ?",
                    (form_id,)
                )
                pos_row = await cursor.fetchone()
                target_pos = pos_row[0] if pos_row else 1
            else:
                target_pos = max(1, position)

            cursor = await db.execute("""
                INSERT INTO form_questions (
                    form_id, label, style, placeholder, required, min_length, max_length, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                form_id, safe_label, safe_style, safe_placeholder,
                1 if required else 0, safe_min, safe_max, target_pos
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_questions(self, form_id: int) -> List[Dict[str, Any]]:
        """Retrieves all questions for a form ordered by position."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM form_questions WHERE form_id = ? ORDER BY position ASC, id ASC",
                (form_id,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_question(self, form_id: int, question_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single question scoped by form_id."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM form_questions WHERE id = ? AND form_id = ?",
                (question_id, form_id)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def update_question(self, form_id: int, question_id: int, **fields) -> bool:
        """Updates a question's attributes."""
        await self.init_db()
        allowed = {"label", "style", "placeholder", "required", "min_length", "max_length", "position"}
        updates = []
        params = []
        for key, val in fields.items():
            if key in allowed:
                if key == "label" and val:
                    val = str(val).strip()[:45]
                elif key == "placeholder" and val:
                    val = str(val).strip()[:100]
                elif key == "style" and val:
                    val = "paragraph" if str(val).lower() in ("paragraph", "long", "textarea") else "short"
                elif key == "required":
                    val = 1 if val else 0
                elif key in ("min_length", "max_length"):
                    val = max(0, min(int(val), 1000))
                elif key == "position":
                    val = max(1, int(val))

                updates.append(f"{key} = ?")
                params.append(val)

        if not updates:
            return False

        params.extend([question_id, form_id])
        sql = f"UPDATE form_questions SET {', '.join(updates)} WHERE id = ? AND form_id = ?"
        async with self.get_connection() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount > 0

    async def delete_question(self, form_id: int, question_id: int) -> bool:
        """Deletes a question and adjusts remaining question positions."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM form_questions WHERE id = ? AND form_id = ?",
                (question_id, form_id)
            )
            deleted = cursor.rowcount > 0
            if deleted:
                # Re-normalize positions: 1, 2, 3...
                cur = await db.execute(
                    "SELECT id FROM form_questions WHERE form_id = ? ORDER BY position ASC, id ASC",
                    (form_id,)
                )
                rows = await cur.fetchall()
                for idx, r in enumerate(rows, start=1):
                    await db.execute("UPDATE form_questions SET position = ? WHERE id = ?", (idx, r[0]))
                await db.commit()
            return deleted

    async def reorder_questions(self, form_id: int, question_ids: List[int]) -> bool:
        """Updates question order based on an ordered list of question IDs."""
        await self.init_db()
        async with self.get_connection() as db:
            for position, qid in enumerate(question_ids, start=1):
                await db.execute(
                    "UPDATE form_questions SET position = ? WHERE id = ? AND form_id = ?",
                    (position, qid, form_id)
                )
            await db.commit()
            return True

    # =========================================================================
    # Submissions CRUD Operations
    # =========================================================================

    async def create_submission(
        self,
        form_id: int,
        guild_id: int,
        user_id: int,
        user_name: str,
        user_discriminator: str,
        user_avatar_url: Optional[str],
        answers: Dict[str, Any],
        status: str = "submitted",
        dest_message_id: Optional[int] = None
    ) -> int:
        """Creates a new submission record."""
        await self.init_db()
        answers_str = json.dumps(answers, ensure_ascii=False)
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO form_submissions (
                    form_id, guild_id, user_id, user_name, user_discriminator,
                    user_avatar_url, status, answers_json, dest_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                form_id, guild_id, user_id, user_name, user_discriminator,
                user_avatar_url, status, answers_str, dest_message_id
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_submission(self, sub_id: int, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Retrieves a single submission, optionally scoped by guild_id."""
        await self.init_db()
        async with self.get_connection() as db:
            if guild_id is not None:
                cursor = await db.execute(
                    "SELECT * FROM form_submissions WHERE id = ? AND guild_id = ?",
                    (sub_id, guild_id)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM form_submissions WHERE id = ?",
                    (sub_id,)
                )
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["answers"] = json.loads(res.get("answers_json") or "{}")
            except Exception:
                res["answers"] = {}
            return res

    async def get_submission_by_dest_msg(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a submission by its destination Discord message ID."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM form_submissions WHERE dest_message_id = ?",
                (message_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["answers"] = json.loads(res.get("answers_json") or "{}")
            except Exception:
                res["answers"] = {}
            return res

    async def list_submissions(
        self,
        guild_id: int,
        form_id: int,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Lists submissions for a form with optional status filter and pagination."""
        await self.init_db()
        async with self.get_connection() as db:
            sql = "SELECT * FROM form_submissions WHERE form_id = ? AND guild_id = ?"
            params: List[Any] = [form_id, guild_id]
            if status and status.lower() != "all":
                sql += " AND status = ?"
                params.append(status.lower())

            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([max(1, limit), max(0, offset)])

            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["answers"] = json.loads(item.get("answers_json") or "{}")
                except Exception:
                    item["answers"] = {}
                results.append(item)
            return results

    async def count_user_submissions(self, form_id: int, user_id: int) -> int:
        """Counts how many submissions a specific user has made for a given form."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM form_submissions WHERE form_id = ? AND user_id = ?",
                (form_id, user_id)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_last_user_submission_time(self, form_id: int, user_id: int) -> Optional[datetime]:
        """Gets timestamp of the user's most recent submission for cooldown calculation."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT created_at FROM form_submissions WHERE form_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
                (form_id, user_id)
            )
            row = await cursor.fetchone()
            if not row or not row[0]:
                return None
            try:
                # Handle SQLite timestamp parsing
                ts_str = row[0]
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                return None

    async def get_submission_stats(self, guild_id: int, form_id: int) -> Dict[str, int]:
        """Aggregates submission statistics for a form."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT 
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) AS submitted
                FROM form_submissions
                WHERE form_id = ? AND guild_id = ?
            """, (form_id, guild_id))
            row = await cursor.fetchone()
            if not row:
                return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "submitted": 0}
            return {
                "total": row[0] or 0,
                "pending": row[1] or 0,
                "approved": row[2] or 0,
                "rejected": row[3] or 0,
                "submitted": row[4] or 0
            }

    async def update_submission_status(
        self,
        sub_id: int,
        status: str,
        reviewer_id: int,
        reviewer_name: str,
        review_note: Optional[str] = None
    ) -> bool:
        """Updates review status of a submission."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                UPDATE form_submissions 
                SET status = ?, reviewer_id = ?, reviewer_name = ?, review_note = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, reviewer_id, reviewer_name, review_note, sub_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_submission_dest_msg(self, sub_id: int, message_id: int) -> bool:
        """Updates the destination message ID of a submission."""
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "UPDATE form_submissions SET dest_message_id = ? WHERE id = ?",
                (message_id, sub_id)
            )
            await db.commit()
            return cursor.rowcount > 0
