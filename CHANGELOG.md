# Changelog

## [0.1.0] - 2026-06-22

### Added
- Gmail IMAP/SMTP support with App Password auth
- Google Calendar OAuth2 integration
- Google Drive MCP server (easter-egg)
- Auto-reply functionality with sender whitelist
- Logging redaction for sensitive data (emails, tokens, passwords)
- IMAP injection prevention via input escaping
- Narrow OAuth scopes (drive.file, documents.readonly, calendar)
- File locking for OAuth auth race condition prevention
- pyproject.toml for pip installability
- MCP distribution metadata (server.json, glama.json, server-card.json)
- GitHub Actions CI workflow
- Basic test suite

### Security
- App Password stored in env var only (GMAIL_APP_PASSWORD)
- OAuth tokens stored with chmod 600
- Password field removed from config template
- Clear error messages for missing credentials
- Path validation on file downloads (home or /tmp only)
- Email validation on share/send operations
