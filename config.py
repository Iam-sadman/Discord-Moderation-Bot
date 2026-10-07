import os
import json
from typing import Optional, List, Dict, Any
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

class Config:
    # --- Sensitive Credentials & Server Hosting (from .env) ---
    TOKEN = os.getenv("DISCORD_TOKEN", "")
    PREFIX = os.getenv("BOT_PREFIX", "!")
    WEB_PORT = int(os.getenv("WEB_DASHBOARD_PORT", 5000))
    WEB_HOST = os.getenv("WEB_DASHBOARD_HOST", "0.0.0.0")
    WEB_SECRET = os.getenv("WEB_SECRET_KEY", "change_me")
    CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
    CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
    REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "settings.db")

    # Optional fallbacks from environment
    SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", 300))
    ARC_CHANNEL_ID = os.getenv("ARC_CHANNEL_ID", "")

    # =========================================================================
    # Database Initialization & Generic Guild Settings
    # =========================================================================

    @classmethod
    async def init_db(cls):
        os.makedirs(os.path.dirname(cls.DB_PATH), exist_ok=True)
        async with aiosqlite.connect(cls.DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (guild_id, key)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_music_playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    playlist_name TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_by_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, playlist_name)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_music_playlist_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    track_title TEXT NOT NULL,
                    track_url TEXT NOT NULL,
                    source_type TEXT DEFAULT 'url',
                    position INTEGER DEFAULT 0,
                    FOREIGN KEY(playlist_id) REFERENCES guild_music_playlists(id) ON DELETE CASCADE
                )
            """)
            await db.commit()

    @classmethod
    async def get_guild_setting(cls, guild_id: int, key: str, default: Any = None) -> Any:
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
                (guild_id, key)
            )
            row = await cursor.fetchone()
            if row is None:
                return default
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return row[0]

    @classmethod
    async def set_guild_setting(cls, guild_id: int, key: str, value: Any):
        async with aiosqlite.connect(cls.DB_PATH) as db:
            serialized = json.dumps(value)
            await db.execute(
                """INSERT INTO guild_settings (guild_id, key, value)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value""",
                (guild_id, key, serialized)
            )
            await db.commit()

    @classmethod
    async def delete_guild_setting(cls, guild_id: int, key: str):
        async with aiosqlite.connect(cls.DB_PATH) as db:
            await db.execute(
                "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
                (guild_id, key)
            )
            await db.commit()

    @classmethod
    async def get_all_guild_settings(cls, guild_id: int) -> Dict[str, Any]:
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT key, value FROM guild_settings WHERE guild_id = ?",
                (guild_id,)
            )
            rows = await cursor.fetchall()
            result = {}
            for key, value in rows:
                try:
                    result[key] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    result[key] = value
            return result

    # =========================================================================
    # Specialized Guild Configuration Helpers (Discord Command Accessible)
    # =========================================================================

    @classmethod
    async def get_prefix(cls, guild_id: Optional[int]) -> str:
        """Returns the custom command prefix for a guild, or global default."""
        if not guild_id:
            return cls.PREFIX
        return await cls.get_guild_setting(guild_id, "prefix", cls.PREFIX)

    @classmethod
    async def set_prefix(cls, guild_id: int, prefix: str):
        """Sets custom command prefix for a guild."""
        await cls.set_guild_setting(guild_id, "prefix", prefix)

    @classmethod
    async def get_announcement_roles(cls, guild_id: int) -> List[int]:
        """Returns list of allowed announcement role IDs for a guild."""
        roles = await cls.get_guild_setting(guild_id, "announcement_roles", None)
        if roles is not None and isinstance(roles, list):
            return [int(r) for r in roles]

        # Fallback to .env if not customized for this guild
        env_raw = os.getenv("ANNOUNCEMENT_ALLOWED_ROLE_IDS", "")
        role_ids = []
        for item in env_raw.split(","):
            item = item.strip()
            if item.isdigit():
                role_ids.append(int(item))
        return role_ids

    @classmethod
    async def set_announcement_roles(cls, guild_id: int, role_ids: List[int]):
        """Sets list of allowed announcement role IDs for a guild."""
        unique_ids = sorted(list(set(int(r) for r in role_ids)))
        await cls.set_guild_setting(guild_id, "announcement_roles", unique_ids)

    @classmethod
    async def get_loa_timezone(cls, guild_id: int) -> int:
        """Returns UTC offset in hours for LOA check (default: 6 for BST UTC+6)."""
        env_offset = int(os.getenv("LOA_TIMEZONE_OFFSET", 6))
        return int(await cls.get_guild_setting(guild_id, "loa_timezone_offset", env_offset))

    @classmethod
    async def set_loa_timezone(cls, guild_id: int, offset_hours: int):
        """Sets custom UTC offset in hours for LOA check."""
        await cls.set_guild_setting(guild_id, "loa_timezone_offset", int(offset_hours))

    @classmethod
    async def is_module_enabled(cls, guild_id: int, module_name: str) -> bool:
        """Checks if a module is enabled for a guild (defaults to True)."""
        return bool(await cls.get_guild_setting(guild_id, f"module_{module_name.lower()}_enabled", True))

    @classmethod
    async def set_module_enabled(cls, guild_id: int, module_name: str, enabled: bool):
        """Enables or disables a bot module for a guild."""
        await cls.set_guild_setting(guild_id, f"module_{module_name.lower()}_enabled", bool(enabled))

    # =========================================================================
    # Cog Permissions & Dynamic Role-Based Access Control (RBAC)
    # =========================================================================

    @classmethod
    async def get_all_cog_permissions(cls, guild_id: int) -> Dict[str, Dict[str, Any]]:
        """
        Returns permission mapping for all cogs in a guild:
        {
            "announcement": {"access_level": "roles_only", "allowed_roles": [123, 456]},
            "music": {"access_level": "everyone", "allowed_roles": []},
            "loa": {"access_level": "roles_only", "allowed_roles": []},
            "logger": {"access_level": "admin_only", "allowed_roles": []},
            "labeler_tracker": {"access_level": "admin_only", "allowed_roles": []},
            "settings": {"access_level": "admin_only", "allowed_roles": []}
        }
        """
        saved = await cls.get_guild_setting(guild_id, "cog_permissions", {})
        if not isinstance(saved, dict):
            saved = {}

        # Default permissions for each cog
        ann_roles = await cls.get_announcement_roles(guild_id)
        defaults = {
            "announcement": {
                "access_level": "roles_only" if ann_roles else "admin_only",
                "allowed_roles": ann_roles
            },
            "music": {
                "access_level": "everyone",
                "allowed_roles": []
            },
            "loa": {
                "access_level": "roles_only",
                "allowed_roles": []
            },
            "logger": {
                "access_level": "admin_only",
                "allowed_roles": []
            },
            "labeler_tracker": {
                "access_level": "admin_only",
                "allowed_roles": []
            },
            "settings": {
                "access_level": "admin_only",
                "allowed_roles": []
            }
        }

        result = {}
        for cog, default_perm in defaults.items():
            if cog in saved and isinstance(saved[cog], dict):
                perm = saved[cog]
                result[cog] = {
                    "access_level": perm.get("access_level", default_perm["access_level"]),
                    "allowed_roles": [int(r) for r in perm.get("allowed_roles", []) if str(r).isdigit()]
                }
            else:
                result[cog] = default_perm
        return result

    @classmethod
    async def get_cog_permission(cls, guild_id: int, cog_name: str) -> Dict[str, Any]:
        cog_key = cog_name.lower().replace(" ", "_")
        if cog_key == "labelertracker":
            cog_key = "labeler_tracker"
        all_perms = await cls.get_all_cog_permissions(guild_id)
        return all_perms.get(cog_key, {"access_level": "admin_only", "allowed_roles": []})

    @classmethod
    async def set_cog_permission(cls, guild_id: int, cog_name: str, access_level: str, allowed_roles: List[Any]):
        cog_key = cog_name.lower().replace(" ", "_")
        if cog_key == "labelertracker":
            cog_key = "labeler_tracker"

        saved = await cls.get_guild_setting(guild_id, "cog_permissions", {})
        if not isinstance(saved, dict):
            saved = {}

        clean_roles = [int(r) for r in allowed_roles if str(r).isdigit()]
        saved[cog_key] = {
            "access_level": access_level,  # "admin_only", "roles_only", "everyone"
            "allowed_roles": clean_roles
        }
        await cls.set_guild_setting(guild_id, "cog_permissions", saved)

        # Sync announcement_roles if this is announcement cog
        if cog_key == "announcement":
            await cls.set_announcement_roles(guild_id, clean_roles)

    @classmethod
    async def check_member_cog_permission(cls, member: Any, cog_name: str) -> bool:
        """Helper to verify if a member has permission to use a cog."""
        if not member or not getattr(member, "guild", None):
            return False
        # Server owner & Discord Administrator always have full access
        if getattr(member, "id", None) == getattr(member.guild, "owner_id", None):
            return True
        perms = getattr(member, "guild_permissions", None)
        if perms and perms.administrator:
            return True

        perm = await cls.get_cog_permission(member.guild.id, cog_name)
        lvl = perm.get("access_level", "admin_only")
        if lvl == "everyone":
            return True
        if lvl == "admin_only":
            return False
        if lvl == "roles_only":
            allowed = set(perm.get("allowed_roles", []))
            member_roles = getattr(member, "roles", [])
            return any(getattr(r, "id", None) in allowed for r in member_roles)
        return False

    # =========================================================================
    # Persistent Guild Music Playlists & Dedicated Channel Settings
    # =========================================================================

    @classmethod
    async def create_playlist(cls, guild_id: int, name: str, user_id: int, user_name: str) -> bool:
        """Creates a named playlist for a guild. Returns False if already exists."""
        clean_name = name.strip()[:64]
        async with aiosqlite.connect(cls.DB_PATH) as db:
            try:
                await db.execute(
                    """INSERT INTO guild_music_playlists (guild_id, playlist_name, created_by, created_by_name)
                       VALUES (?, ?, ?, ?)""",
                    (guild_id, clean_name, user_id, user_name)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    @classmethod
    async def delete_playlist(cls, guild_id: int, name: str) -> bool:
        """Deletes a named playlist and all its tracks."""
        clean_name = name.strip()
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM guild_music_playlists WHERE guild_id = ? AND LOWER(playlist_name) = LOWER(?)",
                (guild_id, clean_name)
            )
            await db.commit()
            return cursor.rowcount > 0

    @classmethod
    async def add_track_to_playlist(cls, guild_id: int, name: str, title: str, url: str, source_type: str = "url") -> bool:
        """Adds a track to a playlist."""
        clean_name = name.strip()
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id FROM guild_music_playlists WHERE guild_id = ? AND LOWER(playlist_name) = LOWER(?)",
                (guild_id, clean_name)
            )
            row = await cursor.fetchone()
            if not row:
                return False
            playlist_id = row[0]

            pos_cur = await db.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM guild_music_playlist_tracks WHERE playlist_id = ?",
                (playlist_id,)
            )
            pos_row = await pos_cur.fetchone()
            position = pos_row[0] if pos_row else 1

            await db.execute(
                """INSERT INTO guild_music_playlist_tracks (playlist_id, track_title, track_url, source_type, position)
                   VALUES (?, ?, ?, ?, ?)""",
                (playlist_id, title[:200], url, source_type, position)
            )
            await db.commit()
            return True

    @classmethod
    async def remove_track_from_playlist(cls, guild_id: int, name: str, position: int) -> Optional[str]:
        """Removes a track by 1-based index. Returns the track title if removed, None otherwise."""
        clean_name = name.strip()
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id FROM guild_music_playlists WHERE guild_id = ? AND LOWER(playlist_name) = LOWER(?)",
                (guild_id, clean_name)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            playlist_id = row[0]

            tracks_cur = await db.execute(
                "SELECT id, track_title FROM guild_music_playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC",
                (playlist_id,)
            )
            tracks = await tracks_cur.fetchall()
            if position < 1 or position > len(tracks):
                return None
            target_id, target_title = tracks[position - 1]
            await db.execute("DELETE FROM guild_music_playlist_tracks WHERE id = ?", (target_id,))
            await db.commit()
            return target_title

    @classmethod
    async def get_playlist(cls, guild_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Returns playlist metadata and track list."""
        clean_name = name.strip()
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                """SELECT id, playlist_name, created_by, created_by_name, created_at
                   FROM guild_music_playlists WHERE guild_id = ? AND LOWER(playlist_name) = LOWER(?)""",
                (guild_id, clean_name)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            p_id, p_name, user_id, user_name, created_at = row

            tracks_cur = await db.execute(
                """SELECT id, track_title, track_url, source_type, position
                   FROM guild_music_playlist_tracks WHERE playlist_id = ? ORDER BY position ASC, id ASC""",
                (p_id,)
            )
            tracks_raw = await tracks_cur.fetchall()
            tracks = [
                {
                    "id": t[0],
                    "title": t[1],
                    "url": t[2],
                    "source_type": t[3],
                    "position": idx + 1
                }
                for idx, t in enumerate(tracks_raw)
            ]
            return {
                "id": p_id,
                "name": p_name,
                "created_by": user_id,
                "created_by_name": user_name,
                "created_at": created_at,
                "tracks": tracks
            }

    @classmethod
    async def list_playlists(cls, guild_id: int) -> List[Dict[str, Any]]:
        """Lists all playlists in a guild with track counts."""
        async with aiosqlite.connect(cls.DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.id, p.playlist_name, p.created_by, p.created_by_name, p.created_at,
                          COUNT(t.id) as track_count
                   FROM guild_music_playlists p
                   LEFT JOIN guild_music_playlist_tracks t ON p.id = t.playlist_id
                   WHERE p.guild_id = ?
                   GROUP BY p.id
                   ORDER BY p.playlist_name ASC""",
                (guild_id,)
            )
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "name": r[1],
                    "created_by": r[2],
                    "created_by_name": r[3],
                    "created_at": r[4],
                    "track_count": r[5]
                }
                for r in rows
            ]

    @classmethod
    async def get_music_channel(cls, guild_id: int) -> Optional[int]:
        """Returns the configured dedicated music text channel ID, if any."""
        val = await cls.get_guild_setting(guild_id, "music_dedicated_channel_id", None)
        return int(val) if val and str(val).isdigit() else None

    @classmethod
    async def set_music_channel(cls, guild_id: int, channel_id: Optional[int]):
        """Sets or clears the dedicated music text channel ID."""
        if channel_id:
            await cls.set_guild_setting(guild_id, "music_dedicated_channel_id", int(channel_id))
        else:
            await cls.delete_guild_setting(guild_id, "music_dedicated_channel_id")

    @classmethod
    async def get_music_player_message_id(cls, guild_id: int) -> Optional[int]:
        """Returns the pinned dedicated player message ID, if any."""
        val = await cls.get_guild_setting(guild_id, "music_player_message_id", None)
        return int(val) if val and str(val).isdigit() else None

    @classmethod
    async def set_music_player_message_id(cls, guild_id: int, message_id: Optional[int]):
        """Sets or clears the dedicated player message ID."""
        if message_id:
            await cls.set_guild_setting(guild_id, "music_player_message_id", int(message_id))
        else:
            await cls.delete_guild_setting(guild_id, "music_player_message_id")

