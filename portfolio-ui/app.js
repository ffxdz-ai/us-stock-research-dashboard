const state = {
  portfolio: null,
  dirty: false,
  companyInfoCache: {},
  companyInfoAttempts: new Set(),
  reports: [],
  activeReport: "",
};

const companyLookupTimers = new WeakMap();

const els = {
  fileStatus: document.querySelector("#fileStatus"),
  reportsButton: document.querySelector("#reportsButton"),
  saveButton: document.querySelector("#saveButton"),
  reloadButton: document.querySelector("#reloadButton"),
  refreshPricesButton: document.querySelector("#refreshPricesButton"),
  runButton: document.querySelector("#runButton"),
  cashUsd: document.querySelector("#cashUsd"),
  cashPct: document.querySelector("#cashPct"),
  principalValue: document.querySelector("#principalValue"),
  depositTotal: document.querySelector("#depositTotal"),
  withdrawalTotal: document.querySelector("#withdrawalTotal"),
  accountPnl: document.querySelector("#accountPnl"),
  accountReturnPct: document.querySelector("#accountReturnPct"),
  capitalFlowsList: document.querySelector("#capitalFlowsList"),
  addCapitalFlowButton: document.querySelector("#addCapitalFlowButton"),
  cashTargetPct: document.querySelector("#cashTargetPct"),
  maxSinglePositionPct: document.querySelector("#maxSinglePositionPct"),
  riskProfile: document.querySelector("#riskProfile"),
  assetValue: document.querySelector("#assetValue"),
  equityValue: document.querySelector("#equityValue"),
  unrealizedPnl: document.querySelector("#unrealizedPnl"),
  positionCount: document.querySelector("#positionCount"),
  holdingsBody: document.querySelector("#holdingsBody"),
  addHoldingButton: document.querySelector("#addHoldingButton"),
  asOfText: document.querySelector("#asOfText"),
  watchInput: document.querySelector("#watchInput"),
  addWatchButton: document.querySelector("#addWatchButton"),
  watchList: document.querySelector("#watchList"),
  watchCount: document.querySelector("#watchCount"),
  feishuStatus: document.querySelector("#feishuStatus"),
  reportStatus: document.querySelector("#reportStatus"),
  reportsSection: document.querySelector("#reportsSection"),
  refreshReportsButton: document.querySelector("#refreshReportsButton"),
  reportSearch: document.querySelector("#reportSearch"),
  reportList: document.querySelector("#reportList"),
  reportKind: document.querySelector("#reportKind"),
  reportTitle: document.querySelector("#reportTitle"),
  reportUpdated: document.querySelector("#reportUpdated"),
  reportContent: document.querySelector("#reportContent"),
  runOutput: document.querySelector("#runOutput"),
  toast: document.querySelector("#toast"),
};

