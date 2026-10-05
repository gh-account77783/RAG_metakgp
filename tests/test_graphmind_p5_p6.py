from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from graphmind.config import Settings
from graphmind.domain import (
    AdapterStatus,
    Chunk,
    Document,
    Evidence,
    Outcome,
    QueryResult,
    Version,
    VersionStatus,
)
from graphmind.errors import (
    AccountStateError,
    ConfigurationError,
    DocumentNotFoundError,
    IdentityResponseError,
)
from graphmind.identity import (
    FlowCookieSigner,
    IdentityPrincipal,
    KeycloakAdminClient,
    KeycloakAuthority,
    TokenSet,
    new_browser_flow,
)
from graphmind.product_server import create_product_server
from graphmind.reader import ReaderService


def identity_settings(root: Path) -> Settings:
    return Settings(
        data_dir=root / "data",
        allowed_import_roots=(root,),
        public_base_url="https://rag.example.test",
        identity_issuer_url="https://auth.example.test/realms/graphmind",
        identity_mcp_audience="https://rag.example.test/mcp",
        identity_browser_client_secret="browser-secret-which-is-private",
        identity_resource_client_secret="resource-secret-which-is-private",
        identity_admin_client_secret="admin-secret-which-is-private",
        identity_flow_signing_key="flow-signing-key-is-at-least-thirty-two-bytes",
    )


def metadata_document() -> tuple[Document, Version, list[Chunk]]:
    document = Document(
        document_id="doc-opaque",
        collection_id="default",
        display_name="Guide <unsafe>.txt",
        normalized_name="guide <unsafe>.txt",
        media_type="text/plain",
        source_key="private",
        created_at="2026-09-16T00:00:00Z",
        active_version_id="version-active",
    )
    version = Version(
        version_id="version-active",
        document_id=document.document_id,
        content_hash="a" * 64,
        extractor_fingerprint="extractor",
        chunker_fingerprint="chunker",
        embedding_fingerprint="embedding",
        private_file_ref="private",
        source_size=20,
        expected_chunk_count=1,
        chunk_digest="b" * 64,
        status=VersionStatus.READY,
        created_at="2026-09-16T00:00:00Z",
    )
    chunks = [
        Chunk(
            chunk_id="chunk-1",
            version_id=version.version_id,
            document_id=document.document_id,
            ordinal=0,
            text="Safe source text with <script>alert(1)</script> kept as plain text.",
            locator="line 1",
            start_offset=0,
            end_offset=65,
        )
    ]
    return document, version, chunks


class FakeMetadata:
    def __init__(self) -> None:
        self.document_value, self.version_value, self.chunks = metadata_document()

    def document(self, document_id):
        return self.document_value if document_id == self.document_value.document_id else None

    def version(self, version_id):
        return self.version_value if version_id == self.version_value.version_id else None

    def all_chunks_for_version(self, version_id):
        return list(self.chunks) if version_id == self.version_value.version_id else []

    def iter_chunks_for_version(self, version_id):
        yield from self.all_chunks_for_version(version_id)

    def active_version_ids(self):
        return ({self.document_value.active_version_id}
                if self.document_value.deleted_at is None else set())


class FakeApplication:
    def __init__(self) -> None:
        self.metadata = FakeMetadata()
        evidence = Evidence(
            citation_id="citation-1",
            document_id="doc-opaque",
            version_id="version-active",
            display_name="Guide <unsafe>.txt",
            locator="line 1",
            excerpt="Safe source text",
            via="vector",
            score=0.9,
        )
        self.retrieval = SimpleNamespace(retrieve=lambda query: [evidence])
        self.answers = SimpleNamespace(
            search=lambda query, **kwargs: [evidence],
            query=lambda question, **kwargs: QueryResult(
                Outcome.ANSWER,
                "Grounded answer",
                (evidence,),
                "request-1",
                {"citations_valid": True},
            )
        )

    def readiness(self):
        return (AdapterStatus("test", True, "ready"),)

    def reader_readiness(self):
        return self.readiness()


