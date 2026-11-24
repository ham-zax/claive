//! Phase 1 integration, contract, failure-mode, and Ratatui TestBackend tests.

use std::fs::{self, Permissions};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Terminal;
use tempfile::TempDir;

use claive_tui::app::{
    classify_worker, App, AppConfig, InspectorTab, KeyOutcome, TriageBadge,
};
use claive_tui::commands::{execute_peek, ReadResponse};
use claive_tui::proc::pid_identity;
use claive_tui::store::{load_snapshot, now_unix_secs, RegistryReader, WorkerRecord};
use claive_tui::ui;

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn write_json(path: &Path, val: &serde_json::Value) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, serde_json::to_string_pretty(val).unwrap()).unwrap();
}

fn make_app(dir: PathBuf, compact: bool, triage: bool, focus: Option<String>) -> App {
    App::new(AppConfig {
        claive_dir: dir,
        claive_bin: "claive".to_string(),
        compact,
        triage,
        all_sessions: true,
        session: None,
        workspace: None,
        focus,
        actions: false,
        read_only: true,
    })
}

fn buffer_text(terminal: &Terminal<TestBackend>) -> String {
    let buf = terminal.backend().buffer();
    let area = buf.area;
    let mut out = String::new();
    for y in 0..area.height {
        for x in 0..area.width {
            out.push_str(buf[(x, y)].symbol());
        }
        out.push('\n');
    }
    out
}

#[test]
fn chmod_500_registry_ingests_with_zero_writes_and_zero_locks() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let my_pid = std::process::id() as i32;
    let my_ident = pid_identity(my_pid).expect("current pid identity");
    let now = now_unix_secs();

    let job_id = "0123456789ab";
    let job_dir = root.join(job_id);
    fs::create_dir_all(job_dir.join("requests")).unwrap();
    fs::create_dir_all(root.join("watchers")).unwrap();
    fs::create_dir_all(root.join("notifications")).unwrap();

    write_json(
        &job_dir.join("state.json"),
        &serde_json::json!({
            "id": job_id,
            "label": "Read-only test worker",
            "engine": "codex",
            "model": "gpt-6.2-sol",
            "workspace": "/home/hamza/repo/claive",
            "parent_session": "claude-abc",
            "status": "running",
            "phase": "running",
            "turn": 1,
            "started_at": now - 45.0,
            "turn_started_at": now - 45.0,
            "last_activity": now - 5.0,
            "supervisor_pid": my_pid,
            "supervisor_identity": my_ident,
        }),
    );
    write_json(
        &job_dir.join("status.json"),
        &serde_json::json!({
            "id": job_id,
            "alias": "ro-test",
            "label": "Read-only test worker",
            "engine": "codex",
            "model": "gpt-6.2-sol",
            "workspace": "/home/hamza/repo/claive",
            "parent_session": "claude-abc",
            "status": "running",
            "phase": "running",
            "turn": 1,
            "elapsed_s": 45.0,
            "turn_elapsed_s": 45.0,
            "last_event_age_s": 5.0,
            "changed_files": ["src/lib.rs"],
            "changed_files_total": 1,
        }),
    );
    write_json(
        &root.join("aliases.json"),
        &serde_json::json!({ "ro-test": job_id }),
    );
    // Include one dead watcher to prove the dashboard NEVER prunes watchers/*.json.
    write_json(
        &root.join("watchers/999999-deadbeef.json"),
        &serde_json::json!({
            "id": "999999-deadbeef",
            "pid": 999999,
            "pid_identity": "bogus:0",
            "worker_ids": [job_id],
        }),
    );
    fs::write(
        root.join("inbox.jsonl"),
        "{\"type\":\"turn\",\"seq\":1,\"at\":\"2026-11-23T10:00:00Z\",\"id\":\"0123456789ab\",\"status\":\"completed\",\"wake\":true,\"digest\":{\"report_headline\":\"ok\"}}\n",
    )
    .unwrap();
    fs::write(
        root.join("notifications/receipts.jsonl"),
        "{\"seq\":1795300000000000000,\"at\":\"2026-11-23T10:00:01Z\",\"event_id\":\"0123456789ab\",\"event_type\":\"turn\",\"sink\":\"desktop\",\"kind\":\"command\",\"status\":\"ok\",\"duration_ms\":12}\n",
    )
    .unwrap();

    // Lock down directory permissions to strictly read-only (0o500).
    for dir in [
        job_dir.join("requests"),
        job_dir.clone(),
        root.join("watchers"),
        root.join("notifications"),
        root.clone(),
    ] {
        fs::set_permissions(&dir, Permissions::from_mode(0o500)).unwrap();
    }

    let snap = load_snapshot(&root, now);

    // Restore permissions before assertions so TempDir cleanup always succeeds.
    for dir in [
        root.clone(),
        root.join("watchers"),
        root.join("notifications"),
        job_dir.clone(),
        job_dir.join("requests"),
    ] {
        let _ = fs::set_permissions(&dir, Permissions::from_mode(0o700));
    }

    assert!(snap.stale_reason.is_none());
    assert_eq!(snap.workers.len(), 1);
    assert_eq!(snap.workers[0].alias.as_deref(), Some("ro-test"));
    assert_eq!(snap.workers[0].effective_status, "running");
    assert!(snap.live_watchers.is_empty(), "dead watcher ignored");
    assert!(
        root.join("watchers/999999-deadbeef.json").exists(),
        "dead watcher MUST NOT be pruned by read-only dashboard"
    );
    assert!(
        !root.join(".inbox.lock").exists(),
        "dashboard MUST NOT create .inbox.lock"
    );
    assert_eq!(snap.inbox.len(), 1);
    assert_eq!(snap.receipts.len(), 1);
}

