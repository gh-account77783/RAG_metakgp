import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiClient, ApiError } from "../src/api.ts";

const response = (status, payload) => new Response(JSON.stringify(payload), {status, headers: {"Content-Type": "application/json"}});

test("default browser transport retains the global fetch receiver", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = function () {
      assert.equal(this, globalThis);
      return Promise.resolve(response(200, {documents: []}));
    };
    assert.deepEqual(await new ApiClient(undefined, () => "csrf").post("/api/catalog", {}), {documents: []});
  } finally { globalThis.fetch = original; }
});

test("only pre-execution authentication failure can refresh and replay a request", async () => {
  const calls = [];
  let asked = 0;
  const client = new ApiClient(async (url, options) => {
    calls.push([url, options]);
    if (url === "/auth/refresh") return response(200, {status: "renewed"});
    return ++asked === 1 ? response(401, {error: "authentication_required"}) : response(200, {answer: "supported"});
  }, () => "csrf");
  assert.equal((await client.post("/api/ask", {question: "fixture"})).answer, "supported");
  assert.deepEqual(calls.map(call => call[0]), ["/api/ask", "/auth/refresh", "/api/ask"]);
  assert.ok(calls.every(call => call[1].credentials === "same-origin"));
  assert.equal(calls[0][1].headers["X-GraphMind-CSRF"], "csrf");
});

test("revocation and service failures never repeat model execution", async () => {
  for (const [status, error] of [[401, "authorization_revoked"], [503, "provider_timeout"]]) {
    let calls = 0;
    const client = new ApiClient(async () => { calls++; return response(status, {error}); }, () => "csrf");
    await assert.rejects(client.post("/api/ask", {}), problem => problem instanceof ApiError && problem.code === error);
    assert.equal(calls, 1);
  }
});

test("concurrent expired requests share renewal; cancellation prevents replay", async () => {
  let renewals = 0, sends = 0;
  const client = new ApiClient(async url => {
    if (url === "/auth/refresh") {
      renewals++; await new Promise(resolve => setImmediate(resolve)); return response(200, {});
    }
    return ++sends <= 2 ? response(401, {error: "authentication_required"}) : response(200, {});
  }, () => "csrf");
  await Promise.all([client.post("/api/catalog", {}), client.post("/api/catalog", {})]);
  assert.equal(renewals, 1);
  const controller = new AbortController();
  let calls = 0;
  const cancelled = new ApiClient(async url => {
    calls++;
    if (url === "/auth/refresh") { controller.abort(); return response(200, {}); }
    return response(401, {error: "authentication_required"});
  }, () => "csrf");
  await assert.rejects(cancelled.post("/api/ask", {}, controller.signal), {name: "AbortError"});
  assert.equal(calls, 2);
});
