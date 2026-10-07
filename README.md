# Aviasales MCP Server

[Русский](#русский) · [English](#english) · [MIT License](LICENSE)

Production-oriented Python MCP server for cached and live Aviasales/Travelpayouts
flight-price data. It exposes `search_flights`, `analyze_price_calendar`, and the
access-controlled `live_search_flights` tool.

> Travelpayouts Data API responses are cached market data, not guaranteed live inventory.
> Price and availability must be confirmed on Aviasales before purchase.

## Русский

### Возможности

- Streamable HTTP endpoint: `https://YOUR_DOMAIN/mcp`;
- строгая Pydantic-валидация запросов;
- Redis Streams, несколько worker-процессов, backpressure и reclaim заданий;
- retry, deadline, cache, rate limit и circuit breaker;
- пустые ответы Data API по умолчанию не кэшируются;
- опциональный живой поиск для проектов с одобренным Flight Search API;
- обязательное преобразование обычных Aviasales URL через Partner Links API;
- Bearer-аутентификация, Host/Origin allowlist, health/readiness и метрики.

### Быстрый запуск в Docker

```bash
git clone https://github.com/neurologicby/mcp-aviasales-search.git
cd mcp-aviasales-search
cp .env.example .env
nano .env
chmod 600 .env
docker compose config --quiet
docker compose up --build -d
curl http://127.0.0.1:8000/readyz
```

В `.env` обязательно замените `TRAVELPAYOUTS_TOKEN`, `TRAVELPAYOUTS_TRS`,
`TRAVELPAYOUTS_MARKER` и `MCP_BEARER_TOKEN`. Для production укажите HTTPS-домен в
`MCP_ISSUER_URL`, `MCP_RESOURCE_SERVER_URL`, `MCP_ALLOWED_HOSTS` и
`MCP_ALLOWED_ORIGINS`. Порт по умолчанию привязан только к `127.0.0.1`; внешний доступ
настраивается через HTTPS reverse proxy.

Подробные руководства:

- [Развёртывание на Ubuntu](docs/DEPLOYMENT_UBUNTU_RU.md)
- [Подключение Codex, Claude Code, VS Code, Cursor и OpenAI Agents](docs/MCP_CLIENTS_RU.md)
- [Архитектура и эксплуатация](docs/ARCHITECTURE_RU.md)

### Проверка

```bash
uv sync --extra test --python 3.13
uv run pytest --cov=aviasales_mcp --cov-report=term-missing
uv run ruff check .
uv run bandit -q -r src
docker compose config --quiet
```

## English

### Features

- Streamable HTTP endpoint at `https://YOUR_DOMAIN/mcp`;
- strict Pydantic request validation;
- Redis Streams, multiple workers, backpressure and stale-job reclaim;
- retries, deadlines, caching, distributed rate limiting and a circuit breaker;
- empty Data API responses are not cached by default;
- optional live search for projects approved for the Flight Search API;
- mandatory conversion of plain Aviasales URLs through the Partner Links API;
- bearer authentication, Host/Origin allowlists, health/readiness and metrics.

### Docker quick start

```bash
git clone https://github.com/neurologicby/mcp-aviasales-search.git
cd mcp-aviasales-search
cp .env.example .env
nano .env
chmod 600 .env
docker compose config --quiet
docker compose up --build -d
curl http://127.0.0.1:8000/readyz
```

Replace `TRAVELPAYOUTS_TOKEN`, `TRAVELPAYOUTS_TRS`, `TRAVELPAYOUTS_MARKER`, and
`MCP_BEARER_TOKEN` in `.env`. For production, set the HTTPS domain in
`MCP_ISSUER_URL`, `MCP_RESOURCE_SERVER_URL`, `MCP_ALLOWED_HOSTS`, and
`MCP_ALLOWED_ORIGINS`. The published port binds to `127.0.0.1` by default; expose it
through an HTTPS reverse proxy.

Full guides:

- [Ubuntu deployment](docs/DEPLOYMENT_UBUNTU_EN.md)
- [Connect Codex, Claude Code, VS Code, Cursor, and OpenAI Agents](docs/MCP_CLIENTS_EN.md)
- [Architecture and operations (Russian)](docs/ARCHITECTURE_RU.md)

### Validation

```bash
uv sync --extra test --python 3.13
uv run pytest --cov=aviasales_mcp --cov-report=term-missing
uv run ruff check .
uv run bandit -q -r src
docker compose config --quiet
```

## License

Released under the [MIT License](LICENSE).
