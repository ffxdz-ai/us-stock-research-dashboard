const state = {
  archive: null,
  reports: [],
  opportunities: [],
  reportContentCache: new Map(),
  activeId: "",
  kind: "all",
  query: "",
  opportunityStatus: "all",
  opportunityQuery: "",
};

const els = {
  filters: document.querySelector("#filters"),
  searchInput: document.querySelector("#searchInput"),
  reportCount: document.querySelector("#reportCount"),
  generatedAt: document.querySelector("#generatedAt"),
  reportList: document.querySelector("#reportList"),
  reportKind: document.querySelector("#reportKind"),
  reportTitle: document.querySelector("#reportTitle"),
  reportDate: document.querySelector("#reportDate"),
  reportContent: document.querySelector("#reportContent"),
  dashboardUpdated: document.querySelector("#dashboardUpdated"),
  marketStatus: document.querySelector("#marketStatus"),
  marketStatusNote: document.querySelector("#marketStatusNote"),
  executableCount: document.querySelector("#executableCount"),
  waitEntryCount: document.querySelector("#waitEntryCount"),
  noChaseCount: document.querySelector("#noChaseCount"),
  dataGapCount: document.querySelector("#dataGapCount"),
  latestReportTime: document.querySelector("#latestReportTime"),
  pendingReviewCount: document.querySelector("#pendingReviewCount"),
  dashboardReports: document.querySelector("#dashboardReports"),
  gapBreakdownUpdated: document.querySelector("#gapBreakdownUpdated"),
  gapBreakdownSummary: document.querySelector("#gapBreakdownSummary"),
  gapBreakdownGrid: document.querySelector("#gapBreakdownGrid"),
  dataHealthSource: document.querySelector("#dataHealthSource"),
  dataHealthList: document.querySelector("#dataHealthList"),
  reviewStatsUpdated: document.querySelector("#reviewStatsUpdated"),
  reviewMetricGrid: document.querySelector("#reviewMetricGrid"),
  reviewStatsRows: document.querySelector("#reviewStatsRows"),
  opportunityCount: document.querySelector("#opportunityCount"),
  opportunityStatusFilters: document.querySelector("#opportunityStatusFilters"),
  opportunitySearch: document.querySelector("#opportunitySearch"),
  opportunityCards: document.querySelector("#opportunityCards"),
};

const DASHBOARD_REPORT_TARGETS = [
  { kind: "cross-market-intelligence", label: "最近一篇跨市场情报报告" },
  { kind: "secondary-queue", label: "最近一篇二次分析队列报告" },
  { kind: "opportunity-radar", label: "最近一篇机会雷达报告" },
];

const DATA_HEALTH_KEYWORDS = ["不可用", "限流", "fallback", "数据不足", "需补充", "缺口", "unavailable"];

const OPPORTUNITY_STATUS_META = {
  executable: { label: "可执行观察", className: "executable" },
  waiting_entry: { label: "等待买点", className: "waiting-entry" },
  avoid_chasing: { label: "禁止追高", className: "avoid-chasing" },
  secondary_analysis: { label: "二次分析", className: "secondary-analysis" },
  watchlist: { label: "观察", className: "watchlist" },
  invalidated: { label: "逻辑失效", className: "invalidated" },
  review: { label: "复盘中", className: "review" },
};

const OPPORTUNITY_REPORT_KINDS = ["secondary-queue", "opportunity-radar", "cross-market-intelligence"];

function escapeHtml(value) {
  return String(value || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function tableCells(line) { return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim()); }
function isTableSeparator(line) { const cells = tableCells(line); return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell)); }

function renderMarkdown(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (line.startsWith("```")) {
      const code = []; index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) { code.push(lines[index]); index += 1; }
      index += 1; html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`); continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) { const level = heading[1].length; html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`); index += 1; continue; }
    if (line.trim().startsWith("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headers = tableCells(line); const rows = []; index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) { rows.push(tableCells(lines[index])); index += 1; }
      html.push(`<div class="table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`); continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items = []; while (index < lines.length && /^[-*]\s+/.test(lines[index])) { items.push(lines[index].replace(/^[-*]\s+/, "")); index += 1; }
      html.push(`<ul>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`); continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items = []; while (index < lines.length && /^\d+\.\s+/.test(lines[index])) { items.push(lines[index].replace(/^\d+\.\s+/, "")); index += 1; }
      html.push(`<ol>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ol>`); continue;
    }
    if (line.startsWith(">")) {
      const quote = []; while (index < lines.length && lines[index].startsWith(">")) { quote.push(lines[index].replace(/^>\s?/, "")); index += 1; }
      html.push(`<blockquote>${quote.map(inlineMarkdown).join("<br>")}</blockquote>`); continue;
    }
    if (/^-{3,}$/.test(line.trim())) { html.push("<hr>"); index += 1; continue; }
    const paragraph = [line.trim()]; index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{1,4})\s+|^```|^[-*]\s+|^\d+\.\s+|^>/.test(lines[index])) {
      if (lines[index].trim().startsWith("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) break;
      paragraph.push(lines[index].trim()); index += 1;
    }
    html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
  }
  return html.join("\n");
}

function archiveObject() {
  return state.archive && typeof state.archive === "object" && !Array.isArray(state.archive) ? state.archive : {};
}

function dashboardData() {
  const archive = archiveObject();
  return [archive.dashboard, archive.today_dashboard, archive.decision_dashboard, archive.today_decision]
    .find((value) => value && typeof value === "object" && !Array.isArray(value)) || {};
}

function latestReport() { return state.reports[0] || null; }
function latestReportByKind(kind) { return state.reports.find((report) => report.kind === kind) || null; }

function getReportCacheKey(report) {
  return String((report && (report.id || report.filename || report.title)) || "");
}

function cachedReportContent(report) {
  const key = getReportCacheKey(report);
  if (!key) return "";
  return state.reportContentCache.get(key) || "";
}

function reportText(report) {
  return String((report && (report.content || cachedReportContent(report) || report.summary || report.description)) || "");
}

async function fetchJson(path) {
  const response = await fetch(`${path}?v=${Date.now()}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadArchiveIndex() {
  try {
    const payload = await fetchJson("./data/index.json");
    payload.__archive_source = "index";
    return payload;
  } catch (indexError) {
    try {
      const payload = await fetchJson("./data/reports.json");
      payload.__archive_source = "legacy";
      return payload;
    } catch (legacyError) {
      throw new Error(`索引加载失败：index.json ${indexError.message}；reports.json ${legacyError.message}`);
    }
  }
}

async function loadReportContent(report) {
  if (!report) return "";
  const key = getReportCacheKey(report);
  if (report.content) {
    if (key) state.reportContentCache.set(key, report.content);
    return report.content;
  }
  if (key && state.reportContentCache.has(key)) return state.reportContentCache.get(key);
  if (!report.id) throw new Error("报告缺少 id，无法加载正文。");
  const detailPath = report.content_path || `./data/reports/${encodeURIComponent(report.id)}.json`;
  const payload = await fetchJson(detailPath);
  const content = String(payload.content || "");
  if (!content) throw new Error("单篇报告文件缺少 content 字段。");
  if (key) state.reportContentCache.set(key, content);
  report.content = content;
  return content;
}

