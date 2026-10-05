# GraphMind browser, identity, and MCP contract

This is the implementation contract for the P5/P6 candidate. It is not a claim
that Google, SMTP, Claude Code, EC2, Windows 11, or Windows Server 2025 acceptance
has already passed.

## Identity ownership

Keycloak 26.7.3 owns email, password hashes, email verification, Google identity
links, account status, login sessions, OAuth grants, signing keys, and schema
migrations. Its production data is stored in PostgreSQL 17 on the same host for
the initial single-instance deployment. GraphMind stores no password and does
not accept a Google ID token as a resource credential.

GraphMind is a stateless OAuth resource server and OIDC relying party. It calls
Keycloak token introspection for every browser request and MCP call. An account
must be active, email-verified, scoped with `graphmind:read`, and have the exact
configured MCP resource audience. Eligibility is checked again after a long
retrieval or model call before its result is returned.

The browser authorization-code flow uses PKCE, state, and nonce. Its short-lived
flow state is HMAC-signed in an HttpOnly cookie. Access and refresh credentials
use Secure, HttpOnly cookies and never appear in URLs, browser storage, response
bodies, or logs. State-changing browser routes require an exact Origin and a
double-submit CSRF token. Local HTTP is permitted only for loopback development.
Secure cookies can only be disabled for an explicit loopback HTTP URL; public
HTTPS configuration cannot opt out. A returning reader with an expired access
cookie sees a no-content renewal page; that page POSTs with Origin+CSRF before
returning to chat or the exact local source link. GET never refreshes credentials.
An invalid/revoked refresh grant clears cookies; an upstream outage returns 503
and retains credentials for retry. Network/JSON/script failures leave the UI's
waiting state and service errors are never labelled successful abstention.

## Public routes

| Route | Owner | Authentication and behavior |
| --- | --- | --- |
| `GET /` | GraphMind | Shows sign-in, escaped chat, or a protected POST-based renewal page. |
| `GET /login`, `GET /signup` | GraphMind → Keycloak | Begins code+PKCE; signup asks Keycloak to register. |
| `GET /auth/callback` | GraphMind | Checks signed state and ID-token nonce, exchanges the code, and sets cookies. |
| `POST /auth/refresh`, `POST /logout` | GraphMind → Keycloak | Requires Origin+CSRF; rotates or revokes credentials. |
| `POST /api/search`, `POST /api/ask` | GraphMind | Requires a current verified active account. |
| `GET /documents/{opaque-id}` | GraphMind | Returns active-version plain text, or renewal UI before any content when access expired. |
| `GET /health/live`, `GET /health/ready` | GraphMind | Returns coarse status without secrets or document details. |
| `POST/GET/DELETE /mcp` | MCP SDK | OAuth-protected Streamable HTTP; request bodies are bounded. |
| `GET /.well-known/oauth-protected-resource/mcp` | MCP SDK | RFC 9728 resource metadata for OAuth discovery. |
| Keycloak realm OIDC endpoints | Keycloak | Login, registration, verification, Google broker, tokens, and revocation. |

Reader routes cannot import documents, change configuration, manage accounts, or
address a filesystem path or arbitrary URL. Document ingestion/configuration
remain host-local CLI operations.

## MCP contracts

- `search_documents(query, limit=8)` returns evidence and citations so a client
  may synthesize with its own model.
- `fetch_document(document_id, version_id=None, max_chars=12000)` returns bounded
  text only from the currently active document version. IDs are opaque.
- `ask(question)` uses the server's configured answer model and returns the
  grounded result and citations. Readers never provide model credentials.
- `graphmind://documents/{document_id}` is the equivalent active-document
  resource.

The server uses MCP SDK 2.0.1, the 2026-07-28 protocol where supported, and the
2025-11-25 compatibility path. It is stateless over Streamable HTTP. SSE and the
legacy static/shared bearer key are removed from the release contract.

## Claude Code candidate setup

The initial tested client target is Claude Code CLI. Keycloak anonymous dynamic
registration remains disabled. Register the public client in the realm template,
then configure the fixed loopback callback:

```sh
claude mcp add --transport http \
  --client-id graphmind-claude-code \
  --callback-port 8765 \
  graphmind https://rag.example.com/mcp
```

Run `/mcp` in Claude Code and complete browser login. Claude Code must be version
2.1.64 or newer for the documented metadata override behavior; the exact tested
version will be recorded during real-client acceptance. Readers install Claude
Code or another MCP client, not the GraphMind server package.

