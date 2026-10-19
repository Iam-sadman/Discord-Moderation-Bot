"""
Meetings Management Database Service.
Manages persistent storage in data/meetings.db with SQLite WAL mode,
transaction safety, indexing, foreign key constraints, and singleton pattern.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import aiosqlite

from config import Config

logger = logging.getLogger("MeetingsDB")

DEFAULT_DB_PATH = getattr(
    Config, "MEETINGS_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "meetings.db")
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


class MeetingsDB:
    _instance: Optional["MeetingsDB"] = None

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "MeetingsDB":
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
            # 1. Meeting Settings per Guild
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_settings (
                    guild_id INTEGER PRIMARY KEY,
                    default_output_channel_id INTEGER,
                    allowed_role_ids TEXT DEFAULT '[]'
                );
            """)

            # 2. Meetings (main records)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meetings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    project_code TEXT,
                    voice_channel_id INTEGER NOT NULL,
                    voice_channel_name TEXT NOT NULL,
                    output_channel_id INTEGER NOT NULL,
                    output_message_id INTEGER,
                    host_id INTEGER NOT NULL,
                    host_name TEXT NOT NULL,
                    status TEXT DEFAULT 'recording',
                    started_at TIMESTAMP NOT NULL,
                    ended_at TIMESTAMP,
                    duration_seconds INTEGER DEFAULT 0,
                    executive_summary TEXT,
                    key_points_json TEXT,
                    action_items_json TEXT,
                    qa_decisions_json TEXT,
                    full_transcript TEXT,
                    error_message TEXT
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_meetings_guild ON meetings(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_meetings_guild_status ON meetings(guild_id, status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_meetings_started ON meetings(started_at DESC);")

            # 3. Meeting Participants (Attendance Tracking)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    joined_at TIMESTAMP NOT NULL,
                    left_at TIMESTAMP,
                    total_seconds INTEGER DEFAULT 0,
                    FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_mp_meeting ON meeting_participants(meeting_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_mp_user ON meeting_participants(user_id);")

            await db.commit()
        self._initialized = True
        logger.info("[OK] MeetingsDB initialized successfully.")

    # =========================================================================
    # Meeting Settings
    # =========================================================================

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Returns guild meeting settings."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM meeting_settings WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "guild_id": row["guild_id"],
                    "default_output_channel_id": row["default_output_channel_id"],
                    "allowed_role_ids": _safe_json_loads(row["allowed_role_ids"], [])
                }
            return {
                "guild_id": guild_id,
                "default_output_channel_id": None,
                "allowed_role_ids": []
            }

    async def save_settings(self, guild_id: int, output_channel_id: Optional[int] = None,
                            allowed_role_ids: Optional[List[int]] = None):
        """Saves or updates guild meeting settings."""
        current = await self.get_settings(guild_id)
        if output_channel_id is not None:
            current["default_output_channel_id"] = output_channel_id
        if allowed_role_ids is not None:
            current["allowed_role_ids"] = allowed_role_ids

        roles_json = json.dumps(current["allowed_role_ids"])
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO meeting_settings (guild_id, default_output_channel_id, allowed_role_ids)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    default_output_channel_id = excluded.default_output_channel_id,
                    allowed_role_ids = excluded.allowed_role_ids
            """, (guild_id, current["default_output_channel_id"], roles_json))
            await db.commit()

    # =========================================================================
    # Meeting CRUD
    # =========================================================================

    async def create_meeting(self, guild_id: int, title: str, voice_channel_id: int,
                             voice_channel_name: str, output_channel_id: int,
                             host_id: int, host_name: str,
                             project_code: Optional[str] = None) -> int:
        """Creates a new meeting record and returns its ID."""
        now = datetime.now(timezone.utc).isoformat()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO meetings (guild_id, title, project_code, voice_channel_id,
                    voice_channel_name, output_channel_id, host_id, host_name,
                    status, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recording', ?)
            """, (guild_id, title, project_code, voice_channel_id,
                  voice_channel_name, output_channel_id, host_id, host_name, now))
            await db.commit()
            return cursor.lastrowid

    async def get_meeting(self, meeting_id: int) -> Optional[Dict[str, Any]]:
        """Returns a meeting record by ID."""
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_meeting(row)

    async def get_active_meeting(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Returns the currently active (recording) meeting for a guild, if any."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM meetings WHERE guild_id = ? AND status = 'recording' ORDER BY id DESC LIMIT 1",
                (guild_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_meeting(row)

    async def update_meeting_status(self, meeting_id: int, status: str,
                                    ended_at: Optional[str] = None,
                                    duration_seconds: int = 0,
                                    error_message: Optional[str] = None):
        """Updates meeting status and timing information."""
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE meetings SET status = ?, ended_at = ?,
                    duration_seconds = ?, error_message = ?
                WHERE id = ?
            """, (status, ended_at, duration_seconds, error_message, meeting_id))
            await db.commit()

    async def save_meeting_results(self, meeting_id: int, executive_summary: str,
                                   key_points: List[str], action_items: List[str],
                                   qa_decisions: List[str], full_transcript: str,
                                   output_message_id: Optional[int] = None):
        """Saves AI-generated meeting minutes and transcript."""
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE meetings SET
                    status = 'completed',
                    executive_summary = ?,
                    key_points_json = ?,
                    action_items_json = ?,
                    qa_decisions_json = ?,
                    full_transcript = ?,
                    output_message_id = ?
                WHERE id = ?
            """, (
                executive_summary,
                json.dumps(key_points, ensure_ascii=False),
                json.dumps(action_items, ensure_ascii=False),
                json.dumps(qa_decisions, ensure_ascii=False),
                full_transcript,
                output_message_id,
                meeting_id
            ))
            await db.commit()

    async def list_meetings(self, guild_id: int, status: Optional[str] = None,
                            limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Lists meetings for a guild with optional status filter and pagination."""
        async with self.get_connection() as db:
            # Count total
            if status:
                count_cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM meetings WHERE guild_id = ? AND status = ?",
                    (guild_id, status)
                )
            else:
                count_cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM meetings WHERE guild_id = ?",
                    (guild_id,)
                )
            total = (await count_cur.fetchone())["cnt"]

            # Fetch page
            if status:
                cursor = await db.execute(
                    """SELECT * FROM meetings WHERE guild_id = ? AND status = ?
                       ORDER BY started_at DESC LIMIT ? OFFSET ?""",
                    (guild_id, status, limit, offset)
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM meetings WHERE guild_id = ?
                       ORDER BY started_at DESC LIMIT ? OFFSET ?""",
                    (guild_id, limit, offset)
                )
            rows = await cursor.fetchall()
            meetings = [self._row_to_meeting(r) for r in rows]

            return {"meetings": meetings, "total": total, "limit": limit, "offset": offset}

    async def get_guild_stats(self, guild_id: int) -> Dict[str, Any]:
        """Returns aggregate meeting statistics for a guild."""
        async with self.get_connection() as db:
            total_cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM meetings WHERE guild_id = ?", (guild_id,)
            )
            total = (await total_cur.fetchone())["cnt"]

            active_cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM meetings WHERE guild_id = ? AND status = 'recording'",
                (guild_id,)
            )
            active = (await active_cur.fetchone())["cnt"]

            completed_cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM meetings WHERE guild_id = ? AND status = 'completed'",
                (guild_id,)
            )
            completed = (await completed_cur.fetchone())["cnt"]

            participants_cur = await db.execute(
                """SELECT COUNT(DISTINCT mp.user_id) as cnt
                   FROM meeting_participants mp
                   JOIN meetings m ON mp.meeting_id = m.id
                   WHERE m.guild_id = ?""",
                (guild_id,)
            )
            unique_participants = (await participants_cur.fetchone())["cnt"]

            return {
                "total_meetings": total,
                "active_recordings": active,
                "completed_meetings": completed,
                "unique_participants": unique_participants
            }

    def _row_to_meeting(self, row) -> Dict[str, Any]:
        """Converts a database row to a meeting dictionary with string-serialized IDs."""
        return {
            "id": row["id"],
            "guild_id": str(row["guild_id"]),
            "title": row["title"],
            "project_code": row["project_code"],
            "voice_channel_id": str(row["voice_channel_id"]),
            "voice_channel_name": row["voice_channel_name"],
            "output_channel_id": str(row["output_channel_id"]),
            "output_message_id": str(row["output_message_id"]) if row["output_message_id"] else None,
            "host_id": str(row["host_id"]),
            "host_name": row["host_name"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_seconds": row["duration_seconds"] or 0,
            "executive_summary": row["executive_summary"],
            "key_points": _safe_json_loads(row["key_points_json"], []),
            "action_items": _safe_json_loads(row["action_items_json"], []),
            "qa_decisions": _safe_json_loads(row["qa_decisions_json"], []),
            "full_transcript": row["full_transcript"],
            "error_message": row["error_message"]
        }

    # =========================================================================
    # Participant / Attendance Tracking
    # =========================================================================

    async def add_participant(self, meeting_id: int, user_id: int, user_name: str) -> int:
        """Adds a participant record when they join. Returns the participant row ID."""
        now = datetime.now(timezone.utc).isoformat()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO meeting_participants (meeting_id, user_id, user_name, joined_at)
                VALUES (?, ?, ?, ?)
            """, (meeting_id, user_id, user_name, now))
            await db.commit()
            return cursor.lastrowid

    async def mark_participant_left(self, meeting_id: int, user_id: int):
        """Marks a participant as having left the meeting and calculates total_seconds."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self.get_connection() as db:
            # Find the most recent un-departed record for this user in this meeting
            cursor = await db.execute("""
                SELECT id, joined_at FROM meeting_participants
                WHERE meeting_id = ? AND user_id = ? AND left_at IS NULL
                ORDER BY id DESC LIMIT 1
            """, (meeting_id, user_id))
            row = await cursor.fetchone()
            if row:
                joined = datetime.fromisoformat(row["joined_at"]).replace(tzinfo=timezone.utc)
                total_sec = max(0, int((now - joined).total_seconds()))
                await db.execute("""
                    UPDATE meeting_participants SET left_at = ?, total_seconds = ?
                    WHERE id = ?
                """, (now_iso, total_sec, row["id"]))
                await db.commit()

    async def finalize_all_participants(self, meeting_id: int):
        """Marks all still-present participants as left at meeting end time."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT id, joined_at FROM meeting_participants
                WHERE meeting_id = ? AND left_at IS NULL
            """, (meeting_id,))
            rows = await cursor.fetchall()
            for row in rows:
                joined = datetime.fromisoformat(row["joined_at"]).replace(tzinfo=timezone.utc)
                total_sec = max(0, int((now - joined).total_seconds()))
                await db.execute("""
                    UPDATE meeting_participants SET left_at = ?, total_seconds = ?
                    WHERE id = ?
                """, (now_iso, total_sec, row["id"]))
            await db.commit()

    async def get_participants(self, meeting_id: int) -> List[Dict[str, Any]]:
        """Returns all participants for a meeting."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM meeting_participants
                WHERE meeting_id = ? ORDER BY joined_at ASC
            """, (meeting_id,))
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "meeting_id": r["meeting_id"],
                    "user_id": str(r["user_id"]),
                    "user_name": r["user_name"],
                    "joined_at": r["joined_at"],
                    "left_at": r["left_at"],
                    "total_seconds": r["total_seconds"] or 0
                }
                for r in rows
            ]

    async def delete_meeting(self, meeting_id: int) -> bool:
        """Deletes a meeting and cascades to participants."""
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            await db.commit()
            return cursor.rowcount > 0
