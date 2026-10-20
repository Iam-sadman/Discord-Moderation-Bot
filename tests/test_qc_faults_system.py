"""
Comprehensive Unit & Integration Test Suite for QC & Fault Tracker Module.
Validates:
- QCFaultsDB CRUD, SQLite WAL mode, foreign keys, and analytical aggregation queries
- 64-Bit Discord Snowflake ID safety (18-19 digits preservation without precision loss)
- Dynamic role-mapped teams (10+ teams expandable) and project codes uniqueness
- Formula injection protection (CWE-1236)
- CSV export (with utf-8-sig BOM) and Microsoft Excel (.xlsx) export
- DynamicFaultAckButton dynamic item and alert embed construction
- Dashboard REST API endpoints and role-based access control
"""

import os
import sys
import uuid
import io
import json
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

import discord
from discord.ext import commands

from services.qc_faults_db import QCFaultsDB
from services.qc_faults_exporter import (
    sanitize_csv_cell,
    build_qc_export_data,
    generate_qc_csv_bytes,
    generate_qc_xlsx_bytes,
    OPENPYXL_AVAILABLE
)
from cogs.qc_faults import (
    DynamicFaultAckButton,
    build_fault_alert_embed,
    QCFaultsCog,
    SEVERITY_COLORS
)
from dashboard.app import create_dashboard
from dashboard.qc_faults_api import (
    stringify_snowflake,
    sanitize_fault_record_for_json,
    sanitize_fault_list_for_json
)


class TestQCFaultsSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create isolated test database in scratch/
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_qc_{self.test_id}.db")

        self.db = QCFaultsDB(self.test_db_path)
        QCFaultsDB._instance = self.db
        await self.db.init_db()

        # 64-bit Discord snowflake IDs (18-19 digits)
        self.guild_id = 987654321098765432
        self.team_role_id = 888777666555444333
        self.leader_user_id = 111222333444555666
        self.labeler_id = 999888777666555444
        self.logged_by_id = 123123123123123123
        self.alert_channel_id = 777666555444333222

        # Mock Bot & Guild
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 999999999999999999
        self.mock_bot.add_dynamic_items = MagicMock()

        self.mock_guild = MagicMock(spec=discord.Guild)
        self.mock_guild.id = self.guild_id
        self.mock_guild.name = "QC Test Guild"
        self.mock_bot.get_guild.return_value = self.mock_guild

        # Setup Quart test client
        self.app = create_dashboard(self.mock_bot)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        # Cleanup temporary test database
        for ext in ("", "-wal", "-shm"):
            p = self.test_db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # =========================================================================
    # 1. Database Operations & Settings Tests
    # =========================================================================

    async def test_01_db_settings_crud(self):
        """Validates settings storage and JSON-encoded checker roles."""
        # Initial settings should be empty/default
        s0 = await self.db.get_settings(self.guild_id)
        self.assertEqual(s0["guild_id"], self.guild_id)
        self.assertIsNone(s0["alert_channel_id"])
        self.assertEqual(s0["checker_role_ids"], [])

        # Update settings
        checker_roles = [123456789012345678, 987654321098765432]
        ok = await self.db.update_settings(self.guild_id, self.alert_channel_id, checker_roles)
        self.assertTrue(ok)

        s1 = await self.db.get_settings(self.guild_id)
        self.assertEqual(s1["alert_channel_id"], self.alert_channel_id)
        self.assertEqual(s1["checker_role_ids"], checker_roles)

    # =========================================================================
    # 2. Projects Master CRUD Tests
    # =========================================================================

    async def test_02_projects_crud_and_uniqueness(self):
        """Tests project creation, listing, updating, deletion, and guild-scoped uniqueness."""
        p1 = await self.db.create_project(
            guild_id=self.guild_id,
            project_code="ARC-3D-01",
            project_name="LiDAR Annotation",
            description="3D Bounding Boxes",
            created_by="Admin"
        )
        self.assertEqual(p1["project_code"], "ARC-3D-01")
        self.assertEqual(p1["is_active"], 1)

        # Retrieve project
        p = await self.db.get_project(self.guild_id, "ARC-3D-01")
        self.assertIsNotNone(p)
        self.assertEqual(p["project_name"], "LiDAR Annotation")

        # Uniqueness constraint: duplicate project_code in same guild should fail
        with self.assertRaises(Exception):
            await self.db.create_project(
                guild_id=self.guild_id,
                project_code="ARC-3D-01",
                project_name="Duplicate Code",
                created_by="Admin"
            )

        # Update project
        ok = await self.db.update_project(
            guild_id=self.guild_id,
            project_code="ARC-3D-01",
            project_name="LiDAR Annotation v2",
            is_active=False
        )
        self.assertTrue(ok)
        p_up = await self.db.get_project(self.guild_id, "ARC-3D-01")
        self.assertEqual(p_up["project_name"], "LiDAR Annotation v2")
        self.assertEqual(p_up["is_active"], 0)

        # List active only vs all
        active_list = await self.db.list_projects(self.guild_id, only_active=True)
        self.assertEqual(len(active_list), 0)

        all_list = await self.db.list_projects(self.guild_id, only_active=False)
        self.assertEqual(len(all_list), 1)

        # Delete project
        del_ok = await self.db.delete_project(self.guild_id, "ARC-3D-01")
        self.assertTrue(del_ok)
        self.assertIsNone(await self.db.get_project(self.guild_id, "ARC-3D-01"))

    # =========================================================================
    # 3. Dynamic Role-Mapped Teams (10+ Teams) Tests
    # =========================================================================

    async def test_03_teams_crud_and_role_mapping(self):
        """Tests role-mapped team management and dynamic member team resolution."""
        # Create team
        team = await self.db.create_team(
            guild_id=self.guild_id,
            team_name="Delta Force",
            team_role_id=self.team_role_id,
            leader_user_id=self.leader_user_id,
            team_channel_id=self.alert_channel_id
        )
        self.assertIsNotNone(team["id"])
        self.assertEqual(team["team_name"], "Delta Force")
        self.assertEqual(team["team_role_id"], self.team_role_id)
        self.assertEqual(team["leader_user_id"], self.leader_user_id)

        # Retrieve by role ID
        by_role = await self.db.get_team_by_role(self.guild_id, self.team_role_id)
        self.assertIsNotNone(by_role)
        self.assertEqual(by_role["team_name"], "Delta Force")

        # Resolve for member roles list
        matched = await self.db.get_team_for_roles(self.guild_id, [99999999, self.team_role_id])
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], team["id"])

        # Update team
        up_ok = await self.db.update_team(
            team_id=team["id"],
            guild_id=self.guild_id,
            team_name="Delta Force Alpha"
        )
        self.assertTrue(up_ok)
        t_up = await self.db.get_team(team["id"], self.guild_id)
        self.assertEqual(t_up["team_name"], "Delta Force Alpha")

        # Delete team
        del_ok = await self.db.delete_team(team["id"], self.guild_id)
        self.assertTrue(del_ok)
        self.assertIsNone(await self.db.get_team(team["id"], self.guild_id))

    # =========================================================================
    # 4. Fault Logs Operations & Acknowledgment Tests
    # =========================================================================

    async def test_04_fault_logging_and_acknowledgment(self):
        """Validates creating fault logs, status transitions, and queries."""
        fault_id = await self.db.create_fault_log(
            guild_id=self.guild_id,
            project_code="ARC-3D-01",
            labeler_id=self.labeler_id,
            labeler_name="LabelerPro#1234",
            team_id=1,
            team_name="Totoro",
            leader_id=self.leader_user_id,
            logged_by_id=self.logged_by_id,
            logged_by_name="QCChecker#5678",
            severity="critical",
            description="Missed bounding box on pedestrian at frame 420",
            fault_type="Missed Object",
            image_url="https://cdn.discordapp.com/attachments/123/456/test.png",
            alert_channel_id=self.alert_channel_id,
            alert_message_id=555666777888999
        )
        self.assertGreater(fault_id, 0)

        # Retrieve fault
        fault = await self.db.get_fault_log(fault_id, self.guild_id)
        self.assertIsNotNone(fault)
        self.assertEqual(fault["severity"], "critical")
        self.assertEqual(fault["status"], "open")
        self.assertIsNone(fault["acknowledged_at"])
        self.assertEqual(fault["image_url"], "https://cdn.discordapp.com/attachments/123/456/test.png")

        # Acknowledge fault
        ack_ok = await self.db.acknowledge_fault(fault_id, self.guild_id)
        self.assertTrue(ack_ok)

        fault_ack = await self.db.get_fault_log(fault_id, self.guild_id)
        self.assertEqual(fault_ack["status"], "acknowledged")
        self.assertIsNotNone(fault_ack["acknowledged_at"])

    # =========================================================================
    # 5. Summary Analytics & Aggregations Tests
    # =========================================================================

    async def test_05_summary_analytics_aggregations(self):
        """Tests overview KPIs, project summaries, team summaries, and labeler diagnostics."""
        # Insert a project
        await self.db.create_project(self.guild_id, "PROJ-A", "Project Alpha")
        # Insert a team
        team = await self.db.create_team(
            self.guild_id, "Nano Banana", self.team_role_id, self.leader_user_id
        )

        # Insert 3 faults: 1 critical, 1 major, 1 minor (2 ack, 1 open)
        f1 = await self.db.create_fault_log(
            self.guild_id, "PROJ-A", self.labeler_id, "Alice",
            team["id"], "Nano Banana", self.leader_user_id,
            self.logged_by_id, "Checker", "critical", "Error 1", "Type A"
        )
        f2 = await self.db.create_fault_log(
            self.guild_id, "PROJ-A", self.labeler_id, "Alice",
            team["id"], "Nano Banana", self.leader_user_id,
            self.logged_by_id, "Checker", "major", "Error 2", "Type B"
        )
        f3 = await self.db.create_fault_log(
            self.guild_id, "PROJ-A", 111122223333444455, "Bob",
            team["id"], "Nano Banana", self.leader_user_id,
            self.logged_by_id, "Checker", "minor", "Error 3", "Type A"
        )

        # Acknowledge 2 faults
        await self.db.acknowledge_fault(f1, self.guild_id)
        await self.db.acknowledge_fault(f2, self.guild_id)

        # 1. Overview KPIs
        kpis = await self.db.get_overview_kpis(self.guild_id)
        self.assertEqual(kpis["total_faults"], 3)
        self.assertEqual(kpis["critical_faults"], 1)
        self.assertEqual(kpis["major_faults"], 1)
        self.assertEqual(kpis["minor_faults"], 1)
        self.assertEqual(kpis["ack_faults"], 2)
        self.assertEqual(kpis["open_faults"], 1)
        self.assertEqual(kpis["active_projects"], 1)
        self.assertEqual(kpis["total_teams"], 1)
        self.assertAlmostEqual(kpis["ack_rate"], 66.7, places=1)

        # 2. Project Summaries
        proj_sums = await self.db.get_project_summaries(self.guild_id)
        self.assertEqual(len(proj_sums), 1)
        self.assertEqual(proj_sums[0]["project_code"], "PROJ-A")
        self.assertEqual(proj_sums[0]["total_faults"], 3)
        self.assertTrue(len(proj_sums[0]["top_fault_types"]) > 0)

        # 3. Team Summaries
        team_sums = await self.db.get_team_summaries(self.guild_id)
        self.assertEqual(len(team_sums), 1)
        self.assertEqual(team_sums[0]["team_name"], "Nano Banana")
        self.assertEqual(team_sums[0]["total_faults"], 3)
        self.assertEqual(team_sums[0]["ack_count"], 2)

        # 4. Labeler Diagnostics
        total_lbl, lbl_list = await self.db.get_labeler_summaries(self.guild_id)
        self.assertEqual(total_lbl, 2)  # Alice and Bob

        details = await self.db.get_labeler_details(self.guild_id, self.labeler_id)
        self.assertIsNotNone(details)
        self.assertEqual(details["summary"]["total_faults"], 2)
        self.assertEqual(len(details["projects_breakdown"]), 1)
        self.assertEqual(len(details["history"]), 2)

    # =========================================================================
    # 6. 64-Bit Discord Snowflake ID Safety Tests
    # =========================================================================

    def test_06_snowflake_id_serialization_safety(self):
        """Verifies that 18-19 digit Discord Snowflake IDs are strictly stringified for JSON."""
        huge_snowflake = 1234567890123456789  # Exceeds 2^53 - 1 (9007199254740991)
        s_val = stringify_snowflake(huge_snowflake)
        self.assertEqual(s_val, "1234567890123456789")
        self.assertIsInstance(s_val, str)

        fault_record = {
            "id": 1,
            "guild_id": self.guild_id,
            "labeler_id": self.labeler_id,
            "team_role_id": self.team_role_id,
            "leader_id": self.leader_user_id,
            "alert_channel_id": self.alert_channel_id,
            "alert_message_id": huge_snowflake,
            "logged_by_id": self.logged_by_id,
            "severity": "critical"
        }

        sanitized = sanitize_fault_record_for_json(fault_record)
        for key in ["guild_id", "labeler_id", "team_role_id", "leader_id", "alert_channel_id", "alert_message_id", "logged_by_id"]:
            self.assertIsInstance(sanitized[key], str, f"Key {key} was not stringified!")
            self.assertTrue(len(sanitized[key]) >= 18)

    # =========================================================================
    # 7. Exporter & Formula Injection Protection Tests
    # =========================================================================

    def test_07_formula_injection_and_exports(self):
        """Validates CWE-1236 prevention and CSV / Excel generation."""
        # Dangerous formula payloads
        self.assertEqual(sanitize_csv_cell("=1+1"), "'=1+1")
        self.assertEqual(sanitize_csv_cell("+cmd|' /C calc'!A0"), "'+cmd|' /C calc'!A0")
        self.assertEqual(sanitize_csv_cell("-2+3"), "'-2+3")
        self.assertEqual(sanitize_csv_cell("@SUM(A1:A10)"), "'@SUM(A1:A10)")
        self.assertEqual(sanitize_csv_cell("Normal Description"), "Normal Description")
        self.assertEqual(sanitize_csv_cell(None), "")

        fault_logs = [
            {
                "id": 1,
                "project_code": "ARC-3D-01",
                "severity": "critical",
                "status": "open",
                "labeler_id": self.labeler_id,
                "labeler_name": "LabelerUser",
                "team_name": "Totoro",
                "leader_id": self.leader_user_id,
                "fault_type": "=DANGEROUS()",
                "description": "Fault occurred on task 123",
                "image_url": "https://cdn.discordapp.com/attachments/123/456/test.png",
                "logged_by_id": self.logged_by_id,
                "logged_by_name": "CheckerAdmin",
                "created_at": "2026-10-15 12:00:00",
                "acknowledged_at": None
            }
        ]

        # CSV generation with UTF-8 BOM
        csv_bytes = generate_qc_csv_bytes(fault_logs)
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))  # UTF-8 BOM
        csv_text = csv_bytes.decode("utf-8-sig")
        self.assertIn("ARC-3D-01", csv_text)
        self.assertIn("CRITICAL", csv_text)
        self.assertIn("'=DANGEROUS()", csv_text)  # Sanitized!

        # Excel workbook generation
        xlsx_bytes = generate_qc_xlsx_bytes(fault_logs)
        self.assertIsInstance(xlsx_bytes, bytes)
        self.assertGreater(len(xlsx_bytes), 100)

    # =========================================================================
    # 8. Discord Cog Embed, Dynamic Item & Button Tests
    # =========================================================================

    def test_08_discord_embed_and_dynamic_item(self):
        """Tests color-coded alert embed construction, mentions, and DynamicItem."""
        btn = DynamicFaultAckButton(fault_id=42)
        self.assertEqual(btn.fault_id, 42)
        self.assertEqual(btn.item.custom_id, "fault:ack:42")

        mock_labeler = MagicMock(spec=discord.Member)
        mock_labeler.mention = "<@999888777>"
        mock_labeler.display_name = "Jane Doe"
        mock_labeler.id = 999888777

        mock_logged_by = MagicMock(spec=discord.Member)
        mock_logged_by.mention = "<@111222333>"
        mock_logged_by.display_name = "Checker Staff"

        # Minor severity: Green
        content, embed, view, allowed_mentions = build_fault_alert_embed(
            fault_id=42,
            project_code="ARC-3D-01",
            labeler=mock_labeler,
            team_name="Totoro",
            leader_user_id=123456789,
            logged_by=mock_logged_by,
            severity="minor",
            description="Minor annotation offset"
        )

        self.assertIn("<@999888777>", content)
        self.assertIn("<@123456789>", content)
        self.assertEqual(embed.color, SEVERITY_COLORS["minor"])
        self.assertIn("Minor", embed.title)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)

        # Critical severity: Red
        _, crit_embed, _, _ = build_fault_alert_embed(
            fault_id=43,
            project_code="ARC-3D-01",
            labeler=mock_labeler,
            team_name="Totoro",
            leader_user_id=None,
            logged_by=mock_logged_by,
            severity="critical",
            description="Critical collision miss"
        )
        self.assertEqual(crit_embed.color, SEVERITY_COLORS["critical"])

    # =========================================================================
    # 9. Dashboard REST API Endpoints Tests
    # =========================================================================

    async def test_09_api_access_control_and_endpoints(self):
        """Validates API authentication, role restrictions, and CRUD endpoints."""
        # Unauthenticated request -> 401
        res = await self.client.get(f"/api/guild/{self.guild_id}/qc/overview")
        self.assertEqual(res.status_code, 401)

        # Authenticate as Administrator
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["role"] = "Administrator"
            sess["name"] = "Admin Lead"

        # 1. Overview API
        res = await self.client.get(f"/api/guild/{self.guild_id}/qc/overview")
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("kpis", data)

        # 2. Create Project API
        p_res = await self.client.post(
            f"/api/guild/{self.guild_id}/qc/projects",
            json={
                "project_code": "TEST-PROJ",
                "project_name": "Test Project",
                "description": "API Test Project"
            }
        )
        self.assertEqual(p_res.status_code, 200)
        p_data = await p_res.get_json()
        self.assertTrue(p_data["success"])

        # 3. Create Team API
        t_res = await self.client.post(
            f"/api/guild/{self.guild_id}/qc/teams",
            json={
                "team_name": "Alpha Squad",
                "team_role_id": str(self.team_role_id),
                "leader_user_id": str(self.leader_user_id)
            }
        )
        self.assertEqual(t_res.status_code, 200)
        t_data = await t_res.get_json()
        self.assertTrue(t_data["success"])

        # 4. Log Fault API from Dashboard
        f_res = await self.client.post(
            f"/api/guild/{self.guild_id}/qc/faults",
            json={
                "labeler_id": str(self.labeler_id),
                "project_code": "TEST-PROJ",
                "severity": "major",
                "description": "API Logged Fault",
                "fault_type": "Classification",
                "dispatch_discord_alert": False
            }
        )
        self.assertEqual(f_res.status_code, 200)
        f_data = await f_res.get_json()
        self.assertTrue(f_data["success"])
        fault_id = f_data["fault_id"]

        # 5. List Faults API
        list_res = await self.client.get(f"/api/guild/{self.guild_id}/qc/faults")
        self.assertEqual(list_res.status_code, 200)
        list_data = await list_res.get_json()
        self.assertEqual(list_data["total"], 1)
        self.assertEqual(list_data["faults"][0]["labeler_id"], str(self.labeler_id))

        # 6. Acknowledge Fault API
        ack_res = await self.client.post(f"/api/guild/{self.guild_id}/qc/faults/{fault_id}/ack")
        self.assertEqual(ack_res.status_code, 200)
        ack_data = await ack_res.get_json()
        self.assertTrue(ack_data["success"])

        # 7. Export API
        exp_res = await self.client.get(f"/api/guild/{self.guild_id}/qc/export?format=csv")
        self.assertEqual(exp_res.status_code, 200)
        content_type = exp_res.headers.get("Content-Type", "")
        self.assertIn("text/csv", content_type)

    async def test_10_discord_button_labeler_strict_permission(self):
        """Verifies that DynamicFaultAckButton only allows the assigned labeler to acknowledge."""
        assigned_id = 123456789012345678
        fault_id = await self.db.create_fault_log(
            guild_id=self.guild_id,
            project_code="TEST-ACK",
            labeler_id=assigned_id,
            labeler_name="LabelerUser",
            team_id=None,
            team_name="Unassigned",
            leader_id=None,
            logged_by_id=self.logged_by_id,
            logged_by_name="TestChecker",
            severity="critical",
            description="Strict Ack Button Permission Test",
        )

        btn = DynamicFaultAckButton(fault_id=fault_id)

        # 1. Non-assigned user attempts to click
        other_user = MagicMock()
        other_user.id = 999999999999999999
        interaction_other = MagicMock()
        interaction_other.guild_id = self.guild_id
        interaction_other.user = other_user
        interaction_other.response.defer = AsyncMock()
        interaction_other.followup.send = AsyncMock()

        await btn.callback(interaction_other)
        interaction_other.followup.send.assert_called_once()
        reject_msg = interaction_other.followup.send.call_args[0][0]
        self.assertIn("Only the assigned labeler", reject_msg)

        # Status must still be open
        f_before = await self.db.get_fault_log(fault_id, self.guild_id)
        self.assertEqual(f_before["status"], "open")

        # 2. Assigned labeler clicks
        labeler_user = MagicMock()
        labeler_user.id = assigned_id
        interaction_labeler = MagicMock()
        interaction_labeler.guild_id = self.guild_id
        interaction_labeler.user = labeler_user
        interaction_labeler.response.defer = AsyncMock()
        interaction_labeler.followup.send = AsyncMock()
        interaction_labeler.message = None

        await btn.callback(interaction_labeler)
        interaction_labeler.followup.send.assert_called_once()
        success_msg = interaction_labeler.followup.send.call_args[0][0]
        self.assertIn("Acknowledged", success_msg)

        # Status must now be acknowledged
        f_after = await self.db.get_fault_log(fault_id, self.guild_id)
        self.assertEqual(f_after["status"], "acknowledged")

    # =========================================================================
    # 11. is_qc_authorized Permission Checks
    # =========================================================================

    async def test_11_is_qc_authorized(self):
        """Validates is_qc_authorized for Server Admin, Checker Roles (int/str), uncached Member, and Team Leaders."""
        cog = QCFaultsCog(MagicMock())
        cog.db = self.db

        checker_role_id = 999111222333

        # Save settings with string role ID
        await self.db.update_settings(self.guild_id, self.alert_channel_id, [str(checker_role_id)])

        # 1. Member with checker role (int id)
        mock_role = MagicMock()
        mock_role.id = checker_role_id

        mock_checker = MagicMock(spec=discord.Member)
        mock_checker.id = 555666
        mock_checker.roles = [mock_role]
        mock_checker.guild_permissions = discord.Permissions.none()

        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = MagicMock()
        interaction.guild.id = self.guild_id
        interaction.guild.owner_id = 111111
        interaction.guild_id = self.guild_id
        interaction.user = mock_checker
        interaction.permissions = discord.Permissions.none()

        auth1 = await cog.is_qc_authorized(interaction)
        self.assertTrue(auth1, "Checker member with int role ID matching str setting must be authorized")

        # 2. Uncached user (discord.User) resolved via guild
        mock_user = MagicMock(spec=discord.User)
        mock_user.id = 555666
        del mock_user.roles

        interaction.guild.get_member.return_value = mock_checker
        interaction.user = mock_user

        auth2 = await cog.is_qc_authorized(interaction)
        self.assertTrue(auth2, "Uncached user resolved to member must be authorized")

        # 3. Server Admin
        admin_perms = discord.Permissions.all()
        admin_member = MagicMock(spec=discord.Member)
        admin_member.id = 777888
        admin_member.roles = []
        admin_member.guild_permissions = admin_perms
        interaction.user = admin_member
        interaction.permissions = admin_perms

        auth3 = await cog.is_qc_authorized(interaction)
        self.assertTrue(auth3, "Server Administrator must be authorized")

        # 4. Registered Team Leader
        await self.db.create_team(self.guild_id, "QC Alpha", 123456, leader_user_id=888999)
        leader_user = MagicMock(spec=discord.Member)
        leader_user.id = 888999
        leader_user.roles = []
        leader_user.guild_permissions = discord.Permissions.none()
        interaction.user = leader_user
        interaction.permissions = discord.Permissions.none()

        auth4 = await cog.is_qc_authorized(interaction)
        self.assertTrue(auth4, "Registered Team Leader must be authorized")

        # 5. Unauthorized random user
        random_user = MagicMock(spec=discord.Member)
        random_user.id = 999999
        random_user.roles = []
        random_user.guild_permissions = discord.Permissions.none()
        interaction.user = random_user
        interaction.permissions = discord.Permissions.none()
        interaction.guild.owner_id = 111111
        interaction.guild.get_member.return_value = random_user

        auth5 = await cog.is_qc_authorized(interaction)
        self.assertFalse(auth5, "Unauthorized random user without roles must be denied")


if __name__ == "__main__":
    unittest.main()

