//! Claive Ratatui triage dashboard entry point (`claive-tui`).

use std::env;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use ratatui::crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEventKind, KeyModifiers,
};
use ratatui::crossterm::execute;

use claive_tui::app::{App, AppConfig, KeyOutcome};
use claive_tui::commands::{resolve_claive_bin, MutationLane, MutationRequest, ReadLane};
use claive_tui::sanitize::osc52_sequence;
use claive_tui::store::{resolve_claive_dir, StoreWatcher};
use claive_tui::ui;

const POLL_INTERVAL: Duration = Duration::from_millis(50);

fn print_help() {
    println!(
        "claive-tui — Ratatui triage dashboard for Claive multi-harness workers\n\n\
         USAGE:\n\
           claive-tui [OPTIONS]\n\n\
         OPTIONS:\n\
           --compact             5-row passive tmux pane layout\n\
           --triage              Start in exception-only triage mode\n\
           --all                 Show workers across all parent sessions\n\
           --session <SESSION>   Scope to a specific parent session\n\
           --workspace <PATH>    Scope to a specific workspace path\n\
           --focus <ID|@ALIAS>   Focus a specific worker on open\n\
           --claive-dir <PATH>   Override CLAIVE_DIR\n\
           --claive-bin <PATH>   Override CLAIVE_BIN (for peek & operator actions)\n\
           --actions             Enable interactive controls (answer/send/steer/park/verify/apply/cancel/close)\n\
           --read-only           Enforce read-only mode (default; overrides --actions)\n\
           -h, --help            Print help\n\
           -V, --version         Print version"
    );
}

fn parse_args() -> Result<Option<AppConfig>, String> {
    let mut args = env::args().skip(1);
    let mut compact = false;
    let mut triage = false;
    let mut all_sessions = false;
    let mut actions = false;
    let mut read_only = false;
    let mut session = None;
    let mut workspace = None;
    let mut focus = None;
    let mut claive_dir_arg: Option<PathBuf> = None;
    let mut claive_bin_arg: Option<String> = None;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_help();
                return Ok(None);
            }
            "-V" | "--version" => {
                println!("claive-tui {}", env!("CARGO_PKG_VERSION"));
                return Ok(None);
            }
            "--compact" => compact = true,
            "--triage" => triage = true,
            "--all" => all_sessions = true,
            "--actions" => actions = true,
            "--read-only" => read_only = true,
            "--session" => {
                let val = args.next().ok_or("--session requires a value")?;
                session = Some(val);
            }
            "--workspace" => {
                let val = args.next().ok_or("--workspace requires a value")?;
                workspace = Some(val);
            }
            "--focus" => {
                let val = args.next().ok_or("--focus requires a value")?;
                focus = Some(val);
            }
            "--claive-dir" => {
                let val = args.next().ok_or("--claive-dir requires a value")?;
                claive_dir_arg = Some(PathBuf::from(val));
            }
            "--claive-bin" => {
                let val = args.next().ok_or("--claive-bin requires a value")?;
                claive_bin_arg = Some(val);
            }
            other => {
                return Err(format!("unknown argument: {other} (try --help)"));
            }
        }
    }

    let claive_dir = resolve_claive_dir(claive_dir_arg.as_deref());
    let claive_bin = resolve_claive_bin(claive_bin_arg.as_deref());

    Ok(Some(AppConfig {
        claive_dir,
        claive_bin,
        compact,
        triage,
        all_sessions,
        session,
        workspace,
        focus,
        actions,
        read_only,
    }))
}

