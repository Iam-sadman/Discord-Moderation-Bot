import os
import json
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

class Config:
    TOKEN = os.getenv("DISCORD_TOKEN", "")
    PREFIX = os.getenv("BOT_PREFIX", "!")
    WEB_PORT = int(os.getenv("WEB_DASHBOARD_PORT", 5000))
    WEB_HOST = os.getenv("WEB_DASHBOARD_HOST", "0.0.0.0")
    WEB_SECRET = os.getenv("WEB_SECRET_KEY", "change_me")
    CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
    CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
    REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "settings.db")

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
    async def get_guild_setting(cls, guild_id: int, key: str, default=None):
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
    async def set_guild_setting(cls, guild_id: int, key: str, value):
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
    async def get_all_guild_settings(cls, guild_id: int) -> dict:
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
