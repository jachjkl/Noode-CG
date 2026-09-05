const $ = (selector) => document.querySelector(selector);

const elements = {
  cloudConnectionBadge: $("#cloudConnectionBadge"),
  connectionBadge: $("#connectionBadge"),
  startButton: $("#startButton"),
  continueButton: $("#continueButton"),
  publishButton: $("#publishButton"),
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
  workflowPanel: $("#workflowPanel"),
  workflowCompletion: $("#workflowCompletion"),
  workflowCompletionLabel: $("#workflowCompletionLabel"),
  workflowCompletionDetail: $("#workflowCompletionDetail"),
  workflowProgress: $("#workflowProgress"),
  workflowProgressText: $("#workflowProgressText"),
  stageGrid: $("#stageGrid"),
  liveTestSummary: $("#liveTestSummary"),
  liveTestStage: $("#liveTestStage"),
  liveTestProgress: $("#liveTestProgress"),
  liveTestProgressText: $("#liveTestProgressText"),
  liveTestRows: $("#liveTestRows"),
  liveTestPageInfo: $("#liveTestPageInfo"),
  liveTestPageSize: $("#liveTestPageSize"),
  liveTestPrevious: $("#liveTestPrevious"),
  liveTestNext: $("#liveTestNext"),
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
  competitionResultSummary: $("#competitionResultSummary"),
  competitionNodeRows: $("#competitionNodeRows"),
  copyCompetitionResults: $("#copyCompetitionResults"),
  logOutput: $("#logOutput"),
  autoScroll: $("#autoScroll"),
  copyLog: $("#copyLog"),
  toast: $("#toast"),
  rulesForm: $("#rulesForm"),
  rulesStatus: $("#rulesStatus"),
  ruleLatencyProbe: $("#ruleLatencyProbe"),
  ruleLatencyMax: $("#ruleLatencyMax"),
  ruleJitter: $("#ruleJitter"),
  ruleLoss: $("#ruleLoss"),
  ruleSpeed: $("#ruleSpeed"),
  resetRules: $("#resetRules"),
  saveRules: $("#saveRules"),
};

let allNodes = [];
let filteredNodes = [];
let liveNodes = [];
let competitionNodes = [];
let competitionReport = {};
let liveTestRecords = [];
let liveTestPage = 1;
let liveTestPageSize = 200;
let liveTestRecordCount = 0;
let lastState = null;
let toastTimer = null;
let continueSubmitting = false;
let resultSource = "published-cache";
let sortState = { key: "rank", direction: "asc" };