The tracked `.mcp.json` shows the same portable configuration without a secret.
Other clients must support Streamable HTTP, OAuth authorization-server discovery,
authorization code with PKCE, and a pre-registered public client or an
operator-approved registration. Untested clients are not advertised as verified.

## Host account controls

```sh
graphmind accounts list
graphmind accounts disable reader@example.com
graphmind accounts enable reader@example.com
graphmind accounts delete reader@example.com
graphmind accounts restore reader@example.com
```

`delete` keeps email, password hash, identity links, and security metadata in
Keycloak while disabling the account and revoking sessions. `enable` cannot
resurrect a deleted account; `restore` is an explicit host-only action. Keycloak's
service account receives only the three user-management roles documented in
`deploy/keycloak/README.md`.
The public reader requires browser/resource/flow secrets only; it must not be
given the account-admin secret. Host account commands require only the admin
client secret, not browser/resource/flow credentials. Account listing follows
100-row pages up to an explicit 10,000-account bound; overflow or repeated pages
fails visibly rather than silently returning an incomplete list. Offset listing
is not a transactional snapshot: avoid concurrent account creation/deletion or
retry if the authority reports duplicates.

The realm also requires the supplied `graphmind-user-profile.json`, applied and
read back by the bootstrap operator before public signup. Security attributes
are managed and administrator-only; registration does not require first/last
name. Supported host account mutations share a non-blocking OS lock, preserve
deletion across later disable operations, and verify persisted state after
session revocation. Do not concurrently edit these users through other APIs.

## Foundation safeguards

Search and server-generated answers share running/waiting admission limits.
HTTP/MCP reader work is bounded before executor submission. Cancellation or
browser disconnect signals the worker; synchronous inference already in flight
must finish or time out before its slot is released. Its cancelled result is
discarded. Real remote-client cancellation remains an integration acceptance
case, and installation-wide multi-process budgeting remains P7 work.

Sources are checked for active-version eligibility after retrieval/fetch and
again after asynchronous authorization, before delivery. Version races return
a retryable error; historical versions are filtered before vector/graph limits.
Short filtered Chroma results use a paged eligible-vector fallback to handle
the pinned runtime's observed indexing-buffer omission. Measure that fallback
under the P7 corpus/load targets.

Ingestion has an OS-held, host-local writer lock in addition to durable leases.
Publication and version-status writes check the current job attempt and both
leases. Lost ownership stops later writes, cannot publish, and requires
reconciliation/retry. Do not remove lock files while services run or use a
network filesystem for these locks. Neo4j node/reference updates commit in one
transaction, affecting only the imported version and references to its document.

Embedding configuration is checked against runtime digest/precision, including
before and after each inference batch. Keep model tags immutable while services
run; do not retag/pull/create models concurrently with indexing or queries.
Detected changes fail closed; restart and explicitly reindex with the accepted
new identity. Ollama's tag-addressed API does not provide an atomic digest-bound
inference contract, so metadata checks do not replace this host ownership rule.

## Readiness, failures and logs

`/health/live` is process liveness. `/health/ready` uses fresh OIDC discovery and
light storage/embedding/answer-model metadata checks, with a five-second response
budget and only one in-flight probe per process. A timed-out worker retains its
probe slot until it finishes; requests cannot accumulate health threads. It
does not perform corpus-wide parity audits or generate paid answers. Hosted
readiness requires that endpoint's configured model to appear in `/api/tags`;
this does not prove generation permission/quota/quality. `graphmind doctor`
retains the host-only full manifest audit. Public status is coarse and no-store.

Provider/capacity/timeout failures return typed errors (browser 503/504, malformed
dependency response 502), not HTTP-200 abstention. MCP tool failures set isError;
an introspection outage before SDK authorization returns 503, not invalid-token
401. Actual invalid/revoked tokens still receive the standard bearer challenge.

`graphmind serve` disables Uvicorn's query-bearing protocol access log and emits
method/canonical-route/status only through `graphmind.access`. External ASGI
launchers and proxies must also disable query/header/body logging, including
callback error logs. See the [real acceptance procedure](deploy/acceptance/README.md)
for the exact log-canary check; source tests cannot qualify a configured proxy.

## Acceptance still required

Before P5/P6 gates can be called complete, run the same candidate through real
SMTP/email verification and failure, Google sign-in/linking, restart-safe account
revocation, two-user browser isolation, Claude Code login/refresh/reconnect, and
an independent MCP SDK client. Repeat native runtime startup on Ubuntu 24.04,
Windows 11, and Windows Server 2025. Record exact client/runtime versions and
keep the admin console private.
