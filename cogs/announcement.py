"""Role-restricted announcement composer with a private preview and delivery flow."""

import os
import re
import mimetypes
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands
from config import Config


logger = logging.getLogger(__name__)


def _env_allowed_role_ids() -> set[int]:
    """Read fallback configured role IDs from .env, ignoring malformed entries safely."""
    role_ids: set[int] = set()
    raw_ids = os.getenv("ANNOUNCEMENT_ALLOWED_ROLE_IDS", "")
    for value in raw_ids.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            role_ids.add(int(value))
        except ValueError:
            continue
    return role_ids


async def _is_allowed(member: discord.Member) -> bool:
    """
    Check if a member is allowed to create or edit announcements:
    1. Server Administrators and members with Manage Server permission are always allowed.
    2. Check dynamic cog permissions configured in web dashboard or /announcement_roles.
    3. Fallback to ANNOUNCEMENT_ALLOWED_ROLE_IDS from .env if no custom roles are set.
    """
    if getattr(member, "guild_permissions", None) and (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
        return True

    allowed = await Config.check_member_cog_permission(member, "announcement")
    if allowed:
        return True

    env_roles = _env_allowed_role_ids()
    if env_roles:
        return any(role.id in env_roles for role in getattr(member, "roles", []))

    return False


def _styled(text: str, styles: set[str]) -> str:
    """Apply Discord Markdown wrappers in a stable order."""
    if not text:
        return text
    opening = ""
    closing = ""
    for style, marker in (("bold", "**"), ("italic", "*"), ("underline", "__")):
        if style in styles:
            opening += marker
            closing = marker + closing
    return f"{opening}{text}{closing}"


def _youtube_preview_urls(text: str) -> list[str]:
    """Return YouTube URLs so Discord can generate native previews."""
    matches = re.finditer(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s<>()]+",
        text,
        flags=re.IGNORECASE,
    )
    return list(dict.fromkeys(match.group(0).rstrip(".,!?;:") for match in matches))


def _bounded_lines(lines: list[str], *, limit: int = 1900) -> Optional[str]:
    """Join unique complete lines without exceeding a Discord message limit."""
    result: list[str] = []
    used = 0
    for line in dict.fromkeys(value for value in lines if value):
        extra = len(line) + (1 if result else 0)
        if used + extra > limit:
            continue
        result.append(line)
        used += extra
    return "\n".join(result) or None


@dataclass
class AnnouncementLink:
    url: str
    label: str


