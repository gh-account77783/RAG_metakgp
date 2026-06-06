import os
import sys
from dotenv import load_dotenv

# Run load_dotenv() at the very top to ensure env vars are populated
load_dotenv()

import time
import httpx
import asyncio
from typing import Dict, Any
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.fastmcp import FastMCP
from jose import jwt, JWTError
from RAG.got_engine import GoTReasoningEngine

# 1. Initialize FastMCP in stateless mode
mcp = FastMCP("GraphMind", stateless_http=True)

# Resolve DB path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VECTOR_STORE_PATH = os.path.join(BASE_DIR, "VectorStore")

# Lazy-loaded reasoning engine to prevent FastAPI startup failures if databases are booting
_engine = None

def get_engine() -> GoTReasoningEngine:
    global _engine
    if _engine is None:
        _engine = GoTReasoningEngine(vector_store_path=VECTOR_STORE_PATH)
    return _engine

# Expose tools using FastMCP decorator syntax
@mcp.tool()
async def query_graphmind(query: str) -> str:
    """Run GraphMind GoT reasoning engine over MetaKGP data to return verified answers."""
    try:
        engine = get_engine()
        # Run synchronous LangGraph reasoning chain in a worker thread to keep the FastAPI loop unblocked
        result = await asyncio.to_thread(engine.reason, query)
        return result["answer"]
    except Exception as e:
        return f"Error executing GoT reasoning engine: {str(e)}"

@mcp.tool()
async def get_page_info(url: str) -> dict:
    """Fetch title and content for a given page URL."""
    try:
        engine = get_engine()
        # Run synchronous Neo4j lookup in a worker thread to keep the FastAPI loop unblocked
        result = await asyncio.to_thread(engine.neo4j.get_page_info, url)
        return result if result is not None else {"error": "Page not found"}
    except Exception as e:
        return {"error": str(e)}

# 2. Asynchronous Token Validation class
class TokenValidator:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.cached_keys = None
        self.keys_fetched_at = 0
        
    async def _get_google_keys(self) -> dict:
        # Fetch Google public certificates with a 24h cache window
        if not self.cached_keys or (time.time() - self.keys_fetched_at > 86400):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get("https://www.googleapis.com/oauth2/v3/certs")
                    self.cached_keys = response.json()
                    self.keys_fetched_at = time.time()
            except Exception as e:
                # If we have cached keys, reuse them on failure instead of failing the request
                if self.cached_keys:
                    pass
                else:
                    raise HTTPException(status_code=502, detail=f"Failed to fetch signing keys from Google: {str(e)}")
        return self.cached_keys

    async def verify_token(self, token: str) -> dict:
        # 1. Fallback check for static API key
        static_key = os.getenv("GRAPHMIND_API_KEY")
        if static_key and token == static_key:
            return {"email": "admin@graphmind.local", "name": "Admin User"}
            
        # 2. Standard Google OIDC verification
        try:
            try:
                header = jwt.get_unverified_header(token)
                kid = header.get("kid")
            except Exception as e:
                raise JWTError(f"Invalid token format or header: {str(e)}")

            google_certs = await self._get_google_keys()
            keys = google_certs.get("keys", [])
            key = next((k for k in keys if k["kid"] == kid), None)
            
            # If the key is not in our cache, clear cache and force-fetch from Google once (handles key rotations)
            if not key:
                self.cached_keys = None
                google_certs = await self._get_google_keys()
                keys = google_certs.get("keys", [])
                key = next((k for k in keys if k["kid"] == kid), None)
                
            if not key:
                raise JWTError("Matching public key not found in Google JWKs after cache refresh.")
                
            return jwt.decode(
                token, 
                key, 
                algorithms=["RS256"], 
                audience=self.client_id, 
                issuer="https://accounts.google.com"
            )
        except JWTError as e:
            raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")

# 3. Custom ASGI middleware for robust token authentication (especially for SSE)
class MCPAuthMiddleware:
    def __init__(self, app, validator: TokenValidator, static_key: str):
        self.app = app
        self.validator = validator
        self.static_key = static_key

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            method = scope.get("method", "")
            
            # Authenticate all routes starting with /mcp, excluding OPTIONS preflights
            if path.startswith("/mcp") and method != "OPTIONS":
                headers = scope.get("headers", [])
                auth_header = None
                for key, val in headers:
                    if key.lower() == b"authorization":
                        auth_header = val.decode("utf-8")
                        break
                
                token = None
                if auth_header and auth_header.lower().startswith("bearer "):
                    parts = auth_header.split(maxsplit=1)
                    if len(parts) == 2:
                        token = parts[1]
                
                # Fallback to query parameter token extraction (for browser-based EventSource)
                if not token:
                    import urllib.parse
                    query_string = scope.get("query_string", b"").decode("utf-8")
                    params = urllib.parse.parse_qs(query_string)
                    token_list = params.get("token")
                    if token_list:
                        token = token_list[0]
                
                is_authorized = False
                error_message = "Missing or invalid Authorization header."
                if token:
                    if self.static_key and token == self.static_key:
                        is_authorized = True
                    else:
                        try:
                            await self.validator.verify_token(token)
                            is_authorized = True
                        except HTTPException as e:
                            error_message = e.detail
                        except Exception as e:
                            error_message = str(e)
                
                if not is_authorized:
                    response_body = f'{{"detail": "Unauthorized: {error_message}"}}'.encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(response_body)).encode("utf-8")),
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": response_body,
                    })
                    return

        await self.app(scope, receive, send)

# 4. Initialize FastAPI Host App & Register Authentication Middleware
app = FastAPI(title="GraphMind Server Host")
validator = TokenValidator(client_id=os.getenv("GOOGLE_CLIENT_ID", ""))

# Add CORS Middleware to support web/browser-based MCP clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    MCPAuthMiddleware,
    validator=validator,
    static_key=os.getenv("GRAPHMIND_API_KEY", "")
)

# 5. Mount FastMCP using sse_app() to support remote server connections
app.mount("/mcp", mcp.sse_app(mount_path="/mcp"))

# 6. OAuth & Login endpoints
@app.get("/login")
async def login():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth is not configured on the server. Please set GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI in your environment variables."
        )
    google_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?client_id={client_id}"
        f"&response_type=code&scope=openid%20email%20profile&redirect_uri={redirect_uri}"
    )
    return RedirectResponse(google_url)

@app.get("/callback")
async def oauth_callback(code: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")
        
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth credentials are not fully configured on the server."
        )
    # Exchange auth code for Google ID token
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        )
    res_data = response.json()
    id_token = res_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Failed to retrieve ID Token from Google.")
    
    html_content = f"""
    <html>
        <head>
            <title>GraphMind Login Success</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                .card {{ background-color: #1e293b; padding: 2.5rem; border-radius: 12px; max-width: 600px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }}
                code {{ background-color: #0f172a; padding: 0.25rem 0.5rem; border-radius: 4px; color: #38bdf8; font-family: monospace; word-break: break-all; }}
                pre {{ background-color: #0f172a; padding: 1rem; border-radius: 8px; overflow-x: auto; color: #38bdf8; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h2>🎉 Authentication Successful</h2>
                <p>Add the following key-value pair to your MCP client configuration headers:</p>
                <pre>"headers": {{\n  "Authorization": "Bearer {id_token}"\n}}</pre>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# 7. Fallback local CLI runner (Stdio Mode)
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sse":
        import uvicorn
        uvicorn.run("mcp_server:app", host="127.0.0.1", port=8000, reload=True)
    else:
        # Run standard stdio transport for local Cursor / Claude Desktop runs
        mcp.run()

