const BEIJING_TIME_ZONE = "Asia/Shanghai";
const DEFAULT_ALLOWED_ORIGIN = "https://ffxdz-ai.github.io";
const DEFAULT_ARCHIVE_INDEX_URL = "https://ffxdz-ai.github.io/us-stock-research-dashboard/data/index.json";
const DEFAULT_OWNER = "ffxdz-ai";
const DEFAULT_REPO = "us-stock-research-dashboard";
const DEFAULT_WORKFLOW = "deepseek-daily-report.yml";
const DEFAULT_REF = "main";
const FUTU_SNAPSHOT_KEY = "latest_futu_public_quote_snapshot";
const FUTU_FEISHU_CONFIG_KEY = "encrypted_futu_feishu_config_v1";
const FUTU_LIVE_ALERT_STATE_KEY = "futu_live_steady_buy_state_v1";
const FUTU_SNAPSHOT_TTL_SECONDS = 12 * 60 * 60;
const FUTU_LIVE_ALERT_STATE_TTL_SECONDS = 30 * 24 * 60 * 60;
const MAX_LIVE_QUOTE_AGE_MS = 15 * 60 * 1000;
const MAX_RESEARCH_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_FEISHU_CONFIG_BYTES = 16 * 1024;
const MAX_FUTU_SNAPSHOT_BYTES = 1024 * 1024;
const MAX_FUTU_QUOTES = 250;
const PUBLIC_QUOTE_FIELDS = new Set([
  "code", "symbol", "name", "last_price", "cur_price", "prev_close_price", "open_price",
  "high_price", "low_price", "volume", "turnover", "turnover_rate", "change_rate",
  "update_time", "data_date", "data_time", "quote_time", "pe_ttm", "pb_rate", "ps_ttm",
  "market_val", "pre_price", "after_price", "overnight_price", "source",
]);

function bridgeJsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store, private",
    },
  });
}

function bearerTokenMatches(request, expected) {
  const actual = String(request.headers.get("Authorization") || "");
  const wanted = `Bearer ${String(expected || "")}`;
  if (!expected || actual.length !== wanted.length) return false;
  let mismatch = 0;
  for (let index = 0; index < wanted.length; index += 1) {
    mismatch |= actual.charCodeAt(index) ^ wanted.charCodeAt(index);
  }
  return mismatch === 0;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function encodeBase64(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function decodeBase64(value) {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
}

async function feishuEncryptionKey(token) {
  const material = new TextEncoder().encode(`futu-feishu-alert-v1:${token}`);
  const digest = await crypto.subtle.digest("SHA-256", material);
  return crypto.subtle.importKey("raw", digest, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

export async function encryptFeishuConfig(config, token) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify(config));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await feishuEncryptionKey(token), plaintext);
  return JSON.stringify({ version: 1, iv: encodeBase64(iv), ciphertext: encodeBase64(ciphertext) });
}

async function decryptFeishuConfig(value, token) {
  const encrypted = JSON.parse(value);
  if (encrypted?.version !== 1 || !encrypted.iv || !encrypted.ciphertext) throw new Error("invalid encrypted configuration");
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decodeBase64(encrypted.iv) },
    await feishuEncryptionKey(token),
    decodeBase64(encrypted.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}

function approvedFeishuWebhook(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:"
      && ["open.feishu.cn", "open.larksuite.com"].includes(parsed.hostname)
      && parsed.pathname.startsWith("/open-apis/bot/v2/hook/")
      && !parsed.username
      && !parsed.password;
  } catch {
    return false;
  }
}

async function handleFeishuConfig(request, env) {
  if (!bearerTokenMatches(request, env.FUTU_BRIDGE_TOKEN)) {
    return bridgeJsonResponse({ ok: false, code: "unauthorized" }, 401);
  }
  if (!env.FUTU_SNAPSHOT_KV) return bridgeJsonResponse({ ok: false, code: "bridge_not_configured" }, 503);
  if (request.method !== "PUT") return bridgeJsonResponse({ ok: false, code: "method_not_allowed" }, 405);
  if (Number(request.headers.get("Content-Length") || 0) > MAX_FEISHU_CONFIG_BYTES) {
    return bridgeJsonResponse({ ok: false, code: "configuration_too_large" }, 413);
  }
  let raw;
  try {
    raw = await request.text();
    if (new TextEncoder().encode(raw).length > MAX_FEISHU_CONFIG_BYTES) {
      return bridgeJsonResponse({ ok: false, code: "configuration_too_large" }, 413);
    }
    const payload = JSON.parse(raw);
    const webhook = String(payload?.webhook || "").trim();
    const secret = String(payload?.secret || "").trim();
    if (!approvedFeishuWebhook(webhook) || secret.length > 2048) {
      return bridgeJsonResponse({ ok: false, code: "invalid_feishu_configuration" }, 422);
    }
    const encrypted = await encryptFeishuConfig({ webhook, secret }, env.FUTU_BRIDGE_TOKEN);
    await env.FUTU_SNAPSHOT_KV.put(FUTU_FEISHU_CONFIG_KEY, encrypted);
    return bridgeJsonResponse({ ok: true, configured: true, encrypted: true });
  } catch {
    return bridgeJsonResponse({ ok: false, code: "configuration_unavailable" }, 503);
  }
}

