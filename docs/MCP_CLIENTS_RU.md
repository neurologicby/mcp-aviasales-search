# Подключение Aviasales MCP к AI-агентам

## Общие параметры

- URL: `https://mcp.example.com/mcp`
- транспорт: Streamable HTTP
- заголовок: `Authorization: Bearer <MCP_BEARER_TOKEN>`
- инструменты: `search_flights`, `analyze_price_calendar`, `live_search_flights`

`MCP_BEARER_TOKEN` — секрет доступа к вашему MCP, а не API-ключ Travelpayouts. Никогда не
передавайте клиентам `TRAVELPAYOUTS_TOKEN`: он остаётся только на сервере.

Для Linux/macOS:

```bash
export AVIASALES_MCP_TOKEN='значение-MCP_BEARER_TOKEN'
```

Для Windows PowerShell:

```powershell
$env:AVIASALES_MCP_TOKEN = "значение-MCP_BEARER_TOKEN"
```

## Codex CLI и Codex IDE

Codex CLI и IDE используют общий `~/.codex/config.toml`. Добавьте:

```toml
[mcp_servers.aviasales]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "AVIASALES_MCP_TOKEN"
enabled_tools = ["search_flights", "analyze_price_calendar", "live_search_flights"]
```

Проверка:

```bash
codex mcp list
```

Перезапустите Codex и попросите: «Используй `search_flights`, чтобы найти цены MOW–LED».
Формат удалённого Streamable HTTP сервера описан в
[официальной документации OpenAI](https://developers.openai.com/learn/docs-mcp).

## Claude Code

Создайте пользовательский или проектный `.mcp.json`:

```json
{
  "mcpServers": {
    "aviasales": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${AVIASALES_MCP_TOKEN}"
      }
    }
  }
}
```

Проверка:

```bash
claude mcp list
```

В Claude Code выполните `/mcp`. Claude Code поддерживает переменные окружения в URL и headers;
см. [официальную MCP-документацию Anthropic](https://docs.anthropic.com/en/docs/mcp).

## VS Code / GitHub Copilot Agent Mode

Откройте `MCP: Open User Configuration` или создайте `.vscode/mcp.json`:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "aviasales-token",
      "description": "Aviasales MCP bearer token",
      "password": true
    }
  ],
  "servers": {
    "aviasales": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${input:aviasales-token}"
      }
    }
  }
}
```

Запустите `MCP: List Servers`, включите сервер, затем в Copilot Chat выберите Agent mode.
VS Code сохраняет password input в защищённом хранилище. Формат подтверждён
[официальной документацией VS Code](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

## Cursor

Создайте глобальный `~/.cursor/mcp.json` или проектный `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "aviasales": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_MCP_BEARER_TOKEN"
      }
    }
  }
}
```

Используйте глобальный файл, не добавляйте его в Git и ограничьте права:

```bash
chmod 600 ~/.cursor/mcp.json
```

Перезапустите Cursor и проверьте сервер в Settings → MCP или командами
`cursor-agent mcp list` и `cursor-agent mcp list-tools aviasales`. Cursor документирует
Streamable HTTP и расположение `mcp.json` в
[официальном руководстве](https://docs.cursor.com/context/model-context-protocol).

## OpenAI Agents API

Сервер должен быть доступен из среды, выполняющей агента. Передавайте токен в настройках
сессии или через vault, но не сохраняйте его в reusable agent definition:

```json
{
  "type": "mcp",
  "server_label": "aviasales",
  "transport": {
    "type": "http",
    "server_url": "https://mcp.example.com/mcp",
    "authorization": "Bearer YOUR_MCP_ACCESS_TOKEN"
  },
  "connection_origin": "environment",
  "allowed_tools": ["search_flights", "analyze_price_calendar", "live_search_flights"],
  "required": true
}
```

Используйте vault или trusted proxy для production. См.
[официальное руководство OpenAI по MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp).

## ChatGPT и Claude.ai

Публичные SaaS-клиенты обычно ожидают OAuth-авторизацию удалённого MCP. Текущая конфигурация
с одним статическим Bearer предназначена для закрытых клиентов и не является полноценным
multi-user OAuth-сервером. Для прямого подключения ChatGPT/Claude.ai необходимо добавить OAuth
2.1/JWT verifier, регистрацию клиентов и индивидуальные права пользователей либо поставить
совместимый authentication gateway перед MCP.

## Проверка и диагностика

1. `https://mcp.example.com/readyz` должен вернуть `ready`.
2. POST на `/mcp` без токена должен вернуть `401`.
3. Клиент должен обнаружить три инструмента.
4. `401` означает неверный Bearer; `400/421` — Host/Origin отсутствует в allowlist.
5. При timeout проверьте Nginx `proxy_read_timeout`, очередь и worker logs.
6. Если Partner Links API отвергает запрос, проверьте `TRS`, `MARKER` и доступ проекта к
   программе Aviasales.

Пример запроса агенту:

> Используй search_flights. Маршрут MOW–LED, вылет 2026-10-15, валюта RUB, только прямые
> рейсы. Верни цену, даты и подтверждённую партнёрскую ссылку.
