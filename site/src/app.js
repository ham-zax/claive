import { createAgent, registerTools } from "@ui-sdk/web/agent"
import "@ui-sdk/web/accordion"
import "@ui-sdk/web/button"
import "@ui-sdk/web/dialog"
import "@ui-sdk/web/listbox"
import "@ui-sdk/web/segmented-control"
import "@ui-sdk/web/tabs"
import "@ui-sdk/web/toggle"

const agent = createAgent({
  allow: (step) => (step.confirm ? window.confirm(step.confirm) : true),
})
registerTools(agent, { allow: () => true })
window.claiveAgent = agent

const WORKERS = [
  {
    id: "ace72d874787",
    alias: "@core-cli",
    badge: "ASK",
    icon: "●",
    badgeClass: "ask",
    actionable: true,
    engine: "codex · gpt-6.1-terra (xhigh)",
    workspace: "repo/satori",
    turn: 1,
    launched: "Nov 21 18:35",
    ago: "2d ago",
    runtime: "8m35s",
    mission: "Fix worktree lock recovery & add CLI regression tests",
    subline: "? Back off for live PIDs or add a helper-age ceiling?",
    subClass: "ask",
    stage: "blocked",
    intent: "Core and CLI fixes pass 47 tests; paused for live-PID lock policy choice.",
    lastCheck: { status: "passed", summary: "47 passed (tsx --test)" },
    filesTotal: 5,
    verify: "passed",
    accept: "pending",
    branch: "claive/ace72d874787",
    baseCommit: "9f4b21c8a012",
    cmdFailures: 1,
    ask: {
      kind: "needs_decision",
      question: "Should lock recovery always back off while the recorded PID is alive, or introduce a separate helper-age ceiling?",
      cmd: 'claive answer @core-cli --message ""',
    },
    applyPreview: {
      destination: "/repo/satori",
      blocked: true,
      blockers: ["Unanswered needs_decision — run claive answer before applying"],
      files: [
        "packages/core/src/core/context.ts",
        "packages/core/src/lock/recovery.ts",
        "packages/cli/src/commands/gc.ts",
      ],
    },
    changedFiles: [
      "packages/core/src/core/context.ts",
      "packages/core/src/lock/recovery.ts",
      "packages/cli/src/commands/gc.ts",
      "packages/cli/src/gc.test.ts",
      "packages/core/src/lock/recovery.test.ts",
    ],
    actions: [
      {
        id: 34, cat: "test", tag: "TEST", summary: "recovery.test.ts",
        outcome: "46 passed", status: "ok", milestone: true,
        cmd: "node --import tsx --test packages/core/src/lock/recovery.test.ts", exit: 0,
        output: "# tests 46  # pass 46  # fail 0  (412ms)",
      },
      {
        id: 35, cat: "read", tag: "READ", summary: "rg reuse|shardReuse in packages/core/src",
        outcome: "14 lines", status: "ok", milestone: false,
        cmd: "rg -n 'reuse|shardReuse' packages/core/src", exit: 0,
        output: "packages/core/src/core/context.ts:55: relationshipVersion",
      },
      {
        id: 36, cat: "edit", tag: "EDIT", summary: "packages/cli/src/gc.test.ts (2 edits)",
        outcome: "applied", status: "ok", milestone: true,
        cmd: null, exit: 0,
        output: "Added live-PID lock backoff regression test.",
      },
      {
        id: 37, cat: "build", tag: "BUILD", summary: "pnpm --filter @satori/cli build",
        outcome: "ok", status: "ok", milestone: true,
        cmd: "pnpm --filter @satori/cli build", exit: 0,
        output: "> tsc -b (0 errors)",
      },
      {
        id: 38, cat: "test", tag: "TEST", summary: "gc.test.ts",
        outcome: "47 passed", status: "ok", milestone: true,
        cmd: "node --import tsx --test packages/cli/src/gc.test.ts", exit: 0,
        output: "# tests 47  # pass 47  # fail 0",
      },
      {
        id: 39, cat: "say", tag: "SAY", summary: "Build and 47 tests pass; paused for lock-recovery decision.",
        outcome: null, status: "done", milestone: true,
        cmd: null, exit: null,
        output: "Build and 47 tests pass. Escalated needs_decision before changing live-PID backoff.",
      },
    ],
    logs: {
      summary: "# Summary\n- Fixed lock recovery in context.ts & recovery.ts\n- Verified 47 passed in core + CLI suites\n- Escalated needs_decision on live-PID backoff policy",
      prompt: "# Task\nFix lock recovery in packages/core and packages/cli. Do not commit. Escalate policy choices with needs_decision.",
      stderr: "(stderr.log is empty)",
      supervisor: "[08:01:12] spawned codex worker in claive/ace72d874787\n[08:09:47] settled: needs_decision",
    },
  },
  {
    id: "92113e98e291",
    alias: "@api-refactor",
    badge: "RUN",
    icon: "▸",
    badgeClass: "run",
    actionable: false,
    engine: "claude · haiku-5-5 (xhigh)",
    workspace: "repo/gateway",
    turn: 2,
    launched: "Today 08:58",
    ago: "2m ago",
    runtime: "▸ 2m14s",
    mission: "Extract connector-policy parser into pure validator",
    subline: "▸ [TESTING] just test-fast",
    subClass: "run",
    stage: "testing",
    intent: "Extracted validator; running edge-case test matrix now.",
    lastCheck: { status: "failed", summary: "1 failed (missing default fallback)" },
    filesTotal: 3,
    verify: "not_run",
    accept: "pending",
    branch: "claive/92113e98e291",
    baseCommit: "3c8d09e14b77",
    cmdFailures: 1,
    ask: null,
    applyPreview: {
      destination: "/repo/gateway",
      blocked: true,
      blockers: ["Turn 2 still running", "Verification has not passed yet (verify=not_run)"],
      files: ["lib/bridge/connector-policy.mjs", "tests/test_connector_policy.mjs"],
    },
    changedFiles: [
      "lib/bridge/connector-policy.mjs",
      "lib/bridge/policy-schema.mjs",
      "tests/test_connector_policy.mjs",
    ],
    actions: [
      {
        id: 12, cat: "edit", tag: "EDIT", summary: "lib/bridge/connector-policy.mjs",
        outcome: "applied", status: "ok", milestone: true,
        cmd: null, exit: 0,
        output: "Extracted pure validatePolicy() helper.",
      },
      {
        id: 13, cat: "test", tag: "TEST", summary: "just test-fast",
        outcome: "exit 1: 1 failed", status: "error", milestone: true,
        cmd: "just test-fast", exit: 1,
        output: "FAIL: expected default policy when CONNECTOR_POLICY env unset",
      },
      {
        id: 14, cat: "edit", tag: "EDIT", summary: "lib/bridge/connector-policy.mjs",
        outcome: "applied", status: "ok", milestone: true,
        cmd: null, exit: 0,
        output: "Restored empty-env default fallback.",
      },
      {
        id: 15, cat: "test", tag: "TEST", summary: "just test-fast",
        outcome: "running...", status: "running", milestone: true,
        cmd: "just test-fast", exit: null,
        output: "Running 28 test cases...",
      },
    ],
    logs: {
      summary: "(turn 2 still running — result.txt writes on settlement)",
      prompt: "# Turn 2\nExtract connector policy validator and cover empty-env defaults.",
      stderr: "(stderr.log is empty)",
      supervisor: "[08:58:11] turn 2 started (--enable-steering)\n[09:00:23] active tool: bash (just test-fast)",
    },
  },
  {
    id: "7d40a1f8c302",
    alias: "@auth-audit",
    badge: "REVIEW",
    icon: "★",
    badgeClass: "review",
    actionable: true,
    engine: "pi · mimo-v2.6-flash",
    workspace: "repo/claive",
    turn: 1,
    launched: "Today 08:42",
    ago: "18m ago",
    runtime: "4m48s",
    mission: "Harden secret redaction for multi-byte UTF-8 boundaries",
    subline: "★ Ready to apply · 2 files · verify passed",
    subClass: "review",
    stage: "done",
    intent: "All 34 Rust tests & clippy -D warnings clean.",
    lastCheck: { status: "passed", summary: "34 passed, 0 failed" },
    filesTotal: 2,
    verify: "passed",
    accept: "pending",
    branch: "claive/7d40a1f8c302",
    baseCommit: "11a7e4f9c203",
    cmdFailures: 0,
    ask: null,
    applyPreview: {
      destination: "/repo/claive",
      blocked: false,
      blockers: [],
      files: ["tui/src/events.rs", "tui/tests/phase1_integration.rs"],
    },
    changedFiles: ["tui/src/events.rs", "tui/tests/phase1_integration.rs"],
    actions: [
      {
        id: 8, cat: "edit", tag: "EDIT", summary: "tui/src/events.rs (2 edits)",
        outcome: "applied", status: "ok", milestone: true,
        cmd: null, exit: 0,
        output: "Switched issuer-token scanner to byte slices for multi-byte UTF-8 safety.",
      },
      {
        id: 9, cat: "test", tag: "TEST", summary: "cargo test --manifest-path tui/Cargo.toml",
        outcome: "34 passed", status: "ok", milestone: true,
        cmd: "cargo test --manifest-path tui/Cargo.toml", exit: 0,
        output: "22 unit + 8 phase1 + 4 phase2_3 passed",
      },
      {
        id: 10, cat: "build", tag: "BUILD", summary: "cargo clippy --all-targets -- -D warnings",
        outcome: "ok", status: "ok", milestone: true,
        cmd: "cargo clippy --manifest-path tui/Cargo.toml --all-targets -- -D warnings", exit: 0,
        output: "0 warnings",
      },
      {
        id: 11, cat: "say", tag: "SAY", summary: "Verified; worktree ready for claive apply --dry-run.",
        outcome: null, status: "done", milestone: true,
        cmd: null, exit: null,
        output: "34/34 tests and clippy clean. Ready to apply.",
      },
    ],
    logs: {
      summary: "# Ready to Apply\n- Hardened redact_secrets for multi-byte UTF-8 boundaries\n- 34/34 Rust tests & clippy clean",
      prompt: "# Task\nAudit tui/src/events.rs secret redaction for UTF-8 boundary panics and add tests.",
      stderr: "(stderr.log is empty)",
      supervisor: "[08:42:10] spawned pi worker\n[08:46:58] verification passed -> REVIEW",
    },
  },
  {
    id: "4f91b2e60019",
    alias: "@spec-scout",
    badge: "DONE",
    icon: "✓",
    badgeClass: "done",
    actionable: false,
    engine: "agy · gemini-3.8-flash-high",
    workspace: "repo/claive",
    turn: 1,
    launched: "Yest 19:44",
    ago: "13h ago",
    runtime: "1m42s",
    mission: "Audit flat-schema MCP tools for forbidden JSON Schema keys",
    subline: "✓ 10/10 MCP tools flat-schema compliant",
    subClass: "muted",
    stage: "done",
    intent: "Zero nested objects, anyOf, or $ref keys across all 10 tools.",
    lastCheck: { status: "passed", summary: "19 passed" },
    filesTotal: 0,
    verify: "passed",
    accept: "accepted",
    branch: "(read-only)",
    baseCommit: "11a7e4f9c203",
    cmdFailures: 0,
    ask: null,
    applyPreview: {
      destination: "/repo/claive",
      blocked: true,
      blockers: ["Read-only worker — no write worktree to apply"],
      files: [],
    },
    changedFiles: [],
    actions: [
      {
        id: 1, cat: "read", tag: "READ", summary: "bin/claivelib/mcp_server.py",
        outcome: "210 lines", status: "ok", milestone: false,
        cmd: null, exit: 0,
        output: "Audited TOOL_DEFINITIONS and FORBIDDEN_SCHEMA_KEYS.",
      },
      {
        id: 2, cat: "test", tag: "TEST", summary: "tests/test_mcp_server.py",
        outcome: "19 passed", status: "ok", milestone: true,
        cmd: "just test-one tests/test_mcp_server.py", exit: 0,
        output: "Ran 19 tests in 0.84s — OK",
      },
      {
        id: 3, cat: "say", tag: "SAY", summary: "All 10 MCP tools flat-schema compliant.",
        outcome: null, status: "done", milestone: true,
        cmd: null, exit: null,
        output: "claive_start through claive_close verified.",
      },
    ],
    logs: {
      summary: "# Audit\n10/10 MCP tools obey flat scalar schema constraints.",
      prompt: "# Scout\nVerify flat scalar schemas in mcp_server.py.",
      stderr: "(stderr.log is empty)",
      supervisor: "[19:44:02] spawned read-only agy worker\n[19:45:44] done",
    },
  },
]

