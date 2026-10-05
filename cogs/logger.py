import asyncio
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import Config


# ============================================================
# LOGGER CONFIG
# ============================================================

logger = logging.getLogger("discord_logger")

DB_PATH = "data/logs.db"

MESSAGE_CACHE_SIZE = 5000
HISTORY_PAGE_SIZE = 4

AUDIT_LOG_LIMIT = 25

# ------------------------------------------------------------
# Generic audit lookup
# ------------------------------------------------------------

AUDIT_LOOKUP_WINDOW = 20.0

# ------------------------------------------------------------
# Voice audit lookup
#
# Discord can create MEMBER_MOVE / MEMBER_DISCONNECT audit
# entries noticeably after the Gateway voice event.
#
# Total move wait:
#     16 * 0.65 ~= 10.4 sec
#
# Total disconnect wait:
#     14 * 0.65 ~= 9.1 sec
# ------------------------------------------------------------

VOICE_MOVE_ATTEMPTS = 16
VOICE_MOVE_DELAY = 0.65
VOICE_MOVE_WINDOW = 20.0

VOICE_DISCONNECT_ATTEMPTS = 14
VOICE_DISCONNECT_DELAY = 0.65
VOICE_DISCONNECT_WINDOW = 20.0

# ------------------------------------------------------------
# Role / other audit events
# ------------------------------------------------------------

ROLE_AUDIT_ATTEMPTS = 10
ROLE_AUDIT_DELAY = 0.60
ROLE_AUDIT_WINDOW = 20.0

SEEN_AUDIT_CACHE_SIZE = 3000


# ============================================================
# MESSAGE CACHE
# ============================================================

class MessageCache:

    def __init__(self, maxsize: int = MESSAGE_CACHE_SIZE):
        self.cache = OrderedDict()
        self.maxsize = maxsize

    def add(self, message_id: int, message_data: dict):

        if message_id in self.cache:
            self.cache.move_to_end(message_id)

        self.cache[message_id] = message_data

        while len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)

    def get(self, message_id: int):

        data = self.cache.get(message_id)

        if data is not None:
            self.cache.move_to_end(message_id)

        return data

    def remove(self, message_id: int):
        self.cache.pop(message_id, None)


