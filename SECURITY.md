# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 3.x     | :white_check_mark: |
| < 3.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it by:

1. **DO NOT** open a public GitHub issue for security vulnerabilities
2. Email the maintainers directly with details of the vulnerability
3. Include steps to reproduce the issue if possible

We will acknowledge receipt within 48 hours and aim to provide a fix within 7 days for critical issues.

## Security Considerations

- **Credentials**: Never commit `.env` files or credentials to version control
- **Session IDs**: Session IDs are bearer tokens - treat them like passwords
- **API Access**: This bridge acts with the authenticated user's Taiga permissions — run it with an account that has only the rights the assistant needs
- **Attachment URLs**: `list_attachments` returns Taiga download links that work without login; treat them as secrets
- **Logs**: credentials, tokens, request bodies and headers are never logged