function money(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

function pct(value) {
  const number = Number(value || 0);
  return `${number.toFixed(2)}%`;
}

function toNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function upperTicker(value) {
  return String(value || "").trim().toUpperCase();
}

function hasChinese(value) {
  return /[\u4e00-\u9fff]/.test(String(value || ""));
}

function shouldReplaceWithChinese(value, ticker) {
  const text = String(value || "").trim();
  return !text || text.toUpperCase() === ticker || !hasChinese(text);
}

function nowLocalLabel() {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).formatToParts(new Date());
  const map = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${map.year}-${map.month}-${map.day} ${map.hour}:${map.minute} ${map.timeZoneName || "Asia/Shanghai"}`;
}

function shortPriceTime(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const match = text.match(/(\d{2}-\d{2})\s+(\d{2}:\d{2})\s+([A-Z]{2,4})$/);
  if (match) return `${match[1]} ${match[2]} ${match[3]}`;
  return text.replace(/^\d{4}-/, "");
}

function updatePriceMeta(element, holding) {
  const session = holding.last_price_session_label || "未刷新";
  const sourceSession = holding.last_price_source_session_label || "";
  const time = shortPriceTime(holding.last_price_time);
  const fallback = Boolean(holding.last_price_is_fallback);
  const note = String(holding.last_price_note || "").trim();

  element.replaceChildren();
  const line = document.createElement("div");
  line.className = `price-session ${fallback ? "fallback" : ""}`;
  line.textContent = [session, fallback && sourceSession && sourceSession !== session ? `${sourceSession}价兜底` : "", time]
    .filter(Boolean)
    .join(" · ");
  element.appendChild(line);

  if (note) {
    const noteLine = document.createElement("div");
    noteLine.className = "price-note";
    noteLine.textContent = note.length > 34 ? `${note.slice(0, 34)}...` : note;
    noteLine.title = note;
    element.appendChild(noteLine);
  }
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("visible");
  }, 2600);
}

function positionValue(holding) {
  return toNumber(holding.shares) * toNumber(holding.current_price_snapshot);
}

function positionCost(holding) {
  return toNumber(holding.shares) * toNumber(holding.cost_basis);
}

function syncHoldingComputed(holding) {
  holding.market_value_snapshot = Number(positionValue(holding).toFixed(3));
  holding.unrealized_pnl_snapshot = Number((positionValue(holding) - positionCost(holding)).toFixed(3));
}

function paintComputedCells(holding, valueCell, pnlCell) {
  const pnl = positionValue(holding) - positionCost(holding);
  valueCell.textContent = money(positionValue(holding));
  pnlCell.className = `computed ${pnl >= 0 ? "positive" : "negative"}`;
  pnlCell.textContent = money(pnl);
}

function totals() {
  const holdings = state.portfolio?.holdings || [];
  const equity = holdings.reduce((sum, item) => sum + positionValue(item), 0);
  const cost = holdings.reduce((sum, item) => sum + positionCost(item), 0);
  const cash = toNumber(state.portfolio?.cash_usd);
  const assets = equity + cash;
  return {
    equity,
    cost,
    cash,
    assets,
    cashPct: assets > 0 ? (cash / assets) * 100 : 0,
    unrealized: equity - cost,
  };
}

function todayIsoDate() {
  return new Date().toISOString().slice(0, 10);
}

function normalizeFlowType(value) {
  return value === "withdrawal" ? "withdrawal" : "deposit";
}

function capitalFlowRows() {
  if (!state.portfolio) return [];
  if (!Array.isArray(state.portfolio.capital_flows)) {
    state.portfolio.capital_flows = [];
  }
  return state.portfolio.capital_flows;
}

function sanitizeCapitalFlows(flows = capitalFlowRows()) {
  return flows
    .map((flow, index) => {
      const type = normalizeFlowType(flow.type);
      const amount = Math.abs(toNumber(flow.amount_usd ?? flow.amount));
      return {
        id: String(flow.id || `flow-${Date.now()}-${index}`),
        date: String(flow.date || todayIsoDate()).slice(0, 10),
        type,
        amount_usd: Number(amount.toFixed(2)),
        note: String(flow.note || "").trim(),
      };
    })
    .filter((flow) => flow.amount_usd > 0);
}

function computeCapitalTotals(flows, assets) {
  const deposit = flows
    .filter((flow) => normalizeFlowType(flow.type) === "deposit")
    .reduce((sum, flow) => sum + toNumber(flow.amount_usd), 0);
  const withdrawal = flows
    .filter((flow) => normalizeFlowType(flow.type) === "withdrawal")
    .reduce((sum, flow) => sum + toNumber(flow.amount_usd), 0);
  const principal = deposit - withdrawal;
  const accountPnl = assets - principal;
  return {
    deposit,
    withdrawal,
    principal,
    accountPnl,
    accountReturnPct: principal > 0 ? (accountPnl / principal) * 100 : 0,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function isTableSeparator(line) {
  const cells = tableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (line.trim().startsWith("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headers = tableCells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      html.push(`<div class="markdown-table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^[-*]\s+/, ""));
        index += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\d+\.\s+/, ""));
        index += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }
    if (line.startsWith(">")) {
      const quote = [];
      while (index < lines.length && lines[index].startsWith(">")) {
        quote.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${quote.map(inlineMarkdown).join("<br>")}</blockquote>`);
      continue;
    }
    if (/^-{3,}$/.test(line.trim())) {
      html.push("<hr>");
      index += 1;
      continue;
    }
    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{1,4})\s+|^```|^[-*]\s+|^\d+\.\s+|^>/.test(lines[index])) {
      if (lines[index].trim().startsWith("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) break;
      paragraph.push(lines[index].trim());
      index += 1;
    }
    html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
  }
  return html.join("\n");
}

function markDirty() {
  state.dirty = true;
  els.fileStatus.textContent = "config/portfolio.json · 未保存";
}

function updateTopLevelFromInputs() {
  if (!state.portfolio) return;
  state.portfolio.cash_usd = toNumber(els.cashUsd.value);
  state.portfolio.cash_target_pct = toNumber(els.cashTargetPct.value);
  state.portfolio.max_single_position_pct = toNumber(els.maxSinglePositionPct.value);
  state.portfolio.risk_profile = els.riskProfile.value;
}

function refreshSummary() {
  if (!state.portfolio) return;
  updateTopLevelFromInputs();
  const total = totals();
  const capital = computeCapitalTotals(sanitizeCapitalFlows(), total.assets);
  els.assetValue.textContent = money(total.assets);
  els.equityValue.textContent = money(total.equity);
  els.cashPct.textContent = pct(total.cashPct);
  els.unrealizedPnl.textContent = money(total.unrealized);
  els.unrealizedPnl.className = total.unrealized >= 0 ? "positive" : "negative";
  els.principalValue.textContent = money(capital.principal);
  els.depositTotal.textContent = money(capital.deposit);
  els.withdrawalTotal.textContent = money(capital.withdrawal);
  els.accountPnl.textContent = money(capital.accountPnl);
  els.accountPnl.className = capital.accountPnl >= 0 ? "positive" : "negative";
  els.accountReturnPct.textContent = pct(capital.accountReturnPct);
  els.accountReturnPct.className = capital.accountPnl >= 0 ? "positive" : "negative";
  els.positionCount.textContent = String(state.portfolio.holdings?.length || 0);
}

function inputCell(value, className, onInput, type = "text", step = "any") {
  const input = document.createElement("input");
  input.className = className;
  input.type = type;
  input.step = step;
  input.value = value ?? "";
  input.addEventListener("input", () => {
    onInput(input.value);
    markDirty();
    refreshSummary();
    renderWatchlist();
  });
  return input;
}

function textareaCell(value, className, onInput) {
  const textarea = document.createElement("textarea");
  textarea.className = className;
  textarea.value = value ?? "";
  textarea.addEventListener("input", () => {
    onInput(textarea.value);
    markDirty();
  });
  return textarea;
}

async function lookupCompanyInfo(ticker) {
  const symbol = upperTicker(ticker);
  if (!symbol) return null;
  if (state.companyInfoCache[symbol]) return state.companyInfoCache[symbol];

  const body = await api(`/api/company-info?ticker=${encodeURIComponent(symbol)}`);
  const company = body.company || null;
  if (company) {
    state.companyInfoCache[symbol] = company;
  }
  return company;
}

async function hydrateCompanyInfo(holding, controls = {}) {
  const ticker = upperTicker(holding.ticker);
  if (!ticker) return;

  controls.tickerInput?.classList.add("is-loading");
  try {
    const company = await lookupCompanyInfo(ticker);
    if (!company) return;

    let changed = false;
    if (company.name && shouldReplaceWithChinese(holding.name, ticker) && holding.name !== company.name) {
      holding.name = company.name;
      if (controls.nameInput) controls.nameInput.value = company.name;
      changed = true;
    }
    if (
      company.business_summary
      && shouldReplaceWithChinese(holding.business_summary, ticker)
      && holding.business_summary !== company.business_summary
    ) {
      holding.business_summary = company.business_summary;
      if (controls.businessInput) controls.businessInput.value = company.business_summary;
      changed = true;
    }
    holding.company_sector = company.sector || holding.company_sector || "";
    holding.company_industry = company.industry || holding.company_industry || "";
    holding.company_sector_en = company.sector_en || holding.company_sector_en || "";
    holding.company_industry_en = company.industry_en || holding.company_industry_en || "";
    holding.company_name_en = company.name_en || holding.company_name_en || "";
    holding.business_summary_en = company.business_summary_en || holding.business_summary_en || "";
    holding.company_website = company.website || holding.company_website || "";
    holding.company_profile_source = company.source || holding.company_profile_source || "";
    holding.company_profile_time = company.as_of_local || holding.company_profile_time || "";
    holding.company_profile_language = company.language || "zh-CN";

    if (changed) {
      markDirty();
      showToast(`${ticker} 公司资料已补全`);
    }
  } catch (error) {
    showToast(`${ticker} 公司资料获取失败`);
  } finally {
    controls.tickerInput?.classList.remove("is-loading");
  }
}

function needsChineseCompanyInfo(holding) {
  const ticker = upperTicker(holding.ticker);
  if (!ticker) return false;
  return shouldReplaceWithChinese(holding.name, ticker) || shouldReplaceWithChinese(holding.business_summary, ticker);
}

function scheduleCompanyInfoLookup(holding, controls) {
  window.clearTimeout(companyLookupTimers.get(holding));
  const ticker = upperTicker(holding.ticker);
  state.companyInfoAttempts.delete(ticker);
  if (ticker.length < 2) return;
  const timer = window.setTimeout(() => {
    hydrateCompanyInfo(holding, controls);
  }, 700);
  companyLookupTimers.set(holding, timer);
}

function renderHoldings() {
  const holdings = state.portfolio?.holdings || [];
  els.holdingsBody.replaceChildren();

  holdings.forEach((holding, index) => {
    const row = document.createElement("tr");
    row.className = "holding-row";
    const valueCell = document.createElement("td");
    const pnlCell = document.createElement("td");

    const tickerCell = document.createElement("td");
    const nameCell = document.createElement("td");
    const businessCell = document.createElement("td");
    const nameInput = inputCell(holding.name, "name-input", (value) => {
      holding.name = value.trim();
    });
    const businessInput = textareaCell(holding.business_summary || "", "business-input", (value) => {
      holding.business_summary = value.trim();
    });
    const tickerInput = inputCell(holding.ticker, "ticker-input", (value) => {
      holding.ticker = upperTicker(value);
    });
    const companyControls = { tickerInput, nameInput, businessInput };
    tickerInput.addEventListener("input", () => scheduleCompanyInfoLookup(holding, companyControls));
    tickerInput.addEventListener("blur", () => hydrateCompanyInfo(holding, companyControls));
    tickerInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      hydrateCompanyInfo(holding, companyControls);
    });
    tickerCell.appendChild(tickerInput);
    nameCell.appendChild(nameInput);
    businessCell.appendChild(businessInput);

    const sharesCell = document.createElement("td");
    sharesCell.appendChild(
      inputCell(
        holding.shares,
        "number-input",
        (value) => {
          holding.shares = toNumber(value);
          syncHoldingComputed(holding);
          paintComputedCells(holding, valueCell, pnlCell);
        },
        "number",
        "0.000001",
      ),
    );

    const costCell = document.createElement("td");
    costCell.appendChild(
      inputCell(
        holding.cost_basis,
        "number-input",
        (value) => {
          holding.cost_basis = toNumber(value);
          syncHoldingComputed(holding);
          paintComputedCells(holding, valueCell, pnlCell);
        },
        "number",
        "0.001",
      ),
    );

    const priceCell = document.createElement("td");
    const priceStack = document.createElement("div");
    priceStack.className = "price-stack";
    const priceMeta = document.createElement("div");
    priceMeta.className = "price-meta";
    const priceInput = inputCell(
      holding.current_price_snapshot,
      "number-input",
      (value) => {
        holding.current_price_snapshot = toNumber(value);
        holding.last_price_source = "手动输入";
        holding.last_price_time = nowLocalLabel();
        holding.last_price_session = "manual";
        holding.last_price_session_label = "手动";
        holding.last_price_source_session = "manual";
        holding.last_price_source_session_label = "手动";
        holding.last_price_is_fallback = false;
        holding.last_price_note = "";
        syncHoldingComputed(holding);
        paintComputedCells(holding, valueCell, pnlCell);
        updatePriceMeta(priceMeta, holding);
      },
      "number",
      "0.001",
    );
    priceStack.append(priceInput, priceMeta);
    priceCell.appendChild(priceStack);
    updatePriceMeta(priceMeta, holding);

    valueCell.className = "computed";

    paintComputedCells(holding, valueCell, pnlCell);

    const targetCell = document.createElement("td");
    targetCell.appendChild(
      inputCell(
        holding.target_weight_pct ?? "",
        "number-input",
        (value) => {
          holding.target_weight_pct = value === "" ? null : toNumber(value);
        },
        "number",
        "0.1",
      ),
    );

    const deleteCell = document.createElement("td");
    const deleteButton = document.createElement("button");
    deleteButton.className = "delete-button";
    deleteButton.type = "button";
    deleteButton.title = "删除持仓";
    deleteButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    deleteButton.addEventListener("click", () => {
      holdings.splice(index, 1);
      markDirty();
      renderAll();
    });
    deleteCell.appendChild(deleteButton);

    row.append(tickerCell, nameCell, businessCell, sharesCell, costCell, priceCell, valueCell, pnlCell, targetCell, deleteCell);
    els.holdingsBody.appendChild(row);

    const ticker = upperTicker(holding.ticker);
    if (ticker && needsChineseCompanyInfo(holding) && !state.companyInfoAttempts.has(ticker)) {
      state.companyInfoAttempts.add(ticker);
      window.setTimeout(() => hydrateCompanyInfo(holding, companyControls), 80 * (index + 1));
    }
  });
}

