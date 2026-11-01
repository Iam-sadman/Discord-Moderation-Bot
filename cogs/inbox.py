"""
Direct Messages Inbox Cog for Discord.py 2.x.
Features:
- DynamicItem persistent Reply button on staff channel relay messages (survives VPS restarts)
- Interactive Reply Modal for staff to DM members directly from Discord
- Native Discord quote-reply support: replying to the bot's relayed alert automatically sends DM to the member
- Automatic persistence in data/inbox.db synced with Web Dashboard
- Automated auto-responder with rate-limiting / spam protection
- Slash commands suite under /dm
"""

import os
import re
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from services.inbox_db import InboxDB, DEFAULT_RELAY_CHANNEL_ID

logger = logging.getLogger("InboxCog")

AUTO_REPLY_COOLDOWN_SECONDS = 600  # 10 minutes per user


# =============================================================================
# Dynamic Persistent Items across Bot & VPS Restarts
# =============================================================================

class DynamicInboxReplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"inbox:reply:(?P<user_id>\d+)"):
    """
    Persistent Dynamic Button for staff channel relay messages.
    Pattern: inbox:reply:<user_id>
    Survives bot restarts without needing active view memory.
    """
    def __init__(self, user_id: int):
        super().__init__(
            discord.ui.Button(
                label="💬 Reply via DM",
                style=discord.ButtonStyle.primary,
                emoji="💬",
                custom_id=f"inbox:reply:{user_id}"
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        user_id = int(match.group("user_id"))
        return cls(user_id=user_id)

    async def callback(self, interaction: discord.Interaction):
        # Check permissions: Administrator, Manage Server, or Moderator
        perms = interaction.user.guild_permissions if interaction.guild else None
        is_mod = False
        if perms:
            is_mod = perms.administrator or perms.manage_guild or perms.manage_messages

        if not is_mod:
            await interaction.response.send_message(
                "❌ You do not have staff permissions to reply to direct messages.",
                ephemeral=True
            )
            return

        cog = interaction.client.get_cog("InboxCog")
        if not cog:
            await interaction.response.send_message("❌ Inbox service unavailable.", ephemeral=True)
            return

        modal = StaffInboxReplyModal(self.user_id, cog)
        await interaction.response.send_modal(modal)


class StaffInboxReplyModal(discord.ui.Modal):
    """Interactive modal allowing staff to type a DM response directly from Discord."""
    reply_text = discord.ui.TextInput(
        label="Direct Message Content",
        style=discord.TextStyle.paragraph,
        placeholder="Type the message you want the bot to send to this member...",
        max_length=2000,
        required=True
    )

    def __init__(self, target_user_id: int, cog: "InboxCog"):
        super().__init__(title="Send Staff DM Reply")
        self.target_user_id = target_user_id
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        content = self.reply_text.value.strip()

        success, err = await self.cog.send_staff_reply(
            target_user_id=self.target_user_id,
            content=content,
            staff_name=interaction.user.name,
            staff_id=interaction.user.id,
            origin="staff_discord",
            interaction=interaction
        )

        if success:
            await interaction.followup.send(
                f"✅ Staff DM sent to <@{self.target_user_id}> successfully.",
                ephemeral=True
            )
            # Post notice in channel
            if interaction.channel and hasattr(interaction.channel, "send"):
                try:
                    await interaction.channel.send(
                        content=f"💬 **{interaction.user.mention}** replied to <@{self.target_user_id}> via DM:\n> {content[:250]}",
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                except Exception:
                    pass
        else:
            await interaction.followup.send(
                f"⚠️ Could not deliver DM: {err}",
                ephemeral=True
            )


# =============================================================================
# Main Inbox Cog
# =============================================================================

class InboxCog(commands.Cog):
    """Production-grade Direct Messages Inbox & Two-Way Relay Cog."""

    dm_group = app_commands.Group(
        name="dm",
        description="Official bot direct message and inbox commands"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = InboxDB.get_instance()
        self._auto_reply_cooldown: Dict[int, float] = {}

    async def cog_load(self):
        """Initializes database schema and registers dynamic UI buttons."""
        await self.db.init_db()
        self.bot.add_dynamic_items(DynamicInboxReplyButton)
        logger.info("[OK] InboxCog dynamic items and database registered")

    # =========================================================================
    # Delivery Engine
    # =========================================================================

    async def send_staff_reply(
        self,
        target_user_id: int,
        content: str,
        staff_name: str,
        staff_id: int,
        origin: str = "staff_discord",
        interaction: Optional[discord.Interaction] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Delivers an official DM to a Discord member, saves it to the database,
        and synchronizes with the conversation thread.
        """
        if not content:
            return False, "Message content cannot be empty."

        target_user = self.bot.get_user(target_user_id)
        if not target_user:
            try:
                target_user = await self.bot.fetch_user(target_user_id)
            except discord.NotFound:
                return False, f"User with ID `{target_user_id}` not found on Discord."
            except Exception as e:
                return False, f"Discord error fetching user: {e}"

        # Build clean DM Embed
        embed = discord.Embed(
            title="💬 Official Server Staff Message",
            description=content,
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Sent by Staff: {staff_name}")

        try:
            sent_msg = await target_user.send(embed=embed)
        except discord.Forbidden:
            return False, "User has direct messages closed or has blocked the bot."
        except Exception as e:
            return False, f"Discord failed to deliver DM: {e}"

        # Retrieve or create conversation record
        avatar_url = ""
        if hasattr(target_user, "display_avatar") and target_user.display_avatar:
            avatar_url = str(getattr(target_user.display_avatar, "url", ""))
        username = str(getattr(target_user, "name", f"User_{target_user_id}"))
        display_name = str(getattr(target_user, "display_name", username))

        conv = await self.db.get_or_create_conversation(
            user_id=target_user_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url
        )

        # Store outgoing message in database
        await self.db.add_message(
            conversation_id=conv["id"],
            sender_type=origin,
            author_id=staff_id,
            author_name=staff_name,
            author_avatar="",
            content=content,
            discord_message_id=sent_msg.id,
            is_read=1
        )

        return True, None

    # =========================================================================
    # Event Listeners
    # =========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Processes incoming DMs and native channel replies."""
        if message.author.bot:
            return

        # ---------------------------------------------------------------------
        # CASE 1: Member DM sent to Bot (message.guild is None)
        # ---------------------------------------------------------------------
        if message.guild is None:
            await self._handle_incoming_dm(message)
            return

        # ---------------------------------------------------------------------
        # CASE 2: Staff Native Quote-Reply in Staff Relay Channel
        # ---------------------------------------------------------------------
        if message.reference and message.reference.message_id:
            await self._handle_staff_native_reply(message)

    async def _handle_incoming_dm(self, message: discord.Message):
        """Handles an incoming user direct message."""
        author = message.author
        user_id = author.id
        username = author.name
        display_name = getattr(author, "display_name", username)
        avatar_url = author.display_avatar.url if author.display_avatar else ""

        # Extract attachments
        attachments = []
        for att in message.attachments:
            attachments.append({
                "url": att.url,
                "filename": att.filename,
                "content_type": getattr(att, "content_type", "") or "application/octet-stream",
                "size": att.size
            })

        # Find first mutual guild for default settings
        target_guild = None
        for g in self.bot.guilds:
            if g.get_member(user_id):
                target_guild = g
                break
        if not target_guild and self.bot.guilds:
            target_guild = self.bot.guilds[0]

        guild_id = target_guild.id if target_guild else None

        # Store or update conversation
        conv = await self.db.get_or_create_conversation(
            user_id=user_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
            guild_id=guild_id
        )

        # Store incoming message
        await self.db.add_message(
            conversation_id=conv["id"],
            sender_type="user",
            author_id=user_id,
            author_name=username,
            author_avatar=avatar_url,
            content=message.content or "",
            attachments=attachments,
            discord_message_id=message.id,
            is_read=0
        )

        logger.info(f"[INBOX] Received DM from {username} ({user_id}): '{message.content[:60]}'")

        # Fetch settings for relay and auto-reply
        settings = await self.db.get_settings(guild_id) if guild_id else {
            "auto_reply_enabled": 1,
            "auto_reply_text": "Thank you for your message! Our team has received it on our dashboard and staff channel. We will get back to you shortly.",
            "notify_channel_id": DEFAULT_RELAY_CHANNEL_ID
        }

        # Auto-reply with cooldown
        if settings.get("auto_reply_enabled"):
            now = time.time()
            last_reply = self._auto_reply_cooldown.get(user_id, 0)
            if now - last_reply > AUTO_REPLY_COOLDOWN_SECONDS:
                reply_text = settings.get("auto_reply_text") or "Thank you for reaching out! We will reply soon."
                try:
                    sent_auto = await author.send(
                        f"🤖 **Automated Notice:**\n{reply_text}"
                    )
                    self._auto_reply_cooldown[user_id] = now
                    await self.db.add_message(
                        conversation_id=conv["id"],
                        sender_type="bot_auto",
                        author_id=self.bot.user.id if self.bot.user else 0,
                        author_name=self.bot.user.name if self.bot.user else "Bot Auto-Reply",
                        author_avatar="",
                        content=reply_text,
                        discord_message_id=sent_auto.id,
                        is_read=1
                    )
                except Exception as e:
                    logger.warning(f"Could not deliver auto-reply to {user_id}: {e}")

        # Relay to Discord channel (e.g. 1566408437603237898)
        relay_channel_id = settings.get("notify_channel_id") or DEFAULT_RELAY_CHANNEL_ID
        if relay_channel_id:
            await self._relay_to_channel(relay_channel_id, author, message.content, attachments, conv["id"])

    async def _relay_to_channel(
        self,
        channel_id: int,
        author: discord.User | discord.Member,
        content: str,
        attachments: List[Dict[str, Any]],
        conv_id: int
    ):
        """Constructs and sends the relay embed with persistent reply button into staff channel."""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"Failed to fetch relay channel {channel_id}: {e}")
                return

        embed = discord.Embed(
            title="📥 New Member Direct Message",
            description=content if content else "*[No text content — file attachment only]*",
            color=0x2B2D31,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(
            name=f"{author.name} ({getattr(author, 'display_name', author.name)})",
            icon_url=author.display_avatar.url if author.display_avatar else None
        )
        embed.add_field(name="User ID", value=f"`{author.id}`", inline=True)
        embed.add_field(name="Thread Status", value="🟢 Open", inline=True)

        if attachments:
            att_links = []
            for idx, a in enumerate(attachments, 1):
                att_links.append(f"[{a.get('filename', f'File {idx}')}]({a.get('url')})")
            embed.add_field(name="Attachments", value="\n".join(att_links), inline=False)
            # If primary attachment is an image, set image
            first_url = attachments[0].get("url", "")
            if any(first_url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                embed.set_image(url=first_url)

        embed.set_footer(text=f"Direct Messages Inbox • User ID: {author.id}")

        view = discord.ui.View(timeout=None)
        view.add_item(DynamicInboxReplyButton(user_id=author.id))

        try:
            relayed_msg = await channel.send(embed=embed, view=view)
            await self.db.update_last_relay_message_id(conv_id, relayed_msg.id)
        except Exception as e:
            logger.error(f"Failed to post relay message to channel {channel_id}: {e}")

    async def _handle_staff_native_reply(self, message: discord.Message):
        """Detects if staff replied to a bot relay message and routes it back as a DM."""
        ref_msg_id = message.reference.message_id
        if not ref_msg_id:
            return

        try:
            ref_msg = await message.channel.fetch_message(ref_msg_id)
        except Exception:
            return

        # Check if the referenced message was sent by the bot and contains an embed with footer User ID
        if not ref_msg or ref_msg.author.id != self.bot.user.id:
            return

        target_user_id = None
        if ref_msg.embeds:
            footer_text = getattr(ref_msg.embeds[0].footer, "text", "") or ""
            match = re.search(r"User ID:\s*(\d+)", footer_text)
            if match:
                target_user_id = int(match.group(1))

        if not target_user_id:
            return

        # Deliver staff reply
        content = message.content.strip()
        if not content:
            return

        success, err = await self.send_staff_reply(
            target_user_id=target_user_id,
            content=content,
            staff_name=message.author.name,
            staff_id=message.author.id,
            origin="staff_discord"
        )

        if success:
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
        else:
            try:
                await message.reply(f"⚠️ Could not deliver DM to user: {err}", mention_author=False)
            except Exception:
                pass

    # =========================================================================
    # Slash Commands (/dm ...)
    # =========================================================================

    @dm_group.command(name="send", description="Send an official staff direct message to a member")
    @app_commands.describe(
        user="The Discord member to receive the DM",
        message="The message to send"
    )
    async def cmd_dm_send(self, interaction: discord.Interaction, user: discord.User, message: str):
        # Operator permission check
        perms = interaction.user.guild_permissions if interaction.guild else None
        if perms and not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await interaction.response.send_message("❌ Staff privileges required.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        success, err = await self.send_staff_reply(
            target_user_id=user.id,
            content=message,
            staff_name=interaction.user.name,
            staff_id=interaction.user.id,
            origin="staff_discord",
            interaction=interaction
        )

        if success:
            await interaction.followup.send(f"✅ DM delivered to {user.mention}.", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ Delivery failed: {err}", ephemeral=True)

    @dm_group.command(name="status", description="Check Direct Messages Inbox statistics")
    async def cmd_dm_status(self, interaction: discord.Interaction):
        unread_count = await self.db.get_total_unread_count()
        convs, total = await self.db.list_conversations(limit=1)

        embed = discord.Embed(
            title="📥 Direct Messages Inbox Status",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Total Conversation Threads", value=str(total), inline=True)
        embed.add_field(name="Unread Threads", value=str(unread_count), inline=True)
        embed.add_field(name="Relay Channel", value=f"<#{DEFAULT_RELAY_CHANNEL_ID}>", inline=False)
        embed.set_footer(text="Manage all incoming DMs on the Web Management Dashboard")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cog setup hook for ModularBot."""
    await bot.add_cog(InboxCog(bot))
