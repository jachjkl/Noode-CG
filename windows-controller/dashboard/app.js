const $ = (selector) => document.querySelector(selector);

const elements = {
  connectionBadge: $("#connectionBadge"),
  startButton: $("#startButton"),
  continueButton: $("#continueButton"),
  stopButton: $("#stopButton"),
  closeButton: $("#closeButton"),
  statusValue: $("#statusValue"),
  statusDetail: $("#statusDetail"),
  runLink: $("#runLink"),
  roundValue: $("#roundValue"),
  nodeCount: $("#nodeCount"),
  resultUpdated: $("#resultUpdated"),
  elapsedValue: $("#elapsedValue"),
  processValue: $("#processValue"),
  currentStage: $("#currentStage"),
  currentStep: $("#currentStep"),
  workflowProgress: $("#workflowProgress"),
  workflowProgressText: $("#workflowProgressText"),
  stageGrid: $("#stageGrid"),
  filterSummary: $("#filterSummary"),
  searchInput: $("#searchInput"),
  countryFilter: $("#countryFilter"),
  jpOnly: $("#jpOnly"),
  resetFilters: $("#resetFilters"),
  copyFiltered: $("#copyFiltered"),
  exportCsv: $("#exportCsv"),
  nodeRows: $("#nodeRows"),
  liveResultSummary: $("#liveResultSummary"),
  liveNodeRows: $("#liveNodeRows"),
  copyLiveResults: $("#copyLiveResults"),
  logOutput: $("#logOutput"),
  autoScroll: $("#autoScroll"),
  copyLog: $("#copyLog"),
  toast: $("#toast"),
  rulesForm: $("#rulesForm"),
  rulesStatus: $("#rulesStatus"),
  ruleTcp: $("#ruleTcp"),
  ruleTls: $("#ruleTls"),
  ruleHttp: $("#ruleHttp"),
  ruleAverage: $("#ruleAverage"),
  ruleJitter: $("#ruleJitter"),
  ruleLoss: $("#ruleLoss"),
  ruleSpeed: $("#ruleSpeed"),
  resetRules: $("#resetRules"),
  saveRules: $("#saveRules"),
  continuousRounds: $("#continuousRounds"),
};

let allNodes = [];
let filteredNodes = [];
let liveNodes = [];
let lastState = null;
let toastTimer = null;
let continueSubmitting = false;
let resultSource = "published-cache";
let sortState = { key: "rank", direction: "asc" };

const defaultRules = {
  tcp_max_ms: 200,
  tls_max_ms: 200,
  http_ttfb_max_ms: 200,
  average_max_ms: 200,
  jitter_max_ms: 200,
  loss_max_percent: 30,
  speed_min_mbps: 3,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 2400);
}

