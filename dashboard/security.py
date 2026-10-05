"""
Dashboard Security, Network Hardware/MAC Resolution, and Audit Logging.
Tracks client IP addresses, local network MAC addresses, browser device signatures,
and records all dashboard login attempts and permission updates.
"""

import os
import re
import uuid
import logging
import subprocess
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List
import aiosqlite
from config import Config

logger = logging.getLogger("DashboardSecurity")


def get_client_ip(request) -> str:
    """Extracts client IP address accounting for proxies and direct connections."""
    headers = request.headers
    # Priority order for reverse proxy / cloudflare headers
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        val = headers.get(header)
        if val:
            # X-Forwarded-For can contain multiple comma-separated IPs (client, proxy1, proxy2)
            return val.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def get_client_mac(ip_address: str) -> str:
    """
    Attempts to resolve client MAC address:
    - If loopback/localhost: returns local hardware MAC via uuid.getnode().
    - If local subnet: queries system ARP cache (Windows/Linux) for client MAC.
    - If remote routed: returns 'Remote / Non-LAN' with subnet identifier.
    """
    if not ip_address:
        return "Unknown"

    clean_ip = ip_address.strip()

    # 1. Localhost / loopback check
    if clean_ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        mac_num = uuid.getnode()
        mac_hex = f"{mac_num:012x}"
        return ":".join(mac_hex[i:i+2] for i in range(0, 12, 2)).upper()

    # 2. Local network ARP resolution (Windows / Linux)
    # Check if IP looks like a private local IPv4 address
    is_private_lan = (
        clean_ip.startswith("192.168.") or
        clean_ip.startswith("10.") or
        re.match(r"^172\.(1[6-9]|2[0-9]|3[0-1])\.", clean_ip)
    )

    if is_private_lan:
        try:
            # Run arp -a <ip>
            output = subprocess.check_output(
                ["arp", "-a", clean_ip],
                timeout=1.5,
                stderr=subprocess.DEVNULL,
                universal_newlines=True
            )
            # Find MAC regex: XX-XX-XX-XX-XX-XX or XX:XX:XX:XX:XX:XX
            match = re.search(r"([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})", output)
            if match:
                return match.group(1).replace("-", ":").upper()
        except Exception:
            pass

    # 3. For routed internet / WAN requests (Layer 2 frame replaced by gateway)
    return "Remote (Gateway Routed)"


def get_device_info(request) -> str:
    """Parses User-Agent into a clean device and browser string."""
    ua = request.headers.get("User-Agent", "Unknown Device")
    
    # Simple OS detection
    os_name = "Unknown OS"
    if "Windows NT 10.0" in ua:
        os_name = "Windows 10/11"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Macintosh" in ua or "Mac OS" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"

    # Browser detection
    browser = "Browser"
    if "Edg/" in ua:
        browser = "Microsoft Edge"
    elif "Chrome/" in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"

    return f"{browser} on {os_name}"


async def init_security_db():
    """Initializes dashboard audit logs table in settings.db."""
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    async with aiosqlite.connect(Config.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT NOT NULL,
                user_name TEXT,
                action TEXT NOT NULL,
                ip_address TEXT,
                mac_address TEXT,
                device_info TEXT,
                details TEXT
            )
        """)
        await db.commit()


async def log_dashboard_event(
    user_id: str = "unknown",
    action: str = "ACTION",
    user_name: Optional[str] = None,
    ip_address: str = "Unknown",
    mac_address: str = "Unknown",
    device_info: str = "Unknown",
    details: str = "",
    target: Optional[str] = None
):
    """Inserts a new dashboard security / audit record."""
    await init_security_db()
    if target:
        details = f"[{target}] {details}" if details else f"[{target}]"
    if not user_name:
        user_name = user_id

    async with aiosqlite.connect(Config.DB_PATH) as db:
        await db.execute("""
            INSERT INTO dashboard_access_logs (
                timestamp, user_id, user_name, action, ip_address, mac_address, device_info, details
            ) VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, user_name, action, ip_address, mac_address, device_info, details))
        await db.commit()



async def get_dashboard_logs(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieves recent dashboard security and audit logs."""
    await init_security_db()
    async with aiosqlite.connect(Config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT id, timestamp, user_id, user_name, action, ip_address, mac_address, device_info, details
            FROM dashboard_access_logs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