#[test]
fn python_status_file_contract_and_open_enum_compatibility() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    let job_id = "abcdef012345";
    let job_dir = root.join(job_id);
    let workspace = tmp.path().join("ws");
    fs::create_dir_all(&job_dir).unwrap();
    fs::create_dir_all(&workspace).unwrap();

    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let bin_dir = repo_root.join("bin");

    let script = r#"
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from claivelib import status_file

job_dir = Path(sys.argv[2])
workspace = sys.argv[3]
state = {
    "id": "abcdef012345",
    "label": "Python contract worker\x1b[31m red\x1b[0m",
    "engine": "future_quantum_engine",
    "model": "q-1",
    "workspace": workspace,
    "reusable": True,
    "parent_session": "agy-99",
    "status": "completed",
    "phase": "custom_future_phase",
    "turn": 2,
    "started_at": 1000.0,
    "ended_at": 1042.0,
    "last_activity": 1042.0,
    "exit_code": 3,
    "needs_parent": {"kind": "needs_decision", "question": "Ship\x1b]0;pwn\x07 now?"},
    "touched_files": [f"src/file_{i:03d}.rs" for i in range(100)],
    "report": {"status": "done", "summary": "All green", "risks": ["check edge"]},
    "turn_usage": {"native_cache_read_tokens": 42100},
}
(job_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
status_file.write(job_dir, state)
"#;

    let out = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(&bin_dir)
        .arg(&job_dir)
        .arg(&workspace)
        .output()
        .expect("run python3 status_file.write");
    assert!(
        out.status.success(),
        "python contract script failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let snap = load_snapshot(root, 1050.0);
    assert_eq!(snap.workers.len(), 1);
    let w = &snap.workers[0];
    assert_eq!(w.engine, "future_quantum_engine");
    assert_eq!(w.phase, "custom_future_phase");
    assert_eq!(w.changed_files_total, 100);
    assert!(w.changed_files_truncated);
    assert_eq!(w.changed_files.len(), 64);
    assert_eq!(w.native_cache_read_tokens, Some(42100));
    assert!(!w.label.contains('\x1b'));
    assert!(w.label.contains("Python contract worker"));
    let ask = w.needs_parent.as_ref().expect("needs_parent parsed");
    assert!(!ask.question.contains('\x1b'));
    assert!(ask.question.contains("Ship"));
    assert_eq!(w.report_summary.as_deref(), Some("All green"));
    assert_eq!(w.report_risks, vec!["check edge".to_string()]);
}

#[test]
fn missing_and_oversized_status_json_fall_back_cleanly_and_torn_inbox_recovers() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let now = now_unix_secs();

    // Worker 1: state.json only (startup race before status.json exists).
    let w1 = "111111111111";
    write_json(
        &root.join(w1).join("state.json"),
        &serde_json::json!({
            "id": w1,
            "label": "Starting without status.json",
            "engine": "pi",
            "status": "completed",
            "phase": "done",
            "turn": 1,
            "started_at": now - 10.0,
            "ended_at": now - 2.0,
        }),
    );

    // Worker 2: oversized (>1 MiB) status.json forces clean fallback to state.json.
    let w2 = "222222222222";
    write_json(
        &root.join(w2).join("state.json"),
        &serde_json::json!({
            "id": w2,
            "label": "Oversized status fallback",
            "engine": "claude",
            "status": "completed",
            "phase": "done",
            "turn": 1,
            "started_at": now - 20.0,
            "ended_at": now - 1.0,
        }),
    );
    let huge = format!("{{\"id\":\"{w2}\",\"pad\":\"{}\"}}", "x".repeat(1_200_000));
    fs::write(root.join(w2).join("status.json"), huge).unwrap();

    // Torn trailing inbox line (no newline yet).
    let inbox_path = root.join("inbox.jsonl");
    fs::write(
        &inbox_path,
        "{\"type\":\"turn\",\"seq\":1,\"at\":\"2026-11-23T10:00:00Z\",\"id\":\"111111111111\",\"status\":\"completed\",\"wake\":true}\n{\"type\":\"turn\",\"seq\":2,\"partial\":",
    )
    .unwrap();

    let mut reader = RegistryReader::new(root.clone());
    let snap1 = reader.scan(now);
    assert_eq!(snap1.workers.len(), 2);
    for w in &snap1.workers {
        assert!(w.status_projection_stale, "both should flag fallback");
    }
    assert_eq!(snap1.inbox.len(), 1);
    assert_eq!(snap1.inbox[0].seq, Some(1));

    // Simulate torn-tail repair (truncate & append valid line 2, as in inbox.py:342).
    fs::write(
        &inbox_path,
        "{\"type\":\"turn\",\"seq\":1,\"at\":\"2026-11-23T10:00:00Z\",\"id\":\"111111111111\",\"status\":\"completed\",\"wake\":true}\n{\"type\":\"turn\",\"seq\":2,\"at\":\"2026-11-23T10:00:05Z\",\"id\":\"222222222222\",\"status\":\"completed\",\"wake\":true}\n",
    )
    .unwrap();
    let snap2 = reader.scan(now + 1.0);
    assert_eq!(snap2.inbox.len(), 2);
    assert_eq!(snap2.inbox[1].seq, Some(2));
}

