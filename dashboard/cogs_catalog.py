"""
Cogs and Slash Commands Catalog Provider for Discord Bot.
Extracts comprehensive metadata about all loaded cogs, slash commands,
prefix commands, parameters, and verified permission access levels.
"""

from typing import Dict, Any, List
import inspect
import discord
from discord import app_commands
from discord.ext import commands


def resolve_command_permissions(cmd_name: str, cog_name: str, app_cmd_perms: Any = None) -> str:
    """Accurately maps command name and cog to user-facing permission level."""
    c = cmd_name.strip()

    # Settings Cog commands (!command) are strictly Administrator
    if cog_name == "Settings" or c.startswith("!"):
        return "Server Administrator"

    # Labeler Tracker Cog commands (/arc*) are strictly Administrator
    if cog_name == "LabelerTracker" or c.startswith("/arc"):
        return "Server Administrator"

    # Announcement Cog
    if c.startswith("/announcement_roles"):
        return "Server Administrator"
    if c == "/announcement":
        return "Allowed Roles / Admin"

    # LOA Cog
    if c in ("/loa_setup", "/loa_config"):
        return "Server Administrator"
    if c == "/loa_report":
        return "Checkers / Admin"
    if c == "/loa_end":
        return "Applicant / Admin"
    if c == "/loa_status":
        return "Everyone"

    # Logger Cog
    if c.startswith("/log") or c.startswith("/logsearch"):
        return "Manage Server / Admin"

    # Music Cog
    if cog_name == "Music":
        return "Everyone (Voice Channel)"

    # Default fallback using discord permission flags
    if app_cmd_perms:
        if getattr(app_cmd_perms, "administrator", False):
            return "Server Administrator"
        if getattr(app_cmd_perms, "manage_guild", False):
            return "Manage Server / Admin"
        if getattr(app_cmd_perms, "manage_messages", False):
            return "Manage Messages"

    return "Everyone"


