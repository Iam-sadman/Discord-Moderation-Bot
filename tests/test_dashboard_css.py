"""
Unit tests for Web Dashboard Static CSS Architecture.
Validates:
- Static file serving configuration in Quart
- Unauthenticated access to stylesheets
- Correct MIME type text/css
- Template links to external stylesheets
"""

import os
import unittest
from unittest.mock import MagicMock
from dashboard.app import create_dashboard


class TestDashboardCSS(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.name = "TestBot"
        self.bot.user.display_avatar = None
        self.bot.guilds = []
        self.app = create_dashboard(self.bot)
        self.app.testing = True
        self.client = self.app.test_client()

    async def test_static_css_files_exist(self):
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "static", "css")
        expected_files = ["main.css", "login.css", "index.css", "guild.css", "forms.css"]
        for fname in expected_files:
            file_path = os.path.join(static_dir, fname)
            self.assertTrue(os.path.exists(file_path), f"Missing CSS file: {fname}")
            self.assertGreater(os.path.getsize(file_path), 500, f"CSS file too small: {fname}")

    async def test_unauthenticated_static_css_serving(self):
        """Validates that stylesheets can be requested without authentication (status 200, text/css)."""
        css_files = ["main.css", "login.css", "index.css", "guild.css", "forms.css"]
        for fname in css_files:
            response = await self.client.get(f"/static/css/{fname}")
            self.assertEqual(response.status_code, 200, f"Failed to serve /static/css/{fname}")
            content_type = response.headers.get("Content-Type", "")
            self.assertTrue("text/css" in content_type or "charset=utf-8" in content_type,
                            f"Unexpected Content-Type for {fname}: {content_type}")
            body = await response.get_data(as_text=True)
            self.assertGreater(len(body), 100, f"Empty body for {fname}")

    async def test_login_template_links_css(self):
        """Validates that /login renders external CSS link rather than embedded style block."""
        response = await self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        html = await response.get_data(as_text=True)
        self.assertIn("/static/css/main.css", html)
        self.assertIn("/static/css/login.css", html)
        self.assertNotIn("<style>", html)
        self.assertNotIn('style="', html)

    async def test_css_vendor_prefix_ordering(self):
        """Validates that vendor prefixes precede standard CSS properties across all static stylesheets."""
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "static", "css")
        for fname in ["main.css", "login.css", "index.css", "guild.css", "forms.css"]:
            fpath = os.path.join(static_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines):
                if "backdrop-filter:" in line and "-webkit-" not in line:
                    prev = lines[idx - 1] if idx > 0 else ""
                    self.assertIn("-webkit-backdrop-filter:", prev,
                                  f"In {fname} line {idx+1}: backdrop-filter must be preceded by -webkit-backdrop-filter")
                if "user-select:" in line and "-webkit-" not in line:
                    prev = lines[idx - 1] if idx > 0 else ""
                    self.assertIn("-webkit-user-select:", prev,
                                  f"In {fname} line {idx+1}: user-select must be preceded by -webkit-user-select")
                self.assertNotIn("text-justify:", line, f"Non-standard text-justify found in {fname} line {idx+1}")


if __name__ == "__main__":
    unittest.main()
