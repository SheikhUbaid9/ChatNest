# MCP Inbox — Real API Setup Guide

Complete instructions for connecting Gmail, Slack, and Telegram.
The app runs in Demo Mode without any keys — add them one at a time.

---

## Quick Start (Demo Mode)

```bash
cd mcp-inbox
pip install -r requirements.txt
python main.py --ui          # UI at http://localhost:8000
python main.py               # MCP stdio (for Claude Desktop)
```

---

## Multi-User Web OAuth (ChatNest Connect)

This branch adds per-user login sessions and per-user Gmail/Slack tokens in SQLite.
`data/token.json` is no longer the primary auth model for the UI flow.

Configure these in `.env`:

```bash
APP_BASE_URL=http://localhost:8000
AUTH_ENCRYPTION_KEY=...                         # Fernet key recommended
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
SLACK_OAUTH_CLIENT_ID=...
SLACK_OAUTH_CLIENT_SECRET=...
SLACK_OAUTH_REDIRECT_URI=http://localhost:8000/auth/slack/callback
```

Then run:

```bash
python -m uvicorn ui.server:app --host 0.0.0.0 --port 8000
```

In ChatNest:
1. Sign in with email/password (auto-register on first sign-in)
2. Click the Gmail or Slack status pill
3. Complete provider consent
4. You return to ChatNest with the provider connected for your own user account

---

## Gmail — OAuth2 Setup

### 1. Create a Google Cloud project

1. Go to https://console.cloud.google.com
2. Click **New Project** → name it `mcp-inbox` → Create
3. Select the project from the top dropdown

### 2. Enable the Gmail API

1. Go to **APIs & Services → Library**
2. Search `Gmail API` → click it → **Enable**

### 3. Configure OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. Choose **External** → Create
3. Fill in:
   - App name: `MCP Inbox`
   - User support email: your Gmail address
   - Developer contact: your Gmail address
4. Click **Save and Continue** through all steps
5. On **Test users** → Add your Gmail address
6. Click **Back to Dashboard**

### 4. Create OAuth2 credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name: `MCP Inbox Desktop`
5. Click **Create**
6. Click **Download JSON** → save as `credentials.json` in `mcp-inbox/`

### 5. Configure .env

```bash
# In mcp-inbox/.env
GMAIL_CREDENTIALS_PATH=./credentials.json
GMAIL_TOKEN_PATH=./token.json
```

### 6. First run (OAuth browser flow)

```bash
cd mcp-inbox
python test_real_apis.py --gmail
# A browser window opens → sign in → grant permissions
# token.json is created automatically
```

### Scopes requested
- `gmail.readonly` — read emails
- `gmail.send`     — send replies
- `gmail.modify`   — mark as read

---

## Slack — Bot Token Setup

### 1. Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App → From scratch**
3. App Name: `MCP Inbox`
4. Pick your workspace → Create App

### 2. Add Bot Token Scopes

1. Go to **OAuth & Permissions** (left sidebar)
2. Scroll to **Scopes → Bot Token Scopes**
3. Add these scopes:

| Scope | Purpose |
|---|---|
| `channels:history` | Read public channel messages |
| `channels:read` | List channels |
| `chat:write` | Send messages |
| `groups:history` | Read private channel messages |
| `groups:read` | List private channels |
| `im:history` | Read DM messages |
| `im:read` | List DMs |
| `users:read` | Resolve user names |

### 3. Install to workspace

1. Scroll up on **OAuth & Permissions**
2. Click **Install to Workspace** → Allow
3. Copy the **Bot User OAuth Token** (starts with `xoxb-`)

### 4. Invite bot to channels

In Slack, for each channel you want to read:
```
/invite @MCP Inbox
```

### 5. Configure .env

```bash
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_DEFAULT_CHANNEL=general
```

### 6. Test

```bash
python test_real_apis.py --slack
```

---

## Telegram — Bot Token Setup

### 1. Create a bot with @BotFather

1. Open Telegram → search `@BotFather`
2. Send `/newbot`
3. Follow prompts:
   - Bot name: `MCP Inbox Bot`
   - Username: `mcp_inbox_YOURNAME_bot` (must end in `bot`)
4. BotFather replies with your token:
   ```
   Use this token to access the HTTP API:
   1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
   ```

### 2. Configure .env

```bash
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
```

### 3. Send a test message to your bot

1. Search for your bot username in Telegram
2. Click **Start** or send any message
3. Your message will appear in the inbox

### 4. Test

```bash
python test_real_apis.py --telegram
```

### Note on Telegram bots
Bots can only receive messages that users send **directly to the bot**
or in groups where the bot has been added. The bot cannot read arbitrary
channels unless it's a member.

---

## Running with Real Keys

After adding credentials, start the server:

```bash
cd mcp-inbox
python main.py --ui
# → http://localhost:8000
```

The **Demo** badge disappears for each platform as keys are added.
Status pills turn green (●) when a platform is connected live.

---

## MCP Integration with Claude

Add to `~/.claude/claude_desktop_config.json` (Claude Desktop) or
configure via Claude.ai MCP settings:

```json
{
  "mcpServers": {
    "mcp-inbox": {
      "command": "python",
      "args": ["/full/path/to/mcp-inbox/main.py"],
      "cwd": "/full/path/to/mcp-inbox"
    }
  }
}
```

Then in Claude, you can say:
- *"Check my unread Gmail and summarize the most important ones"*
- *"Reply to Sarah's email about the budget review"*
- *"What's happening in the #dev Slack channel?"*
- *"Send a message to #general: standup in 5 min"*
- *"Summarize my Telegram messages from today"*

---

## Force Mock Mode

To always use demo data regardless of configured keys:

```bash
# In .env
FORCE_MOCK=true
```

---

## Verify All APIs

```bash
python test_real_apis.py          # all platforms
python test_real_apis.py --gmail
python test_real_apis.py --slack
python test_real_apis.py --telegram
```
