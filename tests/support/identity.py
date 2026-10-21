"""Synthetic identity/browser fixtures shared across contract tests."""
import time
from pathlib import Path
from types import SimpleNamespace

from graphmind.config import Settings
from graphmind.domain import (
    AdapterStatus, Chunk, Document, Evidence, Outcome, QueryResult, Version, VersionStatus,
)
from graphmind.identity import IdentityPrincipal, TokenSet


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