def get_cogs_and_commands_catalog(bot) -> List[Dict[str, Any]]:
    """
    Returns an organized list of cogs and their registered slash/prefix commands.
    Extracts directly from each Cog instance without fragile string heuristics.
    """
    catalog = []

    # Map of cog names to friendly metadata & icons
    cog_meta = {
        "Settings": {
            "display_name": "Server Settings & Prefix Commands",
            "icon": "⚙️",
            "category": "Administration",
            "color": "#F59E0B",
            "description": "Configuration manager for bot command prefixes, feature module toggles, and server settings. Restricted strictly to Server Administrators."
        },
        "LabelerTracker": {
            "display_name": "ARC Telemetry Tracker",
            "icon": "📊",
            "category": "Operations",
            "color": "#06B6D4",
            "description": "Automated telemetry polling and live leaderboard dashboard for ARC team labelers. Restricted strictly to Server Administrators."
        },
        "Announcement": {
            "display_name": "Announcement System",
            "icon": "📢",
            "category": "Communication",
            "color": "#3B82F6",
            "description": "Guided modal announcement composer with formatting styles, channel destinations, and role-based access control."
        },
        "Loa": {
            "display_name": "Leave of Absence (LOA)",
            "icon": "🌴",
            "category": "Staff Management",
            "color": "#10B981",
            "description": "Full lifecycle leave management: member applications, checker reviews, live pinned dashboard, and audit reports."
        },
        "Logger": {
            "display_name": "Server Audit Logger",
            "icon": "🛡️",
            "category": "Moderation & Security",
            "color": "#8B5CF6",
            "description": "High-fidelity server auditing for messages, voice events, moderation actions, and member history lookup."
        },
        "Music": {
            "display_name": "Music Player Engine",
            "icon": "🎵",
            "category": "Entertainment",
            "color": "#EC4899",
            "description": "Voice audio streaming from YouTube and server libraries with queue management, loop modes, and volume control."
        }
    }

    # Desired canonical presentation order
    canonical_order = ["Settings", "LabelerTracker", "Announcement", "Loa", "Logger", "Music"]

    # Function to parse an app_command (command or group)
    def parse_app_command(cmd, cog_name: str, prefix="/") -> List[Dict[str, Any]]:
        results = []
        if isinstance(cmd, app_commands.Group):
            for sub_cmd in cmd.commands:
                results.extend(parse_app_command(sub_cmd, cog_name=cog_name, prefix=f"{prefix}{cmd.name} "))
        else:
            params = []
            for param in getattr(cmd, "parameters", []):
                params.append({
                    "name": param.name,
                    "description": param.description or "",
                    "required": param.required,
                    "type": str(param.type).replace("AppCommandOptionType.", "")
                })

            full_name = f"{prefix}{cmd.name}"
            perms = resolve_command_permissions(full_name, cog_name, getattr(cmd, "default_permissions", None))

            results.append({
                "name": full_name,
                "description": cmd.description or "No description provided.",
                "parameters": params,
                "permissions": perms,
                "type": "Slash Command"
            })
        return results

    # Function to parse prefix commands (with group support)
    def parse_prefix_command(cmd, cog_name: str, prefix="!") -> List[Dict[str, Any]]:
        results = []
        if isinstance(cmd, commands.Group):
            # Include base group command
            results.append({
                "name": f"{prefix}{cmd.name}",
                "description": cmd.help or cmd.brief or "Settings group command",
                "parameters": [],
                "permissions": resolve_command_permissions(f"{prefix}{cmd.name}", cog_name),
                "type": "Prefix Command"
            })
            # Include each subcommand
            for sub in sorted(cmd.commands, key=lambda c: c.name):
                sub_params = []
                for k, v in sub.clean_params.items():
                    sub_params.append({
                        "name": k,
                        "description": "",
                        "required": v.default is inspect.Parameter.empty,
                        "type": "str"
                    })
                full_sub_name = f"{prefix}{cmd.name} {sub.name}"
                results.append({
                    "name": full_sub_name,
                    "description": sub.help or sub.brief or f"{cmd.name} {sub.name} subcommand",
                    "parameters": sub_params,
                    "permissions": resolve_command_permissions(full_sub_name, cog_name),
                    "type": "Prefix Command"
                })
        else:
            params = []
            for k, v in cmd.clean_params.items():
                params.append({
                    "name": k,
                    "description": "",
                    "required": v.default is inspect.Parameter.empty,
                    "type": "str"
                })
            full_name = f"{prefix}{cmd.name}"
            results.append({
                "name": full_name,
                "description": cmd.help or cmd.brief or "Prefix command",
                "parameters": params,
                "permissions": resolve_command_permissions(full_name, cog_name),
                "type": "Prefix Command"
            })
        return results

    if not bot or not hasattr(bot, "cogs"):
        return catalog

    # Sort cogs in canonical order
    loaded_cogs = bot.cogs
    ordered_cog_names = [c for c in canonical_order if c in loaded_cogs]
    ordered_cog_names.extend([c for c in loaded_cogs if c not in ordered_cog_names])

    for cog_name in ordered_cog_names:
        cog = loaded_cogs[cog_name]
        meta = cog_meta.get(cog_name, {
            "display_name": cog_name,
            "icon": "📦",
            "category": "General",
            "color": "#6366F1",
            "description": getattr(cog, "__doc__", "Modular bot extension cog.") or "Modular bot extension cog."
        })

        cog_commands = []

        # 1. App commands directly registered on this Cog
        app_cmds = getattr(cog, "get_app_commands", lambda: [])()
        for app_cmd in app_cmds:
            cog_commands.extend(parse_app_command(app_cmd, cog_name=cog_name))

        # 2. Prefix commands registered on this Cog
        prefix_cmds = getattr(cog, "get_commands", lambda: [])()
        for prefix_cmd in sorted(prefix_cmds, key=lambda c: c.name):
            cog_commands.extend(parse_prefix_command(prefix_cmd, cog_name=cog_name))

        catalog.append({
            "cog_name": cog_name,
            "display_name": meta["display_name"],
            "icon": meta["icon"],
            "category": meta["category"],
            "color": meta["color"],
            "description": meta["description"],
            "commands_count": len(cog_commands),
            "commands": cog_commands
        })

    return catalog