#[test]
fn triage_priority_costimulated_quiet_selection_stability_and_test_backend_views() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let my_pid = std::process::id() as i32;
    let my_ident = pid_identity(my_pid).unwrap();
    let now = now_unix_secs();

    // Watch session `claude-1` so workers there are watched.
    write_json(
        &root.join("watchers/live.json"),
        &serde_json::json!({
            "id": "live",
            "pid": my_pid,
            "pid_identity": my_ident,
            "parent_session": "claude-1",
            "worker_ids": [],
        }),
    );

    // 1. ASK worker (highest priority)
    let id_ask = "aaaa00000001";
    write_json(
        &root.join(id_ask).join("state.json"),
        &serde_json::json!({
            "id": id_ask, "label": "Ask worker", "engine": "claude",
            "workspace": "/home/hamza/repo/claive", "parent_session": "claude-1",
            "reusable": true, "status": "idle", "phase": "idle", "turn": 1,
            "started_at": now - 200.0, "last_activity": now - 10.0,
            "supervisor_pid": my_pid, "supervisor_identity": my_ident,
            "needs_parent": {"kind": "needs_decision", "question": "Approve schema change?"},
        }),
    );

    // 2. UNWATCHED active worker (different session with no watcher)
    let id_unw = "bbbb00000002";
    write_json(
        &root.join(id_unw).join("state.json"),
        &serde_json::json!({
            "id": id_unw, "label": "Unwatched worker", "engine": "agy",
            "workspace": "/home/hamza/repo/claive", "parent_session": "agy-orphan",
            "status": "running", "phase": "running", "turn": 1,
            "started_at": now - 100.0, "last_activity": now - 8.0,
            "supervisor_pid": my_pid, "supervisor_identity": my_ident,
        }),
    );

    // 3. BUSY (active tool running > 60s) -> must NOT trigger QUIET!
    let id_busy = "cccc00000003";
    write_json(
        &root.join(id_busy).join("state.json"),
        &serde_json::json!({
            "id": id_busy, "label": "Running pytest", "engine": "codex",
            "workspace": "/home/hamza/repo/claive", "parent_session": "claude-1",
            "status": "running", "phase": "tool: pytest", "turn": 1,
            "started_at": now - 150.0, "last_activity": now - 90.0,
            "active_tools": {"t1": {"tool": "pytest"}},
            "supervisor_pid": my_pid, "supervisor_identity": my_ident,
        }),
    );

    // 4. QUIET (no active tool and silent >= 60s) -> MUST trigger QUIET!
    let id_quiet = "dddd00000004";
    write_json(
        &root.join(id_quiet).join("state.json"),
        &serde_json::json!({
            "id": id_quiet, "label": "Silent stalled worker", "engine": "codex",
            "workspace": "/home/hamza/repo/claive", "parent_session": "claude-1",
            "status": "running", "phase": "waiting for model", "turn": 1,
            "started_at": now - 140.0, "last_activity": now - 95.0,
            "supervisor_pid": my_pid, "supervisor_identity": my_ident,
        }),
    );

    // 5. REVIEW (settled managed worktree pending acceptance)
    let id_rev = "eeee00000005";
    let wt_dir = root.join(id_rev).join("worktree");
    fs::create_dir_all(&wt_dir).unwrap();
    write_json(
        &root.join(id_rev).join("state.json"),
        &serde_json::json!({
            "id": id_rev, "label": "Docs refactor", "engine": "pi",
            "workspace": "/home/hamza/repo/claive", "parent_session": "claude-1",
            "status": "completed", "phase": "done", "turn": 1,
            "started_at": now - 80.0, "ended_at": now - 5.0, "last_activity": now - 5.0,
            "retained_worktree": wt_dir.to_string_lossy(),
            "launch": {"isolation": {"owner": "claive"}},
        }),
    );
    write_json(
        &root.join(id_rev).join("status.json"),
        &serde_json::json!({
            "id": id_rev, "alias": "docs", "label": "Docs refactor", "engine": "pi",
            "workspace": "/home/hamza/repo/claive", "parent_session": "claude-1",
            "status": "completed", "phase": "done", "turn": 1,
            "retained_worktree": wt_dir.to_string_lossy(),
            "changed_files": ["README.md", "docs/a.md", "docs/b.md"],
            "changed_files_total": 3,
            "verification_status": "passed",
            "acceptance_status": "pending",
        }),
    );
    write_json(
        &root.join("aliases.json"),
        &serde_json::json!({"parser": id_ask, "tests": id_unw, "docs": id_rev}),
    );

    let snap = load_snapshot(&root, now);
    let mut app = make_app(root.clone(), false, false, None);
    app.apply_snapshot(snap);

    let ordered_badges: Vec<(&str, TriageBadge)> = app
        .visible_workers
        .iter()
        .map(|&i| {
            let w: &WorkerRecord = &app.snapshot.workers[i];
            (w.id.as_str(), classify_worker(w))
        })
        .collect();

    assert_eq!(
        ordered_badges,
        vec![
            (id_ask, TriageBadge::Ask),
            (id_unw, TriageBadge::Unwatched),
            (id_rev, TriageBadge::Review),
            (id_quiet, TriageBadge::Quiet),
            (id_busy, TriageBadge::Run),
        ]
    );

    // Move selection to `id_rev`, toggle grouping & re-apply snapshot: selection MUST stay on `id_rev`.
    app.handle_key(key(KeyCode::Char('j')));
    app.handle_key(key(KeyCode::Char('j')));
    assert_eq!(app.selected_id.as_deref(), Some(id_rev));
    app.handle_key(key(KeyCode::Char('g')));
    assert!(app.group_by_session);
    assert_eq!(app.selected_id.as_deref(), Some(id_rev));

    // Remediation copy (`y`) on REVIEW with verification=passed -> `claive apply <id> --close`.
    assert_eq!(
        app.handle_key(key(KeyCode::Char('y'))),
        KeyOutcome::CopyClipboard(format!("claive apply {id_rev} --close"))
    );

    // Toggle exception-only triage mode (`t`) -> hides QUIET and RUN, keeps ASK/UNWATCHED/REVIEW.
    app.handle_key(key(KeyCode::Char('t')));
    assert_eq!(app.visible_workers.len(), 3);
    app.handle_key(key(KeyCode::Char('t')));
    app.handle_key(key(KeyCode::Char('g'))); // back to flat

    // Render across all breakpoints with TestBackend (zero panics, key content present).
    // 1. Wide (120x36) with drawer open
    app.handle_key(key(KeyCode::Char('i')));
    let mut wide_term = Terminal::new(TestBackend::new(120, 36)).unwrap();
    wide_term.draw(|f| ui::render(f, &mut app)).unwrap();
    let wide_txt = buffer_text(&wide_term);
    assert!(wide_txt.contains("CLAIVE"));
    assert!(wide_txt.contains("@parser"));
    assert!(wide_txt.contains("UNWATCHED"));
    assert!(wide_txt.contains("Drawer:"));

    // 2. Help modal
    app.handle_key(key(KeyCode::Char('?')));
    wide_term.draw(|f| ui::render(f, &mut app)).unwrap();
    assert!(buffer_text(&wide_term).contains("Read-Only Mode"));
    app.handle_key(key(KeyCode::Esc));

    // 3. Medium (90x26)
    let mut med_term = Terminal::new(TestBackend::new(90, 26)).unwrap();
    med_term.draw(|f| ui::render(f, &mut app)).unwrap();
    assert!(buffer_text(&med_term).contains("Workers"));

    // 4. Narrow (60x18)
    app.handle_key(key(KeyCode::Char('i'))); // close drawer
    let mut narrow_term = Terminal::new(TestBackend::new(60, 18)).unwrap();
    narrow_term.draw(|f| ui::render(f, &mut app)).unwrap();
    assert!(buffer_text(&narrow_term).contains("Workers"));
    app.handle_key(key(KeyCode::Enter));
    narrow_term.draw(|f| ui::render(f, &mut app)).unwrap();
    assert!(buffer_text(&narrow_term).contains("Inspector"));

    // 5. Compact 5-row pane (80x5)
    let mut compact_app = make_app(root.clone(), true, false, None);
    compact_app.apply_snapshot(load_snapshot(&root, now));
    let mut compact_term = Terminal::new(TestBackend::new(80, 5)).unwrap();
    compact_term
        .draw(|f| ui::render(f, &mut compact_app))
        .unwrap();
    let compact_txt = buffer_text(&compact_term);
    assert!(compact_txt.contains("claive"));
    assert!(compact_txt.contains("1 ASK"));
    assert!(compact_txt.contains("@parser"));
    assert!(compact_txt.contains("+2 more"));
}

