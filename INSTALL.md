# Discord Bot Installation Guide (Ubuntu VPS)

This guide walks you through setting up the bot on an Ubuntu server with systemd for auto-restart and PM2 for process management.

---

## Prerequisites

- Ubuntu 20.04+ VPS
- Root or sudo access
- A Discord bot token from [Discord Developer Portal](https://discord.com/developers/applications)

---

## Step 1: System Update

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Step 2: Install Python 3.11+

```bash
# Install required packages
sudo apt install -y software-properties-common

# Add Python PPA (for Python 3.11+)
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update

# Install Python 3.11 and essentials
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip

# Set Python 3.11 as default (optional but recommended)
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# Verify installation
python --version
```

---

## Step 3: Install FFmpeg (Required for Music)

```bash
sudo apt install -y ffmpeg

# Verify
ffmpeg -version
```

---

## Step 4: Install Additional Dependencies

```bash
# Required for PyNaCl (voice support)
sudo apt install -y build-essential libffi-dev libsodium-dev

# Optional: Git for cloning
sudo apt install -y git
```

---

## Step 5: Create Bot User (Security Best Practice)

```bash
# Create a dedicated user for the bot
sudo useradd -m -s /bin/bash discordbot

# Switch to the bot user
sudo su - discordbot
```

---

## Step 6: Upload Bot Files

**Option A: Using SCP (from your local machine)**

```bash
# From your Windows machine (PowerShell/CMD)
scp -r "D:\Projects\Discord Bot\Discord Bot" discordbot@your-vps-ip:/home/discordbot/
```

**Option B: Using Git**

```bash
# On the VPS (as discordbot user)
cd /home/discordbot
git clone https://github.com/your-username/your-bot-repo.git discord-bot
cd discord-bot
```

**Option C: Using SFTP (FileZilla, WinSCP)**

Connect to your VPS via SFTP and upload the entire `Discord Bot` folder to `/home/discordbot/discord-bot/`

---

## Step 7: Set Up Virtual Environment

```bash
# Navigate to bot directory
cd /home/discordbot/discord-bot

# Create virtual environment
python -m venv venv

# Activate it
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip
```

---

## Step 8: Install Python Dependencies

```bash
# Install all requirements
pip install -r requirements.txt
```

---

## Step 9: Configure Environment Variables

```bash
# Copy example env file
cp .env.example .env

# Edit the file
nano .env
```

**Required values:**
```env
DISCORD_TOKEN=your_actual_bot_token_here
BOT_PREFIX=!
WEB_DASHBOARD_PORT=5000
WEB_DASHBOARD_HOST=0.0.0.0
WEB_SECRET_KEY=generate_a_random_32_char_string_here
```

**Generate a secret key:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save with `Ctrl+O`, then `Enter`, then exit with `Ctrl+X`.

---

## Step 10: Test the Bot

```bash
# Make sure venv is activated
source venv/bin/activate

# Run the bot
python bot.py
```

If successful, you'll see:
```
[✓] Loaded cog: cogs.settings
[✓] Loaded cog: cogs.music
[✓] Loaded cog: cogs.logger
[✓] Synced X slash commands
[✓] Web dashboard starting on 0.0.0.0:5000
Bot is ready!
```

Stop with `Ctrl+C`.

---

## Step 11: Set Up Systemd Service (Auto-start on Boot)

Exit the discordbot user and return to your main user:

```bash
exit  # Exit from discordbot user back to your user
```

Create systemd service file:

```bash
sudo nano /etc/systemd/system/discord-bot.service
```

**Paste this configuration:**

```ini
[Unit]
Description=Discord Modular Bot
After=network.target

[Service]
Type=simple
User=discordbot
Group=discordbot
WorkingDirectory=/home/discordbot/discord-bot
ExecStart=/home/discordbot/discord-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=discord-bot

# Environment
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

Save and exit.

---

## Step 12: Enable and Start the Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable discord-bot

# Start the bot
sudo systemctl start discord-bot

# Check status
sudo systemctl status discord-bot
```

---

## Step 13: Useful Commands

```bash
# View logs
sudo journalctl -u discord-bot -f

# View last 100 lines
sudo journalctl -u discord-bot -n 100

# Restart bot
sudo systemctl restart discord-bot

# Stop bot
sudo systemctl stop discord-bot

# Disable auto-start
sudo systemctl disable discord-bot
```

---

## Step 14: Firewall Configuration (Important!)

**If using UFW:**

```bash
# Allow web dashboard port
sudo ufw allow 5000/tcp

# Enable firewall (if not already)
sudo ufw enable

# Check status
sudo ufw status
```

**If using iptables:**

```bash
sudo iptables -A INPUT -p tcp --dport 5000 -j ACCEPT
sudo iptables-save
```

---

## Step 15: Access the Web Dashboard

Your dashboard is now accessible at:

```
http://your-vps-ip:5000
```

**Security Recommendation:** The dashboard has no authentication by default. For production, consider:

1. **Using a reverse proxy with HTTPS (nginx + Let's Encrypt)**
2. **Adding Discord OAuth authentication**
3. **Restricting access via firewall rules**

---

## Optional: Set Up Nginx Reverse Proxy (HTTPS)

```bash
# Install nginx
sudo apt install -y nginx certbot python3-certbot-nginx

# Create config
sudo nano /etc/nginx/sites-available/discord-bot
```

**Paste:**
```nginx
server {
    listen 80;
    server_name your-domain.com;  # Or use IP

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_cache_bypass $http_upgrade;
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/discord-bot /etc/nginx/sites-enabled/

# Test config
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx

# Get SSL certificate (if you have a domain)
sudo certbot --nginx -d your-domain.com
```

Update `.env` to bind to localhost only:
```env
WEB_DASHBOARD_HOST=127.0.0.1
```

Then restart: `sudo systemctl restart discord-bot`

---

## Directory Structure (Final)

```
/home/discordbot/discord-bot/
├── bot.py
├── config.py
├── requirements.txt
├── .env
├── venv/
├── cogs/
│   ├── music.py
│   ├── logger.py
│   └── settings.py
├── dashboard/
│   └── app.py
├── utils/
│   └── embed_builder.py
└── data/
    └── settings.db
```

---

## Updating the Bot

```bash
# SSH into VPS
ssh your-user@your-vps-ip

# Stop service
sudo systemctl stop discord-bot

# Switch to bot user
sudo su - discordbot
cd discord-bot

# Pull latest changes (if using git)
git pull

# Or upload new files via SCP/SFTP

# Activate venv and update dependencies
source venv/bin/activate
pip install -r requirements.txt

# Exit and restart
exit
sudo systemctl start discord-bot
sudo systemctl status discord-bot
```

---

## Troubleshooting

### Bot won't start
```bash
# Check logs
sudo journalctl -u discord-bot -n 50

# Check if port is in use
sudo netstat -tlnp | grep 5000

# Check file permissions
ls -la /home/discordbot/discord-bot/
```

### Music not playing
```bash
# Verify FFmpeg is installed
ffmpeg -version

# Check if bot has voice permissions
# Check if bot can connect to the voice channel
```

### Permission denied errors
```bash
# Fix ownership
sudo chown -R discordbot:discordbot /home/discordbot/discord-bot

# Make sure data directory is writable
chmod 755 /home/discordbot/discord-bot/data
```

### Dashboard not accessible
```bash
# Check if service is running
sudo systemctl status discord-bot

# Check firewall
sudo ufw status

# Check if port is open
sudo netstat -tlnp | grep 5000
```

---

## Quick Reference

| Action | Command |
|--------|---------|
| Start bot | `sudo systemctl start discord-bot` |
| Stop bot | `sudo systemctl stop discord-bot` |
| Restart bot | `sudo systemctl restart discord-bot` |
| View logs | `sudo journalctl -u discord-bot -f` |
| Check status | `sudo systemctl status discord-bot` |
| Edit config | `sudo nano /home/discordbot/discord-bot/.env` |