function newYorkMinutes(now) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(now);
  const hour = Number(parts.find((part) => part.type === "hour")?.value || 0);
  const minute = Number(parts.find((part) => part.type === "minute")?.value || 0);
  return hour * 60 + minute;
}

export function liveQuotePrice(symbol, quote, now = new Date()) {
  if (!quote || typeof quote !== "object") return null;
  const options = [];
  if (String(symbol || "").startsWith("US.")) {
    const minutes = newYorkMinutes(now);
    if (minutes >= 20 * 60 || minutes < 4 * 60) options.push(quote.overnight_price);
    else if (minutes < 9 * 60 + 30) options.push(quote.pre_price);
    else if (minutes >= 16 * 60) options.push(quote.after_price);
  }
  options.push(quote.last_price, quote.cur_price);
  return options.map(finiteNumber).find((value) => value !== null && value > 0) ?? null;
}

function quoteIsFresh(quote, now) {
  const timestamp = Date.parse(String(quote?.quote_time || ""));
  if (!Number.isFinite(timestamp)) return false;
  const age = now.getTime() - timestamp;
  return age >= -5 * 60 * 1000 && age <= MAX_LIVE_QUOTE_AGE_MS;
}

function validLiveTradePath(item) {
  const entry = finiteNumber(item?.entry_price ?? item?.safe_entry_price);
  const stop = finiteNumber(item?.stop_loss);
  const target = finiteNumber(item?.target_price);
  const ratio = finiteNumber(item?.rr_ratio);
  const required = finiteNumber(item?.rr_required);
  return entry !== null && stop !== null && target !== null && ratio !== null && required !== null
    && stop < entry && entry < target && ratio >= required;
}

export function qualifiesLiveSteadyBuyPlan(item, quote, now = new Date()) {
  if (!item || typeof item !== "object" || !quote || !quoteIsFresh(quote, now)) return false;
  if (item.status !== "waiting_entry" || item.entry_tier !== "formal" || item.signal_type !== "formal") return false;
  if (item.formal_qualified !== true || item.entry_execution_status !== "wait_pullback") return false;
  if (item.execution_allowed !== true || item.technical_data_complete !== true) return false;
  if (item.future_function_audit !== "PASS" || item.price_freshness !== "fresh" || item.gate_failures?.length) return false;
  if (!validLiveTradePath(item)) return false;
  if (["opportunity_score", "trend_score", "crowding_score"].some((field) => finiteNumber(item[field]) === null)) return false;

  const price = liveQuotePrice(item.symbol, quote, now);
  const low = finiteNumber(item.safe_entry_zone_low);
  const high = finiteNumber(item.safe_entry_zone_high);
  const maximum = finiteNumber(item.safe_entry_max_price ?? high);
  const stop = finiteNumber(item.stop_loss);
  return price !== null && low !== null && high !== null && maximum !== null && stop !== null
    && stop < low && low <= high && high <= maximum && price >= low && price <= maximum && price > stop;
}

async function liveSignalFingerprint(item) {
  const fields = {
    entry_price: finiteNumber(item.entry_price),
    risk_policy_version: String(item.risk_policy_version || ""),
    rr_ratio: finiteNumber(item.rr_ratio),
    safe_entry_max_price: finiteNumber(item.safe_entry_max_price),
    safe_entry_zone_high: finiteNumber(item.safe_entry_zone_high),
    safe_entry_zone_low: finiteNumber(item.safe_entry_zone_low),
    signal_type: String(item.signal_type || ""),
    stop_loss: finiteNumber(item.stop_loss),
    symbol: String(item.symbol || ""),
    target_price: finiteNumber(item.target_price),
  };
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(fields)));
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("").slice(0, 24);
}