const ARBITER_STEPS = {
  write: {
    tag: "L0 · Direct attempt",
    score: "6 / 7",
    squares: [1, 1, 1, 1, 1, 1, 0],
    head: "orch/run/a @ c4f1a0",
    verdict: "1 test failed — escalate to cross-family critic",
    verdictClass: "fail",
    cmd: "claive-orch next RUN  ->  critique lane a round 1",
  },
  revert: {
    tag: "L1 · Bad fix auto-reverted",
    score: "4 / 7 -> 6 / 7",
    squares: [1, 1, 1, 1, 0, 0, 0],
    head: "reset --hard c4f1a0 · shelved refs/claive-orch/rejected-r1",
    verdict: "Score dropped — reverted automatically, attempt shelved",
    verdictClass: "warn",
    cmd: "git reset --hard c4f1a0  # regressions never stay on your branch",
  },
  fix: {
    tag: "L1 · Cross-family fix passes",
    score: "7 / 7",
    squares: [1, 1, 1, 1, 1, 1, 1],
    head: "orch/run/a @ 8e2d91",
    verdict: "Verified — ready for claive-orch integrate",
    verdictClass: "ok",
    cmd: "claive-orch integrate RUN  # applies diff and re-verifies in checkout",
  },
  breadth: {
    tag: "L2 · Selective breadth on stall",
    score: "Lane B: 7 / 7",
    squares: [1, 1, 1, 1, 1, 1, 1],
    head: "worktree lane b @ 3a90f2",
    verdict: "Lane A stalled twice -> second approach in fresh worktree wins",
    verdictClass: "ok",
    cmd: "claive-orch init --arm D --verify 'python3 -m pytest -q'",
  },
}

