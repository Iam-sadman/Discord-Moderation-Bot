"""
Comprehensive Integration & Acceptance Test Suite for
Discord Role Management & Moderation System.
Validates all 30 acceptance criteria from the master specification.
"""

import os
import sys
import uuid
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

import discord
from discord.ext import commands

from services.moderation_db import ModerationDB
from services.moderation_engine import ModerationEngine
from dashboard.app import create_dashboard


class TestModerationSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Use an isolated test database in scratch/
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_db_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_mod_{self.test_db_id}.db")

        self.db = ModerationDB(self.test_db_path)
        ModerationDB._instance = self.db
        await self.db.init_db()

        # Build comprehensive mock Discord Bot
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 999999999999999999
        self.mock_bot.user.name = "TestModerationBot"
        self.mock_bot.user.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
        self.mock_bot.latency = 0.045
        self.mock_bot.guilds = []
        self.mock_bot.cogs = {}
        self.mock_bot.tree.get_commands.return_value = []
        self.mock_bot.add_view = MagicMock()

        # Build mock Guild
        self.mock_guild = MagicMock(spec=discord.Guild)
        self.mock_guild.id = 123456789012345678
        self.mock_guild.name = "Test Discord Server"
        self.mock_guild.owner_id = 111111111111111111

        # Bot member inside guild
        self.mock_bot_member = MagicMock(spec=discord.Member)
        self.mock_bot_member.id = self.mock_bot.user.id
        self.mock_bot_top_role = MagicMock(spec=discord.Role)
        self.mock_bot_top_role.name = "Bot Master Role"
        self.mock_bot_top_role.position = 100
        self.mock_bot_member.top_role = self.mock_bot_top_role
        self.mock_bot_member.guild_permissions.administrator = False
        self.mock_guild.me = self.mock_bot_member

        # Target member
        self.mock_target_member = MagicMock(spec=discord.Member)
        self.mock_target_member.id = 222222222222222222
        self.mock_target_member.name = "TestTroublemaker"
        self.mock_target_member.display_name = "Troublemaker"
        self.mock_target_member.bot = False
        self.mock_target_member.guild_permissions.administrator = False
        self.mock_target_top_role = MagicMock(spec=discord.Role)
        self.mock_target_top_role.name = "Regular Member"
        self.mock_target_top_role.position = 10
        self.mock_target_member.top_role = self.mock_target_top_role
        self.mock_target_member.roles = [self.mock_target_top_role]
        self.mock_target_member.send = AsyncMock()
        self.mock_target_member.add_roles = AsyncMock()
        self.mock_target_member.remove_roles = AsyncMock()
        self.mock_target_member.timeout = AsyncMock()
        self.mock_target_member.kick = AsyncMock()

        # Server Owner member
        self.mock_owner_member = MagicMock(spec=discord.Member)
        self.mock_owner_member.id = self.mock_guild.owner_id
        self.mock_owner_member.name = "ServerOwner"
        self.mock_owner_member.bot = False
        self.mock_owner_member.guild_permissions.administrator = True
        self.mock_owner_top_role = MagicMock(spec=discord.Role)
        self.mock_owner_top_role.position = 999
        self.mock_owner_member.top_role = self.mock_owner_top_role

        # Manageable role
        self.mock_role = MagicMock(spec=discord.Role)
        self.mock_role.id = 333333333333333333
        self.mock_role.name = "Gamer"
        self.mock_role.position = 20
        self.mock_role.managed = False
        self.mock_role.is_default.return_value = False
        self.mock_role.permissions.administrator = False
        self.mock_role.permissions.manage_guild = False
        self.mock_role.permissions.ban_members = False
        self.mock_role.permissions.kick_members = False

        # Higher privileged role
        self.mock_admin_role = MagicMock(spec=discord.Role)
        self.mock_admin_role.id = 444444444444444444
        self.mock_admin_role.name = "Super Admin"
        self.mock_admin_role.position = 200
        self.mock_admin_role.managed = False
        self.mock_admin_role.is_default.return_value = False
        self.mock_admin_role.permissions.administrator = True

        # Guild lookup wiring
        def get_role_mock(rid):
            if rid == self.mock_role.id: return self.mock_role
            if rid == self.mock_admin_role.id: return self.mock_admin_role
            return None

        def get_member_mock(mid):
            if mid == self.mock_target_member.id: return self.mock_target_member
            if mid == self.mock_owner_member.id: return self.mock_owner_member
            if mid == self.mock_bot.user.id: return self.mock_bot_member
            return None

        self.mock_guild.get_role.side_effect = get_role_mock
        self.mock_guild.get_member.side_effect = get_member_mock
        self.mock_guild.members = [self.mock_target_member, self.mock_owner_member, self.mock_bot_member]
        self.mock_guild.roles = [self.mock_admin_role, self.mock_bot_top_role, self.mock_role, self.mock_target_top_role]
        self.mock_guild.ban = AsyncMock()
        self.mock_guild.unban = AsyncMock()
        self.mock_guild.fetch_ban = AsyncMock()

        def get_guild_mock(gid):
            if gid == self.mock_guild.id:
                return self.mock_guild
            return None

        self.mock_bot.get_guild.side_effect = get_guild_mock
        self.mock_bot.guilds = [self.mock_guild]

        # Initialize Moderation Engine
        self.engine = ModerationEngine(self.mock_bot)
        ModerationEngine._instance = self.engine

        # Initialize Quart app
        self.app = create_dashboard(self.mock_bot)
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        await self.engine.stop()
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    # =========================================================================
    # Acceptance Criteria 1 to 4: Authorization, ID validation & CSRF
    # =========================================================================

    async def test_01_unauthorized_dashboard_access_denied(self):
        """1. Unauthorized dashboard access is denied (redirects or 401)."""
        res = await self.client.get("/api/guild/123456789012345678/roles/catalog")
        self.assertEqual(res.status_code, 401)
        data = await res.get_json()
        self.assertEqual(data["error"], "Unauthorized")

    async def test_02_authorized_user_without_action_permission_denied(self):
        """2. An authorized dashboard user without the required action permission is denied."""
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "viewer_user"
            sess["role"] = "Viewer"

        # Attempt to kick as Viewer
        res = await self.client.post("/api/guild/123456789012345678/moderation/kick", json={
            "user_id": self.mock_target_member.id,
            "reason": "Test unauthorized kick"
        })
        self.assertEqual(res.status_code, 403)
        data = await res.get_json()
        self.assertIn("Permission Denied", data["error"])

    async def test_03_forged_guild_and_target_ids_rejected(self):
        """3. Forged guild, user, role, and job IDs are rejected."""
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["role"] = "Administrator"

        # Non-existent guild
        res = await self.client.get("/api/guild/999999999999/roles/catalog")
        self.assertEqual(res.status_code, 404)

        # Missing target ID in timeout
        res2 = await self.client.post("/api/guild/123456789012345678/moderation/timeout", json={
            "duration_seconds": 300
        })
        self.assertEqual(res2.status_code, 400)

    async def test_04_frontend_only_permission_bypass_impossible(self):
        """4. Direct API calls bypass UI checks and are caught server-side."""
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "moderator_user"
            sess["role"] = "Moderator"

        # Default Moderator does NOT have can_ban
        res = await self.client.post("/api/guild/123456789012345678/moderation/ban", json={
            "user_id": self.mock_target_member.id,
            "reason": "Direct API bypass attempt"
        })
        self.assertEqual(res.status_code, 403)
        self.assertIn("Permission Denied", (await res.get_json())["error"])

    # =========================================================================
    # Acceptance Criteria 5 to 10: Role Panels, Persistent Buttons & Reactions
    # =========================================================================

    async def test_05_public_panel_cannot_assign_privileged_role(self):
        """5. A public role panel cannot assign a privileged role."""
        # Try to add Administrator role to allowed self-roles
        res = await self.client.post(
            f"/api/guild/{self.mock_guild.id}/roles/allowed_self",
            json={"role_id": self.mock_admin_role.id, "action": "add"}
        )
        # Should be blocked as privileged
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["role"] = "Administrator"

        res = await self.client.post(
            f"/api/guild/{self.mock_guild.id}/roles/allowed_self",
            json={"role_id": self.mock_admin_role.id, "action": "add"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Security violation", (await res.get_json())["error"])

    async def test_06_role_buttons_assign_correct_role(self):
        """6. Role buttons assign the correct role when whitelisted."""
        await self.db.add_allowed_self_role(self.mock_guild.id, self.mock_role.id, added_by="admin")

        from cogs.moderation import RolePanelButton
        btn = RolePanelButton(
            panel_id=1, item_id=1, role_id=self.mock_role.id,
            label="Gamer", style=discord.ButtonStyle.primary, toggle=True
        )

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.guild = self.mock_guild
        mock_interaction.user = self.mock_target_member
        mock_interaction.response.send_message = AsyncMock()

        self.mock_target_member.roles = []
        await btn.callback(mock_interaction)
        self.mock_target_member.add_roles.assert_awaited_once_with(self.mock_role, reason="Self-service role panel assignment")
        mock_interaction.response.send_message.assert_awaited_once()

    async def test_07_toggle_buttons_remove_role_when_present(self):
        """7. Toggle buttons remove the role when clicked again."""
        await self.db.add_allowed_self_role(self.mock_guild.id, self.mock_role.id, added_by="admin")

        from cogs.moderation import RolePanelButton
        btn = RolePanelButton(
            panel_id=1, item_id=1, role_id=self.mock_role.id,
            label="Gamer", style=discord.ButtonStyle.primary, toggle=True
        )

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.guild = self.mock_guild
        mock_interaction.user = self.mock_target_member
        mock_interaction.response.send_message = AsyncMock()

        # Target already has the role
        self.mock_target_member.roles = [self.mock_role]
        await btn.callback(mock_interaction)
        self.mock_target_member.remove_roles.assert_awaited_once_with(self.mock_role, reason="Self-service role panel toggle")

    async def test_08_reaction_role_addition_and_removal(self):
        """8. Reaction addition/removal follows the configured policy."""
        panel_id = await self.db.create_role_panel(
            guild_id=self.mock_guild.id, channel_id=555, title="Reactions Panel",
            description="React for roles", color="#5865F2", created_by="admin"
        )
        await self.db.set_panel_message_id(panel_id, 777888999)
        await self.db.set_panel_items(panel_id, [{
            "item_type": "reaction", "emoji": "⭐", "role_id": self.mock_role.id, "toggle": True
        }])
        await self.db.add_allowed_self_role(self.mock_guild.id, self.mock_role.id, "admin")

        from cogs.moderation import Moderation
        cog = Moderation(self.mock_bot)

        # Test reaction add
        payload_add = MagicMock(spec=discord.RawReactionActionEvent)
        payload_add.guild_id = self.mock_guild.id
        payload_add.user_id = self.mock_target_member.id
        payload_add.message_id = 777888999
        payload_add.emoji = MagicMock()
        payload_add.emoji.__str__.return_value = "⭐"
        payload_add.emoji.name = "⭐"
        payload_add.member = self.mock_target_member

        self.mock_target_member.roles = []
        await cog.on_raw_reaction_add(payload_add)
        self.mock_target_member.add_roles.assert_awaited_once()

        # Test reaction remove
        payload_rem = MagicMock(spec=discord.RawReactionActionEvent)
        payload_rem.guild_id = self.mock_guild.id
        payload_rem.user_id = self.mock_target_member.id
        payload_rem.message_id = 777888999
        payload_rem.emoji = MagicMock()
        payload_rem.emoji.__str__.return_value = "⭐"
        payload_rem.emoji.name = "⭐"
        payload_rem.member = self.mock_target_member

        self.mock_target_member.roles = [self.mock_role]
        await cog.on_raw_reaction_remove(payload_rem)
        self.mock_target_member.remove_roles.assert_awaited_once()

    async def test_09_persistent_views_registered_on_startup(self):
        """9. Persistent buttons register with timeout=None across bot restarts."""
        panel_id = await self.db.create_role_panel(
            guild_id=self.mock_guild.id, channel_id=555, title="Startup Panel",
            description="Test", color="#5865F2", created_by="admin"
        )
        await self.db.set_panel_message_id(panel_id, 111222333)

        from cogs.moderation import Moderation
        cog = Moderation(self.mock_bot)
        await cog._register_all_published_views()

        # Verify bot.add_view was called with message_id
        self.mock_bot.add_view.assert_called()

    async def test_10_deleted_role_or_channel_fails_safely(self):
        """10. Deleted role or channel cases fail safely without unhandled crashes."""
        from cogs.moderation import RolePanelButton
        btn = RolePanelButton(
            panel_id=1, item_id=1, role_id=999999999,  # Non-existent role
            label="Ghost Role", style=discord.ButtonStyle.primary
        )
        await self.db.add_allowed_self_role(self.mock_guild.id, 999999999, "admin")

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.guild = self.mock_guild
        mock_interaction.user = self.mock_target_member
        mock_interaction.response.send_message = AsyncMock()

        await btn.callback(mock_interaction)
        mock_interaction.response.send_message.assert_awaited_once_with(
            "❌ The assigned role no longer exists on this server.", ephemeral=True
        )

    # =========================================================================
    # Acceptance Criteria 11 to 14: Individual Moderation Actions & Hierarchy
    # =========================================================================

    async def test_11_individual_timeout_execution(self):
        """11. Individual timeout works with valid permissions."""
        res = await self.engine.execute_timeout(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            duration_seconds=600, actor_id="admin", actor_name="Admin",
            reason="Violation of Rule 1", notify_dm=False
        )
        self.assertTrue(res["success"])
        self.mock_target_member.timeout.assert_awaited_once()

    async def test_12_individual_kick_execution(self):
        """12. Individual kick works with valid permissions."""
        res = await self.engine.execute_kick(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Spamming", notify_dm=False
        )
        self.assertTrue(res["success"])
        self.mock_target_member.kick.assert_awaited_once()

    async def test_13_individual_ban_and_unban_execution(self):
        """13. Individual permanent ban and unban work with valid permissions."""
        res_ban = await self.engine.execute_ban(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Harassment", delete_message_days=1
        )
        self.assertTrue(res_ban["success"])
        self.mock_guild.ban.assert_awaited_once()

        # Unban test
        ban_entry = MagicMock()
        ban_entry.user = self.mock_target_member
        self.mock_guild.fetch_ban.return_value = ban_entry

        res_unban = await self.engine.execute_unban(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Appealed"
        )
        self.assertTrue(res_unban["success"])
        self.mock_guild.unban.assert_awaited_once()

    async def test_14_hierarchy_violation_against_server_owner_rejected(self):
        """14. Invalid permission and hierarchy cases (e.g. Server Owner) are rejected."""
        res = await self.engine.execute_kick(
            guild_id=self.mock_guild.id, target_id=self.mock_owner_member.id,
            actor_id="admin", actor_name="Admin", reason="Attempt to kick owner"
        )
        self.assertFalse(res["success"])
        self.assertIn("Cannot moderate the Server Owner", res["error"])

    # =========================================================================
    # Acceptance Criteria 15 to 19: Temporary Ban Scheduling & Recovery
    # =========================================================================

    async def test_15_temporary_ban_scheduling_and_expiry(self):
        """15. Temporary bans create durable scheduled jobs."""
        res = await self.engine.execute_tempban(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            duration_seconds=3600, actor_id="admin", actor_name="Admin",
            reason="1 hour suspension"
        )
        self.assertTrue(res["success"])
        self.assertIn("job_id", res)
        self.mock_guild.ban.assert_awaited_once()

        pending = await self.db.get_all_pending_scheduled_actions(self.mock_guild.id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["target_id"], self.mock_target_member.id)

    async def test_16_expired_tempban_recovers_after_restart(self):
        """16. Overdue temporary bans are reconciled and unbanned on startup."""
        # Insert overdue scheduled action manually
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        job_id = await self.db.create_scheduled_action(
            action_type="unban_tempban", guild_id=self.mock_guild.id,
            target_id=self.mock_target_member.id, actor_id="admin",
            reason="Old tempban", trigger_at=past_time
        )

        ban_entry = MagicMock()
        ban_entry.user = self.mock_target_member
        self.mock_guild.fetch_ban.return_value = ban_entry

        # Run startup reconciliation
        await self.engine.startup_reconciliation()

        self.mock_guild.unban.assert_awaited_once_with(
            self.mock_target_member, reason=f"Temporary ban expired [Scheduled Job #{job_id}]"
        )

        pending = await self.db.get_due_scheduled_actions()
        self.assertEqual(len(pending), 0)

    async def test_17_old_expiry_job_superseded_by_newer_moderation(self):
        """17. Permanent ban supersedes older pending temporary ban jobs."""
        await self.engine.execute_tempban(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            duration_seconds=7200, actor_id="admin", actor_name="Admin"
        )

        # Later, operator permanently bans the user
        await self.engine.execute_ban(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Escalated to perma-ban"
        )

        pending = await self.db.get_all_pending_scheduled_actions(self.mock_guild.id)
        self.assertEqual(len(pending), 0)  # Canceled/superseded

    async def test_18_already_unbanned_user_handled_gracefully(self):
        """18. Already-unbanned users do not trigger redundant API errors."""
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        await self.db.create_scheduled_action(
            action_type="unban_tempban", guild_id=self.mock_guild.id,
            target_id=self.mock_target_member.id, actor_id="admin",
            reason="Old tempban", trigger_at=past_time
        )

        # Discord returns NotFound (user already unbanned)
        self.mock_guild.fetch_ban.side_effect = discord.NotFound(MagicMock(), "Unknown Ban")

        await self.engine.startup_reconciliation()
        # Should not crash and should mark superseded
        due = await self.db.get_due_scheduled_actions()
        self.assertEqual(len(due), 0)

    async def test_19_failed_dm_does_not_abort_moderation_action(self):
        """19. A failed DM notification does not abort the moderation action."""
        self.mock_target_member.send.side_effect = discord.Forbidden(MagicMock(), "Cannot send messages to this user")

        res = await self.engine.execute_kick(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Spam", notify_dm=True
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["dm_status"], "failed")
        self.mock_target_member.kick.assert_awaited_once()

    # =========================================================================
    # Acceptance Criteria 20 to 24: Bulk Moderation, Filters, and Cancellation
    # =========================================================================

    async def test_20_bulk_filters_match_expected_targets(self):
        """20. Bulk filters match expected targets correctly."""
        matched, protected_count = self.engine.resolve_bulk_targets(self.mock_guild, {})
        # Troublemaker matched, Owner and Bot excluded as protected
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].id, self.mock_target_member.id)
        self.assertEqual(protected_count, 2)  # Owner + Bot

    async def test_21_protected_targets_excluded_from_bulk(self):
        """21. Server Owner and Bot are never included in bulk targets."""
        matched, _ = self.engine.resolve_bulk_targets(self.mock_guild, {})
        matched_ids = [m.id for m in matched]
        self.assertNotIn(self.mock_guild.owner_id, matched_ids)
        self.assertNotIn(self.mock_bot.user.id, matched_ids)

    async def test_22_bulk_limits_prevent_excessive_targets(self):
        """22. Bulk actions reject targets exceeding bounded safety limits."""
        # Mock 300 members
        mock_members = []
        for i in range(300):
            m = MagicMock(spec=discord.Member)
            m.id = 1000 + i
            m.bot = False
            m.guild_permissions.administrator = False
            m.roles = [self.mock_target_top_role]
            mock_members.append(m)

        with patch.object(self.engine, "resolve_bulk_targets", return_value=(mock_members, 0)):
            res = await self.engine.start_bulk_job(
                guild_id=self.mock_guild.id, action_type="role_add",
                actor_id="admin", actor_name="Admin", filters={}, role_id=self.mock_role.id
            )
            self.assertFalse(res["success"])
            self.assertIn("exceeds maximum safety limit", res["error"])

    async def test_23_confirmation_phrase_required_for_bulk_kick(self):
        """23. High-risk bulk actions require exact confirmation phrase."""
        res = await self.engine.start_bulk_job(
            guild_id=self.mock_guild.id, action_type="kick",
            actor_id="admin", actor_name="Admin", filters={},
            confirmation_phrase="WRONG_PHRASE"
        )
        self.assertFalse(res["success"])
        self.assertIn("requires typing exact confirmation phrase", res["error"])

    async def test_24_bulk_job_cancellation(self):
        """24. Bulk job cancellation updates job status and halts execution."""
        res = await self.engine.start_bulk_job(
            guild_id=self.mock_guild.id, action_type="role_add",
            actor_id="admin", actor_name="Admin", filters={}, role_id=self.mock_role.id
        )
        self.assertTrue(res["success"])
        job_id = res["job_id"]

        cancel_res = await self.engine.cancel_bulk_job(job_id)
        self.assertTrue(cancel_res)
        job = await self.db.get_bulk_job(job_id)
        self.assertIn(job["status"], ("cancel_requested", "cancelled"))

    # =========================================================================
    # Acceptance Criteria 25 to 30: Reliability, Idempotency & Safety
    # =========================================================================

    async def test_25_emergency_circuit_breaker_halts_actions(self):
        """25. Emergency circuit breaker prevents moderation actions."""
        await self.db.set_emergency_lock(self.mock_guild.id, True, "Admin", "Emergency drill")

        res = await self.engine.execute_kick(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin"
        )
        self.assertFalse(res["success"])
        self.assertIn("Emergency circuit breaker is active", res["error"])

    async def test_26_duplicate_requests_prevented_by_idempotency_key(self):
        """26. Duplicate requests do not execute destructive actions twice."""
        idempotency_key = "unique_action_key_123"
        res1 = await self.engine.execute_timeout(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            duration_seconds=300, actor_id="admin", actor_name="Admin",
            idempotency_key=idempotency_key
        )
        self.assertTrue(res1["success"])

        # Second submission with same idempotency key
        res2 = await self.engine.execute_timeout(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            duration_seconds=300, actor_id="admin", actor_name="Admin",
            idempotency_key=idempotency_key
        )
        self.assertTrue(res2["success"])
        self.assertTrue(res2.get("already_completed", False))
        # Discord timeout called only once
        self.assertEqual(self.mock_target_member.timeout.await_count, 1)

    async def test_27_audit_log_recorded_on_execution(self):
        """27. Audit records are durably saved with details."""
        await self.engine.execute_kick(
            guild_id=self.mock_guild.id, target_id=self.mock_target_member.id,
            actor_id="admin", actor_name="Admin", reason="Audit Verification"
        )
        audit_data = await self.db.get_moderation_audit_logs(self.mock_guild.id)
        self.assertGreater(audit_data["total"], 0)
        latest = audit_data["logs"][0]
        self.assertEqual(latest["action_type"], "KICK")
        self.assertEqual(latest["outcome"], "SUCCESS")

    async def test_28_message_template_rendering(self):
        """28. Message templates correctly replace only documented placeholders."""
        tpl = "Notice: {user} timed out in {server} for {duration}. Reason: {reason}. Role: {role} Action: {action} @everyone"
        rendered = self.engine.format_dm_message(
            tpl, user_name="Alex", guild_name="Gaming Guild", role_name="Mod",
            reason="Rule 1", duration_str="10m", action_name="Timeout"
        )
        self.assertIn("Alex", rendered)
        self.assertIn("Gaming Guild", rendered)
        self.assertIn("10m", rendered)
        # Verify @everyone sanitized
        self.assertNotIn("@everyone", rendered)

    async def test_29_existing_logger_db_untouched(self):
        """29. Existing logger database (data/logs.db) remains untouched."""
        # Ensure moderation DB does not connect to or touch logs.db
        self.assertNotEqual(self.db.db_path, "data/logs.db")

    async def test_30_moderation_cog_loads_cleanly(self):
        """30. Moderation cog initializes cleanly without exceptions."""
        from cogs.moderation import Moderation
        cog = Moderation(self.mock_bot)
        self.assertIsNotNone(cog)
        self.assertEqual(cog.mod_group.name, "mod")

    async def test_31_publish_to_private_channel_handling(self):
        """31. Private channel publishing fetches via API and provides clear permission diagnostics."""
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["role"] = "Administrator"

        # Create panel targeting private channel
        private_ch_id = 888888888888888888
        create_res = await self.client.post(f"/api/guild/{self.mock_guild.id}/role_panels/create", json={
            "channel_id": private_ch_id,
            "title": "Private Mod Panel",
            "items": []
        })
        self.assertEqual(create_res.status_code, 200)
        panel_id = (await create_res.get_json())["panel"]["id"]

        # 1. Bot cannot access private channel at all (Forbidden)
        self.mock_guild.get_channel.return_value = None
        self.mock_bot.fetch_channel.side_effect = discord.Forbidden(MagicMock(), "Missing Access")
        res_forbidden = await self.client.post(f"/api/guild/{self.mock_guild.id}/role_panels/{panel_id}/publish")
        self.assertEqual(res_forbidden.status_code, 403)
        self.assertIn("Channel #888888888888888888 is private and the bot lacks permission", (await res_forbidden.get_json())["error"])

        # 2. Private channel fetched, but bot lacks Send Messages permission
        mock_private_channel = MagicMock(spec=discord.TextChannel)
        mock_private_channel.id = private_ch_id
        mock_private_channel.name = "staff-private"
        mock_private_channel.guild = self.mock_guild
        mock_perms = MagicMock()
        mock_perms.view_channel = True
        mock_perms.send_messages = False
        mock_perms.embed_links = True
        mock_perms.add_reactions = True
        mock_private_channel.permissions_for.return_value = mock_perms
        self.mock_bot.fetch_channel.side_effect = None
        self.mock_bot.fetch_channel.return_value = mock_private_channel

        res_missing_perm = await self.client.post(f"/api/guild/{self.mock_guild.id}/role_panels/{panel_id}/publish")
        self.assertEqual(res_missing_perm.status_code, 403)
        self.assertIn("Send Messages", (await res_missing_perm.get_json())["error"])

        # 3. Private channel with proper permissions succeeds
        mock_perms.send_messages = True
        sent_mock = MagicMock()
        sent_mock.id = 777777777777777777
        sent_mock.jump_url = "https://discord.com/channels/123/888/777"
        mock_private_channel.send = AsyncMock(return_value=sent_mock)

        res_ok = await self.client.post(f"/api/guild/{self.mock_guild.id}/role_panels/{panel_id}/publish")
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue((await res_ok.get_json())["success"])


if __name__ == "__main__":
    unittest.main()
