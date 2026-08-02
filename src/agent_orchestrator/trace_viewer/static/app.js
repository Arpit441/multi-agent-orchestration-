const PIPELINE_NODES = {
  research_report: [
    "planner",
    "knowledge_lookup",
    "researcher",
    "writer",
    "source_guard",
    "critic",
    "human_approve",
    "deliver",
  ],
  support_resolution: [
    "knowledge_lookup",
    "frontline",
    "sentiment",
    "faq_agent",
    "technical_agent",
    "billing_agent",
    "quality_critic",
    "human_escalate",
    "deliver",
  ],
};

/** Plain-language labels for the default UI. */
const AGENT_META = {
  planner: { icon: "🗺️", label: "Planner", role: "Budget-aware execution plan" },
  knowledge_lookup: { icon: "🧠", label: "Knowledge", role: "Look up uploaded documents" },
  researcher: { icon: "🧠", label: "Research", role: "ReAct search, browse, synthesize" },
  web_search: { icon: "🔍", label: "Search", role: "Finds sources" },
  writer: { icon: "✍️", label: "Writer", role: "Drafts the report" },
  source_guard: { icon: "🛡️", label: "Source guard", role: "Keeps only verified links" },
  critic: { icon: "✅", label: "Verifier", role: "Scores quality" },
  fixer: { icon: "🔧", label: "Fixer", role: "Recovers from agent failures" },
  human_approve: { icon: "👤", label: "You", role: "Human review (below threshold)" },
  deliver: { icon: "📦", label: "Deliver", role: "Final answer" },
  frontline: { icon: "🎫", label: "Frontline", role: "Classifies the ticket" },
  sentiment: { icon: "💬", label: "Sentiment", role: "Checks urgency and tone" },
  faq_agent: { icon: "📘", label: "FAQ", role: "Drafts FAQ-style replies" },
  technical_agent: { icon: "🛠️", label: "Technical", role: "Diagnoses technical issues" },
  billing_agent: { icon: "💳", label: "Billing", role: "Handles billing questions" },
  quality_critic: { icon: "✅", label: "Verifier", role: "Checks reply quality" },
  human_escalate: { icon: "👤", label: "You", role: "Human review before sending" },
};

const AGENT_TOOLS = {
  planner: "Budget + path selection",
  knowledge_lookup: "Knowledge base",
  researcher: "search_web · browse_url · synthesize (max 3)",
  web_search: "Web search",
  writer: "Verified sources",
  source_guard: "URL validation",
  critic: "Quality rubric",
  fixer: "Error recovery",
  human_approve: "Human review",
  deliver: "Delivery",
  frontline: "Ticket context",
  sentiment: "Sentiment analysis",
  faq_agent: "Knowledge base",
  technical_agent: "Knowledge base",
  billing_agent: "Policy lookup",
  quality_critic: "Quality rubric",
  human_escalate: "Human review",
};

/** High-level progress stages shown to everyone. */
const USER_STAGES = {
  research_report: [
    { id: "plan", label: "Plan", icon: "🗺️", nodes: ["planner"] },
    { id: "search", label: "Search", icon: "🔍", nodes: ["knowledge_lookup", "researcher"] },
    { id: "analyze", label: "Analyze", icon: "🧠", nodes: ["researcher"] },
    { id: "draft", label: "Draft", icon: "✍️", nodes: ["writer", "source_guard"] },
    { id: "verify", label: "Verify", icon: "✅", nodes: ["critic", "human_approve", "deliver"] },
  ],
  support_resolution: [
    { id: "search", label: "Search", icon: "🔍", nodes: ["knowledge_lookup"] },
    { id: "analyze", label: "Analyze", icon: "🧠", nodes: ["frontline", "sentiment"] },
    {
      id: "draft",
      label: "Draft",
      icon: "✍️",
      nodes: ["faq_agent", "technical_agent", "billing_agent"],
    },
    {
      id: "verify",
      label: "Verify",
      icon: "✅",
      nodes: ["quality_critic", "human_escalate", "deliver"],
    },
  ],
};

const STATUS_LABELS = {
  PENDING: "Starting",
  RUNNING: "Working",
  RETRYING: "Retrying",
  PAUSED: "Needs review",
  COMPLETED: "Done",
  FAILED: "Failed",
};

let selectedRunId = null;
/** @type {Map<string, boolean>} user open/closed prefs for agent cards across poll refreshes */
const agentCardOpenPrefs = new Map();
/** @type {string|null} workflow chosen on the landing picker before composer shows */
let selectedWorkflow = null;
let pollTimer = null;
let activeTab = "answer";
let lastStatus = null;
let modalAutoOpenedFor = null;
let currentReportText = "";
let currentTopic = "";
let pipelines = [];
let currentPipelineId = "research_report";
/** @type {Array<Record<string, any>>} */
let historyRunsCache = [];
/** @type {{ key: string, dir: "asc" | "desc" }} */
let historySort = { key: "created_at", dir: "desc" };
/** @type {"report" | "activity" | null} */
let workspaceView = null;
/** Remember explicit user choice per run so polls don't fight the toggle. */
const workspaceViewPrefs = new Map();

const USD_PER_TOKEN_FALLBACK = 0.00000645;

