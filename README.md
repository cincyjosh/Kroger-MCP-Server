# Kroger MCP Server

Integrates Kroger's public API with Claude so meal plan grocery lists
can be added directly to your Kroger cart.

## Tools

| Tool | What it does |
|---|---|
| `kroger_find_store` | Find nearby Kroger stores by ZIP code, returns `location_id` |
| `kroger_search_products` | Search the product catalog, returns UPC + price + aisle |
| `kroger_add_to_cart` | Add a single product by UPC to your cart |
| `kroger_add_grocery_list` | Search + add an entire grocery list in one shot |
| `kroger_clear_auth` | Clear stored tokens (if you need to re-authenticate) |

## Setup

### Step 1 — Register your app at Kroger Developer Portal

1. Go to https://developer.kroger.com
2. Click **Register App**
3. Fill in:
   - App Name: `Meal Plan Assistant` (or anything)
   - Redirect URI: `http://localhost:8080/callback`
   - Scopes: `product.compact`, `cart.basic:write`, `profile.compact`
4. Copy your **Client ID** and **Client Secret**

### Step 2 — Install dependencies

```bash
cd kroger_mcp
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Step 3 — Set environment variables

```bash
export KROGER_CLIENT_ID=your_client_id_here
export KROGER_CLIENT_SECRET=your_client_secret_here
export KROGER_REDIRECT_URI=http://localhost:8080/callback
```

Add these to your shell profile (`~/.zshrc` or `~/.bashrc`) so they persist.

### Step 4 — Connect to Claude

Add to your Claude MCP config (usually `~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "kroger": {
      "command": "/path/to/kroger_mcp/venv/bin/python",
      "args": ["/path/to/kroger_mcp/server.py"],
      "env": {
        "KROGER_CLIENT_ID": "your_client_id",
        "KROGER_CLIENT_SECRET": "your_client_secret",
        "KROGER_REDIRECT_URI": "http://localhost:8080/callback"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

## First Use — OAuth Login

The first time you ask Claude to add items to your cart:
1. Your browser will open to Kroger's login page
2. Log in with your Kroger account
3. Paste the redirect URL back into the terminal
4. Done — tokens are saved at `~/.kroger_mcp_tokens.json`

After that, Claude can add to your cart silently using the stored refresh token.

## Example Flow

Once connected, you can say to Claude:

> "Find my nearest Kroger store and add this week's grocery list to my cart"

Claude will:
1. Call `kroger_find_store` with your ZIP code
2. Call `kroger_add_grocery_list` with all the items
3. Report back what was added, what was skipped, and any issues

## Security Notes

- Your Client ID and Secret are **yours** — registered under your account
- Tokens are stored locally at `~/.kroger_mcp_tokens.json` (permissions: 600)
- No third-party code involved — direct calls to Kroger's official API
- `kroger_clear_auth` removes all stored tokens if needed

## Troubleshooting

**"Missing environment variables"** — Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET

**"Authorization expired"** — Retry the operation; the server will refresh automatically

**"No matching product found"** — Try a shorter search term (e.g., "spinach" instead of "Private Selection Baby Spinach")

**Rate limits (HTTP 429)** — Kroger limits requests; wait 30 seconds and retry
