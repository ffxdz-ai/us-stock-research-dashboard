import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  allowedRequestOrigin, archiveDeepseekState, beijingDayKey, encryptFeishuConfig,
  liveQuotePrice, qualifiesLiveSteadyBuyPlan, sanitizeFutuSnapshot,
} from "../src/index.js";

function liveCandidate(overrides = {}) {
  return {
    symbol: "US.MU",
    name: "Micron",
    currency: "USD",
    status: "waiting_entry",
    entry_tier: "formal",
    signal_type: "formal",
    formal_qualified: true,
    entry_execution_status: "wait_pullback",
    execution_allowed: true,
    technical_data_complete: true,
    future_function_audit: "PASS",
    price_freshness: "fresh",
    gate_failures: [],
    price: 110,
    entry_price: 100,
    safe_entry_zone_low: 97,
    safe_entry_zone_high: 100,
    safe_entry_max_price: 101.5,
    stop_loss: 90,
    target_price: 130,
    rr_ratio: 3,
    rr_required: 3,
    opportunity_score: 82,
    trend_score: 70,
    crowding_score: 45,
    risk_policy_version: "2.0.0",
    ...overrides,
  };
}

function memoryEnvironment() {
  const saved = new Map();
  return {
    saved,
    FUTU_BRIDGE_TOKEN: "test-only-token",
    FUTU_SNAPSHOT_KV: {
      async get(key) { return saved.get(key) || null; },
      async put(key, value) { saved.set(key, value); },
    },
  };
}

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

test("Feishu credentials remain authenticated and encrypted at rest", async () => {
  const env = memoryEnvironment();
  const webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-webhook";
  const secret = "test-only-signing-secret";

  const unauthenticated = await worker.fetch(new Request("https://worker.example/futu/feishu-config", {
    method: "PUT",
    body: JSON.stringify({ webhook, secret }),
  }), env);
  assert.equal(unauthenticated.status, 401);

  const configured = await worker.fetch(new Request("https://worker.example/futu/feishu-config", {
    method: "PUT",
    headers: { Authorization: "Bearer test-only-token", "Content-Type": "application/json" },
    body: JSON.stringify({ webhook, secret }),
  }), env);
  assert.equal(configured.status, 200);
  assert.deepEqual(await configured.json(), { ok: true, configured: true, encrypted: true });
  const stored = env.saved.get("encrypted_futu_feishu_config_v1");
  assert.ok(stored);
  assert.equal(stored.includes(webhook), false);
  assert.equal(stored.includes(secret), false);
  assert.equal(JSON.parse(stored).version, 1);
});

test("Feishu configuration refuses non-approved webhook destinations", async () => {
  const env = memoryEnvironment();
  const response = await worker.fetch(new Request("https://worker.example/futu/feishu-config", {
    method: "PUT",
    headers: { Authorization: "Bearer test-only-token", "Content-Type": "application/json" },
    body: JSON.stringify({ webhook: "https://attacker.example/open-apis/bot/v2/hook/nope", secret: "test" }),
  }), env);
  assert.equal(response.status, 422);
  assert.equal(env.saved.size, 0);
});

test("live formal entry requires fresh authenticated prices and every hard research gate", () => {
  const now = new Date("2026-08-25T14:00:00Z");
  const quote = { last_price: 100, quote_time: "2026-08-25T13:58:00Z" };
  const plan = liveCandidate();
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, quote, now), true);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, last_price: 102 }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, last_price: 96 }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, quote_time: "2026-08-25T13:40:00Z" }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, future_function_audit: "BLOCK" }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, technical_data_complete: false }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, gate_failures: ["data_gap"] }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, rr_ratio: 1.5 }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, price_freshness: "stale" }, quote, now), false);
});

test("US live quotes use the correct extended trading session", () => {
  const quote = { last_price: 100, pre_price: 101, after_price: 102, overnight_price: 103 };
  assert.equal(liveQuotePrice("US.MU", quote, new Date("2026-08-25T10:00:00Z")), 101);
  assert.equal(liveQuotePrice("US.MU", quote, new Date("2026-08-25T14:00:00Z")), 100);
  assert.equal(liveQuotePrice("US.MU", quote, new Date("2026-08-25T21:00:00Z")), 102);
  assert.equal(liveQuotePrice("US.MU", quote, new Date("2026-08-26T01:00:00Z")), 103);
  assert.equal(liveQuotePrice("HK.00981", quote, new Date("2026-08-25T10:00:00Z")), 100);
});

test("Futu entry sends once, rearms after leaving its safe zone, and never exposes credentials", async () => {
  const env = memoryEnvironment();
  const webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-webhook";
  const secret = "test-only-signing-secret";
  env.saved.set("encrypted_futu_feishu_config_v1", await encryptFeishuConfig({ webhook, secret }, env.FUTU_BRIDGE_TOKEN));
  const delivered = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (resource, options = {}) => {
    const destination = String(resource);
    if (destination.startsWith("https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json")) {
      return Response.json({ generated_at: new Date().toISOString(), opportunities: [liveCandidate()] });
    }
    if (destination === webhook) {
      delivered.push(JSON.parse(options.body));
      return Response.json({ code: 0 });
    }
    throw new Error(`Unexpected mocked request: ${destination}`);
  };
  const headers = { Authorization: "Bearer test-only-token", "Content-Type": "application/json" };
  const upload = async (price) => worker.fetch(new Request("https://worker.example/futu/snapshot", {
    method: "PUT",
    headers,
    body: JSON.stringify({
      generated_at: new Date().toISOString(),
      opend: { connected: true },
      quotes: {
        "US.MU": {
          code: "US.MU",
          last_price: price,
          pre_price: price,
          after_price: price,
          overnight_price: price,
          quote_time: new Date().toISOString(),
        },
      },
    }),
  }), env);

  try {
    const first = await (await upload(100)).json();
    assert.equal(first.live_alerts.alerts_sent, 1);
    assert.equal(first.live_alerts.monitored_candidates, 1);
    assert.equal(delivered.length, 1);
    assert.ok(delivered[0].timestamp);
    assert.ok(delivered[0].sign);
    assert.match(delivered[0].card.elements[0].text.content, /真实 Futu 现价/);
    assert.doesNotMatch(JSON.stringify(first), new RegExp(secret));

    const repeated = await (await upload(99)).json();
    assert.equal(repeated.live_alerts.alerts_sent, 0);
    assert.equal(delivered.length, 1);

    const outside = await (await upload(110)).json();
    assert.equal(outside.live_alerts.in_zone, 0);

    const reentered = await (await upload(98)).json();
    assert.equal(reentered.live_alerts.alerts_sent, 1);
    assert.equal(delivered.length, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("expired research cannot generate a live Futu buy notification", async () => {
  const env = memoryEnvironment();
  env.saved.set("encrypted_futu_feishu_config_v1", await encryptFeishuConfig({
    webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-webhook",
    secret: "test",
  }, env.FUTU_BRIDGE_TOKEN));
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    generated_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(),
    opportunities: [liveCandidate()],
  });
  try {
    const response = await worker.fetch(new Request("https://worker.example/futu/snapshot", {
      method: "PUT",
      headers: { Authorization: "Bearer test-only-token", "Content-Type": "application/json" },
      body: JSON.stringify({
        generated_at: new Date().toISOString(),
        opend: { connected: true },
        quotes: { "US.MU": { last_price: 100, quote_time: new Date().toISOString() } },
      }),
    }), env);
    assert.equal((await response.json()).live_alerts.status, "research_expired");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