const QS_SNIPPETS = {
  install: `git clone https://github.com/ham-zax/claive.git && cd claive
just dash-build && ./install.sh --with-tui
claive doctor`,
  worker: `claive open --detach --engine codex --worktree --carry-dirty \\
  --alias backend --workspace /abs/repo --prompt-file /tmp/task.md

claive wait @backend && claive verify @backend && claive worktree apply @backend --dry-run`,
  dash: `claive dash                    # read-only Rust cockpit (0 locks, native tailer)
claive dash --triage           # show only ASK / UNWATCHED / LOST / FAIL / REVIEW
claive dash --actions          # interactive answer / steer / verify / apply
CLAIVE_TMUX_TUI=1 claive-codex # 5-row compact tmux status pane`,
  conv: `claive conversation start @proposer @critic @reviewer \\
  --message-file /abs/design-question.md --rounds 2 --timeout 300`,
  mcp: `# Configure claive-mcp in Claude Code, Codex, or any stdio MCP host:
claive-mcp

# 10 flat-schema tools: claive_start · claive_peek · claive_steer · claive_wait
# claive_verify · claive_apply · claive_followup · claive_list · claive_cancel · claive_close`,
  orch: `# Verifier-gated arbiter (L0 -> L1 refine -> L2 breadth):
claive-orch init --repo /abs/repo --task-file /abs/task.md --verify 'pytest -q' --arm D
claive-orch next RUN && claive-orch integrate RUN

# Unattended server queue (systemd/claive-serve.service):
claive goal add --title "Audit deps" --workspace /abs/repo --prompt-file /abs/goal.md && claive serve`,
}

