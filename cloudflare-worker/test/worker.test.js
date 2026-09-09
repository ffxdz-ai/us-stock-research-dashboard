import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  allowedRequestOrigin, archiveDeepseekState, beijingDayKey, encryptFeishuConfig,
  evaluateLiveSteadyBuyPlan, LiveQuoteStore, liveQuotePrice, liveRiskReward,
  publicLiveQuoteSnapshot, qualifiesLiveSteadyBuyPlan, quoteIsFresh, sanitizeFutuSnapshot,
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

function memoryDurableEnvironment() {
  const durableSaved = new Map();
  const instance = new LiveQuoteStore({
    storage: {
      async get(key) { return durableSaved.get(key); },
      async put(key, value) { durableSaved.set(key, value); },
    },
  }, {});
  const stub = {
    async fetch(resource, options) {
      return instance.fetch(resource instanceof Request ? resource : new Request(resource, options));
    },
  };
  const env = memoryEnvironment();
  env.durableSaved = durableSaved;
  env.LIVE_QUOTES = { getByName() { return stub; } };
  return env;
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

test("manual report refresh can run repeatedly on the same Beijing day and always forces generation", async () => {
  const archiveTimestamp = new Date().toISOString();
  const dispatchedBodies = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (resource, options = {}) => {
    const url = String(resource);
    if (url.startsWith("https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json")) {
      return Response.json({ reports: [{ kind: "deepseek-cloud", published_at: archiveTimestamp }] });
    }
    if (url.includes("/actions/workflows/deepseek-daily-report.yml/runs")) {
      return Response.json({
        workflow_runs: [{
          id: 100,
          status: "completed",
          conclusion: "success",
          created_at: archiveTimestamp,
          updated_at: archiveTimestamp,
        }],
      });
    }
    if (url.endsWith("/actions/workflows/deepseek-daily-report.yml/dispatches")) {
      dispatchedBodies.push(JSON.parse(options.body));
      return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected mocked request: ${url}`);
  };
  const env = { GITHUB_TOKEN: "test-only-token", ALLOWED_ORIGIN: "https://ffxdz-ai.github.io" };
  const request = () => new Request("https://worker.example/trigger", {
    method: "POST",
    headers: { Origin: "https://ffxdz-ai.github.io", "Content-Type": "application/json" },
    body: "{}",
  });

  try {
    const first = await worker.fetch(request(), env);
    const second = await worker.fetch(request(), env);
    assert.equal(first.status, 202);
    assert.equal(second.status, 202);
    assert.equal(dispatchedBodies.length, 2);
    assert.deepEqual(dispatchedBodies[0], { ref: "main", inputs: { mode: "full", force: "true" } });

    const status = await worker.fetch(new Request("https://worker.example/status", {
      headers: { Origin: "https://ffxdz-ai.github.io" },
    }), env);
    const payload = await status.json();
    assert.equal(payload.archive_updated_today, true);
    assert.equal(payload.can_trigger, true);
    assert.equal(payload.repeat_trigger_allowed, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("manual report refresh still blocks a duplicate while a workflow is active", async () => {
  let dispatchCount = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (resource) => {
    const url = String(resource);
    if (url.startsWith("https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json")) {
      return Response.json({ reports: [] });
    }
    if (url.includes("/actions/workflows/deepseek-daily-report.yml/runs")) {
      return Response.json({ workflow_runs: [{ id: 101, status: "in_progress", conclusion: null }] });
    }
    if (url.endsWith("/actions/workflows/deepseek-daily-report.yml/dispatches")) {
      dispatchCount += 1;
      return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected mocked request: ${url}`);
  };

  try {
    const response = await worker.fetch(new Request("https://worker.example/trigger", {
      method: "POST",
      headers: { Origin: "https://ffxdz-ai.github.io", "Content-Type": "application/json" },
      body: "{}",
    }), { GITHUB_TOKEN: "test-only-token", ALLOWED_ORIGIN: "https://ffxdz-ai.github.io" });
    assert.equal(response.status, 409);
    assert.equal((await response.json()).code, "already_running");
    assert.equal(dispatchCount, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
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
        timestamp_kind: "exchange",
        push_confirmed: true,
        currency: "USD",
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
  assert.equal(clean.quotes["US.MU"].push_confirmed, true);
  assert.equal(clean.quotes["US.MU"].currency, "USD");
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

test("Durable Object is authoritative while KV remains a compatibility backup", async () => {
  const env = memoryDurableEnvironment();
  const timestamp = new Date().toISOString();
  const headers = { Authorization: "Bearer test-only-token", "Content-Type": "application/json" };
  const put = await worker.fetch(new Request("https://worker.example/futu/snapshot", {
    method: "PUT",
    headers,
    body: JSON.stringify({
      generated_at: timestamp,
      opend: { connected: true },
      quotes: {
        "US.MU": {
          code: "US.MU",
          live_price: 101.25,
          last_price: 101.25,
          quote_time: timestamp,
          live_session: "regular",
          source: "Futu OpenD",
          currency: "USD",
        },
      },
    }),
  }), env);
  assert.equal(put.status, 200);
  assert.equal((await put.json()).storage.durable, true);
  assert.equal(env.durableSaved.get("snapshot").quotes["US.MU"].live_price, 101.25);

  env.saved.set("latest_futu_public_quote_snapshot", JSON.stringify({
    bridge: { received_at: timestamp },
    quotes: { "US.MU": { last_price: 1, quote_time: timestamp } },
  }));
  const internal = await worker.fetch(new Request("https://worker.example/futu/snapshot", { headers }), env);
  assert.equal((await internal.json()).snapshot.quotes["US.MU"].live_price, 101.25);

  const publicResponse = await worker.fetch(new Request("https://worker.example/futu/quotes", {
    headers: { Origin: "https://ffxdz-ai.github.io" },
  }), env);
  assert.equal(publicResponse.status, 200);
  const publicPayload = await publicResponse.json();
  assert.equal(publicPayload.status, "normal");
  assert.equal(publicPayload.quotes["US.MU"].live_price, 101.25);
  assert.equal(publicPayload.quotes["US.MU"].currency, "USD");
  assert.equal(publicPayload.snapshot, undefined);
  assert.equal(publicPayload.bridge, undefined);
  assert.equal(JSON.stringify(publicPayload).includes("test-only-token"), false);

  const crossOriginRead = await worker.fetch(new Request("https://worker.example/futu/quotes", {
    headers: { Origin: "https://attacker.example" },
  }), env);
  assert.equal(crossOriginRead.status, 200);
  assert.equal(crossOriginRead.headers.get("Access-Control-Allow-Origin"), "*");
  assert.equal((await crossOriginRead.json()).snapshot, undefined);

  const crossOriginWrite = await worker.fetch(new Request("https://worker.example/futu/quotes", {
    method: "POST",
    headers: { Origin: "https://attacker.example" },
  }), env);
  assert.equal(crossOriginWrite.status, 405);
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
  const quote = { last_price: 100, quote_time: "2026-08-25T13:59:30Z" };
  const plan = liveCandidate();
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, quote, now), true);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, last_price: 102 }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, last_price: 96 }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { ...quote, quote_time: "2026-08-25T13:58:00Z" }, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, future_function_audit: "BLOCK" }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, technical_data_complete: false }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, gate_failures: ["data_gap"] }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, rr_ratio: 1.5 }, quote, now), false);
  assert.equal(qualifiesLiveSteadyBuyPlan({ ...plan, price_freshness: "stale" }, quote, now), false);
});

