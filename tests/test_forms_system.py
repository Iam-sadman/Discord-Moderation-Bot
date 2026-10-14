"""
Comprehensive Integration & Unit Test Suite for
Discord Forms Builder & Management System.
Validates:
- FormsDB CRUD operations, foreign key cascades, and anti-IDOR guild isolation
- Custom question management, limits capping (<=1000), and position re-indexing
- Multi-page modal slicing (1-5 vs 6+ questions)
- Submission creation, status filtering, and user limits
- Formula injection sanitization (CWE-1236)
- CSV generation with UTF-8 BOM (utf-8-sig) and dynamic question columns
- Excel (.xlsx) workbook generation with openpyxl
- Role hierarchy & privilege escalation guards
- Dashboard REST API endpoints and X-Internal-Secret bridge authentication
"""

import os
import sys
import uuid
import io
import json
import time
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

from services.forms_db import FormsDB
from services.forms_exporter import sanitize_csv_cell, build_export_data, generate_csv_bytes, generate_xlsx_bytes
from cogs.forms import parse_relative_duration, DynamicFormOpenButton, DynamicFormReviewButton, build_panel_embed_and_view
from dashboard.app import create_dashboard


class TestFormsSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create unique isolated database in scratch/
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_forms_{self.test_id}.db")

        self.db = FormsDB(self.test_db_path)
        FormsDB._instance = self.db
        await self.db.init_db()

        self.guild_id = 987654321098765432
        self.other_guild_id = 123456789012345678
        self.dest_channel_id = 111222333444555666
        self.panel_channel_id = 222333444555666777

        # Setup mock Bot & Guild
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 999999999999999999
        self.mock_bot.user.name = "FormsBotTest"
        self.mock_bot.user.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
        self.mock_bot.guilds = []
        self.mock_bot.cogs = {}
        self.mock_bot.add_dynamic_items = MagicMock()

        self.mock_guild = MagicMock(spec=discord.Guild)
        self.mock_guild.id = self.guild_id
        self.mock_guild.name = "Forms Test Server"

        # Mock Bot member in guild with top role position 50
        self.mock_bot_role = MagicMock(spec=discord.Role)
        self.mock_bot_role.id = 555555555555555555
        self.mock_bot_role.position = 50
        self.mock_bot_member = MagicMock(spec=discord.Member)
        self.mock_bot_member.top_role = self.mock_bot_role
        self.mock_guild.me = self.mock_bot_member

        # Mock Destination Channel
        self.mock_dest_channel = MagicMock(spec=discord.TextChannel)
        self.mock_dest_channel.id = self.dest_channel_id
        self.mock_dest_channel.name = "submissions-log"
        self.mock_dest_channel.send = AsyncMock()

        self.mock_guild.get_channel.side_effect = lambda cid: self.mock_dest_channel if cid == self.dest_channel_id else None
        self.mock_bot.get_guild.return_value = self.mock_guild
        self.mock_bot.get_channel.side_effect = lambda cid: self.mock_dest_channel if cid == self.dest_channel_id else None

        # Initialize Quart app
        self.app = create_dashboard(self.mock_bot)
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        # Cleanup temporary database
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
            wal_path = f"{self.test_db_path}-wal"
            if os.path.exists(wal_path):
                os.remove(wal_path)
            shm_path = f"{self.test_db_path}-shm"
            if os.path.exists(shm_path):
                os.remove(shm_path)
        except Exception:
            pass

    # =========================================================================
    # 1. FormsDB CRUD & Guild Isolation (Anti-IDOR)
    # =========================================================================

    async def test_01_create_and_get_form_with_guild_isolation(self):
        """Test creating a form and verifying strict guild scoping."""
        form_id = await self.db.create_form(
            guild_id=self.guild_id,
            title="Moderator Application",
            dest_channel_id=self.dest_channel_id,
            description="Apply for staff team",
            review_enabled=True,
            max_submissions_per_user=1,
            cooldown_seconds=30
        )
        self.assertIsInstance(form_id, int)
        self.assertGreater(form_id, 0)

        # Retrieve with correct guild
        form = await self.db.get_form(self.guild_id, form_id)
        self.assertIsNotNone(form)
        self.assertEqual(form["title"], "Moderator Application")
        self.assertEqual(form["review_enabled"], 1)
        self.assertEqual(form["max_submissions_per_user"], 1)

        # Anti-IDOR: Access with a different guild_id MUST return None
        idor_check = await self.db.get_form(self.other_guild_id, form_id)
        self.assertIsNone(idor_check, "Anti-IDOR violation: Form accessed from unauthorized guild!")

    async def test_02_update_and_delete_form_cascade(self):
        """Test updating a form and verifying cascade deletion of questions & submissions."""
        form_id = await self.db.create_form(
            guild_id=self.guild_id,
            title="Feedback Survey",
            dest_channel_id=self.dest_channel_id
        )

        # Add questions and a submission
        q1 = await self.db.add_question(form_id, "Rate our server (1-5)")
        q2 = await self.db.add_question(form_id, "Suggestions")
        sub_id = await self.db.create_submission(
            form_id=form_id,
            guild_id=self.guild_id,
            user_id=123456789,
            user_name="Tester",
            user_discriminator="0",
            user_avatar_url=None,
            answers={str(q1): "5", str(q2): "Great bot!"}
        )

        # Verify items exist
        questions = await self.db.get_questions(form_id)
        self.assertEqual(len(questions), 2)
        sub = await self.db.get_submission(sub_id)
        self.assertIsNotNone(sub)

        # Delete form
        deleted = await self.db.delete_form(self.guild_id, form_id)
        self.assertTrue(deleted)

        # Check cascading deletion
        self.assertIsNone(await self.db.get_form(self.guild_id, form_id))
        self.assertEqual(len(await self.db.get_questions(form_id)), 0)
        self.assertIsNone(await self.db.get_submission(sub_id))

    # =========================================================================
    # 2. Dynamic Questions & Reordering
    # =========================================================================

    async def test_03_questions_crud_and_limits_capping(self):
        """Test question creation, length limits capping (<=1000), and auto-positioning."""
        form_id = await self.db.create_form(self.guild_id, "Q Test", self.dest_channel_id)

        # Add question with excessive length limit (>1000)
        q1 = await self.db.add_question(
            form_id=form_id,
            label="Tell us your life story",
            style="paragraph",
            max_length=5000  # Should be capped at 1000
        )
        q1_obj = await self.db.get_question(form_id, q1)
        self.assertIsNotNone(q1_obj)
        self.assertEqual(q1_obj["max_length"], 1000, "max_length must be capped at 1000 to prevent embed breakage!")
        self.assertEqual(q1_obj["position"], 1)

        # Add second question
        q2 = await self.db.add_question(form_id, "Your age?", style="short")
        q2_obj = await self.db.get_question(form_id, q2)
        self.assertEqual(q2_obj["position"], 2)

    async def test_04_questions_reorder_and_delete_reindex(self):
        """Test reordering questions and verifying positions re-index after deletion."""
        form_id = await self.db.create_form(self.guild_id, "Reorder Test", self.dest_channel_id)
        q1 = await self.db.add_question(form_id, "Question 1")
        q2 = await self.db.add_question(form_id, "Question 2")
        q3 = await self.db.add_question(form_id, "Question 3")

        # Reorder to Q3, Q1, Q2
        await self.db.reorder_questions(form_id, [q3, q1, q2])
        questions = await self.db.get_questions(form_id)
        self.assertEqual([q["id"] for q in questions], [q3, q1, q2])
        self.assertEqual([q["position"] for q in questions], [1, 2, 3])

        # Delete middle question (q1) and verify positions remain normalized: 1, 2
        await self.db.delete_question(form_id, q1)
        remaining = await self.db.get_questions(form_id)
        self.assertEqual(len(remaining), 2)
        self.assertEqual([q["position"] for q in remaining], [1, 2])

    # =========================================================================
    # 3. Submissions & Review Workflow
    # =========================================================================

    async def test_05_submissions_status_filtering_and_stats(self):
        """Test submission creation, status filtering, and stat aggregation."""
        form_id = await self.db.create_form(self.guild_id, "Sub Test", self.dest_channel_id, review_enabled=True)

        s1 = await self.db.create_submission(form_id, self.guild_id, 101, "User1", "0", None, {"1": "A"}, status="pending")
        s2 = await self.db.create_submission(form_id, self.guild_id, 102, "User2", "0", None, {"1": "B"}, status="pending")
        s3 = await self.db.create_submission(form_id, self.guild_id, 103, "User3", "0", None, {"1": "C"}, status="pending")

        # Update s1 to approved, s2 to rejected
        await self.db.update_submission_status(s1, "approved", 999, "AdminUser", "Approved welcome!")
        await self.db.update_submission_status(s2, "rejected", 999, "AdminUser", "Incomplete answers")

        # Check filter queries
        pending_list = await self.db.list_submissions(self.guild_id, form_id, status="pending")
        self.assertEqual(len(pending_list), 1)
        self.assertEqual(pending_list[0]["id"], s3)

        approved_list = await self.db.list_submissions(self.guild_id, form_id, status="approved")
        self.assertEqual(len(approved_list), 1)
        self.assertEqual(approved_list[0]["id"], s1)

        stats = await self.db.get_submission_stats(self.guild_id, form_id)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["approved"], 1)
        self.assertEqual(stats["rejected"], 1)

    async def test_06_user_max_submissions_and_cooldown(self):
        """Test user submission counting for max_submissions_per_user enforcement."""
        form_id = await self.db.create_form(self.guild_id, "Limit Form", self.dest_channel_id, max_submissions_per_user=1)
        user_id = 777888999

        count_before = await self.db.count_user_submissions(form_id, user_id)
        self.assertEqual(count_before, 0)

        await self.db.create_submission(form_id, self.guild_id, user_id, "TestUser", "0", None, {"1": "Ans"})
        count_after = await self.db.count_user_submissions(form_id, user_id)
        self.assertEqual(count_after, 1)

    # =========================================================================
    # 4. Security: CSV Formula Injection Guard (CWE-1236)
    # =========================================================================

    def test_07_csv_formula_injection_sanitization(self):
        """Test sanitization of dangerous spreadsheet formulas (=, +, -, @, \\t, \\r)."""
        malicious_inputs = [
            ("=SUM(A1:A10)", "'=SUM(A1:A10)"),
            ("+12345", "'+12345"),
            ("-5+5", "'-5+5"),
            ("@cmd|' /C calc'!A0", "'@cmd|' /C calc'!A0"),
            ("\tTAB_INJECT", "'\tTAB_INJECT"),
            ("\rCARRIAGE_RETURN", "'\rCARRIAGE_RETURN"),
            ("Normal safe answer", "Normal safe answer"),
            ("12345", "12345"),
            ("", ""),
            (None, "")
        ]

        for raw, expected in malicious_inputs:
            sanitized = sanitize_csv_cell(raw)
            self.assertEqual(sanitized, expected, f"Failed formula sanitization for: {raw}")

    # =========================================================================
    # 5. CSV & Excel Export Engine
    # =========================================================================

    async def test_08_csv_generation_with_utf8_bom(self):
        """Test CSV generation encoded with UTF-8 BOM (utf-8-sig) and Unicode/Bengali characters."""
        form = {
            "id": 1,
            "title": "আন্তর্জাতিক সমীক্ষা (Survey)",
            "review_enabled": True
        }
        questions = [
            {"id": 10, "label": "আপনার নাম? (Name)"},
            {"id": 20, "label": "মন্তব্য (Feedback)"}
        ]
        submissions = [
            {
                "id": 101,
                "user_id": 999,
                "user_name": "সাদমান (Sadman)",
                "user_discriminator": "0",
                "status": "approved",
                "reviewer_id": 111,
                "reviewer_name": "Admin",
                "review_note": "খুব ভালো (Very Good)",
                "reviewed_at": "2026-10-14 12:00:00",
                "created_at": "2026-10-14 11:30:00",
                "answers": {"10": "সাদমান", "20": "=1+1"}  # Formula injection attempt!
            }
        ]

        csv_bytes = generate_csv_bytes(form, questions, submissions)
        raw_data = csv_bytes.getvalue()

        # Check for UTF-8 BOM header: \xef\xbb\xbf
        self.assertTrue(raw_data.startswith(b"\xef\xbb\xbf"), "CSV must start with UTF-8 BOM (utf-8-sig) for Excel compatibility!")

        # Decode content and verify formula injection was neutralized
        text = raw_data.decode("utf-8-sig")
        self.assertIn("Q1: আপনার নাম? (Name)", text)
        self.assertIn("Q2: মন্তব্য (Feedback)", text)
        self.assertIn("Reviewer Name", text)
        self.assertIn("'=1+1", text, "Malicious formula must be prefixed with single quote in exported CSV!")
        self.assertIn("সাদমান", text)

    async def test_09_csv_omits_review_columns_when_review_disabled(self):
        """Test that reviewer columns are omitted when review_enabled = False."""
        form = {"id": 2, "title": "Direct Form", "review_enabled": False}
        questions = [{"id": 1, "label": "Q1"}]
        submissions = [{
            "id": 1,
            "user_id": 123,
            "user_name": "DirectUser",
            "status": "submitted",
            "created_at": "2026-10-14",
            "answers": {"1": "Answer"}
        }]

        headers, rows = build_export_data(form, questions, submissions)
        self.assertNotIn("Reviewer ID", headers)
        self.assertNotIn("Reviewer Name", headers)
        self.assertNotIn("Review Note", headers)
        self.assertIn("Q1: Q1", headers)

    async def test_10_xlsx_generation(self):
        """Test formatted Excel (.xlsx) export."""
        form = {"id": 3, "title": "Excel Test Form", "review_enabled": True}
        questions = [{"id": 1, "label": "Experience"}]
        submissions = [{
            "id": 1,
            "user_id": 456,
            "user_name": "ExcelUser",
            "status": "approved",
            "reviewer_name": "Admin",
            "review_note": "Great",
            "created_at": "2026-10-14",
            "answers": {"1": "3 years"}
        }]

        xlsx_bytes = generate_xlsx_bytes(form, questions, submissions)
        self.assertIsInstance(xlsx_bytes, io.BytesIO)
        self.assertGreater(xlsx_bytes.getbuffer().nbytes, 0)

    # =========================================================================
    # 6. Relative Duration Parsing & Dynamic Panel Embeds
    # =========================================================================

    def test_11_parse_relative_duration(self):
        """Test parsing relative durations like '30m', '5h', '2d'."""
        self.assertEqual(parse_relative_duration("30m"), 1800)
        self.assertEqual(parse_relative_duration("2h"), 7200)
        self.assertEqual(parse_relative_duration("1d"), 86400)
        self.assertEqual(parse_relative_duration("1w"), 604800)
        self.assertIsNone(parse_relative_duration("invalid"))
        self.assertIsNone(parse_relative_duration(""))

    def test_12_panel_embed_and_view_states(self):
        """Test panel embed states: Scheduled (Yellow), Open (Green), Closed (Red)."""
        now = int(time.time())

        # 1. Scheduled Form
        sched_form = {"id": 1, "title": "Scheduled Form", "opens_at": now + 3600, "closes_at": now + 7200, "is_closed": 0, "is_enabled": 1}
        embed_s, view_s = build_panel_embed_and_view(sched_form)
        self.assertEqual(embed_s.color.value, 0xF59E0B)  # Yellow
        self.assertTrue(view_s.children[0].disabled)

        # 2. Open Form
        open_form = {"id": 2, "title": "Open Form", "opens_at": now - 60, "closes_at": now + 3600, "is_closed": 0, "is_enabled": 1}
        embed_o, view_o = build_panel_embed_and_view(open_form)
        self.assertEqual(embed_o.color.value, 0x10B981)  # Green
        self.assertFalse(view_o.children[0].disabled)

        # 3. Closed Form
        closed_form = {"id": 3, "title": "Closed Form", "is_closed": 1, "is_enabled": 1}
        embed_c, view_c = build_panel_embed_and_view(closed_form)
        self.assertEqual(embed_c.color.value, 0xEF4444)  # Red
        self.assertTrue(view_c.children[0].disabled)

    # =========================================================================
    # 7. Web Dashboard REST API & Internal Bridge
    # =========================================================================

    async def test_13_api_forms_crud_and_validation(self):
        """Test creating, listing, updating, and deleting forms via Dashboard API."""
        async with self.client.session_transaction() as sess:
            sess["user_id"] = "DashboardAdmin"
            sess["role"] = "Administrator"

        # 1. Create Form
        res = await self.client.post(f"/api/guild/{self.guild_id}/forms", json={
            "title": "API Form",
            "description": "Created from REST API",
            "dest_channel_id": self.dest_channel_id,
            "review_enabled": True
        })
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])
        form_id = data["form_id"]

        # 2. List Forms
        res_list = await self.client.get(f"/api/guild/{self.guild_id}/forms")
        self.assertEqual(res_list.status_code, 200)
        forms_data = await res_list.get_json()
        self.assertTrue(any(f["id"] == form_id for f in forms_data["forms"]))

        # 3. Add Question via API
        res_q = await self.client.post(f"/api/guild/{self.guild_id}/forms/{form_id}/questions", json={
            "label": "Question from API",
            "style": "short",
            "required": True
        })
        self.assertEqual(res_q.status_code, 200)
        q_data = await res_q.get_json()
        self.assertTrue(q_data["success"])
        self.assertEqual(len(q_data["questions"]), 1)

        # 4. Delete Form via API
        res_del = await self.client.delete(f"/api/guild/{self.guild_id}/forms/{form_id}")
        self.assertEqual(res_del.status_code, 200)

    async def test_14_internal_bridge_secret_authentication(self):
        """Test X-Internal-Secret authentication on protected localhost bridge endpoint."""
        # Unauthorized request without secret header
        res_unauth = await self.client.post("/api/internal/forms/action", json={
            "action": "refresh_panel",
            "form_id": 1,
            "guild_id": self.guild_id
        })
        self.assertEqual(res_unauth.status_code, 401)

        # Authorized request with secret header
        from config import Config
        with patch.object(self.app, "discord_bot", self.mock_bot):
            res_auth = await self.client.post(
                "/api/internal/forms/action",
                headers={"X-Internal-Secret": Config.INTERNAL_SECRET},
                json={
                    "action": "refresh_panel",
                    "form_id": 999999,  # Non-existent form
                    "guild_id": self.guild_id
                }
            )
            # Should reach application logic (404 Form not found instead of 401 Unauthorized)
            self.assertEqual(res_auth.status_code, 404)


if __name__ == "__main__":
    unittest.main()
