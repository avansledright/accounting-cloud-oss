# Security Policy

## The app has no built-in authentication

Accounting Cloud has **no login, no user accounts and no authorization checks**.
Anyone who can reach the web port can read, change, export and delete all of
the financial data in it, including through `/export` (a full JSON dump) and
`/import` (which replaces collections).

This is a deliberate design choice for a single-household app: authentication
belongs in front of it. You must run it behind one of:

- a reverse proxy with SSO / forward auth (Authelia, Authentik, oauth2-proxy,
  Cloudflare Access, Tailscale Serve with identity headers, and similar),
- HTTP basic auth at the proxy (at minimum, over TLS), or
- a private network only you can reach (VPN, Tailscale/WireGuard, localhost).

The bundled `docker-compose.yml` binds the web and MCP ports to `127.0.0.1` for
this reason. See [docs/deployment.md](docs/deployment.md) for proxy examples.

The **MCP server** (`mcp_server.py`) is the one exception: it requires a bearer
token (`MCP_API_TOKEN`) on every request, refuses all requests when no token is
configured, and has full write access. Treat the token like a password, serve
it only over TLS, and rotate it if it leaks.

## Other things to know

- `SECRET_KEY` protects CSRF tokens. Set a long random value; without it a
  random key is generated at each start.
- Backups (`/export` JSON, `mongodump` archives) and uploaded statements in
  `CC_STATEMENT_DIR` contain everything. Store them encrypted and off the host.
- Payroll features store employee SSNs and your EIN in MongoDB in plain text.
  Protect the database and its backups accordingly.
- Optional outbound calls: Yahoo Finance and Alpha Vantage (ticker symbols
  only), FRED (mortgage rates), and your configured LLM endpoint, which
  receives merchant descriptions and, for advisor reports, summaries of your
  finances. Point the LLM settings only at a server you trust (ideally local).
- MongoDB in the bundled compose file has no authentication and is reachable
  only on the internal Docker network. Do not publish its port.

## Reporting a vulnerability

Please report security problems privately using GitHub's
[private vulnerability reporting](../../security/advisories/new) for this
repository rather than opening a public issue. Include steps to reproduce and
the affected version or commit. You should get a response within a week.

Because the app intentionally has no authentication, "an unauthenticated user
can access data" is expected behaviour, not a vulnerability. Issues that let
someone bypass the MCP bearer token, escape a reverse-proxy sub-path, inject
script into pages, or exfiltrate data from a properly proxied deployment are
very much in scope.

## Supported versions

Only the latest release on `main` receives fixes.