function esc(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]))
}

function wireCopyButton(uiButtonId, getText, defaultLabel = "Copy") {
  const host = document.getElementById(uiButtonId)
  if (!host) return
  host.addEventListener("action", (event) => {
    const inner = host.querySelector("button")
    event.detail.waitUntil(
      (async () => {
        try {
          await navigator.clipboard.writeText(getText())
          if (inner) inner.textContent = "Copied"
        } catch {
          if (inner) inner.textContent = "Copy manually"
        }
        await new Promise((r) => setTimeout(r, 1200))
        if (inner) inner.textContent = defaultLabel
      })(),
    )
  })
}

function initTheme() {
  const root = document.documentElement
  const toggle = document.getElementById("theme-toggle")
  if (!toggle) return

  let saved = null
  try { saved = localStorage.getItem("claive-theme") } catch {}
  const initialDark = saved ? saved === "dark" : true

  const applyTheme = (dark) => {
    root.dataset.theme = dark ? "dark" : "light"
    const btn = toggle.querySelector("button")
    if (btn) btn.textContent = dark ? "Light" : "Dark"
  }

  toggle.pressed = initialDark
  applyTheme(initialDark)

  toggle.addEventListener("pressedchange", (event) => {
    const dark = Boolean(event.detail.value)
    applyTheme(dark)
    try { localStorage.setItem("claive-theme", dark ? "dark" : "light") } catch {}
  })
}

