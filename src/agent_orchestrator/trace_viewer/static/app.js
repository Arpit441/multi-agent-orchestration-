const PIPELINE_NODES = {
  research_report: [
    "knowledge_lookup",
    "researcher",
    "web_search",
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
  knowledge_lookup: { icon: "1", label: "Search", role: "Look up uploaded documents" },
  researcher: { icon: "2", label: "Analyze", role: "Plan what to investigate" },
  web_search: { icon: "1", label: "Search", role: "Find web sources" },
  writer: { icon: "3", label: "Draft", role: "Write the answer" },
  source_guard: { icon: "✓", label: "Check sources", role: "Keep only verified links" },
  critic: { icon: "4", label: "Verify", role: "Score quality and accuracy" },
  human_approve: { icon: "★", label: "Your review", role: "You approve before delivery" },
  deliver: { icon: "✓", label: "Finish", role: "Deliver the final answer" },
  frontline: { icon: "1", label: "Triage", role: "Classify the ticket" },
  sentiment: { icon: "2", label: "Assess tone", role: "Check urgency and frustration" },
  faq_agent: { icon: "3", label: "Draft", role: "Answer FAQ-style questions" },
  technical_agent: { icon: "3", label: "Draft", role: "Diagnose technical issues" },
  billing_agent: { icon: "3", label: "Draft", role: "Handle billing questions" },
  quality_critic: { icon: "4", label: "Verify", role: "Check reply quality" },
  human_escalate: { icon: "★", label: "Your review", role: "You approve before sending" },
};

/** High-level progress stages shown to everyone. */
const USER_STAGES = {
  research_report: [
    { id: "search", label: "Search", nodes: ["knowledge_lookup", "web_search"] },
    { id: "analyze", label: "Analyze", nodes: ["researcher"] },
    { id: "draft", label: "Draft", nodes: ["writer", "source_guard"] },
    { id: "verify", label: "Verify", nodes: ["critic", "human_approve", "deliver"] },
  ],
  support_resolution: [
    { id: "search", label: "Search", nodes: ["knowledge_lookup"] },
    { id: "analyze", label: "Analyze", nodes: ["frontline", "sentiment"] },
    {
      id: "draft",
      label: "Draft",
      nodes: ["faq_agent", "technical_agent", "billing_agent"],
    },
    {
      id: "verify",
      label: "Verify",
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
let pollTimer = null;
let activeTab = "answer";
let lastStatus = null;
let modalAutoOpenedFor = null;
let currentReportText = "";
let currentTopic = "";
let pipelines = [];
let currentPipelineId = "research_report";

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

async function refreshRuns() {
  const data = await api("/api/runs");
  const list = document.getElementById("run-list");
  list.innerHTML = "";
  for (const run of data.runs || []) {
    const li = document.createElement("li");
    if (run.run_id === selectedRunId) li.classList.add("active");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.innerHTML = `<span class="dot ${run.status}"></span>${esc(
      (run.run_id || "").slice(0, 8)
    )}<em>${esc((run.graph_name || "").replace("_", " ").slice(0, 12))} · ${esc(
      run.status
    )}</em>`;
    btn.addEventListener("click", () => selectRun(run.run_id));
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function nodeOrderFor(run) {
  const name = run.graph_name || "research_report";
  return PIPELINE_NODES[name] || PIPELINE_NODES.research_report;
}

function stagesFor(run) {
  return USER_STAGES[run.graph_name] || USER_STAGES.research_report;
}

function renderSimpleProgress(run) {
  const el = document.getElementById("simple-progress");
  if (!el) return;
  el.innerHTML = "";
  const stages = stagesFor(run);
  const events = run.trace || [];
  const current = run.current_node;

  stages.forEach((stage) => {
    const li = document.createElement("li");
    const stageEvents = events.filter((t) => stage.nodes.includes(t.node_name));
    const hasError = stageEvents.some((t) => t.outcome === "error");
    const touched = stageEvents.length > 0 || stage.nodes.includes(current);
    const isCurrent = stage.nodes.includes(current);

    let cls = "todo";
    if (run.status === "COMPLETED") cls = "done";
    else if (hasError) cls = "error";
    else if (run.status === "PAUSED" && isCurrent) cls = "paused";
    else if (["RUNNING", "RETRYING", "PENDING"].includes(run.status) && isCurrent) cls = "current";
    else if (touched && !isCurrent) cls = "done";
    li.className = cls;
    li.innerHTML = `<span class="step-dot"></span><span class="step-label">${esc(stage.label)}</span>`;
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
    case "researcher": {
      const plan = out.research_plan || state.research_plan || {};
      const brief = out.research_brief || plan.research_brief || state.research_brief || "";
      const queries = out.search_queries || plan.search_queries || state.search_queries || [];
      const queryHtml = Array.isArray(queries) && queries.length
        ? `<p class="muted"><strong>Planned searches</strong></p><ul>${queries
            .map((q) => `<li><code>${esc(q)}</code></li>`)
            .join("")}</ul>`
        : "";
      return { kind: "html", body: `${mdToHtml(brief)}${queryHtml}` };
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

function thinkingHtml(nodeName, ev, state) {
  const thoughts = state.agent_thoughts || {};
  const fromState = thoughts[nodeName] || state[`${nodeName}_thinking`] || "";
  const fromOut =
    (ev.output_snapshot && (ev.output_snapshot[`${nodeName}_thinking`] ||
      (ev.output_snapshot.agent_thoughts || {})[nodeName])) ||
    "";
  const text = fromOut || fromState;
  if (ev.outcome === "running" || (!text && ev.outcome === "running")) {
    return `
      <div class="thinking-block live" data-thinking-live="1">
        <div class="thinking-label"><span class="thinking-pulse"></span> Working</div>
        <p class="thinking-text">Preparing this step…</p>
      </div>`;
  }
  if (!text) return "";
  return `
    <details class="thinking-block">
      <summary class="thinking-label">Reasoning</summary>
      <p class="thinking-text">${esc(text)}</p>
    </details>`;
}

function renderAgentFeed(run) {
  const el = document.getElementById("agent-feed");
  el.innerHTML = "";
  const state = run.state || {};
  const events = run.trace || [];

  if (!events.length && !["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
    el.innerHTML = `<p class="muted">Waiting for the first agent to start…</p>`;
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
    const card = document.createElement("div");
    card.className = `agent-card ${ev.outcome}`;

    const ms = ev.duration_ms != null ? `${Math.round(ev.duration_ms)} ms` : "";
    const statusText =
      ev.outcome === "error"
        ? "failed"
        : ev.outcome === "paused"
        ? "waiting for you"
        : ev.outcome === "running"
        ? "working"
        : "done";

    const isToolish = ["web_search", "knowledge_lookup", "source_guard", "deliver"].includes(
      ev.node_name
    );
    const think =
      isToolish || ev.outcome === "paused"
        ? ""
        : thinkingHtml(ev.node_name, ev, state);

    const contrib = agentContribution(ev.node_name, ev, state);
    let bodyHtml = "";
    if (ev.outcome !== "running" && contrib.body) {
      if (contrib.kind === "markdown") bodyHtml = `<div class="report">${mdToHtml(contrib.body)}</div>`;
      else if (contrib.kind === "html") bodyHtml = contrib.body;
      else bodyHtml = `<p>${esc(contrib.body)}</p>`;
    }

    const techBits = [
      ev.node_name,
      ms ? ms : "",
      ev.attempt > 1 ? `retry ${ev.attempt}` : "",
    ]
      .filter(Boolean)
      .join(" · ");

    card.innerHTML = `
      <div class="agent-avatar">${esc(meta.icon)}</div>
      <div class="agent-body">
        <div class="agent-top">
          <span class="agent-name">${esc(meta.label)}</span>
          <span class="agent-status ${ev.outcome}">${esc(statusText)}</span>
        </div>
        <p class="agent-role">${esc(meta.role)}</p>
        ${ev.error ? `<p class="agent-error">${esc(ev.error)}</p>` : ""}
        ${think}
        <div class="agent-output ${bodyHtml ? "" : "hidden"}">${bodyHtml}</div>
        ${techBits ? `<p class="agent-tech muted">${esc(techBits)}</p>` : ""}
      </div>
    `;
    el.appendChild(card);

    if (idx < events.length - 1) {
      const link = document.createElement("div");
      link.className = "handoff";
      link.innerHTML = `<span>next</span>`;
      el.appendChild(link);
    }
  });
}

function renderReport(run) {
  const el = document.getElementById("report-view");
  const state = run.state || {};
  const report = state.final_report || state.final_reply || state.report || state.draft_reply;
  const dlBtn = document.getElementById("download-md-btn");
  const printBtn = document.getElementById("print-btn");
  if (report) {
    el.classList.remove("empty");
    el.innerHTML = mdToHtml(report);
    currentReportText = report;
    currentTopic = state.topic || state.subject || "output";
    if (dlBtn) dlBtn.disabled = false;
    if (printBtn) printBtn.disabled = false;
  } else {
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

  if (run.status === "COMPLETED" && (state.final_report || state.final_reply || state.report || state.delivered)) {
    deliveredBanner.classList.remove("hidden");
    const isSupport = run.graph_name === "support_resolution";
    deliveredBanner.querySelector("strong").textContent = "Ready";
    deliveredBanner.querySelector("p").textContent = isSupport
      ? (() => {
          const zd = state.zendesk_delivery;
          if (zd && zd.status === "delivered") {
            return `Reply sent to Zendesk ${zd.external_ticket_id} (simulated).`;
          }
          return "Your support reply is below.";
        })()
      : "Your research answer is below.";
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

    // Auto-open modal once per paused run so users don't scroll.
    if (modalAutoOpenedFor !== run.run_id) {
      modalAutoOpenedFor = run.run_id;
      openHitlModal();
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
    document.getElementById("process-panel")?.setAttribute("open", "");
    document.getElementById("process-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (tab === "trace" || tab === "state") {
    document.getElementById("tech-panel")?.setAttribute("open", "");
    document.getElementById("tech-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function selectRun(runId) {
  selectedRunId = runId;
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

  renderSimpleProgress(run);
  renderAgentFeed(run);
  renderReport(run);
  renderTrace(run);
  renderHitl(run);
  const prevStatus = lastStatus;
  lastStatus = run.status;
  document.getElementById("state-view").textContent = JSON.stringify(run.state, null, 2);

  // Auto-expand process while working; keep answer primary when done.
  const processPanel = document.getElementById("process-panel");
  if (processPanel) {
    if (["RUNNING", "RETRYING", "PENDING", "PAUSED"].includes(run.status)) {
      processPanel.open = true;
    } else if (run.status === "COMPLETED" && prevStatus !== "COMPLETED") {
      processPanel.open = false;
    }
  }

  if (pollTimer) clearInterval(pollTimer);
  if (["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
    pollTimer = setInterval(() => selectRun(runId), 2000);
  }
}

document.getElementById("start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pipeline = document.getElementById("pipeline").value || "research_report";
  let payload;
  if (pipeline === "support_resolution") {
    payload = {
      graph: "support_resolution",
      subject: document.getElementById("ticket-subject").value.trim(),
      message: document.getElementById("ticket-message").value.trim(),
      customer_name: document.getElementById("customer-name").value.trim() || "Customer",
      source: "manual",
    };
    if (!payload.subject || !payload.message) {
      alert("Subject and message are required for support tickets.");
      return;
    }
  } else {
    const topic = document.getElementById("topic").value.trim();
    if (topic.length < 2) {
      alert("Enter a research topic.");
      return;
    }
    payload = {
      graph: "research_report",
      topic,
      report_type: document.getElementById("report-type").value || "general",
    };
  }
  const data = await api("/api/runs", {
    method: "POST",
    headers: { "Idempotency-Key": newIdempotencyKey() },
    body: JSON.stringify(payload),
  });
  if (pipeline === "research_report") {
    document.getElementById("topic").value = "";
  }
  switchTab("answer");
  await refreshRuns();
  await selectRun(data.run_id);
});

function syncPipelineForm() {
  const id = document.getElementById("pipeline").value;
  currentPipelineId = id;
  const meta = pipelines.find((p) => p.id === id);
  document.getElementById("pipeline-desc").textContent = meta?.description || "";
  const research = document.getElementById("research-fields");
  const support = document.getElementById("support-fields");
  const isSupport = id === "support_resolution";
  research.classList.toggle("hidden", isSupport);
  support.classList.toggle("hidden", !isSupport);

  // Knowledge upload and Zendesk are support-pipeline concerns.
  document.getElementById("knowledge-panel")?.classList.toggle("hidden", !isSupport);
  document.getElementById("integrations-panel")?.classList.toggle("hidden", !isSupport);
  if (isSupport) {
    refreshKnowledge().catch(() => {});
    refreshZendesk().catch(() => {});
  }
}

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
  closeHitlModal();
  await api(`/api/runs/${selectedRunId}/approve`, {
    method: "POST",
    body: JSON.stringify({ decision: "approve", comment }),
  });
  // Poll until deliver finishes, then report tab auto-opens on COMPLETED.
  const watch = async () => {
    await selectRun(selectedRunId);
    if (["RUNNING", "RETRYING", "PENDING"].includes(lastStatus)) {
      setTimeout(watch, 1500);
    }
  };
  setTimeout(watch, 500);
});

document.getElementById("revise-btn")?.addEventListener("click", async () => {
  if (!selectedRunId) return;
  const comment = document.getElementById("hitl-comment").value.trim();
  if (!comment) {
    alert("Add revision notes in the comment box (e.g. what the writer should fix).");
    return;
  }
  closeHitlModal();
  document.getElementById("hitl-comment").value = "";
  await api(`/api/runs/${selectedRunId}/approve`, {
    method: "POST",
    body: JSON.stringify({ decision: "revise", comment }),
  });
  const watch = async () => {
    await selectRun(selectedRunId);
    if (["RUNNING", "RETRYING", "PENDING"].includes(lastStatus)) {
      setTimeout(watch, 1500);
    }
  };
  setTimeout(watch, 500);
});

document.getElementById("reject-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  const comment = document.getElementById("hitl-comment").value;
  closeHitlModal();
  await api(`/api/runs/${selectedRunId}/approve`, {
    method: "POST",
    body: JSON.stringify({ decision: "reject", comment }),
  });
  setTimeout(() => selectRun(selectedRunId), 500);
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

refreshRuns().catch(console.error);
setInterval(() => refreshRuns().catch(() => {}), 5000);

const logoutBtn = document.getElementById("logout-btn");
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
