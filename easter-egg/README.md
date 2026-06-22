# Google Drive MCP (Easter Egg)

Bonus MCP server for Google Drive. List, read, upload, download, search, share — all from your AI agent.

## Tools

| Tool | What it does |
|------|-------------|
| `drive_list` | List files/folders (filter by folder, type, query) |
| `drive_search` | Search files by name or content |
| `drive_read` | Read file content (Docs/Sheets exported to text/CSV) |
| `drive_download` | Download file to local path |
| `drive_upload` | Upload local file to Drive |
| `drive_create` | Create new Doc, Sheet, or Folder |
| `drive_move` | Move file to a folder |
| `drive_share` | Share file with an email |
| `drive_permissions` | List or revoke permissions |
| `drive_trash` | Move file to trash |

## Prerequisites

- Python 3.10+
- A Google account
- An MCP-compatible client

## Setup

### Auth Pipeline (Overview)

Drive uses **Google OAuth2** — a browser-based consent flow that saves a token file. One-time setup, then it auto-refreshes.

### 1. Install Dependencies

```bash
pip install mcp google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

### 2. Create a Google Cloud Project

Follow these steps **in order**:

**Step A — Create project + enable APIs:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project** → name it → **Create**
3. Select your new project
4. Go to **APIs & Services → Library**
5. Search and enable each:
   - **Google Drive API** → **Enable**
   - **Google Docs API** → **Enable**
   - **Google Sheets API** → **Enable**

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

**Note:** If you already set up the Gmail+Calendar MCP, you can reuse the same `credentials.json` and config directory. Skip to step 4.

### 3. Configure

```bash
mkdir -p ~/.config/claude-2-mail

# If you already set up gmail-calendar MCP, you can reuse the same
# credentials.json and config directory. Just copy the template:
cp config/credentials.json.template ~/.config/claude-2-mail/credentials.json
```

Paste the downloaded OAuth JSON into `~/.config/claude-2-mail/credentials.json`.

```bash
chmod 600 ~/.config/claude-2-mail/credentials.json
```

### 4. Register in Your MCP Client

**Claude Code** (`~/.mcp.json`):

```json
{
  "mcpServers": {
    "google-drive": {
      "command": "python",
      "args": ["/path/to/claude-2-mail/easter-egg/drive_mcp.py"],
      "env": {
        "DRIVE_CONFIG_DIR": "/home/YOUR_USER/.config/claude-2-mail"
      }
    }
  }
}
```

### 5. First Run

First use opens a browser for OAuth approval. One-time — token saves to `~/.config/claude-2-mail/drive_token.json`.

## OAuth Scopes (Narrow)

Only two scopes requested:
- `drive.file` — access files you create or open with this app
- `documents.readonly` — read Google Docs content

Does NOT request full Drive access.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `invalid_client` | Check credentials.json is Desktop app type. |
| `access_blocked` | Add your email as Test user in OAuth consent screen. |
| `Token expired` | Delete `drive_token.json`, re-auth. |
| `File not found` | Use file_id from `drive_list`, not the share link. |
| `Destination must be under home or /tmp` | Downloads only allowed to home or /tmp paths. |
| Missing pip libs | `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib` |

## Security

- OAuth tokens stored chmod 600
- Logging redacts emails/tokens at INFO level
- Downloads restricted to home or /tmp
- File locking prevents auth race conditions
- Narrow OAuth scopes only

## License

MIT.