#[test]
fn focus_flag_selects_worker_and_unseen_settlement_clears_on_enter() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let my_pid = std::process::id() as i32;
    let my_ident = pid_identity(my_pid).unwrap();
    let now = now_unix_secs();

    let id1 = "aaaa11111111";
    let id2 = "bbbb22222222";
    write_json(
        &root.join(id1).join("state.json"),
        &serde_json::json!({
            "id": id1, "label": "Worker 1", "engine": "pi",
            "status": "running", "phase": "running", "turn": 1,
            "started_at": now - 20.0, "last_activity": now - 2.0,
            "supervisor_pid": my_pid, "supervisor_identity": my_ident,
        }),
    );
    write_json(
        &root.join(id2).join("state.json"),
        &serde_json::json!({
            "id": id2, "label": "Worker 2", "engine": "pi",
            "status": "completed", "phase": "done", "turn": 1,
            "started_at": now - 30.0, "ended_at": now - 10.0,
        }),
    );
    write_json(&root.join("aliases.json"), &serde_json::json!({"target": id2}));

    let mut app = make_app(root.clone(), false, true, Some("@target".to_string()));
    app.apply_snapshot(load_snapshot(&root, now));
    assert_eq!(app.selected_id.as_deref(), Some(id2));
    assert!(!app.is_unseen(id2), "already-settled worker on cold start is seen");

    // Transition id1 from running -> completed mid-session: must gain unseen dot!
    write_json(
        &root.join(id1).join("state.json"),
        &serde_json::json!({
            "id": id1, "label": "Worker 1", "engine": "pi",
            "status": "completed", "phase": "done", "turn": 1,
            "started_at": now - 20.0, "ended_at": now + 1.0,
        }),
    );
    app.apply_snapshot(load_snapshot(&root, now + 2.0));
    assert!(app.is_unseen(id1), "newly settled worker should be marked unseen");

    // Select id1 and press Enter -> clears unseen dot.
    app.selected_id = Some(id1.to_string());
    app.recompute_view();
    app.handle_key(key(KeyCode::Enter));
    assert!(!app.is_unseen(id1));
}