fn main() -> ExitCode {
    let config = match parse_args() {
        Ok(Some(cfg)) => cfg,
        Ok(None) => return ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("claive-tui: {msg}");
            return ExitCode::from(2);
        }
    };

    let watcher = StoreWatcher::spawn(config.claive_dir.clone());
    let mut read_lane = ReadLane::spawn();
    let mutation_lane = if config.actions_enabled() {
        Some(MutationLane::spawn())
    } else {
        None
    };
    let mut app = App::new(config);
    if let Some(initial) = watcher.recv_initial(Duration::from_millis(250)) {
        app.apply_snapshot(initial);
    }
    let in_tmux = env::var_os("TMUX").is_some();
    let enable_mouse = !app.config.compact;

    struct MouseGuard(bool);
    impl Drop for MouseGuard {
        fn drop(&mut self) {
            if self.0 {
                let _ = execute!(io::stdout(), DisableMouseCapture);
            }
        }
    }

    let result = ratatui::run(|terminal| -> io::Result<()> {
        if enable_mouse {
            let _ = execute!(io::stdout(), EnableMouseCapture);
        }
        let _mouse_guard = MouseGuard(enable_mouse);
        let mut dirty = true;
        let mut last_tick = Instant::now();
        terminal.clear()?;

        loop {
            if let Some(snap) = watcher.try_recv_latest() {
                app.apply_snapshot(snap);
                dirty = true;
            }
            for resp in read_lane.drain_responses() {
                app.apply_read_response(resp);
                dirty = true;
            }
            if let Some(ref mlane) = mutation_lane {
                let mut need_refresh = false;
                for ev in mlane.drain_events() {
                    if app.apply_mutation_event(ev) {
                        need_refresh = true;
                    }
                    dirty = true;
                }
                if need_refresh {
                    watcher.request_refresh();
                }
            }
            if app.in_flight.is_some() {
                app.tick_spinner();
                dirty = true;
            }
            let had_toast = app.toast.is_some();
            app.expire_toast();
            if had_toast && app.toast.is_none() {
                dirty = true;
            }
            app.pump_reads(&mut read_lane, false);

            if last_tick.elapsed() >= Duration::from_secs(1) {
                last_tick = Instant::now();
                app.tick_clock();
                dirty = true;
            }

            if dirty {
                terminal.draw(|frame| ui::render(frame, &mut app))?;
                dirty = false;
            }

            if event::poll(POLL_INTERVAL)? {
                match event::read()? {
                    Event::Resize(_, _) => {
                        terminal.clear()?;
                        dirty = true;
                    }
                    Event::Mouse(mouse) if enable_mouse => {
                        if app.handle_mouse(mouse) {
                            dirty = true;
                        }
                    }
                    Event::Key(key)
                        if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) =>
                    {
                        if key.modifiers.contains(KeyModifiers::CONTROL)
                            && key.code == KeyCode::Char('l')
                        {
                            terminal.clear()?;
                            dirty = true;
                            continue;
                        }
                        dirty = true;
                        match app.handle_key(key) {
                            KeyOutcome::Quit => break,
                            KeyOutcome::RequestRefresh => {
                                terminal.clear()?;
                                watcher.request_refresh();
                                app.pump_reads(&mut read_lane, true);
                            }
                            KeyOutcome::CopyClipboard(cmd) => {
                                let seq = osc52_sequence(&cmd, in_tmux);
                                let mut stdout = io::stdout();
                                let _ = stdout.write_all(seq.as_bytes());
                                let _ = stdout.flush();
                            }
                            KeyOutcome::RequestApplyDryRun(wid) => {
                                let ok = read_lane.request_apply_dry_run(
                                    &wid,
                                    &app.config.claive_bin,
                                    &app.config.claive_dir,
                                );
                                if !ok {
                                    app.set_toast(
                                        "Read lane busy; try opening apply preview again",
                                        true,
                                    );
                                }
                            }
                            KeyOutcome::SubmitMutation(kind) => {
                                if let Some(ref mlane) = mutation_lane {
                                    let req = MutationRequest {
                                        kind: kind.clone(),
                                        claive_bin: app.config.claive_bin.clone(),
                                        claive_dir: app.config.claive_dir.clone(),
                                    };
                                    match mlane.submit(req) {
                                        Ok(()) => app.mark_mutation_started(&kind),
                                        Err(msg) => app.set_toast(msg, true),
                                    }
                                } else {
                                    app.set_toast("Read-only mode — launch with --actions", true);
                                }
                            }
                            KeyOutcome::None => {}
                        }
                    }
                    _ => {}
                }
            }
        }
        Ok(())
    });

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("claive-tui: terminal error: {err}");
            ExitCode::FAILURE
        }
    }
}