function renderBriefingHtml(w) {
  let html = `<div class="brief-id">
    <span class="st-pill st-${w.badgeClass}">${esc(w.icon)} ${esc(w.badge)}</span>
    <span class="brief-model">${esc(w.engine)}</span>
  </div>
  <div class="brief-timing">${esc(w.workspace)} · Launched ${esc(w.launched)} (${esc(w.ago)}) · ${esc(w.runtime)}</div>`

  if (w.ask) {
    html += `<div class="brief-ask">
      <div class="ask-t">ASK · ${esc(w.ask.kind)}</div>
      <div class="ask-q">${esc(w.ask.question)}</div>
      <div class="ask-cmd">${esc(w.ask.cmd)}</div>
    </div>`
  }

  html += `<div class="brief-block">
    <div class="brief-h">Mission</div>
    <div class="brief-mission">${esc(w.mission)}</div>
  </div>
  <div class="brief-block">
    <dl class="brief-kv">
      <dt>Check</dt><dd class="${w.lastCheck.status === "passed" ? "c-green" : "c-red"}">${esc(w.lastCheck.summary)}</dd>
      <dt>Branch</dt><dd>${esc(w.branch)}</dd>
      <dt>Gates</dt><dd>verify=${esc(w.verify)} · accept=${esc(w.accept)}</dd>
    </dl>
  </div>`

  const milestones = w.actions.filter((a) => a.milestone).slice(-3)
  html += `<div class="brief-block"><div class="brief-h">Recent Milestones</div>`
  for (const m of milestones) {
    const outHtml = m.outcome
      ? ` <span class="c-muted">-&gt;</span> <span class="act-out ${m.status}">${esc(m.outcome)}</span>`
      : ""
    html += `<div class="brief-ms"><span class="cat-tag cat-${m.cat}">${esc(m.tag)}</span> ${esc(m.summary)}${outHtml}</div>`
  }
  html += `</div>`
  return html
}