async function api(path, options = {}) {
  const { headers: extraHeaders, ...rest } = options;
  const res = await fetch(path, {
    credentials: "same-origin",
    ...rest,
    headers: { "Content-Type": "application/json", ...(extraHeaders || {}) },
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Authentication required");
  }
  if (res.status === 429) {
    const text = await res.text();
    throw new Error(text || "Rate limit exceeded — try again later.");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

function newIdempotencyKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function mdToHtml(md) {
  if (!md) return "";
  if (window.marked) {
    try {
      return window.marked.parse(String(md));
    } catch {
      /* fall through */
    }
  }
  return `<pre>${esc(md)}</pre>`;
}

function relativeTime(iso) {
  if (!iso) return "";
  try {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 45) return "just now";
    if (seconds < 90) return "1 min ago";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} mins ago`;
    if (seconds < 5400) return "1 hour ago";
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
    if (seconds < 172800) return "1 day ago";
    return `${Math.floor(seconds / 86400)} days ago`;
  } catch {
    return "";
  }
}

function historyStatusLabel(status, error) {
  const err = String(error || "");
  const interrupted =
    /Process restarted|Interrupted — server restarted/i.test(err) ||
    (["RUNNING", "RETRYING", "PENDING"].includes(status) &&
      /resume to continue/i.test(err));
  if (interrupted) {
    return { icon: "⊘", text: "Interrupted", cls: "bad" };
  }
  switch (status) {
    case "COMPLETED":
      return { icon: "✓", text: "Completed", cls: "ok" };
    case "FAILED":
      return { icon: "✕", text: "Failed", cls: "bad" };
    case "PAUSED":
      return { icon: "★", text: "Needs review", cls: "paused" };
    case "RUNNING":
    case "RETRYING":
    case "PENDING":
      return { icon: "●", text: "Working", cls: "working" };
    default:
      return { icon: "•", text: status || "Unknown", cls: "" };
  }
}

async function deleteHistoryRun(runId) {
  await api(`/api/runs/${runId}`, { method: "DELETE" });
  if (selectedRunId === runId) {
    selectedRunId = null;
    showEmptyState(true);
  }
  await refreshRuns();
}

async function refreshRuns() {
  const data = await api("/api/runs");
  historyRunsCache = data.runs || [];
  const list = document.getElementById("run-list");
  list.innerHTML = "";
  for (const run of historyRunsCache) {
    const li = document.createElement("li");
    if (run.run_id === selectedRunId) li.classList.add("active");

    const title = (run.title || "Untitled task").trim();
    const when = relativeTime(run.updated_at || run.created_at);
    const status = historyStatusLabel(run.status, run.error);
    const stamp = Date.parse(run.updated_at || run.created_at || "") || 0;
    const ageMs = stamp ? Date.now() - stamp : Infinity;
    // Stuck Working / Interrupted — click deletes instead of reopening a dead run.
    // Live Working (recently updated, or currently selected) still opens normally.
    const isDeadWorking =
      status.text === "Interrupted" ||
      (["RUNNING", "RETRYING", "PENDING"].includes(run.status) &&
        (ageMs > 90_000 ||
          /restarted|Interrupted/i.test(String(run.error || ""))));

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "history-item";
    if (isDeadWorking) {
      btn.title = "Stuck session — click to delete";
    }
    btn.innerHTML = `
      <span class="history-main">
        <span class="history-title">${esc(title || "Untitled")}</span>
        <span class="history-meta">${esc(when)}${when ? " · " : ""}${esc(
      run.graph_label || "Research"
    )}</span>
        <span class="history-status ${status.cls}">
          <span aria-hidden="true">${status.icon}</span> ${esc(status.text)}
        </span>
      </span>
      <span class="history-actions">
        <span class="history-copy" title="Copy run id" role="button" tabindex="0">⧉</span>
        <span class="history-delete" title="Delete" role="button" tabindex="0">✕</span>
      </span>
    `;
    btn.addEventListener("click", async (e) => {
      const copy = e.target.closest(".history-copy");
      const del = e.target.closest(".history-delete");
      if (copy) {
        e.preventDefault();
        e.stopPropagation();
        navigator.clipboard.writeText(run.run_id).then(() => {
          copy.textContent = "✓";
          setTimeout(() => {
            copy.textContent = "⧉";
          }, 1000);
        });
        return;
      }
      if (del) {
        e.preventDefault();
        e.stopPropagation();
        await deleteHistoryRun(run.run_id);
        return;
      }
      if (isDeadWorking) {
        await deleteHistoryRun(run.run_id);
        return;
      }
      selectRun(run.run_id);
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function formatCostUsd(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "$0.000";
  if (n < 0.001) return `$${n.toFixed(5)}`;
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function formatLatencyShort(ms) {
  const n = Number(ms) || 0;
  if (n < 1000) return `${Math.round(n)}ms`;
  const secs = n / 1000;
  if (secs < 60) return `${secs < 10 ? secs.toFixed(1) : Math.round(secs)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}m ${s}s`;
}

function formatTokenCount(n) {
  return Number(n || 0).toLocaleString("en-US");
}

function telemetryFromRun(run) {
  const t = run?.telemetry;
  if (t && typeof t === "object") return t;
  const state = run?.state || {};
  const budget = state.budget || run?.budget || {};
  const tokens = Number(budget.tokens_used ?? run?.tokens_used ?? 0) || 0;
  const max = Math.max(1, Number(budget.max_tokens_total ?? run?.max_tokens_total ?? 8000) || 8000);
  const ratio = tokens / max;
  const rate = Number(t?.usd_per_token) || USD_PER_TOKEN_FALLBACK;
  const cost = Number(run?.estimated_cost_usd);
  const path =
    run?.path ||
    (String(state.plan_recommendation || "").toLowerCase() === "fast_path" ||
    state.execution_mode === "fast_fallback"
      ? "fast_path"
      : "agentic_path");
  return {
    tokens_used: tokens,
    max_tokens_total: max,
    token_ratio: ratio,
    bar_level: ratio >= 0.9 ? "red" : ratio >= 0.7 ? "yellow" : "green",
    estimated_cost_usd: Number.isFinite(cost) ? cost : tokens * rate,
    latency_ms: Number(run?.latency_ms ?? 0) || 0,
    latency_seconds: Number(run?.latency_seconds ?? 0) || 0,
    path,
    path_label: run?.path_label || (path === "fast_path" ? "Fast path" : "Agentic path"),
    circuit_breaker_triggered: !!(
      state.circuit_breaker_triggered || run?.circuit_breaker_triggered
    ),
    steps_taken: Number(budget.steps_taken || 0) || 0,
    max_agent_steps: Number(budget.max_agent_steps || 5) || 5,
  };
}

function costSummaryLine(tel) {
  const used = formatTokenCount(tel.tokens_used);
  const max = formatTokenCount(tel.max_tokens_total);
  const cost = formatCostUsd(tel.estimated_cost_usd);
  const lat = formatLatencyShort(tel.latency_ms);
  return `${used} / ${max} tokens · ~${cost} · ${lat}`;
}

function setPathBadge(el, tel) {
  if (!el) return;
  el.textContent = tel.path_label || "—";
  el.className = `path-badge ${tel.path || ""}`;
}

function setTokenBar(el, tel) {
  if (!el) return;
  const pct = Math.max(0, Math.min(100, (tel.token_ratio || 0) * 100));
  el.style.width = `${pct}%`;
  el.className = `token-bar ${tel.bar_level || "green"}`;
}

function renderCostTelemetry(run) {
  const tel = telemetryFromRun(run);
  const line = costSummaryLine(tel);

  const strip = document.getElementById("cost-strip");
  strip?.classList.remove("hidden");
  setTokenBar(document.getElementById("token-bar"), tel);
  const stripText = document.getElementById("cost-strip-text");
  if (stripText) {
    stripText.textContent = line;
    stripText.title = `Steps ${tel.steps_taken || 0}/${tel.max_agent_steps || 5}`;
  }
  setPathBadge(document.getElementById("path-badge"), tel);

  const banner = document.getElementById("circuit-banner");
  banner?.classList.toggle("hidden", !tel.circuit_breaker_triggered);
}

function hideCostTelemetry() {
  document.getElementById("cost-strip")?.classList.add("hidden");
  document.getElementById("circuit-banner")?.classList.add("hidden");
}

function historySortValue(run, key) {
  switch (key) {
    case "run_id":
      return String(run.run_id || "");
    case "title":
      return String(run.title || "").toLowerCase();
    case "path":
      return String(run.path_label || run.path || "");
    case "cost":
      return Number(run.estimated_cost_usd) || 0;
    case "latency":
      return Number(run.latency_ms) || 0;
    case "status":
      return String(run.status || "");
    case "created_at":
    default:
      return String(run.created_at || run.updated_at || "");
  }
}

function sortedHistoryRuns() {
  const rows = [...historyRunsCache];
  const { key, dir } = historySort;
  const mult = dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const av = historySortValue(a, key);
    const bv = historySortValue(b, key);
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * mult;
    return String(av).localeCompare(String(bv)) * mult;
  });
  return rows;
}

function renderHistoryTable() {
  const body = document.getElementById("history-table-body");
  const empty = document.getElementById("history-table-empty");
  if (!body) return;
  body.innerHTML = "";
  const rows = sortedHistoryRuns();
  if (empty) empty.classList.toggle("hidden", rows.length > 0);

  document.querySelectorAll("#history-table th[data-sort]").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === historySort.key) {
      th.classList.add(historySort.dir === "asc" ? "sort-asc" : "sort-desc");
    }
  });

  for (const run of rows) {
    const tr = document.createElement("tr");
    if (run.run_id === selectedRunId) tr.classList.add("active");
    const shortId = String(run.run_id || "").slice(0, 8);
    tr.innerHTML = `
      <td title="${esc(run.run_id || "")}">${esc(shortId)}</td>
      <td title="${esc(run.title || "")}">${esc(run.title || "Untitled")}</td>
      <td>${esc(run.path_label || "—")}</td>
      <td>${esc(formatCostUsd(run.estimated_cost_usd))}</td>
      <td>${esc(formatLatencyShort(run.latency_ms))}</td>
      <td>${esc(STATUS_LABELS[run.status] || run.status || "—")}</td>
    `;
    tr.addEventListener("click", () => selectRun(run.run_id));
    body.appendChild(tr);
  }
}

document.getElementById("history-table")?.addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  if (!key) return;
  if (historySort.key === key) {
    historySort.dir = historySort.dir === "asc" ? "desc" : "asc";
  } else {
    historySort = { key, dir: key === "title" || key === "run_id" || key === "status" ? "asc" : "desc" };
  }
  renderHistoryTable();
});

function nodeOrderFor(run) {
  const name = run.graph_name || "research_report";
  return PIPELINE_NODES[name] || PIPELINE_NODES.research_report;
}

function stagesFor(run) {
  return USER_STAGES[run.graph_name] || USER_STAGES.research_report;
}

function stageDurationMs(stageEvents) {
  return stageEvents.reduce((sum, ev) => sum + (Number(ev.duration_ms) || 0), 0);
}

function formatStageDuration(ms) {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs < 10 ? secs.toFixed(1) : Math.round(secs)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}m ${s}s`;
}

function scrollToStage(stageId, stages) {
  const stage = stages.find((s) => s.id === stageId);
  if (!stage) return;
  const steps = [...document.querySelectorAll(".agent-step[data-node]")];
  const target = steps.find((el) => stage.nodes.includes(el.dataset.node));
  if (!target) return;
  target.open = true;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 1200);
}

function renderSimpleProgress(run) {
  const el = document.getElementById("simple-progress");
  if (!el) return;
  el.innerHTML = "";
  const stages = stagesFor(run);
  const events = run.trace || [];
  const current = run.current_node;

  stages.forEach((stage, idx) => {
    const li = document.createElement("li");
    const stageEvents = events.filter((t) => stage.nodes.includes(t.node_name));
    const hasError = stageEvents.some((t) => t.outcome === "error");
    const touched = stageEvents.length > 0 || stage.nodes.includes(current);
    const isCurrent = stage.nodes.includes(current);
    const duration = stageDurationMs(stageEvents);

    let cls = "todo";
    if (run.status === "COMPLETED") cls = "done";
    else if (hasError) cls = "error";
    else if (run.status === "PAUSED" && isCurrent) cls = "paused";
    else if (["RUNNING", "RETRYING", "PENDING"].includes(run.status) && isCurrent) cls = "current";
    else if (touched && !isCurrent) cls = "done";

    const durationLabel =
      cls === "current" && duration <= 0
        ? "…"
        : cls === "todo"
        ? "—"
        : formatStageDuration(duration);

    li.className = cls;
    li.dataset.stage = stage.id;
    li.setAttribute("role", "button");
    li.tabIndex = 0;
    li.title = `Jump to ${stage.label}`;
    li.innerHTML = `
      <span class="step-icon" aria-hidden="true">${stage.icon || "•"}</span>
      <span class="step-copy">
        <span class="step-label">${esc(stage.label)}</span>
        <span class="step-duration">${esc(durationLabel)}</span>
      </span>
      ${idx < stages.length - 1 ? `<span class="step-connector" aria-hidden="true"></span>` : ""}
    `;
    li.addEventListener("click", () => scrollToStage(stage.id, stages));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        scrollToStage(stage.id, stages);
      }
    });
    el.appendChild(li);
  });
}

function renderGraphPath(run) {
  renderSimpleProgress(run);
}