async function preloadFallbackReportContent() {
  const preloadKinds = [...OPPORTUNITY_REPORT_KINDS, "opportunity-review-metrics", "fmp-research", "macro-regime"];
  const reports = [latestReport(), ...preloadKinds.map((kind) => latestReportByKind(kind))]
    .filter(Boolean)
    .filter((report, index, list) => list.findIndex((item) => item.id === report.id) === index);
  await Promise.all(reports.map((report) => loadReportContent(report).catch(() => "")));
}

function directField(keys) {
  const archive = archiveObject();
  const dashboard = dashboardData();
  const sources = [dashboard, archive.summary, archive.metrics, archive];
  for (const source of sources) {
    if (!source || typeof source !== "object") continue;
    for (const key of keys) {
      const value = source[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
  }
  return null;
}

function directNumber(keys) {
  const value = directField(keys);
  if (value === null) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstRegexNumber(text, patterns) {
  for (const pattern of patterns) {
    const match = String(text || "").match(pattern);
    if (!match) continue;
    const number = Number(match[1]);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function numberFromReports(reports, patterns) {
  for (const report of reports.filter(Boolean)) {
    const parsed = firstRegexNumber(reportText(report), patterns);
    if (parsed !== null) return parsed;
  }
  return null;
}

function countKeywordLines(reports, keywords) {
  const seen = new Set();
  reports.filter(Boolean).forEach((report) => {
    reportText(report).split(/\n+/).forEach((line) => {
      const normalized = line.replace(/\s+/g, " ").trim();
      if (!normalized || normalized.length < 6) return;
      if (!keywords.some((keyword) => normalized.toLowerCase().includes(keyword.toLowerCase()))) return;
      seen.add(normalized.slice(0, 180));
    });
  });
  return seen.size ? seen.size : null;
}

function dashboardCount(keys, reports, patterns, keywordFallback) {
  const direct = directNumber(keys);
  if (direct !== null) return direct;
  const parsed = numberFromReports(reports, patterns);
  if (parsed !== null) return parsed;
  return keywordFallback || null;
}

function countLabel(value) {
  return value === null || value === undefined ? "待确认" : String(value);
}

function splitMarkdownRow(line) {
  return String(line || "").trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function looksLikeSymbol(value) {
  return /^(US|HK|CN|SH|SZ)\.[A-Z0-9]+$/.test(String(value || "").trim()) || /^[A-Z]{1,6}$/.test(String(value || "").trim());
}

function gapCategoryMeta(key) {
  const meta = {
    fmp_estimate_snapshot: {
      label: "缺少 FMP 预期/财报快照",
      group: "data",
      groupLabel: "数据缺口",
      severity: "medium",
      fallback: "Finnhub / SEC / Nasdaq earnings fallback",
    },
    non_us_data_source: {
      label: "非美股统一财务/公告数据不足",
      group: "data",
      groupLabel: "数据缺口",
      severity: "medium",
      fallback: "AkShare / Tushare / Futu 本地镜像",
    },
    analyst_estimate: {
      label: "缺少年/季度分析师预期",
      group: "data",
      groupLabel: "数据缺口",
      severity: "medium",
      fallback: "Finnhub estimates / FMP estimates",
    },
    earnings_surprise: {
      label: "缺少最近财报 surprise",
      group: "data",
      groupLabel: "数据缺口",
      severity: "medium",
      fallback: "FMP earnings surprise / Nasdaq earnings",
    },
    sec_recent_filing: {
      label: "缺少 SEC 最近申报记录",
      group: "data",
      groupLabel: "数据缺口",
      severity: "high",
      fallback: "SEC submissions / companyfacts",
    },
    sec_financial_facts: {
      label: "缺少 SEC 财务事实",
      group: "data",
      groupLabel: "数据缺口",
      severity: "high",
      fallback: "SEC companyfacts",
    },
    entry_path_missing: {
      label: "缺少完整 R/R 或机械入场路径",
      group: "entry",
      groupLabel: "入场路径缺失",
      severity: "medium",
      fallback: "补齐当前价、支撑、止损、目标价、R/R",
    },
    rr_discipline: {
      label: "R/R 未达 2:1",
      group: "discipline",
      groupLabel: "交易纪律不通过",
      severity: "warning",
      fallback: "等待回调、上修目标价或提高止损逻辑质量",
    },
    permission_limited: {
      label: "接口权限/限流受限",
      group: "permission",
      groupLabel: "权限受限",
      severity: "warning",
      fallback: "升级套餐、降低频率或使用 SEC/公司 IR 替代证据",
    },
    fmp_symbol_permission_limited: {
      label: "FMP 个股端点受限",
      group: "permission",
      groupLabel: "权限受限",
      severity: "warning",
      fallback: "降低 FMP 预期层置信度；等待额度恢复或升级套餐",
    },
    other: {
      label: "其他待复核缺口",
      group: "other",
      groupLabel: "待人工复核",
      severity: "unknown",
      fallback: "人工检查报告正文",
    },
  };
  return meta[key] || meta.other;
}

function classifyEvidenceGapItem(item) {
  const text = String(item || "");
  if (/R\/R\s*未达|风险收益比.*不足|不能进入普通买入/.test(text)) return "rr_discipline";
  if (/完整\s*R\/R|机械入场路径|入场路径/.test(text)) return "entry_path_missing";
  if (/FMP.*预期|财报快照/.test(text)) return "fmp_estimate_snapshot";
  if (/非美股.*统一财务|公告正文数据源/.test(text)) return "non_us_data_source";
  if (/年\/季度分析师预期|分析师预期/.test(text)) return "analyst_estimate";
  if (/surprise|财报\s*surprise/i.test(text)) return "earnings_surprise";
  if (/SEC\s*最近申报记录/.test(text)) return "sec_recent_filing";
  if (/SEC\s*财务事实/.test(text)) return "sec_financial_facts";
  if (/端点受限|限流|权限受限/.test(text)) return "fmp_symbol_permission_limited";
  return "other";
}

function addGapCategory(bucket, key, count, affected, overrides = {}) {
  if (!count) return;
  const meta = gapCategoryMeta(key);
  if (!bucket[key]) {
    bucket[key] = {
      key,
      label: overrides.label || meta.label,
      group: overrides.group || meta.group,
      groupLabel: overrides.groupLabel || overrides.group_label || meta.groupLabel,
      severity: overrides.severity || meta.severity,
      fallback: overrides.fallback || meta.fallback,
      count: 0,
      affected: new Set(),
    };
  }
  bucket[key].count += count;
  (Array.isArray(affected) ? affected : [affected]).filter(Boolean).forEach((item) => bucket[key].affected.add(item));
}

function normalizeGapCategories(categories) {
  return Object.values(categories).map((item) => ({
    ...item,
    affected: Array.from(item.affected || []),
  })).sort((a, b) => {
    const order = { data: 1, permission: 2, entry: 3, discipline: 4, other: 5 };
    return (order[a.group] || 9) - (order[b.group] || 9) || b.count - a.count;
  });
}

function structuredGapBreakdown() {
  const archive = archiveObject();
  const source = archive.evidence_gap_breakdown || archive.gap_breakdown || archive.data_gap_breakdown;
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const categories = {};
  const rawCategories = Array.isArray(source.categories) ? source.categories : Array.isArray(source.items) ? source.items : [];
  rawCategories.forEach((item) => {
    const key = item.key || item.type || "other";
    addGapCategory(categories, key, Number(item.count) || 0, item.affected_symbols || item.affected || [], {
      label: item.label,
      group: item.group,
      groupLabel: item.group_label,
      fallback: item.fallback,
      severity: item.severity,
    });
  });
  const normalized = normalizeGapCategories(categories);
  return {
    source: "structured",
    updatedAt: source.updated_at || latestReportTimeLabel(),
    originalTotal: Number(source.original_total ?? source.total) || null,
    dataGap: Number(source.data_gap ?? source.data_gap_count) || normalized.filter((item) => item.group === "data").reduce((sum, item) => sum + item.count, 0),
    permission: Number(source.permission_limited ?? source.permission_count) || normalized.filter((item) => item.group === "permission").reduce((sum, item) => sum + item.count, 0),
    entryPath: Number(source.entry_path_missing ?? source.entry_path_count) || normalized.filter((item) => item.group === "entry").reduce((sum, item) => sum + item.count, 0),
    rrDiscipline: Number(source.rr_discipline ?? source.rr_fail_count) || normalized.filter((item) => item.group === "discipline").reduce((sum, item) => sum + item.count, 0),
    other: normalized.filter((item) => item.group === "other").reduce((sum, item) => sum + item.count, 0),
    categories: normalized,
  };
}

function deriveEvidenceGapBreakdown() {
  const structured = structuredGapBreakdown();
  if (structured) return structured;
  const report = latestReportByKind("event-evidence") || latestReport();
  const text = reportText(report);
  const categories = {};
  const originalTotal = firstRegexNumber(text, [/证据缺口[：:]\s*(\d+)/, /数据缺口[：:]\s*(\d+)/]);
  const permissionCount = firstRegexNumber(text, [/权限缺口[：:]\s*(\d+)/, /受限端点[：:]\s*(\d+)/]);
  const permissionAffected = [];

  text.split(/\n+/).forEach((line) => {
    if (!line.trim().startsWith("|") || line.includes("---")) return;
    const cells = splitMarkdownRow(line);
    if (cells[0] && cells[0].startsWith("/") && /^(4\d\d|5\d\d|limited|restricted|rate_limited|429)$/i.test(cells[1] || "")) {
      permissionAffected.push(cells[0]);
    }
    if (cells.length < 10 || !looksLikeSymbol(cells[0])) return;
    const symbol = cells[0];
    const gapCell = cells[8] || "";
    gapCell.split(/[；;]/).map((item) => item.trim()).filter(Boolean).forEach((item) => {
      addGapCategory(categories, classifyEvidenceGapItem(item), 1, symbol);
    });
  });

  if (permissionCount) addGapCategory(categories, "permission_limited", permissionCount, permissionAffected.length ? permissionAffected : "FMP 受限端点");
  const normalized = normalizeGapCategories(categories);
  const dataGap = normalized.filter((item) => item.group === "data").reduce((sum, item) => sum + item.count, 0);
  const permission = normalized.filter((item) => item.group === "permission").reduce((sum, item) => sum + item.count, 0);
  const entryPath = normalized.filter((item) => item.group === "entry").reduce((sum, item) => sum + item.count, 0);
  const rrDiscipline = normalized.filter((item) => item.group === "discipline").reduce((sum, item) => sum + item.count, 0);
  const other = normalized.filter((item) => item.group === "other").reduce((sum, item) => sum + item.count, 0);
  const computedTotal = dataGap + permission + entryPath + rrDiscipline + other;

  return {
    source: report ? "event-evidence-report" : "fallback",
    updatedAt: report ? report.published_label : latestReportTimeLabel(),
    originalTotal: originalTotal ?? (computedTotal || null),
    dataGap: dataGap || null,
    permission: permission || null,
    entryPath: entryPath || null,
    rrDiscipline: rrDiscipline || null,
    other: other || null,
    categories: normalized,
  };
}

function extractMarketStatusText() {
  const direct = directField(["market_status", "risk_status", "today_market_status", "market_regime"]);
  if (direct) return String(direct);
  const candidates = [latestReportByKind("macro-regime"), latestReportByKind("deepseek-cloud"), latestReport()];
  const patterns = [
    /今日市场状态[：:]\s*([^。\n；;]+)/,
    /宏观状态[：:]\s*([^。\n；;]+)/,
    /今日策略[：:]\s*([^。\n；;]+)/,
    /市场状态[：:]\s*([^。\n；;]+)/,
  ];
  for (const report of candidates.filter(Boolean)) {
    const text = reportText(report);
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match && match[1]) return match[1].trim();
    }
  }
  return "";
}

function normalizeMarketStatus(value) {
  const raw = String(value || "").trim();
  const lower = raw.toLowerCase();
  if (!raw) return { label: "待确认", className: "unknown", note: "等待最新宏观或市场报告" };
  if (/risk[-\s]?off|防守|避险|偏防守|退守/.test(lower)) {
    return { label: "Risk-off", className: "risk-off", note: raw };
  }
  if (/risk[-\s]?on|偏进攻|结构性进攻|主动进攻|进攻/.test(lower)) {
    return { label: "Risk-on", className: "risk-on", note: raw };
  }
  if (/neutral|中性|平衡|震荡/.test(lower)) {
    return { label: "Neutral", className: "neutral", note: raw };
  }
  return { label: "待确认", className: "unknown", note: raw };
}

function latestReportTimeLabel() {
  const archive = archiveObject();
  return archive.generated_label || archive.generated_at || (latestReport() && latestReport().published_label) || "待确认";
}

function buildDashboardMetrics() {
  const latest = latestReport();
  const deepseek = latestReportByKind("deepseek-cloud");
  const secondary = latestReportByKind("secondary-queue");
  const opportunity = latestReportByKind("opportunity-radar");
  const evidence = latestReportByKind("event-evidence");
  const fmp = latestReportByKind("fmp-research");
  const market = normalizeMarketStatus(extractMarketStatusText());
  const reviewStats = deriveReviewStats();
  const gapBreakdown = deriveEvidenceGapBreakdown();
  const opportunityStatusCounts = state.opportunities.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});

  const executableZero = [deepseek, latest].filter(Boolean).some((report) => /今日没有普通买入标的|没有普通买入优先级|没有.*可执行/.test(reportText(report)));
  const executable = executableZero ? 0 : dashboardCount(
    ["executable_opportunity_count", "executable_count", "buyable_count", "actionable_count"],
    [deepseek, secondary, opportunity, latest],
    [/今日可执行机会(?:数量)?[：:]\s*(\d+)/, /可执行机会[：:]\s*(\d+)/, /普通买入标的[：:]\s*(\d+)/, /机械可买候选[：:]\s*(\d+)/],
    opportunityStatusCounts.executable || null
  );
  const waiting = dashboardCount(
    ["waiting_entry_count", "wait_entry_count", "waiting_buy_point_count", "pullback_wait_count"],
    [deepseek, secondary, opportunity, latest],
    [/等待买点(?:数量)?[：:]\s*(\d+)/, /等待回调[：:]\s*(\d+)/, /等回调[：:]\s*(\d+)/, /理想回调[：:]\s*(\d+)/],
    opportunityStatusCounts.waiting_entry || countKeywordLines([deepseek, secondary, opportunity], ["等待买点", "等待回调", "等回调", "理想回调"])
  );
  const noChase = dashboardCount(
    ["no_chase_count", "chase_ban_count", "forbid_chase_count", "overheat_count"],
    [deepseek, secondary, opportunity, latest],
    [/禁止追高(?:数量)?[：:]\s*(\d+)/, /严禁追高[：:]\s*(\d+)/, /不追高[：:]\s*(\d+)/],
    opportunityStatusCounts.avoid_chasing || countKeywordLines([deepseek, secondary, opportunity], ["禁止追高", "严禁追高", "不追高", "不能普通买入", "不允许普通买入", "R/R 未达标"])
  );
  const dataGap = directNumber(["true_data_gap_count", "data_source_gap_count"]) ?? gapBreakdown.dataGap ?? dashboardCount(
    ["data_gap_count", "evidence_gap_count", "gap_count", "permission_gap_count"],
    [evidence, fmp, deepseek, latest],
    [/数据缺口[：:]\s*(\d+)/, /证据缺口[：:]\s*(\d+)/, /权限缺口[：:]\s*(\d+)/, /受限端点[：:]\s*(\d+)/],
    null
  );

  const pendingReview = directNumber(["pending_review_count", "pending_count", "review_pending_count"]) ?? reviewStats?.pending_count ?? null;

  return { market, executable, waiting, noChase, dataGap, pendingReview, latestTime: latestReportTimeLabel(), gapBreakdown };
}

function renderDashboardReports() {
  if (!els.dashboardReports) return;
  els.dashboardReports.replaceChildren();
  DASHBOARD_REPORT_TARGETS.forEach((target) => {
    const report = latestReportByKind(target.kind);
    const card = document.createElement(report ? "button" : "article");
    card.className = `dashboard-report-card ${report ? "" : "disabled"}`;
    if (report) {
      card.type = "button";
      card.dataset.reportId = report.id;
      card.dataset.kind = report.kind;
    }
    const label = document.createElement("span"); label.textContent = target.label;
    const title = document.createElement("strong"); title.textContent = report ? report.title : "暂无报告";
    const time = document.createElement("time"); time.textContent = report ? report.published_label : "待生成";
    card.append(label, title, time);
    els.dashboardReports.appendChild(card);
  });
}

function renderDecisionDashboard() {
  const metrics = buildDashboardMetrics();
  if (els.dashboardUpdated) els.dashboardUpdated.textContent = `更新 ${metrics.latestTime}`;
  if (els.marketStatus) {
    els.marketStatus.textContent = metrics.market.label;
    els.marketStatus.className = `market-status ${metrics.market.className}`;
  }
  if (els.marketStatusNote) els.marketStatusNote.textContent = metrics.market.note;
  if (els.executableCount) els.executableCount.textContent = countLabel(metrics.executable);
  if (els.waitEntryCount) els.waitEntryCount.textContent = countLabel(metrics.waiting);
  if (els.noChaseCount) els.noChaseCount.textContent = countLabel(metrics.noChase);
  if (els.dataGapCount) els.dataGapCount.textContent = countLabel(metrics.dataGap);
  if (els.latestReportTime) els.latestReportTime.textContent = metrics.latestTime;
  if (els.pendingReviewCount) els.pendingReviewCount.textContent = countLabel(metrics.pendingReview);
  renderDashboardReports();
}

function gapCountLabel(value) {
  return value === null || value === undefined ? "待确认" : String(value);
}

function renderGapBreakdown() {
  if (!els.gapBreakdownGrid) return;
  const breakdown = deriveEvidenceGapBreakdown();
  if (els.gapBreakdownUpdated) els.gapBreakdownUpdated.textContent = breakdown.updatedAt ? `更新 ${breakdown.updatedAt}` : "等待结构化数据";
  if (els.gapBreakdownSummary) {
    els.gapBreakdownSummary.textContent = `原始合计 ${gapCountLabel(breakdown.originalTotal)} = 真实数据缺口 ${gapCountLabel(breakdown.dataGap)} + 权限受限 ${gapCountLabel(breakdown.permission)} + 入场路径缺失 ${gapCountLabel(breakdown.entryPath)} + R/R 纪律不通过 ${gapCountLabel(breakdown.rrDiscipline)}。`;
  }
  els.gapBreakdownGrid.replaceChildren();

  const aggregateCards = [
    { label: "真实数据缺口", count: breakdown.dataGap, group: "data", detail: "财务、预期、SEC、港/A统一数据等缺失" },
    { label: "权限受限", count: breakdown.permission, group: "permission", detail: "接口 429、套餐限制或端点不可用" },
    { label: "入场路径缺失", count: breakdown.entryPath, group: "entry", detail: "缺少完整买点、止损、目标价、R/R" },
    { label: "R/R 纪律不通过", count: breakdown.rrDiscipline, group: "discipline", detail: "交易条件不达标，不算数据抓取失败" },
  ];

  aggregateCards.forEach((item) => {
    const card = document.createElement("article");
    card.className = `gap-card gap-${item.group}`;
    const label = document.createElement("span"); label.textContent = item.label;
    const count = document.createElement("strong"); count.textContent = gapCountLabel(item.count);
    const detail = document.createElement("small"); detail.textContent = item.detail;
    card.append(label, count, detail);
    els.gapBreakdownGrid.appendChild(card);
  });

  const categories = document.createElement("div");
  categories.className = "gap-category-list";
  breakdown.categories.forEach((item) => {
    const row = document.createElement("article");
    row.className = `gap-category gap-${item.group}`;
    const header = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = item.label;
    const badge = document.createElement("span"); badge.textContent = `${item.count}`;
    header.append(title, badge);
    const meta = document.createElement("p");
    const affected = item.affected && item.affected.length ? item.affected.slice(0, 10).join("、") : "待确认";
    const more = item.affected && item.affected.length > 10 ? ` 等 ${item.affected.length} 项` : "";
    meta.textContent = `${item.groupLabel}｜影响：${affected}${more}`;
    const fallback = document.createElement("small"); fallback.textContent = `处理方式：${item.fallback}`;
    row.append(header, meta, fallback);
    categories.appendChild(row);
  });
  if (!breakdown.categories.length) {
    const empty = document.createElement("p");
    empty.className = "placeholder";
    empty.textContent = "缺口拆解：等待结构化数据或事件证据报告。";
    categories.appendChild(empty);
  }
  els.gapBreakdownGrid.appendChild(categories);
}

function normalizeHealthStatus(status) {
  const value = String(status || "").toLowerCase();
  if (/normal|healthy|ok|available|success|正常|可用/.test(value)) return "normal";
  if (/limited|degraded|restricted|rate|fallback|stale|限流|受限|降级|不足/.test(value)) return "limited";
  if (/error|failed|fail|unavailable|down|不可用|错误|失败/.test(value)) return "error";
  return "unknown";
}

function buildFallbackHealthItems() {
  const latest = latestReport();
  const text = reportText(latest);
  const lower = text.toLowerCase();
  const matched = DATA_HEALTH_KEYWORDS.filter((keyword) => lower.includes(keyword.toLowerCase()));
  if (matched.length) {
    return {
      source: "由最新报告正文关键词生成",
      items: [{
        name: "公开数据源",
        status: "limited",
        message: "存在数据缺口，需人工复核",
        impact: `匹配关键词：${matched.slice(0, 5).join("、")}。这是投研可信度提示，不代表系统错误。`,
        updated_at: latest ? latest.published_label : latestReportTimeLabel(),
      }],
    };
  }
  return {
    source: "由最新报告正文关键词生成",
    items: [{
      name: "公开数据源",
      status: "normal",
      message: "最新报告未发现明显数据缺口关键词",
      impact: "仍需以报告正文和后续人工复核为准。",
      updated_at: latest ? latest.published_label : latestReportTimeLabel(),
    }],
  };
}

function dataHealthItems() {
  const archive = archiveObject();
  if (Array.isArray(archive.data_health) && archive.data_health.length) {
    return {
      source: "来自 reports.json data_health",
      items: archive.data_health.map((item) => ({
        name: item && item.name ? item.name : "未命名数据源",
        status: normalizeHealthStatus(item && item.status),
        message: item && item.message ? item.message : "状态说明待补充",
        impact: item && item.impact ? item.impact : "仅作为投研可信度提示。",
        updated_at: item && item.updated_at ? item.updated_at : latestReportTimeLabel(),
      })),
    };
  }
  return buildFallbackHealthItems();
}

function renderDataHealth() {
  if (!els.dataHealthList) return;
  const health = dataHealthItems();
  if (els.dataHealthSource) els.dataHealthSource.textContent = health.source;
  els.dataHealthList.replaceChildren();
  health.items.forEach((item) => {
    const card = document.createElement("article");
    card.className = `health-card ${normalizeHealthStatus(item.status)}`;
    const header = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = item.name;
    const status = document.createElement("span"); status.className = "health-status"; status.textContent = normalizeHealthStatus(item.status);
    header.append(name, status);
    const message = document.createElement("p"); message.textContent = item.message;
    const impact = document.createElement("small"); impact.textContent = item.impact;
    const time = document.createElement("time"); time.textContent = item.updated_at || "待确认";
    card.append(header, message, impact, time);
    els.dataHealthList.appendChild(card);
  });
}

function sampleLabel(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "样本不足";
  if (typeof value === "number" && Number.isFinite(value)) return `${value}${suffix}`;
  return String(value);
}

function percentLabel(value) {
  if (value === null || value === undefined || value === "") return "样本不足";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${parsed.toFixed(1)}%`;
}

function priceLabel(price, currency) {
  if (price === null || price === undefined || price === "") return "样本不足";
  const parsed = Number(price);
  const value = Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(price);
  return currency ? `${value} ${currency}` : value;
}

function normalizeReviewStats(stats) {
  if (!stats || typeof stats !== "object" || Array.isArray(stats)) return null;
  return {
    updated_at: stats.updated_at || stats.generated_label || stats.generated_at || "等待结构化数据",
    tracked_count: stats.tracked_count ?? null,
    completed_count: stats.completed_count ?? null,
    pending_count: stats.pending_count ?? null,
    win_rate_30d: stats.win_rate_30d ?? null,
    avg_max_drawdown: stats.avg_max_drawdown ?? null,
    avg_max_gain: stats.avg_max_gain ?? null,
    best_theme: stats.best_theme ?? null,
    worst_error_type: stats.worst_error_type ?? null,
    items: Array.isArray(stats.items) ? stats.items : [],
    source: "structured",
  };
}

function deriveFallbackReviewStats() {
  const reviewReport = latestReportByKind("opportunity-review-metrics") || latestReport();
  const text = reportText(reviewReport);
  if (!/已跟踪股票|已完成复盘|待复盘|跟踪股票|待到期|待复盘/.test(text)) return null;
  const tracked = firstRegexNumber(text, [/已跟踪股票[：:]\s*(\d+)/, /历史跟踪股票[：:]\s*(\d+)/, /跟踪股票[：:]\s*(\d+)/]);
  const completed = firstRegexNumber(text, [/已完成复盘[：:]\s*(\d+)/, /本轮完成复盘[：:]\s*(\d+)/, /已完成复盘：?(\d+)/]);
  const pending = firstRegexNumber(text, [/待复盘[：:]\s*(\d+)/, /待到期[：:]\s*(\d+)/, /待到期：?(\d+)/]);
  if (tracked === null && completed === null && pending === null) return null;
  return {
    updated_at: reviewReport ? reviewReport.published_label : "等待结构化数据",
    tracked_count: tracked,
    completed_count: completed,
    pending_count: pending,
    win_rate_30d: null,
    avg_max_drawdown: null,
    avg_max_gain: null,
    best_theme: null,
    worst_error_type: null,
    items: [],
    source: "fallback",
  };
}

function deriveReviewStats() {
  const archive = archiveObject();
  return normalizeReviewStats(archive.review_stats) || deriveFallbackReviewStats();
}

function reviewStatusLabel(value) {
  const text = String(value || "").trim();
  if (!text) return "样本不足";
  if (/pending|未成熟|观察/.test(text)) return "待复盘";
  if (/completed|done|完成/.test(text)) return "已完成";
  if (/failed|失败/.test(text)) return "失败";
  if (/hit|win|命中/.test(text)) return "命中";
  return text;
}

function renderReviewStats() {
  if (!els.reviewMetricGrid || !els.reviewStatsRows) return;
  const stats = deriveReviewStats();
  els.reviewMetricGrid.replaceChildren();
  els.reviewStatsRows.replaceChildren();
  if (!stats) {
    if (els.reviewStatsUpdated) els.reviewStatsUpdated.textContent = "复盘统计：等待结构化数据";
    const emptyMetric = document.createElement("article");
    emptyMetric.className = "review-metric-card";
    emptyMetric.innerHTML = "<span>复盘统计</span><strong>等待结构化数据</strong><small>后续由 review_stats 提供完整复盘</small>";
    els.reviewMetricGrid.appendChild(emptyMetric);
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="9">复盘统计：等待结构化数据</td>';
    els.reviewStatsRows.appendChild(row);
    return;
  }

  if (els.reviewStatsUpdated) els.reviewStatsUpdated.textContent = `更新 ${stats.updated_at || "样本不足"}`;
  [
    ["已跟踪股票", sampleLabel(stats.tracked_count), "进入机会跟踪池的标的"],
    ["已完成复盘", sampleLabel(stats.completed_count), "完成 7/30/60/90D 检查"],
    ["待复盘", sampleLabel(stats.pending_count), "等待时间窗口成熟"],
    ["30日命中率", percentLabel(stats.win_rate_30d), "样本不足时不强行计算"],
    ["平均最大回撤", percentLabel(stats.avg_max_drawdown), "风险侧复盘"],
    ["平均最大浮盈", percentLabel(stats.avg_max_gain), "机会侧复盘"],
    ["最强主题", sampleLabel(stats.best_theme), "按当前结构化复盘统计"],
    ["主要错误类型", sampleLabel(stats.worst_error_type), "用于修正规则"],
  ].forEach(([label, value, note]) => {
    const card = document.createElement("article");
    card.className = "review-metric-card";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("strong");
    valueEl.textContent = value;
    const noteEl = document.createElement("small");
    noteEl.textContent = note;
    card.append(labelEl, valueEl, noteEl);
    els.reviewMetricGrid.appendChild(card);
  });

  const items = Array.isArray(stats.items) ? stats.items.slice(0, 40) : [];
  if (!items.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="9">复盘列表：等待结构化明细</td>';
    els.reviewStatsRows.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    [
      `${item.symbol || "待确认"}${item.name ? ` ${item.name}` : ""}`,
      item.first_seen || "样本不足",
      priceLabel(item.first_price, item.currency),
      percentLabel(item.return_7d),
      percentLabel(item.return_30d),
      percentLabel(item.max_drawdown),
      percentLabel(item.max_gain),
      reviewStatusLabel(item.status),
      item.lesson || item.error_type || "样本不足",
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    });
    els.reviewStatsRows.appendChild(row);
  });
}

function normalizeOpportunityStatus(status) {
  const value = String(status || "").trim().toLowerCase();
  if (OPPORTUNITY_STATUS_META[value]) return value;
  if (/execute|buyable|可执行|可买/.test(value)) return "executable";
  if (/wait|entry|回调|等待|买点/.test(value)) return "waiting_entry";
  if (/chase|overheat|追高|过热/.test(value)) return "avoid_chasing";
  if (/secondary|二次/.test(value)) return "secondary_analysis";
  if (/invalid|失效/.test(value)) return "invalidated";
  if (/review|复盘/.test(value)) return "review";
  return "watchlist";
}

function inferOpportunityStatusFromText(text) {
  const value = String(text || "");
  if (/逻辑失效|已经失效|失效|invalidated|invalid/i.test(value)) return "invalidated";
  if (/复盘|review/i.test(value)) return "review";
  if (/退回观察/.test(value)) return "watchlist";
  if (/进入二次分析|交给\s*Buy-Side\s*二次分析|机会重点池|二次分析/i.test(value)) return "secondary_analysis";
  if (/禁止追高|严禁追高|不追高|高开大幅追涨|R\/R[^。；\n|]*低于|低于\s*2(?:\.0)?\s*:?\s*1/i.test(value)) return "avoid_chasing";
  if (/等待买点|等待回踩|等待回调|等回调|理想回调|等待/.test(value)) return "waiting_entry";
  if (/保留观察|普通观察|观察跟踪|跨市场观察|观察/.test(value)) return "watchlist";
  return "watchlist";
}

function inferOpportunityStatusLabelFromText(text, status) {
  const value = String(text || "");
  if (/退回观察/.test(value)) return "退回观察";
  if (/禁止追高|严禁追高|不追高/.test(value)) return "禁止追高";
  if (/等待买点|等待回踩|等待回调|等回调|理想回调/.test(value)) return "等待买点";
  if (/进入二次分析|交给\s*Buy-Side\s*二次分析|机会重点池|二次分析/i.test(value)) return "二次分析";
  return OPPORTUNITY_STATUS_META[status]?.label || "待确认";
}

function marketFromSymbol(symbol) {
  const value = String(symbol || "");
  if (value.startsWith("US.")) return "US";
  if (value.startsWith("HK.")) return "HK";
  if (value.startsWith("SH.") || value.startsWith("SZ.") || value.startsWith("CN.")) return "CN";
  return "待确认";
}

function cleanCellText(value) {
  return String(value || "")
    .replace(/<br\s*\/?>/gi, "；")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function parseLooseNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(String(value).replace(/[$,%\s,]/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function parseRR(text) {
  const match = String(text || "").match(/R\/R\s*([0-9]+(?:\.[0-9]+)?)\s*:?\s*1/i);
  return match ? Number(match[1]) : null;
}

function normalizeList(value) {
  if (Array.isArray(value)) return value.map((item) => cleanCellText(item)).filter(Boolean);
  if (typeof value === "string" && value.trim()) return [cleanCellText(value)];
  return [];
}

function normalizeStructuredOpportunity(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) return { symbol: "" };
  const text = [item.status, item.status_label, item.action, ...(Array.isArray(item.why_changed) ? item.why_changed : [])].join(" ");
  const status = normalizeOpportunityStatus(item.status || inferOpportunityStatusFromText(text));
  const scoreDelta = item.score_delta && typeof item.score_delta === "object" && !Array.isArray(item.score_delta) ? item.score_delta : null;
  return {
    symbol: cleanCellText(item.symbol || item.code || ""),
    name: cleanCellText(item.name || ""),
    market: cleanCellText(item.market || marketFromSymbol(item.symbol || item.code)),
    theme: cleanCellText(item.theme || ""),
    segment: cleanCellText(item.segment || ""),
    status,
    status_label: cleanCellText(item.status_label || OPPORTUNITY_STATUS_META[status]?.label || "待确认"),
    action: cleanCellText(item.action || "动作待确认"),
    price: parseLooseNumber(item.price),
    currency: cleanCellText(item.currency || ""),
    price_time: cleanCellText(item.price_time || item.updated_at || ""),
    price_source: cleanCellText(item.price_source || ""),
    opportunity_score: parseLooseNumber(item.opportunity_score),
    trend_score: parseLooseNumber(item.trend_score),
    crowding_score: parseLooseNumber(item.crowding_score),
    rr_ratio: parseLooseNumber(item.rr_ratio),
    rr_required: parseLooseNumber(item.rr_required),
    score_delta: scoreDelta,
    why_changed: normalizeList(item.why_changed),
    buy_conditions: normalizeList(item.buy_conditions),
    avoid_conditions: normalizeList(item.avoid_conditions),
    invalid_conditions: normalizeList(item.invalid_conditions),
    source: "structured",
    source_label: "reports.json opportunities",
    source_rank: 0,
  };
}

function fallbackSourceReports() {
  return OPPORTUNITY_REPORT_KINDS.map((kind, index) => {
    const report = latestReportByKind(kind);
    return report ? { report, rank: index + 1 } : null;
  }).filter(Boolean);
}

function mergeOpportunity(existing, incoming) {
  if (!existing) return incoming;
  const merged = { ...existing };
  const incomingHigherPriority = (incoming.source_rank ?? 99) < (existing.source_rank ?? 99);
  ["name", "market", "theme", "segment", "price_time", "price_source", "currency", "source_label"].forEach((key) => {
    if (!merged[key] && incoming[key]) merged[key] = incoming[key];
  });
  ["opportunity_score", "trend_score", "crowding_score", "rr_ratio", "rr_required", "price"].forEach((key) => {
    if ((merged[key] === null || merged[key] === undefined) && incoming[key] !== null && incoming[key] !== undefined) merged[key] = incoming[key];
  });
  if (incomingHigherPriority) {
    merged.status = incoming.status || merged.status;
    merged.status_label = incoming.status_label || merged.status_label;
    merged.action = incoming.action || merged.action;
    merged.source_rank = incoming.source_rank;
    merged.source_label = incoming.source_label || merged.source_label;
  } else if (!merged.action && incoming.action) {
    merged.action = incoming.action;
  }
  ["why_changed", "buy_conditions", "avoid_conditions", "invalid_conditions"].forEach((key) => {
    const values = [...(merged[key] || []), ...(incoming[key] || [])];
    merged[key] = [...new Set(values)].slice(0, 5);
  });
  return merged;
}

function upsertFallbackOpportunity(map, incoming) {
  if (!incoming.symbol) return;
  const symbol = incoming.symbol.toUpperCase();
  map.set(symbol, mergeOpportunity(map.get(symbol), { ...incoming, symbol }));
}

function fallbackOpportunityBase(symbol, report, rank, context, extras = {}) {
  const cleanContext = cleanCellText(context);
  const status = extras.status || inferOpportunityStatusFromText(cleanContext);
  const statusLabel = extras.status_label || inferOpportunityStatusLabelFromText(cleanContext, status);
  const rr = extras.rr_ratio !== undefined ? extras.rr_ratio : parseRR(cleanContext);
  return {
    symbol,
    name: cleanCellText(extras.name || ""),
    market: cleanCellText(extras.market || marketFromSymbol(symbol)),
    theme: cleanCellText(extras.theme || ""),
    segment: cleanCellText(extras.segment || ""),
    status,
    status_label: statusLabel,
    action: cleanCellText(extras.action || cleanContext || "动作待确认"),
    price: null,
    currency: "",
    price_time: "",
    price_source: "",
    opportunity_score: extras.opportunity_score !== undefined ? extras.opportunity_score : null,
    trend_score: extras.trend_score !== undefined ? extras.trend_score : null,
    crowding_score: extras.crowding_score !== undefined ? extras.crowding_score : null,
    rr_ratio: rr,
    rr_required: 2,
    score_delta: null,
    why_changed: cleanContext ? [cleanContext] : [],
    buy_conditions: [],
    avoid_conditions: [],
    invalid_conditions: [],
    source: "fallback",
    source_label: report ? `${report.kind_label || report.kind} · ${report.published_label || ""}`.trim() : "报告正文解析",
    source_rank: rank,
  };
}

function parseFallbackTableLine(cells, report, rank, map) {
  const joined = cells.map(cleanCellText).join(" ");
  const symbols = [...new Set((joined.match(/\b(?:US|HK|CN|SH|SZ)\.[A-Z0-9]+\b/g) || []).map((item) => item.toUpperCase()))];
  if (!symbols.length) return;

  symbols.forEach((symbol) => {
    const directCodeIndex = cells.findIndex((cell) => cleanCellText(cell).toUpperCase() === symbol);
    if (directCodeIndex >= 0 && cells.length >= 6) {
      const context = [cells[0], cells[8], cells[9], joined].map(cleanCellText).filter(Boolean).join("；");
      upsertFallbackOpportunity(map, fallbackOpportunityBase(symbol, report, rank, context, {
        market: cells[1],
        name: cells[directCodeIndex + 1],
        theme: cells[directCodeIndex + 2],
        opportunity_score: parseLooseNumber(cells[directCodeIndex + 4]),
        trend_score: parseLooseNumber(cells[directCodeIndex + 5]),
      }));
      return;
    }

    const symbolCell = cells.find((cell) => cleanCellText(cell).toUpperCase().includes(symbol));
    const segments = String(symbolCell || "").split(/<br\s*\/?>|；/i).map(cleanCellText).filter((item) => item.toUpperCase().includes(symbol));
    const context = segments[0] || joined;
    upsertFallbackOpportunity(map, fallbackOpportunityBase(symbol, report, rank, context, {
      theme: cleanCellText(cells[0]),
      segment: cleanCellText(cells[1]),
      opportunity_score: parseLooseNumber((context.match(/机会分\s*([0-9]+(?:\.[0-9]+)?)/) || [])[1]) || null,
      crowding_score: /拥挤/.test(context) ? parseLooseNumber(cells[4]) : null,
    }));
  });
}

function deriveFallbackOpportunities() {
  const map = new Map();
  fallbackSourceReports().forEach(({ report, rank }) => {
    const lines = reportText(report).split(/\n+/);
    lines.forEach((line) => {
      const symbols = line.match(/\b(?:US|HK|CN|SH|SZ)\.[A-Z0-9]+\b/g);
      if (!symbols) return;
      if (line.trim().startsWith("|")) {
        parseFallbackTableLine(tableCells(line), report, rank, map);
        return;
      }
      [...new Set(symbols.map((item) => item.toUpperCase()))].forEach((symbol) => {
        upsertFallbackOpportunity(map, fallbackOpportunityBase(symbol, report, rank, line));
      });
    });
  });
  return [...map.values()].sort((a, b) => (a.source_rank - b.source_rank) || a.symbol.localeCompare(b.symbol));
}

function deriveOpportunities() {
  const archive = archiveObject();
  if (Array.isArray(archive.opportunities) && archive.opportunities.length) {
    return archive.opportunities.map(normalizeStructuredOpportunity).filter((item) => item.symbol);
  }
  return deriveFallbackOpportunities();
}

function formatPriceMeta(opportunity) {
  if (
    opportunity.price === null ||
    opportunity.price === undefined ||
    !opportunity.currency ||
    !opportunity.price_time ||
    !opportunity.price_source
  ) {
    return ["价格数据：待确认"];
  }
  const price = Number.isFinite(Number(opportunity.price)) ? Number(opportunity.price).toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(opportunity.price);
  return [
    `最新价：${price} ${opportunity.currency}`,
    `时间：${opportunity.price_time}`,
    `来源：${opportunity.price_source}`,
  ];
}

function formatDelta(value) {
  if (value === null || value === undefined || value === "") return "";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `（${cleanCellText(value)}）`;
  if (parsed > 0) return `（+${parsed}）`;
  return `（${parsed}）`;
}

function formatScoreMetric(label, value, delta, options = {}) {
  const missing = value === null || value === undefined || value === "";
  const display = missing ? "待结构化" : (options.rr ? `${value}:1` : String(value));
  return `${label} ${display}${formatDelta(delta)}`;
}

function hasScoreDelta(opportunity) {
  return opportunity.score_delta && typeof opportunity.score_delta === "object" && Object.keys(opportunity.score_delta).length > 0;
}

function filteredOpportunities() {
  return state.opportunities.filter((opportunity) => {
    if (state.opportunityStatus !== "all" && opportunity.status !== state.opportunityStatus) return false;
    if (!state.opportunityQuery) return true;
    const query = state.opportunityQuery.toLowerCase();
    return [opportunity.symbol, opportunity.name, opportunity.theme, opportunity.segment, opportunity.action]
      .some((value) => String(value || "").toLowerCase().includes(query));
  });
}

function appendListBlock(parent, title, items, fallbackText) {
  const block = document.createElement("div");
  block.className = "opportunity-list-block";
  const heading = document.createElement("strong");
  heading.textContent = title;
  block.appendChild(heading);
  if (items && items.length) {
    const list = document.createElement("ul");
    items.slice(0, 4).forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    block.appendChild(list);
  } else {
    const empty = document.createElement("p");
    empty.textContent = fallbackText;
    block.appendChild(empty);
  }
  parent.appendChild(block);
}

function renderOpportunityCards() {
  if (!els.opportunityCards) return;
  const opportunities = filteredOpportunities();
  if (els.opportunityCount) els.opportunityCount.textContent = `${opportunities.length} / ${state.opportunities.length} 个`;
  els.opportunityCards.replaceChildren();

  if (!state.opportunities.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无股票状态卡数据。";
    els.opportunityCards.appendChild(empty);
    return;
  }
  if (!opportunities.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "没有匹配的股票状态卡。";
    els.opportunityCards.appendChild(empty);
    return;
  }

  opportunities.forEach((opportunity) => {
    const meta = OPPORTUNITY_STATUS_META[opportunity.status] || OPPORTUNITY_STATUS_META.watchlist;
    const card = document.createElement("article");
    card.className = `opportunity-card ${meta.className}`;

    const header = document.createElement("div");
    header.className = "opportunity-card-header";
    const titleWrap = document.createElement("div");
    const symbol = document.createElement("strong");
    symbol.textContent = opportunity.symbol || "代码待确认";
    const name = document.createElement("span");
    name.textContent = opportunity.name || "名称待确认";
    titleWrap.append(symbol, name);
    const status = document.createElement("span");
    status.className = "opportunity-status";
    status.textContent = opportunity.status_label || meta.label;
    header.append(titleWrap, status);

    const theme = document.createElement("p");
    theme.className = "opportunity-theme";
    theme.textContent = [opportunity.market, opportunity.theme, opportunity.segment].filter(Boolean).join(" · ") || "主题待确认";

    const action = document.createElement("p");
    action.className = "opportunity-action";
    action.textContent = opportunity.action || "动作待确认";

    const price = document.createElement("div");
    price.className = "opportunity-price";
    formatPriceMeta(opportunity).forEach((line) => {
      const item = document.createElement("span");
      item.textContent = line;
      price.appendChild(item);
    });

    const scores = document.createElement("div");
    scores.className = "opportunity-scores";
    const delta = opportunity.score_delta || {};
    [
      formatScoreMetric("机会分", opportunity.opportunity_score, delta.opportunity_score),
      formatScoreMetric("趋势确认", opportunity.trend_score, delta.trend_score),
      formatScoreMetric("拥挤度", opportunity.crowding_score, delta.crowding_score),
      formatScoreMetric("R/R", opportunity.rr_ratio, delta.rr_ratio, { rr: true }),
    ].forEach((line) => {
      const item = document.createElement("span");
      item.textContent = line;
      scores.appendChild(item);
    });

    const change = document.createElement("div");
    change.className = "opportunity-change";
    appendListBlock(change, "变化解释", opportunity.why_changed, hasScoreDelta(opportunity) ? "变化原因待确认" : "暂无历史变化");

    const conditions = document.createElement("div");
    conditions.className = "opportunity-conditions";
    appendListBlock(conditions, "买入条件", opportunity.buy_conditions, "待结构化");
    appendListBlock(conditions, "回避条件", opportunity.avoid_conditions, "待结构化");
    appendListBlock(conditions, "失效条件", opportunity.invalid_conditions, "待结构化");

    const source = document.createElement("small");
    source.className = "opportunity-source";
    source.textContent = opportunity.source === "structured" ? "来源：结构化 opportunities" : `来源：${opportunity.source_label || "报告正文保守解析"}`;

    card.append(header, theme, action, price, scores, change, conditions, source);
    els.opportunityCards.appendChild(card);
  });
}

function setActiveKind(kind) {
  state.kind = kind || "all";
  document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item.dataset.kind === state.kind));
}

function reportSearchText(report) {
  return [
    report.title,
    report.published_label,
    report.kind_label,
    report.summary,
    Array.isArray(report.symbols) ? report.symbols.join(" ") : "",
    Array.isArray(report.themes) ? report.themes.join(" ") : "",
    reportText(report),
  ].join(" ");
}

function filteredReports() {
  return state.reports.filter((report) => {
    if (state.kind !== "all" && report.kind !== state.kind) return false;
    if (!state.query) return true;
    return reportSearchText(report).toLowerCase().includes(state.query);
  });
}

async function selectReport(report) {
  if (!report) return;
  state.activeId = report.id;
  els.reportKind.textContent = report.kind_label || report.kind || "报告";
  els.reportTitle.textContent = report.title || "未命名报告";
  els.reportDate.textContent = report.published_label || "";
  els.reportContent.innerHTML = `<p class="placeholder">正在加载报告正文…</p>`;
  els.reportContent.scrollTop = 0;
  renderList();
  try {
    const content = await loadReportContent(report);
    if (state.activeId !== report.id) return;
    els.reportContent.innerHTML = renderMarkdown(content);
  } catch (error) {
    if (state.activeId !== report.id) return;
    els.reportContent.innerHTML = `<p class="empty">报告正文加载失败：${escapeHtml(error.message)}。请刷新页面或稍后再试。</p>`;
  }
}

function renderList() {
  const reports = filteredReports();
  els.reportCount.textContent = String(reports.length);
  els.reportList.replaceChildren();
  if (!reports.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "没有匹配的报告。"; els.reportList.appendChild(empty); return;
  }
  reports.forEach((report) => {
    const button = document.createElement("button"); button.type = "button"; button.className = `report-item ${report.id === state.activeId ? "active" : ""}`;
    const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = report.kind_label;
    const title = document.createElement("strong"); title.textContent = report.title;
    const date = document.createElement("time"); date.textContent = report.published_label;
    button.append(badge, title, date); button.addEventListener("click", () => selectReport(report)); els.reportList.appendChild(button);
  });
  if (!reports.some((report) => report.id === state.activeId)) selectReport(reports[0]);
}

async function loadArchive() {
  state.archive = await loadArchiveIndex();
  state.reports = Array.isArray(state.archive) ? state.archive : (Array.isArray(state.archive.reports) ? state.archive.reports : []);
  els.generatedAt.textContent = `更新 ${state.archive.generated_label || "--"}`;
  await preloadFallbackReportContent();
  state.opportunities = deriveOpportunities();
  renderDecisionDashboard();
  renderGapBreakdown();
  renderDataHealth();
  renderReviewStats();
  renderOpportunityCards();
  renderList();
  if (state.reports[0]) selectReport(state.reports[0]);
}

els.filters.addEventListener("click", (event) => {
  const button = event.target.closest("[data-kind]"); if (!button) return;
  setActiveKind(button.dataset.kind); renderList();
});

els.searchInput.addEventListener("input", () => { state.query = els.searchInput.value.trim().toLowerCase(); renderList(); });

if (els.opportunityStatusFilters) {
  els.opportunityStatusFilters.addEventListener("click", (event) => {
    const button = event.target.closest("[data-status]");
    if (!button) return;
    state.opportunityStatus = button.dataset.status || "all";
    els.opportunityStatusFilters.querySelectorAll(".status-filter").forEach((item) => item.classList.toggle("active", item === button));
    renderOpportunityCards();
  });
}

if (els.opportunitySearch) {
  els.opportunitySearch.addEventListener("input", () => {
    state.opportunityQuery = els.opportunitySearch.value.trim();
    renderOpportunityCards();
  });
}

if (els.dashboardReports) {
  els.dashboardReports.addEventListener("click", (event) => {
    const card = event.target.closest("[data-report-id]");
    if (!card) return;
    const report = state.reports.find((item) => item.id === card.dataset.reportId);
    if (!report) return;
    setActiveKind(card.dataset.kind || report.kind);
    renderList();
    selectReport(report);
    document.querySelector(".research-layout")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

loadArchive().catch((error) => {
  els.reportTitle.textContent = "报告加载失败";
  els.reportContent.innerHTML = `<p class="empty">${escapeHtml(error.message)}。请稍后刷新页面。</p>`;
});