function initCockpit() {
  const listbox = document.getElementById("worker-listbox")
  const triageToggle = document.getElementById("triage-toggle")
  const activityFilter = document.getElementById("activity-filter")
  const activityAccordion = document.getElementById("activity-accordion")
  const activityMeta = document.getElementById("activity-meta")
  const logKindControl = document.getElementById("log-kind")
  const logPre = document.getElementById("log-output")
  const briefingPanel = document.getElementById("panel-briefing-content")
  const evidencePanel = document.getElementById("panel-evidence-content")
  const rightBriefing = document.getElementById("briefing-body")
  const countsEl = document.getElementById("cockpit-counts")
  const workersTitleEl = document.getElementById("workers-title")
  const inspectorTitleEl = document.getElementById("inspector-title")
  const briefingTitleEl = document.getElementById("briefing-title")
  const applyDialog = document.getElementById("apply-dialog")
  const applyBody = document.getElementById("apply-preview-body")
  const applyConfirmBtn = document.getElementById("apply-confirm-btn")
  const applyStatus = document.getElementById("apply-status")

  if (!listbox || !activityAccordion) return

  let selectedId = WORKERS[0].id
  let triageOnly = false
  let filterMode = "actions"
  let logMode = "summary"

  const currentWorker = () => WORKERS.find((w) => w.id === selectedId) || WORKERS[0]

  function renderWorkers() {
    const visible = triageOnly ? WORKERS.filter((w) => w.actionable) : WORKERS
    if (!visible.some((w) => w.id === selectedId) && visible.length) {
      selectedId = visible[0].id
    }

    countsEl.innerHTML = `
      <span class="hdr-brand">◆ CLAIVE</span>
      <span class="hdr-ask">● 1 need input</span>
      <span class="hdr-review">★ 1 review</span>
      <span class="hdr-run">▸ 1 running</span>
      <span class="hdr-muted">0 idle · 1 done</span>
    `

    workersTitleEl.textContent = `Workers (${visible.length})`
    listbox.innerHTML = visible.map((w) => `
      <ui-listbox-item value="${w.id}">
        <span class="w-row">
          <span class="w-top">
            <span class="st-pill st-${w.badgeClass}">${esc(w.icon)} ${esc(w.badge)}</span>
            <span class="w-name">${esc(w.alias)}</span>
            <span class="w-time">${esc(w.launched)} · ${esc(w.runtime)}</span>
          </span>
          <span class="w-mission">${esc(w.mission)}</span>
          <span class="w-sub sub-${w.subClass}">${esc(w.subline)}</span>
        </span>
      </ui-listbox-item>
    `).join("")
    listbox.value = selectedId
  }

  function renderWorkerDetails() {
    const w = currentWorker()
    inspectorTitleEl.textContent = `Inspector: ${w.alias}`
    briefingTitleEl.textContent = `Briefing: ${w.alias}`

    const briefHtml = renderBriefingHtml(w)
    briefingPanel.innerHTML = briefHtml
    rightBriefing.innerHTML = briefHtml

    const filesHtml = w.changedFiles.length
      ? w.changedFiles.map((f) => `<div class="ev-file">${esc(f)}</div>`).join("")
      : `<div class="c-muted">Read-only turn — no changed files</div>`
    evidencePanel.innerHTML = `
      <dl class="brief-kv" style="margin-bottom:0.8rem">
        <dt>Verify</dt><dd class="${w.verify === "passed" ? "c-green" : "c-warn"}">${esc(w.verify)}</dd>
        <dt>Accept</dt><dd>${esc(w.accept)}</dd>
        <dt>Branch</dt><dd>${esc(w.branch)}</dd>
      </dl>
      <div class="brief-h">Changed files (${w.filesTotal})</div>
      ${filesHtml}
    `

    const filtered = w.actions.filter((a) => (filterMode === "milestones" ? a.milestone : true))
    activityMeta.textContent = `${filtered.length} steps`

    const firstId = filtered[0] ? `act-${filtered[0].id}` : ""
    activityAccordion.innerHTML = filtered.map((a, idx) => {
      const val = `act-${a.id}`
      const open = idx === 0
      const outSpan = a.outcome
        ? `<span class="c-muted">-&gt;</span><span class="act-out ${a.status}">${esc(a.outcome)}</span>`
        : ""
      return `
        <ui-accordion-item value="${val}">
          <ui-accordion-header>
            <h4>
              <ui-accordion-trigger>
                <button type="button" class="act-btn">
                  <span class="act-chev" aria-hidden="true"></span>
                  <span class="cat-tag cat-${a.cat}">${esc(a.tag)}</span>
                  <span class="act-sum">${esc(a.summary)}</span>
                  ${outSpan}
                </button>
              </ui-accordion-trigger>
            </h4>
          </ui-accordion-header>
          <ui-accordion-panel${open ? "" : " hidden"}>
            <div class="act-detail">
              ${a.cmd ? `<div class="cmd">$ ${esc(a.cmd)}</div>` : ""}
              ${a.exit !== null ? `<div class="${a.exit !== 0 ? "exit-err" : "c-muted"}">exit ${a.exit}</div>` : ""}
              <div>${esc(a.output)}</div>
            </div>
          </ui-accordion-panel>
        </ui-accordion-item>
      `
    }).join("")
    activityAccordion.value = firstId ? [firstId] : []

    logPre.textContent = w.logs[logMode] || ""

    const ap = w.applyPreview
    applyStatus.textContent = ""
    const blockersHtml = ap.blockers.length
      ? `<div class="dlg-blockers">${ap.blockers.map((b) => `<div class="c-red">✖ ${esc(b)}</div>`).join("")}</div>`
      : `<div class="c-green" style="margin-bottom:0.65rem">✓ All verification &amp; ownership gates clear — ready to apply</div>`
    const applyFilesHtml = ap.files.length
      ? ap.files.map((f) => `<div class="ev-file">+ ${esc(f)}</div>`).join("")
      : `<div class="c-muted">No changed files</div>`
    applyBody.innerHTML = `
      <dl class="brief-kv" style="margin-bottom:0.7rem">
        <dt>Agent</dt><dd>${esc(w.alias)}</dd>
        <dt>Target</dt><dd>${esc(ap.destination)}</dd>
      </dl>
      ${blockersHtml}
      ${applyFilesHtml}
    `
    if (applyConfirmBtn) {
      applyConfirmBtn.disabled = ap.blocked
    }
  }

  listbox.addEventListener("valuechange", (event) => {
    if (typeof event.detail.value === "string" && event.detail.value) {
      selectedId = event.detail.value
      renderWorkerDetails()
    }
  })

  if (triageToggle) {
    triageToggle.addEventListener("pressedchange", (event) => {
      triageOnly = Boolean(event.detail.value)
      renderWorkers()
      renderWorkerDetails()
    })
  }

  if (activityFilter) {
    activityFilter.addEventListener("valuechange", (event) => {
      if (typeof event.detail.value === "string") {
        filterMode = event.detail.value
        renderWorkerDetails()
      }
    })
  }

  if (logKindControl) {
    logKindControl.addEventListener("valuechange", (event) => {
      if (typeof event.detail.value === "string") {
        logMode = event.detail.value
        logPre.textContent = currentWorker().logs[logMode] || ""
      }
    })
  }

  if (applyConfirmBtn) {
    applyConfirmBtn.addEventListener("action", () => {
      const w = currentWorker()
      applyStatus.textContent = `Simulated applying ${w.applyPreview.files.length} verified files from ${w.alias}.`
      if (applyDialog) setTimeout(() => { applyDialog.open = false }, 700)
    })
  }

  renderWorkers()
  renderWorkerDetails()
}

