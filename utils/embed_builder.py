import discord
from datetime import datetime

class EmbedBuilder:
    """Utility class for creating consistent embeds."""

    COLORS = {
        "success": discord.Color.green(),
        "error": discord.Color.red(),
        "warning": discord.Color.orange(),
        "info": discord.Color.blue(),
        "music": discord.Color.purple(),
        "log": discord.Color.dark_grey(),
        "log_message_delete": discord.Color.red(),
        "log_message_edit": discord.Color.orange(),
        "log_member_join": discord.Color.green(),
        "log_member_leave": discord.Color.dark_red(),
        "log_member_ban": discord.Color.dark_red(),
        "log_voice": discord.Color.teal(),
        "log_channel": discord.Color.blue(),
        "log_role": discord.Color.gold(),
    }

    @staticmethod
    def create(
        title: str = None,
        description: str = None,
        color_key: str = "info",
        author: discord.Member = None,
        footer: str = None,
        thumbnail: str = None,
        timestamp: bool = True
    ) -> discord.Embed:
        color = EmbedBuilder.COLORS.get(color_key, discord.Color.blurple())
        embed = discord.Embed(title=title, description=description, color=color)

        if timestamp:
            embed.timestamp = datetime.utcnow()
        if author:
            embed.set_author(name=str(author), icon_url=author.display_avatar.url if author.display_avatar else None)
        if footer:
            embed.set_footer(text=footer)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        return embed