function renderWatchlist() {
  const watchlist = state.portfolio?.watchlist || [];
  els.watchList.replaceChildren();
  els.watchCount.textContent = `${watchlist.length} 个标的`;

  watchlist.forEach((ticker, index) => {
    const chip = document.createElement("span");
    chip.className = "watch-chip";
    chip.textContent = ticker;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "watch-remove";
    button.title = `移除 ${ticker}`;
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    button.addEventListener("click", () => {
      watchlist.splice(index, 1);
      markDirty();
      renderWatchlist();
    });
    chip.appendChild(button);
    els.watchList.appendChild(chip);
  });
}

function renderCapitalFlows() {
  const flows = capitalFlowRows();
  els.capitalFlowsList.replaceChildren();

  if (!flows.length) {
    const empty = document.createElement("div");
    empty.className = "capital-empty";
    empty.textContent = "暂无记录，点新增录入充值或提取。";
    els.capitalFlowsList.appendChild(empty);
    return;
  }

  flows.forEach((flow, index) => {
    flow.type = normalizeFlowType(flow.type);
    flow.date = String(flow.date || todayIsoDate()).slice(0, 10);
    flow.amount_usd = Math.abs(toNumber(flow.amount_usd ?? flow.amount));
    flow.note = String(flow.note || "");

    const row = document.createElement("div");
    row.className = "capital-flow-row";

    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.value = flow.date;
    dateInput.addEventListener("input", () => {
      flow.date = dateInput.value || todayIsoDate();
      markDirty();
      refreshSummary();
    });

    const typeSelect = document.createElement("select");
    [
      ["deposit", "充值"],
      ["withdrawal", "提取"],
    ].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      typeSelect.appendChild(option);
    });
    typeSelect.value = flow.type;
    typeSelect.addEventListener("change", () => {
      flow.type = normalizeFlowType(typeSelect.value);
      markDirty();
      refreshSummary();
    });

    const amountInput = document.createElement("input");
    amountInput.type = "number";
    amountInput.min = "0";
    amountInput.step = "0.01";
    amountInput.inputMode = "decimal";
    amountInput.value = flow.amount_usd || "";
    amountInput.placeholder = "金额";
    amountInput.addEventListener("input", () => {
      flow.amount_usd = Math.abs(toNumber(amountInput.value));
      markDirty();
      refreshSummary();
    });

    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.value = flow.note;
    noteInput.placeholder = "备注";
    noteInput.addEventListener("input", () => {
      flow.note = noteInput.value;
      markDirty();
    });

    const deleteButton = document.createElement("button");
    deleteButton.className = "delete-button";
    deleteButton.type = "button";
    deleteButton.title = "删除资金记录";
    deleteButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    deleteButton.addEventListener("click", () => {
      flows.splice(index, 1);
      markDirty();
      renderCapitalFlows();
      refreshSummary();
    });

    row.append(dateInput, typeSelect, amountInput, deleteButton, noteInput);
    els.capitalFlowsList.appendChild(row);
  });
}

