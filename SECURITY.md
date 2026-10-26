# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Include what you found, the affected version or commit, steps to reproduce, and the impact you expect. Reports are acknowledged as soon as possible, and fixes are coordinated with the reporter before public disclosure.

## Supported versions

Only the latest commit on `main` receives security fixes.

## Scope and deployment model

BAND_BOTS is self-hosted. Operators are responsible for their deployment, including:

- a strong, stable `APP_SECRET` and `BAND_ENV=production` (see [Operations](docs/operations.md#application-secret-and-sessions));
- keeping `.env`, databases, checkpoints, document storage, and `mcp_servers.json` private;
- running one API process and one worker, as described in [deployment limits](docs/reference.md#deployment-limits).

All channels belong to one shared workspace. Channel permissions are access controls, not tenant isolation. Agents act with the credentials and connected apps an administrator grants them, and cloud computers cannot technically block every click; review the approval and computer-control policies in [Configuration and operations](docs/reference.md) before granting access to sensitive accounts.
