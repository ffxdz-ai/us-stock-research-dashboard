import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  allowedRequestOrigin, archiveDeepseekState, beijingDayKey, sanitizeFutuSnapshot,
} from "../src/index.js";

test("beijingDayKey uses the Asia/Shanghai date boundary", () => {
  assert.equal(beijingDayKey("2026-08-22T16:01:00Z"), "2026-08-23");
  assert.equal(beijingDayKey("2026-08-22T15:59:00Z"), "2026-08-22");
});

test("allowedRequestOrigin accepts only the configured public site", () => {
  const env = { ALLOWED_ORIGIN: "https://ffxdz-ai.github.io" };
  assert.equal(
    allowedRequestOrigin(new Request("https://worker.example/status", { headers: { Origin: "https://ffxdz-ai.github.io" } }), env),
    "https://ffxdz-ai.github.io",
  );
  assert.equal(
    allowedRequestOrigin(new Request("https://worker.example/status", { headers: { Origin: "https://example.com" } }), env),
    "",
  );
});

test("archive freshness follows the latest DeepSeek report, not an unrelated archive export", () => {
  const archive = {
    generated_at: "2026-08-23T10:00:00+08:00",
    reports: [
      { kind: "market-brief", published_at: "2026-08-23T10:00:00+08:00" },
      { kind: "deepseek-cloud", published_at: "2026-08-22T09:00:00+08:00" },
    ],
  };
  assert.equal(archiveDeepseekState(archive, "2026-08-23T12:00:00+08:00").updated_today, false);
  archive.reports[1].published_at = "2026-08-23T09:00:00+08:00";
  assert.equal(archiveDeepseekState(archive, "2026-08-23T12:00:00+08:00").updated_today, true);
});

test("Futu snapshot sanitizer keeps only public market quote fields", () => {
  const clean = sanitizeFutuSnapshot({
    generated_at: "2026-08-25T09:20:00+08:00",
    opend: { connected: true, host: "127.0.0.1", port: 11111 },
    account: "must-never-leak",
    quotes: {
      "US.MU": {
        code: "US.MU",
        last_price: 910.43,
        quote_time: "2026-08-25T09:19:00+08:00",
        shares: 100,
        cost_basis: 800,
      },
      "INVALID!": { quote_time: "2026-08-25T09:19:00+08:00" },
    },
  });
  assert.deepEqual(Object.keys(clean.quotes), ["US.MU"]);
  assert.equal(clean.quotes["US.MU"].last_price, 910.43);
  assert.equal(clean.quotes["US.MU"].shares, undefined);
  assert.equal(clean.quotes["US.MU"].cost_basis, undefined);
  assert.equal(clean.opend.host, undefined);
  assert.equal(clean.account, undefined);
  assert.equal(clean.bridge.authenticated, true);
});

test("Futu snapshot bridge rejects missing credentials", async () => {
  const response = await worker.fetch(new Request("https://worker.example/futu/snapshot"), {
    FUTU_BRIDGE_TOKEN: "test-only-token",
    FUTU_SNAPSHOT_KV: {},
  });
  assert.equal(response.status, 401);
});

test("authenticated Futu snapshot can round-trip through KV", async () => {
  const saved = new Map();
  const env = {
    FUTU_BRIDGE_TOKEN: "test-only-token",
    FUTU_SNAPSHOT_KV: {
      async get(key) { return saved.get(key) || null; },
      async put(key, value) { saved.set(key, value); },
    },
  };
  const headers = { Authorization: "Bearer test-only-token", "Content-Type": "application/json" };
  const put = await worker.fetch(new Request("https://worker.example/futu/snapshot", {
    method: "PUT",
    headers,
    body: JSON.stringify({
      generated_at: "2026-08-25T09:20:00+08:00",
      opend: { connected: true },
      quotes: { "HK.00981": { code: "HK.00981", last_price: 66.75, quote_time: "2026-08-25T09:19:00+08:00" } },
    }),
  }), env);
  assert.equal(put.status, 200);
  assert.equal((await put.json()).quotes_returned, 1);

  const get = await worker.fetch(new Request("https://worker.example/futu/snapshot", { headers }), env);
  assert.equal(get.status, 200);
  const payload = await get.json();
  assert.equal(payload.snapshot.quotes["HK.00981"].last_price, 66.75);
  assert.equal(payload.snapshot.bridge.authenticated, true);
});
