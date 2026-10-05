from __future__ import annotations

import asyncio
import io
import importlib.util
import json
import logging
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.testclient import TestClient

from graphmind.config import Settings
from graphmind.domain import AdapterStatus, Outcome, QueryResult
from graphmind.errors import (
    ConfigurationError, IdentityGrantRejectedError, IdentityResponseError,
    IdentityUnavailableError, ReaderQueryError,
)
from graphmind.http_client import HttpUnavailableError
from graphmind.identity import KeycloakAdminClient, KeycloakAuthority, KeycloakIdTokenValidator, UrllibIdentityTransport, new_browser_flow
from graphmind.product_server import BoundedReadiness, JAVASCRIPT, create_product_server
from graphmind.providers import OllamaProvider
from graphmind.reader import ReaderService
from test_graphmind_p5_p6 import FakeApplication, FakeAuthority, FakeTransport, identity_settings


class HardeningConfigurationTests(unittest.TestCase):
    def test_secure_cookie_exception_is_only_loopback_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            for base in ("https://rag.example.test", "https://localhost"):
                with self.subTest(base=base), self.assertRaises(ConfigurationError):
                    replace(settings, public_base_url=base, identity_mcp_audience=base + "/mcp",
                            browser_cookie_secure=False).validate()
            base = "http://127.0.0.1:8000"
            replace(settings, public_base_url=base, identity_mcp_audience=base + "/mcp",
                    browser_cookie_secure=False).validate()

    def test_reader_and_admin_require_separate_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            reader = replace(settings, identity_admin_client_secret="")
            reader.validate(require_identity_secrets=True)
            KeycloakAuthority(reader, transport=FakeTransport())
            create_product_server(reader, FakeApplication(), authority=FakeAuthority())
            with self.assertRaises(ConfigurationError):
                KeycloakAdminClient(reader)
            admin = replace(settings, identity_browser_client_secret="", identity_resource_client_secret="",
                            identity_flow_signing_key="")
            KeycloakAdminClient(admin)
            with self.assertRaises(ConfigurationError):
                admin.validate(require_identity_secrets=True)

    def test_safe_return_paths_reject_browser_backslash_redirects(self):
        for target in ("//foreign.test", "/\\foreign.test", "/\nforeign.test"):
            self.assertEqual(new_browser_flow(target)["return_to"], "/")


class IdentityHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_sdk_client_exercises_acceptance_tools_over_asgi(self):
        # Execute the shipped client against the actual SDK wire protocol, but
        # synthetic identity/storage and in-process ASGI: not real OAuth/network acceptance.
        import httpx2
        path = Path(__file__).resolve().parents[1] / "tools" / "mcp_acceptance.py"
        spec = importlib.util.spec_from_file_location("graphmind_acceptance_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            _, app = create_product_server(settings, FakeApplication(), authority=FakeAuthority())
            async with app.app.router.lifespan_context(app.app):
                def client(**kwargs):
                    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), **kwargs)
                with patch.object(module, "AsyncClient", side_effect=client), \
                        patch.dict("os.environ", {"GRAPHMIND_TEST_ACCESS_TOKEN": "access"}):
                    await module.run(settings.identity_mcp_audience, "question")

    async def test_jwks_outage_is_distinct_from_invalid_signature(self):
        import jwt
        async def metadata():
            return {"jwks_uri": "https://auth.example.test/certs"}
        validator = KeycloakIdTokenValidator("https://auth.example.test", "client", metadata)
        for error, expected in ((jwt.PyJWKClientConnectionError("offline"), IdentityUnavailableError),
                                (jwt.InvalidTokenError("invalid"), IdentityGrantRejectedError)):
            with patch("jwt.PyJWKClient") as client:
                client.return_value.get_signing_key_from_jwt.side_effect = error
                with self.assertRaises(expected):
                    await validator.validate("synthetic", nonce="nonce")

    async def test_readiness_does_not_trust_cached_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeTransport()
            authority = KeycloakAuthority(identity_settings(Path(temporary)), transport=transport)
            await authority.metadata()
            with patch.object(transport, "request_json", side_effect=IdentityUnavailableError("offline")):
                with self.assertRaises(IdentityUnavailableError):
                    await authority.readiness()

    async def test_account_pagination_includes_more_than_one_hundred(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = KeycloakAdminClient(identity_settings(Path(temporary)))
            users = [{"id": f"user-{n}", "email": f"reader{n}@example.test"} for n in range(205)]
            pages = []

            async def request(method, path, **kwargs):
                from urllib.parse import parse_qs, urlparse
                query = parse_qs(urlparse(path).query)
                first, count = int(query["first"][0]), int(query["max"][0])
                pages.append(first)
                return users[first:first + count]

            client._request = request
            accounts = await client.list_accounts()
            self.assertEqual(len(accounts), 205)
            self.assertEqual(pages, [0, 100, 200])
            self.assertEqual(accounts[-1]["id"], "user-204")

    async def test_listing_over_limit_or_repeated_page_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = KeycloakAdminClient(identity_settings(Path(temporary)))

            async def too_many(method, path):
                offset = int(path.split("first=")[1].split("&")[0])
                return [{"id": str(offset)}, {"id": str(offset + 1)}]

            client._request = too_many
            with self.assertRaisesRegex(IdentityResponseError, "3-account limit"):
                await client.list_accounts(page_size=2, max_accounts=3)
            async def repeated(method, path):
                return [{"id": "same"}]
            client._request = repeated
            with self.assertRaisesRegex(IdentityResponseError, "pagination"):
                await client.list_accounts(page_size=1)

    async def test_bounded_readiness_does_not_spawn_more_work_after_timeout(self):
        release = asyncio.Event()
        calls = 0
        async def probe():
            nonlocal calls
            calls += 1
            await release.wait()
            return True
        ready = BoundedReadiness(probe, timeout=0.01)
        self.assertFalse(await ready.available())
        self.assertFalse(await ready.available())
        self.assertEqual(calls, 1)
        release.set()
        await ready.task
        self.assertTrue(await ready.available())
        self.assertEqual(calls, 2)

    async def test_grant_rejection_is_not_an_outage(self):
        for error, expected in (("invalid_grant", IdentityGrantRejectedError),
                                ("invalid_client", IdentityUnavailableError)):
            response = urllib.error.HTTPError("https://auth.example.test/token", 400, "bad", {},
                                              io.BytesIO(json.dumps({"error": error}).encode()))
            with patch("urllib.request.urlopen", side_effect=response):
                with self.assertRaises(expected):
                    UrllibIdentityTransport().request_json("POST", "https://auth.example.test/token",
                                                          form={"grant_type": "refresh_token"}, timeout=1)


class BrowserHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = identity_settings(Path(self.temporary.name))
        self.authority = FakeAuthority()
        self.application = FakeApplication()

    def tearDown(self):
        self.temporary.cleanup()

    def client(self):
        _, app = create_product_server(self.settings, self.application, authority=self.authority)
        return TestClient(app, base_url=self.settings.public_base_url)

    def credentials(self, client):
        client.cookies.set("graphmind_access", "access")
        client.cookies.set("graphmind_csrf", "test-csrf")
        return {"Origin": self.settings.public_base_url, "X-GraphMind-CSRF": "test-csrf"}

    def test_reload_and_source_navigation_resume_without_refresh_on_get(self):
        calls = []
        original = self.authority.refresh
        async def refresh(token):
            calls.append(token)
            return await original(token)
        self.authority.refresh = refresh
        for target in ("/", "/documents/doc-opaque?version_id=version-active"):
            with self.subTest(target=target), self.client() as client:
                client.cookies.set("graphmind_refresh", "refresh")
                response = client.get(target)
                self.assertEqual(response.status_code, 200)
                self.assertIn('id="resume"', response.text)
                self.assertNotIn("Safe source text", response.text)
                self.assertEqual(calls, [])
                csrf = client.cookies.get("graphmind_csrf")
                denied = client.post("/auth/refresh", headers={"X-GraphMind-CSRF": csrf})
                self.assertEqual(denied.status_code, 403)
                renewed = client.post("/auth/refresh", headers={"Origin": self.settings.public_base_url,
                                                              "X-GraphMind-CSRF": csrf})
                self.assertEqual(renewed.status_code, 200)
                final = client.get(target)
                self.assertEqual(final.status_code, 200)
                self.assertNotIn('id="resume"', final.text)
                calls.clear()

    def test_refresh_outage_preserves_credentials_but_invalid_grant_clears(self):
        for error, expected in ((IdentityUnavailableError("offline"), 503),
                                (IdentityGrantRejectedError("revoked"), 401)):
            async def refresh(token):
                raise error
            self.authority.refresh = refresh
            with self.subTest(expected=expected), self.client() as client:
                headers = self.credentials(client)
                client.cookies.set("graphmind_refresh", "refresh")
                response = client.post("/auth/refresh", headers=headers)
                self.assertEqual(response.status_code, expected)
                self.assertEqual("Max-Age=0" in response.headers.get("set-cookie", ""), expected == 401)

    def test_provider_errors_are_not_successful_abstention_in_browser_or_mcp(self):
        self.application.answers.query = lambda question, **kwargs: QueryResult(
            Outcome.ERROR, "A required service is unavailable.", (), "request",
            {"error_code": "provider_timeout"})
        with self.client() as client:
            response = client.post("/api/ask", json={"question": "question"}, headers=self.credentials(client))
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json()["error"], "provider_timeout")
            mcp = client.post("/mcp", headers={"Authorization": "Bearer access",
                               "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": "2025-11-25"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "ask", "arguments": {"question": "question"}}})
            self.assertTrue(mcp.json()["result"]["isError"])
            self.assertIn("provider_timeout", str(mcp.json()["result"]["content"]))

    def test_legitimate_abstention_still_succeeds(self):
        self.application.answers.query = lambda question, **kwargs: QueryResult(
            Outcome.INSUFFICIENT_EVIDENCE, "Not supported by these documents.", (), "request", {})
        with self.client() as client:
            response = client.post("/api/ask", json={"question": "question"}, headers=self.credentials(client))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outcome"], "insufficient_evidence")

    def test_mcp_identity_outage_is_503_not_invalid_token(self):
        async def unavailable(token):
            raise IdentityUnavailableError("offline")
        self.authority.verify_token = unavailable
        with self.client() as client:
            response = client.post("/mcp", headers={"Authorization": "Bearer access"}, json={})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"], "identity_unavailable")
            self.assertNotIn("www-authenticate", response.headers)

    def test_live_readiness_detects_outage_without_manifest_audit(self):
        ready = True
        async def identity_ready():
            return ready
        self.authority.readiness = identity_ready
        self.application.readiness = lambda: self.fail("Public readiness must not run a manifest audit")
        self.application.reader_readiness = lambda: (AdapterStatus("dependencies", True, "ready"),)
        with self.client() as client:
            self.assertEqual(client.get("/health/ready").status_code, 200)
            ready = False
            self.assertEqual(client.get("/health/ready").status_code, 503)
            self.assertEqual(client.get("/health/live").status_code, 200)

    def test_answer_provider_outage_makes_readiness_unavailable(self):
        self.application.reader_readiness = lambda: (AdapterStatus("answer-provider", False, "private detail"),)
        with self.client() as client:
            response = client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private detail", response.text)

    def test_callback_and_unknown_paths_log_no_query_or_identifier(self):
        with self.client() as client, self.assertLogs("graphmind.access", level=logging.INFO) as logs:
            client.get("/auth/callback?code=do-not-log-code&state=do-not-log-state")
            client.get("/unknown-do-not-log-path?token=do-not-log-token")
            client.get("/documents/private-document-id?version_id=private-version-id")
        output = "\n".join(logs.output)
        self.assertIn("/auth/callback", output)
        for forbidden in ("do-not-log", "private-document-id", "private-version-id"):
            self.assertNotIn(forbidden, output)

    def test_cli_disables_protocol_query_logging(self):
        from graphmind.cli import main
        with patch("graphmind.cli._settings", return_value=self.settings), \
                patch("graphmind.cli.GraphMindApplication") as application, \
                patch("graphmind.product_server.create_product_server", return_value=(None, object())), \
                patch("uvicorn.run") as run:
            main(["serve"])
        self.assertIs(run.call_args.kwargs["access_log"], False)