#[test]
fn fake_cli_peek_sanitizes_ansi_controls_and_renders_steps_tab() {
    let tmp = TempDir::new().unwrap();
    let fake_cli = tmp.path().join("fake-claive");
    fs::write(
        &fake_cli,
        "#!/usr/bin/env python3\n\
         import json, sys\n\
         print(json.dumps({\n\
           'total': 2,\n\
           'intent': 'Fixing \\x1b[31mbug\\x1b[0m',\n\
           'steps': [\n\
             {'index': 1, 'kind': 'tool', 'text': 'bash \\x1b]0;title\\x07pytest -q'},\n\
             {'index': 2, 'kind': 'text', 'text': 'All green'}\n\
           ]\n\
         }))\n",
    )
    .unwrap();
    fs::set_permissions(&fake_cli, Permissions::from_mode(0o755)).unwrap();

    let res = execute_peek(
        "abcdef123456",
        10,
        fake_cli.to_str().unwrap(),
        tmp.path(),
        Some((100, 1)),
    );
    assert!(res.error.is_none());
    assert_eq!(res.total, 2);
    assert_eq!(res.intent.as_deref(), Some("Fixing bug"));
    assert_eq!(res.steps.len(), 2);
    assert!(!res.steps[0].text.contains('\x1b'));
    assert!(res.steps[0].text.contains("pytest -q"));

    // Verify rendering in Steps tab doesn't panic and displays sanitized steps.
    let mut app = make_app(tmp.path().to_path_buf(), false, false, None);
    write_json(
        &tmp.path().join("abcdef123456").join("state.json"),
        &serde_json::json!({
            "id": "abcdef123456", "label": "Peek test", "engine": "pi",
            "status": "completed", "phase": "done", "turn": 1,
        }),
    );
    app.apply_snapshot(load_snapshot(tmp.path(), now_unix_secs()));
    app.apply_read_response(ReadResponse::Peek(res));
    app.inspector_tab = InspectorTab::Steps;

    let mut term = Terminal::new(TestBackend::new(120, 36)).unwrap();
    term.draw(|f| ui::render(f, &mut app)).unwrap();
    let txt = buffer_text(&term);
    assert!(txt.contains("Fixing bug"));
    assert!(txt.contains("pytest -q"));
}

