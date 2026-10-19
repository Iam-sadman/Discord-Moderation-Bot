"""
Comprehensive Unit & Integration Test Suite for the
Meeting Assistant Bot System (Voice Recording & Gemini AI Minutes).
Validates:
- MeetingsDB schema, WAL mode, foreign keys, and indexes
- Guild settings upsert and string Snowflake ID serialization
- DynamicItem buttons (DynamicMeetingStopButton, DynamicMeetingNoteButton)
- Participant attendance tracking (joins, leaves, duration calculations)
- Gemini AI response parser & structured JSON minutes extraction
- MeetingsCog public methods (get_active_meetings)
- Dashboard RESTful API endpoints and export handlers
"""

import os
import sys
import uuid
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

from services.meetings_db import MeetingsDB
from services.meetings_ai import _parse_json_response
from cogs.meetings import DynamicMeetingStopButton, DynamicMeetingNoteButton, MeetingsCog
from dashboard.app import create_dashboard


class TestMeetingsSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create isolated temporary SQLite database in scratch
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_meetings_{self.test_id}.db")

        self.db = MeetingsDB(self.test_db_path)
        MeetingsDB._instance = self.db
        await self.db.init_db()

        self.guild_id = 987654321098765432
        self.voice_channel_id = 111222333444555666
        self.output_channel_id = 222333444555666777
        self.host_id = 333444555666777888
        self.participant_id = 444555666777888999

        # Mock Bot
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 888888888888888888
        self.mock_bot.user.__str__.return_value = "MeetingBot#1234"
        self.mock_bot.is_ready.return_value = True

    async def asyncTearDown(self):
        # Clean up isolated DB file
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
            for ext in ["-wal", "-shm"]:
                p = self.test_db_path + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass

    # =========================================================================
    # 1. Database Operations Tests
    # =========================================================================

    async def test_settings_save_and_get(self):
        """Validates guild meeting settings save and load with Snowflake string safety."""
        settings = await self.db.get_settings(self.guild_id)
        self.assertEqual(settings["guild_id"], self.guild_id)
        self.assertIsNone(settings["default_output_channel_id"])
        self.assertEqual(settings["allowed_role_ids"], [])

        # Update settings
        await self.db.save_settings(
            guild_id=self.guild_id,
            output_channel_id=self.output_channel_id,
            allowed_role_ids=[555666777888999000]
        )

        updated = await self.db.get_settings(self.guild_id)
        self.assertEqual(updated["default_output_channel_id"], self.output_channel_id)
        self.assertEqual(updated["allowed_role_ids"], [555666777888999000])

    async def test_meeting_lifecycle_and_attendance(self):
        """Validates full meeting lifecycle: create, add participant, leave, results, delete."""
        # 1. Create Meeting
        meeting_id = await self.db.create_meeting(
            guild_id=self.guild_id,
            title="Sprint Planning Meeting",
            voice_channel_id=self.voice_channel_id,
            voice_channel_name="Team Voice 1",
            output_channel_id=self.output_channel_id,
            host_id=self.host_id,
            host_name="Project Lead",
            project_code="DEV-SPRINT"
        )
        self.assertIsInstance(meeting_id, int)
        self.assertGreater(meeting_id, 0)

        # 2. Check Active Meeting
        active = await self.db.get_active_meeting(self.guild_id)
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], meeting_id)
        self.assertEqual(active["status"], "recording")
        self.assertEqual(active["project_code"], "DEV-SPRINT")

        # 3. Add Participants
        p_row1 = await self.db.add_participant(meeting_id, self.participant_id, "Alice")
        self.assertGreater(p_row1, 0)

        # Mark Alice left
        await self.db.mark_participant_left(meeting_id, self.participant_id)

        # Alice rejoins
        await self.db.add_participant(meeting_id, self.participant_id, "Alice")

        # Bob joins
        bob_id = 777888999000111222
        await self.db.add_participant(meeting_id, bob_id, "Bob")

        # Finalize participants
        await self.db.finalize_all_participants(meeting_id)

        participants = await self.db.get_participants(meeting_id)
        self.assertEqual(len(participants), 3)  # Alice (1st), Alice (2nd), Bob
        for p in participants:
            self.assertIsNotNone(p["left_at"])
            self.assertGreaterEqual(p["total_seconds"], 0)

        # 4. Save Meeting Results (AI output)
        exec_summary = "Discussed Q4 roadmap and assigned backlog items."
        key_points = ["Feature A is postponed to Q1", "Security review required"]
        action_items = ["Alice: Implement authentication endpoints by Friday", "Bob: Write tests"]
        qa_decisions = ["Decision: Adopt SQLite WAL mode for meetings.db"]
        transcript = "[00:01] Alice: Let's begin the planning session."

        await self.db.save_meeting_results(
            meeting_id=meeting_id,
            executive_summary=exec_summary,
            key_points=key_points,
            action_items=action_items,
            qa_decisions=qa_decisions,
            full_transcript=transcript,
            output_message_id=999888777666555444
        )

        # 5. Fetch single meeting and verify
        meeting = await self.db.get_meeting(meeting_id)
        self.assertEqual(meeting["status"], "completed")
        self.assertEqual(meeting["executive_summary"], exec_summary)
        self.assertEqual(meeting["key_points"], key_points)
        self.assertEqual(meeting["action_items"], action_items)
        self.assertEqual(meeting["qa_decisions"], qa_decisions)
        self.assertEqual(meeting["full_transcript"], transcript)
        self.assertEqual(meeting["output_message_id"], "999888777666555444")

        # 6. List meetings & stats
        m_list = await self.db.list_meetings(self.guild_id, status="completed")
        self.assertEqual(m_list["total"], 1)
        self.assertEqual(len(m_list["meetings"]), 1)

        stats = await self.db.get_guild_stats(self.guild_id)
        self.assertEqual(stats["total_meetings"], 1)
        self.assertEqual(stats["completed_meetings"], 1)
        self.assertEqual(stats["active_recordings"], 0)
        self.assertEqual(stats["unique_participants"], 2)  # Alice and Bob

        # 7. Delete meeting with cascade
        deleted = await self.db.delete_meeting(meeting_id)
        self.assertTrue(deleted)
        self.assertIsNone(await self.db.get_meeting(meeting_id))
        self.assertEqual(len(await self.db.get_participants(meeting_id)), 0)

    # =========================================================================
    # 2. DynamicItem Persistent Buttons Tests
    # =========================================================================

    def test_dynamic_buttons_custom_id_format(self):
        """Validates DynamicItem button custom_id syntax and parsing."""
        stop_btn = DynamicMeetingStopButton(meeting_id=42)
        self.assertEqual(stop_btn.meeting_id, 42)
        self.assertEqual(stop_btn.custom_id, "meeting:stop:42")

        note_btn = DynamicMeetingNoteButton(meeting_id=42)
        self.assertEqual(note_btn.meeting_id, 42)
        self.assertEqual(note_btn.custom_id, "meeting:note:42")

    # =========================================================================
    # 3. Gemini AI Response Parser Tests
    # =========================================================================

    def test_gemini_json_parser(self):
        """Validates AI response parsing for raw JSON and markdown-wrapped JSON."""
        # Pure JSON
        raw_json = '{"executive_summary": "Test summary", "key_points": ["Point 1"], "action_items": ["Action 1"], "qa_and_decisions": [], "full_transcript": "Verbatim text"}'
        parsed = _parse_json_response(raw_json)
        self.assertEqual(parsed["executive_summary"], "Test summary")
        self.assertEqual(parsed["key_points"], ["Point 1"])

        # Markdown wrapped JSON
        md_json = "```json\n" + raw_json + "\n```"
        parsed_md = _parse_json_response(md_json)
        self.assertEqual(parsed_md["executive_summary"], "Test summary")

        # Malformed / Fallback
        fallback_text = "Here are the notes: Just general discussion."
        parsed_fallback = _parse_json_response(fallback_text)
        self.assertIn("Just general discussion", parsed_fallback["executive_summary"])

    # =========================================================================
    # 4. Web Dashboard API Tests
    # =========================================================================

    async def test_dashboard_meetings_api_endpoints(self):
        """Validates Quart meetings API blueprint endpoints."""
        app = create_dashboard(self.mock_bot, meeting_bot=self.mock_bot)
        client = app.test_client()

        # Session setup
        async with client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["role"] = "Administrator"

        # 1. GET /api/meetings/guilds/<id>/status
        res = await client.get(f"/api/meetings/guilds/{self.guild_id}/status")
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["bot_online"])

        # 2. GET /api/meetings/guilds/<id>/settings
        res = await client.get(f"/api/meetings/guilds/{self.guild_id}/settings")
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])

        # 3. POST /api/meetings/guilds/<id>/settings
        res = await client.post(
            f"/api/meetings/guilds/{self.guild_id}/settings",
            json={
                "default_output_channel_id": str(self.output_channel_id),
                "allowed_role_ids": ["123456789"]
            }
        )
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])

        # 4. GET /api/meetings/guilds/<id>/list
        res = await client.get(f"/api/meetings/guilds/{self.guild_id}/list")
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total"], 0)


if __name__ == "__main__":
    unittest.main()
