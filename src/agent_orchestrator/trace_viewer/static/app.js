const PIPELINE_NODES = {
  research_report: [
    "knowledge_lookup",
    "researcher",
    "web_search",
    "writer",
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

const AGENT_META = {
  knowledge_lookup: {
    icon: "K",
    label: "Knowledge Lookup",
    role: "Retrieves relevant uploaded organisation documents",
  },
  researcher: { icon: "R", label: "Researcher", role: "Plans the investigation and drafts a research brief" },
  web_search: { icon: "S", label: "Web Search", role: "Gathers external sources with a search tool" },
  writer: { icon: "W", label: "Writer", role: "Drafts the report from the brief and findings" },
  critic: { icon: "C", label: "Critic", role: "Scores the draft and sends it back if weak" },
  human_approve: { icon: "H", label: "Human Checkpoint", role: "You approve before delivery" },
  deliver: { icon: "D", label: "Deliver", role: "Finalizes the signed-off output" },
  frontline: { icon: "F", label: "Frontline Agent", role: "Handles FAQs / classifies intent" },
  sentiment: { icon: "M", label: "Sentiment Agent", role: "Detects frustrated or high-risk customers" },
  faq_agent: { icon: "?", label: "FAQ Specialist", role: "Answers account and how-to questions" },
  technical_agent: { icon: "T", label: "Technical Agent", role: "Diagnoses product and outage issues" },
  billing_agent: { icon: "$", label: "Billing Agent", role: "Handles payment and refund issues" },
  quality_critic: { icon: "Q", label: "Quality Critic", role: "Checks empathy, policy, and clarity" },
  human_escalate: { icon: "H", label: "Escalation (Human)", role: "Routes to you when risk / frustration is high" },
};

let selectedRunId = null;
let pollTimer = null;
let activeTab = "agents";
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

function renderGraphPath(run) {
  const el = document.getElementById("graph-path");
  el.innerHTML = "";
  const order = nodeOrderFor(run);
  const executed = new Set(
    (run.trace || []).filter((t) => t.outcome === "success").map((t) => t.node_name)
  );
  const errored = new Set(
    (run.trace || []).filter((t) => t.outcome === "error").map((t) => t.node_name)
  );
  // Only show nodes that ran, plus current, plus full template for research; for support hide unused specialists unless executed/current
  const visible =
    run.graph_name === "support_resolution"
      ? order.filter(
          (n) =>
            !["faq_agent", "technical_agent", "billing_agent"].includes(n) ||
            executed.has(n) ||
            run.current_node === n ||
            (run.trace || []).some((t) => t.node_name === n)
        )
      : order;

  visible.forEach((name, i) => {
    if (i > 0) {
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "→";
      el.appendChild(arrow);
    }
    const chip = document.createElement("span");
    chip.className = "node-chip";
    chip.textContent = AGENT_META[name]?.label || name;
    if (errored.has(name) && run.current_node === name) chip.classList.add("error");
    else if (run.current_node === name) chip.classList.add("current");
    else if (executed.has(name)) chip.classList.add("done");
    el.appendChild(chip);
  });
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
    case "researcher":
      return { kind: "markdown", body: out.research_brief || state.research_brief };
    case "web_search": {
      const results = out.search_results || state.search_results || [];
      if (Array.isArray(results) && results.length) {
        const items = results
          .map(
            (r) =>
              `<li><a href="${esc(r.href)}" target="_blank" rel="noreferrer">${esc(
                r.title || r.href
              )}</a><br /><span class="muted">${esc((r.body || "").slice(0, 180))}</span></li>`
          )
          .join("");
        return { kind: "html", body: `<ul class="sources">${items}</ul>` };
      }
      return { kind: "text", body: "No sources captured." };
    }
    case "writer":
      return { kind: "markdown", body: out.report || state.report };
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

function renderAgentFeed(run) {
  const el = document.getElementById("agent-feed");
  el.innerHTML = "";
  const state = run.state || {};
  const events = run.trace || [];

  if (!events.length) {
    el.innerHTML = `<p class="muted">Waiting for the first agent to start…</p>`;
    return;
  }

  events.forEach((ev, idx) => {
    const meta = AGENT_META[ev.node_name] || { icon: "?", label: ev.node_name, role: "" };
    const card = document.createElement("div");
    card.className = `agent-card ${ev.outcome}`;

    const ms = ev.duration_ms != null ? `${Math.round(ev.duration_ms)} ms` : "";
    const attempt = ev.attempt > 1 ? ` · retry ${ev.attempt}` : "";
    const statusText =
      ev.outcome === "error"
        ? "failed"
        : ev.outcome === "paused"
        ? "waiting"
        : ev.outcome;

    const contrib = agentContribution(ev.node_name, ev, state);
    let bodyHtml = "";
    if (contrib.body) {
      if (contrib.kind === "markdown") bodyHtml = `<div class="report">${mdToHtml(contrib.body)}</div>`;
      else if (contrib.kind === "html") bodyHtml = contrib.body;
      else bodyHtml = `<p>${esc(contrib.body)}</p>`;
    }

    card.innerHTML = `
      <div class="agent-avatar">${esc(meta.icon)}</div>
      <div class="agent-body">
        <div class="agent-top">
          <span class="agent-name">${esc(meta.label)}</span>
          <span class="agent-status ${ev.outcome}">${esc(statusText)}${esc(attempt)}${
      ms ? ` · ${ms}` : ""
    }</span>
        </div>
        <p class="agent-role">${esc(meta.role)}</p>
        ${ev.error ? `<p class="agent-error">${esc(ev.error)}</p>` : ""}
        <div class="agent-output ${bodyHtml ? "" : "hidden"}">${bodyHtml}</div>
      </div>
    `;
    el.appendChild(card);

    if (idx < events.length - 1) {
      const link = document.createElement("div");
      link.className = "handoff";
      link.innerHTML = `<span>hands off to</span>`;
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
    deliveredBanner.querySelector("strong").textContent = isSupport
      ? "Ticket reply delivered"
      : "Report delivered";
    deliveredBanner.querySelector("p").textContent = isSupport
      ? (() => {
          const route = state.resolution_route || state.route_taken || state.intent || "—";
          const zd = state.zendesk_delivery;
          if (zd && zd.status === "delivered") {
            return `Route: ${route}. Reply posted to Zendesk ${zd.external_ticket_id} (simulated).`;
          }
          if (state.source === "zendesk" && state.external_ticket_id) {
            return `Route: ${route}. Zendesk ticket ${state.external_ticket_id} · see delivery note in raw state.`;
          }
          return `Route: ${route}. Full reply is shown below.`;
        })()
      : "Agents finished and you approved. Full report is shown below.";
    if (lastStatus !== "COMPLETED") {
      switchTab("report");
      if (state.source === "zendesk") {
        refreshZendesk().catch(() => {});
      }
    }
  }

  if (run.status === "PAUSED") {
    pausedBanner.classList.remove("hidden");
    pausedBanner.querySelector("strong").textContent = "Waiting for your approval";
    pausedBanner.querySelector("p").textContent =
      "Review the draft and approve to deliver, or reject.";
    document.getElementById("hitl-message").textContent =
      run.state?.checkpoint_message || run.error || "Waiting for approval";

    const critic = state.critic_output || {};
    const score = critic.score ?? state.score;
    const feedback = critic.feedback ?? state.feedback;
    const approved = critic.approved ?? state.approved;
    const report =
      state.draft_reply ||
      state.preliminary_reply ||
      state.report ||
      state.final_report ||
      "";

    let criticHtml = "";
    if (score != null || feedback) {
      const badge = approved
        ? `<span class="verdict ok">CRITIC APPROVED</span>`
        : `<span class="verdict bad">CRITIC ASKED FOR REVISION</span>`;
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
      ${criticHtml}
      ${routeBits ? `<p class="muted">${esc(routeBits)}</p>` : ""}
      <h4 class="preview-title">${
        run.graph_name === "support_resolution"
          ? "Draft reply ready for your review"
          : "Report ready for your review"
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
  document.querySelectorAll(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.id !== `tab-${tab}`);
  });
}

async function selectRun(runId) {
  selectedRunId = runId;
  await refreshRuns();
  const run = await api(`/api/runs/${runId}`);
  document.getElementById("detail-title").textContent =
    run.state?.topic || run.state?.subject || run.run_id;
  const pill = document.getElementById("status-pill");
  pill.textContent = run.status;
  pill.className = `pill ${run.status}`;
  const revisions = run.state?.revision_count ? ` · ${run.state.revision_count} revision(s)` : "";
  const graphLabel = run.graph_name === "support_resolution" ? "support" : "research";
  document.getElementById("detail-meta").textContent =
    `${graphLabel} · ${run.run_id} · step ${run.step}${revisions} · updated ${fmtTime(run.updated_at)}`;

  renderGraphPath(run);
  renderAgentFeed(run);
  renderReport(run);
  renderTrace(run);
  renderHitl(run);
  lastStatus = run.status;
  document.getElementById("state-view").textContent = JSON.stringify(run.state, null, 2);

  if (pollTimer) clearInterval(pollTimer);
  if (["RUNNING", "RETRYING", "PENDING"].includes(run.status)) {
    pollTimer = setInterval(() => selectRun(runId), 2000);
  } else if (run.status === "PAUSED") {
    // Keep polling lightly in case another tab resumes — but mostly static.
  }
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

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
  switchTab("agents");
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
  switchTab("agents");
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
document.getElementById("open-report-btn").addEventListener("click", () => {
  switchTab("report");
  document.getElementById("tab-report")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

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
