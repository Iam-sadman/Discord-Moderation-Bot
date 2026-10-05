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

