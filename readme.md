# 🤖 Modular Discord Admin & Moderation Bot

A production-ready, modular Discord bot built using Python (`discord.py`). Designed for server administration, user activity logging, and dynamic cog management. Configured for high availability on Linux (Ubuntu VPS) using `systemd`.

---

## 📌 Features

* **Modular Architecture (Cogs):** Dynamically load, unload, or reload commands and modules without stopping the bot.
* **Administration & Moderation:** Tools for member moderation (kick, ban, timeout), role handling, and channel security.
* **Logging System:** Streamlined event logging to monitor server activity and user commands.
* **Systemd Integration:** Runs as a background service with auto-restart on system boot or unexpected crashes.
* **Legal Compliance:** Includes built-in `Terms of Service` and `Privacy Policy` documentation.

---

## 📁 Project Structure

```
/home/gekiye/Downloads/DiscordBot/
├── bot.py                   # Main entry point & cog loader
├── cogs/                    # Cog modules
│   ├── admin.py             # Administrative utilities
│   ├── moderation.py        # Moderation tools
│   ├── logging.py           # Logging handlers
│   └── cog_status.py        # Cog inspection command
├── terms_of_service.md      # Legal Terms of Service
├── privacy_policy.md        # Privacy Policy documentation
├── README.md                # Project documentation
├── venv/                    # Python virtual environment
└── requirements.txt         # Project dependencies
```

---

## ⚙️ Prerequisites & Installation

### 1. System Dependencies
Ensure Python 3.11+ and necessary packages are installed on your Linux machine:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg
```

### 2. Environment Setup
Navigate to your project directory and activate the virtual environment:

```bash
cd /home/gekiye/Downloads/DiscordBot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install discord.py python-dotenv
```

### 3. Environment Variables
Create a `.env` file in the root directory:

```env
DISCORD_TOKEN=your_bot_token_here
PREFIX=!
```

---

## 🚀 Service Setup (systemd)

To run the bot 24/7 as a background process, create a service file at `/etc/systemd/system/discordbot.service`:

```ini
[Unit]
Description=Discord Modular Bot
After=network.target

[Service]
Type=simple
User=gekiye
WorkingDirectory=/home/gekiye/Downloads/DiscordBot
ExecStart=/home/gekiye/Downloads/DiscordBot/venv/bin/python /home/gekiye/Downloads/DiscordBot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Management Commands

* **Reload Daemon Configuration:**
  ```bash
  sudo systemctl daemon-reload
  ```
* **Enable Service on Boot:**
  ```bash
  sudo systemctl enable discordbot
  ```
* **Start Service:**
  ```bash
  sudo systemctl start discordbot
  ```
* **Restart Service:**
  ```bash
  sudo systemctl restart discordbot
  ```
* **Check Service Status:**
  ```bash
  sudo systemctl status discordbot
  ```

---

## 📊 Monitoring & Logs

View real-time bot execution logs or search for specific user actions directly via systemd journals:

* **Real-time Live Logs:**
  ```bash
  sudo journalctl -u discordbot -f
  ```
* **Filter Logs by User or ID:**
  ```bash
  sudo journalctl -u discordbot --since today | grep -i "USER_NAME_OR_ID"
  ```

---

## 📄 Legal Documentation

* [Terms of Service](terms_of_service.md)
* [Privacy Policy](privacy_policy.md)

---

## 👥 Maintainer
Developed and maintained by **Sadman**.