def _parse_links(raw_links: str) -> tuple[list[AnnouncementLink], Optional[str]]:
    """Parse one URL per line, optionally written as `label | URL`."""
    links: list[AnnouncementLink] = []
    seen_urls: set[str] = set()
    for line_number, raw_line in enumerate(raw_links.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            label, url = (part.strip() for part in line.split("|", 1))
        else:
            label, url = f"Open link {len(links) + 1}", line
        if any(character.isspace() for character in url):
            return [], f"The URL on line {line_number} cannot contain spaces."
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return [], f"Line {line_number} is not a full `https://` or `http://` URL."
        if len(url) > 900:
            return [], f"The URL on line {line_number} is too long (maximum 900 characters)."
        if len(label) > 80:
            return [], f"The label on line {line_number} must be 80 characters or fewer."
        normalized_url = url.casefold()
        if normalized_url not in seen_urls:
            links.append(AnnouncementLink(url=url, label=label or f"Open link {len(links) + 1}"))
            seen_urls.add(normalized_url)
        if len(links) > 10:
            return [], "You can add up to 10 links."
    return links, None


def _roles_mentioned_in_text(guild: discord.Guild, texts: list[str]) -> list[discord.Role]:
    """Resolve literal @role-name text to roles that should be pinged."""
    combined_text = "\n".join(texts)
    matched_roles: list[discord.Role] = []
    for role in sorted(guild.roles, key=lambda item: len(item.name), reverse=True):
        if role.is_default():
            continue
        role_pattern = rf"(?<![\w@])@{re.escape(role.name)}(?!\w)"
        if re.search(role_pattern, combined_text, flags=re.IGNORECASE):
            matched_roles.append(role)
    return matched_roles


def _users_mentioned_in_text(
    guild: discord.Guild, texts: list[str]
) -> tuple[list[discord.Member], Optional[str]]:
    """Resolve explicit Discord user mentions and unique inline @usernames."""
    combined_text = "\n".join(texts)
    recipients: dict[int, discord.Member] = {}

    for match in re.finditer(r"<@!?(\d{15,22})>", combined_text):
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        if member is None:
            return [], f"I couldn't find the member in this server for mention `<@{user_id}>`. Use the user selector instead."
        recipients[member.id] = member

    without_discord_mentions = re.sub(r"<@!?\d{15,22}>", " ", combined_text)
    role_spans: list[tuple[int, int]] = []
    for role in guild.roles:
        if role.is_default():
            continue
        pattern = rf"(?<![\w@])@{re.escape(role.name)}(?!\w)"
        role_spans.extend(
            match.span()
            for match in re.finditer(pattern, without_discord_mentions, flags=re.IGNORECASE)
        )

    for match in re.finditer(r"(?<![\w@/])@([A-Za-z0-9_.]{2,32})(?![\w])", without_discord_mentions):
        if any(start <= match.start() and match.end() <= end for start, end in role_spans):
            continue
        username = match.group(1)
        if username.casefold() in {"everyone", "here"}:
            continue

        key = username.casefold()
        members = {
            member.id: member
            for member in guild.members
            if key in {
                member.name.casefold(),
                (member.global_name or "").casefold(),
                member.display_name.casefold(),
            }
        }
        if not members:
            return [], f"I couldn't match `@{username}` to a server member. Use an exact username or select the member in the notification picker."
        if len(members) > 1:
            return [], f"`@{username}` matches more than one member. Use their exact username or select them in the notification picker."
        recipients.update(members)

    return list(recipients.values()), None


def _broadcast_mentions_in_text(texts: list[str]) -> list[str]:
    """Return explicit broadcast mentions in a stable canonical form."""
    combined_text = re.sub(r"https?://\S+", " ", "\n".join(texts), flags=re.IGNORECASE)
    mentions: list[str] = []
    for mention in ("everyone", "here"):
        if re.search(rf"(?<![\w@])@{mention}(?!\w)", combined_text, flags=re.IGNORECASE):
            mentions.append(f"@{mention}")
    return mentions


@dataclass
class AnnouncementDraft:
    author_id: int
    author_name: str
    header: str
    body: str
    footer: str
    images: list[discord.Attachment] = field(default_factory=list)
    links: list[AnnouncementLink] = field(default_factory=list)
    link_style: str = "auto"
    selected_section: str = "body"
    styles: dict[str, set[str]] = field(
        default_factory=lambda: {"header": set(), "body": set(), "footer": set()}
    )
    heading_level: int = 1
    image_placement: str = "large"
    destination: Optional[discord.TextChannel] = None
    notify_roles: list[discord.Role] = field(default_factory=list)
    notify_users: list[discord.User] = field(default_factory=list)

    def build_embed(self, *, image_url: Optional[str] = None) -> discord.Embed:
        header = _styled(self.header, self.styles["header"])
        body = _styled(self.body, self.styles["body"])
        footer = _styled(self.footer, self.styles["footer"])
        description_parts = [f"{'#' * self.heading_level} {header}"]
        if body:
            description_parts.append(body)
        if footer:
            description_parts.append(f"-# {footer}")

        embed = discord.Embed(
            description="\n\n".join(description_parts),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"By {self.author_name[:200]}")
        if self.link_style in {"auto", "card", "button"}:
            for link in self.links:
                value = (
                    f"[{link.label}]({link.url})"
                    if self.link_style != "button"
                    else "A link button will appear below the announcement."
                )
                embed.add_field(name=link.label[:256], value=value, inline=False)
        if image_url and self.images:
            if self.image_placement == "thumbnail":
                embed.set_thumbnail(url=image_url)
            else:
                embed.set_image(url=image_url)
        return embed

    def build_embeds(self, image_urls: Optional[list[str]] = None) -> list[discord.Embed]:
        """Build the announcement plus its uploaded and linked image previews."""
        image_urls = image_urls or []
        embeds = [self.build_embed(image_url=image_urls[0] if image_urls else None)]
        for index, image_url in enumerate(image_urls[1:], start=2):
            gallery_item = discord.Embed(
                title=f"Image {index}",
                color=discord.Color.blurple(),
            )
            gallery_item.set_image(url=image_url)
            embeds.append(gallery_item)

        if self.link_style in {"image", "thumbnail"}:
            link_images = self.links
            if not image_urls and link_images:
                first_link = link_images[0]
                if self.link_style == "image":
                    embeds[0].set_image(url=first_link.url)
                else:
                    embeds[0].set_thumbnail(url=first_link.url)
                embeds[0].add_field(
                    name=first_link.label[:256],
                    value=f"[Open link]({first_link.url})",
                    inline=False,
                )
                link_images = link_images[1:]
            for link in link_images:
                gallery_item = discord.Embed(
                    title=link.label[:256],
                    url=link.url,
                    description=f"[Open link]({link.url})",
                    color=discord.Color.blurple(),
                )
                if self.link_style == "image":
                    gallery_item.set_image(url=link.url)
                else:
                    gallery_item.set_thumbnail(url=link.url)
                embeds.append(gallery_item)
        return embeds

    def preview_content(self) -> Optional[str]:
        return _bounded_lines(self.native_preview_urls())

    def native_preview_urls(self) -> list[str]:
        urls = _youtube_preview_urls(self.body)
        if self.link_style == "auto":
            urls.extend(link.url for link in self.links)
        embed_capacity = max(0, 10 - len(self.build_embeds()))
        return list(dict.fromkeys(urls))[:embed_capacity]

    def embeds_text_length(self) -> int:
        total = 0
        for embed in self.build_embeds():
            total += len(embed.title or "") + len(embed.description or "")
            if embed.footer:
                total += len(embed.footer.text or "")
            for field in embed.fields:
                total += len(field.name) + len(field.value)
        return total


class AnnouncementModal(discord.ui.Modal, title="Create announcement"):
    header = discord.ui.TextInput(
        label="Header",
        placeholder="Announcement title",
        max_length=256,
    )
    body = discord.ui.TextInput(
        label="Body (Markdown and links supported)",
        style=discord.TextStyle.paragraph,
        placeholder="Write the announcement. You can include public document or YouTube links.",
        max_length=3000,
    )
    footer = discord.ui.TextInput(
        label="Footer (optional)",
        placeholder="Short closing text",
        required=False,
        max_length=500,
    )
    links_input = discord.ui.TextInput(
        label="Links (up to 10; optional label | URL)",
        style=discord.TextStyle.paragraph,
        placeholder="Event page | https://example.com\nhttps://youtu.be/example",
        required=False,
        max_length=2500,
    )
    def __init__(self, cog: "Announcement", author: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.author = author
        self.file_upload = discord.ui.FileUpload(
            min_values=0,
            max_values=10,
            required=False,
        )
        self.file_upload_label = discord.ui.Label(
            text="Images (up to 10)",
            description="Upload image files only.",
            component=self.file_upload,
        )
        self.add_item(self.file_upload_label)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not await _is_allowed(interaction.user):
            await interaction.response.send_message(
                "You no longer have an allowed announcement role or permission.", ephemeral=True
            )
            return

        links, link_error = _parse_links(str(self.links_input.value))
        if link_error:
            await interaction.response.send_message(link_error, ephemeral=True)
            return

        images = list(self.file_upload.values)
        for image in images:
            content_type = image.content_type or ""
            extension = os.path.splitext(image.filename)[1].lower()
            if not content_type.startswith("image/") and extension not in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"
            }:
                await interaction.response.send_message(
                    f"`{image.filename}` is not an image. Remove it and submit again.",
                    ephemeral=True,
                )
                return

        draft = AnnouncementDraft(
            author_id=self.author.id,
            author_name=self.author.display_name,
            header=str(self.header.value).strip(),
            body=str(self.body.value).strip(),
            footer=str(self.footer.value).strip(),
            images=images,
            links=links,
        )
        description_length = len(draft.build_embed().description or "")
        if description_length > 4096 or draft.embeds_text_length() > 6000:
            await interaction.response.send_message(
                "This announcement is too long for Discord embeds. Shorten the text or remove some links and try again.",
                ephemeral=True,
            )
            return

        view = AnnouncementPreviewView(self.cog, draft)
        image_urls = [image.url for image in images]
        await interaction.response.send_message(
            content=draft.preview_content(),
            embeds=draft.build_embeds(image_urls),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class FieldSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose which text section to style",
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(label="Header", value="header"),
                discord.SelectOption(label="Body", value="body"),
                discord.SelectOption(label="Footer", value="footer"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: AnnouncementPreviewView = self.view  # type: ignore[assignment]
        view.draft.selected_section = self.values[0]
        view.refresh_style_defaults()
        await view.refresh_preview(interaction)


class StyleSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Formatting for selected section",
            min_values=0,
            max_values=3,
            row=1,
            options=[
                discord.SelectOption(label="Bold", value="bold"),
                discord.SelectOption(label="Italic", value="italic"),
                discord.SelectOption(label="Underline", value="underline"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: AnnouncementPreviewView = self.view  # type: ignore[assignment]
        view.draft.styles[view.draft.selected_section] = set(self.values)
        view.refresh_style_defaults()
        await view.refresh_preview(interaction)


class HeadingSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Header size",
            min_values=1,
            max_values=1,
            row=2,
            options=[
                discord.SelectOption(label="Large heading", value="1", default=True),
                discord.SelectOption(label="Medium heading", value="2"),
                discord.SelectOption(label="Small heading", value="3"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: AnnouncementPreviewView = self.view  # type: ignore[assignment]
        view.draft.heading_level = int(self.values[0])
        for option in self.options:
            option.default = option.value == self.values[0]
        await view.refresh_preview(interaction)


class ImagePlacementSelect(discord.ui.Select):
    def __init__(self, has_image: bool):
        super().__init__(
            placeholder="Image placement",
            min_values=1,
            max_values=1,
            row=3,
            disabled=not has_image,
            options=[
                discord.SelectOption(label="Large image", value="large", default=True),
                discord.SelectOption(label="Small thumbnail", value="thumbnail"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: AnnouncementPreviewView = self.view  # type: ignore[assignment]
        view.draft.image_placement = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await view.refresh_preview(interaction)


class AnnouncementPreviewView(discord.ui.View):
    def __init__(self, cog: "Announcement", draft: AnnouncementDraft):
        super().__init__(timeout=300)
        self.cog = cog
        self.draft = draft
        self.field_select = FieldSelect()
        self.add_item(self.field_select)
        self.style_select = StyleSelect()
        self.add_item(self.style_select)
        self.heading_select = HeadingSelect()
        self.add_item(self.heading_select)
        self.add_item(ImagePlacementSelect(bool(draft.images)))
        self.refresh_link_style_button()

    def refresh_link_style_button(self):
        labels = {
            "auto": "Auto preview",
            "card": "Link card",
            "button": "Link button",
            "image": "Large link image",
            "thumbnail": "Link thumbnail",
        }
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "announcement:link-style":
                item.label = f"Link: {labels[self.draft.link_style]}"
                item.disabled = not bool(self.draft.links)

    def refresh_style_defaults(self):
        selected = self.draft.styles[self.draft.selected_section]
        for option in self.style_select.options:
            option.default = option.value in selected
        for option in self.field_select.options:
            option.default = option.value == self.draft.selected_section

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.draft.author_id:
            await interaction.response.send_message("This announcement editor belongs to someone else.", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member) or not await _is_allowed(interaction.user):
            await interaction.response.send_message("You no longer have an allowed announcement role or permission.", ephemeral=True)
            return False
        return True

    async def refresh_preview(self, interaction: discord.Interaction):
        image_urls = [image.url for image in self.draft.images]
        await interaction.response.edit_message(
            content=self.draft.preview_content(),
            embeds=self.draft.build_embeds(image_urls),
            view=self,
        )

    @discord.ui.button(label="Choose destination", style=discord.ButtonStyle.primary, row=4)
    async def choose_destination(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Choose the destination channel and any roles to notify, then publish.",
            view=AnnouncementDeliveryView(self.cog, self.draft, self),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Announcement cancelled.", embed=None, view=None)

    @discord.ui.button(
        label="Link: Auto preview",
        style=discord.ButtonStyle.secondary,
        custom_id="announcement:link-style",
        row=4,
    )
    async def cycle_link_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        styles = ["auto", "card", "button", "image", "thumbnail"]
        next_style = styles[(styles.index(self.draft.link_style) + 1) % len(styles)]
        if next_style in {"image", "thumbnail"}:
            image_embed_count = len(self.draft.images) + len(self.draft.links)
            if image_embed_count > 10:
                await interaction.response.send_message(
                    "Image link previews plus uploaded images can use up to 10 embeds total. Remove some items to use this style.",
                    ephemeral=True,
                )
                return
        self.draft.link_style = next_style
        self.refresh_link_style_button()
        await self.refresh_preview(interaction)


class AnnouncementDeliveryView(discord.ui.View):
    def __init__(
        self,
        cog: "Announcement",
        draft: AnnouncementDraft,
        preview_view: AnnouncementPreviewView,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.draft = draft
        self.preview_view = preview_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.draft.author_id:
            await interaction.response.send_message("This announcement editor belongs to someone else.", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member) or not await _is_allowed(interaction.user):
            await interaction.response.send_message("You no longer have an allowed announcement role or permission.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select destination text channel",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        min_values=1,
        max_values=1,
        row=0,
    )
    async def destination_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
    ):
        selected = select.values[0]
        channel = interaction.guild.get_channel(selected.id) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            self.draft.destination = channel
            await interaction.response.edit_message(
                content=f"Destination: {channel.mention}. Choose optional roles and users to notify, then publish.",
                view=self,
            )
        else:
            await interaction.response.send_message("Please select a text channel.", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Optional roles to notify",
        min_values=0,
        max_values=25,
        row=1,
    )
    async def role_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.RoleSelect,
    ):
        self.draft.notify_roles = [role for role in select.values if not role.is_default()]
        labels = ", ".join(role.name for role in self.draft.notify_roles) or "None"
        destination = self.draft.destination.mention if self.draft.destination else "not selected"
        users = ", ".join(getattr(user, "display_name", user.name) for user in self.draft.notify_users) or "None"
        await interaction.response.edit_message(
            content=f"Destination: {destination}\nRoles to notify: {labels}\nUsers to notify: {users}",
            view=self,
        )

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Optional users to notify",
        min_values=0,
        max_values=25,
        row=2,
    )
    async def user_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.UserSelect,
    ):
        self.draft.notify_users = list(select.values)
        names = ", ".join(getattr(user, "display_name", user.name) for user in self.draft.notify_users) or "None"
        roles = ", ".join(role.name for role in self.draft.notify_roles) or "None"
        destination = self.draft.destination.mention if self.draft.destination else "not selected"
        await interaction.response.edit_message(
            content=f"Destination: {destination}\nRoles to notify: {roles}\nUsers to notify: {names}",
            view=self,
        )

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, row=3)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.draft.destination is None:
            await interaction.response.send_message("Choose a destination channel first.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not await _is_allowed(interaction.user):
            await interaction.response.send_message("You no longer have an allowed announcement role or permission.", ephemeral=True)
            return

        channel = self.draft.destination
        guild = interaction.guild
        authored_texts = [self.draft.header, self.draft.body, self.draft.footer]
        inline_users, user_mention_error = (
            _users_mentioned_in_text(guild, authored_texts) if guild else ([], None)
        )
        if user_mention_error:
            await interaction.response.send_message(user_mention_error, ephemeral=True)
            return

        broadcast_mentions = _broadcast_mentions_in_text(authored_texts)
        text_mentioned_roles = (
            _roles_mentioned_in_text(guild, authored_texts)
            if guild
            else []
        )
        notify_roles_by_id = {
            role.id: role for role in [*self.draft.notify_roles, *text_mentioned_roles]
        }
        notify_roles = list(notify_roles_by_id.values())
        notify_users_by_id = {
            user.id: user for user in [*self.draft.notify_users, *inline_users]
        }
        notify_users = list(notify_users_by_id.values())
        me = interaction.guild.me if interaction.guild else None
        if me is not None:
            permissions = channel.permissions_for(me)
            if not permissions.send_messages or not permissions.embed_links:
                await interaction.response.send_message(
                    "I need Send Messages and Embed Links permissions in that channel.",
                    ephemeral=True,
                )
                return
            if self.draft.images and not permissions.attach_files:
                await interaction.response.send_message(
                    "I need Attach Files permission to include these images.", ephemeral=True
                )
                return
            if broadcast_mentions and not permissions.mention_everyone:
                await interaction.response.send_message(
                    "I need Mention Everyone permission in that channel to notify `@everyone` or `@here`.",
                    ephemeral=True,
                )
                return
            unmentionable_roles = [
                role for role in notify_roles if not role.mentionable
            ]
            if unmentionable_roles and not permissions.mention_everyone:
                names = ", ".join(role.name for role in unmentionable_roles)
                await interaction.response.send_message(
                    "I cannot notify these roles because they are not mentionable "
                    f"and I lack Mention Everyone permission: {names}",
                    ephemeral=True,
                )
                return

        mention_parts = [role.mention for role in notify_roles]
        mention_parts.extend(f"<@{user.id}>" for user in notify_users)
        mention_parts.extend(broadcast_mentions)
        unique_mentions = list(dict.fromkeys(mention_parts))
        mention_content = "\n".join(unique_mentions)
        if len(mention_content) > 1900:
            await interaction.response.send_message(
                "There are too many notification mentions to fit in one Discord message. Reduce the selected or inline recipients.",
                ephemeral=True,
            )
            return
        content_parts = [*unique_mentions, *self.draft.native_preview_urls()]
        content = _bounded_lines(content_parts, limit=2000)
        allowed_mentions = discord.AllowedMentions(
            everyone=bool(broadcast_mentions),
            users=notify_users or False,
            roles=notify_roles or False,
            replied_user=False,
        )
        files: list[discord.File] = []
        image_urls: list[str] = []

        # Acknowledge the component interaction before image retrieval or the
        # channel send can exceed Discord's interaction response deadline.
        await interaction.response.defer()

        if self.draft.images:
            try:
                for index, image in enumerate(self.draft.images, start=1):
                    extension = os.path.splitext(image.filename)[1].lower()
                    if not extension:
                        extension = mimetypes.guess_extension(image.content_type or "") or ".png"
                    filename = f"announcement-image-{index}{extension}"
                    files.append(await image.to_file(filename=filename))
                    image_urls.append(f"attachment://{filename}")
            except (discord.HTTPException, OSError):
                await interaction.edit_original_response(
                    content="Could not retrieve one of the uploaded images. Please start a new announcement and attach them again.",
                    embed=None,
                    view=None,
                )
                return

        embeds = self.draft.build_embeds(image_urls)
        link_view = None
        if self.draft.links and self.draft.link_style == "button":
            link_view = discord.ui.View(timeout=None)
            for index, link in enumerate(self.draft.links):
                link_view.add_item(
                    discord.ui.Button(
                        label=link.label[:80] or f"Open link {index + 1}",
                        style=discord.ButtonStyle.link,
                        url=link.url,
                        row=index // 5,
                    )
                )
        try:
            send_kwargs = {
                "content": content,
                "embeds": embeds,
                "view": link_view,
                "allowed_mentions": allowed_mentions,
            }
            if files:
                send_kwargs["files"] = files
            await channel.send(**send_kwargs)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="I do not have permission to publish in that channel.",
                embed=None,
                view=None,
            )
            return
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Discord could not send the announcement. Check the text and image size, then try again.",
                embed=None,
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"Announcement published in {channel.mention}.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Back to preview", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=self.draft.preview_content(),
            embeds=self.draft.build_embeds([image.url for image in self.draft.images]),
            view=self.preview_view,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Announcement cancelled.", embed=None, view=None)


class Announcement(commands.Cog):
    """Compose, preview, and publish role-restricted announcements."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="announcement", description="Compose and publish an announcement")
    @app_commands.guild_only()
    async def announcement(
        self,
        interaction: discord.Interaction,
    ):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        if not await Config.is_module_enabled(interaction.guild_id, "announcement"):
            await interaction.response.send_message("❌ The announcement module is currently disabled in this server.", ephemeral=True)
            return

        if not await _is_allowed(interaction.user):
            await interaction.response.send_message(
                "❌ You need an allowed announcement role or Administrator permission to use this command.\n"
                "Server administrators can configure allowed roles with `/announcement_roles add <role>`.",
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(AnnouncementModal(self, interaction.user))
        except Exception:
            logger.exception("Failed to open the announcement modal")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I couldn't open the announcement form. Please try again or contact a server admin.",
                    ephemeral=True,
                )

    announcement_roles_group = app_commands.Group(
        name="announcement_roles",
        description="Manage roles allowed to create announcements"
    )

    @announcement_roles_group.command(name="add", description="Add a role allowed to create announcements")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to allow")
    async def roles_add(self, interaction: discord.Interaction, role: discord.Role):
        roles = await Config.get_announcement_roles(interaction.guild_id)
        if role.id in roles:
            await interaction.response.send_message(f"ℹ️ {role.mention} is already allowed to create announcements.", ephemeral=True)
            return
        roles.append(role.id)
        await Config.set_announcement_roles(interaction.guild_id, roles)
        await interaction.response.send_message(f"✅ {role.mention} is now allowed to create announcements.", ephemeral=True)

    @announcement_roles_group.command(name="remove", description="Remove a role from allowed announcement creators")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to remove")
    async def roles_remove(self, interaction: discord.Interaction, role: discord.Role):
        roles = await Config.get_announcement_roles(interaction.guild_id)
        if role.id not in roles:
            await interaction.response.send_message(f"ℹ️ {role.mention} is not in the allowed announcement roles list.", ephemeral=True)
            return
        roles.remove(role.id)
        await Config.set_announcement_roles(interaction.guild_id, roles)
        await interaction.response.send_message(f"✅ {role.mention} has been removed from allowed announcement roles.", ephemeral=True)

    @announcement_roles_group.command(name="list", description="List roles allowed to create announcements")
    @app_commands.default_permissions(administrator=True)
    async def roles_list(self, interaction: discord.Interaction):
        roles = await Config.get_announcement_roles(interaction.guild_id)
        embed = discord.Embed(title="📢 Announcement Allowed Roles", color=discord.Color.blue())
        embed.description = "🛡️ **Server Administrators** always have full access to `/announcement` by default.\n\n"
        if roles:
            role_mentions = [f"• <@&{r_id}>" for r_id in roles]
            embed.add_field(name="Configured Server Roles", value="\n".join(role_mentions), inline=False)
        else:
            env_roles = _env_allowed_role_ids()
            if env_roles:
                role_mentions = [f"• <@&{r_id}> *(from .env fallback)*" for r_id in env_roles]
                embed.add_field(name="Active Fallback Roles (.env)", value="\n".join(role_mentions), inline=False)
            else:
                embed.add_field(name="Configured Server Roles", value="*No specific roles added. (Administrators only)*\nUse `/announcement_roles add <role>` to grant access to other roles.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Announcement(bot))