function renderAll() {
  if (!state.portfolio) return;
  els.cashUsd.value = state.portfolio.cash_usd ?? 0;
  els.cashTargetPct.value = state.portfolio.cash_target_pct ?? 15;
  els.maxSinglePositionPct.value = state.portfolio.max_single_position_pct ?? 35;
  els.riskProfile.value = state.portfolio.risk_profile || "balanced";
  els.asOfText.textContent = state.portfolio.as_of_local || "--";
  renderHoldings();
  renderWatchlist();
  renderCapitalFlows();
  refreshSummary();
}

function cleanPortfolioForSave() {
  updateTopLevelFromInputs();
  const total = totals();
  const capitalFlows = sanitizeCapitalFlows();
  const capital = computeCapitalTotals(capitalFlows, total.assets);
  const holdings = (state.portfolio.holdings || [])
    .map((item) => {
      const ticker = upperTicker(item.ticker);
      const shares = toNumber(item.shares);
      const cost = toNumber(item.cost_basis);
      const price = toNumber(item.current_price_snapshot);
      const marketValue = shares * price;
      const unrealized = marketValue - shares * cost;
      return {
        ...item,
        ticker,
        name: String(item.name || ticker).trim(),
        business_summary: String(item.business_summary || "").trim(),
        company_sector: String(item.company_sector || "").trim(),
        company_industry: String(item.company_industry || "").trim(),
        company_sector_en: String(item.company_sector_en || "").trim(),
        company_industry_en: String(item.company_industry_en || "").trim(),
        company_name_en: String(item.company_name_en || "").trim(),
        business_summary_en: String(item.business_summary_en || "").trim(),
        company_website: String(item.company_website || "").trim(),
        company_profile_source: String(item.company_profile_source || "").trim(),
        company_profile_time: String(item.company_profile_time || "").trim(),
        company_profile_language: String(item.company_profile_language || "").trim(),
        shares,
        cost_basis: cost,
        current_price_snapshot: price,
        market_value_snapshot: Number(marketValue.toFixed(3)),
        unrealized_pnl_snapshot: Number(unrealized.toFixed(3)),
        target_weight_pct: item.target_weight_pct === null || item.target_weight_pct === "" ? null : toNumber(item.target_weight_pct),
      };
    })
    .filter((item) => item.ticker);

  const watchlist = Array.from(
    new Set([
      ...holdings.map((item) => item.ticker),
      ...(state.portfolio.watchlist || []).map(upperTicker),
    ]),
  ).filter(Boolean);

  return {
    ...state.portfolio,
    as_of_local: nowLocalLabel(),
    cash_usd: Number(total.cash.toFixed(2)),
    cash_pct: Number(total.cashPct.toFixed(2)),
    total_equity_market_value_usd: Number(total.equity.toFixed(2)),
    estimated_total_assets_usd: Number(total.assets.toFixed(2)),
    portfolio_unrealized_pnl_usd: Number(total.unrealized.toFixed(2)),
    capital_flows: capitalFlows,
    total_deposit_usd: Number(capital.deposit.toFixed(2)),
    total_withdrawal_usd: Number(capital.withdrawal.toFixed(2)),
    net_deposit_usd: Number(capital.principal.toFixed(2)),
    account_principal_usd: Number(capital.principal.toFixed(2)),
    account_profit_loss_usd: Number(capital.accountPnl.toFixed(2)),
    account_return_pct: Number(capital.accountReturnPct.toFixed(2)),
    holdings,
    watchlist,
  };
}

