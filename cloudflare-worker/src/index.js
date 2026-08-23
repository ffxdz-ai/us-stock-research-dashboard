const BEIJING_TIME_ZONE = "Asia/Shanghai";
const DEFAULT_ALLOWED_ORIGIN = "https://ffxdz-ai.github.io";
const DEFAULT_ARCHIVE_INDEX_URL = "https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json";
const DEFAULT_OWNER = "ffxdz-ai";
const DEFAULT_REPO = "us-stock-research-dashboard";
const DEFAULT_WORKFLOW = "deepseek-daily-report.yml";
const DEFAULT_REF = "main";
const COOLDOWN_SECONDS = 120;

export function beijingDayKey(value = new Date()) {
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: BEIJING_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(parsed);
}

export function allowedRequestOrigin(request, env = {}) {
  const allowed = String(env.ALLOWED_ORIGIN || DEFAULT_ALLOWED_ORIGIN).replace(/\/$/, "");
  const origin = String(request.headers.get("Origin") || "").replace(/\/$/, "");
  return origin && origin === allowed ? origin : "";
}

export function archiveDeepseekState(archive, now = new Date()) {
  const reports = Array.isArray(archive?.reports) ? archive.reports : [];
  const latestDeepseek = reports.find((report) => report && report.kind === "deepseek-cloud") || null;
  const reportTimestamp = latestDeepseek?.published_at
    || latestDeepseek?.published_label
    || latestDeepseek?.id
    || "";
  return {
    generated_at: reportTimestamp,
    updated_today: Boolean(reportTimestamp && beijingDayKey(reportTimestamp) === beijingDayKey(now)),
  };
}

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Cache-Control": "no-store",
    Vary: "Origin",
  };
}

function jsonResponse(payload, status, origin) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...corsHeaders(origin),
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function githubConfig(env) {
  return {
    owner: env.GITHUB_OWNER || DEFAULT_OWNER,
    repo: env.GITHUB_REPO || DEFAULT_REPO,
    workflow: env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW,
    ref: env.GITHUB_REF || DEFAULT_REF,
  };
}

function githubHeaders(env, authenticated = false) {
  const headers = {
    Accept: "application/vnd.github+json",
    "User-Agent": "us-stock-research-trigger/1.0",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  if (authenticated && env.GITHUB_TOKEN) headers.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return headers;
}

async function readArchiveState(env) {
  const url = new URL(env.ARCHIVE_INDEX_URL || DEFAULT_ARCHIVE_INDEX_URL);
  url.searchParams.set("trigger_check", Date.now().toString());
  const response = await fetch(url, {
    headers: { Accept: "application/json", "User-Agent": "us-stock-research-trigger/1.0" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`archive index HTTP ${response.status}`);
  const archive = await response.json();
  return archiveDeepseekState(archive);
}

async function readLatestWorkflowRun(env) {
  const { owner, repo, workflow, ref } = githubConfig(env);
  const url = new URL(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs`);
  url.searchParams.set("branch", ref);
  url.searchParams.set("per_page", "1");
  const response = await fetch(url, {
    headers: githubHeaders(env, Boolean(env.GITHUB_TOKEN)),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`GitHub runs HTTP ${response.status}`);
  const payload = await response.json();
  const run = Array.isArray(payload.workflow_runs) ? payload.workflow_runs[0] : null;
  if (!run) return null;
  return {
    id: run.id,
    status: run.status,
    conclusion: run.conclusion,
    created_at: run.created_at,
    updated_at: run.updated_at,
  };
}

async function dispatchWorkflow(env) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN secret is not configured");
  const { owner, repo, workflow, ref } = githubConfig(env);
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      ...githubHeaders(env, true),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref, inputs: { mode: "full", force: "false" } }),
  });
  if (response.status !== 204 && !response.ok) {
    throw new Error(`GitHub dispatch HTTP ${response.status}`);
  }
}

function runIsActive(run) {
  return Boolean(run && ["queued", "in_progress", "waiting", "pending", "requested"].includes(run.status));
}

async function cooldownActive(request) {
  if (typeof caches === "undefined" || !caches.default) return false;
  const key = new Request(new URL("/__internal/trigger-cooldown", request.url), { method: "GET" });
  return Boolean(await caches.default.match(key));
}

async function setCooldown(request) {
  if (typeof caches === "undefined" || !caches.default) return;
  const key = new Request(new URL("/__internal/trigger-cooldown", request.url), { method: "GET" });
  const value = new Response("active", { headers: { "Cache-Control": `public, max-age=${COOLDOWN_SECONDS}` } });
  await caches.default.put(key, value);
}

async function statusPayload(env) {
  const [archive, latestRun] = await Promise.all([
    readArchiveState(env),
    readLatestWorkflowRun(env),
  ]);
  return {
    ok: true,
    archive_updated_today: archive.updated_today,
    archive_generated_at: archive.generated_at,
    latest_run: latestRun,
    can_trigger: !archive.updated_today && !runIsActive(latestRun),
    time_zone: BEIJING_TIME_ZONE,
  };
}

async function handleRequest(request, env) {
  const origin = allowedRequestOrigin(request, env);
  if (!origin) return jsonResponse({ ok: false, code: "origin_not_allowed", message: "请求来源不受信任。" }, 403, "null");

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin) });
  const url = new URL(request.url);

  if (request.method === "GET" && url.pathname === "/status") {
    try {
      return jsonResponse(await statusPayload(env), 200, origin);
    } catch (error) {
      console.error("status check failed", error);
      return jsonResponse({ ok: false, code: "status_unavailable", message: "更新状态暂时不可用。" }, 503, origin);
    }
  }

  if (request.method === "POST" && url.pathname === "/trigger") {
    try {
      const current = await statusPayload(env);
      if (current.archive_updated_today) {
        return jsonResponse({ ...current, code: "already_current", message: "北京时间今日报告已经更新。" }, 409, origin);
      }
      if (runIsActive(current.latest_run)) {
        return jsonResponse({ ...current, code: "already_running", message: "更新任务已经在运行。" }, 409, origin);
      }
      if (await cooldownActive(request)) {
        return jsonResponse({ ...current, code: "cooldown", message: "刚刚已经提交更新，请稍后查看进度。" }, 429, origin);
      }
      await dispatchWorkflow(env);
      await setCooldown(request);
      return jsonResponse({ ok: true, code: "accepted", message: "更新任务已提交。" }, 202, origin);
    } catch (error) {
      console.error("workflow dispatch failed", error);
      return jsonResponse({ ok: false, code: "trigger_failed", message: "暂时无法提交更新，请稍后重试。" }, 502, origin);
    }
  }

  return jsonResponse({ ok: false, code: "not_found", message: "Not found" }, 404, origin);
}

export default { fetch: handleRequest };
