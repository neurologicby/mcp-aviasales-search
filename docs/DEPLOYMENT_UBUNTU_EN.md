# Deploy Aviasales MCP Server on Ubuntu

This guide targets Ubuntu Server 22.04/24.04, a dedicated DNS name, and Docker Compose.
The production endpoint is `https://mcp.example.com/mcp`.

## 1. Prerequisites

- 2 vCPU, 2–4 GB RAM, and 20 GB SSD for an initial deployment;
- an A/AAAA record for `mcp.example.com`;
- a Travelpayouts project authorized for Aviasales, plus API token, `trs`, and `marker`;
- inbound ports 22, 80, and 443.

Redis must not be internet-accessible. The MCP port binds to `127.0.0.1:8000` by default and
is exposed only through an HTTPS reverse proxy.

## 2. Install Docker

Use Docker's current [Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/):

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

source /etc/os-release
echo "Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

The commands below retain `sudo`. Membership in the `docker` group effectively grants root-level
control and should be enabled only deliberately.

## 3. Clone and configure

```bash
sudo install -d -m 0750 -o "$USER" -g "$USER" /opt/aviasales-mcp
git clone https://github.com/neurologicby/mcp-aviasales-search.git /opt/aviasales-mcp
cd /opt/aviasales-mcp
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put the generated value and the real Travelpayouts credentials in `.env`:

```dotenv
TRAVELPAYOUTS_TOKEN=replace_with_real_api_token
TRAVELPAYOUTS_TRS=123456
TRAVELPAYOUTS_MARKER=654321
AFFILIATE_SUB_ID=mcp-flight-search
AFFILIATE_SHORTEN=false

MCP_BEARER_TOKEN=replace_with_generated_random_value
MCP_ISSUER_URL=https://mcp.example.com
MCP_RESOURCE_SERVER_URL=https://mcp.example.com/mcp
MCP_ALLOWED_HOSTS=mcp.example.com
MCP_ALLOWED_ORIGINS=https://mcp.example.com

BIND_ADDRESS=127.0.0.1
PORT=8000
WORKER_CONCURRENCY=20
QUEUE_MAX_LENGTH=10000
JOB_TIMEOUT_SECONDS=25
CACHE_TTL_SECONDS=300
PARTNER_LINKS_PER_MINUTE=90
MCP_REQUESTS_PER_MINUTE=600
```

`TRAVELPAYOUTS_TRS` and `TRAVELPAYOUTS_MARKER` are positive integers. Never commit `.env` or
copy it into unencrypted backups or support messages.

## 4. Start the stack

```bash
cd /opt/aviasales-mcp
sudo docker compose config --quiet
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Readiness should report `status=ready`, Redis and queue availability, at least one worker,
provider configuration, and authentication.

## 5. Nginx and TLS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo nano /etc/nginx/sites-available/aviasales-mcp
```

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name mcp.example.com;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/aviasales-mcp /etc/nginx/sites-enabled/aviasales-mcp
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d mcp.example.com
curl --fail https://mcp.example.com/readyz
```

Production services should use HTTPS; see the official
[Ubuntu Nginx guide](https://ubuntu.com/server/docs/how-to/web-services/configure-nginx/).

## 6. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Do not open ports 8000 or 6379. Docker-published ports can interact unexpectedly with firewall
rules, so the localhost-only bind prevents bypassing Nginx. See
[Docker security](https://docs.docker.com/engine/security/) and
[Ubuntu firewall](https://ubuntu.com/server/docs/security-firewall/).

## 7. Acceptance checks

```bash
curl -i -X POST https://mcp.example.com/mcp
# Expected: 401 without Authorization.

curl --fail https://mcp.example.com/healthz
curl --fail https://mcp.example.com/readyz
sudo docker compose logs --tail=100 server worker redis
```

After configuring a client, confirm that it discovers only `search_flights` and
`analyze_price_calendar`. Run one canary search and verify the click in the Travelpayouts
dashboard. Mocked tests cannot prove attribution for a real partner account.

## 8. Operations

Update:

```bash
cd /opt/aviasales-mcp
git pull --ff-only
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
curl --fail https://mcp.example.com/readyz
```

Scale workers and inspect logs:

```bash
sudo docker compose up -d --scale worker=4
sudo docker compose logs -f --tail=100 server worker
curl --fail https://mcp.example.com/metrics
```

Monitor readiness, queue depth, upstream 429/5xx responses, timeouts, and Redis memory. For a
multi-host installation, use managed Redis with TLS, authentication, backups, and no public
network exposure.

## 9. Production boundaries

- the static bearer token is intended for private service-to-service access;
- public multi-tenant deployments require an OAuth 2.1/JWT verifier and per-client rate limits;
- Data API prices are cached market observations, not guaranteed live inventory;
- prefer a secret manager over long-term `.env` storage.
