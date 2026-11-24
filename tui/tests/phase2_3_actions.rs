//! Phase 2 & 3 interactive action, single-flight mutation lane, dual-driver hijack gate,
//! real Python CLI `answer` / `apply --dry-run` contract, and modal TestBackend tests.

use std::fs::{self, Permissions};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Terminal;
use tempfile::TempDir;

use claive_tui::app::{ActiveModal, App, AppConfig, KeyOutcome};
use claive_tui::commands::{
    execute_apply_dry_run, execute_mutation, MutationEvent, MutationKind, MutationLane,
    MutationRequest, ReadResponse, SendMode,
};
use claive_tui::proc::pid_identity;
use claive_tui::store::{load_snapshot, now_unix_secs};
use claive_tui::ui;

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}

fn write_json(path: &Path, val: &serde_json::Value) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, serde_json::to_string_pretty(val).unwrap()).unwrap();
}

fn make_app(dir: PathBuf, bin: String, actions: bool) -> App {
    App::new(AppConfig {
        claive_dir: dir,
        claive_bin: bin,
        compact: false,
        triage: false,
        all_sessions: true,
        session: None,
        workspace: None,
        focus: None,
        actions,
        read_only: !actions,
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

fn repo_claive_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("bin/claive")
}

#[test]
fn read_only_mode_blocks_actions_and_guides_user() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let now = now_unix_secs();
    let id = "1111aaaa2222";
    write_json(
        &root.join(id).join("state.json"),
        &serde_json::json!({
            "id": id, "label": "Worker", "engine": "pi",
            "status": "completed", "phase": "done", "turn": 1,
        }),
    );

    let mut app = make_app(root.clone(), "claive".to_string(), false);
    app.apply_snapshot(load_snapshot(&root, now));

    assert_eq!(app.handle_key(key(KeyCode::Char('p'))), KeyOutcome::None);
    assert!(app.modal.is_none());
    let toast = app.toast.as_ref().expect("guidance toast shown");
    assert!(toast.text.contains("--actions"));
}

#[test]
fn dual_driver_hijack_gate_and_real_python_answer_turn_hash_contract() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().to_path_buf();
    let my_pid = std::process::id() as i32;
    let my_ident = pid_identity(my_pid).unwrap();
    let now = now_unix_secs();
    let id = "aaaa1234bbbb";
    let job_dir = root.join(id);
    fs::create_dir_all(job_dir.join("requests")).unwrap();

    // Live watcher covers this worker -> MUST require Ctrl-Y hijack confirmation.
    write_json(
        &root.join("watchers/w1.json"),
        &serde_json::json!({
            "id": "w1",
            "pid": my_pid,
            "pid_identity": my_ident,
            "parent_session": "claude-live",
            "worker_ids": [],
        }),
    );

    // Reusable idle worker with an open ASK containing raw newlines (tests raw SHA-256 parity!).
    let raw_question = "Approve DB migration?\nIncludes drop column.";
    write_json(
        &job_dir.join("state.json"),
        &serde_json::json!({
            "schema_version": 2,
            "id": id,
            "label": "Migration worker",
            "engine": "pi",
            "model": "deepseek-v4.1-flash-free",
            "reasoning_effort": "max",
            "max_model_steps": null,
            "session_id": "sess-1",
            "workspace": "/home/hamza/repo/claive",
            "parent_session": "claude-live",
            "reusable": true,
            "status": "idle",
            "phase": "idle",
            "turn": 2,
            "started_at": now - 50.0,
            "last_activity": now - 5.0,
            "supervisor_pid": my_pid,
            "supervisor_identity": my_ident,
            "needs_parent": {"kind": "needs_decision", "question": raw_question},
            "launch": {
                "engine": "pi",
                "binary": "/bin/true",
                "provider": "opencode2api",
                "model": "deepseek-v4.1-flash-free",
                "workspace": "/home/hamza/repo/claive",
                "read_only": true,
                "web": false,
            },
        }),
    );
    write_json(
        &job_dir.join("policy.json"),
        &serde_json::json!({
            "reasoning_effort": "max",
            "max_model_steps": null
        }),
    );

    let claive_bin = repo_claive_bin().to_string_lossy().into_owned();
    let mut app = make_app(root.clone(), claive_bin.clone(), true);
    app.apply_snapshot(load_snapshot(&root, now));

    // Open Answer modal (`a`)
    assert_eq!(app.handle_key(key(KeyCode::Char('a'))), KeyOutcome::None);
    assert!(matches!(app.modal, Some(ActiveModal::Composer(_))));

    // Render modal to prove WATCHED BY warning renders cleanly.
    let mut term = Terminal::new(TestBackend::new(120, 36)).unwrap();
    term.draw(|f| ui::render(f, &mut app)).unwrap();
    let rendered = buffer_text(&term);
    assert!(rendered.contains("WATCHED BY claude-live"));
    assert!(rendered.contains("Ctrl-Y"));

    // Type answer text
    for c in "Yes, proceed".chars() {
        app.handle_key(key(KeyCode::Char(c)));
    }

    // Attempt Ctrl-D BEFORE Ctrl-Y hijack confirmation -> MUST be blocked!
    assert_eq!(app.handle_key(ctrl('d')), KeyOutcome::None);
    assert!(app.modal.is_some(), "modal must stay open until hijack confirmed");

    // Confirm hijack (Ctrl-Y) and submit (Ctrl-D)
    app.handle_key(ctrl('y'));
    let outcome = app.handle_key(ctrl('d'));
    let KeyOutcome::SubmitMutation(kind) = outcome else {
        panic!("expected SubmitMutation, got {outcome:?}");
    };

    // Execute against REAL `bin/claive answer`!
    let (ev_tx, _ev_rx) = mpsc::sync_channel(4);
    let req = MutationRequest {
        kind: kind.clone(),
        claive_bin,
        claive_dir: root.clone(),
    };
    let ev1 = execute_mutation(&req, &ev_tx);
    match &ev1 {
        MutationEvent::Finished { ok, message, .. } => {
            assert!(ok, "first real `claive answer` should succeed, got: {message}");
        }
        other => panic!("unexpected event: {other:?}"),
    }

    // Submitting a second answer for the same turn MUST be rejected by Python's duplicate-turn guard!
    let ev2 = execute_mutation(&req, &ev_tx);
    match &ev2 {
        MutationEvent::Finished { ok, message, .. } => {
            assert!(!ok, "duplicate answer for same turn must fail");
            assert!(
                message.contains("already queued"),
                "expected duplicate guard error, got: {message}"
            );
        }
        other => panic!("unexpected event: {other:?}"),
    }
}