function formatElapsed(totalSeconds) {
  const value = Number(totalSeconds || 0);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = Math.floor(value % 60);
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatDate(value) {
  if (!value) return "等待同步结果";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `结果更新于 ${date.toLocaleString("zh-CN", { hour12: false })}`;
}

function statusLabel(status, conclusion) {
  if (status === "in_progress") return "正在执行";
  if (["queued", "waiting", "pending"].includes(status)) return "等待执行";
  if (conclusion === "success") return "已完成";
  if (conclusion === "cancelled") return "已取消";
  if (conclusion === "failure") return "失败";
  if (status === "completed") return "已结束";
  return "尚未开始";
}

function statusClass(status, conclusion) {
  if (status === "in_progress") return "running";
  if (conclusion === "success") return "success";
  if (["failure", "cancelled", "timed_out"].includes(conclusion)) return "failure";
  return "pending";
}

function renderStages(stages) {
  elements.stageGrid.innerHTML = stages.map((stage, index) => {
    const klass = statusClass(stage.status, stage.conclusion);
    const currentStep = (stage.steps || []).find((step) => step.status === "in_progress");
    const completed = (stage.steps || []).filter((step) => step.status === "completed").length;
    const detail = currentStep?.name || ((stage.steps || []).length ? `${completed}/${stage.steps.length} 个步骤` : "等待工作流数据");
    const cardClass = stage.status === "in_progress" ? "active" : stage.conclusion === "success" ? "done" : "";
    return `
      <article class="stage-card ${cardClass}">
        <div class="stage-top">
          <span class="stage-index">0${index + 1}</span>
          <span class="stage-state ${klass}">${escapeHtml(statusLabel(stage.status, stage.conclusion))}</span>
        </div>
        <h3>${escapeHtml(stage.label)}</h3>
        <p title="${escapeHtml(detail)}">${escapeHtml(detail)}</p>
      </article>`;
  }).join("");
}

function renderState(state) {
  lastState = state;
  const labels = {
    idle: "等待启动",
    running: "优选进行中",
    stopping: "正在停止优选",
    success: "本轮已完成",
    failure: "运行失败",
    stopped: "本地监控已停止",
  };
  const badgeClass = ["running", "stopping"].includes(state.status) ? "running" : state.status === "success" ? "success" : state.status === "failure" ? "failure" : "neutral";
  elements.connectionBadge.className = `badge ${badgeClass}`;
  elements.connectionBadge.textContent = labels[state.status] || "已连接";
  elements.statusValue.textContent = labels[state.status] || state.status;
  elements.statusDetail.textContent = state.last_error || (state.gh_status ? `GitHub 状态：${statusLabel(state.gh_status, state.gh_conclusion)}` : "本地服务已连接");
  elements.elapsedValue.textContent = formatElapsed(state.elapsed_seconds);
  elements.processValue.textContent = state.pid ? `本地监控 PID ${state.pid}` : state.exit_code === 0 ? "本地监控已正常退出" : "本地监控进程未运行";
  elements.startButton.disabled = Boolean(state.running);
  elements.startButton.textContent = state.running ? "任务运行中" : "开始新一轮";
  if (!continueSubmitting) {
    elements.continueButton.disabled = !state.cycle_started;
    elements.continueButton.textContent = !state.cycle_started
      ? "请先开始新一轮"
      : state.continuation_queued
        ? "续选已排队"
        : state.running ? "排队继续优选" : "继续优选并重排";
  }
  elements.stopButton.disabled = Boolean(state.stop_requested && !state.running);
  elements.stopButton.textContent = state.status === "stopping" ? "已请求停止" : "停止优选";

  if (state.run_id) {
    elements.runLink.textContent = `#${state.run_id}`;
    elements.runLink.href = state.run_url;
  } else {
    elements.runLink.textContent = "尚未获取";
    elements.runLink.removeAttribute("href");
  }

  const progress = state.workflow_progress || {};
  elements.roundValue.textContent = `本次面板已跟踪 ${progress.round || 0} 轮`;
  elements.currentStage.textContent = progress.current_stage || (state.gh_status === "queued" ? "工作流排队中" : "等待工作流");
  elements.currentStep.textContent = progress.current_step || (state.gh_conclusion === "success" ? "本轮全部步骤已完成" : progress.current_stage ? "正在等待该阶段开始执行" : "尚未进入执行步骤");
  const total = Number(progress.total_steps || 0);
  const completed = Number(progress.completed_steps || 0);
  const progressPercent = total ? Math.min(state.running && completed >= total ? 96 : 100, (completed / total) * 100) : 0;
  elements.workflowProgress.style.width = `${progressPercent}%`;
  elements.workflowProgressText.textContent = `已完成 ${completed} / ${total} 个步骤`;
  renderStages(state.stages || []);

  const logChanged = elements.logOutput.textContent !== state.log;
  if (logChanged) {
    elements.logOutput.textContent = state.log || "等待控制器写入日志……";
    if (elements.autoScroll.checked) elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
  }
  elements.resultUpdated.textContent = formatDate(state.result_updated_at);
}

function numberClass(value, good, medium) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  if (number <= good) return "good";
  if (number <= medium) return "medium";
  return "bad";
}

