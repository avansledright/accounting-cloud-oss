# Deployment

> **The app has no built-in authentication.** Whoever can reach the web port
> can read, change, export and wipe all of your data. Run it on localhost, a
> private network/VPN, or behind a reverse proxy that authenticates users.
> Everything below assumes you do one of those.

- [Docker Compose](#docker-compose) (recommended)
- [Putting it behind a reverse proxy with authentication](#reverse-proxy-and-sso)
- [Kubernetes](#kubernetes)
- [Backups and restore](#backups-and-restore)
- [Upgrading](#upgrading)
- [Configuration reference](#configuration-reference)

## Docker Compose

Requirements: Docker Engine with the Compose plugin (v2.24 or newer).

```bash
git clone https://github.com/avansledright/accounting-cloud-oss.git
cd accounting-cloud-oss
cp .env.example .env
# Set a real secret:
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')/" .env
docker compose up -d --build
```

Open <http://localhost:5052>. The first run starts with an empty database:
create your first account on the **Accounts** page and mark it primary (the
ledger and projections follow the primary account by default). Until an
account exists the ledger uses `STARTING_BALANCE`.

To try the app with fake data instead:

```bash
docker compose exec web python scripts/seed_demo.py
```

The seeder refuses to run if any accounts exist. `--force` **deletes** every
document in the collections it seeds, so never use it on real data.

### What the stack contains

| Service | Image | Purpose | Persistent data |
|---|---|---|---|
| `web` | built from `Dockerfile` | Flask app under gunicorn (1 worker, 8 threads), port 5000 in the container, `127.0.0.1:${WEB_PORT:-5052}` on the host | `cc_statements` volume (uploaded statement CSVs) |
| `db` | `mongo:7.0` | MongoDB, internal network only, no auth | `mongo_data` volume |
| `mcp` (profile `mcp`) | same image | MCP server for AI clients, `127.0.0.1:${MCP_PORT:-8765}` | none |

The web container runs as an unprivileged user (uid 10001) and has a Docker
healthcheck against `/health`, which returns `200 {"status": "healthy"}` when
MongoDB answers a ping and `503` otherwise.

gunicorn deliberately runs a single worker: the live-price cache, report-job
status and the background threads (17:00 price refresh, advisor scheduler) live
in process memory. Threads provide the concurrency.

### Optional: MCP server

```bash
# in .env
MCP_API_TOKEN=<python3 -c 'import secrets; print(secrets.token_urlsafe(32))'>
docker compose --profile mcp up -d
```

Clients connect to `http://127.0.0.1:8765/http` with
`Authorization: Bearer <token>`. See [mcp-server.md](mcp-server.md).

### Optional: LLM features

Credit-card categorization and the advisor reports talk to any
Ollama-compatible `/api/chat` endpoint. From inside the container, an Ollama
running on the Docker host is usually reachable as
`http://host.docker.internal:11434`; on Linux add this to the `web` (and `mcp`)
service:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Or run Ollama as another Compose service and use `http://ollama:11434`. If the
endpoint is unreachable the app keeps working: categorization falls back to the
keyword rules and the AI sections are hidden. See
[credit-cards.md](credit-cards.md) and [advisor-agent.md](advisor-agent.md).

To give the advisor a household profile, mount the file and point at it:

```yaml
  web:
    volumes:
      - ./household_profile.md:/app/household_profile.md:ro
    environment:
      ADVISOR_PROFILE_FILE: /app/household_profile.md
```

## Reverse proxy and SSO

Change nothing in the Compose port binding (keep `127.0.0.1`) and run the proxy
on the same host, or put both on a shared Docker network.

The app supports being served under a sub-path. Either set
`APPLICATION_ROOT=/accounting`, or have the proxy send an `X-Script-Name:
/accounting` header; the app's `ReverseProxyMiddleware` then generates links
with that prefix and strips it from incoming paths. Allow request bodies of at
least 16 MB (statement and backup uploads).

### Caddy with forward auth (Authelia, Authentik, oauth2-proxy)

```caddyfile
accounting.example.com {
    forward_auth authelia:9091 {
        uri /api/authz/forward-auth
        copy_headers Remote-User Remote-Groups
    }
    reverse_proxy 127.0.0.1:5052
}
```

### Caddy with basic auth (minimum viable)

```caddyfile
accounting.example.com {
    basic_auth {
        # caddy hash-password
        household $2a$14$replace-with-a-bcrypt-hash
    }
    reverse_proxy 127.0.0.1:5052
}
```

### nginx with oauth2-proxy

```nginx
server {
    listen 443 ssl;
    server_name accounting.example.com;
    # ssl_certificate ...;

    client_max_body_size 16m;

    location /oauth2/ {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        auth_request /oauth2/auth;
        error_page 401 = /oauth2/sign_in;

        proxy_pass http://127.0.0.1:5052;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Serving under a sub-path with nginx:

```nginx
    location /accounting/ {
        auth_request /oauth2/auth;
        proxy_pass http://127.0.0.1:5052;          # no trailing slash: keep the prefix
        proxy_set_header X-Script-Name /accounting;
        proxy_set_header Host $host;
    }
```

### Traefik (Docker labels) with a ForwardAuth middleware

```yaml
  web:
    labels:
      - traefik.enable=true
      - traefik.http.routers.accounting.rule=Host(`accounting.example.com`)
      - traefik.http.routers.accounting.entrypoints=websecure
      - traefik.http.routers.accounting.tls=true
      - traefik.http.routers.accounting.middlewares=sso@file
      - traefik.http.services.accounting.loadbalancer.server.port=5000
```

where `sso` is a ForwardAuth middleware for your identity provider. (When
Traefik routes to the container directly you can drop the `ports:` mapping.)

### The MCP server behind a proxy

MCP clients generally cannot complete an interactive SSO login, so the MCP
server authenticates with its own bearer token. If you expose it, give it a
separate host or path that **bypasses SSO but is TLS-only**, and keep the
token secret. Do not add response buffering in front of it (it breaks the
legacy SSE endpoint). If the proxy serves it under a prefix that it strips,
set `MCP_MOUNT_PATH` to that prefix so the SSE endpoint advertises correct
URLs. The preferred `/http` endpoint is stateless and works behind any load
balancer.

### Without a proxy: VPN only

If only your household needs access, a VPN such as WireGuard or Tailscale is
the simplest secure option: bind the port to the VPN interface address instead
of `127.0.0.1` and do not expose it anywhere else.

## Kubernetes

Generic manifests live in [`deploy/kubernetes/`](../deploy/kubernetes):

| File | Contents |
|---|---|
| `namespace.yaml` | `accounting` namespace |
| `secret.example.yaml` | template for `accounting-secret` (`SECRET_KEY`, `MCP_API_TOKEN`, `ALPHA_VANTAGE_KEY`, `OLLAMA_API_KEY`) |
| `configmap.yaml` | non-secret settings (Mongo URI, LLM endpoints, `APPLICATION_ROOT`, ...) |
| `mongodb.yaml` | single-member MongoDB StatefulSet with a 5 Gi PVC |
| `deployment.yaml` | web Deployment (1 replica, non-root), statements PVC, Service |
| `ingress.yaml` | Ingress template with **authentication options to fill in** |
| `mcp.yaml` | optional MCP server Deployment + Service |
| `backup-cronjob.yaml` | nightly `mongodump` into a PVC, 14 archives retained |
| `kustomization.yaml` | applies everything except ingress and MCP |

```bash
# 1. Build and push the image somewhere your cluster can pull from
docker build -t registry.example.com/accounting-cloud:0.1.0 .
docker push registry.example.com/accounting-cloud:0.1.0
#    then set that image in deployment.yaml and mcp.yaml

# 2. Secret
kubectl create namespace accounting
kubectl -n accounting create secret generic accounting-secret \
  --from-literal=SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  --from-literal=MCP_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --from-literal=ALPHA_VANTAGE_KEY= --from-literal=OLLAMA_API_KEY=

# 3. Everything else
kubectl apply -k deploy/kubernetes

# 4. Try it without exposing it
kubectl -n accounting port-forward svc/accounting 5052:5000
```

Only add `ingress.yaml` after you have filled in one of its authentication
options (oauth2-proxy / forward auth, basic auth, or your own). If you serve
the app under a sub-path, set `APPLICATION_ROOT` in the ConfigMap and prefix
the probe paths in `deployment.yaml`.

Scaling notes: the web Deployment runs one replica with a `Recreate` strategy
because the statements volume is ReadWriteOnce and some state is in memory.
To run several replicas, use a ReadWriteMany volume, keep `SECRET_KEY` shared
(it already comes from the Secret), and accept that each replica runs its own
price-refresh and advisor-scheduler threads (set `ADVISOR_ENABLED=true` on one
replica only, or not at all). The MCP server is stateless and scales freely.
For MongoDB high availability, run a replica set or an operator and point
`MONGO_URI` at it.

## Backups and restore

Everything lives in MongoDB, plus the raw statement CSVs in
`CC_STATEMENT_DIR`. Backups contain your whole financial life: encrypt them
and keep a copy off the host.

### mongodump (recommended)

Docker Compose:

```bash
# Backup
docker compose exec -T db mongodump --db accounting_cloud --gzip --archive \
  > accounting-$(date +%F).archive.gz

# Restore (replaces existing collections)
docker compose exec -T db mongorestore --gzip --archive --drop \
  < accounting-2026-01-01.archive.gz
```

Cron example (daily at 03:00, keep 14):

```cron
0 3 * * * cd /srv/accounting-cloud-oss && docker compose exec -T db mongodump --db accounting_cloud --gzip --archive > backups/accounting-$(date +\%F).archive.gz && ls -1t backups/accounting-*.archive.gz | tail -n +15 | xargs -r rm -f
```

Kubernetes: `backup-cronjob.yaml` does the same nightly into the
`accounting-backups` PVC. Restore by running a pod with the `mongo:7.0` image
that mounts the PVC and runs `mongorestore --uri=mongodb://mongodb:27017/
--gzip --drop --archive=/backups/<file>`.

Statement CSVs: back up the `cc_statements` volume too if you want to keep the
originals (`docker run --rm -v accounting-cloud-oss_cc_statements:/d -v
"$PWD":/out alpine tar czf /out/cc_statements.tgz -C /d .`).

### Application-level JSON export

**Admin → Export** (or `GET /export`) downloads every collection as Extended
JSON; **Admin → Import** (`POST /import`) restores such a file, dropping and
replacing each collection present in it. New collections are included
automatically. `scripts/backup-accounting.sh` automates the download with
retention (`BACKUP_URL`, `BACKUP_DIR`, `KEEP_COUNT`, optional `BACKUP_HEADER`
for proxies that accept a machine token). If your proxy requires SSO, either
run the script on the host against `http://127.0.0.1:5052/export` or prefer
`mongodump`.

## Upgrading

```bash
git pull
docker compose up -d --build
```

Take a backup first. Schema changes are additive: new fields are read with
defaults, and one-time data migrations are recorded in the `app_migrations`
collection so they run once.

## Configuration reference

All configuration is through environment variables (in Docker Compose, the
`.env` file). See [`.env.example`](../.env.example) for a commented copy.

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `SECRET_KEY` | random per start | web | Flask secret for sessions/CSRF. Always set it. |
| `MONGO_URI` | `mongodb://localhost:27017/` | web, mcp | MongoDB connection string (Compose sets `mongodb://db:27017/`). |
| `MONGO_DB_NAME` | `accounting_cloud` | web, mcp, seeder | Database name. |
| `APPLICATION_ROOT` | `/` | web | Sub-path prefix behind a proxy (alternative: `X-Script-Name` header). |
| `STARTING_BALANCE` | `0` | web | Ledger balance used only until the first account exists. |
| `FLASK_DEBUG` | `false` | web | Debug mode when running `python app.py`. Never in production. |
| `TZ` | `UTC` | web, mcp | Time zone for schedules and for "today" in projections. |
| `CC_STATEMENT_DIR` | `./data/cc_statements` | web | Where raw statement CSVs are stored. |
| `ESCROW_PROPERTY_TAX_MONTHS` | `6,12` | web | Months the escrow simulation pays property tax (annual bill split evenly). |
| `ESCROW_INSURANCE_MONTH` | `1` | web | Month the escrow simulation pays homeowner's insurance. |
| `ALPHA_VANTAGE_KEY` | empty | web | Fallback market-data key (Yahoo needs none). |
| `OLLAMA_URL` | `http://localhost:11434` | web | Ollama-compatible endpoint for categorization and spending tips. |
| `OLLAMA_MODEL` | `granite4.2:3b` | web | Model for categorization. |
| `OLLAMA_API_KEY` | empty | web | Optional bearer token for that endpoint. |
| `CC_LLM_ENABLED` | `true` | web | Enable the LLM categorization pass. |
| `CC_LLM_TIMEOUT` | `60` | web | Seconds per categorization request. |
| `ADVISOR_ENABLED` | `false` | web | Run the weekly/monthly advisor report scheduler. |
| `AGENT_OLLAMA_HOST` | `http://localhost:11434` | web, mcp | Endpoint for advisor reports. |
| `AGENT_OLLAMA_MODEL` | `gemma4:e4b` | web, mcp | Model for advisor reports. |
| `AGENT_OLLAMA_API_KEY` | empty | web, mcp | Optional bearer token for that endpoint. |
| `AGENT_OLLAMA_TIMEOUT` | `3600` | web, mcp | Seconds to wait for a report. |
| `ADVISOR_PROFILE_FILE` | empty | web, mcp | Path to your household profile Markdown. |
| `MCP_API_TOKEN` | empty (refuses all) | mcp | Bearer token required by the MCP server. |
| `MCP_HOST` | `0.0.0.0` | mcp | Bind address. |
| `MCP_PORT` | `8765` | mcp | Port (Compose also uses it for the host port). |
| `MCP_MOUNT_PATH` | empty | mcp | Public prefix when a proxy strips one (legacy SSE URLs). |
| `WEB_PORT` | `5052` | Compose | Host port for the web app (bound to 127.0.0.1). |
