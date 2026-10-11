"""
Moderation & Role Management Execution Engine.
Handles rate-limited Discord operations, durable temporary ban scheduling,
safe DM dispatches, hierarchy enforcement, bulk batching, and crash recovery.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import discord
from discord.ext import commands

from services.moderation_db import ModerationDB

logger = logging.getLogger("ModerationEngine")


class ModerationEngine:
    _instance: Optional["ModerationEngine"] = None

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = ModerationDB.get_instance()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._bulk_active_jobs: Dict[str, asyncio.Task] = {}
        self._action_lock = asyncio.Lock()
        self._rate_limit_delay = 1.2  # Seconds between individual bulk operations

    @classmethod
    def get_instance(cls, bot: Optional[commands.Bot] = None) -> "ModerationEngine":
        if cls._instance is None:
            if bot is None:
                raise ValueError("ModerationEngine must be initialized with bot reference first")
            cls._instance = cls(bot)
        return cls._instance

    async def start(self):
        """Starts background watchdog and runs startup reconciliation."""
        await self.db.init_db()
        await self.startup_reconciliation()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._scheduled_watchdog_loop())
            logger.info("ModerationEngine background watchdog started")

    async def stop(self):
        """Cancels running watchdog and background bulk jobs."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        for jid, task in list(self._bulk_active_jobs.items()):
            if not task.done():
                task.cancel()
        logger.info("ModerationEngine stopped")

    # =========================================================================
    # Hierarchy & Permission Validation
    # =========================================================================

    def validate_hierarchy(self, guild: discord.Guild, target: discord.Member,
                           role: Optional[discord.Role] = None) -> Tuple[bool, str]:
        """
        Validates Discord role hierarchy safety:
        - Target cannot be server owner.
        - Target cannot be the bot itself.
        - Target cannot have Administrator if destructive action.
        - Bot's top role must strictly outrank target's top role.
        - If role is provided, bot's top role must strictly outrank the role.
        """
        if not guild or not target:
            return False, "Target or Guild not found."

        if target.id == guild.owner_id:
            return False, "Action denied: Cannot moderate the Server Owner."

        if self.bot.user and target.id == self.bot.user.id:
            return False, "Action denied: Cannot moderate the bot itself."

        bot_member = guild.me
        if not bot_member:
            return False, "Bot is not recognized in the target guild."

        # Bot hierarchy check against target
        target_pos = getattr(getattr(target, "top_role", None), "position", 0)
        bot_pos = getattr(getattr(bot_member, "top_role", None), "position", 0)
        if target_pos >= bot_pos and target.id != bot_member.id:
            bot_role_name = getattr(getattr(bot_member, "top_role", None), "name", "Bot Role")
            target_role_name = getattr(getattr(target, "top_role", None), "name", "Target Role")
            return False, f"Role hierarchy error: Bot's top role ({bot_role_name}) does not outrank target's top role ({target_role_name})."

        # Role hierarchy check
        if role is not None:
            is_def = role.is_default() if callable(getattr(role, "is_default", None)) else getattr(role, "is_default", False)
            if is_def:
                return False, "Action denied: Cannot assign or remove the default @everyone role."
            if getattr(role, "managed", False):
                return False, f"Action denied: Role '{getattr(role, 'name', 'Unknown')}' is managed by an integration/bot and cannot be assigned manually."
            role_pos = getattr(role, "position", 0)
            if role_pos >= bot_pos:
                bot_role_name = getattr(getattr(bot_member, "top_role", None), "name", "Bot Role")
                return False, f"Role hierarchy error: Bot's top role ({bot_role_name}) does not outrank target role ({getattr(role, 'name', 'Unknown')})."

        return True, ""

    def is_protected_member(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Determines if a member is protected from bulk actions (Owner, Bot, Admin)."""
        if not member or not guild:
            return True
        if member.id == guild.owner_id:
            return True
        if self.bot.user and member.id == self.bot.user.id:
            return True
        if member.bot:
            return True
        if member.guild_permissions.administrator:
            return True
        return False

    # =========================================================================
    # Safe DM Notification Service
    # =========================================================================

    def format_dm_message(self, template: str, user_name: str, guild_name: str,
                          role_name: str = "", reason: str = "",
                          duration_str: str = "", action_name: str = "") -> str:
        """Replaces documented placeholders and sanitizes mention injection."""
        replacements = {
            "{user}": user_name,
            "{server}": guild_name,
            "{role}": role_name or "N/A",
            "{reason}": reason or "No reason provided",
            "{duration}": duration_str or "N/A",
            "{action}": action_name or "Moderation Notice"
        }
        msg = template
        for placeholder, val in replacements.items():
            msg = msg.replace(placeholder, str(val))

        # Sanitize against mass mentions using literal zero-width space (\u200b)
        msg = re.sub(r"@(everyone|here)", "@\u200b\\1", msg, flags=re.IGNORECASE)
        return msg.strip()

    async def send_dm_notification(self, user: discord.abc.User, text: str) -> Tuple[str, Optional[str]]:
        """
        Sends an isolated DM with AllowedMentions.none().
        Returns (dm_status, error_message).
        """
        if not text or not text.strip():
            return "skipped", None

        try:
            # Enforce zero mentions in DMs
            allowed_mentions = discord.AllowedMentions.none()
            await user.send(content=text, allowed_mentions=allowed_mentions)
            return "sent", None
        except discord.Forbidden:
            return "failed", "User DMs are disabled or bot is blocked"
        except discord.HTTPException as e:
            return "failed", f"Discord HTTP error: {e}"
        except Exception as e:
            return "failed", str(e)

    # =========================================================================
    # Individual Moderation Actions
    # =========================================================================

    async def execute_role_assign(self, guild_id: int, target_id: int, role_id: int,
                                  actor_id: str, actor_name: str, reason: str = "",
                                  notify_dm: bool = False, custom_dm: str = "",
                                  idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Assigns an allowed role to a server member."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        key = idempotency_key or f"role_add_{guild_id}_{target_id}_{role_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        if not member:
            try:
                member = await guild.fetch_member(target_id)
            except Exception:
                return {"success": False, "error": "Target member not found in server"}

        role = guild.get_role(role_id)
        if not role:
            return {"success": False, "error": "Target role not found in server"}

        if role in member.roles:
            return {"success": True, "message": f"Member already possesses role '{role.name}'"}

        valid, err = self.validate_hierarchy(guild, member, role)
        if not valid:
            return {"success": False, "error": err}

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=str(member),
            actor_id=actor_id, actor_name=actor_name, action_type="role_add", duration_seconds=None,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        dm_status = "not_requested"
        dm_error = None
        if notify_dm:
            tpl_data = await self.db.get_message_template(guild_id, "role_assigned")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                rendered = self.format_dm_message(text, user_name=member.name, guild_name=guild.name, role_name=role.name, reason=reason)
                dm_status, dm_error = await self.send_dm_notification(member, rendered)

        try:
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Assigned by {actor_name} via Dashboard"
            await member.add_roles(role, reason=audit_reason)
            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=str(member), action_type="ROLE_ASSIGN", role_id=role_id, reason=reason,
                outcome="SUCCESS", dm_status=dm_status, details=f"Assigned role '{role.name}'"
            )
            return {"success": True, "action_id": action_id, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Manage Roles' permission or hierarchy restricts assignment"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_role_remove(self, guild_id: int, target_id: int, role_id: int,
                                  actor_id: str, actor_name: str, reason: str = "",
                                  notify_dm: bool = False, custom_dm: str = "",
                                  idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Removes a role from a server member."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        key = idempotency_key or f"role_remove_{guild_id}_{target_id}_{role_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        if not member:
            try:
                member = await guild.fetch_member(target_id)
            except Exception:
                return {"success": False, "error": "Target member not found in server"}

        role = guild.get_role(role_id)
        if not role:
            return {"success": False, "error": "Target role not found in server"}

        if role not in member.roles:
            return {"success": True, "message": f"Member does not possess role '{role.name}'"}

        valid, err = self.validate_hierarchy(guild, member, role)
        if not valid:
            return {"success": False, "error": err}

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=str(member),
            actor_id=actor_id, actor_name=actor_name, action_type="role_remove", duration_seconds=None,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        dm_status = "not_requested"
        dm_error = None
        if notify_dm:
            tpl_data = await self.db.get_message_template(guild_id, "role_removed")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                rendered = self.format_dm_message(text, user_name=member.name, guild_name=guild.name, role_name=role.name, reason=reason)
                dm_status, dm_error = await self.send_dm_notification(member, rendered)

        try:
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Removed by {actor_name} via Dashboard"
            await member.remove_roles(role, reason=audit_reason)
            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=str(member), action_type="ROLE_REMOVE", role_id=role_id, reason=reason,
                outcome="SUCCESS", dm_status=dm_status, details=f"Removed role '{role.name}'"
            )
            return {"success": True, "action_id": action_id, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Manage Roles' permission or hierarchy restricts removal"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_timeout(self, guild_id: int, target_id: int, duration_seconds: int,
                               actor_id: str, actor_name: str, reason: str = "",
                               notify_dm: bool = False, custom_dm: str = "",
                               idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Applies a Discord timeout (communication disabled) up to 28 days."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        max_timeout = 28 * 24 * 3600
        duration_seconds = max(10, min(duration_seconds, max_timeout))

        key = idempotency_key or f"timeout_{guild_id}_{target_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        if not member:
            try:
                member = await guild.fetch_member(target_id)
            except Exception:
                return {"success": False, "error": "Target member not found in server"}

        valid, err = self.validate_hierarchy(guild, member)
        if not valid:
            return {"success": False, "error": err}

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=str(member),
            actor_id=actor_id, actor_name=actor_name, action_type="timeout", duration_seconds=duration_seconds,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        def _fmt_dur(secs):
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            d, h = divmod(h, 24)
            parts = []
            if d: parts.append(f"{d}d")
            if h: parts.append(f"{h}h")
            if m: parts.append(f"{m}m")
            if s and not parts: parts.append(f"{s}s")
            return " ".join(parts) or f"{secs}s"

        dur_str = _fmt_dur(duration_seconds)
        dm_status = "not_requested"
        dm_error = None
        if notify_dm:
            tpl_data = await self.db.get_message_template(guild_id, "timeout")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                rendered = self.format_dm_message(text, user_name=member.name, guild_name=guild.name, reason=reason, duration_str=dur_str, action_name="Timeout")
                dm_status, dm_error = await self.send_dm_notification(member, rendered)

        try:
            until = discord.utils.utcnow() + timedelta(seconds=duration_seconds)
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Timed out by {actor_name} via Dashboard"
            await member.timeout(until, reason=audit_reason)
            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=str(member), action_type="TIMEOUT", reason=reason, outcome="SUCCESS",
                dm_status=dm_status, details=f"Timeout for {dur_str} (until {until.isoformat()})"
            )
            return {"success": True, "action_id": action_id, "duration": dur_str, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Moderate Members' permission or hierarchy restricts timeout"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_kick(self, guild_id: int, target_id: int, actor_id: str,
                           actor_name: str, reason: str = "", notify_dm: bool = False,
                           custom_dm: str = "", idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Kicks a member from the guild with pre-action DM dispatch."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        key = idempotency_key or f"kick_{guild_id}_{target_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        if not member:
            try:
                member = await guild.fetch_member(target_id)
            except Exception:
                return {"success": False, "error": "Target member not found in server"}

        valid, err = self.validate_hierarchy(guild, member)
        if not valid:
            return {"success": False, "error": err}

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=str(member),
            actor_id=actor_id, actor_name=actor_name, action_type="kick", duration_seconds=None,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        # Pre-action DM: Dispatch BEFORE kick so mutual server condition is met
        dm_status = "not_requested"
        dm_error = None
        if notify_dm:
            tpl_data = await self.db.get_message_template(guild_id, "kick")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                rendered = self.format_dm_message(text, user_name=member.name, guild_name=guild.name, reason=reason, action_name="Kick")
                dm_status, dm_error = await self.send_dm_notification(member, rendered)

        try:
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Kicked by {actor_name} via Dashboard"
            await member.kick(reason=audit_reason)
            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=str(member), action_type="KICK", reason=reason, outcome="SUCCESS",
                dm_status=dm_status, details=f"Kicked member"
            )
            return {"success": True, "action_id": action_id, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Kick Members' permission or hierarchy restricts kick"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_ban(self, guild_id: int, target_id: int, actor_id: str,
                          actor_name: str, reason: str = "", delete_message_days: int = 0,
                          notify_dm: bool = False, custom_dm: str = "",
                          idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Permanently bans a user/member and cancels any pending temporary ban jobs."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        key = idempotency_key or f"ban_{guild_id}_{target_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        user_obj = member
        if not user_obj:
            try:
                user_obj = await self.bot.fetch_user(target_id)
            except Exception:
                user_obj = discord.Object(id=target_id)

        if member:
            valid, err = self.validate_hierarchy(guild, member)
            if not valid:
                return {"success": False, "error": err}

        target_name = str(user_obj) if hasattr(user_obj, "name") else str(target_id)

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=target_name,
            actor_id=actor_id, actor_name=actor_name, action_type="ban", duration_seconds=None,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        dm_status = "not_requested"
        dm_error = None
        if notify_dm and hasattr(user_obj, "send"):
            tpl_data = await self.db.get_message_template(guild_id, "ban")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                u_name = getattr(user_obj, "name", str(target_id))
                rendered = self.format_dm_message(text, user_name=u_name, guild_name=guild.name, reason=reason, action_name="Permanent Ban")
                dm_status, dm_error = await self.send_dm_notification(user_obj, rendered)

        try:
            delete_seconds = max(0, min(delete_message_days, 7)) * 86400
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Banned by {actor_name} via Dashboard"
            await guild.ban(user_obj, reason=audit_reason, delete_message_seconds=delete_seconds)

            # Supersede any scheduled tempbans for this target
            await self.db.cancel_scheduled_actions_for_target(guild_id, target_id, "unban_tempban")

            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=target_name, action_type="BAN", reason=reason, outcome="SUCCESS",
                dm_status=dm_status, details=f"Permanently banned (deleted {delete_message_days}d messages)"
            )
            return {"success": True, "action_id": action_id, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Ban Members' permission or hierarchy restricts ban"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_tempban(self, guild_id: int, target_id: int, duration_seconds: int,
                              actor_id: str, actor_name: str, reason: str = "",
                              delete_message_days: int = 0, notify_dm: bool = False,
                              custom_dm: str = "", idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Temporarily bans a member and stores a durable expiry job surviving bot restarts."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        if duration_seconds < 60:
            return {"success": False, "error": "Temporary ban duration must be at least 1 minute (60 seconds)"}

        key = idempotency_key or f"tempban_{guild_id}_{target_id}_{uuid.uuid4().hex[:8]}"
        existing = await self.db.get_action_by_idempotency_key(key)
        if existing and existing["status"] == "completed":
            return {"success": True, "action_id": existing["id"], "already_completed": True}

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        member = guild.get_member(target_id)
        user_obj = member
        if not user_obj:
            try:
                user_obj = await self.bot.fetch_user(target_id)
            except Exception:
                user_obj = discord.Object(id=target_id)

        if member:
            valid, err = self.validate_hierarchy(guild, member)
            if not valid:
                return {"success": False, "error": err}

        target_name = str(user_obj) if hasattr(user_obj, "name") else str(target_id)

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=target_name,
            actor_id=actor_id, actor_name=actor_name, action_type="tempban", duration_seconds=duration_seconds,
            reason=reason, dm_status="pending" if notify_dm else "not_requested"
        )

        def _fmt_dur(secs):
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            d, h = divmod(h, 24)
            parts = []
            if d: parts.append(f"{d}d")
            if h: parts.append(f"{h}h")
            if m: parts.append(f"{m}m")
            if s and not parts: parts.append(f"{s}s")
            return " ".join(parts) or f"{secs}s"

        dur_str = _fmt_dur(duration_seconds)
        dm_status = "not_requested"
        dm_error = None
        if notify_dm and hasattr(user_obj, "send"):
            tpl_data = await self.db.get_message_template(guild_id, "tempban")
            text = custom_dm if custom_dm.strip() else (tpl_data["template_text"] if tpl_data["is_enabled"] else "")
            if text:
                u_name = getattr(user_obj, "name", str(target_id))
                rendered = self.format_dm_message(text, user_name=u_name, guild_name=guild.name, reason=reason, duration_str=dur_str, action_name="Temporary Ban")
                dm_status, dm_error = await self.send_dm_notification(user_obj, rendered)

        try:
            delete_seconds = max(0, min(delete_message_days, 7)) * 86400
            audit_reason = f"[{actor_name} | TempBan {dur_str}] {reason}".strip()
            await guild.ban(user_obj, reason=audit_reason, delete_message_seconds=delete_seconds)

            # Persist durable scheduled action
            trigger_dt = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            trigger_iso = trigger_dt.strftime("%Y-%m-%d %H:%M:%S")
            sched_id = await self.db.create_scheduled_action(
                action_type="unban_tempban", guild_id=guild_id, target_id=target_id,
                actor_id=actor_id, reason=f"Expiry of tempban issued by {actor_name}",
                trigger_at=trigger_iso, related_action_id=action_id
            )

            await self.db.update_action_status(action_id, status="completed", dm_status=dm_status, dm_error=dm_error)
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=target_name, action_type="TEMPBAN", reason=reason, outcome="SUCCESS",
                dm_status=dm_status, details=f"Temporarily banned for {dur_str} (Job #{sched_id}, until {trigger_iso})"
            )
            return {"success": True, "action_id": action_id, "job_id": sched_id, "expires_at": trigger_iso, "duration": dur_str, "dm_status": dm_status}
        except discord.Forbidden:
            err_msg = "Bot lacks 'Ban Members' permission or hierarchy restricts ban"
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=err_msg)
            return {"success": False, "error": err_msg}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", dm_status=dm_status, error_message=str(e))
            return {"success": False, "error": str(e)}

    async def execute_unban(self, guild_id: int, target_id: int, actor_id: str,
                            actor_name: str, reason: str = "", notify_dm: bool = False,
                            idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Unbans a user and cancels any pending temporary-ban expiration job."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        key = idempotency_key or f"unban_{guild_id}_{target_id}_{uuid.uuid4().hex[:8]}"
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        # Verify target is currently banned
        try:
            ban_entry = await guild.fetch_ban(discord.Object(id=target_id))
            target_user = ban_entry.user
            target_name = str(target_user)
        except discord.NotFound:
            # User is not banned
            await self.db.cancel_scheduled_actions_for_target(guild_id, target_id, "unban_tempban")
            return {"success": True, "already_unbanned": True, "message": "User is not currently banned"}
        except discord.Forbidden:
            return {"success": False, "error": "Bot lacks 'Ban Members' permission to view or manage bans"}
        except Exception as e:
            return {"success": False, "error": str(e)}

        action_id = await self.db.record_action_queued(
            idempotency_key=key, guild_id=guild_id, target_id=target_id, target_name=target_name,
            actor_id=actor_id, actor_name=actor_name, action_type="unban", duration_seconds=None,
            reason=reason, dm_status="not_requested"
        )

        try:
            audit_reason = f"[{actor_name}] {reason}".strip() if reason else f"Unbanned by {actor_name} via Dashboard"
            await guild.unban(target_user, reason=audit_reason)

            # Invalidate any pending tempban job
            await self.db.cancel_scheduled_actions_for_target(guild_id, target_id, "unban_tempban")

            await self.db.update_action_status(action_id, status="completed", dm_status="not_requested")
            await self.db.record_audit_log(
                guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, target_id=target_id,
                target_name=target_name, action_type="UNBAN", reason=reason, outcome="SUCCESS",
                dm_status="not_requested", details="Unbanned user"
            )
            return {"success": True, "action_id": action_id, "target_name": target_name}
        except Exception as e:
            await self.db.update_action_status(action_id, status="failed", error_message=str(e))
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Temporary Ban Watchdog & Crash Reconciliation
    # =========================================================================

    async def startup_reconciliation(self):
        """
        Reconciles pending temporary bans on startup:
        1. Identifies overdue jobs.
        2. Reconciles stored state with Discord API.
        3. Executes only actions that are still safe and authorized.
        4. Invalids superseded or already-unbanned jobs.
        """
        logger.info("[Startup] Reconciling pending scheduled moderation actions...")
        due_actions = await self.db.get_due_scheduled_actions()
        logger.info("[Startup] Found %d overdue scheduled actions", len(due_actions))

        for job in due_actions:
            jid = job["id"]
            gid = job["guild_id"]
            tid = job["target_id"]
            action_type = job["action_type"]

            if action_type == "unban_tempban":
                guild = self.bot.get_guild(gid)
                if not guild:
                    logger.warning("[Reconciliation] Guild %d not found for job #%d; skipping", gid, jid)
                    continue

                try:
                    # Check if user is actually still banned
                    ban_entry = await guild.fetch_ban(discord.Object(id=tid))
                    logger.info("[Reconciliation] Executing overdue unban for user %s (%d) in guild %s", ban_entry.user, tid, guild.name)
                    await guild.unban(ban_entry.user, reason=f"Temporary ban expired [Scheduled Job #{jid}]")
                    await self.db.update_scheduled_action_status(jid, "executed")
                    await self.db.record_audit_log(
                        guild_id=gid, actor_id="System Watchdog", actor_name="Automated Watchdog",
                        target_id=tid, target_name=str(ban_entry.user), action_type="TEMPBAN_EXPIRED",
                        outcome="SUCCESS", details=f"Overdue temporary ban reconciled and unbanned (Job #{jid})"
                    )
                except discord.NotFound:
                    # Already unbanned outside dashboard or previously
                    logger.info("[Reconciliation] User %d already unbanned; marking job #%d superseded", tid, jid)
                    await self.db.update_scheduled_action_status(jid, "superseded")
                except Exception as e:
                    logger.error("[Reconciliation] Error unbanning user %d: %s", tid, e)
                    await self.db.update_scheduled_action_status(jid, "failed")

    async def _scheduled_watchdog_loop(self):
        """Periodic background loop checking due scheduled actions every 30 seconds."""
        while True:
            try:
                await asyncio.sleep(30)
                due_actions = await self.db.get_due_scheduled_actions()
                for job in due_actions:
                    jid = job["id"]
                    gid = job["guild_id"]
                    tid = job["target_id"]
                    action_type = job["action_type"]

                    if action_type == "unban_tempban":
                        guild = self.bot.get_guild(gid)
                        if not guild:
                            continue
                        try:
                            ban_entry = await guild.fetch_ban(discord.Object(id=tid))
                            await guild.unban(ban_entry.user, reason=f"Temporary ban expired [Job #{jid}]")
                            await self.db.update_scheduled_action_status(jid, "executed")
                            await self.db.record_audit_log(
                                guild_id=gid, actor_id="System Watchdog", actor_name="Automated Watchdog",
                                target_id=tid, target_name=str(ban_entry.user), action_type="TEMPBAN_EXPIRED",
                                outcome="SUCCESS", details=f"Temporary ban duration completed; unbanned (Job #{jid})"
                            )
                        except discord.NotFound:
                            await self.db.update_scheduled_action_status(jid, "superseded")
                        except Exception as e:
                            logger.error("Watchdog unban error for user %d: %s", tid, e)
                            await self.db.update_scheduled_action_status(jid, "failed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in scheduled watchdog loop: %s", e)
                await asyncio.sleep(10)

    # =========================================================================
    # Bulk Moderation Engine
    # =========================================================================

    def resolve_bulk_targets(self, guild: discord.Guild, filters: Dict[str, Any]) -> Tuple[List[discord.Member], int]:
        """
        Resolves guild members matching filter criteria.
        Returns (eligible_members, protected_members_count).
        """
        all_members = list(guild.members)
        matched = []
        protected_count = 0

        has_roles = [int(r) for r in filters.get("has_roles", []) if str(r).isdigit()]
        lacks_roles = [int(r) for r in filters.get("lacks_roles", []) if str(r).isdigit()]
        joined_before = filters.get("joined_before")
        joined_after = filters.get("joined_after")
        account_before = filters.get("account_before")
        is_bot = filters.get("is_bot")
        has_timeout = filters.get("has_timeout")
        explicit_exclusions = set(int(u) for u in filters.get("explicit_exclusions", []) if str(u).isdigit())
        explicit_inclusions = set(int(u) for u in filters.get("explicit_inclusions", []) if str(u).isdigit())

        dt_joined_before = datetime.fromisoformat(joined_before).replace(tzinfo=timezone.utc) if joined_before else None
        dt_joined_after = datetime.fromisoformat(joined_after).replace(tzinfo=timezone.utc) if joined_after else None
        dt_account_before = datetime.fromisoformat(account_before).replace(tzinfo=timezone.utc) if account_before else None

        for m in all_members:
            if explicit_inclusions and m.id not in explicit_inclusions:
                continue

            if m.id in explicit_exclusions:
                continue

            # Protected member check (Server Owner, Bot, Admin)
            if self.is_protected_member(guild, m):
                protected_count += 1
                continue

            m_role_ids = {r.id for r in m.roles}

            if has_roles and not all(r in m_role_ids for r in has_roles):
                continue

            if lacks_roles and any(r in m_role_ids for r in lacks_roles):
                continue

            if dt_joined_before and (not m.joined_at or m.joined_at > dt_joined_before):
                continue

            if dt_joined_after and (not m.joined_at or m.joined_at < dt_joined_after):
                continue

            if dt_account_before and m.created_at > dt_account_before:
                continue

            if is_bot is not None and m.bot != bool(is_bot):
                continue

            if has_timeout is not None:
                is_timed_out = m.timed_out_until is not None and m.timed_out_until > discord.utils.utcnow()
                if is_timed_out != bool(has_timeout):
                    continue

            matched.append(m)

        return matched, protected_count

    async def preview_bulk_targets(self, guild_id: int, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Provides preview snapshot of target count and sample targets."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        matched, protected_count = self.resolve_bulk_targets(guild, filters)
        sample = [
            {
                "id": m.id,
                "name": str(m),
                "display_name": m.display_name,
                "avatar": str(m.display_avatar.url),
                "joined_at": m.joined_at.strftime("%Y-%m-%d %H:%M") if m.joined_at else "Unknown",
                "roles": [r.name for r in m.roles if not r.is_default()][:3]
            }
            for m in matched[:30]
        ]

        return {
            "success": True,
            "total_matched": len(matched),
            "protected_excluded": protected_count,
            "sample_targets": sample
        }

    async def start_bulk_job(self, guild_id: int, action_type: str, actor_id: str,
                             actor_name: str, filters: Dict[str, Any], reason: str = "",
                             role_id: Optional[int] = None, duration_seconds: Optional[int] = None,
                             notify_dm: bool = False, confirmation_phrase: str = "") -> Dict[str, Any]:
        """Validates, snapshots targets, and launches bounded rate-limited bulk moderation job."""
        if await self.db.is_emergency_locked(guild_id):
            return {"success": False, "error": "Emergency circuit breaker is active for this server"}

        # Destructive bulk kick/ban requires explicit confirmation phrase
        if action_type in ("kick", "ban"):
            expected_phrase = f"CONFIRM {action_type.upper()}"
            if confirmation_phrase.strip() != expected_phrase:
                return {
                    "success": False,
                    "error": f"High-risk operation requires typing exact confirmation phrase: '{expected_phrase}'"
                }

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "error": "Guild not found"}

        matched, protected_count = self.resolve_bulk_targets(guild, filters)
        if not matched:
            return {"success": False, "error": "No eligible targets matched the filter criteria"}

        MAX_BULK_LIMIT = 250
        if len(matched) > MAX_BULK_LIMIT:
            return {
                "success": False,
                "error": f"Target count ({len(matched)}) exceeds maximum safety limit of {MAX_BULK_LIMIT} members"
            }

        job_id = f"job_{action_type}_{uuid.uuid4().hex[:10]}"
        targets_snapshot = [(m.id, str(m)) for m in matched]

        await self.db.create_bulk_job(
            job_id=job_id, guild_id=guild_id, action_type=action_type, actor_id=actor_id,
            actor_name=actor_name, reason=reason, filter_criteria=filters,
            total_targets=len(targets_snapshot), role_id=role_id, duration_seconds=duration_seconds,
            dm_template_used="Standard" if notify_dm else "None"
        )
        await self.db.populate_bulk_targets(job_id, targets_snapshot)

        # Launch background execution task
        task = asyncio.create_task(
            self._execute_bulk_job_worker(job_id, guild_id, action_type, actor_id, actor_name,
                                          reason, role_id, duration_seconds, notify_dm)
        )
        self._bulk_active_jobs[job_id] = task

        await self.db.record_audit_log(
            guild_id=guild_id, actor_id=actor_id, actor_name=actor_name, action_type=f"BULK_{action_type.upper()}_START",
            outcome="STARTED", bulk_job_id=job_id,
            details=f"Initiated bulk {action_type} for {len(targets_snapshot)} targets (Excluded {protected_count} protected)"
        )

        return {
            "success": True,
            "job_id": job_id,
            "total_targets": len(targets_snapshot),
            "status": "queued"
        }

    async def _execute_bulk_job_worker(self, job_id: str, guild_id: int, action_type: str,
                                       actor_id: str, actor_name: str, reason: str,
                                       role_id: Optional[int], duration_seconds: Optional[int],
                                       notify_dm: bool):
        """Worker task processing bulk job targets sequentially with rate limiting."""
        logger.info("[BulkWorker] Starting bulk job %s (%s)", job_id, action_type)
        await self.db.update_bulk_job_status(job_id, "running")

        guild = self.bot.get_guild(guild_id)
        if not guild:
            await self.db.update_bulk_job_status(job_id, "failed")
            return

        targets = await self.db.get_bulk_job_targets(job_id, limit=500)
        target_role = guild.get_role(role_id) if role_id else None

        for t in targets:
            # Check for emergency lock or manual cancellation request
            if await self.db.is_emergency_locked(guild_id):
                logger.warning("[BulkWorker] Emergency lock detected; halting bulk job %s", job_id)
                await self.db.update_bulk_job_status(job_id, "cancelled")
                break

            current_job = await self.db.get_bulk_job(job_id)
            if current_job and current_job["status"] == "cancel_requested":
                logger.info("[BulkWorker] Cancel requested for job %s; stopping worker", job_id)
                await self.db.update_bulk_job_status(job_id, "cancelled")
                break

            tid = t["target_id"]
            member = guild.get_member(tid)
            if not member and action_type not in ("ban", "unban"):
                await self.db.update_bulk_target_result(job_id, tid, status="skipped", error="Member left server")
                continue

            # Respect token bucket rate limiter
            await asyncio.sleep(self._rate_limit_delay)

            try:
                res = None
                if action_type == "role_add" and target_role:
                    res = await self.execute_role_assign(guild_id, tid, role_id, actor_id, actor_name, reason=reason, notify_dm=notify_dm)
                elif action_type == "role_remove" and target_role:
                    res = await self.execute_role_remove(guild_id, tid, role_id, actor_id, actor_name, reason=reason, notify_dm=notify_dm)
                elif action_type == "timeout":
                    dur = duration_seconds or 300
                    res = await self.execute_timeout(guild_id, tid, dur, actor_id, actor_name, reason=reason, notify_dm=notify_dm)
                elif action_type == "kick":
                    res = await self.execute_kick(guild_id, tid, actor_id, actor_name, reason=reason, notify_dm=notify_dm)
                elif action_type == "ban":
                    res = await self.execute_ban(guild_id, tid, actor_id, actor_name, reason=reason, notify_dm=notify_dm)
                elif action_type == "unban":
                    res = await self.execute_unban(guild_id, tid, actor_id, actor_name, reason=reason)

                if res and res.get("success"):
                    dm_stat = res.get("dm_status", "not_requested")
                    await self.db.update_bulk_target_result(job_id, tid, status="success", dm_status=dm_stat)
                else:
                    err_msg = res.get("error", "Unknown error") if res else "Execution returned None"
                    await self.db.update_bulk_target_result(job_id, tid, status="failed", error=err_msg)
            except Exception as e:
                logger.error("[BulkWorker] Exception on target %d in job %s: %s", tid, job_id, e)
                await self.db.update_bulk_target_result(job_id, tid, status="failed", error=str(e))

        # Finalize job status
        final_job = await self.db.get_bulk_job(job_id)
        if final_job and final_job["status"] not in ("cancelled", "failed"):
            if final_job["failed_targets"] == 0:
                await self.db.update_bulk_job_status(job_id, "completed")
            elif final_job["completed_targets"] > 0:
                await self.db.update_bulk_job_status(job_id, "partially_completed")
            else:
                await self.db.update_bulk_job_status(job_id, "failed")

        self._bulk_active_jobs.pop(job_id, None)
        logger.info("[BulkWorker] Bulk job %s finished", job_id)

    async def cancel_bulk_job(self, job_id: str) -> bool:
        """Requests cancellation of a running bulk job."""
        job = await self.db.get_bulk_job(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        await self.db.update_bulk_job_status(job_id, "cancel_requested")
        return True
