"""Authenticated browser and Streamable HTTP MCP product boundary."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import threading
from contextlib import suppress
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from .config import Settings
from .errors import (
    DocumentNotFoundError, GraphMindError, IdentityError, IdentityGrantRejectedError, IdentityResponseError,
    QueryCapacityError, RetryableQueryError,
)
from .identity import (
    FlowCookieSigner,
    IdentityAuthority,
    IdentityPrincipal,
    KeycloakAuthority,
    new_browser_flow,
)
from .reader import ReaderService


ACCESS_COOKIE = "graphmind_access"
REFRESH_COOKIE = "graphmind_refresh"
FLOW_COOKIE = "graphmind_oauth_flow"
CSRF_COOKIE = "graphmind_csrf"


class GraphMindTokenVerifier(TokenVerifier):
    def __init__(self, authority: IdentityAuthority, settings: Settings) -> None:
        self.authority = authority
        self.settings = settings
        self.cached: ContextVar[tuple[str, AccessToken | None] | None] = ContextVar("graphmind_verified_token", default=None)

    async def verify_token(self, token: str) -> AccessToken | None:
        cached = self.cached.get()
        if cached is not None and secrets.compare_digest(cached[0], token):
            return cached[1]
        principal = await self.authority.verify_token(token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.client_id,
            scopes=list(principal.scopes),
            expires_at=principal.expires_at,
            resource=self.settings.identity_mcp_audience,
            subject=principal.subject,
            claims={"iss": principal.issuer, "email": principal.email},
        )


class ProductBoundary:
    """Handle identity outages before SDK auth and emit query-free access records."""

    def __init__(self, app: Any, verifier: GraphMindTokenVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        known = {"/", "/login", "/signup", "/auth/callback", "/auth/refresh", "/logout",
                 "/api/ask", "/api/search", "/health/live", "/health/ready", "/mcp",
                 "/.well-known/oauth-protected-resource/mcp", "/assets/graphmind.js", "/assets/graphmind.css"}
        route = path if path in known else "/documents/{document_id}" if path.startswith("/documents/") else "<unmatched>"

        async def safe_send(message):
            if message["type"] == "http.response.start":
                method = scope.get("method", "")
                method = method if method in {"GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"} else "OTHER"
                logging.getLogger("graphmind.access").info("%s %s %d", method, route, message["status"])
            await send(message)

        header = next((value.decode("latin-1") for key, value in scope.get("headers", [])
                       if key.lower() == b"authorization"), "")
        context = None
        if header.lower().startswith("bearer "):
            token = header[7:]
            try:
                verified = await self.verifier.verify_token(token)
            except IdentityError:
                return await _error(503, "identity_unavailable", "Identity service is unavailable")(scope, receive, safe_send)
            context = self.verifier.cached.set((token, verified))
        try:
            await self.app(scope, receive, safe_send)
        finally:
            if context is not None:
                self.verifier.cached.reset(context)


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    return response


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _security_headers(JSONResponse({"error": code, "message": message}, status_code=status))


def _cookie_options(settings: Settings, *, http_only: bool = True, same_site: str = "lax") -> dict[str, Any]:
    return {
        "secure": settings.browser_cookie_secure,
        "httponly": http_only,
        "samesite": same_site,
        "path": "/",
    }


def _set_token_cookies(response: Response, settings: Settings, tokens: Any) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        tokens.access_token,
        max_age=min(int(tokens.expires_in), 900),
        **_cookie_options(settings),
    )
    if tokens.refresh_token:
        response.set_cookie(
            REFRESH_COOKIE,
            tokens.refresh_token,
            max_age=8 * 60 * 60,
            **_cookie_options(settings, same_site="strict"),
        )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, FLOW_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=settings.browser_cookie_secure)


def _login_page() -> str:
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GraphMind</title>
<link rel="stylesheet" href="/assets/graphmind.css"></head><body><main>
<h1>GraphMind</h1><p>Sign in to search and ask questions over this installation's documents.</p>
<p><a class="button" href="/login">Sign in</a> <a class="button secondary" href="/signup">Create account</a></p>
</main></body></html>"""