async function loadPortfolio() {
  const body = await api("/api/portfolio");
  state.portfolio = body.portfolio;
  state.dirty = false;
  state.companyInfoAttempts = new Set();
  els.fileStatus.textContent = "config/portfolio.json";
  renderAll();
}

async function loadStatus() {
  try {
    const body = await api("/api/status");
    els.feishuStatus.textContent = body.feishu_configured ? "已接通" : "未配置";
    els.reportStatus.textContent = body.latest_report ? `${body.latest_report} 已生成` : "暂无";
  } catch (error) {
    els.feishuStatus.textContent = "检查失败";
    els.reportStatus.textContent = "检查失败";
  }
}

function filteredReports() {
  const query = String(els.reportSearch.value || "").trim().toLowerCase();
  if (!query) return state.reports;
  return state.reports.filter((report) => [report.title, report.name, report.kind_label, report.updated_label]
    .some((value) => String(value || "").toLowerCase().includes(query)));
}

function renderReportList() {
  els.reportList.replaceChildren();
  const reports = filteredReports();
  if (!reports.length) {
    const empty = document.createElement("p");
    empty.className = "report-list-empty";
    empty.textContent = state.reports.length ? "没有匹配的报告。" : "reports目录中暂无报告。";
    els.reportList.appendChild(empty);
    return;
  }
  reports.forEach((report) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `report-list-item ${state.activeReport === report.name ? "active" : ""}`;
    const badge = document.createElement("span");
    badge.className = "report-kind-badge";
    badge.textContent = report.kind_label;
    const title = document.createElement("strong");
    title.textContent = report.title;
    const meta = document.createElement("span");
    meta.className = "report-list-meta";
    meta.textContent = `${report.updated_label}${report.is_latest ? " · 最新" : ""}`;
    button.append(badge, title, meta);
    button.addEventListener("click", () => loadReport(report.name).catch((error) => showToast(error.message)));
    els.reportList.appendChild(button);
  });
}