function displayPrice(value, currency) {
  const parsed = finiteNumber(value);
  return parsed === null ? "待确认" : `${parsed.toFixed(2)} ${String(currency || "").trim()}`.trim();
}

function liveFeishuCard(items) {
  const elements = [];
  for (const [index, item] of items.entries()) {
    if (index) elements.push({ tag: "hr" });
    const currency = String(item.currency || "");
    const content = [
      `**${item.symbol} · ${String(item.name || "名称待确认")}**`,
      `真实 Futu 现价：${displayPrice(item.live_price, currency)}（${item.live_quote_time}）`,
      `稳健买入区间：${displayPrice(item.safe_entry_zone_low, currency)} ～ ${displayPrice(item.safe_entry_zone_high, currency)}`,
      `最高执行价：${displayPrice(item.safe_entry_max_price, currency)}｜止损：${displayPrice(item.stop_loss, currency)}｜目标：${displayPrice(item.target_price, currency)}`,
      `R/R：${finiteNumber(item.rr_ratio).toFixed(2)}:1｜机会分：${finiteNumber(item.opportunity_score).toFixed(1)}｜趋势：${finiteNumber(item.trend_score).toFixed(1)}`,
      "**已进入稳健买入区间；下单前请再次核对实时行情，跳空高于最高执行价不追。**",
    ].join("\n");
    elements.push({ tag: "div", text: { tag: "lark_md", content } });
  }
  elements.push(
    { tag: "note", elements: [{ tag: "plain_text", content: "正式到价提醒来自认证 Futu 行情，已复核全部研究风控门槛；不自动下单。" }] },
    { tag: "action", actions: [{ tag: "button", type: "primary", text: { tag: "plain_text", content: "查看投研驾驶舱" }, url: "https://ffxdz-ai.github.io/us-stock-research-dashboard/" }] },
  );
  return {
    config: { wide_screen_mode: true },
    header: { template: "green", title: { tag: "plain_text", content: `Futu 稳健买点到价 · ${items.length} 只` } },
    elements,
  };
}

async function signedFeishuPayload(card, secret) {
  const payload = { msg_type: "interactive", card };
  if (!secret) return payload;
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(`${timestamp}\n${secret}`),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new Uint8Array());
  return { ...payload, timestamp, sign: encodeBase64(signature) };
}

async function sendLiveFeishuCard(config, items) {
  const response = await fetch(config.webhook, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8", "User-Agent": "us-stock-research-futu-alerts/1.0" },
    body: JSON.stringify(await signedFeishuPayload(liveFeishuCard(items), config.secret)),
  });
  if (!response.ok) throw new Error(`Feishu HTTP ${response.status}`);
  const result = await response.json();
  if (![0, null, undefined].includes(result?.code ?? result?.StatusCode)) {
    throw new Error(`Feishu rejected code ${result.code ?? result.StatusCode}`);
  }
}