def _chat_page(email: str) -> str:
    safe_email = html.escape(email, quote=True)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GraphMind</title>
<link rel="stylesheet" href="/assets/graphmind.css"></head><body><main>
<header><h1>GraphMind</h1><p>Signed in as <strong>{safe_email}</strong></p></header>
<form id="ask-form"><label for="question">Question</label><textarea id="question" maxlength="2000" required></textarea>
<button type="submit">Ask</button> <button id="logout" type="button" class="secondary">Sign out</button></form>
<p id="status" role="status"></p><section id="answer" aria-live="polite"></section>
<h2>Sources</h2><ol id="citations"></ol></main><script src="/assets/graphmind.js" defer></script></body></html>"""


CSS = """body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f5f7fb;color:#172033}main{max-width:52rem;margin:3rem auto;padding:2rem;background:white;border-radius:.75rem;box-shadow:0 8px 30px #1d2a4418}textarea{display:block;width:100%;min-height:7rem;margin:.5rem 0 1rem;box-sizing:border-box}button,.button{display:inline-block;padding:.65rem 1rem;background:#3157d5;color:white;border:0;border-radius:.35rem;text-decoration:none;cursor:pointer}.secondary{background:#596273}li{margin:.75rem 0}.excerpt{white-space:pre-wrap;color:#37435a}"""


JAVASCRIPT = r"""const cookie=n=>document.cookie.split('; ').find(x=>x.startsWith(n+'='))?.split('=').slice(1).join('=');
const csrf=()=>decodeURIComponent(cookie('graphmind_csrf')||'');
async function post(url,body){
  const send=()=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-GraphMind-CSRF':csrf()},body:JSON.stringify(body)});
  let r=await send();
  if(r.status===401&&url!='/auth/refresh'){
    const q=await fetch('/auth/refresh',{method:'POST',headers:{'X-GraphMind-CSRF':csrf()}});
    if(q.ok)r=await send();else return q;
  }
  return r;
}
async function resumeSession(){
  const resume=document.getElementById('resume');if(!resume)return;
  const status=document.getElementById('status');
  try{
    const r=await post('/auth/refresh',{});
    if(r.ok){location.replace(resume.dataset.returnTo);return;}
    status.textContent=r.status===401?'Your session expired. Sign in again.':'Session renewal is unavailable. Retry or sign in.';
  }catch{status.textContent='Session renewal failed. Retry or sign in.';}
}
document.getElementById('ask-form')?.addEventListener('submit',async e=>{
  e.preventDefault();
  const status=document.getElementById('status'),answer=document.getElementById('answer'),list=document.getElementById('citations');
  const button=e.currentTarget.querySelector('button[type="submit"]');button.disabled=true;
  status.textContent='Working…';answer.textContent='';list.replaceChildren();
  try{
    const r=await post('/api/ask',{question:document.getElementById('question').value});
    const p=await r.json();
    if(!r.ok){status.textContent=p.message||'Request failed';return;}
    if(!['answer','insufficient_evidence'].includes(p.outcome))throw new Error('Invalid response');
    status.textContent=p.outcome==='answer'?'Answered':'No supported answer';answer.textContent=p.answer||'';
    (p.citations||[]).forEach(c=>{
      const li=document.createElement('li'),a=document.createElement('a'),x=document.createElement('div');
      a.href='/documents/'+encodeURIComponent(c.document_id)+'?version_id='+encodeURIComponent(c.version_id);
      a.textContent=(c.display_name||'Source')+' — '+(c.locator||'');x.className='excerpt';x.textContent=c.excerpt||'';
      li.append(a,x);list.append(li);
    });
  }catch{status.textContent='Request failed. Please retry.';}
  finally{button.disabled=false;}
});
document.getElementById('logout')?.addEventListener('click',async()=>{
  try{const r=await post('/logout',{});if(!r.ok)throw new Error('Sign-out failed');location.href='/';}
  catch{document.getElementById('status').textContent='Sign-out could not be confirmed. Please retry.';}
});
void resumeSession();"""


