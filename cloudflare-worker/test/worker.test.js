import assert from "node:assert/strict";
import test from "node:test";

import { allowedRequestOrigin, archiveDeepseekState, beijingDayKey } from "../src/index.js";

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
