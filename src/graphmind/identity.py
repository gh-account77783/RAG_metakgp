"""Stateless Keycloak/OIDC integration shared by browser and MCP readers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from .config import Settings
from .locking import exclusive_file_lock
from .errors import (
    AccountNotFoundError,
    AccountStateError,
    IdentityResponseError,
    IdentityUnavailableError,
    IdentityGrantRejectedError,
)


@dataclass(frozen=True, slots=True)
class IdentityPrincipal:
    subject: str
    email: str
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int | None
    issuer: str
    audience: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int


class IdentityAuthority(Protocol):
    issuer_url: str

    async def verify_token(self, token: str) -> IdentityPrincipal | None: ...

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        signup: bool,
    ) -> str: ...

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce: str,
    ) -> TokenSet: ...

    async def refresh(self, refresh_token: str) -> TokenSet: ...

    async def revoke(self, token: str) -> None: ...

    async def readiness(self) -> bool: ...


class IdentityHttpTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float,
        allow_empty: bool = False,
    ) -> Any: ...


class UrllibIdentityTransport:
    """Small transport which never includes credentials or response bodies in errors."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float,
        allow_empty: bool = False,
    ) -> Any:
        if form is not None and payload is not None:
            raise ValueError("Only one HTTP request body may be supplied")
        request_headers = dict(headers or {})
        body: bytes | None = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise IdentityResponseError("Identity response exceeds the allowed size")
        except urllib.error.HTTPError as exc:
            # Distinguish revoked/expired grants from upstream/configuration failures.
            # Inspect only a bounded error code; never log the response body.
            if method == "POST" and form and form.get("grant_type") in {"refresh_token", "authorization_code"}:
                try:
                    error = json.loads(exc.read(4096)).get("error")
                except (ValueError, AttributeError, OSError):
                    error = None
                finally:
                    exc.close()
                if exc.code == 400 and error == "invalid_grant":
                    raise IdentityGrantRejectedError("Identity grant was rejected") from exc
            raise IdentityUnavailableError(
                f"Identity service request failed with HTTP {int(exc.code)}"
            ) from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            raise IdentityUnavailableError("Identity service is unavailable") from exc
        if not raw and allow_empty:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityResponseError("Identity service returned invalid JSON") from exc


class IdTokenValidator(Protocol):
    async def validate(self, token: str, *, nonce: str) -> Mapping[str, Any]: ...


class KeycloakIdTokenValidator:
    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        metadata_loader,
        timeout: float = 5.0,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self._metadata_loader = metadata_loader
        self.timeout = timeout

    async def validate(self, token: str, *, nonce: str) -> Mapping[str, Any]:
        metadata = await self._metadata_loader()
        jwks_uri = str(metadata.get("jwks_uri", ""))
        if not jwks_uri:
            raise IdentityResponseError("Identity metadata has no JWKS endpoint")

        def decode() -> Mapping[str, Any]:
            import jwt
            try:
                key = jwt.PyJWKClient(jwks_uri, timeout=self.timeout).get_signing_key_from_jwt(token)
                claims = jwt.decode(
                    token,
                    key.key,
                    algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                    audience=self.client_id,
                    issuer=self.issuer_url,
                    options={"require": ["exp", "iss", "aud", "sub", "nonce"]},
                )
            except jwt.PyJWKClientConnectionError as exc:
                raise IdentityUnavailableError("Identity signing keys are unavailable") from exc
            except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
                raise IdentityGrantRejectedError("Identity token validation failed") from exc
            except Exception as exc:
                raise IdentityResponseError("Identity token validation is unavailable") from exc
            if not hmac.compare_digest(str(claims.get("nonce", "")).encode("utf-8"), nonce.encode("utf-8")):
                raise IdentityGrantRejectedError("Identity token nonce is invalid")
            return claims

        return await asyncio.to_thread(decode)


