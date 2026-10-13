const test = require("node:test");
const assert = require("node:assert/strict");
const { updatePhase, reportTimestamp } = require("../docs/update-status.js");

test("a skipped successful workflow releases the update button instead of waiting for Pages", () => {
  assert.equal(updatePhase({ status: "completed", conclusion: "success", outcome: "blocked" }, { requested: true }), "blocked");
});

test("workflow success without deployment evidence never indicates deployment", () => {
  assert.equal(updatePhase({ status: "completed", conclusion: "success" }, { requested: true }), "unverified");
});

test("confirmed deployment still waits for the website's new report", () => {
  const run = { status: "completed", conclusion: "success", deployment_confirmed: true };
  assert.equal(updatePhase(run, { archiveConfirmed: false }), "syncing");
  assert.equal(updatePhase(run, { archiveConfirmed: true }), "updated");
});

test("pending and active jobs remain pollable; failed jobs release the button", () => {
  assert.equal(updatePhase(null, { requested: true }), "waiting");
  assert.equal(updatePhase({ status: "in_progress" }), "running");
  assert.equal(updatePhase({ status: "in_progress", outcome: "deploying" }), "deploying");
  assert.equal(updatePhase({ status: "completed", conclusion: "failure" }), "failed");
});

test("legacy report timestamps consistently use Beijing wall time", () => {
  assert.equal(reportTimestamp({ published_label: "2026-10-13 13:14", published_at: "2026-10-13T13:14+00:00" }), "2026-10-13T13:14+08:00");
  assert.equal(reportTimestamp({ published_at: "2026-10-13T05:14:00Z" }), "2026-10-13T05:14:00Z");
});