#[test]
fn single_flight_mutation_lane_blocks_overlap_and_parses_send_steer_receipts() {
    let tmp = TempDir::new().unwrap();
    let fake_cli_tmp = tmp.path().join("fake-claive.tmp");
    let fake_cli = tmp.path().join("fake-claive");
    fs::write(
        &fake_cli_tmp,
        r#"#!/usr/bin/env python3
import json, sys, time
cmd = sys.argv[1]
if cmd == 'park':
    time.sleep(0.25)
    print(json.dumps({'id': sys.argv[2], 'parked': True}))
elif cmd == 'send':
    print(json.dumps({'id': 'm1', 'result': 'woke worker', 'fallback_from': 'now'}))
elif cmd == 'steer':
    print(json.dumps({'status': 'acknowledged', 'disposition': 'entered turn'}))
"#,
    )
    .unwrap();
    fs::set_permissions(&fake_cli_tmp, Permissions::from_mode(0o755)).unwrap();
    fs::rename(&fake_cli_tmp, &fake_cli).unwrap();

    let lane = MutationLane::spawn();
    let req1 = MutationRequest {
        kind: MutationKind::Park {
            worker_id: "aaaa00001111".to_string(),
            unpark: false,
        },
        claive_bin: fake_cli.to_string_lossy().into_owned(),
        claive_dir: tmp.path().to_path_buf(),
    };
    assert!(lane.submit(req1.clone()).is_ok());
    assert!(lane.is_busy());
    // Second overlapping submission MUST be rejected while first is in flight!
    assert!(lane.submit(req1).is_err());

    // Wait for first completion
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut got = None;
    while Instant::now() < deadline {
        for ev in lane.drain_events() {
            got = Some(ev);
        }
        if got.is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        matches!(got, Some(MutationEvent::Finished { ok: true, .. })),
        "expected park Finished(ok: true), got: {got:?}"
    );
    assert!(!lane.is_busy());

    // Send receipt with now->wake fallback
    let (tx, _rx) = mpsc::sync_channel(4);
    let send_ev = execute_mutation(
        &MutationRequest {
            kind: MutationKind::Send {
                worker_id: "aaaa00001111".to_string(),
                mode: SendMode::Now,
                message: "status?".to_string(),
                message_id: "0123456789abcdef0123456789abcdef".to_string(),
            },
            claive_bin: fake_cli.to_string_lossy().into_owned(),
            claive_dir: tmp.path().to_path_buf(),
        },
        &tx,
    );
    match send_ev {
        MutationEvent::Finished { ok, message, .. } => {
            assert!(ok);
            assert!(message.contains("fallback from now"), "got: {message}");
        }
        other => panic!("unexpected {other:?}"),
    }

    // Steer acknowledged receipt
    let steer_ev = execute_mutation(
        &MutationRequest {
            kind: MutationKind::Steer {
                worker_id: "aaaa00001111".to_string(),
                message: "focus on tests".to_string(),
            },
            claive_bin: fake_cli.to_string_lossy().into_owned(),
            claive_dir: tmp.path().to_path_buf(),
        },
        &tx,
    );
    match steer_ev {
        MutationEvent::Finished { ok, message, .. } => {
            assert!(ok);
            assert!(message.contains("acknowledged"), "got: {message}");
        }
        other => panic!("unexpected {other:?}"),
    }
}