class HistoryPaginationView(discord.ui.View):
    """Load matching history rows from SQLite as the user changes pages."""

    def __init__(
        self,
        cog,
        user: discord.Member,
        guild_id: int,
        days: int,
        cutoff: str,
        rows,
        has_next: bool,
        *,
        timeout: float = 180
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user = user
        self.guild_id = guild_id
        self.days = days
        self.cutoff = cutoff
        self.page_index = 0
        self.page_cursors: list[Optional[int]] = [None]
        self.current_rows = rows[:HISTORY_PAGE_SIZE]
        self.has_next = has_next
        self.result_count: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self._interaction_lock = asyncio.Lock()
        self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = self.page_index <= 0
        self.next_page.disabled = not self.has_next

    async def _show_page(
        self,
        interaction: discord.Interaction,
        cursor: Optional[int],
        page_index: int,
        *,
        update_cursor_stack: bool
    ):
        async with self._interaction_lock:
            try:
                rows = await self.cog._fetch_user_history_page(
                    self.guild_id,
                    self.user.id,
                    self.cutoff,
                    cursor
                )
            except Exception:
                logger.exception("Failed to load a logger history page")
                await interaction.response.send_message(
                    "❌ Failed to load this history page.",
                    ephemeral=True
                )
                return

            if not rows:
                await interaction.response.send_message(
                    "📭 No more logs were found on this page.",
                    ephemeral=True
                )
                return

            if update_cursor_stack:
                self.page_cursors.append(cursor)
            elif len(self.page_cursors) > 1:
                self.page_cursors.pop()

            self.page_index = page_index
            self.current_rows = rows[:HISTORY_PAGE_SIZE]
            self.has_next = len(rows) > HISTORY_PAGE_SIZE
            self._update_buttons()
            embed = self.cog._build_history_embed(
                self.user,
                self.days,
                self.result_count,
                self.page_index,
                self.has_next,
                self.current_rows
            )
            await interaction.response.edit_message(embed=embed, view=self)

    async def set_result_count(self, total_count: int):
        async with self._interaction_lock:
            self.result_count = total_count
            if self.message is None:
                return
            embed = self.cog._build_history_embed(
                self.user,
                self.days,
                self.result_count,
                self.page_index,
                self.has_next,
                self.current_rows
            )
            await self.message.edit(embed=embed, view=self)

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        disabled=True
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if self.page_index <= 0:
            await interaction.response.defer()
            return
        await self._show_page(
            interaction,
            self.page_cursors[-2],
            self.page_index - 1,
            update_cursor_stack=False
        )

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not self.has_next or not self.current_rows:
            await interaction.response.defer()
            return
        await self._show_page(
            interaction,
            self.current_rows[-1][5],
            self.page_index + 1,
            update_cursor_stack=True
        )

    async def on_timeout(self):
        self.previous_page.disabled = True
        self.next_page.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ============================================================
# LOGGER COG
# ============================================================

class Logger(commands.Cog):

    log_group = app_commands.Group(
        name="log",
        description="Logger commands"
    )

    def __init__(self, bot: commands.Bot):

        self.bot = bot

        self.db: Optional[aiosqlite.Connection] = None
        self.read_db: Optional[aiosqlite.Connection] = None
        self.count_db: Optional[aiosqlite.Connection] = None
        self._history_count_tasks: set[asyncio.Task] = set()

        self.message_cache = MessageCache()

        self._db_ready = asyncio.Event()

        self._db_task = asyncio.create_task(
            self._init_db()
        )

        # Audit entries already consumed by this logger.
        self._seen_audit_entries = OrderedDict()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        allowed = await Config.check_member_cog_permission(interaction.user, "logger")
        if not allowed:
            await interaction.response.send_message(
                "❌ You do not have permission to use the **Logger** module on this server.",
                ephemeral=True
            )
            return False
        return True

    # ========================================================
    # DATABASE
    # ========================================================

    async def _init_db(self):

        try:

            os.makedirs(
                os.path.dirname(DB_PATH),
                exist_ok=True
            )

            self.db = await aiosqlite.connect(
                DB_PATH,
                timeout=30
            )

            # SQLite performance / concurrency.
            await self.db.execute(
                "PRAGMA journal_mode=WAL"
            )

            await self.db.execute(
                "PRAGMA synchronous=NORMAL"
            )

            await self.db.execute(
                "PRAGMA busy_timeout=30000"
            )

            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS audit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER,
                    actor_id INTEGER,
                    action_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
            """)

            # ------------------------------------------------
            # Search indexes
            # ------------------------------------------------

            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_user_time
                ON audit_history(
                    guild_id,
                    user_id,
                    created_at DESC,
                    id DESC
                )
            """)

            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_actor_time
                ON audit_history(
                    guild_id,
                    actor_id,
                    created_at DESC,
                    id DESC
                )
            """)

            # These indexes support newest-first history pages. The time-based
            # indexes above remain useful for counting records in a date range.
            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_user_id
                ON audit_history(
                    guild_id,
                    user_id,
                    id DESC
                )
            """)

            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_actor_id
                ON audit_history(
                    guild_id,
                    actor_id,
                    id DESC
                )
            """)

            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_action_time
                ON audit_history(
                    guild_id,
                    action_type,
                    created_at DESC,
                    id DESC
                )
            """)

            # Useful for general recent guild history.
            await self.db.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_audit_guild_time
                ON audit_history(
                    guild_id,
                    created_at DESC,
                    id DESC
                )
            """)

            await self.db.commit()

            # Keep history reads off the frequently committed write connection.
            # WAL allows this read-only connection to run alongside log inserts.
            self.read_db = await aiosqlite.connect(
                DB_PATH,
                timeout=30
            )
            await self.read_db.execute(
                "PRAGMA busy_timeout=30000"
            )
            await self.read_db.execute(
                "PRAGMA query_only=ON"
            )

            # Keep the potentially broad exact-count query from queueing ahead
            # of interactive page reads on the same aiosqlite connection.
            self.count_db = await aiosqlite.connect(
                DB_PATH,
                timeout=30
            )
            await self.count_db.execute(
                "PRAGMA busy_timeout=30000"
            )
            await self.count_db.execute(
                "PRAGMA query_only=ON"
            )

            logger.info(
                "Logger database initialized: %s",
                DB_PATH
            )

        except Exception:

            logger.exception(
                "Failed to initialize logger database"
            )

        finally:

            self._db_ready.set()

    async def cog_load(self):

        await self._db_ready.wait()

    async def cog_unload(self):

        for task in tuple(self._history_count_tasks):
            task.cancel()
        if self._history_count_tasks:
            await asyncio.gather(
                *self._history_count_tasks,
                return_exceptions=True
            )
        self._history_count_tasks.clear()

        if (
            self._db_task
            and not self._db_task.done()
        ):

            self._db_task.cancel()

            try:
                await self._db_task
            except asyncio.CancelledError:
                pass

        if self.read_db:

            try:
                await self.read_db.close()
            except Exception:
                logger.exception(
                    "Failed to close logger read database"
                )
            self.read_db = None

        if self.count_db:
            try:
                await self.count_db.close()
            except Exception:
                logger.exception(
                    "Failed to close logger count database"
                )
            self.count_db = None

        if self.db:

            try:
                await self.db.close()
            except Exception:
                logger.exception(
                    "Failed to close logger database"
                )
            self.db = None

    # ========================================================
    # DATABASE SAVE
    # ========================================================

    async def _save_log_to_db(
        self,
        guild_id: int,
        user_id: Optional[int],
        actor_id: Optional[int],
        action_type: str,
        details: str
    ):

        await self._db_ready.wait()

        if self.db is None:

            logger.error(
                "Logger DB is unavailable"
            )

            return

        created_at = datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        try:

            await self.db.execute(
                """
                INSERT INTO audit_history
                (
                    guild_id,
                    user_id,
                    actor_id,
                    action_type,
                    details,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    actor_id,
                    action_type,
                    details,
                    created_at
                )
            )

            await self.db.commit()

        except Exception:

            logger.exception(
                "Failed to save logger DB entry"
            )

    # ========================================================
    # AUDIT CACHE
    # ========================================================

    def _audit_seen(
        self,
        entry_id: int
    ) -> bool:

        return entry_id in self._seen_audit_entries

    def _mark_audit_seen(
        self,
        entry_id: int
    ):

        self._seen_audit_entries[entry_id] = None

        self._seen_audit_entries.move_to_end(
            entry_id
        )

        while (
            len(self._seen_audit_entries)
            > SEEN_AUDIT_CACHE_SIZE
        ):

            self._seen_audit_entries.popitem(
                last=False
            )

    # ========================================================
    # FETCH AUDIT LOG
    # ========================================================

    async def _fetch_audit_entries(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction
    ):

        try:

            return [
                entry
                async for entry in guild.audit_logs(
                    limit=AUDIT_LOG_LIMIT,
                    action=action
                )
            ]

        except discord.Forbidden:

            logger.warning(
                "Missing View Audit Log permission "
                "in guild %s",
                guild.id
            )

        except discord.HTTPException as exc:

            logger.warning(
                "Audit log HTTP error in guild %s: %s",
                guild.id,
                exc
            )

        except Exception:

            logger.exception(
                "Unexpected audit log error "
                "in guild %s",
                guild.id
            )

        return []

    # ========================================================
    # GENERIC AUDIT FINDER
    # ========================================================

    async def _find_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
        window: float = AUDIT_LOOKUP_WINDOW,
        mark_seen: bool = True
    ):

        now = datetime.now(timezone.utc)

        entries = await self._fetch_audit_entries(
            guild,
            action
        )

        best_entry = None
        best_age = None

        for entry in entries:

            if not entry.created_at:
                continue

            age = (
                now - entry.created_at
            ).total_seconds()

            if age < -2:
                continue

            if age > window:
                continue

            if self._audit_seen(entry.id):
                continue

            if target_id is not None:

                entry_target_id = getattr(
                    entry.target,
                    "id",
                    None
                )

                if entry_target_id != target_id:
                    continue

            if (
                best_age is None
                or abs(age) < abs(best_age)
            ):

                best_entry = entry
                best_age = age

        if best_entry and mark_seen:

            self._mark_audit_seen(
                best_entry.id
            )

        return best_entry

    # ========================================================
    # VOICE AUDIT HELPERS
    # ========================================================

    @staticmethod
    def _audit_age(
        event_time: datetime,
        created_at: datetime
    ) -> float:

        return (
            event_time - created_at
        ).total_seconds()

    # ========================================================
    # FIND VOICE MOVE ACTOR
    #
    # This is intentionally separate from the generic finder.
    #
    # Why?
    #
    # MEMBER_MOVE is one of the audit events where Discord
    # frequently produces the audit entry after the Gateway
    # voice_state_update event.
    #
    # We therefore:
    #
    # 1. Retry.
    # 2. Require target member ID.
    # 3. Compare audit timestamp with original event time.
    # 4. Prefer matching destination channel if Discord
    #    exposes it.
    # 5. Never mark unrelated audit entries as consumed.
    # ========================================================

    async def _find_voice_move_actor(
        self,
        guild: discord.Guild,
        member: discord.Member,
        before_channel: discord.VoiceChannel,
        after_channel: discord.VoiceChannel,
        event_time: datetime
    ):

        for attempt in range(
            VOICE_MOVE_ATTEMPTS
        ):

            entries = await self._fetch_audit_entries(
                guild,
                discord.AuditLogAction.member_move
            )

            candidates = []

            for entry in entries:

                if not entry.created_at:
                    continue

                if self._audit_seen(entry.id):
                    continue

                # --------------------------------------------
                # HARD REQUIREMENT:
                # Audit target must be the moved member.
                # --------------------------------------------

                target_id = getattr(
                    entry.target,
                    "id",
                    None
                )

                if target_id != member.id:
                    continue

                age = self._audit_age(
                    event_time,
                    entry.created_at
                )

                # Audit event must not be substantially newer
                # than our Gateway event.
                if age < -3:
                    continue

                if age > VOICE_MOVE_WINDOW:
                    continue

                # --------------------------------------------
                # SCORING
                # --------------------------------------------

                score = 1000

                # Closer timestamp is better.
                score -= min(
                    500,
                    int(abs(age) * 20)
                )

                # --------------------------------------------
                # Destination channel
                # --------------------------------------------

                extra = getattr(
                    entry,
                    "extra",
                    None
                )

                audit_channel = getattr(
                    extra,
                    "channel",
                    None
                )

                audit_channel_id = getattr(
                    audit_channel,
                    "id",
                    None
                )

                if audit_channel_id == after_channel.id:
                    score += 500

                candidates.append(
                    (
                        score,
                        abs(age),
                        entry
                    )
                )

            if candidates:

                candidates.sort(
                    key=lambda item: (
                        -item[0],
                        item[1]
                    )
                )

                selected = candidates[0][2]

                self._mark_audit_seen(
                    selected.id
                )

                return selected

            # --------------------------------------------
            # Give Discord time to publish audit entry.
            # --------------------------------------------

            if attempt < (
                VOICE_MOVE_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    VOICE_MOVE_DELAY
                )

        return None

    # ========================================================
    # FIND VOICE DISCONNECT ACTOR
    #
    # MEMBER_DISCONNECT can also arrive after the Gateway
    # voice event.
    #
    # The old implementation did only one lookup.
    # That is the main reason administrator/moderator names
    # were frequently shown as Unknown.
    # ========================================================

    async def _find_voice_disconnect_actor(
        self,
        guild: discord.Guild,
        member: discord.Member,
        event_time: datetime
    ):

        for attempt in range(
            VOICE_DISCONNECT_ATTEMPTS
        ):

            entries = await self._fetch_audit_entries(
                guild,
                discord.AuditLogAction.member_disconnect
            )

            candidates = []

            for entry in entries:

                if not entry.created_at:
                    continue

                if self._audit_seen(entry.id):
                    continue

                target_id = getattr(
                    entry.target,
                    "id",
                    None
                )

                if target_id != member.id:
                    continue

                age = self._audit_age(
                    event_time,
                    entry.created_at
                )

                if age < -3:
                    continue

                if age > VOICE_DISCONNECT_WINDOW:
                    continue

                score = 1000

                score -= min(
                    500,
                    int(abs(age) * 20)
                )

                candidates.append(
                    (
                        score,
                        abs(age),
                        entry
                    )
                )

            if candidates:

                candidates.sort(
                    key=lambda item: (
                        -item[0],
                        item[1]
                    )
                )

                selected = candidates[0][2]

                self._mark_audit_seen(
                    selected.id
                )

                return selected

            if attempt < (
                VOICE_DISCONNECT_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    VOICE_DISCONNECT_DELAY
                )

        return None

    # ========================================================
    # VOICE STATE UPDATE
    # ========================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):

        if member.bot:
            return

        guild = member.guild

        if guild is None:
            return

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        # ====================================================
        # JOIN / LEAVE / MOVE
        # ====================================================

        if before.channel != after.channel:

            # ------------------------------------------------
            # JOIN
            # ------------------------------------------------

            if (
                before.channel is None
                and after.channel is not None
            ):

                await self._save_log_to_db(
                    guild.id,
                    member.id,
                    member.id,
                    "voice_join",
                    f"Joined {after.channel.name}"
                )

                embed = discord.Embed(
                    title="🔊 Voice Join",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )

                embed.add_field(
                    name="User",
                    value=member.mention,
                    inline=True
                )

                embed.add_field(
                    name="Channel",
                    value=after.channel.mention,
                    inline=True
                )

                await self._send_log(
                    guild,
                    embed
                )

                return

            # ------------------------------------------------
            # LEAVE / DISCONNECT
            # ------------------------------------------------

            if (
                before.channel is not None
                and after.channel is None
            ):

                event_time = datetime.now(
                    timezone.utc
                )

                entry = await self._find_voice_disconnect_actor(
                    guild=guild,
                    member=member,
                    event_time=event_time
                )

                actor = (
                    entry.user
                    if entry
                    else None
                )

                # ------------------------------------------------
                # IMPORTANT:
                #
                # Do NOT set actor = member when audit lookup
                # fails.
                #
                # A self-leave has no moderator disconnect actor.
                # A moderator disconnect has MEMBER_DISCONNECT
                # audit entry.
                #
                # Therefore:
                #
                # actor found  -> actual moderator
                # actor absent -> Self / Unknown
                # ------------------------------------------------

                if actor:
                    disconnected_by = actor.mention
                    actor_id = actor.id
                    detection = "Discord Audit Log"
                else:
                    disconnected_by = "Self / Unknown"
                    actor_id = member.id
                    detection = "No matching audit entry"

                await self._save_log_to_db(
                    guild.id,
                    member.id,
                    actor_id,
                    "voice_leave",
                    f"Left {before.channel.name}"
                )

                embed = discord.Embed(
                    title="🔇 Voice Leave",
                    color=discord.Color.red(),
                    timestamp=event_time
                )

                embed.add_field(
                    name="User",
                    value=member.mention,
                    inline=True
                )

                embed.add_field(
                    name="Channel",
                    value=before.channel.mention,
                    inline=True
                )

                embed.add_field(
                    name="Disconnected By",
                    value=disconnected_by,
                    inline=True
                )

                embed.add_field(
                    name="Detection",
                    value=detection,
                    inline=False
                )

                await self._send_log(
                    guild,
                    embed
                )

                return

            # ------------------------------------------------
            # MOVE / DRAG
            # ------------------------------------------------

            if (
                before.channel is not None
                and after.channel is not None
            ):

                event_time = datetime.now(
                    timezone.utc
                )

                entry = await self._find_voice_move_actor(
                    guild=guild,
                    member=member,
                    before_channel=before.channel,
                    after_channel=after.channel,
                    event_time=event_time
                )

                actor = (
                    entry.user
                    if entry
                    else None
                )

                if actor:

                    moved_by = actor.mention
                    actor_id = actor.id
                    detection = "Discord Audit Log"

                else:

                    moved_by = "Self / Unknown"
                    actor_id = member.id
                    detection = "No matching audit entry"

                await self._save_log_to_db(
                    guild.id,
                    member.id,
                    actor_id,
                    "voice_move",
                    (
                        f"Moved "
                        f"{before.channel.name} -> "
                        f"{after.channel.name}"
                    )
                )

                embed = discord.Embed(
                    title="🔀 Voice Move",
                    color=discord.Color.blue(),
                    timestamp=event_time
                )

                embed.add_field(
                    name="User",
                    value=member.mention,
                    inline=True
                )

                embed.add_field(
                    name="Moved By",
                    value=moved_by,
                    inline=True
                )

                embed.add_field(
                    name="From",
                    value=before.channel.mention,
                    inline=True
                )

                embed.add_field(
                    name="To",
                    value=after.channel.mention,
                    inline=True
                )

                embed.add_field(
                    name="Detection",
                    value=detection,
                    inline=False
                )

                await self._send_log(
                    guild,
                    embed
                )

        # ====================================================
        # SERVER MUTE / DEAFEN
        # ====================================================

        for (
            attr,
            title,
            action_type
        ) in (
            (
                "mute",
                "Server Mute",
                "server_mute"
            ),
            (
                "deaf",
                "Server Deafen",
                "server_deaf"
            )
        ):

            old_value = getattr(
                before,
                attr,
                False
            )

            new_value = getattr(
                after,
                attr,
                False
            )

            if old_value == new_value:
                continue

            await asyncio.sleep(0.5)

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.member_update,
                target_id=member.id
            )

            actor = (
                entry.user
                if entry
                else None
            )

            state = (
                "Enabled"
                if new_value
                else "Disabled"
            )

            await self._save_log_to_db(
                guild.id,
                member.id,
                actor.id if actor else None,
                action_type,
                f"{title} {state}"
            )

            embed = discord.Embed(
                title=f"🎙️ {title} {state}",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="User",
                value=member.mention,
                inline=True
            )

            embed.add_field(
                name="By",
                value=(
                    actor.mention
                    if actor
                    else "Unknown"
                ),
                inline=True
            )

            await self._send_log(
                guild,
                embed
            )

    # ========================================================
    # MEMBER ROLE ADD / REMOVE
    # ========================================================

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member
    ):

        guild = after.guild

        if guild is None:
            return

        if after.bot:
            return

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        before_roles = {
            role.id: role
            for role in before.roles
            if role != guild.default_role
        }

        after_roles = {
            role.id: role
            for role in after.roles
            if role != guild.default_role
        }

        added_ids = (
            set(after_roles)
            - set(before_roles)
        )

        removed_ids = (
            set(before_roles)
            - set(after_roles)
        )

        if (
            not added_ids
            and not removed_ids
        ):
            return

        entry = None

        for attempt in range(
            ROLE_AUDIT_ATTEMPTS
        ):

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.member_role_update,
                target_id=after.id,
                window=ROLE_AUDIT_WINDOW
            )

            if entry:
                break

            if attempt < (
                ROLE_AUDIT_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    ROLE_AUDIT_DELAY
                )

        actor = (
            entry.user
            if entry
            else None
        )

        actor_id = (
            actor.id
            if actor
            else None
        )

        # ----------------------------------------------------
        # ROLE ADDED
        # ----------------------------------------------------

        for role_id in added_ids:

            role = after_roles.get(role_id)

            if role is None:
                continue

            await self._save_log_to_db(
                guild.id,
                after.id,
                actor_id,
                "role_add",
                f"Role {role.name} added to {after}"
            )

            embed = discord.Embed(
                title="🟢 Role Added",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="Member",
                value=after.mention,
                inline=True
            )

            embed.add_field(
                name="Role",
                value=role.mention,
                inline=True
            )

            embed.add_field(
                name="Added By",
                value=(
                    actor.mention
                    if actor
                    else "Unknown"
                ),
                inline=True
            )

            await self._send_log(
                guild,
                embed
            )

        # ----------------------------------------------------
        # ROLE REMOVED
        # ----------------------------------------------------

        for role_id in removed_ids:

            role = before_roles.get(role_id)

            if role is None:
                continue

            await self._save_log_to_db(
                guild.id,
                after.id,
                actor_id,
                "role_remove",
                f"Role {role.name} removed from {after}"
            )

            embed = discord.Embed(
                title="🔴 Role Removed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="Member",
                value=after.mention,
                inline=True
            )

            embed.add_field(
                name="Role",
                value=role.mention,
                inline=True
            )

            embed.add_field(
                name="Removed By",
                value=(
                    actor.mention
                    if actor
                    else "Unknown"
                ),
                inline=True
            )

            await self._send_log(
                guild,
                embed
            )

    # ========================================================
    # ROLE CREATE
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_role_create(
        self,
        role: discord.Role
    ):

        guild = role.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        entry = None

        for attempt in range(
            ROLE_AUDIT_ATTEMPTS
        ):

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.role_create,
                target_id=role.id,
                window=ROLE_AUDIT_WINDOW
            )

            if entry:
                break

            if attempt < (
                ROLE_AUDIT_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    ROLE_AUDIT_DELAY
                )

        actor = (
            entry.user
            if entry
            else None
        )

        await self._save_log_to_db(
            guild.id,
            None,
            actor.id if actor else None,
            "role_create",
            f"Role created: {role.name}"
        )

        embed = discord.Embed(
            title="🟢 Role Created",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Role",
            value=role.mention,
            inline=True
        )

        embed.add_field(
            name="Name",
            value=f"`{role.name}`",
            inline=True
        )

        embed.add_field(
            name="Created By",
            value=(
                actor.mention
                if actor
                else "Unknown"
            ),
            inline=True
        )

        embed.add_field(
            name="Role ID",
            value=str(role.id),
            inline=False
        )

        await self._send_log(
            guild,
            embed
        )

    # ========================================================
    # ROLE DELETE
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_role_delete(
        self,
        role: discord.Role
    ):

        guild = role.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        entry = None

        for attempt in range(
            ROLE_AUDIT_ATTEMPTS
        ):

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.role_delete,
                target_id=role.id,
                window=ROLE_AUDIT_WINDOW
            )

            if entry:
                break

            if attempt < (
                ROLE_AUDIT_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    ROLE_AUDIT_DELAY
                )

        actor = (
            entry.user
            if entry
            else None
        )

        await self._save_log_to_db(
            guild.id,
            None,
            actor.id if actor else None,
            "role_delete",
            f"Role deleted: {role.name}"
        )

        embed = discord.Embed(
            title="🔴 Role Deleted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Role",
            value=f"`{role.name}`",
            inline=True
        )

        embed.add_field(
            name="Deleted By",
            value=(
                actor.mention
                if actor
                else "Unknown"
            ),
            inline=True
        )

        embed.add_field(
            name="Role ID",
            value=str(role.id),
            inline=False
        )

        await self._send_log(
            guild,
            embed
        )

    # ========================================================
    # ROLE PERMISSION DIFF
    # ========================================================

    def _diff_role_permissions(
        self,
        before: discord.Permissions,
        after: discord.Permissions
    ) -> list[str]:

        changes = []

        if before.value == after.value:
            return changes

        for permission_name in discord.Permissions.VALID_FLAGS:

            old_state = getattr(
                before,
                permission_name
            )

            new_state = getattr(
                after,
                permission_name
            )

            if old_state == new_state:
                continue

            display_name = (
                permission_name
                .replace("_", " ")
                .title()
            )

            old_text = (
                "✅ Allow"
                if old_state
                else "❌ Deny"
            )

            new_text = (
                "✅ Allow"
                if new_state
                else "❌ Deny"
            )

            changes.append(
                f"• **{display_name}:** "
                f"{old_text} ➜ {new_text}"
            )

        return changes

    # ========================================================
    # ROLE UPDATE
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_role_update(
        self,
        before: discord.Role,
        after: discord.Role
    ):

        guild = after.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        changes = []

        if before.name != after.name:

            changes.append(
                f"**Name:** "
                f"`{before.name}` ➜ "
                f"`{after.name}`"
            )

        permission_changes = (
            self._diff_role_permissions(
                before.permissions,
                after.permissions
            )
        )

        if permission_changes:

            changes.append(
                "**Permissions:**\n"
                + "\n".join(
                    permission_changes
                )
            )

        if before.colour != after.colour:

            changes.append(
                f"**Color:** "
                f"`{before.colour}` ➜ "
                f"`{after.colour}`"
            )

        if before.hoist != after.hoist:

            changes.append(
                f"**Hoisted:** "
                f"`{before.hoist}` ➜ "
                f"`{after.hoist}`"
            )

        if before.mentionable != after.mentionable:

            changes.append(
                f"**Mentionable:** "
                f"`{before.mentionable}` ➜ "
                f"`{after.mentionable}`"
            )

        if before.position != after.position:

            changes.append(
                f"**Position:** "
                f"`{before.position}` ➜ "
                f"`{after.position}`"
            )

        before_icon = getattr(
            before,
            "icon",
            None
        )

        after_icon = getattr(
            after,
            "icon",
            None
        )

        if before_icon != after_icon:

            changes.append(
                "**Role Icon:** Changed"
            )

        before_emoji = getattr(
            before,
            "unicode_emoji",
            None
        )

        after_emoji = getattr(
            after,
            "unicode_emoji",
            None
        )

        if before_emoji != after_emoji:

            changes.append(
                f"**Unicode Emoji:** "
                f"`{before_emoji or 'None'}` ➜ "
                f"`{after_emoji or 'None'}`"
            )

        if not changes:
            return

        # ----------------------------------------------------
        # ACTOR LOOKUP
        # ----------------------------------------------------

        entry = None

        for attempt in range(
            ROLE_AUDIT_ATTEMPTS
        ):

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.role_update,
                target_id=after.id,
                window=ROLE_AUDIT_WINDOW
            )

            if entry:
                break

            if attempt < (
                ROLE_AUDIT_ATTEMPTS - 1
            ):

                await asyncio.sleep(
                    ROLE_AUDIT_DELAY
                )

        actor = (
            entry.user
            if entry
            else None
        )

        await self._save_log_to_db(
            guild.id,
            None,
            actor.id if actor else None,
            "role_update",
            (
                f"Role {after.name} updated: "
                + " | ".join(changes)
            )
        )

        embed = discord.Embed(
            title="✏️ Role Updated",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Role",
            value=after.mention,
            inline=True
        )

        embed.add_field(
            name="Changed By",
            value=(
                actor.mention
                if actor
                else "Unknown"
            ),
            inline=True
        )

        change_text = "\n".join(
            changes
        )

        if len(change_text) > 4000:
            change_text = (
                change_text[:3997]
                + "..."
            )

        embed.add_field(
            name="Changes",
            value=change_text,
            inline=False
        )

        await self._send_log(
            guild,
            embed
        )

    # ========================================================
    # CHANNEL OVERWRITE DIFF
    # ========================================================

    def _diff_overwrites(
        self,
        before_ow,
        after_ow
    ) -> list[str]:

        differences = []

        before_dict = (
            dict(before_ow)
            if before_ow
            else {}
        )

        after_dict = (
            dict(after_ow)
            if after_ow
            else {}
        )

        aliases = {
            "view_channel": "View Channel",
            "read_messages": "View Channel",
            "send_messages": "Send Messages",
            "connect": "Connect",
            "speak": "Speak",
            "stream": "Stream",
            "use_voice_activation": "Voice Activity",
            "manage_messages": "Manage Messages",
            "manage_channels": "Manage Channels"
        }

        def format_value(value):

            if value is True:
                return "✅ Allow"

            if value is False:
                return "❌ Deny"

            return "⚪ Neutral"

        permissions = (
            set(before_dict)
            | set(after_dict)
        )

        for permission in sorted(
            permissions
        ):

            old_value = before_dict.get(
                permission
            )

            new_value = after_dict.get(
                permission
            )

            if old_value == new_value:
                continue

            display_name = aliases.get(
                permission,
                permission.replace(
                    "_",
                    " "
                ).title()
            )

            differences.append(
                f"• **{display_name}:** "
                f"{format_value(old_value)} ➜ "
                f"{format_value(new_value)}"
            )

        return differences

    # ========================================================
    # CHANNEL UPDATE
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before,
        after
    ):

        guild = before.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        changes = []

        if before.name != after.name:

            changes.append(
                f"Name: `{before.name}` ➜ "
                f"`{after.name}`"
            )

        if (
            getattr(before, "topic", None)
            != getattr(after, "topic", None)
        ):

            changes.append(
                "Topic changed"
            )

        if before.overwrites != after.overwrites:

            targets = (
                set(before.overwrites.keys())
                | set(after.overwrites.keys())
            )

            for target in targets:

                old_ow = before.overwrites.get(
                    target
                )

                new_ow = after.overwrites.get(
                    target
                )

                if old_ow == new_ow:
                    continue

                diffs = self._diff_overwrites(
                    old_ow,
                    new_ow
                )

                if not diffs:
                    continue

                target_name = getattr(
                    target,
                    "mention",
                    str(target)
                )

                changes.append(
                    f"Permissions for {target_name}:\n"
                    + "\n".join(diffs)
                )

        if not changes:
            return

        entry = None

        for attempt in range(
            8
        ):

            entry = await self._find_audit_entry(
                guild,
                discord.AuditLogAction.channel_update,
                target_id=after.id
            )

            if entry:
                break

            if attempt < 7:
                await asyncio.sleep(0.5)

        if entry is None:

            for action in (
                discord.AuditLogAction.overwrite_update,
                discord.AuditLogAction.overwrite_create,
                discord.AuditLogAction.overwrite_delete
            ):

                entry = await self._find_audit_entry(
                    guild,
                    action,
                    target_id=after.id
                )

                if entry:
                    break

        actor = (
            entry.user
            if entry
            else None
        )

        await self._save_log_to_db(
            guild.id,
            None,
            actor.id if actor else None,
            "channel_update",
            f"Channel {after.name} modified"
        )

        embed = discord.Embed(
            title="✏️ Channel Updated",
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Channel",
            value=after.mention,
            inline=True
        )

        embed.add_field(
            name="By",
            value=(
                actor.mention
                if actor
                else "Unknown"
            ),
            inline=True
        )

        change_text = "\n".join(
            changes
        )

        if len(change_text) > 1024:
            change_text = (
                change_text[:1021]
                + "..."
            )

        embed.add_field(
            name="Changes",
            value=change_text,
            inline=False
        )

        await self._send_log(
            guild,
            embed
        )

    # ========================================================
    # MESSAGE CACHE
    # ========================================================

    @commands.Cog.listener()
    async def on_message(
        self,
        message: discord.Message
    ):

        if message.guild is None:
            return

        if message.author.bot:
            return

        self.message_cache.add(
            message.id,
            {
                "content": message.content,
                "attachments": [
                    attachment.filename
                    for attachment
                    in message.attachments
                ]
            }
        )

    # ========================================================
    # MESSAGE DELETE
    # ========================================================

    @commands.Cog.listener()
    async def on_message_delete(
        self,
        message: discord.Message
    ):

        if message.guild is None:
            return

        if message.author.bot:
            return

        guild = message.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        cached = self.message_cache.get(
            message.id
        )

        content = (
            cached.get("content")
            if cached
            else message.content
        )

        content = content or "*No text*"

        entry = await self._find_audit_entry(
            guild,
            discord.AuditLogAction.message_delete,
            target_id=message.author.id
        )

        actor = (
            entry.user
            if entry
            else None
        )

        await self._save_log_to_db(
            guild.id,
            message.author.id,
            actor.id if actor else None,
            "message_delete",
            f"Deleted in #{message.channel.name}"
        )

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.set_author(
            name=str(message.author),
            icon_url=message.author.display_avatar.url
        )

        embed.add_field(
            name="Author",
            value=message.author.mention,
            inline=True
        )

        embed.add_field(
            name="Channel",
            value=message.channel.mention,
            inline=True
        )

        if (
            actor
            and actor.id != message.author.id
        ):

            embed.add_field(
                name="Deleted By",
                value=actor.mention,
                inline=True
            )

        if len(content) > 1024:
            content = content[:1021] + "..."

        embed.add_field(
            name="Content",
            value=content,
            inline=False
        )

        await self._send_log(
            guild,
            embed
        )

        self.message_cache.remove(
            message.id
        )

    # ========================================================
    # MESSAGE EDIT
    # ========================================================

    @commands.Cog.listener()
    async def on_message_edit(
        self,
        before: discord.Message,
        after: discord.Message
    ):

        if before.guild is None:
            return

        if before.author.bot:
            return

        if before.content == after.content:
            return

        guild = before.guild

        if not await self._is_module_enabled(
            guild.id
        ):
            return

        old_content = (
            before.content
            or "*Empty*"
        )

        new_content = (
            after.content
            or "*Empty*"
        )

        if len(old_content) > 1020:
            old_content = old_content[:1017] + "..."

        if len(new_content) > 1020:
            new_content = new_content[:1017] + "..."

        await self._save_log_to_db(
            guild.id,
            before.author.id,
            before.author.id,
            "message_edit",
            f"Edited in #{before.channel.name}"
        )

        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.set_author(
            name=str(before.author),
            icon_url=before.author.display_avatar.url
        )

        embed.add_field(
            name="Before",
            value=old_content,
            inline=False
        )

        embed.add_field(
            name="After",
            value=new_content,
            inline=False
        )

        embed.set_footer(
            text=f"Channel: #{before.channel.name}"
        )

        await self._send_log(
            guild,
            embed
        )

    # ========================================================
    # LOGGER SETTINGS
    # ========================================================

    async def _is_module_enabled(
        self,
        guild_id: int
    ) -> bool:

        try:

            channel_id = await Config.get_guild_setting(
                guild_id,
                "log_channel_id"
            )

            return bool(channel_id)

        except Exception:

            logger.exception(
                "Failed to read log channel setting "
                "for guild %s",
                guild_id
            )

            return False

    async def _get_log_channel(
        self,
        guild: discord.Guild
    ):

        try:

            channel_id = await Config.get_guild_setting(
                guild.id,
                "log_channel_id"
            )

            if not channel_id:
                return None

            channel = guild.get_channel(
                channel_id
            )

            if channel:
                return channel

            return await guild.fetch_channel(
                channel_id
            )

        except discord.NotFound:

            logger.warning(
                "Configured log channel does not exist "
                "in guild %s",
                guild.id
            )

        except discord.Forbidden:

            logger.warning(
                "Cannot access log channel "
                "in guild %s",
                guild.id
            )

        except discord.HTTPException as exc:

            logger.warning(
                "Failed to fetch log channel "
                "in guild %s: %s",
                guild.id,
                exc
            )

        except Exception:

            logger.exception(
                "Unexpected log channel error "
                "in guild %s",
                guild.id
            )

        return None

    # ========================================================
    # SEND LOG
    # ========================================================

    async def _send_log(
        self,
        guild: discord.Guild,
        embed: discord.Embed
    ):

        channel = await self._get_log_channel(
            guild
        )

        if channel is None:
            return

        try:

            await channel.send(
                embed=embed
            )

        except discord.Forbidden:

            logger.warning(
                "Cannot send logger message "
                "in guild %s",
                guild.id
            )

        except discord.HTTPException as exc:

            logger.warning(
                "Failed to send logger message "
                "in guild %s: %s",
                guild.id,
                exc
            )

        except Exception:

            logger.exception(
                "Unexpected logger send error "
                "in guild %s",
                guild.id
            )

    # ========================================================
    # /LOG SETUP
    # ========================================================

    @log_group.command(
        name="setup",
        description="Set the server logging channel."
    )
    @app_commands.default_permissions(
        manage_guild=True
    )
    async def log_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):

        if interaction.guild_id is None:

            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True
            )

            return

        await Config.set_guild_setting(
            interaction.guild_id,
            "log_channel_id",
            channel.id
        )

        await interaction.response.send_message(
            f"✅ Log channel: {channel.mention}",
            ephemeral=True
        )

    # ========================================================
    # /LOG HISTORY
    # ========================================================

    @log_group.command(
        name="history",
        description="Show recent history of a member."
    )
    @app_commands.default_permissions(
        manage_guild=True
    )
    async def log_history(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        days: app_commands.Range[int, 1, 90] = 7
    ):

        await self._show_user_history(
            interaction,
            user,
            days
        )

    # ========================================================
    # /LOGSEARCH
    #
    # Example:
    #
    # /logsearch user:@Rahim days:30
    #
    # ========================================================

    @app_commands.command(
        name="logsearch",
        description="Search a specific member's logger history."
    )
    @app_commands.default_permissions(
        manage_guild=True
    )
    @app_commands.describe(
        user="The member whose logs you want to search.",
        days="How many days back to search (1-90)."
    )
    async def logsearch(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        days: app_commands.Range[int, 1, 90] = 7
    ):

        await self._show_user_history(
            interaction,
            user,
            days
        )

    # ========================================================
    # USER HISTORY
    #
    # OPTIMIZED QUERY
    #
    # Old:
    #
    # WHERE guild_id=?
    # AND created_at >= ?
    # AND (user_id=? OR actor_id=?)
    #
    # New:
    #
    # Two index-friendly branches:
    #
    #   user_id
    #   actor_id
    #
    # combined with UNION.
    #
    # This allows SQLite to use:
    #
    # idx_audit_guild_user_time
    # idx_audit_guild_actor_time
    #
    # instead of doing a broader OR scan.
    # ========================================================

    async def _count_user_history(
        self,
        guild_id: int,
        user_id: int,
        cutoff: str
    ) -> int:

        if self.count_db is None:
            raise RuntimeError("Logger count database is unavailable")

        async with self.count_db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT id
                FROM audit_history
                WHERE guild_id = ?
                  AND user_id = ?
                  AND created_at >= ?

                UNION ALL

                SELECT id
                FROM audit_history
                WHERE guild_id = ?
                  AND actor_id = ?
                  AND created_at >= ?
                  AND (user_id IS NULL OR user_id != ?)
            )
            """,
            (
                guild_id,
                user_id,
                cutoff,
                guild_id,
                user_id,
                cutoff,
                user_id
            )
        ) as cursor:
            row = await cursor.fetchone()

        return int(row[0]) if row else 0

    async def _fetch_user_history_page(
        self,
        guild_id: int,
        user_id: int,
        cutoff: str,
        before_id: Optional[int] = None
    ):

        if self.read_db is None:
            raise RuntimeError("Logger read database is unavailable")

        cursor_clause = " AND id < ?" if before_id is not None else ""
        query = f"""
            WITH target_rows AS (
                SELECT
                    action_type,
                    details,
                    created_at,
                    user_id,
                    actor_id,
                    id
                FROM audit_history
                WHERE guild_id = ?
                  AND user_id = ?
                  AND created_at >= ?
                  {cursor_clause}
                ORDER BY id DESC
                LIMIT ?

            ), actor_rows AS (
                SELECT
                    action_type,
                    details,
                    created_at,
                    user_id,
                    actor_id,
                    id
                FROM audit_history
                WHERE guild_id = ?
                  AND actor_id = ?
                  AND created_at >= ?
                  AND (user_id IS NULL OR user_id != ?)
                  {cursor_clause}
                ORDER BY id DESC
                LIMIT ?
            )
            SELECT
                action_type,
                details,
                created_at,
                user_id,
                actor_id,
                id
            FROM (
                SELECT * FROM target_rows
                UNION ALL
                SELECT * FROM actor_rows
            )
            ORDER BY id DESC
            LIMIT ?
        """
        parameters = [guild_id, user_id, cutoff]
        if before_id is not None:
            parameters.append(before_id)
        parameters.append(HISTORY_PAGE_SIZE + 1)

        parameters.extend((guild_id, user_id, cutoff, user_id))
        if before_id is not None:
            parameters.append(before_id)
        parameters.extend((HISTORY_PAGE_SIZE + 1, HISTORY_PAGE_SIZE + 1))

        async with self.read_db.execute(query, parameters) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def _build_history_embed(
        user: discord.Member,
        days: int,
        total_count: Optional[int],
        page_index: int,
        has_next: bool,
        rows
    ):
        result_text = (
            str(total_count)
            if total_count is not None
            else "Counting…"
        )

        embed = discord.Embed(
            title="🔎 Logger Search",
            description=(
                f"Member: {user.mention}\n"
                f"Period: Last {days} day(s)\n"
                f"Results: {result_text}"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        footer = f"Page {page_index + 1}"
        if has_next:
            footer += " • More results"
        embed.set_footer(text=footer)

        for (
            action_type,
            details,
            created_at,
            target_id,
            actor_id,
            _row_id
        ) in rows:

            if target_id == user.id and actor_id == user.id:
                relation = "Target + Actor"
            elif actor_id == user.id:
                relation = "Actor"
            else:
                relation = "Target"

            name = f"{action_type.upper()} • {created_at}"
            if len(name) > 128:
                name = name[:125] + "..."

            value = f"**{relation}**\n{details or '*No details*'}"
            if len(value) > 1024:
                value = value[:1021] + "..."

            embed.add_field(
                name=name,
                value=value,
                inline=False
            )

        return embed

    async def _show_user_history(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        days: int
    ):

        await interaction.response.defer(ephemeral=True)
        await self._db_ready.wait()

        if self.read_db is None:
            await interaction.followup.send(
                "❌ Logger database is unavailable.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        try:
            rows = await self._fetch_user_history_page(
                guild_id,
                user.id,
                cutoff
            )
            if not rows:
                await interaction.followup.send(
                    (
                        f"📭 No logs found for {user.mention} in the last "
                        f"{days} day(s)."
                    ),
                    ephemeral=True
                )
                return

        except Exception:
            logger.exception("Failed to query member logger history")
            await interaction.followup.send(
                "❌ Failed to search logger history.",
                ephemeral=True
            )
            return

        has_next = len(rows) > HISTORY_PAGE_SIZE
        view = None
        if has_next:
            view = HistoryPaginationView(
                self,
                user,
                guild_id,
                days,
                cutoff,
                rows,
                has_next
            )

        embed = self._build_history_embed(
            user,
            days,
            None,
            0,
            has_next,
            rows[:HISTORY_PAGE_SIZE]
        )
        message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True
        )

        if view is not None:
            view.message = message

        task = asyncio.create_task(
            self._refresh_history_result_count(
                guild_id,
                user,
                days,
                cutoff,
                message,
                view,
                rows[:HISTORY_PAGE_SIZE],
                has_next
            )
        )
        self._history_count_tasks.add(task)
        task.add_done_callback(self._history_count_tasks.discard)

    async def _refresh_history_result_count(
        self,
        guild_id: int,
        user: discord.Member,
        days: int,
        cutoff: str,
        message: discord.Message,
        view: Optional[HistoryPaginationView],
        rows,
        has_next: bool
    ):
        try:
            total_count = await self._count_user_history(
                guild_id,
                user.id,
                cutoff
            )
            if view is not None:
                await view.set_result_count(total_count)
            else:
                embed = self._build_history_embed(
                    user,
                    days,
                    total_count,
                    0,
                    has_next,
                    rows
                )
                await message.edit(embed=embed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to update logger history result count")


# ============================================================
# EXTENSION SETUP
# ============================================================

async def setup(
    bot: commands.Bot
):

    await bot.add_cog(
        Logger(bot)
    )