test("live entry recomputes R/R from the current quote without changing research levels", () => {
  const now = new Date("2026-08-25T14:00:00Z");
  const plan = liveCandidate({ safe_entry_zone_low: 97, safe_entry_zone_high: 108, safe_entry_max_price: 108 });
  assert.equal(liveRiskReward(plan, 100), 3);
  assert.equal(qualifiesLiveSteadyBuyPlan(plan, { last_price: 100, quote_time: "2026-08-25T13:59:30Z" }, now), true);
  const evaluation = evaluateLiveSteadyBuyPlan(plan, { last_price: 107, quote_time: "2026-08-25T13:59:30Z" }, now);
  assert.equal(evaluation.qualified, false);
  assert.equal(evaluation.status, "rr_below_required");
  assert.ok(evaluation.live_rr_ratio < 3);
  assert.equal(plan.entry_price, 100);
  assert.equal(plan.stop_loss, 90);
  assert.equal(plan.target_price, 130);
});

test("extended-hours freshness requires a confirmed push and never treats receipt as trade time", () => {
  const now = new Date("2026-08-26T01:00:00Z");
  const quote = {
    live_price: 103,
    live_session: "overnight",
    exchange_quote_time: "2026-08-25T20:00:00Z",
    received_at: "2026-08-26T00:59:30Z",
  };
  assert.equal(quoteIsFresh(quote, now), false);
  assert.equal(quoteIsFresh({ ...quote, push_confirmed: true }, now), true);
  const projected = publicLiveQuoteSnapshot({
    generated_at: now.toISOString(),
    bridge: { received_at: now.toISOString(), authenticated: true },
    quotes: { "US.MU": { ...quote, push_confirmed: true, secret: "must-not-leak" } },
  }, now);
  assert.equal(projected.quotes["US.MU"].stale, false);
  assert.equal(projected.quotes["US.MU"].secret, undefined);
  assert.equal(projected.quotes["US.MU"].exchange_quote_time, "2026-08-25T20:00:00Z");
  assert.equal(projected.received_at, now.toISOString());
  assert.equal(projected.bridge, undefined);
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
  const upload = async (price, quoteTime = new Date().toISOString()) => worker.fetch(new Request("https://worker.example/futu/snapshot", {
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
          quote_time: quoteTime,
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
    assert.match(delivered[0].card.elements[0].text.content, /Futu 当前价/);
    assert.doesNotMatch(JSON.stringify(first), new RegExp(secret));

    const repeated = await (await upload(99)).json();
    assert.equal(repeated.live_alerts.alerts_sent, 0);
    assert.equal(delivered.length, 1);

    const staleOutside = await (await upload(110, new Date(Date.now() - 5 * 60 * 1000).toISOString())).json();
    assert.equal(staleOutside.live_alerts.in_zone, 0);
    const afterStaleReconnect = await (await upload(99)).json();
    assert.equal(afterStaleReconnect.live_alerts.alerts_sent, 0);
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
