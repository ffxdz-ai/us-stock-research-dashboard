const state = { archive: null, reports: [], activeId: "", kind: "all", query: "" };

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
};

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

function filteredReports() {
  return state.reports.filter((report) => {
    if (state.kind !== "all" && report.kind !== state.kind) return false;
    if (!state.query) return true;
    return [report.title, report.published_label, report.content].some((value) => String(value || "").toLowerCase().includes(state.query));
  });
}

function selectReport(report) {
  if (!report) return;
  state.activeId = report.id;
  els.reportKind.textContent = report.kind_label;
  els.reportTitle.textContent = report.title;
  els.reportDate.textContent = report.published_label;
  els.reportContent.innerHTML = renderMarkdown(report.content);
  els.reportContent.scrollTop = 0;
  renderList();
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
  const response = await fetch(`./data/reports.json?v=${Date.now()}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  state.archive = await response.json();
  state.reports = Array.isArray(state.archive.reports) ? state.archive.reports : [];
  els.generatedAt.textContent = `更新 ${state.archive.generated_label || "--"}`;
  renderList();
  if (state.reports[0]) selectReport(state.reports[0]);
}

els.filters.addEventListener("click", (event) => {
  const button = event.target.closest("[data-kind]"); if (!button) return;
  state.kind = button.dataset.kind; document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button)); renderList();
});

els.searchInput.addEventListener("input", () => { state.query = els.searchInput.value.trim().toLowerCase(); renderList(); });

loadArchive().catch((error) => {
  els.reportTitle.textContent = "报告加载失败";
  els.reportContent.innerHTML = `<p class="empty">${escapeHtml(error.message)}。请稍后刷新页面。</p>`;
});
