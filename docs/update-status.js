(function exposeUpdateStatus(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.UpdateStatusUtils = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createUpdateStatusUtils() {
  "use strict";

  function reportTimestamp(report) {
    const label = String(report?.published_label || "");
    return /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?$/.test(label)
      ? `${label.replace(" ", "T")}+08:00`
      : report?.published_at || "";
  }

  function updatePhase(run, { requested = false, archiveConfirmed = false } = {}) {
    if (run?.outcome === "blocked") return "blocked";
    if (run?.outcome === "failed" || (run?.status === "completed" && run.conclusion !== "success")) return "failed";
    if (["queued", "in_progress", "waiting", "pending", "requested"].includes(run?.status)) {
      return run.outcome === "deploying" ? "deploying" : "running";
    }
    if (run?.deployment_confirmed === true) return archiveConfirmed ? "updated" : "syncing";
    if (run?.status === "completed") return "unverified";
    return requested ? "waiting" : "idle";
  }

  return { reportTimestamp, updatePhase };
}));