const defaultRules = {
  latency_probe: "tcp",
  tcp_enabled: true,
  tls_enabled: false,
  http_enabled: false,
  latency_max_ms: 200,
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
  const signature = JSON.stringify(stages);
  if (elements.stageGrid.dataset.signature === signature) return;
  elements.stageGrid.dataset.signature = signature;
  elements.stageGrid.innerHTML = stages.map((stage, index) => {
    const klass = statusClass(stage.status, stage.conclusion);
    const currentStep = (stage.steps || []).find((step) => step.status === "in_progress");
    const completed = (stage.steps || []).filter((step) => step.status === "completed").length;
    const firstStep = (stage.steps || [])[0];
    const detail = currentStep?.name
      || (stage.name === "pre-publish-test" && firstStep?.name)
      || ((stage.steps || []).length ? `${completed}/${stage.steps.length} 个步骤` : "等待工作流数据");
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
  elements.stageGrid.querySelectorAll(".done").forEach(addCardAtmosphere);
}

function renderRoundCompletion(state) {
  const completion = state.round_completion || {
    status: "idle",
    label: "等待本轮开始",
    detail: "完成复测与推送后会保持显示直到下一轮",
  };
  const allowed = ["idle", "running", "testing", "publishing", "completed", "failure"];
  const status = allowed.includes(completion.status) ? completion.status : "idle";
  elements.workflowCompletion.className = `round-completion ${status}`;
  elements.workflowCompletionLabel.textContent = completion.label || "等待本轮开始";
  elements.workflowCompletionDetail.textContent = completion.detail || "";
  elements.workflowPanel.classList.toggle("round-complete", status === "completed");
  elements.workflowPanel.classList.toggle("round-active", ["running", "testing", "publishing"].includes(status));
}

function renderState(state) {
  lastState = state;
  const labels = {
    idle: "等待启动",
    running: "优选进行中",
    stopping: "正在停止优选",
    success: "本轮已完成",
    failure: "运行失败",
    stopped: "本轮优选已停止",
  };
  const badgeClass = ["running", "stopping"].includes(state.status) ? "running" : state.status === "success" ? "success" : state.status === "failure" ? "failure" : "neutral";
  elements.connectionBadge.className = `badge ${badgeClass}`;
  elements.connectionBadge.textContent = labels[state.status] || "已连接";
  const cloud = state.cloud_connection || {};
  const cloudLabels = {
    checking: "云端检测中",
    connected: "云端已连接",
    unauthenticated: "云端未登录",
    offline: "云端不可达",
    cli_missing: "缺少 GitHub CLI",
  };
  const cloudClass = cloud.status === "connected"
    ? "success"
    : cloud.status === "checking" ? "neutral" : "failure";
  elements.cloudConnectionBadge.className = `badge ${cloudClass}`;
  elements.cloudConnectionBadge.textContent = cloudLabels[cloud.status] || "云端未知";
  elements.cloudConnectionBadge.title = cloud.detail || "尚未完成云端连接检查";
  elements.statusValue.textContent = labels[state.status] || state.status;
  elements.statusDetail.textContent = state.last_error || (state.gh_status ? `GitHub 状态：${statusLabel(state.gh_status, state.gh_conclusion)}` : "本地服务已连接");
  elements.elapsedValue.textContent = formatElapsed(state.elapsed_seconds);
  elements.processValue.textContent = state.pid ? `本地监控 PID ${state.pid}` : state.exit_code === 0 ? "本地监控已正常退出" : "本地监控进程未运行";
  elements.startButton.disabled = Boolean(state.running || state.cycle_started);
  elements.startButton.textContent = state.running
    ? "任务运行中"
    : state.cycle_started ? "本会话已开始" : "开始新一轮";
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
  elements.publishButton.disabled = !state.cycle_started;
  elements.publishButton.textContent = state.publish_queued
    ? "推送已排队"
    : state.running ? "排队手动推送" : "手动推送";

  if (state.run_id) {
    elements.runLink.textContent = `#${state.run_id}`;
    elements.runLink.href = state.run_url;
  } else {
    elements.runLink.textContent = "尚未获取";
    elements.runLink.removeAttribute("href");
  }

  const progress = state.workflow_progress || {};
  elements.roundValue.textContent = `本会话已获取 ${progress.round || 0} 轮云端候选`;
  elements.currentStage.textContent = progress.current_stage || (state.gh_status === "queued" ? "工作流排队中" : "等待工作流");
  elements.currentStep.textContent = progress.current_step || (state.gh_conclusion === "success" ? "本轮全部步骤已完成" : progress.current_stage ? "正在等待该阶段开始执行" : "尚未进入执行步骤");
  const total = Number(progress.total_steps || 0);
  const completed = Number(progress.completed_steps || 0);
  const progressPercent = total ? Math.min(state.running && completed >= total ? 96 : 100, (completed / total) * 100) : 0;
  elements.workflowProgress.style.width = `${progressPercent}%`;
  elements.workflowProgressText.textContent = `已完成 ${completed} / ${total} 个步骤`;
  renderStages(state.stages || []);
  renderRoundCompletion(state);

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
    ? `本会话已实时优选 ${liveNodes.length} 条；后续手动续选只追加去重，关闭软件后下次运行才清空；日本节点独立豁免`
    : "本轮尚无合格结果；测速通过一个，这里立即增加一个";
  elements.liveNodeRows.innerHTML = liveNodes.length
    ? nodeRowsHtml(sortResultNodes(liveNodes, "liveNodeTable"))
    : '<tr><td class="empty" colspan="12">等待本轮实时优选结果</td></tr>';
}

function selectedLatency(node) {
  const probe = String(lastState?.local_rules?.latency_probe || elements.ruleLatencyProbe?.value || "tcp");
  if (probe === "tls") return { label: "TLS", value: node.tls_latency_ms };
  if (probe === "https") return { label: "HTTPS", value: node.http_latency_ms };
  return { label: "TCP", value: node.tcp_latency_ms };
}

function competitionRowsHtml(nodes) {
  return nodes.map((node) => {
    const country = currentCountry(node);
    const location = [node.city, node.region].filter(Boolean).join(" · ") || "未知位置";
    const latency = selectedLatency(node);
    const jitter = formatMetric(node.jitter_ms);
    const loss = Number.isFinite(Number(node.loss_rate)) ? `${(Number(node.loss_rate) * 100).toFixed(1)}%` : "--";
    const speed = formatMetric(node.speed_mbps);
    const endpoint = node.ip_port || `${node.ip}:${node.port || 443}`;
    return `
      <tr>
        <td class="rank">${node._rank}</td>
        <td><span class="ip-value">${escapeHtml(endpoint)}</span></td>
        <td><span class="location-main">${escapeHtml(country)} · ${escapeHtml(node.city || "未知城市")}</span><span class="location-sub">${escapeHtml(location)}</span></td>
        <td>${escapeHtml(node.colo || "--")}</td>
        <td class="number ${numberClass(latency.value, 150, 300)}"><span class="probe-chip">${latency.label}</span> ${formatMetric(latency.value)} ms</td>
        <td class="number ${numberClass(node.jitter_ms, 50, 200)}">${jitter} ms</td>
        <td class="number ${numberClass(Number(node.loss_rate) * 100, 0, 10)}">${loss}</td>
        <td><span class="speed">${speed} Mbps</span></td>
        <td><button class="button secondary small copy-one" data-value="${escapeHtml(endpoint)}" type="button">复制</button></td>
      </tr>`;
  }).join("");
}

function renderCompetitionNodes() {
  elements.copyCompetitionResults.disabled = competitionNodes.length === 0;
  const status = String(competitionReport.status || "idle");
  const counts = competitionReport.counts || {};
  if (status === "running") {
    elements.competitionResultSummary.textContent = `正在合并复测：输入 ${counts.input || 0} 条，已完成测速 ${counts.tested || 0} 条，当前 TOP ${competitionNodes.length}`;
  } else if (status === "completed") {
    elements.competitionResultSummary.textContent = `竞赛复测已完成：云端旧 IP 与本轮合格 IP 已去重并完整复测，当前选出 TOP ${competitionNodes.length}；JP 另行豁免附加`;
  } else if (status === "degraded") {
    elements.competitionResultSummary.textContent = competitionReport.stage || "竞赛复测结果读取失败";
  } else {
    elements.competitionResultSummary.textContent = "等待合并云端与本轮合格 IP 后开始完整复测";
  }
  elements.competitionNodeRows.innerHTML = competitionNodes.length
    ? competitionRowsHtml(sortResultNodes(competitionNodes, "competitionNodeTable"))
    : '<tr><td class="empty" colspan="9">等待发布前竞赛复测结果</td></tr>';
}

function liveTestStatusLabel(status) {
  return {
    queued: "待测",
    testing: "测试中",
    passed: "通过",
    eliminated: "已淘汰",
    retained: "JP 保留",
  }[status] || "未知";
}

function liveTestStatusClass(status) {
  if (status === "testing") return "testing";
  if (status === "eliminated") return "eliminated";
  if (status === "passed") return "passed";
  if (status === "retained") return "retained";
  return "queued";
}

function liveTestRowsHtml(records, startIndex = 0) {
  return records.map((node, index) => {
    const country = String(node.country || node.country_hint || "未知").toUpperCase();
    const lane = node.lane === "jp" ? "JP 豁免" : "普通";
    const location = [country, node.city, node.region].filter(Boolean).join(" · ") || "未知位置";
    const endpoint = node.ip_port || `${node.ip}:${node.port || 443}`;
    const lossRate = node.loss_rate ?? node.tcp_loss_rate;
    const loss = Number.isFinite(Number(lossRate)) ? `${(Number(lossRate) * 100).toFixed(1)}%` : "--";
    const speed = formatMetric(node.speed_mbps);
    const megaBytes = Number.isFinite(Number(node.speed_mbps)) ? (Number(node.speed_mbps) / 8).toFixed(1) : "--";
    const status = String(node.status || "queued");
    const reason = node.reason || "";
    return `
      <tr class="live-test-row ${status === "eliminated" ? "is-eliminated" : ""}">
        <td class="rank">${startIndex + index + 1}</td>
        <td><span class="ip-value">${escapeHtml(endpoint)}</span></td>
        <td><span class="location-main">${escapeHtml(lane)} · ${escapeHtml(country)}</span><span class="location-sub">${escapeHtml(location)}</span></td>
        <td>${escapeHtml(node.colo || "--")}</td>
        <td>${escapeHtml(node.stage || "等待测试")}</td>
        <td><span class="test-status ${liveTestStatusClass(status)}">${escapeHtml(liveTestStatusLabel(status))}</span></td>
        <td class="number ${numberClass(node.tcp_latency_ms, 150, 300)}">${formatMetric(node.tcp_latency_ms)} ms</td>
        <td class="number ${numberClass(node.tls_latency_ms, 200, 300)}">${formatMetric(node.tls_latency_ms)} ms</td>
        <td class="number ${numberClass(node.http_latency_ms, 200, 300)}">${formatMetric(node.http_latency_ms)} ms</td>
        <td class="number ${numberClass(node.average_latency_ms, 180, 300)}">${formatMetric(node.average_latency_ms)} ms</td>
        <td class="number ${numberClass(node.overall_jitter_ms, 50, 200)}">${formatMetric(node.overall_jitter_ms ?? node.tcp_jitter_ms)} ms</td>
        <td class="number ${numberClass(Number(lossRate) * 100, 0, 20)}">${loss}</td>
        <td><span class="speed">${speed} Mbps</span><span class="location-sub">${megaBytes} MB/s</span></td>
        <td class="test-reason" title="${escapeHtml(reason)}">${escapeHtml(reason || "--")}</td>
      </tr>`;
  }).join("");
}

function renderLiveTests(data) {
  const report = data.report || {};
  liveTestRecords = Array.isArray(data.tests) ? data.tests : [];
  const total = Number(data.total ?? report.total ?? liveTestRecords.length) || 0;
  const processed = Number(report.processed || 0) || 0;
  const queued = Number(report.queued || 0) || 0;
  const testing = Number(report.testing || 0) || 0;
  const passed = Number(report.passed || 0) || 0;
  const eliminated = Number(report.eliminated || 0) || 0;
  const retained = Number(report.retained || 0) || 0;
  liveTestRecordCount = Number(data.records_total ?? report.records_total ?? liveTestRecords.length) || 0;
  const offset = Number(data.offset || 0) || 0;
  const pageCount = Math.max(1, Math.ceil(liveTestRecordCount / liveTestPageSize));
  const responsePage = Math.floor(offset / liveTestPageSize) + 1;
  if (liveTestPage > pageCount) {
    liveTestPage = pageCount;
    setTimeout(fetchLiveTests, 0);
  } else {
    liveTestPage = responsePage;
  }
  const percent = total ? Math.min(100, (processed / total) * 100) : 0;
  elements.liveTestProgress.style.width = `${percent}%`;
  elements.liveTestProgressText.textContent = `已处理 ${processed} / ${total} 个候选 · 待测 ${queued} · 测试中 ${testing}`;
  elements.liveTestSummary.textContent = total
    ? `候选 ${total} 条 · 通过 ${passed} · 淘汰 ${eliminated} · JP 保留 ${retained} · 本轮 ${liveTestRecordCount} 条记录均可分页查看`
    : "等待本地测试开始；每个 IP 完成一阶段就会在这里更新";
  elements.liveTestPageInfo.textContent = `共 ${liveTestRecordCount} 条记录 · 第 ${liveTestPage} / ${pageCount} 页`;
  elements.liveTestPrevious.disabled = liveTestPage <= 1;
  elements.liveTestNext.disabled = liveTestPage >= pageCount;
  const reportStatus = String(report.status || "idle");
  const badgeClass = reportStatus === "running" ? "running" : reportStatus === "completed" ? "success" : reportStatus === "degraded" ? "failure" : "neutral";
  elements.liveTestStage.className = `badge ${badgeClass}`;
  elements.liveTestStage.textContent = report.stage || (reportStatus === "completed" ? "本轮已完成" : "尚未开始");
  elements.liveTestRows.innerHTML = liveTestRecords.length
    ? liveTestRowsHtml(liveTestRecords, offset)
    : '<tr><td class="empty" colspan="14">等待本地测试数据</td></tr>';
}

function renderNodes() {
  const sourceLabels = {
    "local-ready": "本机保存的云端已发布结果",
    "published-cache": "线上已发布结果",
    "published-cloud": "已从 GitHub 核对的云端发布结果",
    "published-unavailable": "云端发布结果暂不可用",
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

async function fetchCompetitionNodes() {
  try {
    const response = await fetch("/api/competition-nodes", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    competitionReport = data.report || {};
    competitionNodes = (data.nodes || []).map((node, index) => ({ ...node, _rank: index + 1 }));
    renderCompetitionNodes();
  } catch (error) {
    elements.competitionResultSummary.textContent = `竞赛复测结果读取失败：${error.message}`;
  }
}

async function fetchLiveTests() {
  try {
    const offset = (liveTestPage - 1) * liveTestPageSize;
    const response = await fetch(`/api/live-tests?limit=${liveTestPageSize}&offset=${offset}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderLiveTests(await response.json());
  } catch (error) {
    elements.liveTestSummary.textContent = `实时测速面板读取失败：${error.message}`;
  }
}

elements.liveTestPageSize.addEventListener("change", () => {
  liveTestPageSize = Number(elements.liveTestPageSize.value) || 200;
  liveTestPage = 1;
  fetchLiveTests();
});

elements.liveTestPrevious.addEventListener("click", () => {
  if (liveTestPage <= 1) return;
  liveTestPage -= 1;
  fetchLiveTests();
});

elements.liveTestNext.addEventListener("click", () => {
  const pageCount = Math.max(1, Math.ceil(liveTestRecordCount / liveTestPageSize));
  if (liveTestPage >= pageCount) return;
  liveTestPage += 1;
  fetchLiveTests();
});

function renderRules(rules) {
  const values = { ...defaultRules, ...(rules || {}) };
  const legacyProbe = values.tcp_enabled ? "tcp" : values.tls_enabled ? "tls" : values.http_enabled ? "https" : "tcp";
  elements.ruleLatencyProbe.value = ["tcp", "tls", "https"].includes(values.latency_probe)
    ? values.latency_probe
    : legacyProbe;
  const legacyLimit = elements.ruleLatencyProbe.value === "tls"
    ? values.tls_max_ms
    : elements.ruleLatencyProbe.value === "https"
      ? values.http_ttfb_max_ms
      : values.tcp_max_ms;
  elements.ruleLatencyMax.value = values.latency_max_ms ?? legacyLimit ?? 200;
  elements.ruleJitter.value = values.jitter_max_ms;
  elements.ruleLoss.value = values.loss_max_percent;
  elements.ruleSpeed.value = values.speed_min_mbps;
  renderRuleAvailability();
}

function renderRuleAvailability() {
  const selected = elements.ruleLatencyProbe.value;
  elements.ruleLoss.disabled = selected !== "tcp";
}

function collectRules() {
  const fields = {
    latency_max_ms: elements.ruleLatencyMax,
    jitter_max_ms: elements.ruleJitter,
    loss_max_percent: elements.ruleLoss,
    speed_min_mbps: elements.ruleSpeed,
  };
  const latencyProbe = elements.ruleLatencyProbe.value;
  const rules = {
    latency_probe: latencyProbe,
    tcp_enabled: latencyProbe === "tcp",
    tls_enabled: latencyProbe === "tls",
    http_enabled: latencyProbe === "https",
  };
  for (const [name, input] of Object.entries(fields)) {
    if (!input.value.trim()) throw new Error(`${name} 不能为空`);
    const value = Number(input.value);
    if (!Number.isFinite(value)) throw new Error(`${name} 必须是有效数字`);
    rules[name] = value;
  }
  return rules;
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
    showToast(`规则读取失败：${error.message}`);
  }
}

async function post(path, body = undefined) {
  const options = { method: "POST" };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
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
    elements.stopButton.textContent = "正在登记";
    const result = await post("/api/stop-selection");
    showToast(result.cloud_cancelled
      ? "本地测速尚未开始，云端工作流已取消"
      : result.stopped
        ? "已登记：完成当前100-IP批次后，进行发布前竞赛复测并推送TOP300"
        : "当前没有运行中的优选任务");
    await fetchState();
  } catch (error) {
    elements.stopButton.disabled = false;
    elements.stopButton.textContent = "停止优选";
    showToast(`停止失败：${error.message}`);
  }
});

elements.publishButton.addEventListener("click", async () => {
  try {
    elements.publishButton.disabled = true;
    elements.publishButton.textContent = "正在提交或排队";
    const result = await post("/api/publish");
    showToast(result.started
      ? "已启动手动推送：先按本地规则复测和重排，再发布到 GitHub"
      : result.reason || "手动推送已排队");
    await fetchState();
  } catch (error) {
    elements.publishButton.disabled = !lastState?.cycle_started;
    elements.publishButton.textContent = "手动推送";
    showToast(`手动推送失败：${error.message}`);
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

elements.closeButton.addEventListener("click", async () => {
  try {
    await post("/api/shutdown");
    showToast("已请求关闭：后台将完成复测与推送收尾后退出");
    elements.closeButton.disabled = true;
    setTimeout(() => window.close(), 250);
  } catch (error) {
    showToast(error.message);
  }
});

elements.ruleLatencyProbe.addEventListener("change", renderRuleAvailability);

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
elements.copyCompetitionResults.addEventListener("click", () => {
  const text = competitionNodes.map((node) => node.ip_port || `${node.ip}:${node.port || 443}`).join("\n");
  copyText(text, `已复制 ${competitionNodes.length} 个竞赛复测 IP`);
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
elements.competitionNodeRows.addEventListener("click", (event) => {
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
  button.classList.remove("button-pressed");
  void button.offsetWidth;
  button.classList.add("button-pressed");
  setTimeout(() => button.classList.remove("button-pressed"), 460);
  ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
});

function createAmbientParticles() {
  const layer = $("#ambientParticles");
  if (!layer || layer.childElementCount) return;
  const colors = ["cyan", "green", "violet"];
  for (let index = 0; index < 30; index += 1) {
    const particle = document.createElement("i");
    particle.className = `ambient-particle ${colors[index % colors.length]}`;
    particle.style.setProperty("--x", `${(index * 37 + 11) % 100}%`);
    particle.style.setProperty("--drift", `${((index * 29) % 31) - 15}vw`);
    particle.style.setProperty("--size", `${2 + (index % 4)}px`);
    particle.style.setProperty("--duration", `${14 + (index % 8) * 2}s`);
    particle.style.setProperty("--delay", `${-(index % 12) * 1.7}s`);
    layer.appendChild(particle);
  }
}


const resultSorts = {};
function sortResultNodes(nodes, tableId) {
  const sort = resultSorts[tableId];
  if (!sort) return nodes;
  const value = node => sort.key === "selected" ? selectedLatency(node).value : node[sort.key];
  return [...nodes].sort((a, b) => {
    const left = value(a), right = value(b);
    const missing = v => v === null || v === undefined || !Number.isFinite(Number(v));
    if (missing(left)) return missing(right) ? a._rank - b._rank : 1;
    if (missing(right)) return -1;
    return (Number(left) - Number(right)) * sort.direction || a._rank - b._rank;
  });
}
for (const [tableId, keys] of Object.entries({
  liveNodeTable: ["_rank", null, null, null, "tcp_latency_ms", "tls_latency_ms", "http_latency_ms", "average_latency_ms", "jitter_ms", "loss_rate", "speed_mbps"],
  competitionNodeTable: ["_rank", null, null, null, "selected", "jitter_ms", "loss_rate", "speed_mbps"],
})) {
  const table = document.getElementById(tableId);
  table.querySelectorAll("th").forEach((th, index) => {
    const key = keys[index];
    if (!key) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "result-sort-header";
    const label = th.textContent;
    button.textContent = label + " ↕";
    th.replaceChildren(button);
    button.addEventListener("click", () => {
      const old = resultSorts[tableId];
      const direction = old?.key === key ? -old.direction : 1;
      resultSorts[tableId] = {key, direction};
      table.querySelectorAll("th").forEach(cell => cell.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", direction === 1 ? "ascending" : "descending");
      button.setAttribute("aria-label", label + (direction === 1 ? "，升序" : "，降序"));
      renderLiveNodes();
      renderCompetitionNodes();
    });
  });
}
document.querySelectorAll(".input-unit input[type=number]").forEach(input => {
  input.step = "1";
  const controls = document.createElement("span");
  controls.className = "number-controls";
  for (const [label, delta] of [["减少", -1], ["增加", 1]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = delta < 0 ? "−" : "+";
    button.setAttribute("aria-label", input.closest("label").querySelector("span").textContent + label + "1");
    button.addEventListener("click", () => {
      const next = Math.round(Number(input.value) || 0) + delta;
      input.value = String(Math.min(input.max === "" ? Infinity : Number(input.max), Math.max(input.min === "" ? -Infinity : Number(input.min), next)));
      input.dispatchEvent(new Event("input", {bubbles:true}));
      input.dispatchEvent(new Event("change", {bubbles:true}));
    });
    controls.appendChild(button);
  }
  input.parentElement.appendChild(controls);
});

createAmbientParticles();
// Decorative only: one lightweight particle layer per hovered card, no timers.
function addCardAtmosphere(card) {
  if (!card || card.querySelector(":scope > .card-atmosphere")) return;
  const layer = document.createElement("span");
  layer.className = "card-atmosphere";
  layer.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 32; index += 1) {
    const spark = document.createElement("i");
    spark.style.setProperty("--px", `${7 + (index * 31) % 86}%`);
    spark.style.setProperty("--pd", `${2.8 + index % 4 * .4}s`);
    spark.style.setProperty("--pl", `${-index * .31}s`);
    spark.style.setProperty("--sway", `${index % 2 ? 24 : -24}px`);
    layer.appendChild(spark);
  }
  card.appendChild(layer);
}
document.addEventListener("pointerover", (event) => {
  addCardAtmosphere(event.target.closest(".panel, .metric-card, .stage-card"));
});
fetchState();
const browserClient = sessionStorage.getItem("noodeBrowserClient") || crypto.randomUUID();
sessionStorage.setItem("noodeBrowserClient", browserClient);
function browserPresence(closed = false) {
  const body = JSON.stringify({client: browserClient, closed});
  if (closed) navigator.sendBeacon("/api/browser-presence", new Blob([body], {type:"application/json"}));
  else fetch("/api/browser-presence", {method:"POST", headers:{"Content-Type":"application/json"}, body}).catch(() => {});
}
browserPresence();
window.addEventListener("pageshow", () => browserPresence());
window.addEventListener("pagehide", () => browserPresence(true));
setInterval(() => browserPresence(), 5000);
fetchNodes();
fetchLiveNodes();
fetchCompetitionNodes();
fetchLiveTests();
fetchRules();
setInterval(fetchState, 2500);
setInterval(fetchNodes, 5000);
setInterval(fetchLiveNodes, 1000);
setInterval(fetchCompetitionNodes, 1000);
setInterval(fetchLiveTests, 1000);