class FakeAuthority:
    issuer_url = "https://auth.example.test/realms/graphmind"

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.principal = IdentityPrincipal(
            subject="user-1",
            email="reader+<unsafe>@example.test",
            client_id="graphmind-web",
            scopes=("openid", "email", "graphmind:read"),
            expires_at=int(time.time()) + 600,
            issuer=self.issuer_url,
            audience=("https://rag.example.test/mcp",),
        )
        self.valid_tokens = {"access": self.principal, "refreshed": self.principal}

    async def verify_token(self, token):
        return self.valid_tokens.get(token)

    async def readiness(self):
        return True

    async def authorization_url(self, **kwargs):
        query = {key: value for key, value in kwargs.items() if key != "signup"}
        query["signup"] = str(kwargs["signup"]).lower()
        query["code_challenge_method"] = "S256"
        from urllib.parse import urlencode

        return "https://auth.example.test/authorize?" + urlencode(query)

    async def exchange_code(self, **kwargs):
        if kwargs["code"] != "valid-code":
            raise AssertionError("unexpected code")
        return TokenSet("access", "refresh", "id-token", 600)

    async def refresh(self, refresh_token):
        if refresh_token != "refresh":
            raise AssertionError("unexpected refresh token")
        return TokenSet("refreshed", "refresh-2", None, 600)

    async def revoke(self, token):
        self.revoked.append(token)
        self.valid_tokens.pop(token, None)


class FakeIdTokenValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def validate(self, token, *, nonce):
        self.calls.append((token, nonce))
        return {"sub": "user-1", "nonce": nonce}


class FakeTransport:
    def __init__(self, introspection: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.introspection = introspection or {}

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/.well-known/openid-configuration"):
            issuer = "https://auth.example.test/realms/graphmind"
            return {
                "issuer": issuer,
                "authorization_endpoint": issuer + "/protocol/openid-connect/auth",
                "token_endpoint": issuer + "/protocol/openid-connect/token",
                "introspection_endpoint": issuer + "/protocol/openid-connect/token/introspect",
                "revocation_endpoint": issuer + "/protocol/openid-connect/revoke",
                "jwks_uri": issuer + "/protocol/openid-connect/certs",
            }
        if url.endswith("/token/introspect"):
            return dict(self.introspection)
        if url.endswith("/token"):
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "expires_in": 300,
            }
        if url.endswith("/revoke"):
            return {}
        raise AssertionError(f"unexpected URL {url}")


class FakeAdminTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.user = {
            "id": "account-1",
            "username": "reader@example.test",
            "email": "Reader@Example.Test",
            "emailVerified": True,
            "enabled": True,
            "createdTimestamp": 1,
            "attributes": {"graphmind_status": ["active"]},
        }

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/protocol/openid-connect/token"):
            return {"access_token": "admin-token"}
        if method == "GET" and "/users?" in url:
            return [dict(self.user)]
        if method == "GET" and "/users/" in url:
            return dict(self.user)
        if method == "PUT":
            self.user.update(kwargs["payload"])
            return {}
        if method == "POST":
            return {}
        raise AssertionError((method, url))


class IdentityContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_introspection_enforces_issuer_audience_scope_email_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            accepted = {
                "active": True,
                "iss": settings.identity_issuer_url,
                "aud": [settings.identity_resource_client_id, settings.identity_mcp_audience],
                "scope": "openid email graphmind:read",
                "sub": "user-1",
                "email": "Reader@Example.Test",
                "email_verified": True,
                "client_id": "graphmind-claude-code",
                "graphmind_status": "active",
                "exp": int(time.time()) + 300,
            }
            transport = FakeTransport(accepted)
            authority = KeycloakAuthority(
                settings, transport=transport, id_token_validator=FakeIdTokenValidator()
            )
            principal = await authority.verify_token("opaque-access-token")
            self.assertIsNotNone(principal)
            self.assertEqual(principal.email, "reader@example.test")
            introspection_call = transport.calls[-1][2]["form"]
            self.assertEqual(introspection_call["client_id"], "graphmind-resource")
            self.assertNotIn("opaque-access-token", repr(principal))

            for change in (
                {"active": False},
                {"iss": "https://foreign.example/realms/wrong"},
                {"aud": [settings.identity_resource_client_id]},
                {"scope": "openid email"},
                {"email_verified": False},
                {"graphmind_status": "deleted"},
                {"graphmind_status": "unknown-state"},
                {"exp": int(time.time()) - 1},
            ):
                with self.subTest(change=change):
                    transport.introspection = accepted | change
                    self.assertIsNone(await authority.verify_token("opaque-access-token"))

    async def test_code_exchange_validates_nonce_then_rechecks_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            transport = FakeTransport(
                {
                    "active": True,
                    "iss": settings.identity_issuer_url,
                    "aud": [settings.identity_resource_client_id, settings.identity_mcp_audience],
                    "scope": "openid email graphmind:read",
                    "sub": "user-1",
                    "email": "reader@example.test",
                    "email_verified": True,
                    "client_id": settings.identity_browser_client_id,
                    "exp": int(time.time()) + 300,
                }
            )
            validator = FakeIdTokenValidator()
            authority = KeycloakAuthority(
                settings, transport=transport, id_token_validator=validator
            )
            tokens = await authority.exchange_code(
                code="one-time-code",
                redirect_uri=settings.public_base_url + "/auth/callback",
                code_verifier="pkce-verifier",
                nonce="expected-nonce",
            )
            self.assertEqual(tokens.access_token, "access-token")
            self.assertEqual(validator.calls, [("id-token", "expected-nonce")])
            token_form = next(call[2]["form"] for call in transport.calls if call[1].endswith("/token"))
            self.assertEqual(token_form["code_verifier"], "pkce-verifier")
            self.assertEqual(token_form["client_secret"], settings.identity_browser_client_secret)

    async def test_soft_delete_retains_user_and_revokes_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeAdminTransport()
            client = KeycloakAdminClient(identity_settings(Path(temporary)), transport=transport)
            result = await client.set_state("reader@example.test", "delete")
            self.assertEqual(result["status"], "deleted")
            methods = [method for method, _, _ in transport.calls]
            self.assertNotIn("DELETE", methods)
            update = next(call for call in transport.calls if call[0] == "PUT")[2]["payload"]
            self.assertFalse(update["enabled"])
            self.assertEqual(update["email"], "Reader@Example.Test")
            self.assertEqual(update["attributes"]["graphmind_status"], ["deleted"])
            self.assertIn("graphmind_deleted_at", update["attributes"])
            self.assertTrue(any(url.endswith("/users/account-1/logout") for _, url, _ in transport.calls))

            transport.user["attributes"] = {"graphmind_status": ["deleted"]}
            with self.assertRaises(AccountStateError):
                await client.set_state("reader@example.test", "enable")


