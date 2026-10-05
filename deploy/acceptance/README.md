# Disposable P5/P6 EC2 acceptance — not a production installer

Status: prepared procedure; no real-service acceptance is claimed until each
row below has evidence from the same candidate. Offline tests and an installed
wheel cannot establish mail delivery, Google linking or client OAuth.

## Before SSH/testing

The owner supplies an approved disposable Ubuntu 24.04 EC2 host, SSH username,
host/DNS, key **local path** or existing SSH config alias, and allowed test scope.
Do not paste a private key/password/token into chat. Confirm existing services,
data ownership, available disk/RAM and whether sudo/reboot/service interruption
is permitted. Do not change security groups, DNS, spend or overwrite a live
installation without separate approval.

Prepare test GraphMind/auth hostnames and TLS, Google's OAuth broker client,
SMTP sender and two controlled test mailboxes, and exact pinned Claude Code and
independent-client versions. Missing inputs mean pending checks, not passes.
Private secrets are supplied through host-local protected configuration, never
argv, URLs, shell tracing, commits or reports. Keep all local components on the
one EC2 host; hosted-answer calls are the accepted exception.

Use CPython 3.13, the checked-in constraints, Keycloak 26.7.3, PostgreSQL 17,
the recorded Neo4j edition/version and approved Ollama/model identities. Record
artifact hashes, Git base **and all dirty-file hashes**, OS/runtime versions,
proxy URLs and settings with secrets redacted. Do not stage/commit/push the
candidate merely to make an old test-deploy script accept a clean checkout.
Transfer the allowlisted wheel/sdist and operator assets by approved SSH/SCP;
compare SHA-256 at both ends. Preserve the original checkout and stores.

## Isolation and exposure

- Create a new private run/data/database/collection root and synthetic documents;
  never target existing MetaKGP or production state. Keep PostgreSQL, Neo4j,
  Chroma, Ollama and Keycloak admin interfaces private. Use one ingestion writer
  and one public reader process until installation-wide budgets are qualified.
- Follow [the Keycloak bootstrap](../keycloak/README.md), import the realm,
  apply/read back the separate minimal user profile and least-privilege roles.
  Render secrets only in the private run directory. PostgreSQL, not the dev-file
  database, owns persistent identity state.
- Public reader configuration contains only browser/resource/flow secrets and
  RAG dependencies; **no account-admin secret**. A separate protected host-account
  CLI environment contains the admin secret. No remote account mutation route.
- `graphmind --dotenv /private/reader.env serve --host 127.0.0.1 --port 8000`
  disables Uvicorn's query-bearing access log and emits canonical-route/status
  records. If another ASGI launcher is used, disable its protocol access log too.
- A proxy must not log request targets/queries, Authorization/Cookie headers or
  bodies. For Nginx, use a dedicated format containing `$request_method $uri
  $status` (not `$request`, `$request_uri` or `$args`); restrict/error-log handling
  for the callback, because even error messages can contain the request target.
  For this disposable test, turn callback access/error logging off and verify
  synthetic code/state markers are absent across **all** enabled proxy/app logs.
  Do not capture browser HARs or raw requests containing credentials.

## Executable checks

From a checkout containing tests, in an isolated constrained test venv:

```sh
python -m pip install --constraint constraints/core-py313.txt --constraint constraints/linux-py313.txt '.[test]' setuptools==80.9.0 wheel==0.45.1
python -m pip check
python -B -m unittest discover -s tests -v
python tools/verify_package.py --output /private/run/artifacts
python tools/run_real_acceptance.py --stores --dotenv /private/run/test.env
python tools/run_real_acceptance.py --identity --dotenv /private/run/reader.env
python tools/mcp_acceptance.py https://rag.test.example/mcp
```

`--identity` requires privately loaded `GRAPHMIND_TEST_ACCESS_TOKEN` and
`GRAPHMIND_TEST_REVOKED_TOKEN` from disposable readers. It checks discovery and
introspection only; no implicit repository `.env` is read. The MCP script uses
the official SDK over the network with a previously obtained test grant; it
does **not** establish interactive PKCE/refresh interoperability. Obtain that
grant with the independent client's real PKCE flow, not a password grant,
universal service token or copied Google ID token. Never put grants on argv.

The wheel includes configuration/auth/bootstrap/acceptance assets and network
client tools under `<venv>/share/graphmind` (`<venv>/data/share/graphmind` on
platforms using that data scheme; inspect the installed file inventory).
Real-store tests and package-build verification require the development checkout;
original data and tests are deliberately not shipped in the wheel.

## Mandatory acceptance matrix

Record pass/fail/pending, timestamp, candidate hash, exact versions, expected and
observed behavior, sanitized evidence and cleanup for **every** row. Stop public
exposure on authorization bypass, leaked credentials or inconsistent retained
state. No skipped/unavailable row counts as passed.

| Area | Required observed behavior |
| --- | --- |
| Profile/authority | Read-back has required email, no required first/last name, managed admin-only status/deleted-at/epoch attributes; readers cannot change them. Only one credential store. |
| Persistence | Restart Keycloak/PostgreSQL with permission; accounts, verification, broker links and retained tombstones persist. No application-side password store. |
| Password signup/email | Both users sign up; unverified access denied; real verification delivered; expired/replayed link, resend/abuse and SMTP failure handled safely. |
| Google | Real broker callback/login; stable subject mapping, verified-email policy, existing local-email conflict/link proof, Google-only account and no account mixups. Do not approve an unreviewed re-registration policy. |
| Browser | PKCE/state/nonce denial, expired/tampered callback, CSRF/Origin denial, safe rendering, ask/search/source, active-version races, both users, expiry/reload/source refresh and logout. Cookies Secure/HttpOnly; no tokens in page/storage/URLs. |
| Account controls | Host disable/enable/delete/restore, deleted→disable→enable denied, repeated deletion retains original timestamp, read-back persists security attributes and revocation works across browser/MCP/processes/restart. Listing >100 accounts uses pages; explicit bound reported. |
| TLS/proxy | Exact public discovery/audience/callback, restricted Host/Origin, no public admin/data/model ports, no callback code/state in access/error logs. Anonymous and foreign/static/Google/query credentials denied. |
| Readiness/errors | Fresh identity/provider outages after a successful probe yield bounded 503, liveness remains responsive, no corpus audit/secret details; invalid grant is not an outage; service failure is not a successful abstention, UI leaves waiting state. |
| Claude Code | Record exact CLI version; configure pre-registered public client/8765 callback; real interactive login, list/call all tools, source use, renewal/reconnect and revoked access; repeat for both users and both model modes. |
| Independent MCP | Real client discovery/PKCE login, network initialize/list/search/fetch/ask/resource, reconnect/renewal/revoke; cannot substitute the already-issued-token script for OAuth acceptance. |
| Changed stores/worker | Real Neo4j rollback/scoped-reference/concurrent-reader tests, eligible Chroma retrieval, Linux cross-process writer exclusion/expired-lease recovery; current Ollama digest/precision before/after batch, no concurrent tag mutation. |
| Cancellation/resources | Real disconnect/cancel signals work, bounded queues/admission and no early slot release; record CPU/RAM/disk and errors without claiming final P7 capacity. |

Keep failed attempts. Review the matrix before promoting P5/P6 gates. Native
Windows 11/Server 2025 service/reboot acceptance, full model quality/load,
password recovery, consistent backup/restore and release rights remain separate
later gates. Cleanup only the exact agent-created test resources after verifying
their identifiers; preserve logs/reports and never delete broad runtime roots.