function initArbiterDemo() {
  const ctrl = document.getElementById("arbiter-step")
  const stageEl = document.getElementById("arbiter-stage")
  if (!ctrl || !stageEl) return

  function render(key) {
    const s = ARBITER_STEPS[key] || ARBITER_STEPS.write
    const bars = s.squares
      .map((ok) => `<span class="t-sq ${ok ? "ok" : "fail"}" aria-hidden="true"></span>`)
      .join("")
    stageEl.innerHTML = `
      <div class="arb-top">
        <span class="arb-tag">${esc(s.tag)}</span>
        <span class="arb-score ${s.verdictClass}">${esc(s.score)}</span>
      </div>
      <div class="t-bars" role="img" aria-label="${esc(s.score)} tests">${bars}</div>
      <div class="arb-meta">
        <span class="arb-verdict ${s.verdictClass}">${esc(s.verdict)}</span>
        <span class="arb-head">${esc(s.head)}</span>
      </div>
      <div class="arb-cmd">$ ${esc(s.cmd)}</div>
    `
  }

  ctrl.addEventListener("valuechange", (e) => {
    if (typeof e.detail.value === "string") render(e.detail.value)
  })
  render("write")
}

function initHeroCompare() {
  const card = document.getElementById("compare-card")
  const tabs = document.getElementById("hero-compare")
  const typed = document.getElementById("cmp-typed")
  const stat = document.getElementById("cmp-stat")
  const replay = document.getElementById("cmp-replay")
  if (!card || !tabs || !typed) return

  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches
  const fullText = typed.dataset.text || typed.textContent || ""
  const steps = ["step-1", "step-2", "step-3", "step-4"]
  const wait = (ms) => new Promise((r) => setTimeout(r, ms))
  let runSeq = 0

  function setStat(mode) {
    card.dataset.mode = mode
    if (!stat) return
    stat.textContent = mode === "manual-tabs"
      ? "dirty tree · 3 regressions"
      : "0 collisions · verified"
  }

  async function play(mode, withTyping) {
    const seq = ++runSeq
    setStat(mode)
    card.classList.remove("is-typing", ...steps)
    void card.offsetWidth

    if (reduceMotion) {
      typed.textContent = fullText
      card.classList.add(...steps)
      return
    }

    if (withTyping) {
      card.classList.add("is-typing")
      typed.textContent = ""
      await wait(120)
      for (let i = 3; i <= fullText.length + 2; i += 3) {
        if (seq !== runSeq) return
        typed.textContent = fullText.slice(0, Math.min(i, fullText.length))
        await wait(16)
      }
      if (seq !== runSeq) return
      card.classList.remove("is-typing")
      await wait(90)
    } else {
      typed.textContent = fullText
      await wait(60)
    }

    const delays = mode === "manual-tabs" ? [320, 320, 340, 260] : [340, 340, 360, 260]
    for (let i = 0; i < steps.length; i += 1) {
      if (seq !== runSeq) return
      card.classList.add(steps[i])
      await wait(delays[i])
    }
  }

  tabs.addEventListener("valuechange", (event) => {
    const next = typeof event.detail.value === "string" ? event.detail.value : "with-claive"
    play(next, false)
  })

  if (replay) {
    replay.addEventListener("action", () => {
      play(tabs.value || "with-claive", true)
    })
  }

  card.classList.add("is-armed")
  play(tabs.value || "with-claive", true)
}

async function boot() {
  await Promise.all([
    customElements.whenDefined("ui-toggle"),
    customElements.whenDefined("ui-listbox"),
    customElements.whenDefined("ui-tabs"),
    customElements.whenDefined("ui-segmented-control"),
    customElements.whenDefined("ui-accordion"),
    customElements.whenDefined("ui-dialog"),
    customElements.whenDefined("ui-button"),
  ])
  initTheme()
  initHeroCompare()
  initCockpit()
  initArbiterDemo()
  wireCopyButton(
    "hero-copy",
    () => "git clone https://github.com/ham-zax/claive.git && cd claive && just dash-build && ./install.sh --with-tui",
    "Copy",
  )
  const qsTabs = document.getElementById("quickstart-tabs")
  wireCopyButton(
    "qs-copy",
    () => QS_SNIPPETS[qsTabs?.value || "install"] || QS_SNIPPETS.install,
    "Copy snippet",
  )
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true })
} else {
  boot()
}
