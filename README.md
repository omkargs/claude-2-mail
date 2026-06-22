# claude-2-mail

Local MCP server for Gmail + Google Calendar. Lets your AI agent read/send email and manage your calendar — runs locally via stdio.

## Tools

| Tool | What it does |
|------|-------------|
| `mail_list` | List recent emails (folder, count, unread filter) |
| `mail_read` | Read full email by UID |
| `mail_search` | Search by sender, subject, body, date range |
| `mail_draft` | Draft a reply or new email |
| `mail_send` | Send an approved email |
| `mail_auto_reply` | Auto-reply to whitelisted senders |
| `calendar_list` | List upcoming events |
| `calendar_create` | Create a new event |
| `calendar_update` | Update or delete an event |

## Prerequisites

- Python 3.10+
- A Google account
- An MCP-compatible client (Claude Code, Cursor, etc.)

## Setup

### Auth Pipeline (Overview)

There are **two separate auth systems**:

| System | Used for | How it works |
|--------|----------|-------------|
| **Gmail App Password** | IMAP (read) + SMTP (send) | 16-char password from Google Account settings |
| **Google OAuth2** | Calendar API | Browser-based consent flow, saves a token file |

You need both. App Password for email, OAuth2 for calendar.

### 1. Install Dependencies

```bash
pip install mcp google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

### 2. Create a Gmail App Password (for Email)

Gmail uses IMAP/SMTP with an App Password — NOT your real Google password.

**Steps:**
1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (required — App Passwords won't work without it)
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Select app: **Mail**, device: **Other (Custom name)** → type "claude-2-mail"
5. Click **Generate** → copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

**Why:** Google blocks "less secure apps". App Passwords are per-app tokens that bypass this.

### 3. Create a Google Cloud Project (for Calendar)

Calendar needs OAuth2. This is a multi-step process — follow in order:

**Step A — Create project + enable API:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project** → name it → **Create**
3. Select your new project
4. Go to **APIs & Services → Library**
5. Search **"Google Calendar API"** → click it → **Enable**

**Step B — Configure OAuth consent screen:**
1. Go to **APIs & Services → OAuth consent screen**
2. User type: **External** → **Create**
3. Fill in: App name, User support email, Developer contact email
4. **Save and Continue** through scopes (add nothing) → **Back to Dashboard**
5. Under **Test users** → **Add users** → add your own email address

**Why Test users:** Until your app is published, only test users can authorize it. Without this step, OAuth will fail with "access_blocked".

**Step C — Create OAuth credentials:**
1. Go to **APIs & Services → Credentials**
2. **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app** → name it → **Create**
4. Click **Download JSON** → save the file

**Why Desktop app:** MCP servers run locally. "Desktop app" type doesn't need a redirect URI — it uses localhost callback.

### 4. Configure

```bash
mkdir -p ~/.config/claude-2-mail

cp config/config.template.json ~/.config/claude-2-mail/config.json
cp config/.secrets.template ~/.config/claude-2-mail/.secrets
cp config/credentials.json.template ~/.config/claude-2-mail/credentials.json
```

Edit each file:

**`~/.config/claude-2-mail/config.json`** — your email, display name, settings.

**`~/.config/claude-2-mail/.secrets`** — your App Password:
```
export GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
```

**`~/.config/claude-2-mail/credentials.json`** — paste the downloaded OAuth JSON.

Lock permissions:
```bash
chmod 600 ~/.config/claude-2-mail/config.json
chmod 600 ~/.config/claude-2-mail/.secrets
chmod 600 ~/.config/claude-2-mail/credentials.json
```

### 5. Source the Secrets

Add to `~/.bashrc` or `~/.zshrc`:

```bash
[ -f ~/.config/claude-2-mail/.secrets ] && source ~/.config/claude-2-mail/.secrets
```

Reload: `source ~/.bashrc`

### 6. Register in Your MCP Client

**Claude Code** (`~/.mcp.json`):

```json
{
  "mcpServers": {
    "gmail-calendar": {
      "command": "python",
      "args": ["/path/to/claude-2-mail/gmail_calendar_mcp.py"],
      "env": {
        "GMAIL_MAIL_CONFIG": "/home/YOUR_USER/.config/claude-2-mail/config.json"
      }
    }
  }
}
```

### 7. First Run

First Calendar use opens a browser for OAuth approval. One-time — token saves for future.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `AUTHENTICATIONFAILED` (IMAP) | Regenerate App Password. 2FA must be on. |
| `invalid_client` (OAuth) | Check credentials.json is Desktop app type. |
| `access_blocked` (OAuth) | Add your email as Test user in OAuth consent screen. |
| `Token expired` | Delete `calendar_token.json`, re-auth. |
| `Config not found` | Check `GMAIL_MAIL_CONFIG` env var path. |
| `CredentialsWithRegionalAccessBoundary.refresh() missing argument` | Update google-auth: `pip install --upgrade google-auth` |
| Missing pip libs | `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib` |

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Security

- App Password in `.secrets` (chmod 600), NOT in config.json or shell rc
- OAuth tokens stored chmod 600
- Logging redacts emails/tokens/passwords at INFO level
- IMAP input escaped to prevent injection
- Narrow OAuth scopes only

## License

MIT.