#[test]
fn slices_2_3_4_briefing_structured_activity_wide_split_and_prompt_log() {
    use claive_tui::app::ActivityFilter;
    use claive_tui::commands::{read_log_tail_with_prompt, LogKind};

    let tmp = TempDir::new().unwrap();
    let wid = "fedcba654321";
    let job_dir = tmp.path().join(wid);
    fs::create_dir_all(&job_dir).unwrap();
    fs::write(
        job_dir.join("prompt-0001.md"),
        "# Mission\nImplement [progress.py](bin/claivelib/progress.py) cleanly.\n",
    )
    .unwrap();
    write_json(
        &job_dir.join("state.json"),
        &serde_json::json!({
            "id": wid,
            "label": "Upgrade TUI",
            "mission": "Deliver Slices 2-4 with paired activity",
            "engine": "codex",
            "model": "gpt-5.4",
            "status": "running",
            "phase": "running",
            "turn": 1,
            "prompt_file": "prompt-0001.md",
            "latest_intent": "Running unit tests for [store.rs](tui/src/store.rs)",
            "activity": {
                "stage": "testing",
                "label": "Test tests/test_peek.py",
                "targets": ["tests/test_peek.py"]
            },
            "last_check": {
                "command": "pytest tests/test_peek.py",
                "status": "passed",
                "summary": "37 passed"
            }
        }),
    );

    let fake_cli = tmp.path().join("fake-claive-structured");
    fs::write(
        &fake_cli,
        "#!/usr/bin/env python3\n\
         import json, sys\n\
         assert '--structured' in sys.argv\n\
         print(json.dumps({\n\
           'total': 4,\n\
           'intent': 'All checks green',\n\
           'steps': [],\n\
           'actions': [\n\
             {'id': 1, 'kind': 'action', 'category': 'read', 'stage': 'exploring', 'tool': 'read',\n\
              'target': 'tui/src/ui.rs', 'summary': 'tui/src/ui.rs', 'outcome': '42 lines', 'status': 'ok'},\n\
             {'id': 2, 'kind': 'action', 'category': 'test', 'stage': 'testing', 'tool': 'shell',\n\
              'target': 'tests/test_peek.py', 'summary': 'pytest tests/test_peek.py', 'outcome': '37 passed',\n\
              'status': 'ok', 'exit_code': 0, 'command': 'pytest tests/test_peek.py', 'output': '37 passed'},\n\
             {'id': 3, 'kind': 'say', 'category': 'say', 'stage': 'narrative',\n\
              'summary': 'Finished [ui.rs](tui/src/ui.rs) update', 'status': 'done',\n\
              'output': 'Finished [ui.rs](tui/src/ui.rs) update'}\n\
           ]\n\
         }))\n",
    )
    .unwrap();
    fs::set_permissions(&fake_cli, Permissions::from_mode(0o755)).unwrap();

    let snap = load_snapshot(tmp.path(), now_unix_secs());
    let w = &snap.workers[0];
    assert_eq!(w.activity.as_ref().unwrap().stage, "testing");
    assert_eq!(w.last_check.as_ref().unwrap().summary, "37 passed");
    assert_eq!(w.prompt_file.as_deref(), Some("prompt-0001.md"));

    let (prompt_content, _) = read_log_tail_with_prompt(&job_dir, LogKind::Prompt, w.prompt_file.as_deref());
    assert!(prompt_content.contains("Implement [progress.py]"));

    let res = execute_peek(wid, 20, fake_cli.to_str().unwrap(), tmp.path(), Some((200, 2)));
    assert!(res.error.is_none());
    assert_eq!(res.actions.len(), 3);

    let mut app = make_app(tmp.path().to_path_buf(), false, false, None);
    app.apply_snapshot(snap);
    app.apply_read_response(ReadResponse::Peek(res));

    // Actions filter shows all 3; Milestones filter hides READ (keeps TEST + SAY).
    assert_eq!(app.visible_steps_for(wid).0.len(), 3);
    app.activity_filter = ActivityFilter::Milestones;
    let (ms, hidden) = app.visible_steps_for(wid);
    assert_eq!(ms.len(), 2);
    assert_eq!(hidden, 1);

    // 3-column wide split renders Agents | Inspector (Activity) | Briefing at 160x40.
    app.inspector_tab = InspectorTab::Steps;
    let mut term = Terminal::new(TestBackend::new(160, 40)).unwrap();
    term.draw(|f| ui::render(f, &mut app)).unwrap();
    let txt = buffer_text(&term);
    assert!(txt.contains("TASK") && (txt.contains("LAUNCHED") || txt.contains("AGO")));
    assert!(txt.contains("Briefing:"));
    let header_line = txt.lines().find(|l| l.contains("Briefing:")).unwrap();
    assert!(
        header_line.find("Inspector:").unwrap() < header_line.find("Briefing:").unwrap(),
        "expected Inspector (Activity) in middle and Briefing on right: {header_line}"
    );
    assert!(txt.contains("37 passed"));
    assert!(txt.contains("Finished ui.rs update"));

    // Standard 2-column wide layout (160x40 on Briefing tab) shows explicit LAUNCHED & RUNTIME columns.
    app.inspector_tab = InspectorTab::Overview;
    term.draw(|f| ui::render(f, &mut app)).unwrap();
    let wide_txt = buffer_text(&term);
    assert!(wide_txt.contains("LAUNCHED") && wide_txt.contains("RUNTIME"));
    assert!(wide_txt.contains("TASK & STATUS"));
}

