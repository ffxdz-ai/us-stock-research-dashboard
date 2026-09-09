"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  assessQuoteFreshness,
  calculateLivePlanMetrics,
  normalizeSnapshot,
} = require("../docs/live-quotes.js");

const NOW = Date.parse("2026-09-09T00:30:00+08:00");

test("normalizes the public Worker quote map without inventing an exchange timestamp", () => {
  const snapshot = normalizeSnapshot({
    status: "normal",
    received_at: "2026-09-09T00:29:50+08:00",
    quotes: {
      "US.CIEN": {
        live_price: 343.83,
        currency: "USD",
        received_at: "2026-09-09T00:29:48+08:00",
        live_session: "overnight",
        timestamp_kind: "transport_receipt",
        push_confirmed: true,
      },
    },
  }, NOW);

  const quote = snapshot.quotes.get("US.CIEN");
  assert.equal(quote.price, 343.83);
  assert.equal(quote.quoteTime, "");
  assert.equal(quote.pushReceivedAt, "2026-09-09T00:29:48+08:00");
  assert.equal(quote.sessionLabel, "夜盘");
  assert.equal(assessQuoteFreshness(quote, snapshot, NOW).status, "push");
});

test("requires a confirmed push for receipt-timed extended-hours quotes", () => {
  const snapshot = normalizeSnapshot({
    received_at: "2026-09-09T00:29:55+08:00",
    quotes: {
      "US.MU": {
        live_price: 118.2,
        received_at: "2026-09-09T00:29:50+08:00",
        live_session: "overnight",
        timestamp_kind: "transport_receipt",
        push_confirmed: false,
      },
    },
  }, NOW);

  assert.equal(assessQuoteFreshness(snapshot.quotes.get("US.MU"), snapshot, NOW).status, "pending");
});

test("accepts a fresh exchange timestamp even on the initial regular-session snapshot", () => {
  const snapshot = normalizeSnapshot({
    received_at: "2026-09-09T00:29:55+08:00",
    quotes: {
      "US.MU": {
        live_price: 118.2,
        exchange_quote_time: "2026-09-09T00:29:50+08:00",
        live_session: "regular",
        timestamp_kind: "exchange",
        push_confirmed: false,
      },
    },
  }, NOW);

  assert.equal(assessQuoteFreshness(snapshot.quotes.get("US.MU"), snapshot, NOW).status, "live");
});

test("marks confirmed, zoned exchange quotes live only inside both freshness windows", () => {
  const snapshot = normalizeSnapshot({
    received_at: "2026-09-09T00:29:55+08:00",
    quotes: {
      "US.MU": {
        live_price: 118.2,
        exchange_quote_time: "2026-09-09T00:29:50+08:00",
        live_session: "regular",
        timestamp_kind: "exchange",
        push_confirmed: true,
      },
    },
  }, NOW);

  const quote = snapshot.quotes.get("US.MU");
  assert.equal(assessQuoteFreshness(quote, snapshot, NOW).status, "live");
  assert.equal(assessQuoteFreshness(quote, snapshot, NOW + 200000).status, "disconnected");
});

test("computes live distance and R/R from fixed research levels", () => {
  const plan = {
    safe_entry_zone_low: 110,
    safe_entry_max_price: 115,
    stop_loss: 100,
    target_price: 145,
  };
  const inside = calculateLivePlanMetrics(plan, 114);
  assert.equal(inside.entryState, "inside");
  assert.equal(inside.rr, 31 / 14);
  assert.match(inside.rrText, /2\.21:1/);

  const above = calculateLivePlanMetrics(plan, 120);
  assert.equal(above.entryState, "above");
  assert.match(above.entryText, /需回落 4\.2%/);

  assert.equal(calculateLivePlanMetrics(plan, 99).rrText.includes("计划止损"), true);
});
