"""
Kroger MCP Server
-----------------
Integrates with Kroger's public API to enable product search,
store lookup, and cart management directly from Claude.

Authentication:
  - Product/Location search: Client Credentials (no user login needed)
  - Cart operations: OAuth2 Authorization Code with PKCE (user login required once)

Setup:
  1. Register at https://developer.kroger.com
  2. Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in environment
  3. Set KROGER_REDIRECT_URI to http://localhost:8080/callback
  4. Run: python server.py
  5. First cart operation will open browser for one-time login
"""

import json
import os
import hashlib
import base64
import secrets
import time
import webbrowser
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ─── Constants ────────────────────────────────────────────────────────────────

KROGER_BASE_URL = "https://api.kroger.com/v1"
KROGER_AUTH_URL = "https://api.kroger.com/v1/connect/oauth2"
TOKEN_CACHE_FILE = Path.home() / ".kroger_mcp_tokens.json"

DEFAULT_RADIUS_MILES = 10
DEFAULT_CHAIN = "KROGER"

PRODUCT_SEARCH_SCOPE = "product.compact"
CART_SCOPE = "cart.basic:write profile.compact"

KROGER_PREFERRED_BRANDS = {
    "kroger", "private selection", "simple truth", "simple truth organic",
    "heritage farm", "comforts", "home chef", "murray's cheese",
    "pet pride", "splash refresh", "abound", "luvsome",
}

# ─── Token Storage ────────────────────────────────────────────────────────────