#[test]
fn read_lane_uses_native_events_tailer_without_cli() {
    use claive_tui::commands::ReadLane;

    let tmp = TempDir::new().unwrap();
    let wid = "beef12345678";
    let job_dir = tmp.path().join(wid);
    fs::create_dir_all(&job_dir).unwrap();
    fs::write(
        job_dir.join("events.jsonl"),
        format!(
            "{}\n{}\n{}\n",
            serde_json::json!({"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "/bin/bash -lc 'cargo test --manifest-path tui/Cargo.toml'"}}),
            serde_json::json!({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "exit_code": 0, "aggregated_output": "30 passed"}}),
            serde_json::json!({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "All tests green."}})
        ),
    )
    .unwrap();

    let mut lane = ReadLane::spawn();
    // Passing a nonexistent CLI proves the native Rust tailer handles Codex with zero subprocesses.
    let queued = lane.maybe_request_peek(
        wid,
        &job_dir,
        "codex",
        None,
        20,
        "/nonexistent/claive-bin-must-not-run",
        tmp.path(),
        Some((100, 1)),
        true,
    );
    assert!(queued);

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let mut got = None;
    while std::time::Instant::now() < deadline {
        for resp in lane.drain_responses() {
            if let ReadResponse::Peek(res) = resp {
                got = Some(res);
                break;
            }
        }
        if got.is_some() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let res = got.expect("native ReadLane peek response");
    assert!(res.error.is_none(), "unexpected error: {:?}", res.error);
    assert_eq!(res.actions.len(), 2);
    assert_eq!(res.actions[0].category, "test");
    assert_eq!(res.actions[0].outcome.as_deref(), Some("30 passed"));
    assert_eq!(res.actions[1].kind, "say");
    assert_eq!(res.intent.as_deref(), Some("All tests green."));
}
