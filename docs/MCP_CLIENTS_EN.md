# Connect Aviasales MCP to AI agents

## Connection details

- URL: `https://mcp.example.com/mcp`
- transport: Streamable HTTP
- header: `Authorization: Bearer <MCP_BEARER_TOKEN>`
- tools: `search_flights`, `analyze_price_calendar`

`MCP_BEARER_TOKEN` protects the MCP endpoint. It is not the Travelpayouts API token. Never
distribute `TRAVELPAYOUTS_TOKEN` to clients; it remains server-side only.

Set the client token:

```bash
export AVIASALES_MCP_TOKEN='your-MCP_BEARER_TOKEN'
```

Windows PowerShell:

```powershell
$env:AVIASALES_MCP_TOKEN = "your-MCP_BEARER_TOKEN"
```

## Codex CLI and Codex IDE

Add this to the shared `~/.codex/config.toml`:

```toml
[mcp_servers.aviasales]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "AVIASALES_MCP_TOKEN"
enabled_tools = ["search_flights", "analyze_price_calendar"]
```

Run `codex mcp list`, restart Codex, and ask it to use `search_flights`. The remote Streamable
HTTP format is documented in the
[official OpenAI documentation](https://developers.openai.com/learn/docs-mcp).

## Claude Code

Create a user or project `.mcp.json`:

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

Run `claude mcp list` and `/mcp`. Claude Code supports environment expansion in remote HTTP
headers; see the [official Anthropic MCP documentation](https://docs.anthropic.com/en/docs/mcp).

## VS Code / GitHub Copilot Agent Mode

Open `MCP: Open User Configuration` or create `.vscode/mcp.json`:

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

Run `MCP: List Servers`, enable the server, and use Copilot Chat in Agent mode. VS Code stores
the password input in secure storage. See the
[official VS Code MCP reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

## Cursor

Create global `~/.cursor/mcp.json` or project-level `.cursor/mcp.json`:

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

Keep the file out of Git and run `chmod 600 ~/.cursor/mcp.json`. Restart Cursor, then inspect
Settings → MCP or run `cursor-agent mcp list` and `cursor-agent mcp list-tools aviasales`.
Cursor documents Streamable HTTP and `mcp.json` locations in its
[official MCP guide](https://docs.cursor.com/context/model-context-protocol).

## OpenAI Agents API

The MCP server must be reachable from the agent execution environment. Provide the credential
for the session or through a vault, not in a reusable agent definition:

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
  "allowed_tools": ["search_flights", "analyze_price_calendar"],
  "required": true
}
```

Use a vault or trusted proxy in production. See the
[official OpenAI MCP connections guide](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp).

## ChatGPT and Claude.ai

Public SaaS clients generally expect OAuth for a remote MCP server. The current single static
bearer is intended for private clients and is not a complete multi-user OAuth server. Direct
ChatGPT/Claude.ai integration requires an OAuth 2.1/JWT verifier, client registration, and
per-user authorization, or a compatible authentication gateway in front of this MCP server.

## Verification and troubleshooting

1. `https://mcp.example.com/readyz` must report `ready`.
2. An unauthenticated POST to `/mcp` must return `401`.
3. The client must discover exactly two tools.
4. `401` means an invalid bearer; `400/421` usually means a Host/Origin allowlist mismatch.
5. For timeouts, inspect Nginx `proxy_read_timeout`, queue depth, and worker logs.
6. If Partner Links rejects requests, verify `TRS`, `MARKER`, and Aviasales program access.

Example agent request:

> Use search_flights for MOW–LED, departure 2026-10-15, RUB, direct flights only. Return the
> price, dates, and verified affiliate URL.