async function readLiveAlertArchive(env, now) {
  const url = new URL(env.ARCHIVE_INDEX_URL || DEFAULT_ARCHIVE_INDEX_URL);
  url.searchParams.set("live_quote_check", now.getTime().toString());
  const response = await fetch(url, {
    headers: { Accept: "application/json", "User-Agent": "us-stock-research-futu-alerts/1.0" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`archive HTTP ${response.status}`);
  const archive = await response.json();
  const generatedAt = Date.parse(String(archive?.generated_at || ""));
  const age = now.getTime() - generatedAt;
  if (!Number.isFinite(generatedAt) || age < -5 * 60 * 1000 || age > MAX_RESEARCH_AGE_MS) {
    return { archive: null, status: "research_expired" };
  }
  return { archive, status: "ready" };
}

async function evaluateLiveFeishuAlerts(snapshot, env) {
  const encryptedConfig = await env.FUTU_SNAPSHOT_KV.get(FUTU_FEISHU_CONFIG_KEY);
  if (!encryptedConfig) return { status: "not_configured", monitored_candidates: 0, alerts_sent: 0 };
  const config = await decryptFeishuConfig(encryptedConfig, env.FUTU_BRIDGE_TOKEN);
  if (!approvedFeishuWebhook(config.webhook)) throw new Error("invalid encrypted notification configuration");

  const now = new Date();
  const { archive, status } = await readLiveAlertArchive(env, now);
  if (!archive) return { status, monitored_candidates: 0, alerts_sent: 0 };
  const opportunities = Array.isArray(archive.opportunities) ? archive.opportunities : [];
  const monitored = opportunities.filter((item) => item?.formal_qualified === true && item?.status === "waiting_entry");
  let state = {};
  try {
    state = JSON.parse(await env.FUTU_SNAPSHOT_KV.get(FUTU_LIVE_ALERT_STATE_KEY) || "{}");
  } catch {
    state = {};
  }
  const previous = state.signals && typeof state.signals === "object" ? state.signals : {};
  const current = {};
  const selected = [];

  for (const item of monitored) {
    const symbol = String(item.symbol || "").trim().toUpperCase();
    const quote = snapshot.quotes[symbol];
    if (!qualifiesLiveSteadyBuyPlan(item, quote, now)) continue;
    const fingerprint = await liveSignalFingerprint(item);
    current[symbol] = fingerprint;
    const prior = previous[symbol] && typeof previous[symbol] === "object" ? previous[symbol] : {};
    if (prior.active !== true || prior.fingerprint !== fingerprint) {
      selected.push({ ...item, live_price: liveQuotePrice(symbol, quote, now), live_quote_time: quote.quote_time });
    }
  }

  const timestamp = now.toISOString();
  const signals = {};
  for (const [symbol, prior] of Object.entries(previous)) {
    signals[symbol] = { ...(prior && typeof prior === "object" ? prior : {}), active: Boolean(current[symbol]), last_checked_at: timestamp };
  }
  let sent = 0;
  for (let offset = 0; offset < selected.length; offset += 4) {
    const batch = selected.slice(offset, offset + 4);
    try {
      await sendLiveFeishuCard(config, batch);
      for (const item of batch) {
        signals[item.symbol] = { active: true, fingerprint: current[item.symbol], last_notified_at: timestamp, last_checked_at: timestamp };
      }
      sent += batch.length;
    } catch {
      for (const item of batch) {
        signals[item.symbol] = { ...signals[item.symbol], active: false, pending_fingerprint: current[item.symbol], last_checked_at: timestamp };
      }
    }
  }
  for (const [symbol, fingerprint] of Object.entries(current)) {
    if (!signals[symbol]) signals[symbol] = { active: false, pending_fingerprint: fingerprint, last_checked_at: timestamp };
  }
  await env.FUTU_SNAPSHOT_KV.put(
    FUTU_LIVE_ALERT_STATE_KEY,
    JSON.stringify({ schema_version: 1, updated_at: timestamp, signals }),
    { expirationTtl: FUTU_LIVE_ALERT_STATE_TTL_SECONDS },
  );
  return {
    status: selected.length > sent ? "notification_retry_pending" : "active",
    monitored_candidates: monitored.length,
    in_zone: Object.keys(current).length,
    alerts_sent: sent,
  };
}

export function sanitizeFutuSnapshot(payload, receivedAt = new Date().toISOString()) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (!payload.opend?.connected || !payload.quotes || typeof payload.quotes !== "object") return null;
  const entries = Object.entries(payload.quotes);
  if (!entries.length || entries.length > MAX_FUTU_QUOTES) return null;

  const quotes = {};
  for (const [key, quote] of entries) {
    const code = String(key || "").trim().toUpperCase();
    if (!/^(US|HK|SH|SZ|SG|MY|JP|CC)\.[A-Z0-9]+$/.test(code)) continue;
    if (!quote || typeof quote !== "object" || Array.isArray(quote)) continue;
    const clean = {};
    for (const [field, value] of Object.entries(quote)) {
      if (!PUBLIC_QUOTE_FIELDS.has(field)) continue;
      if (value === null || ["string", "number", "boolean"].includes(typeof value)) clean[field] = value;
    }
    clean.code = code;
    if (!clean.quote_time) continue;
    quotes[code] = clean;
  }
  if (!Object.keys(quotes).length) return null;

  return {
    schema_version: 2,
    generated_at: String(payload.generated_at || ""),
    generated_label: String(payload.generated_label || ""),
    opend: { connected: true, market_data_only: true },
    summary: {
      scope: String(payload.summary?.scope || "all"),
      quotes_returned: Object.keys(quotes).length,
    },
    privacy: {
      contains_account: false,
      contains_positions: false,
      contains_cash: false,
      contains_cost_basis: false,
      trading_disabled: true,
    },
    bridge: {
      authenticated: true,
      transport: "cloudflare-worker-kv",
      received_at: receivedAt,
    },
    quotes,
  };
}

async function handleFutuSnapshot(request, env) {
  if (!bearerTokenMatches(request, env.FUTU_BRIDGE_TOKEN)) {
    return bridgeJsonResponse({ ok: false, code: "unauthorized" }, 401);
  }
  if (!env.FUTU_SNAPSHOT_KV) {
    return bridgeJsonResponse({ ok: false, code: "bridge_not_configured" }, 503);
  }

  if (request.method === "GET") {
    const stored = await env.FUTU_SNAPSHOT_KV.get(FUTU_SNAPSHOT_KEY);
    if (!stored) return bridgeJsonResponse({ ok: false, code: "snapshot_unavailable" }, 404);
    try {
      return bridgeJsonResponse({ ok: true, snapshot: JSON.parse(stored) });
    } catch {
      return bridgeJsonResponse({ ok: false, code: "snapshot_invalid" }, 503);
    }
  }

  if (request.method !== "PUT") {
    return bridgeJsonResponse({ ok: false, code: "method_not_allowed" }, 405);
  }
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (contentLength > MAX_FUTU_SNAPSHOT_BYTES) {
    return bridgeJsonResponse({ ok: false, code: "snapshot_too_large" }, 413);
  }
  let raw;
  try {
    raw = await request.text();
  } catch {
    return bridgeJsonResponse({ ok: false, code: "invalid_snapshot" }, 400);
  }
  if (new TextEncoder().encode(raw).length > MAX_FUTU_SNAPSHOT_BYTES) {
    return bridgeJsonResponse({ ok: false, code: "snapshot_too_large" }, 413);
  }
  let payload;
  try {
    payload = sanitizeFutuSnapshot(JSON.parse(raw));
  } catch {
    payload = null;
  }
  if (!payload) return bridgeJsonResponse({ ok: false, code: "invalid_or_empty_snapshot" }, 422);
  await env.FUTU_SNAPSHOT_KV.put(FUTU_SNAPSHOT_KEY, JSON.stringify(payload), {
    expirationTtl: FUTU_SNAPSHOT_TTL_SECONDS,
  });
  let liveAlerts;
  try {
    liveAlerts = await evaluateLiveFeishuAlerts(payload, env);
  } catch {
    // A notification outage must never interrupt the authenticated quote feed.
    liveAlerts = { status: "temporarily_unavailable", monitored_candidates: 0, alerts_sent: 0 };
  }
  return bridgeJsonResponse({
    ok: true,
    quotes_returned: payload.summary.quotes_returned,
    received_at: payload.bridge.received_at,
    expires_in_seconds: FUTU_SNAPSHOT_TTL_SECONDS,
    live_alerts: liveAlerts,
  });
}

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
    body: JSON.stringify({ ref, inputs: { mode: "full", force: "true" } }),
  });
  if (response.status !== 204 && !response.ok) {
    throw new Error(`GitHub dispatch HTTP ${response.status}`);
  }
}