#[test]
fn apply_preview_surfaces_real_python_gate_blockers_and_disables_confirm() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path().join("registry");
    let source = tmp.path().join("src_repo");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&source).unwrap();

    // Create minimal git repo + worktree
    let git = |dir: &Path, args: &[&str]| {
        let out = Command::new("git")
            .current_dir(dir)
            .args(args)
            .output()
            .expect("git");
        assert!(
            out.status.success(),
            "git {:?} failed: {}",
            args,
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    };
    git(&source, &["init"]);
    git(&source, &["config", "user.email", "test@example.com"]);
    git(&source, &["config", "user.name", "Test"]);
    fs::write(source.join("a.txt"), "base\n").unwrap();
    git(&source, &["add", "a.txt"]);
    git(&source, &["commit", "-m", "init"]);
    let base = git(&source, &["rev-parse", "HEAD"]);

    let id = "deadbeef1234";
    let job_dir = root.join(id);
    let wt = job_dir.join("worktree");
    fs::create_dir_all(&job_dir).unwrap();
    git(&source, &["worktree", "add", "-b", "claive/deadbeef1234", wt.to_str().unwrap(), &base]);
    fs::write(wt.join("a.txt"), "modified\n").unwrap();

    // Settled worker with a required check that has NOT passed yet -> gate_blockers must fire!
    write_json(
        &job_dir.join("state.json"),
        &serde_json::json!({
            "schema_version": 2,
            "id": id,
            "label": "Unverified edit",
            "engine": "pi",
            "workspace": source.to_string_lossy(),
            "actual_workspace": wt.to_string_lossy(),
            "retained_worktree": wt.to_string_lossy(),
            "status": "completed",
            "phase": "done",
            "turn": 1,
            "launch": {
                "isolation": {
                    "owner": "claive",
                    "source": source.to_string_lossy(),
                    "path": wt.to_string_lossy(),
                    "branch": "claive/deadbeef1234",
                    "base_commit": base,
                }
            },
            "acceptance_contract": {
                "checks": [{"name": "unit", "command": ["true"]}]
            }
        }),
    );

    let claive_bin = repo_claive_bin().to_string_lossy().into_owned();
    let dry = execute_apply_dry_run(id, &claive_bin, &root);
    assert!(dry.error.is_none(), "unexpected dry-run error: {:?}", dry.error);
    assert_eq!(dry.applied, vec!["a.txt".to_string()]);
    assert!(!dry.gate_blockers.is_empty(), "must report verification gate blocker");
    assert!(dry.is_blocked());

    // Drive modal through App: Confirm (`y`) MUST be refused, `[v]` MUST trigger Verify!
    let mut app = make_app(root.clone(), claive_bin, true);
    app.apply_snapshot(load_snapshot(&root, now_unix_secs()));
    assert_eq!(
        app.handle_key(key(KeyCode::Char('A'))),
        KeyOutcome::RequestApplyDryRun(id.to_string())
    );
    app.apply_read_response(ReadResponse::ApplyDryRun(dry));

    // Render modal and assert blockers + verify hint appear.
    let mut term = Terminal::new(TestBackend::new(120, 36)).unwrap();
    term.draw(|f| ui::render(f, &mut app)).unwrap();
    let txt = buffer_text(&term);
    assert!(txt.contains("Gate blockers (Confirm disabled)"));
    assert!(txt.contains("Run claive verify first"));

    // Pressing `y` while blocked must NOT submit Apply; source file stays untouched!
    assert_eq!(app.handle_key(key(KeyCode::Char('y'))), KeyOutcome::None);
    assert_eq!(fs::read_to_string(source.join("a.txt")).unwrap(), "base\n");

    // Pressing `v` inside the modal triggers `Verify` directly!
    assert_eq!(
        app.handle_key(key(KeyCode::Char('v'))),
        KeyOutcome::SubmitMutation(MutationKind::Verify {
            worker_id: id.to_string()
        })
    );
}