def _load_tokens() -> dict:
    if TOKEN_CACHE_FILE.exists():
        try:
            return json.loads(TOKEN_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_tokens(tokens: dict) -> None:
    TOKEN_CACHE_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_CACHE_FILE.chmod(0o600)


def _get_credentials() -> tuple[str, str]:
    client_id = os.environ.get("KROGER_CLIENT_ID", "")
    client_secret = os.environ.get("KROGER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError(
            "KROGER_CLIENT_ID and KROGER_CLIENT_SECRET environment variables required. "
            "Register at https://developer.kroger.com to get credentials."
        )
    return client_id, client_secret


# ─── Auth Helpers ─────────────────────────────────────────────────────────────

async def _get_client_credentials_token(client: httpx.AsyncClient) -> str:
    """Get a client credentials token for product/location search (no user login)."""
    client_id, client_secret = _get_credentials()
    tokens = _load_tokens()

    if tokens.get("client_access_token") and tokens.get("client_token_expires_at", 0) > time.time() + 60:
        return tokens["client_access_token"]

    resp = await client.post(
        f"{KROGER_AUTH_URL}/token",
        data={
            "grant_type": "client_credentials",
            "scope": PRODUCT_SEARCH_SCOPE,
        },
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    data = resp.json()
    tokens["client_access_token"] = data["access_token"]
    tokens["client_token_expires_at"] = time.time() + data.get("expires_in", 1800)
    _save_tokens(tokens)
    return data["access_token"]


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def _get_user_token(client: httpx.AsyncClient) -> str:
    """
    Get a user-authorized token for cart operations.
    First call opens browser for one-time OAuth login.
    Subsequent calls use stored refresh token automatically.
    """
    client_id, client_secret = _get_credentials()
    redirect_uri = os.environ.get("KROGER_REDIRECT_URI", "http://localhost:8080/callback")
    tokens = _load_tokens()

    # Use cached access token if not expired (with 60s buffer)
    if tokens.get("user_access_token") and tokens.get("user_token_expires_at", 0) > time.time() + 60:
        return tokens["user_access_token"]

    # Try refresh token
    if tokens.get("refresh_token"):
        try:
            resp = await client.post(
                f"{KROGER_AUTH_URL}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
                auth=(client_id, client_secret),
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens["user_access_token"] = data["access_token"]
                tokens["user_token_expires_at"] = time.time() + data.get("expires_in", 1800)
                if "refresh_token" in data:
                    tokens["refresh_token"] = data["refresh_token"]
                _save_tokens(tokens)
                return data["access_token"]
        except Exception:
            pass

    # Need full OAuth flow — open browser
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": CART_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{KROGER_AUTH_URL}/authorize?" + urllib.parse.urlencode(params)

    print("\n" + "="*60)
    print("KROGER AUTHORIZATION REQUIRED")
    print("="*60)
    print("Opening browser for one-time Kroger login...")
    print(f"\nIf browser doesn't open, visit:\n{auth_url}\n")
    print("After logging in, paste the full redirect URL below.")
    print("(It will look like: http://localhost:8080/callback?code=...)")
    print("="*60)

    webbrowser.open(auth_url)
    redirect_response = input("\nPaste the redirect URL here: ").strip()

    parsed = urllib.parse.urlparse(redirect_response)
    query_params = urllib.parse.parse_qs(parsed.query)

    returned_state = query_params.get("state", [None])[0]
    if returned_state != state:
        raise ValueError("OAuth state mismatch. The redirect URL may have been tampered with. Please try again.")

    code = query_params.get("code", [None])[0]
    if not code:
        raise ValueError("No authorization code found in URL. Please try again.")

    resp = await client.post(
        f"{KROGER_AUTH_URL}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    data = resp.json()

    tokens["user_access_token"] = data["access_token"]
    tokens["user_token_expires_at"] = time.time() + data.get("expires_in", 1800)
    tokens["refresh_token"] = data.get("refresh_token", "")
    _save_tokens(tokens)

    print("\n✅ Authorization successful! Token saved. You won't need to log in again.")
    return data["access_token"]


# ─── API Helpers ──────────────────────────────────────────────────────────────

def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 400:
            return f"Error: Bad request — check your parameters. Details: {e.response.text}"
        elif status == 401:
            # Clear cached tokens so next call re-authorizes
            tokens = _load_tokens()
            tokens.pop("user_access_token", None)
            tokens.pop("client_access_token", None)
            _save_tokens(tokens)
            return "Error: Authorization expired. Please retry — you may need to log in again."
        elif status == 403:
            return "Error: Permission denied. Make sure cart.basic:write scope is authorized."
        elif status == 404:
            return "Error: Resource not found. Check the product UPC or location ID."
        elif status == 429:
            return "Error: Kroger API rate limit hit. Please wait a moment and try again."
        return f"Error: API request failed with status {status}. Details: {e.response.text}"
    elif isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Please try again."
    elif isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: Unexpected error — {type(e).__name__}: {e}"


def _format_product(p: dict) -> dict:
    """Extract the most useful fields from a Kroger product object."""
    price_info = {}
    if p.get("items"):
        item = p["items"][0]
        price_info = {
            "price": item.get("price", {}).get("regular"),
            "sale_price": item.get("price", {}).get("promo"),
            "size": item.get("size"),
            "sold_by": item.get("soldBy"),
            "in_stock": item.get("inventory", {}).get("stockLevel") != "TEMPORARILY_OUT_OF_STOCK",
        }
    return {
        "upc": p.get("upc"),
        "name": p.get("description"),
        "brand": p.get("brand"),
        "categories": p.get("categories", []),
        "aisle": p.get("aisleLocations", [{}])[0].get("description") if p.get("aisleLocations") else None,
        **price_info,
    }


def _pick_best_product(products: list[dict]) -> dict:
    def score(p: dict) -> int:
        s = 0
        if p.get("in_stock"):
            s += 10
        if (p.get("brand") or "").lower() in KROGER_PREFERRED_BRANDS:
            s += 5
        if p.get("sale_price") is not None:
            s += 3
        return s

    return max(products, key=score)


mcp = FastMCP("kroger_mcp")

# ─── Input Models ─────────────────────────────────────────────────────────────

class SearchProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Product search term (e.g., 'chicken breast', 'baby spinach', 'ground beef')", min_length=1, max_length=100)
    location_id: Optional[str] = Field(default=None, description="Kroger store location ID. Use kroger_find_store first if unknown.")
    limit: Optional[int] = Field(default=5, description="Max results to return (1-10)", ge=1, le=10)


class FindStoreInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    zip_code: str = Field(..., description="ZIP code to search near.")
    radius_miles: Optional[int] = Field(default=DEFAULT_RADIUS_MILES, description="Search radius in miles", ge=1, le=50)


class AddToCartInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    upc: str = Field(..., description="Product UPC code from kroger_search_products", min_length=1)
    quantity: Optional[int] = Field(default=1, description="Quantity to add", ge=1, le=20)


class AddGroceryListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    items: list[str] = Field(..., description="List of grocery item names to search and add (e.g. ['chicken breast', 'baby spinach', 'brown rice'])", min_length=1, max_length=50)
    location_id: str = Field(..., description="Kroger store location ID from kroger_find_store")
    quantity_each: Optional[int] = Field(default=1, description="Quantity to add for each item", ge=1, le=10)


class ClearAuthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(..., description="Set to true to confirm clearing stored tokens")


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="kroger_find_store",
    annotations={
        "title": "Find Nearest Kroger Store",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def kroger_find_store(params: FindStoreInput) -> str:
    """
    Find Kroger store locations near a ZIP code.

    Returns store name, address, phone, hours, and location_id.
    The location_id is required for product search and cart operations.

    Args:
        params (FindStoreInput):
            - zip_code (str): ZIP code to search near (required)
            - radius_miles (int): Search radius, defaults to 10

    Returns:
        str: JSON list of nearby stores with location IDs and addresses.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token = await _get_client_credentials_token(client)
            resp = await client.get(
                f"{KROGER_BASE_URL}/locations",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "filter.zipCode.near": params.zip_code,
                    "filter.radiusInMiles": params.radius_miles,
                    "filter.chain": DEFAULT_CHAIN,
                    "filter.limit": 5,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            stores = []
            for loc in data.get("data", []):
                addr = loc.get("address", {})
                hours_raw = loc.get("hours", {})
                stores.append({
                    "location_id": loc.get("locationId"),
                    "name": loc.get("name"),
                    "address": f"{addr.get('addressLine1')}, {addr.get('city')}, {addr.get('state')} {addr.get('zipCode')}",
                    "phone": loc.get("phone"),
                    "hours_monday": "Open 24hrs" if hours_raw.get("monday", {}).get("open24") else f"{hours_raw.get('monday', {}).get('open', 'N/A')} - {hours_raw.get('monday', {}).get('close', 'N/A')}",
                })
            return json.dumps({"stores": stores, "count": len(stores)}, indent=2)
        except Exception as e:
            return _handle_error(e)


@mcp.tool(
    name="kroger_search_products",
    annotations={
        "title": "Search Kroger Products",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def kroger_search_products(params: SearchProductsInput) -> str:
    """
    Search the Kroger product catalog by keyword.

    Returns product name, brand, UPC, price, size, aisle location, and stock status.
    Use the UPC from results with kroger_add_to_cart.

    Args:
        params (SearchProductsInput):
            - query (str): Search term (e.g., 'baby spinach', 'salmon fillet')
            - location_id (str): Store location ID for price/stock data
            - limit (int): Number of results (1-10, default 5)

    Returns:
        str: JSON list of matching products with UPC, name, price, and aisle.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token = await _get_client_credentials_token(client)
            query_params = {
                "filter.term": params.query,
                "filter.limit": params.limit,
            }
            if params.location_id:
                query_params["filter.locationId"] = params.location_id

            resp = await client.get(
                f"{KROGER_BASE_URL}/products",
                headers={"Authorization": f"Bearer {token}"},
                params=query_params,
            )
            resp.raise_for_status()
            data = resp.json()
            products = [_format_product(p) for p in data.get("data", [])]
            return json.dumps({
                "query": params.query,
                "count": len(products),
                "products": products,
            }, indent=2)
        except Exception as e:
            return _handle_error(e)


@mcp.tool(
    name="kroger_add_to_cart",
    annotations={
        "title": "Add Product to Kroger Cart",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def kroger_add_to_cart(params: AddToCartInput) -> str:
    """
    Add a single product to the authenticated user's Kroger cart by UPC.

    Requires one-time browser login on first use. Token is saved and
    refreshed automatically after that.

    Args:
        params (AddToCartInput):
            - upc (str): Product UPC from kroger_search_products
            - quantity (int): Quantity to add (default 1)

    Returns:
        str: Success confirmation or error message.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token = await _get_user_token(client)
            resp = await client.put(
                f"{KROGER_BASE_URL}/cart/add",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"items": [{"upc": params.upc, "quantity": params.quantity}]},
            )
            resp.raise_for_status()
            return json.dumps({
                "success": True,
                "message": f"Added UPC {params.upc} (qty: {params.quantity}) to your Kroger cart.",
            }, indent=2)
        except Exception as e:
            return _handle_error(e)


@mcp.tool(
    name="kroger_add_grocery_list",
    annotations={
        "title": "Add Full Grocery List to Kroger Cart",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def kroger_add_grocery_list(params: AddGroceryListInput) -> str:
    """
    Search for and add an entire grocery list to the Kroger cart in one operation.

    For each item: searches the product catalog, picks the best match,
    and adds it to cart. Returns a summary of what was added and any
    items that couldn't be matched.

    This is the primary tool for the meal plan → cart workflow.

    Args:
        params (AddGroceryListInput):
            - items (list[str]): List of grocery item names
            - location_id (str): Store location ID from kroger_find_store
            - quantity_each (int): Quantity for each item (default 1)

    Returns:
        str: JSON summary with added items, skipped items, and any errors.
    """
    added = []
    skipped = []
    errors = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            user_token = await _get_user_token(client)
            client_token = await _get_client_credentials_token(client)
        except Exception as e:
            return _handle_error(e)

        for item_name in params.items:
            try:
                # Search for the product
                search_resp = await client.get(
                    f"{KROGER_BASE_URL}/products",
                    headers={"Authorization": f"Bearer {client_token}"},
                    params={
                        "filter.term": item_name,
                        "filter.locationId": params.location_id,
                        "filter.limit": 5,
                    },
                )
                search_resp.raise_for_status()
                products = search_resp.json().get("data", [])

                if not products:
                    skipped.append({"item": item_name, "reason": "No matching product found"})
                    continue

                best = _pick_best_product(products)
                upc = best.get("upc")
                name = best.get("description", item_name)

                if not upc:
                    skipped.append({"item": item_name, "reason": "Product found but no UPC available"})
                    continue

                # Add to cart
                cart_resp = await client.put(
                    f"{KROGER_BASE_URL}/cart/add",
                    headers={
                        "Authorization": f"Bearer {user_token}",
                        "Content-Type": "application/json",
                    },
                    json={"items": [{"upc": upc, "quantity": params.quantity_each}]},
                )
                cart_resp.raise_for_status()

                price = None
                if best.get("items"):
                    price = best["items"][0].get("price", {}).get("regular")

                added.append({
                    "requested": item_name,
                    "matched_to": name,
                    "upc": upc,
                    "price": price,
                    "quantity": params.quantity_each,
                })

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    # Token expired mid-run — clear and report
                    tokens = _load_tokens()
                    tokens.pop("user_access_token", None)
                    _save_tokens(tokens)
                    errors.append({"item": item_name, "error": "Auth token expired mid-run. Please retry."})
                    break
                errors.append({"item": item_name, "error": f"HTTP {e.response.status_code}: {e.response.text[:100]}"})
            except Exception as e:
                errors.append({"item": item_name, "error": str(e)})

        return json.dumps({
            "summary": {
                "total_requested": len(params.items),
                "added_to_cart": len(added),
                "skipped": len(skipped),
                "errors": len(errors),
            },
            "added": added,
            "skipped": skipped,
            "errors": errors,
        }, indent=2)


@mcp.tool(
    name="kroger_clear_auth",
    annotations={
        "title": "Clear Stored Kroger Tokens",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def kroger_clear_auth(params: ClearAuthInput) -> str:
    """
    Clear all stored Kroger OAuth tokens from local cache.

    Use this if you want to log in as a different account or if
    tokens have become invalid. Next cart operation will require
    browser login again.

    Args:
        params (ClearAuthInput):
            - confirm (bool): Must be true to proceed

    Returns:
        str: Confirmation that tokens were cleared.
    """
    if not params.confirm:
        return "No action taken. Set confirm=true to clear tokens."
    if TOKEN_CACHE_FILE.exists():
        TOKEN_CACHE_FILE.unlink()
    return json.dumps({"success": True, "message": "All stored Kroger tokens cleared. Next cart operation will require browser login."}, indent=2)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    required = ["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"⚠️  Missing environment variables: {', '.join(missing)}")
        print("Register at https://developer.kroger.com to get credentials.")
        print("Then set them before running:\n")
        print("  export KROGER_CLIENT_ID=your_client_id")
        print("  export KROGER_CLIENT_SECRET=your_client_secret")
        print("  export KROGER_REDIRECT_URI=http://localhost:8080/callback\n")
    mcp.run()
