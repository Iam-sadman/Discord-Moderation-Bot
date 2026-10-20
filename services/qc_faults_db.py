"""
QC & Fault Tracker Database Service.
Manages persistent storage in data/qc_faults.db with SQLite WAL mode,
transaction safety, composite indexing, and analytical aggregations.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import aiosqlite

logger = logging.getLogger("QCFaultsDB")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "qc_faults.db")


class QCFaultsDB:
    _instance: Optional["QCFaultsDB"] = None

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._initialized = False

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "QCFaultsDB":
        if cls._instance is None:
            cls._instance = cls(db_path or DB_PATH)
        return cls._instance

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            yield conn

    @asynccontextmanager
    async def get_connection(self):
        """Yields a configured connection with WAL mode, busy timeout, and foreign keys."""
        if not self._initialized:
            await self.init_db()
        async with self._connect() as conn:
            yield conn

    async def init_db(self):
        """Initializes tables, indices, and integrity constraints."""
        if self._initialized:
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with self._connect() as db:
            # 1. QC Settings table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS qc_settings (
                    guild_id INTEGER PRIMARY KEY,
                    alert_channel_id INTEGER,
                    checker_role_ids TEXT DEFAULT '[]'
                );
            """)

            # 2. QC Projects master table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS qc_projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    project_code TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    description TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, project_code)
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_projects_guild ON qc_projects(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_projects_code ON qc_projects(guild_id, project_code);")

            # 3. QC Teams (Dynamic Role-Mapped Teams) table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS qc_teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    team_name TEXT NOT NULL,
                    team_role_id INTEGER NOT NULL,
                    leader_user_id INTEGER,
                    leader_role_id INTEGER,
                    team_channel_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, team_role_id)
                );
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_teams_guild ON qc_teams(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_teams_role ON qc_teams(guild_id, team_role_id);")

            # 4. QC Fault Logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS qc_fault_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    project_code TEXT NOT NULL,
                    labeler_id INTEGER NOT NULL,
                    labeler_name TEXT NOT NULL,
                    team_id INTEGER,
                    team_name TEXT NOT NULL,
                    leader_id INTEGER,
                    logged_by_id INTEGER NOT NULL,
                    logged_by_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    fault_type TEXT,
                    description TEXT NOT NULL,
                    image_url TEXT,
                    alert_channel_id INTEGER,
                    alert_message_id INTEGER,
                    status TEXT DEFAULT 'open',
                    acknowledged_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Required composite indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_proj ON qc_fault_logs(guild_id, project_code);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_labeler ON qc_fault_logs(guild_id, labeler_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_team ON qc_fault_logs(guild_id, team_name);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_sev ON qc_fault_logs(guild_id, severity);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_created ON qc_fault_logs(guild_id, created_at DESC);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_qc_faults_status ON qc_fault_logs(guild_id, status);")

            await db.commit()

        self._initialized = True
        logger.info(f"[OK] QCFaultsDB initialized successfully at {self.db_path}")

    # =========================================================================
    # 1. Settings Operations
    # =========================================================================

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Retrieves QC settings for a guild."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT guild_id, alert_channel_id, checker_role_ids FROM qc_settings WHERE guild_id = ?;",
                (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    try:
                        raw_roles = json.loads(row["checker_role_ids"] or "[]")
                        role_ids = [int(r) for r in raw_roles if str(r).isdigit()]
                    except Exception:
                        role_ids = []
                    return {
                        "guild_id": row["guild_id"],
                        "alert_channel_id": row["alert_channel_id"],
                        "checker_role_ids": role_ids
                    }
        return {
            "guild_id": guild_id,
            "alert_channel_id": None,
            "checker_role_ids": []
        }

    async def update_settings(
        self,
        guild_id: int,
        alert_channel_id: Optional[int] = None,
        checker_role_ids: Optional[List[int]] = None
    ) -> bool:
        """Upserts QC settings for a guild."""
        clean_roles = [int(r) for r in (checker_role_ids or []) if str(r).isdigit()]
        roles_json = json.dumps(clean_roles)
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO qc_settings (guild_id, alert_channel_id, checker_role_ids)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    alert_channel_id = excluded.alert_channel_id,
                    checker_role_ids = excluded.checker_role_ids;
            """, (guild_id, alert_channel_id, roles_json))
            await db.commit()
            return True

    # =========================================================================
    # 2. Projects Operations
    # =========================================================================

    async def list_projects(self, guild_id: int, only_active: bool = False) -> List[Dict[str, Any]]:
        """Lists projects for a guild, optionally filtered to active only."""
        query = "SELECT * FROM qc_projects WHERE guild_id = ?"
        params: List[Any] = [guild_id]
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY project_code ASC;"

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_project(self, guild_id: int, project_code: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single project by code."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM qc_projects WHERE guild_id = ? AND project_code = ?;",
                (guild_id, project_code.strip())
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def create_project(
        self,
        guild_id: int,
        project_code: str,
        project_name: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates a new project."""
        code = project_code.strip()
        name = project_name.strip()
        desc = description.strip() if description else None
        by = str(created_by) if created_by else None

        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO qc_projects (guild_id, project_code, project_name, description, created_by)
                VALUES (?, ?, ?, ?, ?);
            """, (guild_id, code, name, desc, by))
            project_id = cursor.lastrowid
            await db.commit()

        return {
            "id": project_id,
            "guild_id": guild_id,
            "project_code": code,
            "project_name": name,
            "description": desc,
            "is_active": 1,
            "created_by": by
        }

    async def update_project(
        self,
        guild_id: int,
        project_code: str,
        project_name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> bool:
        """Updates project details or active status."""
        fields = []
        params = []
        if project_name is not None:
            fields.append("project_name = ?")
            params.append(project_name.strip())
        if description is not None:
            fields.append("description = ?")
            params.append(description.strip() if description else None)
        if is_active is not None:
            fields.append("is_active = ?")
            params.append(1 if is_active else 0)

        if not fields:
            return False

        params.extend([guild_id, project_code.strip()])
        async with self.get_connection() as db:
            cursor = await db.execute(
                f"UPDATE qc_projects SET {', '.join(fields)} WHERE guild_id = ? AND project_code = ?;",
                tuple(params)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_project(self, guild_id: int, project_code: str) -> bool:
        """Deletes a project."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM qc_projects WHERE guild_id = ? AND project_code = ?;",
                (guild_id, project_code.strip())
            )
            await db.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # 3. Teams Operations
    # =========================================================================

    async def list_teams(self, guild_id: int) -> List[Dict[str, Any]]:
        """Lists all teams configured for a guild."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM qc_teams WHERE guild_id = ? ORDER BY team_name ASC;",
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_team(self, team_id: int, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Retrieves a single team by ID."""
        query = "SELECT * FROM qc_teams WHERE id = ?"
        params: List[Any] = [team_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_team_by_role(self, guild_id: int, team_role_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a team matching a Discord role ID."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT * FROM qc_teams WHERE guild_id = ? AND team_role_id = ?;",
                (guild_id, team_role_id)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_team_for_roles(self, guild_id: int, role_ids: List[int]) -> Optional[Dict[str, Any]]:
        """Finds the first configured team matching any of the member's role IDs."""
        if not role_ids:
            return None
        placeholders = ",".join("?" for _ in role_ids)
        query = f"SELECT * FROM qc_teams WHERE guild_id = ? AND team_role_id IN ({placeholders}) LIMIT 1;"
        params = [guild_id] + list(role_ids)

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def create_team(
        self,
        guild_id: int,
        team_name: str,
        team_role_id: int,
        leader_user_id: Optional[int] = None,
        leader_role_id: Optional[int] = None,
        team_channel_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Creates a new role-mapped team."""
        name = team_name.strip()
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO qc_teams (guild_id, team_name, team_role_id, leader_user_id, leader_role_id, team_channel_id)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (guild_id, name, team_role_id, leader_user_id, leader_role_id, team_channel_id))
            team_id = cursor.lastrowid
            await db.commit()

        return {
            "id": team_id,
            "guild_id": guild_id,
            "team_name": name,
            "team_role_id": team_role_id,
            "leader_user_id": leader_user_id,
            "leader_role_id": leader_role_id,
            "team_channel_id": team_channel_id
        }

    async def update_team(
        self,
        team_id: int,
        guild_id: int,
        team_name: Optional[str] = None,
        team_role_id: Optional[int] = None,
        leader_user_id: Optional[int] = None,
        leader_role_id: Optional[int] = None,
        team_channel_id: Optional[int] = None
    ) -> bool:
        """Updates team attributes."""
        fields = []
        params = []
        if team_name is not None:
            fields.append("team_name = ?")
            params.append(team_name.strip())
        if team_role_id is not None:
            fields.append("team_role_id = ?")
            params.append(team_role_id)
        if leader_user_id is not None:
            fields.append("leader_user_id = ?")
            params.append(leader_user_id if leader_user_id != 0 else None)
        if leader_role_id is not None:
            fields.append("leader_role_id = ?")
            params.append(leader_role_id if leader_role_id != 0 else None)
        if team_channel_id is not None:
            fields.append("team_channel_id = ?")
            params.append(team_channel_id if team_channel_id != 0 else None)

        if not fields:
            return False

        params.extend([team_id, guild_id])
        async with self.get_connection() as db:
            cursor = await db.execute(
                f"UPDATE qc_teams SET {', '.join(fields)} WHERE id = ? AND guild_id = ?;",
                tuple(params)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_team(self, team_id: int, guild_id: int) -> bool:
        """Deletes a team configuration."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM qc_teams WHERE id = ? AND guild_id = ?;",
                (team_id, guild_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # 4. Fault Logs Operations
    # =========================================================================

    async def create_fault_log(
        self,
        guild_id: int,
        project_code: str,
        labeler_id: int,
        labeler_name: str,
        team_id: Optional[int],
        team_name: str,
        leader_id: Optional[int],
        logged_by_id: int,
        logged_by_name: str,
        severity: str,
        description: str,
        fault_type: Optional[str] = None,
        image_url: Optional[str] = None,
        alert_channel_id: Optional[int] = None,
        alert_message_id: Optional[int] = None
    ) -> int:
        """
        Creates a new fault log record.
        Strictly stores only the image URL string in SQLite, never binary image data.
        """
        sev = severity.strip().lower()
        if sev not in ("minor", "major", "critical"):
            sev = "minor"

        ftype = fault_type.strip() if fault_type else None
        img_url = image_url.strip() if image_url else None

        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT INTO qc_fault_logs (
                    guild_id, project_code, labeler_id, labeler_name,
                    team_id, team_name, leader_id, logged_by_id, logged_by_name,
                    severity, fault_type, description, image_url,
                    alert_channel_id, alert_message_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open');
            """, (
                guild_id, project_code.strip(), labeler_id, labeler_name.strip(),
                team_id, team_name.strip(), leader_id, logged_by_id, logged_by_name.strip(),
                sev, ftype, description.strip(), img_url,
                alert_channel_id, alert_message_id
            ))
            fault_id = cursor.lastrowid
            await db.commit()
            return fault_id

    async def get_fault_log(self, fault_id: int, guild_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Retrieves a single fault log by ID."""
        query = "SELECT * FROM qc_fault_logs WHERE id = ?"
        params: List[Any] = [fault_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_fault_alert_message(self, fault_id: int, alert_channel_id: int, alert_message_id: int) -> bool:
        """Updates the posted alert channel and message IDs for a fault log."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                UPDATE qc_fault_logs
                SET alert_channel_id = ?, alert_message_id = ?
                WHERE id = ?;
            """, (alert_channel_id, alert_message_id, fault_id))
            await db.commit()
            return cursor.rowcount > 0

    async def acknowledge_fault(self, fault_id: int, guild_id: Optional[int] = None) -> bool:
        """Marks a fault as acknowledged."""
        query = "UPDATE qc_fault_logs SET status = 'acknowledged', acknowledged_at = CURRENT_TIMESTAMP WHERE id = ?"
        params: List[Any] = [fault_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)

        async with self.get_connection() as db:
            cursor = await db.execute(query, tuple(params))
            await db.commit()
            return cursor.rowcount > 0

    async def list_fault_logs(
        self,
        guild_id: int,
        project_code: Optional[str] = None,
        team_name: Optional[str] = None,
        labeler_id: Optional[int] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Retrieves filtered, paginated fault logs."""
        conditions = ["guild_id = ?"]
        params: List[Any] = [guild_id]

        if project_code and project_code.strip():
            conditions.append("project_code = ?")
            params.append(project_code.strip())

        if team_name and team_name.strip():
            conditions.append("team_name = ?")
            params.append(team_name.strip())

        if labeler_id:
            conditions.append("labeler_id = ?")
            params.append(labeler_id)

        if severity and severity.strip() and severity.strip().lower() != "all":
            conditions.append("severity = ?")
            params.append(severity.strip().lower())

        if status and status.strip() and status.strip().lower() != "all":
            conditions.append("status = ?")
            params.append(status.strip().lower())

        if date_from and date_from.strip():
            conditions.append("created_at >= ?")
            params.append(f"{date_from.strip()} 00:00:00")

        if date_to and date_to.strip():
            conditions.append("created_at <= ?")
            params.append(f"{date_to.strip()} 23:59:59")

        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            conditions.append("""(
                LOWER(description) LIKE ? OR
                LOWER(labeler_name) LIKE ? OR
                LOWER(logged_by_name) LIKE ? OR
                LOWER(project_code) LIKE ? OR
                LOWER(team_name) LIKE ? OR
                LOWER(COALESCE(fault_type, '')) LIKE ? OR
                CAST(labeler_id AS TEXT) LIKE ? OR
                CAST(id AS TEXT) LIKE ?
            )""")
            params.extend([term] * 8)

        where_clause = " AND ".join(conditions)
        query = f"SELECT * FROM qc_fault_logs WHERE {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?;"
        params.extend([max(1, limit), max(0, offset)])

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def count_fault_logs(
        self,
        guild_id: int,
        project_code: Optional[str] = None,
        team_name: Optional[str] = None,
        labeler_id: Optional[int] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None
    ) -> int:
        """Counts total fault logs matching filter criteria for pagination."""
        conditions = ["guild_id = ?"]
        params: List[Any] = [guild_id]

        if project_code and project_code.strip():
            conditions.append("project_code = ?")
            params.append(project_code.strip())

        if team_name and team_name.strip():
            conditions.append("team_name = ?")
            params.append(team_name.strip())

        if labeler_id:
            conditions.append("labeler_id = ?")
            params.append(labeler_id)

        if severity and severity.strip() and severity.strip().lower() != "all":
            conditions.append("severity = ?")
            params.append(severity.strip().lower())

        if status and status.strip() and status.strip().lower() != "all":
            conditions.append("status = ?")
            params.append(status.strip().lower())

        if date_from and date_from.strip():
            conditions.append("created_at >= ?")
            params.append(f"{date_from.strip()} 00:00:00")

        if date_to and date_to.strip():
            conditions.append("created_at <= ?")
            params.append(f"{date_to.strip()} 23:59:59")

        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            conditions.append("""(
                LOWER(description) LIKE ? OR
                LOWER(labeler_name) LIKE ? OR
                LOWER(logged_by_name) LIKE ? OR
                LOWER(project_code) LIKE ? OR
                LOWER(team_name) LIKE ? OR
                LOWER(COALESCE(fault_type, '')) LIKE ? OR
                CAST(labeler_id AS TEXT) LIKE ? OR
                CAST(id AS TEXT) LIKE ?
            )""")
            params.extend([term] * 8)

        where_clause = " AND ".join(conditions)
        query = f"SELECT COUNT(*) AS total FROM qc_fault_logs WHERE {where_clause};"

        async with self.get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return row["total"] if row else 0

    # =========================================================================
    # 5. Summary & Analytics Queries
    # =========================================================================

    async def get_overview_kpis(self, guild_id: int) -> Dict[str, Any]:
        """Calculates global KPI metrics for the QC dashboard."""
        async with self.get_connection() as db:
            # Fault stats
            async with db.execute("""
                SELECT
                    COUNT(*) AS total_faults,
                    SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_faults,
                    SUM(CASE WHEN severity = 'major' THEN 1 ELSE 0 END) AS major_faults,
                    SUM(CASE WHEN severity = 'minor' THEN 1 ELSE 0 END) AS minor_faults,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_faults,
                    SUM(CASE WHEN status = 'acknowledged' THEN 1 ELSE 0 END) AS ack_faults
                FROM qc_fault_logs
                WHERE guild_id = ?;
            """, (guild_id,)) as cursor:
                fault_row = await cursor.fetchone()

            # Active projects count
            async with db.execute("""
                SELECT COUNT(*) AS active_projects FROM qc_projects WHERE guild_id = ? AND is_active = 1;
            """, (guild_id,)) as cursor:
                proj_row = await cursor.fetchone()

            # Teams count
            async with db.execute("""
                SELECT COUNT(*) AS total_teams FROM qc_teams WHERE guild_id = ?;
            """, (guild_id,)) as cursor:
                team_row = await cursor.fetchone()

        total = fault_row["total_faults"] if fault_row else 0
        ack = fault_row["ack_faults"] if fault_row else 0
        ack_rate = round((ack / total) * 100, 1) if total > 0 else 100.0

        return {
            "total_faults": total or 0,
            "critical_faults": (fault_row["critical_faults"] if fault_row else 0) or 0,
            "major_faults": (fault_row["major_faults"] if fault_row else 0) or 0,
            "minor_faults": (fault_row["minor_faults"] if fault_row else 0) or 0,
            "open_faults": (fault_row["open_faults"] if fault_row else 0) or 0,
            "ack_faults": ack or 0,
            "ack_rate": ack_rate,
            "active_projects": proj_row["active_projects"] if proj_row else 0,
            "total_teams": team_row["total_teams"] if team_row else 0
        }

    async def get_project_summaries(self, guild_id: int) -> List[Dict[str, Any]]:
        """Returns project-wise breakdown of faults, severities, and top recurring fault types."""
        async with self.get_connection() as db:
            # Group faults by project_code
            async with db.execute("""
                SELECT
                    p.id,
                    p.project_code,
                    p.project_name,
                    p.description,
                    p.is_active,
                    COUNT(f.id) AS total_faults,
                    SUM(CASE WHEN f.severity = 'minor' THEN 1 ELSE 0 END) AS minor_count,
                    SUM(CASE WHEN f.severity = 'major' THEN 1 ELSE 0 END) AS major_count,
                    SUM(CASE WHEN f.severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                    SUM(CASE WHEN f.status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN f.status = 'acknowledged' THEN 1 ELSE 0 END) AS ack_count
                FROM qc_projects p
                LEFT JOIN qc_fault_logs f ON p.guild_id = f.guild_id AND p.project_code = f.project_code
                WHERE p.guild_id = ?
                GROUP BY p.project_code
                ORDER BY total_faults DESC, p.project_code ASC;
            """, (guild_id,)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    p_dict = dict(r)
                    p_code = p_dict["project_code"]

                    # Fetch top 3 fault types for this project
                    async with db.execute("""
                        SELECT fault_type, COUNT(*) AS type_count
                        FROM qc_fault_logs
                        WHERE guild_id = ? AND project_code = ? AND fault_type IS NOT NULL AND fault_type != ''
                        GROUP BY fault_type
                        ORDER BY type_count DESC
                        LIMIT 3;
                    """, (guild_id, p_code)) as type_cursor:
                        top_types = [f"{t['fault_type']} ({t['type_count']})" for t in await type_cursor.fetchall()]

                    p_dict["top_fault_types"] = top_types
                    p_dict["minor_count"] = p_dict["minor_count"] or 0
                    p_dict["major_count"] = p_dict["major_count"] or 0
                    p_dict["critical_count"] = p_dict["critical_count"] or 0
                    p_dict["open_count"] = p_dict["open_count"] or 0
                    p_dict["ack_count"] = p_dict["ack_count"] or 0
                    results.append(p_dict)
                return results

    async def get_team_summaries(self, guild_id: int) -> List[Dict[str, Any]]:
        """Returns team-wise and team-leader breakdown with acknowledgement rates."""
        async with self.get_connection() as db:
            async with db.execute("""
                SELECT
                    t.id AS team_id,
                    t.team_name,
                    t.team_role_id,
                    t.leader_user_id,
                    t.leader_role_id,
                    t.team_channel_id,
                    COUNT(f.id) AS total_faults,
                    SUM(CASE WHEN f.severity = 'minor' THEN 1 ELSE 0 END) AS minor_count,
                    SUM(CASE WHEN f.severity = 'major' THEN 1 ELSE 0 END) AS major_count,
                    SUM(CASE WHEN f.severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                    SUM(CASE WHEN f.status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN f.status = 'acknowledged' THEN 1 ELSE 0 END) AS ack_count
                FROM qc_teams t
                LEFT JOIN qc_fault_logs f ON t.guild_id = f.guild_id AND (t.id = f.team_id OR t.team_name = f.team_name)
                WHERE t.guild_id = ?
                GROUP BY t.id
                ORDER BY total_faults DESC, t.team_name ASC;
            """, (guild_id,)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    t_dict = dict(r)
                    tot = t_dict["total_faults"] or 0
                    ack = t_dict["ack_count"] or 0
                    t_dict["total_faults"] = tot
                    t_dict["minor_count"] = t_dict["minor_count"] or 0
                    t_dict["major_count"] = t_dict["major_count"] or 0
                    t_dict["critical_count"] = t_dict["critical_count"] or 0
                    t_dict["open_count"] = t_dict["open_count"] or 0
                    t_dict["ack_count"] = ack
                    t_dict["ack_rate"] = round((ack / tot) * 100, 1) if tot > 0 else 100.0
                    results.append(t_dict)
                return results

    async def get_labeler_summaries(
        self,
        guild_id: int,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Returns paginated summaries aggregated per labeler."""
        conditions = ["guild_id = ?"]
        params: List[Any] = [guild_id]

        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            conditions.append("(LOWER(labeler_name) LIKE ? OR CAST(labeler_id AS TEXT) LIKE ? OR LOWER(team_name) LIKE ?)")
            params.extend([term, term, term])

        where_clause = " AND ".join(conditions)

        async with self.get_connection() as db:
            # Count total distinct labelers matching filter
            async with db.execute(
                f"SELECT COUNT(DISTINCT labeler_id) AS total FROM qc_fault_logs WHERE {where_clause};",
                tuple(params)
            ) as cursor:
                c_row = await cursor.fetchone()
                total = c_row["total"] if c_row else 0

            # Fetch paginated labeler summaries
            query = f"""
                SELECT
                    labeler_id,
                    labeler_name,
                    team_name,
                    COUNT(*) AS total_faults,
                    SUM(CASE WHEN severity = 'minor' THEN 1 ELSE 0 END) AS minor_count,
                    SUM(CASE WHEN severity = 'major' THEN 1 ELSE 0 END) AS major_count,
                    SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'acknowledged' THEN 1 ELSE 0 END) AS ack_count,
                    MAX(created_at) AS last_fault_at
                FROM qc_fault_logs
                WHERE {where_clause}
                GROUP BY labeler_id
                ORDER BY total_faults DESC, last_fault_at DESC
                LIMIT ? OFFSET ?;
            """
            q_params = list(params) + [max(1, limit), max(0, offset)]
            async with db.execute(query, tuple(q_params)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    tot = d["total_faults"] or 0
                    ack = d["ack_count"] or 0
                    d["total_faults"] = tot
                    d["minor_count"] = d["minor_count"] or 0
                    d["major_count"] = d["major_count"] or 0
                    d["critical_count"] = d["critical_count"] or 0
                    d["open_count"] = d["open_count"] or 0
                    d["ack_count"] = ack
                    d["ack_rate"] = round((ack / tot) * 100, 1) if tot > 0 else 100.0
                    results.append(d)
                return total, results

    async def get_labeler_details(self, guild_id: int, labeler_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves in-depth fault profile, breakdown, recurring fault types, and history for a labeler."""
        async with self.get_connection() as db:
            # Overall stats
            async with db.execute("""
                SELECT
                    labeler_id,
                    labeler_name,
                    team_name,
                    COUNT(*) AS total_faults,
                    SUM(CASE WHEN severity = 'minor' THEN 1 ELSE 0 END) AS minor_count,
                    SUM(CASE WHEN severity = 'major' THEN 1 ELSE 0 END) AS major_count,
                    SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'acknowledged' THEN 1 ELSE 0 END) AS ack_count
                FROM qc_fault_logs
                WHERE guild_id = ? AND labeler_id = ?
                GROUP BY labeler_id;
            """, (guild_id, labeler_id)) as cursor:
                summary_row = await cursor.fetchone()
                if not summary_row:
                    return None
                summary = dict(summary_row)

            tot = summary["total_faults"] or 0
            ack = summary["ack_count"] or 0
            summary["ack_rate"] = round((ack / tot) * 100, 1) if tot > 0 else 100.0

            # Project breakdown
            async with db.execute("""
                SELECT
                    project_code,
                    COUNT(*) AS total,
                    SUM(CASE WHEN severity = 'minor' THEN 1 ELSE 0 END) AS minor,
                    SUM(CASE WHEN severity = 'major' THEN 1 ELSE 0 END) AS major,
                    SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical
                FROM qc_fault_logs
                WHERE guild_id = ? AND labeler_id = ?
                GROUP BY project_code
                ORDER BY total DESC;
            """, (guild_id, labeler_id)) as cursor:
                projects_breakdown = [dict(r) for r in await cursor.fetchall()]

            # Recurring fault types
            async with db.execute("""
                SELECT fault_type, COUNT(*) AS count
                FROM qc_fault_logs
                WHERE guild_id = ? AND labeler_id = ? AND fault_type IS NOT NULL AND fault_type != ''
                GROUP BY fault_type
                ORDER BY count DESC
                LIMIT 10;
            """, (guild_id, labeler_id)) as cursor:
                recurring_types = [dict(r) for r in await cursor.fetchall()]

            # Complete history
            async with db.execute("""
                SELECT *
                FROM qc_fault_logs
                WHERE guild_id = ? AND labeler_id = ?
                ORDER BY created_at DESC;
            """, (guild_id, labeler_id)) as cursor:
                history = [dict(r) for r in await cursor.fetchall()]

            return {
                "summary": summary,
                "projects_breakdown": projects_breakdown,
                "recurring_types": recurring_types,
                "history": history
            }
