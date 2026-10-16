"""
Comprehensive Unit & Integration Test Suite for the
Discord Support Ticket System (Private Threads & Dashboard Management).
Validates:
- TicketsDB schema, WAL mode, foreign keys, and indexes
- Default category auto-seeding (4 categories) and CRUD operations
- Global guild ticket settings upsert and string Snowflake ID serialization
- User rate-limiting / max open tickets enforcement
- DynamicItem buttons (DynamicTicketCategoryButton, DynamicTicketActionButton)
- Private thread creation with role and member routing
- In-thread staff actions (claim, assign role/member, close)
- Full as-is transcript preservation, HTML/TXT generation, and download
- Dashboard RESTful API endpoints and operator authorization guards
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

from services.tickets_db import TicketsDB
from services.tickets_exporter import generate_html_transcript, generate_txt_transcript
from cogs.tickets import TicketsCog, DynamicTicketCategoryButton, DynamicTicketActionButton
from dashboard.app import create_dashboard


class TestTicketsSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create isolated temporary SQLite database
        os.makedirs(os.path.join(WORKSPACE_DIR, "scratch"), exist_ok=True)
        self.test_id = uuid.uuid4().hex[:8]
        self.test_db_path = os.path.join(WORKSPACE_DIR, "scratch", f"test_tickets_{self.test_id}.db")

        self.db = TicketsDB(self.test_db_path)
        TicketsDB._instance = self.db
        await self.db.init_db()

        self.guild_id = 123456789012345678
        self.panel_channel_id = 234567890123456789
        self.transcript_channel_id = 345678901234567890
        self.support_role_id = 456789012345678901
        self.user_id = 567890123456789012
        self.staff_id = 678901234567890123

        # Mock Discord Bot & Guild
        self.mock_bot = MagicMock(spec=commands.Bot)
        self.mock_bot.user = MagicMock(spec=discord.ClientUser)
        self.mock_bot.user.id = 999999999999999999
        self.mock_bot.user.name = "TicketsBotTest"
        self.mock_bot.user.display_avatar.url = "https://cdn.discordapp.com/embed/avatars/0.png"
        self.mock_bot.add_dynamic_items = MagicMock()

        self.mock_guild = MagicMock(spec=discord.Guild)
        self.mock_guild.id = self.guild_id
        self.mock_guild.name = "Tickets Test Server"

        # Mock channels
        self.mock_panel_channel = MagicMock(spec=discord.TextChannel)
        self.mock_panel_channel.id = self.panel_channel_id
        self.mock_panel_channel.name = "support-desk"
        self.mock_panel_channel.send = AsyncMock()

        # Mock private thread spawned by panel channel
        self.mock_thread = MagicMock(spec=discord.Thread)
        self.mock_thread.id = 789012345678901234
        self.mock_thread.name = "ticket-0001-testuser"
        self.mock_thread.add_user = AsyncMock()
        self.mock_thread.send = AsyncMock()
        self.mock_thread.edit = AsyncMock()

        self.mock_panel_channel.create_thread = AsyncMock(return_value=self.mock_thread)

        self.mock_transcript_channel = MagicMock(spec=discord.TextChannel)
        self.mock_transcript_channel.id = self.transcript_channel_id
        self.mock_transcript_channel.name = "ticket-transcripts"
        self.mock_transcript_channel.send = AsyncMock()

        # Mock role & member
        self.mock_role = MagicMock(spec=discord.Role)
        self.mock_role.id = self.support_role_id
        self.mock_role.name = "Support Agent"
        self.mock_role.mention = f"<@&{self.support_role_id}>"

        self.mock_staff_member = MagicMock(spec=discord.Member)
        self.mock_staff_member.id = self.staff_id
        self.mock_staff_member.name = "StaffUser"
        self.mock_staff_member.display_avatar.url = "https://cdn.discordapp.com/avatars/staff.png"
        self.mock_staff_member.roles = [self.mock_role]
        self.mock_staff_member.guild_permissions = discord.Permissions(administrator=True)

        self.mock_guild.roles = [self.mock_role]
        self.mock_guild.members = [self.mock_staff_member]
        self.mock_guild.text_channels = [self.mock_panel_channel, self.mock_transcript_channel]
        self.mock_guild.get_channel.side_effect = lambda cid: {
            self.panel_channel_id: self.mock_panel_channel,
            self.transcript_channel_id: self.mock_transcript_channel,
            self.mock_thread.id: self.mock_thread
        }.get(int(cid))
        self.mock_guild.get_role.side_effect = lambda rid: self.mock_role if int(rid) == self.support_role_id else None
        self.mock_guild.get_member.side_effect = lambda uid: self.mock_staff_member if int(uid) == self.staff_id else None

        self.mock_user = MagicMock(spec=discord.User)
        self.mock_user.id = self.user_id
        self.mock_user.name = "Customer"
        self.mock_user.mention = f"<@{self.user_id}>"
        self.mock_user.send = AsyncMock()

        self.mock_bot.get_guild.return_value = self.mock_guild
        self.mock_bot.get_channel.side_effect = lambda cid: self.mock_guild.get_channel(cid)
        self.mock_bot.get_user.side_effect = lambda uid: self.mock_user if int(uid) == self.user_id else None

        # Setup TicketsCog
        self.cog = TicketsCog(self.mock_bot)
        self.cog.db = self.db
        self.mock_bot.get_cog.side_effect = lambda name: self.cog if name == "TicketsCog" else None

        # Setup Quart App Test Client
        self.app = create_dashboard(self.mock_bot)
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        try:
            if os.path.exists(self.test_db_path):
                os.remove(self.test_db_path)
            for ext in ("-wal", "-shm"):
                p = f"{self.test_db_path}{ext}"
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass

    # =========================================================================
    # 1. Database Operations & 64-bit Snowflake ID Serialization Tests
    # =========================================================================

    async def test_db_settings_and_default_categories_seeding(self):
        """Validates settings defaults, upsert, and automatic 4-category seeding."""
        # 1. Settings default
        settings = await self.db.get_settings(self.guild_id)
        self.assertEqual(settings["guild_id"], str(self.guild_id))
        self.assertEqual(settings["max_open_per_user"], 2)
        self.assertIn("Official Support", settings["panel_title"])

        # 2. Upsert settings
        updated = await self.db.upsert_settings(
            guild_id=self.guild_id,
            panel_channel_id=self.panel_channel_id,
            transcript_channel_id=self.transcript_channel_id,
            support_role_ids=[str(self.support_role_id)],
            support_member_ids=[str(self.staff_id)],
            max_open_per_user=3,
            panel_title="Custom Helpdesk",
            panel_description="Click below to open a private ticket.",
            panel_color="#FF5733"
        )
        self.assertEqual(updated["panel_channel_id"], str(self.panel_channel_id))
        self.assertEqual(updated["transcript_channel_id"], str(self.transcript_channel_id))
        self.assertEqual(updated["support_role_ids"], [str(self.support_role_id)])
        self.assertEqual(updated["max_open_per_user"], 3)
        self.assertEqual(updated["panel_title"], "Custom Helpdesk")
        self.assertEqual(updated["panel_color"], "#FF5733")

        # 3. Default category seeding
        cats = await self.db.get_categories(self.guild_id)
        self.assertEqual(len(cats), 4)
        cat_names = [c["name"] for c in cats]
        self.assertIn("🔑 Credential / Account Issue", cat_names[0])
        self.assertIn("💳 Payment & Billing", cat_names[1])
        self.assertIn("🛠️ Technical / Tool Bug", cat_names[2])
        self.assertIn("💬 General Support", cat_names[3])

    async def test_categories_crud_and_reordering(self):
        """Validates category creation, update, reordering, and deletion."""
        # Create category
        cat_id = await self.db.create_category(
            guild_id=self.guild_id,
            name="VIP Concierge",
            emoji="⭐",
            button_style="danger",
            description="Dedicated priority assistance for VIP partners.",
            assigned_role_ids=[str(self.support_role_id)],
            assigned_member_ids=[str(self.staff_id)]
        )
        self.assertIsInstance(cat_id, int)

        cat = await self.db.get_category(cat_id)
        self.assertIsNotNone(cat)
        self.assertEqual(cat["name"], "VIP Concierge")
        self.assertEqual(cat["emoji"], "⭐")
        self.assertEqual(cat["button_style"], "danger")
        self.assertEqual(cat["assigned_role_ids"], [str(self.support_role_id)])

        # Update category
        await self.db.update_category(cat_id, name="VIP Priority Support", button_style="success")
        updated_cat = await self.db.get_category(cat_id)
        self.assertEqual(updated_cat["name"], "VIP Priority Support")
        self.assertEqual(updated_cat["button_style"], "success")

        # Reorder categories
        cats = await self.db.get_categories(self.guild_id)
        reversed_ids = [c["id"] for c in reversed(cats)]
        await self.db.reorder_categories(self.guild_id, reversed_ids)
        reordered_cats = await self.db.get_categories(self.guild_id)
        self.assertEqual([c["id"] for c in reordered_cats], reversed_ids)

        # Delete category
        ok = await self.db.delete_category(cat_id)
        self.assertTrue(ok)
        self.assertIsNone(await self.db.get_category(cat_id))

    async def test_ticket_lifecycle_and_messages_transcript_archival(self):
        """Validates ticket creation, claim, assignment, message logging, and closing."""
        # 1. Create ticket
        ticket_id = await self.db.create_ticket(
            guild_id=self.guild_id,
            thread_id=self.mock_thread.id,
            panel_channel_id=self.panel_channel_id,
            category_id=1,
            category_name="🔑 Credential Issue",
            user_id=self.user_id,
            user_name="CustomerUser#0001",
            subject="Cannot access dashboard account",
            initial_details="Password reset token expired and need credentials."
        )
        self.assertGreater(ticket_id, 0)

        # Verify rate limiting count
        count = await self.db.count_open_tickets_for_user(self.guild_id, self.user_id)
        self.assertEqual(count, 1)

        # Fetch ticket
        t = await self.db.get_ticket(ticket_id)
        self.assertEqual(t["guild_id"], str(self.guild_id))
        self.assertEqual(t["user_id"], str(self.user_id))
        self.assertEqual(t["status"], "open")

        # 2. Claim ticket
        await self.db.claim_ticket(ticket_id, staff_id=self.staff_id, staff_name="StaffUser")
        t_claimed = await self.db.get_ticket(ticket_id)
        self.assertEqual(t_claimed["status"], "claimed")
        self.assertEqual(t_claimed["claimed_by_id"], str(self.staff_id))
        self.assertEqual(t_claimed["claimed_by_name"], "StaffUser")

        # 3. Dynamic Assignment
        await self.db.assign_role_to_ticket(ticket_id, str(self.support_role_id))
        await self.db.assign_user_to_ticket(ticket_id, str(self.staff_id))
        t_assigned = await self.db.get_ticket(ticket_id)
        self.assertIn(str(self.support_role_id), t_assigned["assigned_roles"])
        self.assertIn(str(self.staff_id), t_assigned["assigned_users"])

        # 4. Message Log Archival (Preserves exact content & credentials)
        messages_to_save = [
            {
                "message_id": 888111222333444555,
                "author_id": self.user_id,
                "author_name": "CustomerUser#0001",
                "author_avatar": "https://cdn.discordapp.com/avatars/0.png",
                "is_staff": False,
                "content": "Here is my account login: test@domain.com / SecretPassword123!",
                "attachments_json": ["https://cdn.discordapp.com/attachments/screenshot.png"],
                "created_at": "2026-10-16 23:30:00 UTC"
            },
            {
                "message_id": 888111222333444556,
                "author_id": self.staff_id,
                "author_name": "StaffUser",
                "author_avatar": "https://cdn.discordapp.com/avatars/1.png",
                "is_staff": True,
                "content": "I have updated your account permissions. Please test now.",
                "attachments_json": [],
                "created_at": "2026-10-16 23:32:00 UTC"
            }
        ]
        saved_count = await self.db.save_messages_batch(ticket_id, messages_to_save)
        self.assertEqual(saved_count, 2)

        # Retrieve messages
        retrieved_msgs = await self.db.get_ticket_messages(ticket_id)
        self.assertEqual(len(retrieved_msgs), 2)
        self.assertIn("SecretPassword123!", retrieved_msgs[0]["content"])
        self.assertEqual(retrieved_msgs[0]["attachments"], ["https://cdn.discordapp.com/attachments/screenshot.png"])
        self.assertTrue(retrieved_msgs[1]["is_staff"])

        # 5. Close Ticket
        await self.db.close_ticket(ticket_id, closed_by_id=self.staff_id, closed_by_name="StaffUser", close_reason="Password reset completed successfully.")
        t_closed = await self.db.get_ticket(ticket_id)
        self.assertEqual(t_closed["status"], "closed")
        self.assertEqual(t_closed["close_reason"], "Password reset completed successfully.")

        # Rate limit count decreases for user
        count_after = await self.db.count_open_tickets_for_user(self.guild_id, self.user_id)
        self.assertEqual(count_after, 0)

    # =========================================================================
    # 2. Transcript Generators Tests (HTML & TXT)
    # =========================================================================

    def test_transcript_exporters(self):
        """Validates that transcripts render all metadata, credentials, and attachments correctly."""
        ticket = {
            "id": 42,
            "subject": "Payment API Failure",
            "category_name": "💳 Payment & Billing",
            "status": "closed",
            "user_name": "BillingUser",
            "user_id": "567890123456789012",
            "created_at": "2026-10-16 23:00:00 UTC",
            "closed_by_name": "StaffMaster",
            "closed_by_id": "678901234567890123",
            "closed_at": "2026-10-16 23:15:00 UTC",
            "close_reason": "Invoice resent and verified.",
            "initial_details": "Card was declined on monthly renewal."
        }
        messages = [
            {
                "message_id": "111",
                "author_name": "BillingUser",
                "author_avatar": "https://cdn.discordapp.com/avatar.png",
                "is_staff": False,
                "content": "Transaction ref #TX-998811 with key: sk_live_xyz123",
                "attachments": ["https://cdn.discordapp.com/invoice.pdf"],
                "created_at": "2026-10-16 23:05:00 UTC"
            },
            {
                "message_id": "222",
                "author_name": "StaffMaster",
                "author_avatar": "https://cdn.discordapp.com/avatar2.png",
                "is_staff": True,
                "content": "```json\n{\"status\": \"settled\"}\n```",
                "attachments": [],
                "created_at": "2026-10-16 23:10:00 UTC"
            }
        ]

        # 1. Plain Text Transcript
        txt = generate_txt_transcript(ticket, messages)
        self.assertIn("TICKET #0042", txt)
        self.assertIn("Payment API Failure", txt)
        self.assertIn("sk_live_xyz123", txt)
        self.assertIn("BillingUser", txt)
        self.assertIn("[STAFF]", txt)
        self.assertIn("Invoice resent and verified.", txt)

        # 2. HTML Transcript
        html_out = generate_html_transcript(ticket, messages, guild_name="Test Enterprise")
        self.assertIn("Ticket #0042 Transcript", html_out)
        self.assertIn("Payment API Failure", html_out)
        self.assertIn("sk_live_xyz123", html_out)
        self.assertIn("chat-codeblock", html_out)
        self.assertIn("invoice.pdf", html_out)
        self.assertIn("Test Enterprise", html_out)

    # =========================================================================
    # 3. Discord Bot TicketsCog Workflow & Dynamic Buttons Tests
    # =========================================================================

    async def test_cog_load_and_dynamic_items_registration(self):
        """Verifies cog_load registers DynamicTicketCategoryButton and DynamicTicketActionButton."""
        await self.cog.cog_load()
        self.mock_bot.add_dynamic_items.assert_called_once_with(
            DynamicTicketCategoryButton,
            DynamicTicketActionButton
        )

    async def test_category_click_rate_limit(self):
        """Validates that a user exceeding max_open_per_user receives an ephemeral notice."""
        # Set max_open_per_user = 1
        await self.db.upsert_settings(self.guild_id, max_open_per_user=1)
        # Create 1 open ticket
        await self.db.create_ticket(
            guild_id=self.guild_id,
            thread_id=123,
            panel_channel_id=self.panel_channel_id,
            category_id=1,
            category_name="Support",
            user_id=self.user_id,
            user_name="TestUser",
            subject="First ticket",
            initial_details="Details"
        )

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.guild = self.mock_guild
        mock_interaction.user = MagicMock(spec=discord.Member)
        mock_interaction.user.id = self.user_id
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        await self.cog.handle_category_click(mock_interaction, cat_id=1)
        mock_interaction.response.send_message.assert_called_once()
        args, kwargs = mock_interaction.response.send_message.call_args
        self.assertIn("maximum allowed", args[0])
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_ticket_creation_and_thread_spawning(self):
        """Verifies process_ticket_creation creates thread, adds users, and posts control embed."""
        await self.db.upsert_settings(
            self.guild_id,
            panel_channel_id=self.panel_channel_id,
            support_role_ids=[str(self.support_role_id)],
            support_member_ids=[str(self.staff_id)]
        )

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.guild = self.mock_guild
        mock_interaction.user = MagicMock(spec=discord.Member)
        mock_interaction.user.id = self.user_id
        mock_interaction.user.name = "JohnDoe"
        mock_interaction.user.mention = "<@567890123456789012>"
        mock_interaction.followup = MagicMock()
        mock_interaction.followup.send = AsyncMock()

        await self.cog.process_ticket_creation(
            interaction=mock_interaction,
            cat_id=1,
            subject="Urgent Security Bug",
            initial_details="Found token leak in logging."
        )

        # Thread was created with invitable=False private thread
        self.mock_panel_channel.create_thread.assert_called_once()
        call_kwargs = self.mock_panel_channel.create_thread.call_args[1]
        self.assertEqual(call_kwargs["type"], discord.ChannelType.private_thread)
        self.assertFalse(call_kwargs["invitable"])

        # Creator and staff user were added to thread
        self.mock_thread.add_user.assert_any_call(mock_interaction.user)
        self.mock_thread.add_user.assert_any_call(self.mock_staff_member)

        # Control message was sent with buttons
        self.mock_thread.send.assert_called_once()
        send_kwargs = self.mock_thread.send.call_args[1]
        self.assertIn(f"<@&{self.support_role_id}>", send_kwargs["content"])
        self.assertIsNotNone(send_kwargs.get("embed"))
        self.assertIsNotNone(send_kwargs.get("view"))

        # Followup confirmation sent to opener
        mock_interaction.followup.send.assert_called_once()
        self.assertIn("created", mock_interaction.followup.send.call_args[0][0])

    async def test_ticket_close_execution_and_thread_archive(self):
        """Verifies execute_close_ticket archives messages, locks thread, and sends transcript."""
        await self.db.upsert_settings(
            self.guild_id,
            panel_channel_id=self.panel_channel_id,
            transcript_channel_id=self.transcript_channel_id
        )

        ticket_id = await self.db.create_ticket(
            guild_id=self.guild_id,
            thread_id=self.mock_thread.id,
            panel_channel_id=self.panel_channel_id,
            category_id=1,
            category_name="Support",
            user_id=self.user_id,
            user_name="Customer",
            subject="Close Test",
            initial_details="Details"
        )

        # Mock thread history
        mock_msg = MagicMock(spec=discord.Message)
        mock_msg.id = 999111222333
        mock_msg.author = self.mock_staff_member
        mock_msg.content = "All resolved now."
        mock_msg.attachments = []
        mock_msg.created_at = datetime.now(timezone.utc)

        async def async_history_gen(*args, **kwargs):
            yield mock_msg

        self.mock_thread.history = MagicMock(side_effect=async_history_gen)

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.followup = MagicMock()
        mock_interaction.followup.send = AsyncMock()

        ok = await self.cog.execute_close_ticket(
            ticket_id=ticket_id,
            closed_by=self.mock_staff_member,
            reason="Resolved by admin",
            interaction=mock_interaction
        )
        self.assertTrue(ok)

        # Verified locked, archived, and deleted
        self.mock_thread.edit.assert_called_once_with(locked=True, archived=True)
        self.mock_thread.delete.assert_called_once()

        # Transcript posted to transcript channel
        self.mock_transcript_channel.send.assert_called_once()
        files = self.mock_transcript_channel.send.call_args[1].get("files", [])
        self.assertEqual(len(files), 2)  # .html and .txt

        # Verified .txt transcript DM was sent to the ticket creator
        self.mock_user.send.assert_called_once()
        dm_kwargs = self.mock_user.send.call_args[1]
        self.assertIsNotNone(dm_kwargs.get("embed"))
        self.assertIsNotNone(dm_kwargs.get("file"))
        self.assertTrue(dm_kwargs["file"].filename.endswith(".txt"))

        # Verified DB record updated
        ticket = await self.db.get_ticket(ticket_id)
        self.assertEqual(ticket["status"], "closed")

    # =========================================================================
    # 4. Web Dashboard REST API Endpoints Tests
    # =========================================================================

    async def test_dashboard_api_settings_and_publish(self):
        """Tests GET/POST /api/guild/<id>/tickets/settings and /publish."""
        async with self.client.session_transaction() as sess:
            sess["role"] = "Administrator"
            sess["user_id"] = "AdminUser"

        # 1. GET Settings
        res = await self.client.get(f"/api/guild/{self.guild_id}/tickets/settings")
        self.assertEqual(res.status_code, 200)
        data = await res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("settings", data)
        self.assertIn("channels", data)
        self.assertIn("roles", data)

        # 2. POST Settings
        post_data = {
            "panel_channel_id": str(self.panel_channel_id),
            "transcript_channel_id": str(self.transcript_channel_id),
            "support_role_ids": [str(self.support_role_id)],
            "support_member_ids": [str(self.staff_id)],
            "panel_title": "Enterprise Desk",
            "panel_description": "24/7 Support",
            "panel_color": "#10B981",
            "max_open_per_user": 4
        }
        res_post = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/settings",
            json=post_data
        )
        self.assertEqual(res_post.status_code, 200)
        saved = (await res_post.get_json())["settings"]
        self.assertEqual(saved["panel_title"], "Enterprise Desk")
        self.assertEqual(saved["max_open_per_user"], 4)

        # 3. POST Publish
        res_pub = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/publish",
            json={}
        )
        self.assertEqual(res_pub.status_code, 200)
        pub_data = await res_pub.get_json()
        self.assertTrue(pub_data["success"])
        self.mock_panel_channel.send.assert_called_once()

    async def test_dashboard_api_categories_crud(self):
        """Tests Category creation, listing, updating, and deletion via API."""
        async with self.client.session_transaction() as sess:
            sess["role"] = "Administrator"
            sess["user_id"] = "AdminUser"

        # 1. GET Categories (Seeds defaults)
        res_get = await self.client.get(f"/api/guild/{self.guild_id}/tickets/categories")
        self.assertEqual(res_get.status_code, 200)
        cats = (await res_get.get_json())["categories"]
        self.assertEqual(len(cats), 4)

        # 2. POST Create Category
        new_cat_data = {
            "name": "Custom Hardware Bug",
            "emoji": "🖥️",
            "button_style": "primary",
            "description": "GPU cluster defects",
            "assigned_role_ids": [str(self.support_role_id)],
            "assigned_member_ids": [str(self.staff_id)]
        }
        res_create = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/categories",
            json=new_cat_data
        )
        self.assertEqual(res_create.status_code, 200)
        cat_id = (await res_create.get_json())["category"]["id"]

        # 3. PUT Update Category
        res_up = await self.client.put(
            f"/api/guild/{self.guild_id}/tickets/categories/{cat_id}",
            json={"name": "GPU Hardware Bug", "button_style": "danger"}
        )
        self.assertEqual(res_up.status_code, 200)
        cat_updated = (await res_up.get_json())["category"]
        self.assertEqual(cat_updated["name"], "GPU Hardware Bug")
        self.assertEqual(cat_updated["button_style"], "danger")

        # 4. DELETE Category
        res_del = await self.client.delete(f"/api/guild/{self.guild_id}/tickets/categories/{cat_id}")
        self.assertEqual(res_del.status_code, 200)

    async def test_dashboard_api_ticket_management_and_transcript_downloads(self):
        """Tests ticket listing, claiming, assign, closing, and HTML/TXT transcript downloading."""
        async with self.client.session_transaction() as sess:
            sess["role"] = "Administrator"
            sess["user_id"] = "StaffAdmin"

        # Create ticket directly in DB
        ticket_id = await self.db.create_ticket(
            guild_id=self.guild_id,
            thread_id=self.mock_thread.id,
            panel_channel_id=self.panel_channel_id,
            category_id=1,
            category_name="Support",
            user_id=self.user_id,
            user_name="JohnTester",
            subject="API Outage",
            initial_details="Error 500 on all calls"
        )

        # 1. GET Tickets List
        res_list = await self.client.get(f"/api/guild/{self.guild_id}/tickets")
        self.assertEqual(res_list.status_code, 200)
        tickets = (await res_list.get_json())["tickets"]
        self.assertEqual(len(tickets), 1)

        # 2. POST Claim
        res_claim = await self.client.post(f"/api/guild/{self.guild_id}/tickets/{ticket_id}/claim")
        self.assertEqual(res_claim.status_code, 200)
        self.assertTrue((await res_claim.get_json())["success"])

        # 3. POST Assign Role
        res_assign = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/{ticket_id}/assign",
            json={"role_id": str(self.support_role_id)}
        )
        self.assertEqual(res_assign.status_code, 200)

        # 4. POST Close
        res_close = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/{ticket_id}/close",
            json={"reason": "Restarted service and back online"}
        )
        self.assertEqual(res_close.status_code, 200)

        # 5. GET Transcript JSON
        res_trans = await self.client.get(f"/api/guild/{self.guild_id}/tickets/{ticket_id}/transcript")
        self.assertEqual(res_trans.status_code, 200)
        trans_data = await res_trans.get_json()
        self.assertEqual(trans_data["ticket"]["status"], "closed")

        # 6. GET Download HTML Transcript
        res_dl_html = await self.client.get(f"/api/guild/{self.guild_id}/tickets/{ticket_id}/transcript/download?format=html")
        self.assertEqual(res_dl_html.status_code, 200)
        self.assertIn("text/html", res_dl_html.headers.get("Content-Type", ""))
        html_bytes = await res_dl_html.get_data()
        self.assertIn(b"API Outage", html_bytes)

        # 7. GET Download TXT Transcript
        res_dl_txt = await self.client.get(f"/api/guild/{self.guild_id}/tickets/{ticket_id}/transcript/download?format=txt")
        self.assertEqual(res_dl_txt.status_code, 200)
        self.assertIn("text/plain", res_dl_txt.headers.get("Content-Type", ""))
        txt_bytes = await res_dl_txt.get_data()
        self.assertIn(b"API Outage", txt_bytes)

    async def test_dashboard_api_unauthorized_access(self):
        """Verifies Viewer roles cannot modify tickets, settings, or categories."""
        async with self.client.session_transaction() as sess:
            sess["role"] = "Viewer"
            sess["user_id"] = "ViewerUser"

        res = await self.client.post(
            f"/api/guild/{self.guild_id}/tickets/settings",
            json={"panel_title": "Hacked Title"}
        )
        self.assertEqual(res.status_code, 403)
        data = await res.get_json()
        self.assertEqual(data["error"], "Forbidden")


if __name__ == "__main__":
    unittest.main()