async function loadReport(name) {
  const body = await api(`/api/report?name=${encodeURIComponent(name)}`);
  const report = body.report;
  const metadata = state.reports.find((item) => item.name === name);
  state.activeReport = name;
  els.reportKind.textContent = report.kind_label || "分析报告";
  els.reportTitle.textContent = report.title || name;
  els.reportUpdated.textContent = metadata?.updated_label || "";
  els.reportContent.innerHTML = renderMarkdown(report.content);
  els.reportContent.scrollTop = 0;
  renderReportList();
}

async function loadReports(options = {}) {
  const body = await api("/api/reports");
  state.reports = Array.isArray(body.reports) ? body.reports : [];
  renderReportList();
  const preferred = state.reports.find((report) => report.name === state.activeReport)
    || state.reports.find((report) => report.name === "latest-public-equity-brief.md")
    || state.reports[0];
  if (preferred && (options.force || !state.activeReport)) {
    await loadReport(preferred.name);
  }
}

async function savePortfolio() {
  const portfolio = cleanPortfolioForSave();
  const body = await api("/api/portfolio", {
    method: "POST",
    body: JSON.stringify({ portfolio }),
  });
  state.portfolio = body.portfolio;
  state.dirty = false;
  els.fileStatus.textContent = "config/portfolio.json";
  renderAll();
  showToast("已保存配置");
}

