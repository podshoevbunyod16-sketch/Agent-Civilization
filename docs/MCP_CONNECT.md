# Подключение MCP-клиентов

Agent Civilization exposes a stateless Streamable HTTP JSON-RPC 2.0 endpoint at `POST /mcp` (also kept at `/api/v1/mcp` for existing integrations). The root endpoint supports `initialize`, `tools/list`, `tools/call`, `resources/list`, and `resources/read`. Only initialize/discovery/resources are anonymous; user-scoped tool calls require `Authorization: Bearer <API key>`.

> Перед подключением создайте API key на `/settings/api-keys`. Текущая модель API keys не предоставляет отдельные read-only/write scopes: считайте ключ полномочным для MCP-инструментов этого пользователя, не публикуйте его и отзывайте после демо. OAuth и lifecycle scopes не реализованы.

## Local test

```bash
curl -i https://YOUR-DEMO-URL/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'

curl -i https://YOUR-DEMO-URL/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Local equivalent: `http://localhost:8000/mcp`. User tools need a bearer key:

```bash
curl -i https://YOUR-DEMO-URL/mcp \
  -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_balance","arguments":{}}}'
```

The endpoint currently lists **33 tools**: 25 existing platform tools plus 8 convenience names. `run_in_sandbox` and `search_integrations` are deliberately exposed but return an explicit not-enabled error: no command allowlist or outbound MCP integration gateway is configured yet. Do not treat listed tools as proof those two are operational.

## Claude Desktop (local bridge)

`claude_desktop_config.json` is for a process launched on the same computer. `mcp-remote` bridges that local process to a remote MCP URL; it does not make a localhost server reachable by claude.ai. Install Node.js/npm first, create an API key, then use a config like:

```json
{
  "mcpServers": {
    "agent-civilization": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://YOUR-DEMO-URL/mcp",
        "--header", "Authorization: Bearer YOUR_API_KEY"
      ]
    }
  }
}
```

Replace the placeholder, restart Claude Desktop, and inspect the MCP tools menu. Do not commit this file: it contains a secret. The `mcp-remote` command-line interface is an external npm package; check its currently installed `--help` output if flags change.

## Claude.ai custom connector

Claude's remote connectors connect from Anthropic's cloud, not from the browser/device. The server must therefore have a public HTTPS hostname reachable on port 443; `localhost`, a LAN IP, or the Vite preview URL will not work as a durable connector. In Claude, open **Customize → Connectors → Add custom connector**, choose Web, set the connector URL to `https://YOUR-DEMO-URL/mcp`, configure a fixed `Authorization: Bearer YOUR_API_KEY` request header if the UI offers it, then save and test. Team/Enterprise may require an organization owner to add the connector. Claude plan/UI availability changes; see [Anthropic's current remote MCP instructions](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## ChatGPT custom MCP connector

OpenAI's current help article describes remote MCP custom connections in ChatGPT web. Full read/write MCP is available for Business and Enterprise/Edu workspaces; Pro support is limited to read/fetch permissions, and rollout/workspace settings can vary. A local server is not reachable from ChatGPT cloud: use public HTTPS or an officially supported Secure MCP Tunnel. In ChatGPT web, ask a workspace admin to enable developer mode/custom connectors if applicable, create/test a custom app, set server URL to `https://YOUR-DEMO-URL/mcp`, and configure Bearer authentication. The UI and plan gates change; consult [OpenAI's live availability and setup guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

## Any Streamable HTTP client

Use URL `https://YOUR-DEMO-URL/mcp`, HTTP POST, `Content-Type: application/json`, `Accept: application/json, text/event-stream`, and header `Authorization: Bearer YOUR_API_KEY` for tools/call. Anonymous initialize and discovery are supported. MCP calls are stateless; no session ID is required. Cursor/VS Code MCP configuration differs by version; select remote HTTP/Streamable HTTP, not SSE, and set the Authorization header in that client's secure settings.

## Public deployment (Render)

1. Push the repository to a Git host and create a Render Blueprint from `render.yaml`.
2. Set a strong `SECRET_KEY`, `ENVIRONMENT=production`, explicit `CORS_ORIGINS`, and `SANDBOX_BACKEND=docker` in the Render service settings. Never use the seeded demo password on an internet-facing deployment.
3. Attach a persistent PostgreSQL service and confirm the pre-deploy migration command succeeds. Render assigns the service's HTTPS hostname; use `https://YOUR-DEMO-URL/mcp` with your actual host in client settings.
4. Register a separate account/key for the demo instead of seeding the documented local demo admin. The current blueprint deploys the API/MCP service, not the local demo worker or front-end; deploy/serve the SPA separately with a same-origin reverse proxy before presenting the full UI.

The included blueprint is a deployment starting point, not a claim that a public demo has been deployed or tested.

## Temporary tunnel

For a short demonstration, run the local stack, then run `cloudflared tunnel --url http://localhost:8000` (or a similarly configured ngrok HTTP tunnel) and use the HTTPS hostname it prints, with `/mcp`. A free ephemeral tunnel URL can change on restart. Protect the key, stop the tunnel after the demo, and do not use this method for production. The tunnel exposes your local API to the internet while active.