async def _request_json(request: Request, settings: Settings) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if not 0 <= int(content_length) <= settings.browser_max_request_bytes:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Request body is too large") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > settings.browser_max_request_bytes - len(body):
            raise ValueError("Request body is too large")
        body.extend(chunk)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


def _valid_csrf(request: Request, settings: Settings) -> bool:
    if request.headers.get("origin") != settings.public_base_url.rstrip("/"):
        return False
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-graphmind-csrf", "")
    return bool(cookie and header and secrets.compare_digest(cookie, header))


def _resume_page(request: Request, settings: Settings) -> Response:
    # GET does not refresh credentials. A same-origin page POSTs with CSRF, then
    # navigates back to the exact local source URL without putting tokens in it.
    target = new_browser_flow(str(request.url.path) + ("?" + request.url.query if request.url.query else ""))["return_to"]
    body = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>GraphMind session</title><link rel="stylesheet" href="/assets/graphmind.css"></head><body>
<main id="resume" data-return-to="{html.escape(target, quote=True)}"><h1>Renewing your session</h1>
<p id="status" role="status">Working…</p><p><a href="/login">Sign in again</a></p></main>
<script src="/assets/graphmind.js" defer></script></body></html>'''
    response = _security_headers(HTMLResponse(body))
    if not request.cookies.get(CSRF_COOKIE):
        response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), max_age=8 * 60 * 60,
                            **_cookie_options(settings, http_only=False, same_site="strict"))
    return response


def _query_error(exc: GraphMindError) -> Response:
    code = exc.code
    status = 503
    message = "A required service is unavailable; retry"
    if code in {"invalid_request", "query_budget_exceeded"}:
        status, message = 400, "Request exceeds the allowed query budget"
    elif code in {"invalid_provider_response", "invalid_embedding_response"}:
        status, message = 502, "A required service returned an invalid response"
    elif code == "provider_timeout":
        status, message = 504, "Answer service timed out; retry"
    elif code in {"retryable_query_error", "query_cancelled"}:
        status, message = 409, "Query or source state changed; retry"
    return _error(status, code, message)


class BoundedReadiness:
    """One in-flight probe, no cached green status and no timed-out thread pileup."""

    def __init__(self, probe, *, timeout: float = 5.0) -> None:
        self.probe = probe
        self.timeout = timeout
        self.task: asyncio.Task | None = None

    async def available(self) -> bool:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.probe())
        try:
            return bool(await asyncio.wait_for(asyncio.shield(self.task), timeout=self.timeout))
        except Exception:
            return False


async def _browser_principal(
    request: Request, authority: IdentityAuthority
) -> tuple[str, IdentityPrincipal] | None:
    token = request.cookies.get(ACCESS_COOKIE, "")
    if not token:
        return None
    principal = await authority.verify_token(token)
    return (token, principal) if principal is not None else None


async def _still_authorized(token: str, authority: IdentityAuthority) -> bool:
    return await authority.verify_token(token) is not None


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    parsed = urlparse(settings.public_base_url)
    host = parsed.hostname or ""
    netloc = parsed.netloc
    hosts = [netloc]
    if host and host != netloc:
        hosts.extend([host, f"{host}:*"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=[settings.public_base_url.rstrip("/")],
    )


def create_product_server(
    settings: Settings,
    application: Any,
    *,
    authority: IdentityAuthority | None = None,
) -> tuple[MCPServer, Any]:
    """Build the product server without binding a socket."""

    settings.validate(require_identity_secrets=True)
    selected_authority = authority or KeycloakAuthority(settings)
    verifier = GraphMindTokenVerifier(selected_authority, settings)
    reader = ReaderService(application, max_fetch_chars=settings.browser_max_fetch_chars)
    offload_slots = threading.BoundedSemaphore(settings.max_concurrent_queries + settings.max_queued_queries)

    async def reader_work(function, *args, request: Request | None = None, **kwargs):
        # Admit before submitting to the executor, whose own queue is unbounded.
        if not offload_slots.acquire(blocking=False):
            raise QueryCapacityError("Reader capacity is full")
        cancelled = threading.Event()
        supports_cancellation = function in (reader.ask, reader.search_documents)
        if supports_cancellation:
            kwargs["cancel_event"] = cancelled

        def run():
            try:
                return function(*args, **kwargs)
            finally:
                offload_slots.release()  # Keep capacity until the actual worker exits.

        async def watch_disconnect():
            while True:
                if await request.is_disconnected():
                    cancelled.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(watch_disconnect()) if request is not None else None
        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            # A synchronous transport cannot be force-killed safely. Discard
            # its eventual result and retain its slot until its timeout/exit.
            worker.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher

    signer = FlowCookieSigner(settings.identity_flow_signing_key)
    server = MCPServer(
        "GraphMind",
        version="0.1.0",
        description="Authenticated document search and grounded answers",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(selected_authority.issuer_url),
            resource_server_url=AnyHttpUrl(settings.identity_mcp_audience),
            required_scopes=[settings.identity_required_scope],
        ),
    )

    async def mcp_token() -> tuple[str, IdentityPrincipal]:
        access = get_access_token()
        if access is None:
            raise ToolError("authentication_required")
        try:
            principal = await selected_authority.verify_token(access.token)
        except IdentityError as exc:
            raise ToolError("identity_unavailable") from exc
        if principal is None:
            raise ToolError("authorization_denied")
        return access.token, principal

    async def checked_result(token: str, function, *args, **kwargs):
        result = await reader_work(function, *args, **kwargs)
        try:
            still_authorized = await _still_authorized(token, selected_authority)
        except IdentityError as exc:
            raise ToolError("identity_unavailable") from exc
        if not still_authorized:
            raise ToolError("authorization_revoked")
        reader.validate_result(result)
        return result

    @server.tool(name="search_documents", structured_output=True)
    async def search_documents(query: str, limit: int = 8) -> dict[str, Any]:
        """Search active documents and return evidence for client-side synthesis."""
        token, _ = await mcp_token()
        try:
            return await checked_result(token, reader.search_documents, query, limit=limit)
        except (GraphMindError, ValueError) as exc:
            raise ToolError(getattr(exc, "code", "invalid_request")) from exc

    @server.tool(name="fetch_document", structured_output=True)
    async def fetch_document(
        document_id: str,
        version_id: str | None = None,
        max_chars: int = 12_000,
    ) -> dict[str, Any]:
        """Fetch text from one currently active document using opaque identifiers."""
        token, _ = await mcp_token()
        try:
            return await checked_result(
                token,
                reader.fetch_document,
                document_id,
                version_id=version_id,
                max_chars=max_chars,
            )
        except (GraphMindError, ValueError) as exc:
            raise ToolError(getattr(exc, "code", "invalid_request")) from exc

    @server.tool(name="ask", structured_output=True)
    async def ask(question: str) -> dict[str, Any]:
        """Ask the configured server model for a grounded answer with citations."""
        token, _ = await mcp_token()
        try:
            return await checked_result(token, reader.ask, question)
        except GraphMindError as exc:
            raise ToolError(exc.code) from exc

    @server.resource(
        "graphmind://documents/{document_id}",
        name="active-document",
        mime_type="application/json",
    )
    async def active_document(document_id: str) -> str:
        token, _ = await mcp_token()
        try:
            result = await checked_result(token, reader.fetch_document, document_id)
        except DocumentNotFoundError as exc:
            raise ResourceNotFoundError("document_not_found") from exc
        return json.dumps(result, ensure_ascii=False)

    async def begin_login(request: Request, *, signup: bool) -> Response:
        flow = new_browser_flow(request.query_params.get("return_to", "/"))
        redirect_uri = settings.public_base_url.rstrip("/") + "/auth/callback"
        try:
            destination = await selected_authority.authorization_url(
                redirect_uri=redirect_uri,
                state=flow["state"],
                nonce=flow["nonce"],
                code_challenge=flow["challenge"],
                signup=signup,
            )
        except IdentityError:
            return _error(503, "identity_unavailable", "Sign-in is temporarily unavailable")
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            FLOW_COOKIE,
            signer.seal(flow),
            max_age=600,
            **_cookie_options(settings),
        )
        return _security_headers(response)

    @server.custom_route("/login", methods=["GET"])
    async def login(request: Request) -> Response:
        return await begin_login(request, signup=False)

    @server.custom_route("/signup", methods=["GET"])
    async def signup(request: Request) -> Response:
        return await begin_login(request, signup=True)

    @server.custom_route("/auth/callback", methods=["GET"])
    async def callback(request: Request) -> Response:
        if request.query_params.get("error"):
            return _error(401, "authentication_failed", "Identity provider rejected the sign-in")
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        flow_cookie = request.cookies.get(FLOW_COOKIE, "")
        if not code or not state or not flow_cookie:
            return _error(400, "invalid_callback", "Sign-in callback is incomplete")
        try:
            flow = signer.open(flow_cookie)
        except IdentityResponseError:
            return _error(400, "invalid_callback", "Sign-in state is invalid or expired")
        try:
            if not secrets.compare_digest(str(flow.get("state", "")), state):
                return _error(400, "invalid_callback", "Sign-in state is invalid")
            tokens = await selected_authority.exchange_code(
                code=code,
                redirect_uri=settings.public_base_url.rstrip("/") + "/auth/callback",
                code_verifier=str(flow["verifier"]),
                nonce=str(flow["nonce"]),
            )
        except (IdentityGrantRejectedError, KeyError):
            return _error(401, "authentication_failed", "Sign-in could not be completed")
        except IdentityError:
            return _error(503, "identity_unavailable", "Sign-in is temporarily unavailable")
        response = RedirectResponse(str(flow.get("return_to", "/")), status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/", secure=settings.browser_cookie_secure)
        _set_token_cookies(response, settings, tokens)
        response.set_cookie(
            CSRF_COOKIE,
            secrets.token_urlsafe(32),
            max_age=8 * 60 * 60,
            **_cookie_options(settings, http_only=False, same_site="strict"),
        )
        return _security_headers(response)

    @server.custom_route("/auth/refresh", methods=["POST"])
    async def refresh(request: Request) -> Response:
        if not _valid_csrf(request, settings):
            return _error(403, "csrf_denied", "Request origin or CSRF token is invalid")
        token = request.cookies.get(REFRESH_COOKIE, "")
        if not token:
            return _error(401, "authentication_required", "Sign in again")
        try:
            tokens = await selected_authority.refresh(token)
        except IdentityGrantRejectedError:
            response = _error(401, "authentication_required", "Sign in again")
            _clear_auth_cookies(response, settings)
            return response
        except IdentityError:
            return _error(503, "identity_unavailable", "Session renewal is temporarily unavailable")
        response = _security_headers(JSONResponse({"status": "refreshed"}))
        _set_token_cookies(response, settings, tokens)
        return response

    @server.custom_route("/logout", methods=["POST"])
    async def logout(request: Request) -> Response:
        if not _valid_csrf(request, settings):
            return _error(403, "csrf_denied", "Request origin or CSRF token is invalid")
        for token in (request.cookies.get(ACCESS_COOKIE), request.cookies.get(REFRESH_COOKIE)):
            if token:
                try:
                    await selected_authority.revoke(token)
                except IdentityError:
                    return _error(503, "identity_unavailable", "Sign-out could not be confirmed; retry")
        response = _security_headers(JSONResponse({"status": "signed_out"}))
        _clear_auth_cookies(response, settings)
        return response

    @server.custom_route("/", methods=["GET"])
    async def browser_home(request: Request) -> Response:
        try:
            authenticated = await _browser_principal(request, selected_authority)
        except IdentityError:
            return _error(503, "identity_unavailable", "Identity service is unavailable")
        if authenticated is None and request.cookies.get(REFRESH_COOKIE):
            return _resume_page(request, settings)
        body = _login_page() if authenticated is None else _chat_page(authenticated[1].email)
        return _security_headers(HTMLResponse(body))

    async def browser_operation(request: Request, operation) -> Response:
        if not _valid_csrf(request, settings):
            return _error(403, "csrf_denied", "Request origin or CSRF token is invalid")
        try:
            authenticated = await _browser_principal(request, selected_authority)
            if authenticated is None:
                return _error(401, "authentication_required", "Sign in again")
            payload = await _request_json(request, settings)
            token, _ = authenticated
            result = await operation(payload)
            if not await _still_authorized(token, selected_authority):
                return _error(401, "authorization_revoked", "Account access was revoked")
            reader.validate_result(result)
            return _security_headers(JSONResponse(result))
        except IdentityError:
            return _error(503, "identity_unavailable", "Identity service is unavailable")
        except DocumentNotFoundError:
            return _error(404, "document_not_found", "Document is unavailable")
        except QueryCapacityError:
            return _error(503, "query_capacity_exceeded", "Reader capacity is full; retry")
        except RetryableQueryError:
            return _error(409, "retryable_query_error", "Document version changed; retry")
        except GraphMindError as exc:
            return _query_error(exc)
        except ValueError:
            return _error(400, "invalid_request", "Request was rejected")

    @server.custom_route("/api/search", methods=["POST"])
    async def browser_search(request: Request) -> Response:
        async def run(payload: dict[str, Any]):
            query = payload.get("query")
            limit = payload.get("limit", 8)
            if not isinstance(query, str) or not isinstance(limit, int):
                raise ValueError("Invalid search request")
            return await reader_work(reader.search_documents, query, limit=limit, request=request)

        return await browser_operation(request, run)

    @server.custom_route("/api/ask", methods=["POST"])
    async def browser_ask(request: Request) -> Response:
        async def run(payload: dict[str, Any]):
            question = payload.get("question")
            if not isinstance(question, str):
                raise ValueError("Invalid question")
            return await reader_work(reader.ask, question, request=request)

        return await browser_operation(request, run)

    @server.custom_route("/documents/{document_id}", methods=["GET"])
    async def browser_document(request: Request) -> Response:
        try:
            authenticated = await _browser_principal(request, selected_authority)
            if authenticated is None:
                if request.cookies.get(REFRESH_COOKIE):
                    return _resume_page(request, settings)
                return _error(401, "authentication_required", "Sign in again")
            token, _ = authenticated
            result = await reader_work(
                reader.fetch_document,
                request.path_params["document_id"],
                version_id=request.query_params.get("version_id"),
                request=request,
            )
            if not await _still_authorized(token, selected_authority):
                return _error(401, "authorization_revoked", "Account access was revoked")
            reader.validate_result(result)
            body = f"{result['display_name']}\n\n{result['text']}"
            return _security_headers(PlainTextResponse(body))
        except IdentityError:
            return _error(503, "identity_unavailable", "Identity service is unavailable")
        except DocumentNotFoundError:
            return _error(404, "document_not_found", "Document is unavailable")
        except QueryCapacityError:
            return _error(503, "query_capacity_exceeded", "Reader capacity is full; retry")
        except RetryableQueryError:
            return _error(409, "retryable_query_error", "Document version changed; retry")
        except GraphMindError as exc:
            return _query_error(exc)
        except ValueError:
            return _error(400, "invalid_request", "Request was rejected")

    @server.custom_route("/health/live", methods=["GET"])
    async def live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def probe_readiness():
        results = await asyncio.gather(
            asyncio.to_thread(application.reader_readiness), selected_authority.readiness(),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            return False
        statuses, identity_ready = results
        return identity_ready and all(item.available for item in statuses)

    readiness = BoundedReadiness(probe_readiness)

    @server.custom_route("/health/ready", methods=["GET"])
    async def ready(_: Request) -> Response:
        available = await readiness.available()
        return _security_headers(JSONResponse({"status": "ready" if available else "unavailable"},
                                              status_code=200 if available else 503))

    @server.custom_route("/assets/graphmind.css", methods=["GET"])
    async def stylesheet(_: Request) -> Response:
        return PlainTextResponse(CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})

    @server.custom_route("/assets/graphmind.js", methods=["GET"])
    async def javascript(_: Request) -> Response:
        return PlainTextResponse(
            JAVASCRIPT,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=settings.browser_max_request_bytes,
        transport_security=_transport_security(settings),
    )
    return server, ProductBoundary(app, verifier)