// Build a human-readable "what this agent produced" summary per node.
function agentContribution(nodeName, ev, state) {
  const out = ev.output_snapshot || {};
  switch (nodeName) {
    case "knowledge_lookup": {
      const hits = out.knowledge_hits || state.knowledge_hits || [];
      const n = out.knowledge_doc_count ?? state.knowledge_doc_count ?? 0;
      if (!hits.length) {
        return {
          kind: "text",
          body: `Knowledge base: ${n} document(s). No strong match for this query — agents will still see a fallback note.`,
        };
      }
      const items = hits
        .map(
          (h) =>
            `<li><strong>${esc(h.filename)}</strong> (score ${esc(h.score)})<br /><span class="muted">${esc(
              (h.content || "").slice(0, 220)
            )}…</span></li>`
        )
        .join("");
      return { kind: "html", body: `<p class="muted">${n} doc(s) indexed · top chunks:</p><ul class="sources">${items}</ul>` };
    }
    case "planner": {
      const plan = out.execution_plan || state.execution_plan || out.plan || [];
      const rec = out.plan_recommendation || state.plan_recommendation || out.recommendation || "";
      const reason = out.plan_reasoning || state.plan_reasoning || out.reasoning || "";
      const est = out.estimated_tokens ?? state.estimated_tokens;
      const steps = Array.isArray(plan)
        ? `<ol>${plan
            .map(
              (s) =>
                `<li><strong>${esc(s.type || "")}</strong> — ${esc(s.reason || "")}</li>`
            )
            .join("")}</ol>`
        : "";
      return {
        kind: "html",
        body: `<p><strong>Recommendation:</strong> ${esc(rec || "—")} · est. tokens ${esc(
          est ?? "—"
        )}</p>${reason ? `<p class="muted">${esc(reason)}</p>` : ""}${steps}`,
      };
    }
    case "researcher": {
      const plan = out.research_plan || state.research_plan || {};
      const brief = out.research_brief || plan.research_brief || state.research_brief || "";
      const queries = out.search_queries || plan.search_queries || state.search_queries || [];
      const trace = out.research_trace || state.research_trace || [];
      const results = out.search_results || state.search_results || [];
      const queryHtml = Array.isArray(queries) && queries.length
        ? `<p class="muted"><strong>Queries used</strong></p><ul>${queries
            .map((q) => `<li><code>${esc(q)}</code></li>`)
            .join("")}</ul>`
        : "";
      const traceHtml = Array.isArray(trace) && trace.length
        ? `<p class="muted"><strong>ReAct trace</strong> (${trace.length} step(s))</p><ol>${trace
            .map((step) => {
              const action = step.action
                ? `<code>${esc(step.action)}</code> ${esc(
                    JSON.stringify(step.action_input || {})
                  )}`
                : "final";
              return `<li><em>${esc(step.thought || "")}</em><br/>→ ${action}<br/><span class="muted">${esc(
                String(step.observation || "").slice(0, 220)
              )}</span></li>`;
            })
            .join("")}</ol>`
        : "";
      const sourcesHtml = Array.isArray(results) && results.length
        ? `<p class="muted"><strong>Sources gathered</strong></p><ul class="sources">${results
            .map(
              (r) =>
                `<li><a href="${esc(r.href)}" target="_blank" rel="noreferrer">${esc(
                  r.title || r.href
                )}</a></li>`
            )
            .join("")}</ul>`
        : "";
      return {
        kind: "html",
        body: `${mdToHtml(brief)}${queryHtml}${traceHtml}${sourcesHtml}`,
      };
    }
    case "web_search": {
      const results = out.search_results || state.search_results || [];
      const used = out.search_queries_used || state.search_queries_used || [];
      const cacheHit = out.search_cache_hit ?? state.search_cache_hit;
      const meta = Array.isArray(used) && used.length
        ? `<p class="muted">Queries used: ${used.map((q) => `<code>${esc(q)}</code>`).join(" · ")}${
            cacheHit ? " · cache hit" : " · live search"
          }</p>`
        : "";
      if (Array.isArray(results) && results.length) {
        const items = results
          .map(
            (r) =>
              `<li><a href="${esc(r.href)}" target="_blank" rel="noreferrer">${esc(
                r.title || r.href
              )}</a><br /><span class="muted">${esc((r.body || "").slice(0, 180))}</span></li>`
          )
          .join("");
        return { kind: "html", body: `${meta}<ul class="sources">${items}</ul>` };
      }
      const warning = out.search_warning || state.search_warning || "No relevant sources captured.";
      return { kind: "html", body: `${meta}<p class="feedback">${esc(warning)}</p>` };
    }
    case "writer":
      return { kind: "markdown", body: out.report || state.report };
    case "source_guard": {
      const check = out.source_validation || state.source_validation || {};
      const removed = check.removed_url_count || 0;
      return {
        kind: "html",
        body: `<p><strong>${esc(check.allowed_url_count || 0)}</strong> search URL(s) allowed · <strong>${esc(
          removed
        )}</strong> invented/unverified URL(s) removed.</p>`,
      };
    }
    case "critic":
    case "quality_critic": {
      const c = out.critic_output || out.quality_output || state.critic_output || state.quality_output || {};
      const approved = c.approved ?? out.approved ?? state.approved;
      const score = c.score ?? out.score ?? state.score;
      const feedback = c.feedback ?? out.feedback ?? state.feedback;
      const badge = approved
        ? `<span class="verdict ok">APPROVED</span>`
        : `<span class="verdict bad">NEEDS REVISION</span>`;
      const scoreHtml = score != null ? `<span class="score">Score: ${esc(score)}/10</span>` : "";
      return {
        kind: "html",
        body: `<div class="critic-head">${badge}${scoreHtml}</div>${
          feedback ? `<p class="feedback">${esc(feedback)}</p>` : ""
        }`,
      };
    }
    case "frontline": {
      const f = out.frontline_output || state.frontline_output || {};
      return {
        kind: "html",
        body: `<p><strong>Intent:</strong> ${esc(f.intent || state.intent || "—")} · <strong>Escalate hint:</strong> ${esc(
          String(f.escalate ?? "—")
        )}</p><p class="muted">${esc(f.summary || state.summary || "")}</p><div class="report">${mdToHtml(
          f.preliminary_reply || state.preliminary_reply || ""
        )}</div>`,
      };
    }
    case "sentiment": {
      const s = out.sentiment_output || state.sentiment_output || {};
      const fr = s.frustrated ?? state.frustrated;
      const badge = fr
        ? `<span class="verdict bad">FRUSTRATED / RISK</span>`
        : `<span class="verdict ok">CALM ENOUGH</span>`;
      return {
        kind: "html",
        body: `<div class="critic-head">${badge}<span class="score">${esc(
          s.sentiment_label || ""
        )} · urgency ${esc(s.urgency || state.urgency || "")}</span></div><p class="feedback">${esc(
          s.reason || ""
        )}</p>`,
      };
    }
    case "faq_agent":
    case "technical_agent":
    case "billing_agent": {
      const sp = out.specialist_output || state.specialist_output || {};
      const reply = sp.draft_reply || state.draft_reply || "";
      const action = sp.suggested_action || state.suggested_action || "";
      return {
        kind: "html",
        body: `<p class="muted">Suggested action: <code>${esc(action)}</code></p><div class="report">${mdToHtml(
          reply
        )}</div>`,
      };
    }
    case "human_approve":
    case "human_escalate":
      return {
        kind: "text",
        body:
          ev.outcome === "paused"
            ? "Paused — waiting for your approval."
            : `Human decision: ${state.human_decision || "approved"}.`,
      };
    case "deliver":
      return {
        kind: "markdown",
        body: out.final_report || out.final_reply || state.final_reply || state.final_report || state.report,
      };
    default:
      return { kind: "text", body: ev.outcome };
  }
}

function friendlyStepError(raw) {
  const msg = String(raw || "");
  if (!msg) return "";
  if (/Could not parse JSON/i.test(msg)) {
    return "This step had trouble formatting its reply. It will retry automatically when possible.";
  }
  if (/timed\s*out|timeout/i.test(msg)) {
    return "This step timed out waiting for the model. Retrying automatically…";
  }
  if (/Gemini|rate limit|429|503|unavailable/i.test(msg)) {
    return "The model hit a temporary error on this step. Retrying automatically…";
  }
  return "Something went wrong on this step. The run will retry when possible.";
}

function thinkingHtml(nodeName, ev, state) {
  const thoughts = state.agent_thoughts || {};
  const fromState = thoughts[nodeName] || state[`${nodeName}_thinking`] || "";
  const fromOut =
    (ev.output_snapshot &&
      (ev.output_snapshot[`${nodeName}_thinking`] ||
        (ev.output_snapshot.agent_thoughts || {})[nodeName])) ||
    "";
  const text = (fromOut || fromState || "").trim();
  if (ev.outcome === "running" && !text) {
    return `<p class="thinking-text muted">Working on this step…</p>`;
  }
  if (!text) return "";
  return `<p class="thinking-text">${esc(text)}</p>`;
}

function midResultHtml(contrib, nodeName) {
  if (!contrib || !contrib.body) return "";
  let bodyHtml = "";
  if (contrib.kind === "markdown") bodyHtml = `<div class="report">${mdToHtml(contrib.body)}</div>`;
  else if (contrib.kind === "html") bodyHtml = contrib.body;
  else bodyHtml = `<p>${esc(contrib.body)}</p>`;
  const label =
    nodeName === "deliver" ? "Final result" : "Mid result (passed to next agent)";
  // Collapsed by default — user toggles open if they want the artifact.
  return `
    <details class="mid-result">
      <summary>${esc(label)}</summary>
      <div class="mid-result-body">${bodyHtml}</div>
    </details>`;
}

function handoffHtml(fromNode, toNode) {
  const to = AGENT_META[toNode] || { label: toNode || "next" };
  const from = AGENT_META[fromNode] || { label: fromNode || "agent" };
  return `<div class="handoff"><span>Passing from ${esc(from.label)} → ${esc(to.label)}</span></div>`;
}

function retryHandoffHtml(nodeName, nextAttempt) {
  const meta = AGENT_META[nodeName] || { label: nodeName || "agent" };
  return `<div class="handoff handoff-retry"><span>Retrying ${esc(meta.label)} · attempt ${esc(nextAttempt)}</span></div>`;
}

function inlineHitlHtml(run) {
  const state = run.state || {};
  const score = state.score ?? (state.critic_output || {}).score;
  const feedback = state.feedback || (state.critic_output || {}).feedback || "";
  const scoreBit = score != null ? `Critic score: <strong>${esc(score)}/10</strong> (below threshold). ` : "";
  return `
    <div class="chat-hitl" data-inline-hitl="1">
      <p class="chat-hitl-title">Your review needed</p>
      <p class="muted">${scoreBit}Automatic revisions ran out — decide here or use the buttons below.</p>
      ${feedback ? `<p class="feedback">${esc(feedback)}</p>` : ""}
      <details class="mid-result" open>
        <summary>Draft to review</summary>
        <div class="mid-result-body report">${mdToHtml(
          state.report || state.draft_reply || ""
        )}</div>
      </details>
      <textarea class="chat-hitl-comment" id="chat-hitl-comment" placeholder="Required for Request revision…"></textarea>
      <div class="row modal-actions">
        <button type="button" class="ok" data-chat-hitl="approve">Approve &amp; deliver</button>
        <button type="button" class="revise" data-chat-hitl="revise">Request revision</button>
        <button type="button" class="danger" data-chat-hitl="reject">Reject</button>
      </div>
    </div>`;
}

