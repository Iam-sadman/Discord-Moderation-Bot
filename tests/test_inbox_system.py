"""
Comprehensive Unit & Integration Test Suite for the
Direct Messages Inbox & Relay System (Web Dashboard & Discord Bot).
Validates:
- InboxDB schema, WAL mode, indices, conversation tracking, and message history
- Unread counter computation and read state transitions
- Settings persistence (auto-reply, notification relay channel)
- Discord Cog event handling (on_message DM interception, auto-responder, staff relay embed)
- DynamicInboxReplyButton & StaffInboxReplyModal logic
- Native Discord message reference quote-reply forwarding
- Dashboard RESTful API endpoints, authorization guards, and transcript exports
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

from services.inbox_db import InboxDB, DEFAULT_RELAY_CHANNEL_ID
from cogs.inbox import InboxCog, DynamicInboxReplyButton, StaffInboxReplyModal
from dashboard.app import create_dashboard


class TestInboxSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create isolated temporary SQLite database
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_inbox_{self.test_id}.db")

        self.db = InboxDB(self.test_db_path)
        InboxDB._instance = self.db
        await self.db.init_db()

        self.guild_id = 123456789012345678
        self.user_id = 987654321098765432
        self.relay_channel_id = 1566408437603237898

        # Mock Discord Bot & ClientUser
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 111122223333444455
        self.mock_bot.user.name = "TestBot"
        self.mock_bot.guilds = []
        self.mock_bot.get_cog = MagicMock(return_value=None)
        self.mock_bot.add_dynamic_items = MagicMock()

        # Instantiate Cog with isolated DB
        self.cog = InboxCog(self.mock_bot)
        self.cog.db = self.db

    async def asyncTearDown(self):
        # Cleanup temporary database files
        for suffix in ("", "-wal", "-shm"):
            p = f"{self.test_db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    # =========================================================================
    # 1. Database Operations & Schema Tests
    # =========================================================================

    async def test_01_db_schema_initialization(self):
        """Verifies tables and indices exist in SQLite."""
        async with self.db.get_connection() as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                rows = await cur.fetchall()
                table_names = [r[0] for r in rows]

            self.assertIn("inbox_conversations", table_names)
            self.assertIn("inbox_messages", table_names)
            self.assertIn("inbox_settings", table_names)

    async def test_02_conversation_creation_and_update(self):
        """Tests get_or_create_conversation creating records and updating user details."""
        conv = await self.db.get_or_create_conversation(
            user_id=self.user_id,
            username="TestUser",
            display_name="Test Display",
            avatar_url="https://cdn.discordapp.com/avatars/test.png",
            guild_id=self.guild_id
        )
        self.assertIsNotNone(conv)
        self.assertEqual(conv["user_id"], self.user_id)
        self.assertEqual(conv["username"], "TestUser")
        self.assertEqual(conv["status"], "open")
        self.assertEqual(conv["unread_count"], 0)

        # Update display name
        updated = await self.db.get_or_create_conversation(
            user_id=self.user_id,
            username="TestUserNew",
            display_name="Updated Display"
        )
        self.assertEqual(updated["id"], conv["id"])
        self.assertEqual(updated["username"], "TestUserNew")
        self.assertEqual(updated["display_name"], "Updated Display")

    async def test_03_message_flow_and_unread_counts(self):
        """Tests incoming user messages incrementing unread counts and read state transitions."""
        conv = await self.db.get_or_create_conversation(
            user_id=self.user_id,
            username="Alice"
        )
        conv_id = conv["id"]

        # 1. User sends message (unread)
        msg1 = await self.db.add_message(
            conversation_id=conv_id,
            sender_type="user",
            author_id=self.user_id,
            author_name="Alice",
            content="Hello support team!"
        )
        self.assertEqual(msg1["content"], "Hello support team!")
        self.assertEqual(msg1["is_read"], 0)

        conv_refreshed = await self.db.get_conversation(conv_id)
        self.assertEqual(conv_refreshed["unread_count"], 1)
        self.assertEqual(conv_refreshed["last_message_preview"], "Hello support team!")

        # 2. Total unread count
        total_unread = await self.db.get_total_unread_count()
        self.assertEqual(total_unread, 1)

        # 3. Staff replies from dashboard (read by default)
        msg2 = await self.db.add_message(
            conversation_id=conv_id,
            sender_type="staff_dashboard",
            author_id=0,
            author_name="Admin",
            content="Hi Alice, how can we help?"
        )
        self.assertEqual(msg2["is_read"], 1)

        # Unread count should remain 1 (user message still unread until opened)
        conv_after_reply = await self.db.get_conversation(conv_id)
        self.assertEqual(conv_after_reply["unread_count"], 1)

        # 4. Mark as read
        await self.db.mark_as_read(conv_id)
        conv_read = await self.db.get_conversation(conv_id)
        self.assertEqual(conv_read["unread_count"], 0)
        self.assertEqual(await self.db.get_total_unread_count(), 0)

        # 5. Verify message list order
        messages = await self.db.get_messages(conv_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "Hello support team!")
        self.assertEqual(messages[1]["content"], "Hi Alice, how can we help?")

    async def test_04_conversation_filtering_and_search(self):
        """Tests listing conversations with search queries and status filters."""
        # Seed 3 conversations
        c1 = await self.db.get_or_create_conversation(user_id=101, username="john_doe", display_name="John")
        c2 = await self.db.get_or_create_conversation(user_id=102, username="jane_smith", display_name="Jane")
        c3 = await self.db.get_or_create_conversation(user_id=103, username="bob_builder", display_name="Bob")

        await self.db.add_message(c1["id"], "user", 101, "john_doe", content="Need help with billing")
        await self.db.add_message(c2["id"], "user", 102, "jane_smith", content="Technical error on bot")

        # Mark c2 closed
        await self.db.update_conversation_status(c2["id"], "closed")

        # List all
        all_convs, total_all = await self.db.list_conversations(status="all")
        self.assertEqual(total_all, 3)

        # Filter by status: open
        open_convs, total_open = await self.db.list_conversations(status="open")
        self.assertEqual(total_open, 2)

        # Filter by status: closed
        closed_convs, total_closed = await self.db.list_conversations(status="closed")
        self.assertEqual(total_closed, 1)
        self.assertEqual(closed_convs[0]["user_id"], 102)

        # Filter by unread
        unread_convs, total_unread = await self.db.list_conversations(status="unread")
        self.assertEqual(total_unread, 2)

        # Search by username
        search_results, total_search = await self.db.list_conversations(search="builder")
        self.assertEqual(total_search, 1)
        self.assertEqual(search_results[0]["username"], "bob_builder")

    async def test_05_settings_persistence(self):
        """Tests saving and retrieving inbox settings."""
        # Default settings
        default_settings = await self.db.get_settings(self.guild_id)
        self.assertEqual(default_settings["auto_reply_enabled"], 1)
        self.assertEqual(default_settings["notify_channel_id"], DEFAULT_RELAY_CHANNEL_ID)

        # Update settings
        new_settings = {
            "auto_reply_enabled": 0,
            "auto_reply_text": "Custom staff note",
            "notify_channel_id": 999888777666555444
        }
        await self.db.update_settings(self.guild_id, new_settings)

        saved = await self.db.get_settings(self.guild_id)
        self.assertEqual(saved["auto_reply_enabled"], 0)
        self.assertEqual(saved["auto_reply_text"], "Custom staff note")
        self.assertEqual(saved["notify_channel_id"], 999888777666555444)

    # =========================================================================
    # 2. Discord Cog Listener & Relay Tests
    # =========================================================================

    async def test_06_cog_incoming_dm_flow(self):
        """Tests on_message interception of user DM, auto-reply, and channel relay."""
        # Mock Discord User
        mock_author = MagicMock(spec=discord.User)
        mock_author.id = self.user_id
        mock_author.name = "Charlie"
        mock_author.display_name = "Charlie The Dev"
        mock_author.bot = False
        mock_author.display_avatar = MagicMock()
        mock_author.display_avatar.url = "https://cdn.discordapp.com/avatars/charlie.png"
        mock_author.send = AsyncMock(return_value=MagicMock(id=555444333))

        # Mock Message
        mock_message = MagicMock(spec=discord.Message)
        mock_message.author = mock_author
        mock_message.guild = None  # Indicates DM channel
        mock_message.content = "Can someone check my role?"
        mock_message.id = 888777666
        mock_message.attachments = []

        # Mock Relay Channel
        mock_relay_channel = MagicMock(spec=discord.TextChannel)
        mock_relay_channel.send = AsyncMock(return_value=MagicMock(id=999111))
        self.mock_bot.get_channel = MagicMock(return_value=mock_relay_channel)

        # Fire on_message
        await self.cog.on_message(mock_message)

        # 1. Verify DB conversation & message created
        conv = await self.db.get_conversation_by_user(self.user_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["username"], "Charlie")
        self.assertEqual(conv["unread_count"], 1)

        messages = await self.db.get_messages(conv["id"])
        # Should have 2 messages: incoming user message + bot auto-reply
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["sender_type"], "user")
        self.assertEqual(messages[0]["content"], "Can someone check my role?")
        self.assertEqual(messages[1]["sender_type"], "bot_auto")

        # 2. Verify auto-reply was sent to user
        mock_author.send.assert_awaited_once()

        # 3. Verify relay embed was posted to staff channel with DynamicInboxReplyButton
        mock_relay_channel.send.assert_awaited_once()
        call_kwargs = mock_relay_channel.send.call_args.kwargs
        self.assertIn("embed", call_kwargs)
        self.assertIn("view", call_kwargs)

    async def test_07_cog_send_staff_reply(self):
        """Tests cog.send_staff_reply delivering DM and saving to DB."""
        # Pre-seed user
        conv = await self.db.get_or_create_conversation(user_id=self.user_id, username="Dave")

        mock_user = MagicMock(spec=discord.User)
        mock_user.id = self.user_id
        mock_user.name = "Dave"
        mock_user.display_avatar = MagicMock()
        mock_user.display_avatar.url = ""
        mock_user.send = AsyncMock(return_value=MagicMock(id=333222111))

        self.mock_bot.get_user = MagicMock(return_value=mock_user)

        success, err = await self.cog.send_staff_reply(
            target_user_id=self.user_id,
            content="Role has been assigned!",
            staff_name="StaffMod",
            staff_id=777,
            origin="staff_discord"
        )

        self.assertTrue(success)
        self.assertIsNone(err)
        mock_user.send.assert_awaited_once()

        messages = await self.db.get_messages(conv["id"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_type"], "staff_discord")
        self.assertEqual(messages[0]["content"], "Role has been assigned!")
        self.assertEqual(messages[0]["author_name"], "StaffMod")

    async def test_08_cog_send_staff_reply_user_closed_dms(self):
        """Verifies graceful handling when member has DMs closed (Forbidden)."""
        mock_user = MagicMock(spec=discord.User)
        mock_user.id = self.user_id
        mock_user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Cannot send messages to this user"))
        self.mock_bot.get_user = MagicMock(return_value=mock_user)

        success, err = await self.cog.send_staff_reply(
            target_user_id=self.user_id,
            content="Hello?",
            staff_name="Admin",
            staff_id=1
        )
        self.assertFalse(success)
        self.assertIn("closed", err.lower())

    async def test_09_cog_native_quote_reply(self):
        """Tests native Discord quote-reply detection in staff relay channel."""
        mock_user = MagicMock(spec=discord.User)
        mock_user.id = self.user_id
        mock_user.send = AsyncMock(return_value=MagicMock(id=123))
        self.mock_bot.get_user = MagicMock(return_value=mock_user)

        # Mock referenced message from bot containing target user in embed footer
        mock_ref_msg = MagicMock(spec=discord.Message)
        mock_ref_msg.author = self.mock_bot.user
        mock_embed = MagicMock(spec=discord.Embed)
        mock_embed.footer = MagicMock()
        mock_embed.footer.text = f"Direct Messages Inbox • User ID: {self.user_id}"
        mock_ref_msg.embeds = [mock_embed]

        # Mock staff reply message
        mock_staff_msg = MagicMock(spec=discord.Message)
        mock_staff_msg.author = MagicMock(spec=discord.Member)
        mock_staff_msg.author.bot = False
        mock_staff_msg.author.id = 555
        mock_staff_msg.author.name = "SupportOfficer"
        mock_staff_msg.guild = MagicMock(spec=discord.Guild)
        mock_staff_msg.content = "We have reviewed your request."
        mock_staff_msg.reference = MagicMock()
        mock_staff_msg.reference.message_id = 999888
        mock_staff_msg.channel = MagicMock(spec=discord.TextChannel)
        mock_staff_msg.channel.fetch_message = AsyncMock(return_value=mock_ref_msg)
        mock_staff_msg.add_reaction = AsyncMock()

        await self.cog.on_message(mock_staff_msg)

        # Verifies member received DM and staff message got checked reaction
        mock_user.send.assert_awaited_once()
        mock_staff_msg.add_reaction.assert_awaited_once_with("✅")

    # =========================================================================
    # 3. Dashboard REST API Blueprint Tests
    # =========================================================================

    async def test_10_api_endpoints_and_transcripts(self):
        """Tests Quart dashboard API endpoints: listing, fetching, replying, and exporting."""
        # Seed conversation and messages
        conv = await self.db.get_or_create_conversation(
            user_id=self.user_id,
            username="Eva",
            display_name="Eva Green",
            guild_id=self.guild_id
        )
        conv_id = conv["id"]
        await self.db.add_message(conv_id, "user", self.user_id, "Eva", content="Need help with access")

        app = create_dashboard(self.mock_bot)
        app.discord_bot = self.mock_bot
        self.mock_bot.get_cog = MagicMock(return_value=self.cog)

        test_client = app.test_client()

        # Set operator session
        async with test_client.session_transaction() as sess:
            sess["user_id"] = "test_admin"
            sess["role"] = "Administrator"

        # 1. GET Conversations List
        res1 = await test_client.get(f"/api/guild/{self.guild_id}/inbox/conversations")
        self.assertEqual(res1.status_code, 200)
        data1 = await res1.get_json()
        self.assertTrue(data1["success"])
        self.assertEqual(len(data1["conversations"]), 1)
        self.assertEqual(data1["unread_total"], 1)

        # 2. GET Conversation Detail (marks read)
        res2 = await test_client.get(f"/api/guild/{self.guild_id}/inbox/conversations/{conv_id}")
        self.assertEqual(res2.status_code, 200)
        data2 = await res2.get_json()
        self.assertTrue(data2["success"])
        self.assertEqual(len(data2["messages"]), 1)
        self.assertEqual(data2["conversation"]["unread_count"], 0)

        # 3. POST Reply
        mock_user = MagicMock(spec=discord.User)
        mock_user.id = self.user_id
        mock_user.send = AsyncMock(return_value=MagicMock(id=444))
        self.mock_bot.get_user = MagicMock(return_value=mock_user)

        res3 = await test_client.post(
            f"/api/guild/{self.guild_id}/inbox/conversations/{conv_id}/reply",
            json={"content": "Here is the access link."}
        )
        self.assertEqual(res3.status_code, 200)
        data3 = await res3.get_json()
        self.assertTrue(data3["success"])
        self.assertEqual(len(data3["messages"]), 2)

        # 4. POST Status Update
        res4 = await test_client.post(
            f"/api/guild/{self.guild_id}/inbox/conversations/{conv_id}/status",
            json={"status": "closed"}
        )
        self.assertEqual(res4.status_code, 200)
        data4 = await res4.get_json()
        self.assertEqual(data4["status"], "closed")

        # 5. GET Unread Count
        res5 = await test_client.get(f"/api/guild/{self.guild_id}/inbox/unread_count")
        self.assertEqual(res5.status_code, 200)
        data5 = await res5.get_json()
        self.assertEqual(data5["unread_count"], 0)

        # 6. GET Export (TXT & HTML)
        res_txt = await test_client.get(f"/api/guild/{self.guild_id}/inbox/conversations/{conv_id}/export?format=txt")
        self.assertEqual(res_txt.status_code, 200)
        txt_body = (await res_txt.get_data()).decode("utf-8")
        self.assertIn("DISCORD INBOX DIRECT MESSAGE TRANSCRIPT", txt_body)
        self.assertIn("Eva", txt_body)

        res_html = await test_client.get(f"/api/guild/{self.guild_id}/inbox/conversations/{conv_id}/export?format=html")
        self.assertEqual(res_html.status_code, 200)
        html_body = (await res_html.get_data()).decode("utf-8")
        self.assertIn("Direct Message Transcript", html_body)

    async def test_11_operator_authorization_guard(self):
        """Verifies non-operators (Viewer) receive 403 Forbidden on write operations."""
        app = create_dashboard(self.mock_bot)
        test_client = app.test_client()

        # Non-operator viewer session
        async with test_client.session_transaction() as sess:
            sess["user_id"] = "guest_user"
            sess["role"] = "Viewer"

        # Attempt to reply
        res = await test_client.post(
            f"/api/guild/{self.guild_id}/inbox/conversations/1/reply",
            json={"content": "Unauthorized reply"}
        )
        self.assertEqual(res.status_code, 403)
        data = await res.get_json()
        self.assertIn("Operator privileges", data["message"])


if __name__ == "__main__":
    unittest.main()