class ProviderReadinessTests(unittest.TestCase):
    def test_host_store_doctor_does_not_require_answer_generation_service(self):
        from graphmind.service import GraphMindApplication
        from test_graphmind_p2 import make_app
        with tempfile.TemporaryDirectory() as temporary, make_app(Path(temporary)) as application:
            with patch.object(application.answers.provider, "readiness", side_effect=AssertionError("not a storage check")):
                self.assertTrue(all(status.available for status in application.readiness()))
            with patch.object(application.answers.provider, "readiness", return_value=AdapterStatus("answer-provider", False, "offline")):
                self.assertFalse(all(status.available for status in application.reader_readiness()))

    def test_probe_checks_configured_model_with_short_metadata_request(self):
        calls = []
        class Transport:
            def request_json(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return {"models": [{"name": "test-model", "digest": "known"}]}
        provider = OllamaProvider("http://localhost:11434", "", "test-model", mode="local",
                                  timeout=180, transport=Transport())
        self.assertTrue(provider.readiness().available)
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].endswith("/api/tags"))
        self.assertLessEqual(calls[0][2]["timeout"], 3)
        self.assertEqual(provider.call_count, 0)

    def test_missing_model_credentials_and_outage_are_not_ready(self):
        class Transport:
            def request_json(self, *args, **kwargs):
                raise HttpUnavailableError("offline")
        provider = OllamaProvider("http://localhost:11434", "", "test-model", mode="local", transport=Transport())
        self.assertFalse(provider.readiness().available)
        provider.mode = "hosted"
        self.assertFalse(provider.readiness().available)


class BrowserScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for executable browser-script fault regression")
    def test_script_network_json_failures_and_resume_exit_waiting_state(self):
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
async function scenario(kind){
  let handler,renewed=false;const status={textContent:''},button={disabled:false};
  const elements={'status':status,'answer':{textContent:''},'citations':{replaceChildren(){},append(){}},
    'question':{value:'question'},'ask-form':{addEventListener(_,h){handler=h}}};
  if(kind==='resume')elements.resume={dataset:{returnTo:'/documents/opaque?version_id=active'}};
  const document={cookie:'graphmind_csrf=test',getElementById:id=>elements[id],createElement:()=>({append(){}})};
  const location={replace(target){assert.equal(target,'/documents/opaque?version_id=active');renewed=true}};
  const fetch=async()=>{if(kind==='network')throw Error('offline');
    if(kind==='json')return {status:200,ok:true,json:async()=>{throw Error('bad JSON')}};
    if(kind==='error')return {status:503,ok:false,json:async()=>({message:'Service unavailable'})};
    if(kind==='resume')return {status:200,ok:true};
    return {status:200,ok:true,json:async()=>({outcome:'error',answer:'not an abstention'})};};
  vm.runInNewContext(process.argv[1],{document,location,fetch,decodeURIComponent,encodeURIComponent,Error});
  if(kind==='resume'){await new Promise(r=>setImmediate(r));assert.equal(renewed,true);return;}
  await handler({preventDefault(){},currentTarget:{querySelector(){return button}}});
  assert.equal(button.disabled,false);assert.notEqual(status.textContent,'Working…');
  assert.notEqual(status.textContent,'No supported answer');
}
(async()=>{for(const kind of ['network','json','error','invalid','resume'])await scenario(kind)})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run([shutil.which("node"), "-e", harness, JAVASCRIPT], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