function userBubbleHtml(run) {
  const state = run.state || {};
  const isSupport = run.graph_name === "support_resolution";
  const text = isSupport
    ? `${state.subject || ""}\n\n${state.message || ""}`.trim()
    : state.topic || "";
  if (!text) return "";
  return `
    <div class="user-bubble">
      <div class="user-bubble-inner">${mdToHtml(esc(text))}</div>
    </div>`;
}

function renderUserPrompt(run) {
  const el = document.getElementById("user-prompt");
  if (!el) return;
  el.innerHTML = userBubbleHtml(run);
}

function renderAgentFeed(run) {
  const el = document.getElementById("agent-chat") || document.getElementById("agent-feed");
  if (!el) return;

  // Keep expand/scroll state across poll re-renders so users can read thinking & mid-results.
  const prevOpen = new Set(
    [...el.querySelectorAll("details.agent-step[open]")].map((d) => d.dataset.node)
  );
  const prevMidOpen = new Set(
    [...el.querySelectorAll("details.agent-step details.mid-result[open]")].map(
      (d) => d.closest("details.agent-step")?.dataset.node
    )
  );
  const prevScroll = el.scrollTop;

  el.innerHTML = "";
  const state = run.state || {};
  const SYSTEM_TRACE_NODES = new Set(["workflow_config", "circuit_breaker"]);
  const events = (run.trace || []).filter((ev) => !SYSTEM_TRACE_NODES.has(ev.node_name));
  const nodeOrder = PIPELINE_NODES[run.graph_name] || [];

  if (!events.length && !["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
    el.innerHTML = `<p class="muted chat-empty">Waiting for the first agent to start…</p>`;
    return;
  }
  if (!events.length) {
    el.innerHTML = `
      <div class="agent-card running">
        <div class="agent-avatar">…</div>
        <div class="agent-body">
          <div class="agent-top">
            <span class="agent-name">Starting</span>
            <span class="agent-status running">thinking</span>
          </div>
          <div class="thinking-block live">
            <div class="thinking-label"><span class="thinking-pulse"></span> Thinking</div>
            <p class="thinking-text">Agents are spinning up…</p>
          </div>
        </div>
      </div>`;
    return;
  }

  events.forEach((ev, idx) => {
    const meta = AGENT_META[ev.node_name] || { icon: "?", label: ev.node_name, role: "" };
    const details = document.createElement("details");
    details.className = `agent-step ${ev.outcome}`;
    details.dataset.node = ev.node_name;
    details.dataset.attempt = String(ev.attempt || 1);
    const prefKey = `${run.run_id}:${ev.node_name}:${ev.attempt || 1}:${idx}`;
    const userPref = agentCardOpenPrefs.get(prefKey);
    // Prefer user toggle, then prior open state, then live/paused/error only.
    // Completed runs stay collapsed so the report stays the focus.
    details.open =
      userPref != null
        ? userPref
        : prevOpen.has(ev.node_name) ||
          ev.outcome === "running" ||
          ev.outcome === "paused" ||
          ev.outcome === "error";

    const durationText =
      ev.duration_ms != null
        ? ev.duration_ms < 1000
          ? `${Math.round(ev.duration_ms)} ms`
          : `${(ev.duration_ms / 1000).toFixed(1)}s`
        : "—";
    const attemptNum = Number(ev.attempt) || 1;
    const statusText =
      ev.outcome === "error"
        ? /timed\s*out|timeout/i.test(String(ev.error || ""))
          ? "Timed out"
          : "Failed"
        : ev.outcome === "paused"
        ? "Waiting for you"
        : ev.outcome === "running"
        ? attemptNum > 1
          ? `Retrying… (${attemptNum})`
          : "Thinking…"
        : "Completed";
    const toolText = AGENT_TOOLS[ev.node_name] || meta.role || "None";

    const isToolish = ["web_search", "knowledge_lookup", "source_guard", "deliver"].includes(
      ev.node_name
    );
    const think =
      isToolish || ev.outcome === "paused" ? "" : thinkingHtml(ev.node_name, ev, state);

    const contrib = agentContribution(ev.node_name, ev, state);
    // Failed/timeout attempts are retries — don't imply a handoff to the next agent.
    const showMid =
      ev.outcome !== "running" &&
      ev.outcome !== "error" &&
      contrib.body &&
      !["human_approve", "human_escalate"].includes(ev.node_name);
    let midHtml = showMid ? midResultHtml(contrib, ev.node_name) : "";
    if (ev.node_name === "deliver" && contrib.body && ev.outcome !== "error") {
      midHtml = midResultHtml(
        { kind: contrib.kind === "markdown" ? "markdown" : "text", body: contrib.body },
        "deliver"
      );
    }

    let hitl = "";
    if (
      ev.outcome === "paused" &&
      ["human_approve", "human_escalate"].includes(ev.node_name) &&
      run.status === "PAUSED"
    ) {
      hitl = inlineHitlHtml(run);
    }

    const errHtml = ev.error
      ? `<p class="agent-error">${esc(friendlyStepError(ev.error))}</p>`
      : "";
    const thinkBlock = think || "";

    const bodyInner = `
      ${errHtml}
      ${thinkBlock}
      ${midHtml}
      ${hitl}
    `;

    const cardTitle =
      ["You", "Deliver"].includes(meta.label) || /\bAgent\b/i.test(meta.label)
        ? meta.label
        : `${meta.label} Agent`;
    const attemptLabel = attemptNum > 1 ? ` · attempt ${attemptNum}` : "";

    details.innerHTML = `
      <summary class="agent-summary">
        <span class="agent-card-icon" aria-hidden="true">${esc(meta.icon)}</span>
        <span class="agent-card-info">
          <span class="agent-card-title">${esc(cardTitle)}${esc(attemptLabel)}</span>
          <span class="agent-card-meta">
            <span><b>Status:</b> ${esc(statusText)}</span>
            <span><b>Duration:</b> ${esc(durationText)}</span>
            <span><b>Tools:</b> ${esc(toolText)}</span>
          </span>
          <span class="agent-view-reasoning">
            <span class="agent-caret" aria-hidden="true">▾</span>
            ${thinkBlock ? "View reasoning" : "View result"}
          </span>
        </span>
        <button class="agent-copy" type="button" title="Copy agent output" aria-label="Copy ${esc(
          meta.label
        )} output">⧉</button>
      </summary>
      <div class="agent-step-body">${bodyInner}</div>
    `;

    // Restore mid-result only if the user had it open; keep collapsed by default.
    if (prevMidOpen.has(ev.node_name)) {
      details.querySelectorAll("details.mid-result").forEach((m) => {
        m.open = true;
      });
    }

    details.addEventListener("toggle", () => {
      agentCardOpenPrefs.set(prefKey, details.open);
      if (details.open) {
        // Surface reasoning when expanding a card; mid-results stay user-toggled.
        details.querySelectorAll("details.thinking-block").forEach((m) => {
          m.open = true;
        });
        details.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });

    el.appendChild(details);

    if (idx < events.length - 1) {
      const nextEv = events[idx + 1];
      if (nextEv.node_name === ev.node_name) {
        el.insertAdjacentHTML(
          "beforeend",
          retryHandoffHtml(ev.node_name, nextEv.attempt || attemptNum + 1)
        );
      } else {
        el.insertAdjacentHTML("beforeend", handoffHtml(ev.node_name, nextEv.node_name));
      }
    } else if (ev.outcome === "success" && run.status === "RUNNING") {
      const pos = nodeOrder.indexOf(ev.node_name);
      const guess = pos >= 0 ? nodeOrder[pos + 1] : null;
      if (guess) el.insertAdjacentHTML("beforeend", handoffHtml(ev.node_name, guess));
    }
  });

  el.scrollTop = prevScroll;
}

function renderReport(run) {
  const el = document.getElementById("report-view");
  const section = document.getElementById("answer-section");
  const state = run.state || {};
  const dlBtn = document.getElementById("download-md-btn");
  const printBtn = document.getElementById("print-btn");
  // Only surface the standalone answer card once the run is delivered;
  // mid-run drafts live inside the chat bubbles.
  const report =
    run.status === "COMPLETED"
      ? state.final_report || state.final_reply || state.report || state.draft_reply
      : "";
  if (report) {
    section?.classList.remove("hidden");
    el.classList.remove("empty");
    el.innerHTML = mdToHtml(report);
    currentReportText = report;
    currentTopic = state.topic || state.subject || "output";
    if (dlBtn) dlBtn.disabled = false;
    if (printBtn) printBtn.disabled = false;
  } else {
    section?.classList.add("hidden");
    el.classList.add("empty");
    el.textContent = "No output yet — it appears after agents produce a draft/reply.";
    currentReportText = "";
    if (dlBtn) dlBtn.disabled = true;
    if (printBtn) printBtn.disabled = true;
  }
}

function renderTrace(run) {
  const el = document.getElementById("trace");
  el.innerHTML = "";
  const events = [...(run.trace || [])].reverse();
  for (const ev of events) {
    const item = document.createElement("div");
    item.className = "trace-item";
    const ms = ev.duration_ms != null ? `${Math.round(ev.duration_ms)}ms` : "—";
    const diffKeys = Object.keys(ev.state_diff || {});
    item.innerHTML = `
      <header>
        <span>${esc(ev.node_name)} · attempt ${esc(ev.attempt)} · ${esc(ev.outcome)}</span>
        <span>${esc(ms)}</span>
      </header>
      ${
        diffKeys.length
          ? `<p class="diff">changed: ${diffKeys.map((k) => `<code>${esc(k)}</code>`).join(" ")}</p>`
          : ""
      }
    `;
    if (ev.error) {
      const p = document.createElement("p");
      p.className = "err";
      p.textContent = ev.error;
      item.appendChild(p);
    }
    el.appendChild(item);
  }
}

function openHitlModal() {
  const modal = document.getElementById("hitl-modal");
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
}

function closeHitlModal() {
  const modal = document.getElementById("hitl-modal");
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

function renderHitl(run) {
  const preview = document.getElementById("hitl-preview");
  const pausedBanner = document.getElementById("paused-banner");
  const deliveredBanner = document.getElementById("delivered-banner");
  const state = run.state || {};

  deliveredBanner.classList.add("hidden");
  pausedBanner.classList.add("hidden");

  // Final answer section is enough — skip the redundant "Ready" banner.
  if (run.status === "COMPLETED" && (state.final_report || state.final_reply || state.report || state.delivered)) {
    if (lastStatus !== "COMPLETED") {
      switchTab("answer");
      if (state.source === "zendesk") {
        refreshZendesk().catch(() => {});
      }
    }
  }

  if (run.status === "PAUSED") {
    pausedBanner.classList.remove("hidden");
    const wasRevised =
      !!state.pending_human_revision || Number(state.human_revision_count || 0) > 0;
    pausedBanner.querySelector("strong").textContent = wasRevised
      ? "Revised — review again"
      : "Needs your review";
    pausedBanner.querySelector("p").textContent = wasRevised
      ? "The draft was updated from your notes. Approve to finish, or request another revision."
      : "To change the draft, use Request revision with notes (Approve alone does not rewrite).";
    document.getElementById("hitl-message").textContent = wasRevised
      ? "This is a revised draft after your feedback. Review it again, then approve or revise."
      : "Please review the draft. Comments only apply when you click Request revision.";

    const critic = state.critic_output || {};
    const score = critic.score ?? state.score;
    const feedback = critic.feedback ?? state.feedback;
    const approved = critic.approved ?? state.approved;
    const humanNote = (state.human_feedback || "").trim();
    const report =
      state.draft_reply ||
      state.preliminary_reply ||
      state.report ||
      state.final_report ||
      "";

    let criticHtml = "";
    if (score != null || feedback) {
      const badge = approved
        ? `<span class="verdict ok">LOOKS GOOD</span>`
        : `<span class="verdict bad">NEEDS IMPROVEMENT</span>`;
      criticHtml = `
        <div class="critic-head">${badge}${
          score != null ? `<span class="score">Score: ${esc(score)}/10</span>` : ""
        }</div>
        ${feedback ? `<p class="feedback">${esc(feedback)}</p>` : ""}
      `;
    }

    const routeBits = [
      state.intent ? `Intent: ${state.intent}` : "",
      state.frustrated ? "Frustrated: yes" : "",
      state.urgency ? `Urgency: ${state.urgency}` : "",
      state.suggested_action ? `Action: ${state.suggested_action}` : "",
    ]
      .filter(Boolean)
      .join(" · ");

    preview.innerHTML = `
      ${
        humanNote
          ? `<div class="revision-note"><strong>Your revision notes</strong><p>${esc(humanNote)}</p></div>`
          : ""
      }
      ${criticHtml}
      ${routeBits ? `<p class="muted">${esc(routeBits)}</p>` : ""}
      ${(() => {
        const thoughts = state.agent_thoughts || {};
        const keys = Object.keys(thoughts);
        if (!keys.length) return "";
        const lastKey = keys[keys.length - 1];
        return `<details class="thinking-block"><summary class="thinking-label">Reasoning</summary><p class="thinking-text">${esc(thoughts[lastKey])}</p></details>`;
      })()}
      <h4 class="preview-title">${
        run.graph_name === "support_resolution"
          ? "Draft reply"
          : "Draft answer"
      }</h4>
      <div class="report hitl-report">${
        report ? mdToHtml(report) : "<p class='muted'>No draft text in state yet.</p>"
      }</div>
    `;

    // Prefer inline chat HITL; don't auto-open the modal.
    if (modalAutoOpenedFor !== run.run_id) {
      modalAutoOpenedFor = run.run_id;
      document.getElementById("agent-chat")?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  } else if ((run.error || "").includes("resume") && run.status !== "COMPLETED") {
    pausedBanner.classList.remove("hidden");
    pausedBanner.querySelector("strong").textContent = "Run interrupted";
    pausedBanner.querySelector("p").textContent =
      "You can resume from the last checkpoint.";
    document.getElementById("hitl-message").textContent =
      run.error || "Run interrupted — you can resume from the last checkpoint.";
    preview.innerHTML = `<p class="muted">${esc(run.error || "")}</p>`;
  } else {
    closeHitlModal();
    if (run.status !== "PAUSED") {
      modalAutoOpenedFor = null;
    }
  }
}

function switchTab(tab) {
  activeTab = tab;
  // Legacy no-op kept for older call sites; answer is always primary now.
  if (tab === "report" || tab === "answer") {
    document.getElementById("report-view")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (tab === "agents") {
    document.getElementById("agent-chat")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (tab === "trace" || tab === "state") {
    document.getElementById("tech-panel")?.setAttribute("open", "");
    document.getElementById("tech-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function sourceHostname(href) {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function collectRunSources(run) {
  const state = run.state || {};
  const raw = state.search_results || [];
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const out = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const href = String(item.href || "").trim();
    if (!href || seen.has(href)) continue;
    seen.add(href);
    out.push({
      title: String(item.title || href).trim(),
      href,
      host: sourceHostname(href),
      body: String(item.body || "").trim(),
    });
  }
  return out;
}

function renderSources(run) {
  const panel = document.getElementById("right-panel");
  const list = document.getElementById("sources-list");
  const countEl = document.getElementById("sources-count");
  const emptyEl = document.getElementById("sources-empty");
  const fold = document.getElementById("resources-fold");
  if (!panel || !list) return;

  // Always show the right rail while a run is open (agents live here).
  panel.classList.remove("hidden");
  document.querySelector(".chat-app")?.classList.add("has-sources");

  const sources = collectRunSources(run);
  list.innerHTML = "";
  if (countEl) countEl.textContent = `(${sources.length})`;

  // Keep Resources collapsed by default so Agents stay fully visible.
  // User can expand the toggle when they want sources.
  if (fold && fold.dataset.userToggled !== "1") fold.open = false;

  if (!sources.length) {
    if (emptyEl) emptyEl.classList.remove("hidden");
    return;
  }

  if (emptyEl) emptyEl.classList.add("hidden");

  sources.forEach((s, idx) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <a href="${esc(s.href)}" target="_blank" rel="noreferrer" class="source-link">
        <span class="source-index">${idx + 1}</span>
        <span class="source-copy">
          <span class="source-title">${esc(s.title)}</span>
          <span class="source-host">${esc(s.host || s.href)}</span>
        </span>
      </a>`;
    list.appendChild(li);
  });
}

function hideSourcesPanel() {
  document.getElementById("right-panel")?.classList.add("hidden");
  document.querySelector(".chat-app")?.classList.remove("has-sources");
  const list = document.getElementById("sources-list");
  if (list) list.innerHTML = "";
  const countEl = document.getElementById("sources-count");
  if (countEl) countEl.textContent = "(0)";
  const fold = document.getElementById("resources-fold");
  if (fold) {
    fold.open = false;
    delete fold.dataset.userToggled;
  }
  const agents = document.getElementById("agent-chat");
  if (agents) agents.innerHTML = "";
  const prompt = document.getElementById("user-prompt");
  if (prompt) prompt.innerHTML = "";
}

const EXEC_LOG_STEPS = {
  research_report: [
    { node: "planner", label: "Planning execution path..." },
    { node: "knowledge_lookup", label: "Reading knowledge docs..." },
    { node: "researcher", label: "Researching (ReAct tools)..." },
    { node: "writer", label: "Drafting report..." },
    { node: "source_guard", label: "Verifying citations..." },
    { node: "critic", label: "Checking quality..." },
    { node: "fixer", label: "Fixing agent failure..." },
    { node: "human_approve", label: "Waiting for your review..." },
    { node: "deliver", label: "Delivering answer..." },
  ],
  support_resolution: [
    { node: "knowledge_lookup", label: "Reading knowledge docs..." },
    { node: "frontline", label: "Triaging ticket..." },
    { node: "sentiment", label: "Assessing tone..." },
    { node: "faq_agent", label: "Drafting FAQ reply..." },
    { node: "technical_agent", label: "Drafting technical reply..." },
    { node: "billing_agent", label: "Drafting billing reply..." },
    { node: "quality_critic", label: "Verifying reply quality..." },
    { node: "fixer", label: "Fixing agent failure..." },
    { node: "human_escalate", label: "Waiting for your review..." },
    { node: "deliver", label: "Sending reply..." },
  ],
};

function renderLiveLog(run) {
  const panel = document.getElementById("live-log");
  const list = document.getElementById("live-log-list");
  if (!panel || !list) return;

  const steps = EXEC_LOG_STEPS[run.graph_name] || EXEC_LOG_STEPS.research_report;
  const events = run.trace || [];
  const byNode = {};
  for (const ev of events) {
    // Keep the latest event for each node.
    byNode[ev.node_name] = ev;
  }

  // Only show steps that appear in this run's path or are currently running.
  const touched = new Set(Object.keys(byNode));
  if (run.current_node) touched.add(run.current_node);

  const visible = steps.filter((step) => {
    // Always show steps once anything has started, but hide specialist branches
    // that never ran (support FAQ vs billing etc.).
    if (["faq_agent", "technical_agent", "billing_agent"].includes(step.node)) {
      return touched.has(step.node);
    }
    if (step.node === "fixer") {
      return touched.has(step.node);
    }
    if (step.node === "human_approve" || step.node === "human_escalate") {
      return touched.has(step.node) || run.status === "PAUSED";
    }
    return true;
  });

  list.innerHTML = "";
  let any = false;
  for (const step of visible) {
    const ev = byNode[step.node];
    let cls = "pending";
    let mark = "○";
    if (ev?.outcome === "success") {
      cls = "done";
      mark = "✓";
    } else if (ev?.outcome === "error") {
      cls = "error";
      mark = "✕";
    } else if (ev?.outcome === "paused" || (run.status === "PAUSED" && run.current_node === step.node)) {
      cls = "paused";
      mark = "★";
    } else if (
      ev?.outcome === "running" ||
      (["RUNNING", "RETRYING", "PENDING"].includes(run.status) && run.current_node === step.node)
    ) {
      cls = "active";
      mark = "●";
    } else if (!ev && !touched.has(step.node) && !events.length) {
      // Before first event, keep pending.
      cls = "pending";
    } else if (!ev) {
      // Not reached yet (or skipped branch already filtered).
      cls = "pending";
    }

    // Hide untouched future steps until the run has at least started,
    // but once started show the pipeline ahead as pending.
    if (!events.length && !["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
      continue;
    }

    any = true;
    const li = document.createElement("li");
    li.className = cls;
    const label =
      cls === "active" ? step.label : step.label.replace(/\.\.\.$/, cls === "done" ? "" : "...");
    li.innerHTML = `<span class="log-mark" aria-hidden="true">${mark}</span><span class="log-text">${esc(
      label || step.label
    )}</span>`;
    list.appendChild(li);
  }

  panel.classList.toggle("hidden", !any);
}

function hideLiveLog() {
  document.getElementById("live-log")?.classList.add("hidden");
  const list = document.getElementById("live-log-list");
  if (list) list.innerHTML = "";
}

function defaultWorkspaceView(status) {
  if (status === "COMPLETED") return "report";
  if (status === "PAUSED") return "activity";
  if (["RUNNING", "RETRYING", "PENDING"].includes(status)) return "activity";
  return "report";
}

function setWorkspaceView(view, { persist = true, status = null } = {}) {
  workspaceView = view === "activity" ? "activity" : "report";
  if (persist && selectedRunId) {
    workspaceViewPrefs.set(selectedRunId, workspaceView);
  }

  const app = document.querySelector(".chat-app");
  const switcher = document.getElementById("view-switcher");
  const form = document.getElementById("start-form");
  const right = document.getElementById("right-panel");
  const st = status || lastStatus;

  switcher?.classList.remove("hidden");
  switcher?.querySelectorAll(".view-tab").forEach((btn) => {
    const on = btn.dataset.view === workspaceView;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });

  app?.classList.toggle("view-report", workspaceView === "report");
  app?.classList.toggle("view-activity", workspaceView === "activity");
  // Hide composer while inspecting a run — use + New task instead.
  form?.classList.add("composer-tucked");

  if (workspaceView === "report") {
    right?.classList.add("hidden");
    app?.classList.remove("has-sources");
  } else {
    right?.classList.remove("hidden");
    app?.classList.add("has-sources");
  }

  // Completed + Report: hide process chrome. Activity keeps the checklist/agents.
  if (st === "COMPLETED" && workspaceView === "report") {
    document.getElementById("simple-progress")?.classList.add("hidden");
    document.getElementById("live-log")?.classList.add("hidden");
    document.getElementById("delivered-banner")?.classList.add("hidden");
    document.getElementById("user-prompt")?.classList.add("prompt-compact");
  } else {
    document.getElementById("user-prompt")?.classList.remove("prompt-compact");
    if (st === "COMPLETED" && workspaceView === "activity") {
      document.getElementById("simple-progress")?.classList.remove("hidden");
    }
  }
}

function applyRunLayout(run) {
  const status = run.status;
  const preferred =
    (selectedRunId && workspaceViewPrefs.get(selectedRunId)) ||
    defaultWorkspaceView(status);
  setWorkspaceView(preferred, { persist: false, status });
}

document.getElementById("view-switcher")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".view-tab");
  if (!btn || !btn.dataset.view) return;
  setWorkspaceView(btn.dataset.view);
});

async function selectRun(runId) {
  selectedRunId = runId;
  showEmptyState(false);
  await refreshRuns();
  const run = await api(`/api/runs/${runId}`);
  const title = run.state?.topic || run.state?.subject || "Untitled task";
  document.getElementById("detail-title").textContent = title;
  const kicker = document.getElementById("detail-kicker");
  if (kicker) {
    kicker.textContent =
      run.graph_name === "support_resolution" ? "Support reply" : "Research report";
  }
  const pill = document.getElementById("status-pill");
  pill.textContent = STATUS_LABELS[run.status] || run.status;
  pill.className = `pill ${run.status}`;

  const revisions = run.state?.revision_count ? `${run.state.revision_count} revision(s) · ` : "";
  document.getElementById("detail-meta").textContent =
    `${revisions}id ${run.run_id} · step ${run.step} · updated ${fmtTime(run.updated_at)}`;

  renderUserPrompt(run);
  renderSimpleProgress(run);
  renderLiveLog(run);
  renderAgentFeed(run);
  renderReport(run);
  renderTrace(run);
  renderHitl(run);
  renderSources(run);
  renderCostTelemetry(run);
  applyRunLayout(run);
  lastStatus = run.status;
  document.getElementById("state-view").textContent = JSON.stringify(run.state, null, 2);

  if (pollTimer) clearInterval(pollTimer);
  if (["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
    pollTimer = setInterval(() => selectRun(runId), 2000);
  }
}

document.getElementById("start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pipeline = document.getElementById("pipeline").value || "research_report";
  const input = document.getElementById("composer-input");
  const text = (input?.value || "").trim();
  let payload;
  if (pipeline === "support_resolution") {
    payload = {
      graph: "support_resolution",
      subject: document.getElementById("ticket-subject").value.trim(),
      message: text,
      customer_name: document.getElementById("customer-name").value.trim() || "Customer",
      source: "manual",
    };
    if (!payload.subject || !payload.message) {
      alert("Enter the ticket subject (above the box) and the customer message.");
      return;
    }
  } else {
    if (text.length < 2) {
      alert("Type what you want researched.");
      return;
    }
    payload = {
      graph: "research_report",
      topic: text,
      report_type: document.getElementById("report-type").value || "general",
    };
  }
  const advanced = readAdvancedConfig(pipeline);
  if (advanced.ui_preset) payload.ui_preset = advanced.ui_preset;
  if (advanced.config_override) payload.config_override = advanced.config_override;
  const data = await api("/api/runs", {
    method: "POST",
    headers: { "Idempotency-Key": newIdempotencyKey() },
    body: JSON.stringify(payload),
  });
  input.value = "";
  input.style.height = "";
  resetAdvancedToggles();
  await refreshRuns();
  await selectRun(data.run_id);
});

// Composer behaves like a chat box: Enter sends, Shift+Enter is a newline.
const composerInput = document.getElementById("composer-input");
composerInput?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    document.getElementById("start-form").requestSubmit();
  }
});
composerInput?.addEventListener("input", () => {
  composerInput.style.height = "auto";
  composerInput.style.height = `${Math.min(composerInput.scrollHeight, 180)}px`;
});

function showEmptyState(show) {
  document.getElementById("empty-state")?.classList.toggle("hidden", !show);
  document.getElementById("simple-progress")?.classList.toggle("hidden", show);
  // Hero mode: top-aligned landing (workflow picker + composer).
  document.querySelector(".chat-main")?.classList.toggle("hero", show);
  // Keep technical details out of the chat surface.
  document.getElementById("tech-panel")?.classList.add("hidden");
  if (show) {
    hideSourcesPanel();
    hideLiveLog();
    hideCostTelemetry();
    document.getElementById("answer-section")?.classList.add("hidden");
    document.getElementById("delivered-banner")?.classList.add("hidden");
    document.getElementById("paused-banner")?.classList.add("hidden");
    document.getElementById("view-switcher")?.classList.add("hidden");
    document.getElementById("start-form")?.classList.remove("composer-tucked");
    document.querySelector(".chat-app")?.classList.remove("view-report", "view-activity", "has-sources");
    document.getElementById("user-prompt")?.classList.remove("prompt-compact");
    workspaceView = null;
    const chat = document.getElementById("agent-chat");
    if (chat) chat.innerHTML = "";
    const prompt = document.getElementById("user-prompt");
    if (prompt) prompt.innerHTML = "";
    // New task / landing always starts at workflow picker.
    resetWorkflowPicker();
  } else {
    document.getElementById("start-form")?.classList.remove("composer-awaiting");
  }
}

function resetWorkflowPicker() {
  selectedWorkflow = null;
  document.getElementById("workflow-picker")?.classList.remove("hidden");
  document.getElementById("compose-hero")?.classList.add("hidden");
  document.getElementById("start-form")?.classList.add("composer-awaiting");
  document.querySelector(".composer-pipeline-wrap")?.classList.remove("hidden");
  document.querySelectorAll(".workflow-card").forEach((btn) => btn.classList.remove("selected"));
  if (composerInput) {
    composerInput.value = "";
    composerInput.style.height = "";
  }
  resetAdvancedToggles();
}

function selectWorkflow(workflowId) {
  const sel = document.getElementById("pipeline");
  if (!sel) return;
  selectedWorkflow = workflowId;
  if ([...sel.options].some((o) => o.value === workflowId)) {
    sel.value = workflowId;
  }
  syncPipelineForm();

  document.getElementById("workflow-picker")?.classList.add("hidden");
  document.getElementById("compose-hero")?.classList.remove("hidden");
  document.getElementById("start-form")?.classList.remove("composer-awaiting");

  document.querySelectorAll(".workflow-card").forEach((btn) => {
    btn.classList.toggle("selected", btn.dataset.workflow === workflowId);
  });

  const isSupport = workflowId === "support_resolution";
  // Workflow already chosen on the picker — hide the redundant select.
  document.querySelector(".composer-pipeline-wrap")?.classList.add("hidden");

  const desc = document.getElementById("compose-hero-desc");
  const detailTitle = document.getElementById("detail-title");
  const kicker = document.getElementById("detail-kicker");
  if (isSupport) {
    if (desc)
      desc.textContent =
        "Describe the customer issue — agents will draft a policy-aware reply.";
    if (detailTitle) detailTitle.textContent = "Customer support ticket";
    if (kicker) kicker.textContent = "Support workflow";
  } else {
    if (desc) desc.textContent = "Ask anything for a detailed report at one place.";
    if (detailTitle) detailTitle.textContent = "Research report";
    if (kicker) kicker.textContent = "Research workflow";
  }

  composerInput?.focus();
}

document.getElementById("workflow-picker")?.addEventListener("click", (e) => {
  const card = e.target.closest("[data-workflow]");
  if (!card) return;
  selectWorkflow(card.getAttribute("data-workflow"));
});

document.getElementById("change-workflow-btn")?.addEventListener("click", () => {
  resetWorkflowPicker();
  document.getElementById("detail-title").textContent = "Ready when you are.";
  document.getElementById("detail-kicker").textContent = "New task";
  document.querySelector(".composer-pipeline-wrap")?.classList.remove("hidden");
});

document.getElementById("live-log-copy")?.addEventListener("click", async () => {
  const lines = [...document.querySelectorAll("#live-log-list li")]
    .map((li) => li.innerText.trim())
    .join("\n");
  if (!lines) return;
  await navigator.clipboard.writeText(lines);
  const btn = document.getElementById("live-log-copy");
  if (!btn) return;
  btn.textContent = "✓";
  setTimeout(() => {
    btn.textContent = "⧉";
  }, 1000);
});

document.getElementById("sources-copy-btn")?.addEventListener("click", async (e) => {
  e.preventDefault();
  e.stopPropagation();
  const links = [...document.querySelectorAll("#sources-list .source-link")]
    .map((a, i) => `${i + 1}. ${a.querySelector(".source-title")?.textContent || ""} — ${a.href}`)
    .join("\n");
  if (!links) return;
  await navigator.clipboard.writeText(links);
  const btn = document.getElementById("sources-copy-btn");
  if (!btn) return;
  btn.textContent = "✓";
  setTimeout(() => {
    btn.textContent = "⧉";
  }, 1000);
});

document.getElementById("resources-fold")?.addEventListener("toggle", (e) => {
  const fold = e.currentTarget;
  if (fold instanceof HTMLDetailsElement) fold.dataset.userToggled = "1";
});

document.getElementById("new-task-btn")?.addEventListener("click", () => {
  selectedRunId = null;
  agentCardOpenPrefs.clear();
  if (pollTimer) clearInterval(pollTimer);
  lastStatus = null;
  document.getElementById("detail-title").textContent = "Ready when you are.";
  document.getElementById("detail-kicker").textContent = "New task";
  const pill = document.getElementById("status-pill");
  pill.textContent = "—";
  pill.className = "pill";
  showEmptyState(true);
  refreshRuns().catch(() => {});
});

function syncPipelineForm() {
  const id = document.getElementById("pipeline").value;
  currentPipelineId = id;
  const meta = pipelines.find((p) => p.id === id);
  document.getElementById("pipeline-desc").textContent = meta?.description || "";
  const support = document.getElementById("support-fields");
  const isSupport = id === "support_resolution";
  support.classList.toggle("hidden", !isSupport);
  document.getElementById("report-type-wrap")?.classList.toggle("hidden", isSupport);
  if (composerInput) {
    composerInput.placeholder = isSupport
      ? "Paste the customer's message — e.g. I've been charged twice for Pro, order #48291…"
      : "Ask anything — e.g. Edge AI market for industrial IoT";
  }

  // Knowledge upload and Zendesk are support-pipeline concerns.
  document.getElementById("knowledge-panel")?.classList.toggle("hidden", !isSupport);
  document.getElementById("integrations-panel")?.classList.toggle("hidden", !isSupport);
  if (isSupport) {
    refreshKnowledge().catch(() => {});
    refreshZendesk().catch(() => {});
  }
  syncAdvancedPanel(id);
}

function syncAdvancedPanel(workflowId) {
  const deepWrap = document.getElementById("adv-deep-research");
  const forceWrap = document.getElementById("adv-force-human");
  const budgetWrap = document.getElementById("adv-token-budget");
  const empty = document.getElementById("advanced-empty");
  const budgetSel = document.getElementById("token-budget-select");
  const isResearch = workflowId === "research_report";
  const isSupport = workflowId === "support_resolution";
  deepWrap?.classList.toggle("hidden", !isResearch);
  forceWrap?.classList.toggle("hidden", !isSupport);
  budgetWrap?.classList.toggle("hidden", !(isResearch || isSupport));
  empty?.classList.add("hidden");
  if (budgetSel && !budgetSel.dataset.userSet) {
    budgetSel.value = isSupport ? "4000" : "8000";
  }
  updateAdvancedEstimates();
}

function resetAdvancedToggles() {
  const deep = document.getElementById("deep-research-toggle");
  const force = document.getElementById("force-human-toggle");
  const budgetSel = document.getElementById("token-budget-select");
  if (deep) deep.checked = false;
  if (force) force.checked = false;
  if (budgetSel) {
    delete budgetSel.dataset.userSet;
    const pipeline = document.getElementById("pipeline")?.value || "research_report";
    budgetSel.value = pipeline === "support_resolution" ? "4000" : "8000";
  }
  updateAdvancedEstimates();
}

function updateAdvancedEstimates() {
  const deepOn = !!document.getElementById("deep-research-toggle")?.checked;
  const forceOn = !!document.getElementById("force-human-toggle")?.checked;
  const budgetSel = document.getElementById("token-budget-select");
  const deepEst = document.getElementById("deep-research-estimate");
  const forceEst = document.getElementById("force-human-estimate");
  // Deep Research suggests 15k once — only if the user hasn't picked a budget yet.
  if (deepOn && budgetSel && !budgetSel.dataset.userSet) {
    budgetSel.value = "15000";
  }
  if (deepEst) deepEst.textContent = deepOn ? "on" : "off";
  if (forceEst) forceEst.textContent = forceOn ? "pause" : "auto";
}

function readAdvancedConfig(pipeline) {
  const out = {};
  const budgetSel = document.getElementById("token-budget-select");
  const tokens = Number(budgetSel?.value || 0);
  const defaults = {
    research_report: 8000,
    support_resolution: 4000,
  };
  const workflowDefault = defaults[pipeline] ?? 8000;
  if (tokens > 0 && tokens !== workflowDefault) {
    out.config_override = {
      budget: { max_tokens_total: Math.min(tokens, 50000) },
    };
  } else if (tokens > 0 && tokens === workflowDefault && budgetSel?.dataset.userSet) {
    // Explicitly confirm default when user touched the control.
    out.config_override = {
      budget: { max_tokens_total: tokens },
    };
  }

  if (pipeline === "research_report") {
    const on = !!document.getElementById("deep-research-toggle")?.checked;
    if (on) {
      out.ui_preset = "deep_research";
      // Keep user's token pick — deep preset also sets 15k, so re-apply override after.
      if (tokens > 0) {
        out.config_override = {
          ...(out.config_override || {}),
          budget: {
            ...((out.config_override && out.config_override.budget) || {}),
            max_tokens_total: Math.min(tokens, 50000),
          },
          features: { debate_loop: true },
        };
      }
    }
  }
  if (pipeline === "support_resolution") {
    const on = !!document.getElementById("force-human-toggle")?.checked;
    if (on) out.ui_preset = "force_human_review";
  }
  return out;
}

document.getElementById("deep-research-toggle")?.addEventListener("change", updateAdvancedEstimates);
document.getElementById("force-human-toggle")?.addEventListener("change", updateAdvancedEstimates);
document.getElementById("token-budget-select")?.addEventListener("change", () => {
  const sel = document.getElementById("token-budget-select");
  if (sel) sel.dataset.userSet = "1";
  updateAdvancedEstimates();
});

async function loadPipelines() {
  const sel = document.getElementById("pipeline");
  try {
    const data = await api("/api/pipelines");
    pipelines = data.pipelines || [];
  } catch {
    pipelines = [
      { id: "research_report", label: "Research report", description: "", input_mode: "research" },
      {
        id: "support_resolution",
        label: "Customer Support Resolution Network",
        description: "",
        input_mode: "support",
      },
    ];
  }
  sel.innerHTML = "";
  for (const p of pipelines) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", syncPipelineForm);
  syncPipelineForm();
}
loadPipelines();

async function loadReportTypes() {
  const sel = document.getElementById("report-type");
  if (!sel) return;
  try {
    const data = await api("/api/report-types");
    sel.innerHTML = "";
    for (const rt of data.report_types || []) {
      const opt = document.createElement("option");
      opt.value = rt.id;
      opt.textContent = rt.label;
      sel.appendChild(opt);
    }
  } catch {
    sel.innerHTML = '<option value="general">General research brief</option>';
  }
}
loadReportTypes();

async function refreshKnowledge() {
  const list = document.getElementById("knowledge-list");
  const status = document.getElementById("knowledge-status");
  if (!list) return;
  try {
    const data = await api("/api/knowledge/documents");
    const docs = data.documents || [];
    status.textContent = docs.length
      ? `${docs.length} document(s) available to agents`
      : "No documents yet — upload a policy or FAQ to ground replies.";
    list.innerHTML = "";
    for (const d of docs) {
      const li = document.createElement("li");
      li.innerHTML = `<span><strong>${esc(d.filename)}</strong> · ${esc(d.char_count)} chars · ${esc(
        d.chunk_count
      )} chunks</span>`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost danger-text";
      btn.textContent = "Delete";
      btn.addEventListener("click", async () => {
        await api(`/api/knowledge/documents/${d.doc_id}`, { method: "DELETE" });
        await refreshKnowledge();
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
  } catch (err) {
    status.textContent = String(err);
  }
}

document.getElementById("knowledge-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("knowledge-file");
  const status = document.getElementById("knowledge-status");
  const file = input?.files?.[0];
  if (!file) {
    status.textContent = "Choose a file first.";
    return;
  }
  status.textContent = "Uploading…";
  const body = new FormData();
  body.append("file", file);
  const res = await fetch("/api/knowledge/documents", {
    method: "POST",
    credentials: "same-origin",
    body,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    status.textContent = data.detail || "Upload failed";
    return;
  }
  input.value = "";
  status.textContent = `Uploaded ${data.document?.filename} (${data.document?.chunk_count} chunks)`;
  await refreshKnowledge();
});

// --- Zendesk simulator UI ---

async function refreshZendesk() {
  const badge = document.getElementById("zd-badge");
  const statusEl = document.getElementById("zd-status");
  const connectedBlock = document.getElementById("zd-connected-block");
  const connectBtn = document.getElementById("zd-connect-btn");
  const disconnectBtn = document.getElementById("zd-disconnect-btn");
  const ticketList = document.getElementById("zd-ticket-list");
  const deliveryList = document.getElementById("zd-delivery-list");
  if (!badge) return;

  try {
    const st = await api("/api/integrations/zendesk");
    badge.textContent = st.connected ? "connected · simulator" : "disconnected";
    badge.className = `pill ${st.connected ? "COMPLETED" : ""}`;
    statusEl.textContent = st.connected
      ? `Connected to ${st.subdomain}.zendesk.com (simulated) · ${st.open_tickets} open ticket(s)`
      : "Not connected — click Connect to load sample inbound tickets.";
    connectedBlock.classList.toggle("hidden", !st.connected);
    connectBtn.classList.toggle("hidden", st.connected);
    disconnectBtn.classList.toggle("hidden", !st.connected);

    if (!st.connected) {
      if (ticketList) ticketList.innerHTML = "";
      if (deliveryList) deliveryList.innerHTML = "";
      return;
    }

    const [ticketsData, deliveriesData] = await Promise.all([
      api("/api/integrations/zendesk/tickets?open_only=false"),
      api("/api/integrations/zendesk/deliveries"),
    ]);
    const tickets = ticketsData.tickets || [];
    const deliveries = deliveriesData.deliveries || [];

    if (ticketList) {
      ticketList.innerHTML = "";
      if (!tickets.length) {
        ticketList.innerHTML = "<li><span class='muted'>No tickets yet</span></li>";
      }
      for (const t of tickets) {
        const li = document.createElement("li");
        const solved = t.status === "solved";
        const statusHtml = solved
          ? `<span class="zd-solved">solved · reply sent</span>`
          : `<em>${esc(t.status)}</em>`;
        li.innerHTML = `<span><strong>${esc(t.external_ticket_id)}</strong> · ${esc(
          t.subject
        )} · ${statusHtml}<br/><span class="muted">${esc(t.customer_name)} · ${esc(
          t.customer_plan
        )}</span></span>`;
        const btn = document.createElement("button");
        btn.type = "button";
        if (solved) {
          btn.textContent = "Delivered";
          btn.disabled = true;
          btn.className = "ghost";
        } else {
          btn.textContent = "Resolve";
          btn.addEventListener("click", () => startZendeskTicket(t.external_ticket_id));
        }
        li.appendChild(btn);
        ticketList.appendChild(li);
      }
    }

    if (deliveryList) {
      deliveryList.innerHTML = "";
      if (!deliveries.length) {
        deliveryList.innerHTML =
          "<li><span class='muted'>No outbound deliveries yet — approve a Zendesk-sourced run to see one.</span></li>";
      }
      for (const d of deliveries) {
        const li = document.createElement("li");
        li.innerHTML = `<span><strong>${esc(d.external_ticket_id)}</strong> · HTTP ${esc(
          d.http_status
        )} · ${esc(d.status)}<br/><span class="muted">${esc(d.detail || "")} · ${esc(
          d.created_at || ""
        )}</span></span>`;
        deliveryList.appendChild(li);
      }
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = String(err);
  }
}

async function startZendeskTicket(ticketId) {
  if (!ticketId) {
    alert("Pick a Zendesk ticket first.");
    return;
  }
  const data = await api(`/api/integrations/zendesk/tickets/${encodeURIComponent(ticketId)}/start-run`, {
    method: "POST",
    body: "{}",
  });
  switchTab("answer");
  await refreshRuns();
  await selectRun(data.run_id);
  await refreshZendesk();
}

document.getElementById("zd-connect-btn")?.addEventListener("click", async () => {
  await api("/api/integrations/zendesk/connect", {
    method: "POST",
    body: JSON.stringify({ subdomain: "acme-demo" }),
  });
  await refreshZendesk();
});

document.getElementById("zd-disconnect-btn")?.addEventListener("click", async () => {
  await api("/api/integrations/zendesk/disconnect", { method: "POST", body: "{}" });
  await refreshZendesk();
});

document.getElementById("zd-refresh-btn")?.addEventListener("click", () => refreshZendesk());

function slugify(s) {
  return String(s || "report")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60) || "report";
}

document.getElementById("download-md-btn")?.addEventListener("click", () => {
  if (!currentReportText) return;
  const blob = new Blob([currentReportText], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slugify(currentTopic)}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

document.getElementById("print-btn")?.addEventListener("click", () => {
  if (!currentReportText) return;
  const w = window.open("", "_blank");
  if (!w) return;
  w.document.write(`
    <html><head><title>${esc(currentTopic)}</title>
    <style>body{font-family:Georgia,serif;max-width:760px;margin:2rem auto;padding:0 1rem;line-height:1.6;}
    h1,h2,h3{line-height:1.25;} code{background:#f2f2f2;padding:0.1rem 0.3rem;border-radius:4px;}
    table{border-collapse:collapse;width:100%;} th,td{border:1px solid #ccc;padding:6px;}</style>
    </head><body>${mdToHtml(currentReportText)}</body></html>
  `);
  w.document.close();
  w.focus();
  setTimeout(() => w.print(), 300);
});

document.getElementById("view-final-btn")?.addEventListener("click", () => {
  document.getElementById("answer-section")?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
});

async function submitHitlDecision(decision, comment) {
  if (!selectedRunId) return;
  closeHitlModal();
  await api(`/api/runs/${selectedRunId}/approve`, {
    method: "POST",
    body: JSON.stringify({ decision, comment: comment || "" }),
  });
  const watch = async () => {
    await selectRun(selectedRunId);
    if (["RUNNING", "RETRYING", "PENDING"].includes(lastStatus)) {
      setTimeout(watch, 1500);
    }
  };
  setTimeout(watch, 500);
}

document.getElementById("agent-chat")?.addEventListener("click", async (e) => {
  const copyBtn = e.target.closest(".agent-copy");
  if (copyBtn) {
    e.preventDefault();
    e.stopPropagation();
    const step = copyBtn.closest(".agent-step");
    const text = step?.querySelector(".agent-step-body")?.innerText?.trim() || "";
    if (text) {
      await navigator.clipboard.writeText(text);
      copyBtn.textContent = "✓";
      copyBtn.title = "Copied";
      setTimeout(() => {
        copyBtn.textContent = "⧉";
        copyBtn.title = "Copy agent output";
      }, 1200);
    }
    return;
  }

  const btn = e.target.closest("[data-chat-hitl]");
  if (!btn || !selectedRunId) return;
  const decision = btn.getAttribute("data-chat-hitl");
  const commentEl = document.getElementById("chat-hitl-comment");
  const comment = (commentEl?.value || "").trim();
  if (decision === "revise" && !comment) {
    alert("Add revision notes before requesting a revision.");
    return;
  }
  if (decision === "approve" && comment) {
    const ok = confirm(
      "You typed feedback, but Approve does not rewrite.\n\nOK = approve as-is\nCancel = use Request revision"
    );
    if (!ok) return;
  }
  await submitHitlDecision(decision, comment);
});

document.getElementById("approve-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  const comment = document.getElementById("hitl-comment").value;
  if (comment.trim()) {
    const useApproveAnyway = confirm(
      "You typed feedback, but Approve does not rewrite the draft.\n\n" +
        "OK = Approve as-is (ignore the comment for rewriting)\n" +
        "Cancel = go back and use Request revision instead"
    );
    if (!useApproveAnyway) return;
  }
  await submitHitlDecision("approve", comment);
});

document.getElementById("revise-btn")?.addEventListener("click", async () => {
  if (!selectedRunId) return;
  const comment = document.getElementById("hitl-comment").value.trim();
  if (!comment) {
    alert("Add revision notes in the comment box (e.g. what the writer should fix).");
    return;
  }
  document.getElementById("hitl-comment").value = "";
  await submitHitlDecision("revise", comment);
});

document.getElementById("reject-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  const comment = document.getElementById("hitl-comment").value;
  await submitHitlDecision("reject", comment);
});

document.getElementById("resume-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  closeHitlModal();
  await api(`/api/runs/${selectedRunId}/resume`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setTimeout(() => selectRun(selectedRunId), 500);
});

document.getElementById("open-modal-btn").addEventListener("click", () => openHitlModal());

document.querySelectorAll("[data-close-modal]").forEach((el) => {
  el.addEventListener("click", () => closeHitlModal());
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeHitlModal();
});

showEmptyState(true);
refreshRuns().catch(console.error);
setInterval(() => refreshRuns().catch(() => {}), 5000);

const logoutBtn = document.getElementById("logout-btn");

function setSidebarCollapsed(collapsed) {
  const app = document.querySelector(".chat-app");
  const showBtn = document.getElementById("sidebar-show-btn");
  app?.classList.toggle("sidebar-collapsed", !!collapsed);
  showBtn?.classList.toggle("hidden", !collapsed);
  try {
    localStorage.setItem("ao_sidebar_collapsed", collapsed ? "1" : "0");
  } catch {
    /* ignore */
  }
}

document.getElementById("sidebar-hide-btn")?.addEventListener("click", () => {
  setSidebarCollapsed(true);
});
document.getElementById("sidebar-show-btn")?.addEventListener("click", () => {
  setSidebarCollapsed(false);
});
try {
  if (localStorage.getItem("ao_sidebar_collapsed") === "1") {
    setSidebarCollapsed(true);
  }
} catch {
  /* ignore */
}
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/login";
  });
  // Hide logout when auth is disabled
  fetch("/api/auth/status", { credentials: "same-origin" })
    .then((r) => r.json())
    .then((s) => {
      if (!s.auth_required) logoutBtn.classList.add("hidden");
    })
    .catch(() => {});
}
