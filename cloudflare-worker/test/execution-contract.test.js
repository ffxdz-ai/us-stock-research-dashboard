import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { evaluateLiveSteadyBuyPlan } from "../src/index.js";

const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/execution_contract.json", import.meta.url), "utf8"));

test("T19 Worker matches the shared qualification/execution fixture", () => {
  for (const row of fixture.cases) {
    const quote = {
      last_price: row.price,
      quote_time: row.quote_time,
      live_session: row.live_session,
    };
    const result = evaluateLiveSteadyBuyPlan(fixture.candidate, quote, new Date(row.now));
    assert.equal(result.qualified, row.expected_execution, row.name);
  }
});