function runIsActive(run) {
  return Boolean(run && ["queued", "in_progress", "waiting", "pending", "requested"].includes(run.status));
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
    can_trigger: !runIsActive(latestRun),
    repeat_trigger_allowed: true,
    time_zone: BEIJING_TIME_ZONE,
  };
}

async function handleRequest(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/futu/feishu-config") {
    return handleFeishuConfig(request, env);
  }
  if (url.pathname === "/futu/snapshot") {
    try {
      return await handleFutuSnapshot(request, env);
    } catch (error) {
      console.error("Futu quote bridge failed", error);
      return bridgeJsonResponse({ ok: false, code: "bridge_unavailable" }, 503);
    }
  }
  const origin = allowedRequestOrigin(request, env);
  if (!origin) return jsonResponse({ ok: false, code: "origin_not_allowed", message: "请求来源不受信任。" }, 403, "null");

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin) });

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
      if (runIsActive(current.latest_run)) {
        return jsonResponse({ ...current, code: "already_running", message: "更新任务已经在运行。" }, 409, origin);
      }
      await dispatchWorkflow(env);
      return jsonResponse({ ok: true, code: "accepted", message: "强制更新任务已提交；本次完成后可再次运行。" }, 202, origin);
    } catch (error) {
      console.error("workflow dispatch failed", error);
      return jsonResponse({ ok: false, code: "trigger_failed", message: "暂时无法提交更新，请稍后重试。" }, 502, origin);
    }
  }

  return jsonResponse({ ok: false, code: "not_found", message: "Not found" }, 404, origin);
}

export default { fetch: handleRequest };
