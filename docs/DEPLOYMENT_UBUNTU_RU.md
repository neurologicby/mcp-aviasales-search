# Развёртывание Aviasales MCP Server на Ubuntu

Руководство рассчитано на Ubuntu Server 22.04/24.04, отдельный DNS-домен и Docker
Compose. Production endpoint: `https://mcp.example.com/mcp`.

## 1. Что потребуется

- сервер с 2 vCPU, 2–4 ГБ RAM и 20 ГБ SSD для начальной нагрузки;
- DNS A/AAAA-запись `mcp.example.com`, направленная на сервер;
- аккаунт Travelpayouts, проект с доступом к Aviasales, API-ключ, `trs` и `marker`;
- открытые входящие порты 22, 80 и 443.

Redis не должен публиковаться в интернет. MCP-контейнер по умолчанию привязан к
`127.0.0.1:8000`; наружу его публикует Nginx с TLS.

## 2. Установка Docker из официального репозитория

Следуйте актуальной [инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Краткая последовательность:

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

Команды ниже используют `sudo docker`. Добавление пользователя в группу `docker` даёт ему
практически root-доступ; делайте это только осознанно.

## 3. Получение проекта

```bash
sudo install -d -m 0750 -o "$USER" -g "$USER" /opt/aviasales-mcp
git clone https://github.com/neurologicby/mcp-aviasales-search.git /opt/aviasales-mcp
cd /opt/aviasales-mcp
cp .env.example .env
chmod 600 .env
```

## 4. Настройка `.env`

Создайте MCP-токен:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Заполните `.env`:

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
EMPTY_CACHE_TTL_SECONDS=0
PARTNER_LINKS_PER_MINUTE=90
MCP_REQUESTS_PER_MINUTE=600
LIVE_SEARCH_TIMEOUT_SECONDS=65
LIVE_SEARCH_POLL_INTERVAL_SECONDS=2
LIVE_SEARCH_REQUESTS_PER_HOUR=100
LIVE_SEARCH_LOCALE=ru
LIVE_SEARCH_MARKET=RU
```

`TRAVELPAYOUTS_TRS` и `TRAVELPAYOUTS_MARKER` — положительные целые числа. Не помещайте
`.env` в Git, резервные копии без шифрования или сообщения поддержки.

`live_search_flights` заработает только после отдельного одобрения Flight Search API со стороны
Travelpayouts. Вызывающий gateway должен передавать реальные `X-User-IP`, `User-Agent` и
`Referer` конечного пользователя; не подставляйте адрес сервера или фиктивные значения.

## 5. Запуск

```bash
cd /opt/aviasales-mcp
sudo docker compose config --quiet
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Ожидаемый readiness: `status=ready`, `redis=true`, `queue=true`, `workers>0`,
`provider=true`, `authentication=true`.

## 6. Nginx и HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo nano /etc/nginx/sites-available/aviasales-mcp
```

Конфигурация:

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

Активируйте сайт и сертификат:

```bash
sudo ln -s /etc/nginx/sites-available/aviasales-mcp /etc/nginx/sites-enabled/aviasales-mcp
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d mcp.example.com
curl --fail https://mcp.example.com/readyz
```

Production-системы должны использовать HTTPS; это также рекомендует
[документация Ubuntu по Nginx](https://ubuntu.com/server/docs/how-to/web-services/configure-nginx/).

## 7. Firewall

Сначала разрешите SSH, чтобы не потерять доступ:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Порт 8000 и Redis 6379 открывать не нужно. Docker предупреждает, что опубликованные порты
могут обходить часть правил firewall; локальная привязка `127.0.0.1` исключает внешний доступ
к MCP в обход Nginx. См. [Docker security](https://docs.docker.com/engine/security/) и
[Ubuntu firewall](https://ubuntu.com/server/docs/security-firewall/).

## 8. Контрольная проверка

```bash
curl -i -X POST https://mcp.example.com/mcp
# Ожидается 401 без Authorization.

curl --fail https://mcp.example.com/healthz
curl --fail https://mcp.example.com/readyz
sudo docker compose logs --tail=100 server worker redis
```

После настройки клиента убедитесь, что он видит только `search_flights` и
`analyze_price_calendar`. Затем выполните один canary-поиск и проверьте полученный переход в
статистике Travelpayouts. Автоматические тесты не доказывают атрибуцию реального аккаунта.

## 9. Эксплуатация

Обновление:

```bash
cd /opt/aviasales-mcp
git pull --ff-only
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
curl --fail https://mcp.example.com/readyz
```

Масштабирование workers:

```bash
sudo docker compose up -d --scale worker=4
```

Логи и метрики:

```bash
sudo docker compose logs -f --tail=100 server worker
curl --fail https://mcp.example.com/metrics
```

Настройте внешнее наблюдение за `/readyz`, глубиной очереди, 429/5xx Travelpayouts,
таймаутами и свободной памятью Redis. Для нескольких серверов используйте управляемый Redis с
TLS, аутентификацией, резервным копированием и запретом публичного доступа.

## 10. Ограничения production-контура

- статический Bearer предназначен для закрытого service-to-service доступа;
- для публичного multi-tenant доступа нужен OAuth 2.1/JWT verifier и per-client rate limit;
- Data API содержит кэш цен, а не гарантированную live-доступность;
- секреты рекомендуется передавать через secret manager, а не хранить постоянно в `.env`.