function formatMetric(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "--";
}

function currentCountry(node) {
  return String(node.colo_country || node.country || "未知").toUpperCase();
}

function applyFilters() {
  const query = elements.searchInput.value.trim().toLowerCase();
  const country = elements.countryFilter.value;
  const jpOnly = elements.jpOnly.checked;

  filteredNodes = allNodes.filter((node) => {
    const nodeCountry = currentCountry(node);
    const searchable = [node.ip, node.ip_port, nodeCountry, node.city, node.region, node.colo].join(" ").toLowerCase();
    return (!query || searchable.includes(query))
      && (!country || nodeCountry === country)
      && (!jpOnly || nodeCountry === "JP");
  });

  const valueForSort = (node) => {
    const value = sortState.key === "rank" ? node._rank : node[sortState.key];
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  filteredNodes.sort((a, b) => {
    const left = valueForSort(a);
    const right = valueForSort(b);
    if (left === null && right === null) return Number(a._rank) - Number(b._rank);
    if (left === null) return 1;
    if (right === null) return -1;
    const compared = left - right;
    return compared === 0
      ? Number(a._rank) - Number(b._rank)
      : compared * (sortState.direction === "asc" ? 1 : -1);
  });
  document.querySelectorAll(".sort-header").forEach((button) => {
    const active = button.dataset.sort === sortState.key;
    button.classList.toggle("active", active);
    button.classList.toggle("asc", active && sortState.direction === "asc");
    button.classList.toggle("desc", active && sortState.direction === "desc");
    button.setAttribute("aria-sort", active ? (sortState.direction === "asc" ? "ascending" : "descending") : "none");
  });
  renderNodes();
}

function nodeRowsHtml(nodes) {
  return nodes.map((node) => {
    const country = currentCountry(node);
    const location = [node.city, node.region].filter(Boolean).join(" · ") || "未知位置";
    const tcp = formatMetric(node.tcp_latency_ms);
    const tls = formatMetric(node.tls_latency_ms);
    const http = formatMetric(node.http_latency_ms);
    const average = formatMetric(node.average_latency_ms);
    const jitter = formatMetric(node.jitter_ms);
    const loss = Number.isFinite(Number(node.loss_rate)) ? `${(Number(node.loss_rate) * 100).toFixed(1)}%` : "--";
    const speed = formatMetric(node.speed_mbps);
    const megaBytes = Number.isFinite(Number(node.speed_mbps)) ? (Number(node.speed_mbps) / 8).toFixed(1) : "--";
    const endpoint = node.ip_port || `${node.ip}:${node.port || 443}`;
    return `
      <tr>
        <td class="rank">${node._rank}</td>
        <td><span class="ip-value">${escapeHtml(endpoint)}</span></td>
        <td><span class="location-main">${escapeHtml(country)} · ${escapeHtml(node.city || "未知城市")}</span><span class="location-sub">${escapeHtml(location)}</span></td>
        <td>${escapeHtml(node.colo || "--")}</td>
        <td class="number ${numberClass(node.tcp_latency_ms, 150, 300)}">${tcp} ms</td>
        <td class="number ${numberClass(node.tls_latency_ms, 200, 300)}">${tls} ms</td>
        <td class="number ${numberClass(node.http_latency_ms, 200, 300)}">${http} ms</td>
        <td class="number ${numberClass(node.average_latency_ms, 180, 300)}">${average} ms</td>
        <td class="number ${numberClass(node.jitter_ms, 50, 200)}">${jitter} ms</td>
        <td class="number ${numberClass(Number(node.loss_rate) * 100, 0, 10)}">${loss}</td>
        <td><span class="speed">${speed} Mbps</span><span class="location-sub">${megaBytes} MB/s</span></td>
        <td><button class="button secondary small copy-one" data-value="${escapeHtml(endpoint)}" type="button">复制</button></td>
      </tr>`;
  }).join("");
}

function renderLiveNodes() {
  elements.copyLiveResults.disabled = liveNodes.length === 0;
  elements.liveResultSummary.textContent = liveNodes.length
    ? `本轮已实时优选 ${liveNodes.length} 条（连续模式以 300 个普通节点为目标，第三轮结束后发布已有合格结果；单轮模式完成即替换；日本节点另行附加）`
    : "本轮尚无合格结果；测速通过一个，这里立即增加一个";
  elements.liveNodeRows.innerHTML = liveNodes.length
    ? nodeRowsHtml(liveNodes)
    : '<tr><td class="empty" colspan="12">等待本轮实时优选结果</td></tr>';
}

function renderNodes() {
  const sourceLabels = {
    "local-ready": "本机保存的云端已发布结果",
    "published-cache": "线上已发布结果",
  };
  elements.nodeCount.textContent = String(allNodes.length);
  elements.filterSummary.textContent = `${sourceLabels[resultSource] || "当前结果"} · 共 ${allNodes.length} 条，当前显示 ${filteredNodes.length} 条`;
  elements.copyFiltered.disabled = filteredNodes.length === 0;
  elements.exportCsv.disabled = filteredNodes.length === 0;
  if (!filteredNodes.length) {
    elements.nodeRows.innerHTML = '<tr><td class="empty" colspan="12">没有符合当前筛选条件的 IP</td></tr>';
    return;
  }
  elements.nodeRows.innerHTML = nodeRowsHtml(filteredNodes);
}

function updateCountries() {
  const selected = elements.countryFilter.value;
  const countries = [...new Set(allNodes.map(currentCountry))].sort();
  elements.countryFilter.innerHTML = '<option value="">全部国家</option>'
    + countries.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  elements.countryFilter.value = countries.includes(selected) ? selected : "";
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
    showToast(successMessage);
  } catch {
    const area = document.createElement("textarea");
    area.value = value;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    showToast(successMessage);
  }
}