async function refreshPrices(options = {}) {
  const silent = Boolean(options.silent);
  const holdings = state.portfolio?.holdings || [];
  const tickers = holdings.map((item) => upperTicker(item.ticker)).filter(Boolean);
  if (!tickers.length) {
    if (!silent) showToast("没有可刷新的持仓代码");
    return;
  }

  if (!silent) {
    els.refreshPricesButton.disabled = true;
    els.runOutput.textContent = "正在通过互联网刷新现价...";
  }

  const body = await api("/api/quotes", {
    method: "POST",
    body: JSON.stringify({ tickers }),
  });

  if (body.session === "overnight" && body.futu && !body.futu.connected) {
    if (!silent) {
      els.runOutput.textContent = [
        "当前是美股夜盘，但 Futu OpenD 未连接。",
        "为了避免用 Yahoo/Nasdaq 盘后兜底价覆盖券商夜盘价，本次没有改动持仓现价。",
        `纽约时间: ${body.as_of_new_york || "--"}`,
        `Futu OpenD: 未连接 ${body.futu.host || "127.0.0.1"}:${body.futu.port || "11111"}`,
        "请打开 Futu OpenD 后再点刷新，或继续使用当前已校准的券商截图价格。",
      ].join("\n");
      showToast("夜盘需连接 Futu OpenD");
      els.refreshPricesButton.disabled = false;
    }
    return;
  }

  let updated = 0;
  holdings.forEach((holding) => {
    const ticker = upperTicker(holding.ticker);
    const quote = body.quotes?.[ticker];
    if (!quote || typeof quote.price !== "number") return;
    holding.current_price_snapshot = quote.price;
    holding.last_price_source = quote.source;
    holding.last_price_time = quote.time || body.as_of_local;
    holding.last_price_session = quote.session || body.session || "";
    holding.last_price_session_label = quote.session_label || body.session_label || "";
    holding.last_price_source_session = quote.source_session || quote.session || "";
    holding.last_price_source_session_label = quote.source_session_label || quote.session_label || "";
    holding.last_price_is_fallback = Boolean(quote.is_fallback);
    holding.last_price_note = quote.fallback_reason || "";
    syncHoldingComputed(holding);
    updated += 1;
  });

  state.portfolio.as_of_local = body.as_of_local || nowLocalLabel();
  renderAll();
  markDirty();
  if (!silent) {
    const fallbackCount = holdings.filter((holding) => Boolean(holding.last_price_is_fallback)).length;
    const futuLine = body.futu?.connected
      ? `Futu OpenD: 已连接 ${body.futu.host}:${body.futu.port}`
      : `Futu OpenD: 未连接，无法对齐券商夜盘价；当前为公网行情回退`;
    els.runOutput.textContent = [
      `已按美股${body.session_label || "当前时段"}刷新 ${updated}/${tickers.length} 个持仓现价。`,
      `纽约时间: ${body.as_of_new_york || "--"}`,
      futuLine,
      `来源: ${body.source || "互联网行情源"}`,
      fallbackCount ? `兜底: ${fallbackCount} 个标的使用最新可用时段价格。` : "兜底: 无",
    ].join("\n");
    showToast("现价已刷新，记得保存");
    els.refreshPricesButton.disabled = false;
  }
}

