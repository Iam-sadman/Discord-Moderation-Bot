"""
Server Audit Logs Provider for Discord Bot Dashboard.
Queries data/logs.db (table: audit_history) for real-time Discord server events.
"""

import aiosqlite
import os
from typing import Dict, Any, List, Optional

LOGS_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs.db")


async def get_available_action_types(guild_id: Optional[int] = None) -> List[str]:
    """
    Returns a distinct list of action_types recorded in the audit history.
    """
    if not os.path.exists(LOGS_DB_PATH):
        return []

    try:
        async with aiosqlite.connect(LOGS_DB_PATH) as db:
            if guild_id:
                try:
                    gid_int = int(guild_id)
                    cursor = await db.execute(
                        "SELECT DISTINCT action_type FROM audit_history WHERE (guild_id = ? OR CAST(guild_id AS TEXT) = ?) ORDER BY action_type ASC",
                        (gid_int, str(gid_int))
                    )
                except (ValueError, TypeError):
                    cursor = await db.execute(
                        "SELECT DISTINCT action_type FROM audit_history WHERE CAST(guild_id AS TEXT) = ? ORDER BY action_type ASC",
                        (str(guild_id),)
                    )
            else:
                cursor = await db.execute(
                    "SELECT DISTINCT action_type FROM audit_history ORDER BY action_type ASC"
                )
            rows = await cursor.fetchall()
            return [r[0] for r in rows if r[0]]
    except Exception as e:
        print(f"[!] Error fetching action types from logs.db: {e}")
        return []


async def fetch_server_audit_logs(
    guild_id: Optional[int] = None,
    action_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> Dict[str, Any]:
    """
    Fetches paginated audit logs from audit_history with optional filtering and search.
    """
    if not os.path.exists(LOGS_DB_PATH):
        return {"total": 0, "logs": [], "action_types": []}

    limit = min(max(1, limit), 200)
    offset = max(0, offset)

    where_clauses = []
    params = []

    if guild_id:
        try:
            gid_int = int(guild_id)
            where_clauses.append("(guild_id = ? OR CAST(guild_id AS TEXT) = ?)")
            params.extend([gid_int, str(gid_int)])
        except (ValueError, TypeError):
            where_clauses.append("CAST(guild_id AS TEXT) = ?")
            params.append(str(guild_id))

    if action_type and action_type.strip() and action_type.strip().lower() != "all":
        where_clauses.append("LOWER(action_type) = LOWER(?)")
        params.append(action_type.strip())

    if search and search.strip():
        term = f"%{search.strip()}%"
        where_clauses.append("(details LIKE ? OR action_type LIKE ? OR CAST(user_id AS TEXT) LIKE ? OR CAST(actor_id AS TEXT) LIKE ?)")
        params.extend([term, term, term, term])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        async with aiosqlite.connect(LOGS_DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            # Count total matching rows
            count_query = f"SELECT COUNT(*) FROM audit_history {where_sql}"
            cursor = await db.execute(count_query, params)
            total = (await cursor.fetchone())[0]

            # Fetch paginated rows
            select_query = f"""
                SELECT id, guild_id, user_id, actor_id, action_type, details, created_at
                FROM audit_history
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            cursor = await db.execute(select_query, params + [limit, offset])
            rows = await cursor.fetchall()

            logs = []
            for r in rows:
                logs.append({
                    "id": r["id"],
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "actor_id": r["actor_id"],
                    "action_type": r["action_type"],
                    "details": r["details"],
                    "created_at": r["created_at"],
                })

            action_types = await get_available_action_types(guild_id)

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "logs": logs,
                "action_types": action_types
            }
    except Exception as e:
        print(f"[!] Error querying audit_history: {e}")
        return {"total": 0, "logs": [], "action_types": [], "error": str(e)}