class FlowAndConfigurationTests(unittest.TestCase):
    def test_signed_flow_expiry_tamper_and_safe_return_path(self) -> None:
        signer = FlowCookieSigner("x" * 40, lifetime_seconds=60)
        token = signer.seal({"state": "state", "return_to": "/chat"}, now=100)
        self.assertEqual(signer.open(token, now=120)["return_to"], "/chat")
        with self.assertRaises(IdentityResponseError):
            signer.open(token + "tamper", now=120)
        with self.assertRaises(IdentityResponseError):
            signer.open(token, now=161)
        self.assertEqual(new_browser_flow("//foreign.example")["return_to"], "/")

    def test_identity_settings_require_https_match_audience_and_redact_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            settings.validate(require_identity_secrets=True)
            diagnostics = settings.diagnostics()
            for name in (
                "identity_browser_client_secret",
                "identity_resource_client_secret",
                "identity_admin_client_secret",
                "identity_flow_signing_key",
            ):
                self.assertEqual(diagnostics[name], "<configured>")
            with self.assertRaises(ConfigurationError):
                replace(settings, public_base_url="http://public.example").validate()
            with self.assertRaises(ConfigurationError):
                replace(settings, identity_mcp_audience="https://rag.example.test/wrong").validate()


class ReaderContractTests(unittest.TestCase):
    def test_search_ask_and_active_only_bounded_fetch(self) -> None:
        reader = ReaderService(FakeApplication(), max_fetch_chars=30)
        search = reader.search_documents("safe", limit=1)
        self.assertEqual(search["results"][0]["document_id"], "doc-opaque")
        answer = reader.ask("question")
        self.assertEqual(answer["answer"], "Grounded answer")
        fetched = reader.fetch_document("doc-opaque", max_chars=30)
        self.assertTrue(fetched["truncated"])
        self.assertLessEqual(len(fetched["text"]), 30)
        with self.assertRaises(DocumentNotFoundError):
            reader.fetch_document("doc-opaque", version_id="stale-version")


class BrowserAndMCPContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = identity_settings(Path(self.temporary.name))
        self.authority = FakeAuthority()
        self.server, self.app = create_product_server(
            self.settings, FakeApplication(), authority=self.authority
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_browser_pkce_callback_csrf_chat_plain_source_and_logout(self) -> None:
        with TestClient(self.app, base_url=self.settings.public_base_url) as client:
            landing = client.get("/")
            self.assertEqual(landing.status_code, 200)
            self.assertIn("Create account", landing.text)
            self.assertNotIn("reader+", landing.text)

            login = client.get("/login", follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            query = parse_qs(urlparse(login.headers["location"]).query)
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertIn("HttpOnly", login.headers["set-cookie"])
            self.assertIn("Secure", login.headers["set-cookie"])

            callback = client.get(
                "/auth/callback",
                params={"code": "valid-code", "state": query["state"][0]},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303)
            self.assertNotIn("access", callback.text)
            self.assertNotIn("access", callback.headers["location"])

            chat = client.get("/")
            self.assertIn("reader+&lt;unsafe&gt;@example.test", chat.text)
            csrf = client.cookies.get("graphmind_csrf")
            ask = client.post(
                "/api/ask",
                json={"question": "question"},
                headers={"Origin": self.settings.public_base_url, "X-GraphMind-CSRF": csrf},
            )
            self.assertEqual(ask.status_code, 200, ask.text)
            self.assertEqual(ask.json()["answer"], "Grounded answer")

            denied = client.post(
                "/api/ask",
                json={"question": "question"},
                headers={"Origin": "https://foreign.example", "X-GraphMind-CSRF": csrf},
            )
            self.assertEqual(denied.status_code, 403)

            source = client.get("/documents/doc-opaque?version_id=version-active")
            self.assertEqual(source.status_code, 200)
            self.assertEqual(source.headers["content-type"].split(";")[0], "text/plain")
            self.assertIn("<script>alert(1)</script>", source.text)

            logout = client.post(
                "/logout",
                json={},
                headers={"Origin": self.settings.public_base_url, "X-GraphMind-CSRF": csrf},
            )
            self.assertEqual(logout.status_code, 200)
            self.assertEqual(set(self.authority.revoked), {"access", "refresh"})

    def test_mcp_discovery_denial_and_portable_contracts(self) -> None:
        tool_names = {item.name for item in asyncio.run(self.server.list_tools())}
        self.assertEqual(tool_names, {"search_documents", "fetch_document", "ask"})
        templates = asyncio.run(self.server.list_resource_templates())
        self.assertEqual(len(templates), 1)
        self.assertEqual(str(templates[0].uri_template), "graphmind://documents/{document_id}")

        with TestClient(self.app, base_url=self.settings.public_base_url) as client:
            denied = client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "contract-test", "version": "1"},
                    },
                },
            )
            self.assertEqual(denied.status_code, 401)
            self.assertIn("resource_metadata=", denied.headers.get("www-authenticate", ""))

            metadata = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(metadata.status_code, 200, metadata.text)
            payload = metadata.json()
            self.assertEqual(payload["resource"], self.settings.identity_mcp_audience)
            self.assertEqual(payload["authorization_servers"], [self.authority.issuer_url])
            self.assertEqual(payload["scopes_supported"], ["graphmind:read"])

            initialized = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer access",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "independent-sdk", "version": "1"},
                    },
                },
            )
            self.assertEqual(initialized.status_code, 200, initialized.text)
            self.assertIn("result", initialized.json())

            tools = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer access",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            )
            self.assertEqual(tools.status_code, 200, tools.text)
            listed = {item["name"] for item in tools.json()["result"]["tools"]}
            self.assertEqual(listed, {"search_documents", "fetch_document", "ask"})

            called = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer access",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "search_documents", "arguments": {"query": "safe"}},
                },
            )
            self.assertEqual(called.status_code, 200, called.text)
            self.assertFalse(called.json()["result"]["isError"])
            self.assertEqual(
                called.json()["result"]["structuredContent"]["results"][0]["document_id"],
                "doc-opaque",
            )

    def test_tracked_client_and_realm_templates_have_safe_defaults(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        client = json.loads((repository / ".mcp.json").read_text(encoding="utf-8"))
        definition = client["mcpServers"]["graphmind-remote"]
        self.assertEqual(definition["type"], "http")
        self.assertNotIn("headers", definition)
        self.assertEqual(definition["oauth"]["clientId"], "graphmind-claude-code")
        self.assertEqual(definition["oauth"]["callbackPort"], 8765)

        realm = json.loads(
            (repository / "deploy" / "keycloak" / "graphmind-realm.template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(realm["registrationAllowed"])
        self.assertTrue(realm["verifyEmail"])
        self.assertFalse(realm["resetPasswordAllowed"])
        google = next(item for item in realm["identityProviders"] if item["alias"] == "google")
        self.assertFalse(google["enabled"])
        claude = next(item for item in realm["clients"] if item["clientId"] == "graphmind-claude-code")
        self.assertTrue(claude["publicClient"])
        self.assertEqual(claude["attributes"]["pkce.code.challenge.method"], "S256")

    def test_legacy_public_entry_points_are_retired(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for relative in ("app.py", "mcp_server.py"):
            source = (repository / relative).read_text(encoding="utf-8")
            self.assertIn("legacy", source.lower())
            self.assertIn("graphmind serve", source)
            self.assertNotIn("GRAPHMIND_API_KEY", source)
            self.assertNotIn("GOOGLE_CLIENT_ID", source)
            self.assertNotIn("query_string", source)

        deployment = (repository / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("deploy.sh is retired", deployment)
        self.assertNotIn("mcp_server:app", deployment)
        self.assertNotIn("GRAPHMIND_API_KEY", deployment)


if __name__ == "__main__":
    unittest.main()
