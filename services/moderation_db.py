"""
Moderation & Role Management Database Service.
Manages persistent storage in data/moderation.db with SQLite WAL mode,
transaction safety, indexing, and connection pooling.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite

logger = logging.getLogger("ModerationDB")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "moderation.db")


class ModerationDB:
    _instance: Optional["ModerationDB"] = None

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "ModerationDB":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @asynccontextmanager
    async def get_connection(self):
        """Yields a configured connection with WAL and busy timeout."""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=30000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            yield conn

    async def init_db(self):
        """Initializes tables, indexes, and default records."""
        if self._initialized:
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with self.get_connection() as db:
            # 1. Role Panels
            await db.execute("""
                CREATE TABLE IF NOT EXISTS role_panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    title TEXT NOT NULL,
                    description TEXT,
                    color TEXT DEFAULT '#5865F2',
                    image_url TEXT,
                    thumbnail_url TEXT,
                    is_enabled BOOLEAN DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_role_panels_guild ON role_panels(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_role_panels_msg ON role_panels(message_id);")

            # 2. Role Panel Items
            await db.execute("""
                CREATE TABLE IF NOT EXISTS role_panel_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL,           -- 'button' or 'reaction'
                    identifier TEXT NOT NULL,          -- custom_id or emoji
                    role_id INTEGER NOT NULL,
                    label TEXT,
                    style TEXT DEFAULT 'secondary',    -- primary, secondary, success, danger
                    emoji TEXT,
                    toggle BOOLEAN DEFAULT 1,
                    position INTEGER DEFAULT 0,
                    FOREIGN KEY(panel_id) REFERENCES role_panels(id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_panel_items_panel ON role_panel_items(panel_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_panel_items_ident ON role_panel_items(identifier);")

            # 3. Allowed Self-Service Roles Whitelist
            await db.execute("""
                CREATE TABLE IF NOT EXISTS allowed_self_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    added_by TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(guild_id, role_id)
                );
            """)

            # 4. Message Templates
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,         -- 'role_assigned', 'role_removed', 'timeout', 'kick', 'ban', 'tempban', 'unban'
                    template_text TEXT NOT NULL,
                    is_enabled BOOLEAN DEFAULT 1,
                    updated_by TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, action_type)
                );
            """)

            # 5. Moderation Actions History & Idempotency
            await db.execute("""
                CREATE TABLE IF NOT EXISTS moderation_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    guild_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    target_name TEXT,
                    actor_id TEXT NOT NULL,
                    actor_name TEXT,
                    action_type TEXT NOT NULL,
                    duration_seconds INTEGER,
                    reason TEXT,
                    dm_status TEXT DEFAULT 'not_requested',
                    dm_error TEXT,
                    status TEXT DEFAULT 'queued',       -- 'queued', 'running', 'completed', 'failed', 'cancelled'
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    executed_at TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_mod_actions_guild_target ON moderation_actions(guild_id, target_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_mod_actions_status ON moderation_actions(status);")

            # 6. Scheduled Actions (Temporary Bans & Delayed Operations)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,         -- 'unban_tempban', 'remove_temprole'
                    guild_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    reason TEXT,
                    trigger_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'pending',     -- 'pending', 'executed', 'cancelled', 'superseded', 'failed'
                    version_id INTEGER DEFAULT 1,
                    related_action_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    executed_at TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sched_pending ON scheduled_actions(status, trigger_at);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sched_guild_target ON scheduled_actions(guild_id, target_id);")

            # 7. Bulk Moderation Jobs
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bulk_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    guild_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_name TEXT,
                    reason TEXT,
                    role_id INTEGER,
                    duration_seconds INTEGER,
                    dm_template_used TEXT,
                    filter_criteria TEXT NOT NULL,
                    total_targets INTEGER DEFAULT 0,
                    completed_targets INTEGER DEFAULT 0,
                    failed_targets INTEGER DEFAULT 0,
                    skipped_targets INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'queued',       -- 'queued', 'running', 'completed', 'partially_completed', 'cancelled', 'failed'
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bulk_jobs_guild ON bulk_jobs(guild_id);")

            # 8. Bulk Job Targets
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bulk_job_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    target_name TEXT,
                    status TEXT DEFAULT 'pending',     -- 'pending', 'success', 'failed', 'skipped'
                    error TEXT,
                    dm_status TEXT DEFAULT 'not_requested',
                    executed_at TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES bulk_jobs(job_id) ON DELETE CASCADE
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bulk_targets_job ON bulk_job_targets(job_id);")

            # 9. Moderation Audit Records
            await db.execute("""
                CREATE TABLE IF NOT EXISTS moderation_audit_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_name TEXT,
                    target_id INTEGER,
                    target_name TEXT,
                    action_type TEXT NOT NULL,
                    role_id INTEGER,
                    reason TEXT,
                    outcome TEXT NOT NULL,             -- 'SUCCESS', 'FAILED', 'DENIED', 'CANCELLED'
                    dm_status TEXT,
                    bulk_job_id TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_mod_audit_lookup ON moderation_audit_records(guild_id, action_type, created_at DESC);")

            # 10. Dashboard Action Permissions
            await db.execute("""
                CREATE TABLE IF NOT EXISTS dashboard_action_permissions (
                    guild_id INTEGER NOT NULL,
                    dashboard_role TEXT NOT NULL,      -- 'Administrator', 'Moderator', 'Viewer'
                    can_manage_roles BOOLEAN DEFAULT 0,
                    can_create_panels BOOLEAN DEFAULT 0,
                    can_timeout BOOLEAN DEFAULT 0,
                    can_kick BOOLEAN DEFAULT 0,
                    can_ban BOOLEAN DEFAULT 0,
                    can_tempban BOOLEAN DEFAULT 0,
                    can_unban BOOLEAN DEFAULT 0,
                    can_bulk_mod BOOLEAN DEFAULT 0,
                    can_edit_templates BOOLEAN DEFAULT 0,
                    can_emergency_stop BOOLEAN DEFAULT 0,
                    PRIMARY KEY(guild_id, dashboard_role)
                );
            """)

            # 11. Guild Emergency Controls
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_emergency_controls (
                    guild_id INTEGER PRIMARY KEY,
                    is_locked BOOLEAN DEFAULT 0,
                    triggered_by TEXT,
                    triggered_at TIMESTAMP,
                    reason TEXT
                );
            """)

            await db.commit()
            self._initialized = True
            logger.info("Initialized moderation database at %s", self.db_path)

    # =========================================================================
    # Role Panels & Items
    # =========================================================================

    async def create_role_panel(self, guild_id: int, channel_id: int, title: str, description: str,
                                color: str, created_by: str, image_url: str = "", thumbnail_url: str = "") -> int:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO role_panels (guild_id, channel_id, title, description, color, image_url, thumbnail_url, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, channel_id, title, description, color, image_url, thumbnail_url, created_by))
            panel_id = cursor.lastrowid
            await db.commit()
            return panel_id

    async def update_role_panel(self, panel_id: int, title: str, description: str, color: str,
                                image_url: str = "", thumbnail_url: str = "", is_enabled: bool = True,
                                channel_id: Optional[int] = None):
        await self.init_db()
        async with self.get_connection() as db:
            if channel_id is not None:
                await db.execute("""
                    UPDATE role_panels
                    SET title = ?, description = ?, color = ?, image_url = ?, thumbnail_url = ?, is_enabled = ?, channel_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (title, description, color, image_url, thumbnail_url, is_enabled, channel_id, panel_id))
            else:
                await db.execute("""
                    UPDATE role_panels
                    SET title = ?, description = ?, color = ?, image_url = ?, thumbnail_url = ?, is_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (title, description, color, image_url, thumbnail_url, is_enabled, panel_id))
            await db.commit()

    async def set_panel_message_id(self, panel_id: int, message_id: int):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("UPDATE role_panels SET message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (message_id, panel_id))
            await db.commit()

    async def delete_role_panel(self, panel_id: int) -> Optional[Dict[str, Any]]:
        await self.init_db()
        panel = await self.get_role_panel(panel_id)
        if not panel:
            return None
        async with self.get_connection() as db:
            await db.execute("DELETE FROM role_panels WHERE id = ?", (panel_id,))
            await db.commit()
        return panel

    async def get_role_panel(self, panel_id: int) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM role_panels WHERE id = ?", (panel_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            panel = dict(row)
            items_cur = await db.execute("SELECT * FROM role_panel_items WHERE panel_id = ? ORDER BY position ASC, id ASC", (panel_id,))
            panel["items"] = [dict(r) for r in await items_cur.fetchall()]
            return panel

    async def get_role_panel_by_message_id(self, message_id: int) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM role_panels WHERE message_id = ?", (message_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            panel = dict(row)
            items_cur = await db.execute("SELECT * FROM role_panel_items WHERE panel_id = ? ORDER BY position ASC, id ASC", (panel["id"],))
            panel["items"] = [dict(r) for r in await items_cur.fetchall()]
            return panel

    async def list_role_panels(self, guild_id: int) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM role_panels WHERE guild_id = ? ORDER BY id DESC", (guild_id,))
            rows = await cursor.fetchall()
            panels = []
            for r in rows:
                p = dict(r)
                items_cur = await db.execute("SELECT * FROM role_panel_items WHERE panel_id = ? ORDER BY position ASC, id ASC", (p["id"],))
                p["items"] = [dict(item) for item in await items_cur.fetchall()]
                panels.append(p)
            return panels

    async def set_panel_items(self, panel_id: int, items: List[Dict[str, Any]]):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("DELETE FROM role_panel_items WHERE panel_id = ?", (panel_id,))
            for idx, item in enumerate(items):
                item_type = item.get("item_type", "button")
                identifier = item.get("identifier") or (f"role_panel:{panel_id}:{idx+1}" if item_type == "button" else item.get("emoji", ""))
                role_id = int(item["role_id"])
                label = item.get("label", "")
                style = item.get("style", "secondary")
                emoji = item.get("emoji", "")
                toggle = bool(item.get("toggle", True))
                await db.execute("""
                    INSERT INTO role_panel_items (panel_id, item_type, identifier, role_id, label, style, emoji, toggle, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (panel_id, item_type, identifier, role_id, label, style, emoji, toggle, idx))
            await db.commit()

    async def get_panel_item_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM role_panel_items WHERE identifier = ?", (identifier,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Allowed Self-Service Roles Whitelist
    # =========================================================================

    async def get_allowed_self_roles(self, guild_id: int) -> List[int]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT role_id FROM allowed_self_roles WHERE guild_id = ?", (guild_id,))
            rows = await cursor.fetchall()
            return [int(r["role_id"]) for r in rows]

    async def is_role_self_assign_allowed(self, guild_id: int, role_id: int) -> bool:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT 1 FROM allowed_self_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
            return (await cursor.fetchone()) is not None

    async def add_allowed_self_role(self, guild_id: int, role_id: int, added_by: str) -> bool:
        await self.init_db()
        async with self.get_connection() as db:
            try:
                await db.execute("INSERT INTO allowed_self_roles (guild_id, role_id, added_by) VALUES (?, ?, ?)",
                                 (guild_id, role_id, added_by))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_allowed_self_role(self, guild_id: int, role_id: int) -> bool:
        await self.init_db()
        async with self.get_connection() as db:
            cur = await db.execute("DELETE FROM allowed_self_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
            await db.commit()
            return cur.rowcount > 0

    # =========================================================================
    # Message DM Templates
    # =========================================================================

    DEFAULT_TEMPLATES = {
        "role_assigned": "Hello {user}, you have been assigned the **{role}** role in **{server}**.",
        "role_removed": "Hello {user}, your **{role}** role in **{server}** has been removed.",
        "timeout": "You have been timed out in **{server}** for {duration}.\nReason: {reason}",
        "kick": "You have been kicked from **{server}**.\nReason: {reason}",
        "ban": "You have been permanently banned from **{server}**.\nReason: {reason}",
        "tempban": "You have been temporarily banned from **{server}** for {duration}.\nReason: {reason}",
        "unban": "You have been unbanned from **{server}**."
    }

    async def get_message_template(self, guild_id: int, action_type: str) -> Dict[str, Any]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM message_templates WHERE guild_id = ? AND action_type = ?",
                (guild_id, action_type)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {
                "guild_id": guild_id,
                "action_type": action_type,
                "template_text": self.DEFAULT_TEMPLATES.get(action_type, "Notification from {server}: {action}"),
                "is_enabled": True,
                "updated_by": "System Default"
            }

    async def set_message_template(self, guild_id: int, action_type: str, template_text: str,
                                   is_enabled: bool, updated_by: str):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO message_templates (guild_id, action_type, template_text, is_enabled, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, action_type) DO UPDATE SET
                    template_text = excluded.template_text,
                    is_enabled = excluded.is_enabled,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, action_type, template_text, is_enabled, updated_by))
            await db.commit()

    async def get_all_message_templates(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        await self.init_db()
        result = {}
        for action in self.DEFAULT_TEMPLATES.keys():
            result[action] = await self.get_message_template(guild_id, action)
        return result

    # =========================================================================
    # Moderation Actions & Idempotency
    # =========================================================================

    async def record_action_queued(self, idempotency_key: str, guild_id: int, target_id: int,
                                   target_name: str, actor_id: str, actor_name: str, action_type: str,
                                   duration_seconds: Optional[int], reason: str, dm_status: str = "not_requested") -> int:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO moderation_actions (
                    idempotency_key, guild_id, target_id, target_name, actor_id, actor_name,
                    action_type, duration_seconds, reason, dm_status, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
            """, (idempotency_key, guild_id, target_id, target_name, actor_id, actor_name, action_type, duration_seconds, reason, dm_status))
            action_id = cursor.lastrowid
            await db.commit()
            return action_id

    async def get_action_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM moderation_actions WHERE idempotency_key = ?", (key,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_action_status(self, action_id: int, status: str, dm_status: Optional[str] = None,
                                   dm_error: Optional[str] = None, error_message: Optional[str] = None):
        await self.init_db()
        async with self.get_connection() as db:
            updates = ["status = ?", "executed_at = CURRENT_TIMESTAMP"]
            params = [status]
            if dm_status is not None:
                updates.append("dm_status = ?")
                params.append(dm_status)
            if dm_error is not None:
                updates.append("dm_error = ?")
                params.append(dm_error)
            if error_message is not None:
                updates.append("error_message = ?")
                params.append(error_message)
            params.append(action_id)

            await db.execute(f"UPDATE moderation_actions SET {', '.join(updates)} WHERE id = ?", params)
            await db.commit()

    # =========================================================================
    # Scheduled Actions (Temporary Bans & Delayed Operations)
    # =========================================================================

    async def create_scheduled_action(self, action_type: str, guild_id: int, target_id: int,
                                      actor_id: str, reason: str, trigger_at: str,
                                      related_action_id: Optional[int] = None) -> int:
        await self.init_db()
        async with self.get_connection() as db:
            # Invalidate any older pending jobs for this target and action_type
            await db.execute("""
                UPDATE scheduled_actions
                SET status = 'superseded'
                WHERE guild_id = ? AND target_id = ? AND action_type = ? AND status = 'pending'
            """, (guild_id, target_id, action_type))

            cursor = await db.execute("""
                INSERT INTO scheduled_actions (action_type, guild_id, target_id, actor_id, reason, trigger_at, status, related_action_id)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (action_type, guild_id, target_id, actor_id, reason, trigger_at, related_action_id))
            job_id = cursor.lastrowid
            await db.commit()
            return job_id

    async def get_due_scheduled_actions(self) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM scheduled_actions
                WHERE status = 'pending' AND datetime(trigger_at) <= datetime('now')
                ORDER BY datetime(trigger_at) ASC
            """)
            return [dict(r) for r in await cursor.fetchall()]

    async def get_all_pending_scheduled_actions(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            if guild_id:
                cursor = await db.execute("""
                    SELECT * FROM scheduled_actions
                    WHERE status = 'pending' AND guild_id = ?
                    ORDER BY datetime(trigger_at) ASC
                """, (guild_id,))
            else:
                cursor = await db.execute("""
                    SELECT * FROM scheduled_actions
                    WHERE status = 'pending'
                    ORDER BY datetime(trigger_at) ASC
                """)
            return [dict(r) for r in await cursor.fetchall()]

    async def update_scheduled_action_status(self, job_id: int, status: str):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE scheduled_actions
                SET status = ?, executed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, job_id))
            await db.commit()

    async def cancel_scheduled_actions_for_target(self, guild_id: int, target_id: int, action_type: str = "unban_tempban"):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE scheduled_actions
                SET status = 'cancelled', executed_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND target_id = ? AND action_type = ? AND status = 'pending'
            """, (guild_id, target_id, action_type))
            await db.commit()

    # =========================================================================
    # Bulk Jobs & Targets
    # =========================================================================

    async def create_bulk_job(self, job_id: str, guild_id: int, action_type: str, actor_id: str,
                              actor_name: str, reason: str, filter_criteria: Dict[str, Any],
                              total_targets: int, role_id: Optional[int] = None,
                              duration_seconds: Optional[int] = None, dm_template_used: str = "") -> int:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO bulk_jobs (
                    job_id, guild_id, action_type, actor_id, actor_name, reason, role_id,
                    duration_seconds, dm_template_used, filter_criteria, total_targets, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
            """, (job_id, guild_id, action_type, actor_id, actor_name, reason, role_id,
                  duration_seconds, dm_template_used, json.dumps(filter_criteria), total_targets))
            pk = cursor.lastrowid
            await db.commit()
            return pk

    async def populate_bulk_targets(self, job_id: str, targets: List[Tuple[int, str]]):
        await self.init_db()
        async with self.get_connection() as db:
            for tid, tname in targets:
                await db.execute("""
                    INSERT INTO bulk_job_targets (job_id, target_id, target_name, status)
                    VALUES (?, ?, ?, 'pending')
                """, (job_id, tid, tname))
            await db.commit()

    async def update_bulk_target_result(self, job_id: str, target_id: int, status: str,
                                       error: Optional[str] = None, dm_status: str = "not_requested"):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE bulk_job_targets
                SET status = ?, error = ?, dm_status = ?, executed_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND target_id = ?
            """, (status, error, dm_status, job_id, target_id))

            # Update aggregated counters on bulk_jobs
            if status == "success":
                await db.execute("UPDATE bulk_jobs SET completed_targets = completed_targets + 1, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
            elif status == "failed":
                await db.execute("UPDATE bulk_jobs SET failed_targets = failed_targets + 1, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
            elif status == "skipped":
                await db.execute("UPDATE bulk_jobs SET skipped_targets = skipped_targets + 1, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
            await db.commit()

    async def update_bulk_job_status(self, job_id: str, status: str):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("UPDATE bulk_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (status, job_id))
            await db.commit()

    async def get_bulk_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM bulk_jobs WHERE job_id = ?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            job = dict(row)
            try:
                job["filter_criteria"] = json.loads(job["filter_criteria"])
            except Exception:
                pass
            return job

    async def get_bulk_job_targets(self, job_id: str, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM bulk_job_targets WHERE job_id = ? ORDER BY id ASC LIMIT ? OFFSET ?
            """, (job_id, limit, offset))
            return [dict(r) for r in await cursor.fetchall()]

    async def list_bulk_jobs(self, guild_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM bulk_jobs WHERE guild_id = ? ORDER BY id DESC LIMIT ?
            """, (guild_id, limit))
            rows = await cursor.fetchall()
            jobs = []
            for r in rows:
                j = dict(r)
                try:
                    j["filter_criteria"] = json.loads(j["filter_criteria"])
                except Exception:
                    pass
                jobs.append(j)
            return jobs

    # =========================================================================
    # Moderation Audit Records
    # =========================================================================

    async def record_audit_log(self, guild_id: int, actor_id: str, actor_name: str,
                               action_type: str, outcome: str, target_id: Optional[int] = None,
                               target_name: Optional[str] = None, role_id: Optional[int] = None,
                               reason: Optional[str] = None, dm_status: Optional[str] = None,
                               bulk_job_id: Optional[str] = None, details: str = ""):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO moderation_audit_records (
                    guild_id, actor_id, actor_name, target_id, target_name, action_type,
                    role_id, reason, outcome, dm_status, bulk_job_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, actor_id, actor_name, target_id, target_name, action_type,
                  role_id, reason, outcome, dm_status, bulk_job_id, details))
            await db.commit()

    async def get_moderation_audit_logs(self, guild_id: int, action_type: Optional[str] = None,
                                       search: Optional[str] = None, limit: int = 50,
                                       offset: int = 0) -> Dict[str, Any]:
        await self.init_db()
        limit = min(max(1, limit), 200)
        offset = max(0, offset)

        where_clauses = ["guild_id = ?"]
        params: List[Any] = [guild_id]

        if action_type and action_type.strip() and action_type.lower() != "all":
            where_clauses.append("LOWER(action_type) = LOWER(?)")
            params.append(action_type.strip())

        if search and search.strip():
            term = f"%{search.strip()}%"
            where_clauses.append("(details LIKE ? OR reason LIKE ? OR target_name LIKE ? OR actor_name LIKE ? OR CAST(target_id AS TEXT) LIKE ? OR CAST(actor_id AS TEXT) LIKE ?)")
            params.extend([term, term, term, term, term, term])

        where_sql = "WHERE " + " AND ".join(where_clauses)

        async with self.get_connection() as db:
            count_cur = await db.execute(f"SELECT COUNT(*) FROM moderation_audit_records {where_sql}", params)
            total = (await count_cur.fetchone())[0]

            select_sql = f"""
                SELECT * FROM moderation_audit_records
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            cursor = await db.execute(select_sql, params + [limit, offset])
            rows = await cursor.fetchall()

            # Distinct action types for filters
            types_cur = await db.execute("SELECT DISTINCT action_type FROM moderation_audit_records WHERE guild_id = ? ORDER BY action_type ASC", (guild_id,))
            action_types = [r[0] for r in await types_cur.fetchall() if r[0]]

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "logs": [dict(r) for r in rows],
                "action_types": action_types
            }

    # =========================================================================
    # Dashboard Permissions & Emergency Controls
    # =========================================================================

    DEFAULT_ROLE_PERMISSIONS = {
        "Administrator": {
            "can_manage_roles": True,
            "can_create_panels": True,
            "can_timeout": True,
            "can_kick": True,
            "can_ban": True,
            "can_tempban": True,
            "can_unban": True,
            "can_bulk_mod": True,
            "can_edit_templates": True,
            "can_emergency_stop": True
        },
        "Moderator": {
            "can_manage_roles": True,
            "can_create_panels": True,
            "can_timeout": True,
            "can_kick": True,
            "can_ban": False,
            "can_tempban": True,
            "can_unban": True,
            "can_bulk_mod": False,
            "can_edit_templates": False,
            "can_emergency_stop": False
        },
        "Viewer": {
            "can_manage_roles": False,
            "can_create_panels": False,
            "can_timeout": False,
            "can_kick": False,
            "can_ban": False,
            "can_tempban": False,
            "can_unban": False,
            "can_bulk_mod": False,
            "can_edit_templates": False,
            "can_emergency_stop": False
        }
    }

    async def get_dashboard_permissions(self, guild_id: int) -> Dict[str, Dict[str, bool]]:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM dashboard_action_permissions WHERE guild_id = ?", (guild_id,))
            rows = await cursor.fetchall()
            saved = {r["dashboard_role"]: dict(r) for r in rows}

            perms = {}
            for role, defaults in self.DEFAULT_ROLE_PERMISSIONS.items():
                if role in saved:
                    row_dict = saved[role]
                    perms[role] = {k: bool(row_dict.get(k, v)) for k, v in defaults.items()}
                else:
                    perms[role] = defaults.copy()
            return perms

    async def set_dashboard_permission_for_role(self, guild_id: int, role: str, capabilities: Dict[str, bool]):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO dashboard_action_permissions (
                    guild_id, dashboard_role, can_manage_roles, can_create_panels, can_timeout,
                    can_kick, can_ban, can_tempban, can_unban, can_bulk_mod, can_edit_templates, can_emergency_stop
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, dashboard_role) DO UPDATE SET
                    can_manage_roles = excluded.can_manage_roles,
                    can_create_panels = excluded.can_create_panels,
                    can_timeout = excluded.can_timeout,
                    can_kick = excluded.can_kick,
                    can_ban = excluded.can_ban,
                    can_tempban = excluded.can_tempban,
                    can_unban = excluded.can_unban,
                    can_bulk_mod = excluded.can_bulk_mod,
                    can_edit_templates = excluded.can_edit_templates,
                    can_emergency_stop = excluded.can_emergency_stop
            """, (
                guild_id, role,
                bool(capabilities.get("can_manage_roles", False)),
                bool(capabilities.get("can_create_panels", False)),
                bool(capabilities.get("can_timeout", False)),
                bool(capabilities.get("can_kick", False)),
                bool(capabilities.get("can_ban", False)),
                bool(capabilities.get("can_tempban", False)),
                bool(capabilities.get("can_unban", False)),
                bool(capabilities.get("can_bulk_mod", False)),
                bool(capabilities.get("can_edit_templates", False)),
                bool(capabilities.get("can_emergency_stop", False))
            ))
            await db.commit()

    async def is_emergency_locked(self, guild_id: int) -> bool:
        await self.init_db()
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT is_locked FROM guild_emergency_controls WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            return bool(row["is_locked"]) if row else False

    async def set_emergency_lock(self, guild_id: int, is_locked: bool, triggered_by: str, reason: str = ""):
        await self.init_db()
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO guild_emergency_controls (guild_id, is_locked, triggered_by, triggered_at, reason)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    is_locked = excluded.is_locked,
                    triggered_by = excluded.triggered_by,
                    triggered_at = CURRENT_TIMESTAMP,
                    reason = excluded.reason
            """, (guild_id, is_locked, triggered_by, reason))
            await db.commit()
