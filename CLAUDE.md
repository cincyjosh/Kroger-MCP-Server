# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that connects Claude to Kroger's public API, enabling product search, store lookup, and adding items to a Kroger cart. The primary use case is meal-plan grocery workflows: find a store → search products → add a full grocery list.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Required environment variables:
```bash
export KROGER_CLIENT_ID=your_client_id
export KROGER_CLIENT_SECRET=your_client_secret
export KROGER_REDIRECT_URI=http://localhost:8080/callback
```

## Running the server

```bash
python server.py
```

## Claude Desktop config (MCP registration)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "kroger": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/kroger_mcp/server.py"],
      "env": {
        "KROGER_CLIENT_ID": "...",
        "KROGER_CLIENT_SECRET": "...",
        "KROGER_REDIRECT_URI": "http://localhost:8080/callback"
      }
    }
  }
}
```

## Architecture

Single-file server (`server.py`) built on `FastMCP` with five tools. Each tool creates its own `httpx.AsyncClient(timeout=15.0)` inline via `async with` — there is no lifespan manager or shared client.

**Two auth paths run in parallel:**
- **Client Credentials** (`_get_client_credentials_token`) — used for read-only tools (`kroger_find_store`, `kroger_search_products`). No user login required.
- **OAuth2 Authorization Code + PKCE** (`_get_user_token`) — required for cart write tools. First call opens a browser; the user pastes the redirect URL back into the terminal. Tokens (both access and refresh) are cached at `~/.kroger_mcp_tokens.json` (chmod 600). Subsequent calls silently refresh via the stored refresh token.

**Token cache (`~/.kroger_mcp_tokens.json`) keys:**
- `client_access_token` — client credentials token
- `user_access_token` — user OAuth token
- `refresh_token` — used to renew `user_access_token` without re-prompting

On a 401 response, `_handle_error` clears cached tokens so the next call re-authorizes. In `kroger_add_grocery_list`, a mid-loop 401 breaks the loop immediately and instructs the caller to retry.

**Tool input validation** uses Pydantic v2 models with `extra="forbid"` and `str_strip_whitespace=True`. Each tool's input model is the single source of truth for parameter constraints.

**`kroger_add_grocery_list`** is the high-value composite tool: it loops over item names, calls the products endpoint with `filter.limit=1` per item, picks the first result, then calls the cart endpoint — all within a single tool invocation. Items with no product match are returned in `skipped`; HTTP errors per item go into `errors`.

## API base URLs

- `https://api.kroger.com/v1` — all API calls
- `https://api.kroger.com/v1/connect/oauth2` — auth endpoints