function csvCell(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

function exportCsv() {
  const headers = ["rank", "ip_port", "country", "city", "colo", "tcp_ms", "tls_ms", "ttfb_ms", "average_ms", "jitter_ms", "loss_rate", "speed_mbps"];
  const lines = [headers.join(",")];
  for (const node of filteredNodes) {
    lines.push([
      node._rank,
      node.ip_port || `${node.ip}:${node.port || 443}`,
      currentCountry(node),
      node.city,
      node.colo,
      node.tcp_latency_ms,
      node.tls_latency_ms,
      node.http_latency_ms,
      node.average_latency_ms,
      node.jitter_ms,
      node.loss_rate,
      node.speed_mbps,
    ].map(csvCell).join(","));
  }
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `Noode-CG-filtered-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast(`已导出 ${filteredNodes.length} 条结果`);
}

async function fetchState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderState(await response.json());
  } catch (error) {
    elements.connectionBadge.className = "badge failure";
    elements.connectionBadge.textContent = "连接中断";
    elements.statusDetail.textContent = `本地面板连接失败：${error.message}`;
  }
}

async function fetchNodes() {
  try {
    const response = await fetch("/api/nodes", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    resultSource = data.source || "published-cache";
    allNodes = (data.nodes || []).map((node, index) => ({ ...node, _rank: index + 1 }));
    updateCountries();
    applyFilters();
  } catch (error) {
    elements.filterSummary.textContent = `结果读取失败：${error.message}`;
  }
}

async function fetchLiveNodes() {
  try {
    const response = await fetch("/api/live-nodes", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    liveNodes = (data.nodes || []).map((node, index) => ({ ...node, _rank: index + 1 }));
    renderLiveNodes();
  } catch (error) {
    elements.liveResultSummary.textContent = `实时结果读取失败：${error.message}`;
  }
}

function renderRules(rules) {
  const values = { ...defaultRules, ...(rules || {}) };
  elements.ruleTcp.value = values.tcp_max_ms;
  elements.ruleTls.value = values.tls_max_ms;
  elements.ruleHttp.value = values.http_ttfb_max_ms;
  elements.ruleAverage.value = values.average_max_ms;
  elements.ruleJitter.value = values.jitter_max_ms;
  elements.ruleLoss.value = values.loss_max_percent;
  elements.ruleSpeed.value = values.speed_min_mbps;
}

function collectRules() {
  return {
    tcp_max_ms: Number(elements.ruleTcp.value),
    tls_max_ms: Number(elements.ruleTls.value),
    http_ttfb_max_ms: Number(elements.ruleHttp.value),
    average_max_ms: Number(elements.ruleAverage.value),
    jitter_max_ms: Number(elements.ruleJitter.value),
    loss_max_percent: Number(elements.ruleLoss.value),
    speed_min_mbps: Number(elements.ruleSpeed.value),
  };
}

async function fetchRules() {
  try {
    const response = await fetch("/api/rules", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderRules(data.ordinary);
    elements.rulesStatus.className = "badge success";
    elements.rulesStatus.textContent = "本地规则已载入 · JP 豁免";
  } catch (error) {
    elements.rulesStatus.className = "badge failure";
    elements.rulesStatus.textContent = "规则读取失败";
  }
}

async function fetchOptions() {
  try {
    const response = await fetch("/api/options", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    elements.continuousRounds.checked = Boolean(data.selection?.continuous_three_rounds);
  } catch (error) {
    showToast(`连续筛选选项读取失败：${error.message}`);
  }
}

async function post(path, body = undefined) {
  const options = { method: "POST" };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

elements.startButton.addEventListener("click", async () => {
  try {
    const result = await post("/api/start");
    showToast(result.started ? "已启动新一轮优选" : "已有优选任务正在运行");
    await fetchState();
  } catch (error) {
    showToast(`启动失败：${error.message}`);
  }
});

elements.continueButton.addEventListener("click", async () => {
  try {
    continueSubmitting = true;
    elements.continueButton.disabled = true;
    elements.continueButton.textContent = "正在提交或排队";
    const result = await post("/api/continue");
    if (result.started) {
      const retest = Number(result.previous_top100_retest_count || 0);
      showToast(`已保留 ${result.existing_count} 条实时合格结果，请求新的10000个官方候选；TOP${retest} 不重复测试`);
      elements.continueButton.textContent = "续选已启动";
    } else if (result.queued) {
      showToast(result.reason || "已排队，当前流程结束后自动续选");
      elements.continueButton.textContent = "续选已排队";
    } else {
      showToast(result.reason || "续选未启动");
      elements.continueButton.textContent = "继续优选并重排";
    }
    continueSubmitting = false;
    elements.continueButton.disabled = !lastState?.cycle_started;
    await fetchState();
  } catch (error) {
    continueSubmitting = false;
    elements.continueButton.textContent = "继续优选并重排";
    elements.continueButton.disabled = !lastState?.cycle_started;
    showToast(`续选失败：${error.message}`);
  }
});

elements.stopButton.addEventListener("click", async () => {
  try {
    elements.stopButton.disabled = true;
    elements.stopButton.textContent = "正在停止";
    const result = await post("/api/stop-selection");
    showToast(result.workflow_active
      ? "本地优选已停止；当前云端轮次结束后不会继续自动补充"
      : "当前没有运行中的优选任务");
    await fetchState();
  } catch (error) {
    elements.stopButton.disabled = false;
    elements.stopButton.textContent = "停止优选";
    showToast(`停止失败：${error.message}`);
  }
});

elements.rulesForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    elements.saveRules.disabled = true;
    elements.saveRules.textContent = "正在保存";
    const result = await post("/api/rules", { ordinary: collectRules() });
    renderRules(result.ordinary);
    elements.rulesStatus.className = "badge success";
    elements.rulesStatus.textContent = "已保存 · 下轮生效 · JP 豁免";
    showToast("自定义规则已保存到本机，下次本地优选自动使用");
  } catch (error) {
    elements.rulesStatus.className = "badge failure";
    elements.rulesStatus.textContent = "保存失败";
    showToast(`规则保存失败：${error.message}`);
  } finally {
    elements.saveRules.disabled = false;
    elements.saveRules.textContent = "保存到本地";
  }
});

elements.resetRules.addEventListener("click", () => {
  renderRules(defaultRules);
  elements.rulesStatus.className = "badge neutral";
  elements.rulesStatus.textContent = "默认值尚未保存";
});

elements.continuousRounds.addEventListener("change", async () => {
  try {
    const enabled = elements.continuousRounds.checked;
    await post("/api/options", { selection: { continuous_three_rounds: enabled } });
    showToast(enabled ? "已开启最多三轮连续筛选" : "已关闭连续筛选：每轮合格结果立即发布");
  } catch (error) {
    elements.continuousRounds.checked = !elements.continuousRounds.checked;
    showToast(`运行选项保存失败：${error.message}`);
  }
});

elements.closeButton.addEventListener("click", async () => {
  try {
    await post("/api/shutdown");
    showToast("本地面板正在关闭，GitHub Actions 不受影响");
    elements.closeButton.disabled = true;
  } catch (error) {
    showToast(`关闭失败：${error.message}`);
  }
});

for (const input of [elements.searchInput, elements.countryFilter, elements.jpOnly]) {
  input.addEventListener(input.tagName === "INPUT" && input.type === "search" ? "input" : "change", applyFilters);
}

elements.resetFilters.addEventListener("click", () => {
  elements.searchInput.value = "";
  elements.countryFilter.value = "";
  elements.jpOnly.checked = false;
  sortState = { key: "rank", direction: "asc" };
  applyFilters();
});

const nodeTableHead = document.querySelector("#nodeTable thead");
if (nodeTableHead) {
  nodeTableHead.addEventListener("click", (event) => {
    const button = event.target.closest(".sort-header");
    if (!button) return;
    const key = button.dataset.sort;
    if (sortState.key === key) {
      sortState.direction = sortState.direction === "asc" ? "desc" : "asc";
    } else {
      sortState = { key, direction: "asc" };
    }
    applyFilters();
  });
}

elements.copyFiltered.addEventListener("click", () => {
  const text = filteredNodes.map((node) => node.ip_port || `${node.ip}:${node.port || 443}`).join("\n");
  copyText(text, `已复制 ${filteredNodes.length} 个 IP`);
});
elements.copyLiveResults.addEventListener("click", () => {
  const text = liveNodes.map((node) => node.ip_port || `${node.ip}:${node.port || 443}`).join("\n");
  copyText(text, `已复制本轮 ${liveNodes.length} 个 IP`);
});

elements.exportCsv.addEventListener("click", exportCsv);
elements.copyLog.addEventListener("click", () => copyText(elements.logOutput.textContent, "日志已复制"));
elements.nodeRows.addEventListener("click", (event) => {
  const button = event.target.closest(".copy-one");
  if (button) copyText(button.dataset.value, `已复制 ${button.dataset.value}`);
});
elements.liveNodeRows.addEventListener("click", (event) => {
  const button = event.target.closest(".copy-one");
  if (button) copyText(button.dataset.value, `已复制 ${button.dataset.value}`);
});

document.addEventListener("pointerdown", (event) => {
  const button = event.target.closest(".button");
  if (!button || button.disabled) return;
  const rect = button.getBoundingClientRect();
  const ripple = document.createElement("span");
  ripple.className = "ripple-ink";
  ripple.style.left = `${event.clientX - rect.left}px`;
  ripple.style.top = `${event.clientY - rect.top}px`;
  button.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
});

fetchState();
fetchNodes();
fetchLiveNodes();
fetchRules();
fetchOptions();
setInterval(fetchState, 2500);
setInterval(fetchNodes, 5000);
setInterval(fetchLiveNodes, 1000);