function addHolding() {
  state.portfolio.holdings.push({
    ticker: "",
    name: "",
    shares: 0,
    cost_basis: 0,
    current_price_snapshot: 0,
    market_value_snapshot: 0,
    unrealized_pnl_snapshot: 0,
    target_weight_pct: null,
    business_summary: "",
    company_profile_language: "zh-CN",
    last_price_source: "",
    last_price_time: "",
    last_price_session: "",
    last_price_session_label: "",
    last_price_source_session: "",
    last_price_source_session_label: "",
    last_price_is_fallback: false,
    last_price_note: "",
  });
  markDirty();
  renderAll();
}

function addCapitalFlow() {
  capitalFlowRows().push({
    id: `flow-${Date.now()}`,
    date: todayIsoDate(),
    type: "deposit",
    amount_usd: 0,
    note: "",
  });
  markDirty();
  renderCapitalFlows();
  refreshSummary();
}

function addWatch() {
  const ticker = upperTicker(els.watchInput.value);
  if (!ticker) return;
  const watchlist = state.portfolio.watchlist || [];
  if (!watchlist.includes(ticker)) {
    watchlist.push(ticker);
    state.portfolio.watchlist = watchlist;
    markDirty();
    renderWatchlist();
  }
  els.watchInput.value = "";
}

async function runReport() {
  els.runButton.disabled = true;
  els.runOutput.classList.remove("success", "error");
  els.runOutput.textContent = "正在刷新现价、保存配置、生成报告并发送飞书...\n这一步通常需要几十秒，请稍等。";
  try {
    await refreshPrices({ silent: true });
    await savePortfolio();
    const body = await api("/api/run-report", { method: "POST", body: "{}" });
    const output = body.output ? `\n\n脚本输出:\n${body.output}` : "";
    els.runOutput.classList.add("success");
    els.runOutput.textContent = [
      "报告已生成并发送到飞书。",
      `发送时间: ${body.sent_at || nowLocalLabel()}`,
      `报告文件: ${body.report_path || "reports/latest-market-brief.md"}`,
    ].join("\n") + output;
    await loadStatus();
    showToast("报告已发送飞书");
  } catch (error) {
    els.runOutput.classList.add("error");
    els.runOutput.textContent = error.message;
    showToast("运行失败");
  } finally {
    els.runButton.disabled = false;
  }
}

["input", "change"].forEach((eventName) => {
  [els.cashUsd, els.cashTargetPct, els.maxSinglePositionPct, els.riskProfile].forEach((input) => {
    input.addEventListener(eventName, () => {
      markDirty();
      refreshSummary();
    });
  });
});

els.saveButton.addEventListener("click", () => savePortfolio().catch((error) => showToast(error.message)));
els.reloadButton.addEventListener("click", () => loadPortfolio().then(() => showToast("已重新读取")).catch((error) => showToast(error.message)));
els.refreshPricesButton.addEventListener("click", () => refreshPrices().catch((error) => {
  els.refreshPricesButton.disabled = false;
  els.runOutput.textContent = error.message;
  showToast("刷新失败");
}));
els.addHoldingButton.addEventListener("click", addHolding);
els.addCapitalFlowButton.addEventListener("click", addCapitalFlow);
els.addWatchButton.addEventListener("click", addWatch);
els.watchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") addWatch();
});
els.runButton.addEventListener("click", runReport);
els.reportsButton.addEventListener("click", () => els.reportsSection.scrollIntoView({ behavior: "smooth", block: "start" }));
els.refreshReportsButton.addEventListener("click", () => {
  els.refreshReportsButton.disabled = true;
  loadReports({ force: true })
    .then(() => showToast("报告目录已刷新"))
    .catch((error) => showToast(error.message))
    .finally(() => { els.refreshReportsButton.disabled = false; });
});
els.reportSearch.addEventListener("input", renderReportList);

window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

Promise.all([loadPortfolio(), loadStatus(), loadReports({ force: true })]).catch((error) => {
  els.runOutput.textContent = error.message;
  showToast("读取失败");
});
