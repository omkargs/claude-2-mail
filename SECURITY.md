# Security Policy

## Reporting Security Issues

**DO NOT open public issues for security vulnerabilities.**

Email omkargskrishnan@gmail.com with:
- Description of vulnerability
- Steps to reproduce
- Potential impact

## Security Best Practices

- Never commit secrets to git
- Use `chmod 600` for all credential files
- Rotate App Passwords regularly
- Review Drive sharing permissions monthly
- Keep dependencies updated

## Known Limitations

- OAuth tokens stored locally (single-machine only)
- No end-to-end encryption
- Logging may contain metadata (but redacts sensitive data)