class KeycloakAuthority:
    """Keycloak client using introspection so account changes revoke access immediately."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: IdentityHttpTransport | None = None,
        id_token_validator: IdTokenValidator | None = None,
    ) -> None:
        settings.validate(require_identity_secrets=True)
        self.settings = settings
        self.issuer_url = settings.identity_issuer_url.rstrip("/")
        self.transport = transport or UrllibIdentityTransport()
        self.timeout = float(settings.identity_http_timeout_seconds)
        self._metadata_cache: dict[str, Any] | None = None
        self._metadata_lock = asyncio.Lock()
        self.id_token_validator = id_token_validator or KeycloakIdTokenValidator(
            self.issuer_url,
            settings.identity_browser_client_id,
            self.metadata,
            timeout=self.timeout,
        )

    @staticmethod
    def _validate_endpoint(url: str, label: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise IdentityResponseError(f"Identity metadata has an invalid {label}")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise IdentityResponseError(f"Identity metadata {label} must use HTTPS")
        return url

    async def metadata(self, *, fresh: bool = False) -> Mapping[str, Any]:
        if not fresh and self._metadata_cache is not None:
            return self._metadata_cache
        async with self._metadata_lock:
            if not fresh and self._metadata_cache is not None:
                return self._metadata_cache
            url = f"{self.issuer_url}/.well-known/openid-configuration"
            payload = await asyncio.to_thread(
                self.transport.request_json,
                "GET",
                url,
                timeout=self.timeout,
            )
            if not isinstance(payload, dict) or str(payload.get("issuer", "")).rstrip("/") != self.issuer_url:
                raise IdentityResponseError("Identity metadata issuer does not match configuration")
            for name in (
                "authorization_endpoint",
                "token_endpoint",
                "introspection_endpoint",
                "revocation_endpoint",
                "jwks_uri",
            ):
                payload[name] = self._validate_endpoint(str(payload.get(name, "")), name)
            self._metadata_cache = payload
            return payload

    async def readiness(self) -> bool:
        # A cached discovery document proves configuration, not current availability.
        await self.metadata(fresh=True)
        return True

    async def _form_request(
        self,
        endpoint_name: str,
        form: Mapping[str, str],
        *,
        allow_empty: bool = False,
    ) -> Any:
        metadata = await self.metadata()
        return await asyncio.to_thread(
            self.transport.request_json,
            "POST",
            str(metadata[endpoint_name]),
            form=form,
            timeout=self.timeout,
            allow_empty=allow_empty,
        )

    @staticmethod
    def _audiences(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        return ()

    async def verify_token(self, token: str) -> IdentityPrincipal | None:
        if not token:
            return None
        payload = await self._form_request(
            "introspection_endpoint",
            {
                "token": token,
                "client_id": self.settings.identity_resource_client_id,
                "client_secret": self.settings.identity_resource_client_secret,
            },
        )
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return None
        issuer = str(payload.get("iss", "")).rstrip("/")
        audience = self._audiences(payload.get("aud"))
        scopes = tuple(str(payload.get("scope", "")).split())
        expires_at = payload.get("exp")
        status = str(payload.get("graphmind_status", "active")).casefold()
        if issuer != self.issuer_url:
            return None
        if self.settings.identity_mcp_audience not in audience:
            return None
        if self.settings.identity_required_scope not in scopes:
            return None
        if status != "active":
            return None
        if payload.get("email_verified") is not True:
            return None
        if not isinstance(expires_at, int) or expires_at <= int(time.time()):
            return None
        subject = str(payload.get("sub", ""))
        email = str(payload.get("email", "")).strip().casefold()
        client_id = str(payload.get("client_id") or payload.get("azp") or "")
        if not subject or not email or "@" not in email or not client_id:
            return None
        return IdentityPrincipal(
            subject=subject,
            email=email,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            issuer=issuer,
            audience=audience,
        )

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        signup: bool,
    ) -> str:
        metadata = await self.metadata()
        query = {
            "client_id": self.settings.identity_browser_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": f"openid email {self.settings.identity_required_scope}",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if signup:
            query["prompt"] = "create"
        return f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(query)}"

    @staticmethod
    def _token_set(payload: Any) -> TokenSet:
        if not isinstance(payload, dict):
            raise IdentityResponseError("Identity token endpoint returned a non-object")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise IdentityResponseError("Identity token endpoint omitted the access token")
        if not isinstance(expires_in, int) or expires_in < 1:
            raise IdentityResponseError("Identity token endpoint returned an invalid expiry")
        refresh = payload.get("refresh_token")
        id_token = payload.get("id_token")
        return TokenSet(
            access_token=access_token,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            id_token=id_token if isinstance(id_token, str) and id_token else None,
            expires_in=expires_in,
        )

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        nonce: str,
    ) -> TokenSet:
        payload = await self._form_request(
            "token_endpoint",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "client_id": self.settings.identity_browser_client_id,
                "client_secret": self.settings.identity_browser_client_secret,
            },
        )
        tokens = self._token_set(payload)
        if tokens.id_token is None:
            raise IdentityResponseError("Identity token endpoint omitted the ID token")
        await self.id_token_validator.validate(tokens.id_token, nonce=nonce)
        if await self.verify_token(tokens.access_token) is None:
            raise IdentityGrantRejectedError("Identity service issued an ineligible access token")
        return tokens

    async def refresh(self, refresh_token: str) -> TokenSet:
        payload = await self._form_request(
            "token_endpoint",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.settings.identity_browser_client_id,
                "client_secret": self.settings.identity_browser_client_secret,
            },
        )
        tokens = self._token_set(payload)
        if await self.verify_token(tokens.access_token) is None:
            raise IdentityGrantRejectedError("Identity service refreshed an ineligible access token")
        return tokens

    async def revoke(self, token: str) -> None:
        await self._form_request(
            "revocation_endpoint",
            {
                "token": token,
                "client_id": self.settings.identity_browser_client_id,
                "client_secret": self.settings.identity_browser_client_secret,
            },
            allow_empty=True,
        )


class FlowCookieSigner:
    """Short-lived signed browser state for OAuth state, nonce, and PKCE verifier."""

    def __init__(self, secret: str, *, lifetime_seconds: int = 600) -> None:
        key = secret.encode("utf-8")
        if len(key) < 32:
            raise ValueError("Flow signing secret must be at least 32 bytes")
        self.key = key
        self.lifetime_seconds = lifetime_seconds

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def seal(self, payload: Mapping[str, Any], *, now: int | None = None) -> str:
        body = dict(payload)
        body["iat"] = int(time.time() if now is None else now)
        encoded = self._encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        signature = self._encode(hmac.new(self.key, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def open(self, value: str, *, now: int | None = None) -> dict[str, Any]:
        try:
            encoded, supplied = value.split(".", 1)
            expected = self._encode(hmac.new(self.key, encoded.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError
            payload = json.loads(self._decode(encoded).decode("utf-8"))
            issued_at = int(payload["iat"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IdentityResponseError("Browser authorization state is invalid") from exc
        current = int(time.time() if now is None else now)
        if issued_at > current + 30 or current - issued_at > self.lifetime_seconds:
            raise IdentityResponseError("Browser authorization state has expired")
        return payload


def new_browser_flow(return_to: str = "/") -> dict[str, str]:
    if (not return_to.startswith("/") or return_to.startswith("//")
            or "\\" in return_to or any(ord(char) < 32 for char in return_to)):
        return_to = "/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": verifier,
        "challenge": challenge.decode("ascii"),
        "return_to": return_to,
    }


class KeycloakAdminClient:
    """Host-only account controls. Delete is a retained, disabled account state."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: IdentityHttpTransport | None = None,
    ) -> None:
        settings.validate(require_identity_admin_secret=True)
        self.settings = settings
        self.transport = transport or UrllibIdentityTransport()
        self.timeout = float(settings.identity_http_timeout_seconds)
        self.issuer = settings.identity_issuer_url.rstrip("/")
        marker = "/realms/"
        if marker not in self.issuer:
            raise IdentityResponseError("Keycloak issuer URL must contain /realms/<realm>")
        base, realm = self.issuer.split(marker, 1)
        if not realm or "/" in realm:
            raise IdentityResponseError("Keycloak issuer URL must identify one realm")
        self.realm = realm
        self.token_url = f"{self.issuer}/protocol/openid-connect/token"
        self.admin_url = f"{base}/admin/realms/{urllib.parse.quote(realm, safe='')}"

    async def _access_token(self) -> str:
        payload = await asyncio.to_thread(
            self.transport.request_json,
            "POST",
            self.token_url,
            form={
                "grant_type": "client_credentials",
                "client_id": self.settings.identity_admin_client_id,
                "client_secret": self.settings.identity_admin_client_secret,
            },
            timeout=self.timeout,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise IdentityResponseError("Keycloak admin token response is invalid")
        return payload["access_token"]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        token = await self._access_token()
        return await asyncio.to_thread(
            self.transport.request_json,
            method,
            f"{self.admin_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            payload=payload,
            timeout=self.timeout,
            allow_empty=allow_empty,
        )

    async def list_accounts(self, *, page_size: int = 100, max_accounts: int = 10_000) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 1000 or max_accounts < 1:
            raise ValueError("Invalid account-list bounds")
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            users = await self._request("GET", f"/users?first={len(accounts)}&max={page_size}")
            if not isinstance(users, list) or len(users) > page_size:
                raise IdentityResponseError("Keycloak users response is invalid")
            for user in users:
                if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
                    raise IdentityResponseError("Keycloak users response contains an invalid account")
                if user["id"] in seen:
                    raise IdentityResponseError("Accounts changed during pagination; retry the listing")
                if len(accounts) >= max_accounts:
                    raise IdentityResponseError(f"Account listing exceeds the explicit {max_accounts}-account limit")
                seen.add(user["id"])
                accounts.append(self._safe_account(user))
            if len(users) < page_size:
                return accounts

    async def _resolve(self, reference: str) -> dict[str, Any]:
        if "@" in reference:
            email = reference.strip().casefold()
            query = urllib.parse.urlencode({"email": email, "exact": "true"})
            users = await self._request("GET", f"/users?{query}")
            if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
                raise AccountNotFoundError("Exactly one account was not found for that email")
            return users[0]
        user = await self._request("GET", f"/users/{urllib.parse.quote(reference, safe='')}")
        if not isinstance(user, dict) or not user.get("id"):
            raise AccountNotFoundError("Account was not found")
        return user

    @staticmethod
    def _attributes(user: Mapping[str, Any]) -> dict[str, list[str]]:
        source = user.get("attributes")
        attributes: dict[str, list[str]] = {}
        if isinstance(source, dict):
            for key, value in source.items():
                if isinstance(key, str) and isinstance(value, list):
                    attributes[key] = [str(item) for item in value]
        return attributes

    @staticmethod
    def _safe_account(user: Mapping[str, Any]) -> dict[str, Any]:
        attributes = KeycloakAdminClient._attributes(user)
        status = (attributes.get("graphmind_status") or ["active"])[0]
        return {
            "id": str(user.get("id", "")),
            "email": str(user.get("email", "")).casefold(),
            "email_verified": user.get("emailVerified") is True,
            "enabled": user.get("enabled") is True,
            "status": status,
            "created_at_ms": user.get("createdTimestamp"),
        }

    async def set_state(self, reference: str, action: str) -> dict[str, Any]:
        # All supported account mutations are host-local. Serialize CLI processes
        # as well as coroutines so stale reads cannot undo a concurrent deletion.
        with exclusive_file_lock(self.settings.data_dir / "account-admin.lock") as acquired:
            if not acquired:
                raise AccountStateError("Another host account operation is running; retry")
            return await self._set_state(reference, action)

    async def _set_state(self, reference: str, action: str) -> dict[str, Any]:
        if action not in {"disable", "enable", "delete", "restore"}:
            raise ValueError("Unsupported account action")
        user = await self._resolve(reference)
        account_id = str(user.get("id", ""))
        if not account_id:
            raise AccountNotFoundError("Account was not found")
        attributes = self._attributes(user)
        current = (attributes.get("graphmind_status") or ["active"])[0].casefold()
        deleted = current == "deleted" or bool(attributes.get("graphmind_deleted_at"))
        if action == "enable" and deleted:
            raise AccountStateError("Deleted accounts require an explicit host restore")
        next_status = {
            "disable": "disabled",
            "enable": "active",
            "delete": "deleted",
            "restore": "active",
        }[action]
        if deleted and action == "disable":
            next_status = "deleted"
        try:
            epoch = int((attributes.get("graphmind_credential_epoch") or ["0"])[0]) + 1
        except ValueError:
            epoch = 1
        attributes["graphmind_status"] = [next_status]
        attributes["graphmind_credential_epoch"] = [str(epoch)]
        if action == "delete":
            attributes.setdefault("graphmind_deleted_at", [datetime.now(UTC).isoformat()])
        elif action == "restore":
            attributes.pop("graphmind_deleted_at", None)
        update = {
            "id": account_id,
            "username": user.get("username"),
            "email": user.get("email"),
            "emailVerified": user.get("emailVerified") is True,
            "enabled": action in {"enable", "restore"},
            "attributes": attributes,
        }
        quoted_id = urllib.parse.quote(account_id, safe="")
        await self._request("PUT", f"/users/{quoted_id}", payload=update, allow_empty=True)
        await self._request("POST", f"/users/{quoted_id}/logout", allow_empty=True)
        stored = await self._resolve(account_id)
        stored_attributes = self._attributes(stored)
        if stored.get("enabled") is not update["enabled"] or any(
            stored_attributes.get(name) != attributes.get(name)
            for name in ("graphmind_status", "graphmind_deleted_at", "graphmind_credential_epoch")
        ):
            raise AccountStateError("Identity service did not persist the requested account state")
        return {
            "id": account_id,
            "email": str(user.get("email", "")).casefold(),
            "status": next_status,
            "enabled": update["enabled"],
        }
