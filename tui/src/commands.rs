//! Off-thread command lanes:
//! 1. `ReadLane` (read-only): `claive peek --json`, bounded log tails, `claive apply --dry-run --json`.
//! 2. `MutationLane` (single-flight write gate): `answer`, `send`, `steer`, `park`, `verify`, `cancel`, `close`, `apply`.
//!
//! Never blocks UI frames; caps output bytes and respects CLI ownership/cleanup contracts.

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use serde_json::Value;
use tempfile::NamedTempFile;

use crate::events::{supports_native_engine, EventsTailer};
use crate::sanitize::{multiline, sha256_hex, single_line};

pub const MAX_CMD_OUTPUT_BYTES: usize = 512 * 1024;
pub const MAX_LOG_TAIL_BYTES: u64 = 64 * 1024;
pub const CMD_TIMEOUT: Duration = Duration::from_secs(10);
pub const DRY_RUN_TIMEOUT: Duration = Duration::from_secs(20);
pub const MUTATION_SOFT_TIMEOUT: Duration = Duration::from_secs(30);
pub const LONG_MUTATION_SOFT_TIMEOUT: Duration = Duration::from_secs(120);
pub const PEEK_COOLDOWN: Duration = Duration::from_secs(2);
pub const NATIVE_PEEK_COOLDOWN: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LogKind {
    Result,
    Prompt,
    Stderr,
    Supervisor,
}

impl LogKind {
    pub fn filename(self) -> &'static str {
        match self {
            LogKind::Result => "result.txt",
            LogKind::Prompt => "prompt-0001.md",
            LogKind::Stderr => "stderr.log",
            LogKind::Supervisor => "supervisor.log",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            LogKind::Result => "Summary (result.txt)",
            LogKind::Prompt => "Prompt",
            LogKind::Stderr => "Stderr (stderr.log)",
            LogKind::Supervisor => "Supervisor (supervisor.log)",
        }
    }

    pub fn next(self) -> Self {
        match self {
            LogKind::Result => LogKind::Prompt,
            LogKind::Prompt => LogKind::Stderr,
            LogKind::Stderr => LogKind::Supervisor,
            LogKind::Supervisor => LogKind::Result,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PeekStep {
    pub index: u64,
    pub kind: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StructuredAction {
    pub id: u64,
    pub kind: String,
    pub category: String,
    pub stage: String,
    pub tool: Option<String>,
    pub target: String,
    pub summary: String,
    pub outcome: Option<String>,
    pub status: String,
    pub exit_code: Option<i64>,
    pub command: Option<String>,
    pub output: Option<String>,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PeekResult {
    pub worker_id: String,
    pub total: u64,
    pub intent: Option<String>,
    pub steps: Vec<PeekStep>,
    pub actions: Vec<StructuredAction>,
    pub error: Option<String>,
    pub events_sig: Option<(u64, i64)>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LogResult {
    pub worker_id: String,
    pub kind: LogKind,
    pub content: String,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ApplyDryRunResult {
    pub worker_id: String,
    pub source: String,
    pub worktree: String,
    pub applied: Vec<String>,
    pub deleted: Vec<String>,
    pub conflicts: Vec<String>,
    pub gate_blockers: Vec<String>,
    pub error: Option<String>,
}

impl ApplyDryRunResult {
    pub fn is_blocked(&self) -> bool {
        self.error.is_some() || !self.conflicts.is_empty() || !self.gate_blockers.is_empty()
    }
}

#[derive(Debug, Clone)]
pub enum ReadRequest {
    Peek {
        worker_id: String,
        job_dir: PathBuf,
        engine: String,
        workspace: Option<String>,
        steps: usize,
        claive_bin: String,
        claive_dir: PathBuf,
        events_sig: Option<(u64, i64)>,
        force: bool,
    },
    ReadLog {
        worker_id: String,
        job_dir: PathBuf,
        kind: LogKind,
        prompt_file: Option<String>,
    },
    ApplyDryRun {
        worker_id: String,
        claive_bin: String,
        claive_dir: PathBuf,
    },
}

#[derive(Debug, Clone)]
pub enum ReadResponse {
    Peek(PeekResult),
    Log(LogResult),
    ApplyDryRun(ApplyDryRunResult),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SendMode {
    Wake,
    Now,
    Note,
}

impl SendMode {
    pub fn as_cli_str(self) -> &'static str {
        match self {
            SendMode::Wake => "wake",
            SendMode::Now => "now",
            SendMode::Note => "note",
        }
    }

    pub fn next(self) -> Self {
        match self {
            SendMode::Wake => SendMode::Now,
            SendMode::Now => SendMode::Note,
            SendMode::Note => SendMode::Wake,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MutationKind {
    Answer {
        worker_id: String,
        expected_turn: u64,
        expected_question_hash: Option<String>,
        idempotency_key: String,
        message: String,
        is_goal: bool,
    },
    Send {
        worker_id: String,
        mode: SendMode,
        message: String,
        message_id: String,
    },
    Steer {
        worker_id: String,
        message: String,
    },
    Park {
        worker_id: String,
        unpark: bool,
    },
    Verify {
        worker_id: String,
    },
    Cancel {
        worker_id: String,
    },
    Close {
        worker_id: String,
    },
    Apply {
        worker_id: String,
        close_after: bool,
    },
}

impl MutationKind {
    pub fn summary_label(&self) -> String {
        match self {
            MutationKind::Answer { worker_id, is_goal, .. } => {
                if *is_goal {
                    format!("claive goal answer {worker_id}")
                } else {
                    format!("claive answer {worker_id}")
                }
            }
            MutationKind::Send { worker_id, mode, .. } => {
                format!("claive send {worker_id} --mode {}", mode.as_cli_str())
            }
            MutationKind::Steer { worker_id, .. } => format!("claive steer {worker_id}"),
            MutationKind::Park { worker_id, unpark } => {
                if *unpark {
                    format!("claive park {worker_id} --unpark")
                } else {
                    format!("claive park {worker_id}")
                }
            }
            MutationKind::Verify { worker_id } => format!("claive verify {worker_id}"),
            MutationKind::Cancel { worker_id } => format!("claive cancel {worker_id}"),
            MutationKind::Close { worker_id } => format!("claive close {worker_id}"),
            MutationKind::Apply { worker_id, close_after } => {
                if *close_after {
                    format!("claive apply {worker_id} --close")
                } else {
                    format!("claive apply {worker_id}")
                }
            }
        }
    }

    pub fn worker_id(&self) -> &str {
        match self {
            MutationKind::Answer { worker_id, .. }
            | MutationKind::Send { worker_id, .. }
            | MutationKind::Steer { worker_id, .. }
            | MutationKind::Park { worker_id, .. }
            | MutationKind::Verify { worker_id }
            | MutationKind::Cancel { worker_id }
            | MutationKind::Close { worker_id }
            | MutationKind::Apply { worker_id, .. } => worker_id,
        }
    }

    /// True for operations (`verify`, `apply`) that must never be SIGKILLed mid-run on soft timeout.
    fn preserve_on_soft_timeout(&self) -> bool {
        matches!(self, MutationKind::Verify { .. } | MutationKind::Apply { .. })
    }
}

#[derive(Debug, Clone)]
pub struct MutationRequest {
    pub kind: MutationKind,
    pub claive_bin: String,
    pub claive_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MutationEvent {
    SlowWarning {
        label: String,
        elapsed_secs: u64,
    },
    Finished {
        worker_id: String,
        label: String,
        ok: bool,
        message: String,
    },
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawPeekOutput {
    total: Option<u64>,
    intent: Option<String>,
    steps: Option<Vec<RawPeekStep>>,
    actions: Option<Vec<RawStructuredAction>>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawStructuredAction {
    id: Option<u64>,
    kind: Option<String>,
    category: Option<String>,
    stage: Option<String>,
    tool: Option<String>,
    target: Option<String>,
    summary: Option<String>,
    outcome: Option<String>,
    status: Option<String>,
    exit_code: Option<i64>,
    command: Option<String>,
    output: Option<String>,
    truncated: Option<bool>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawPeekStep {
    index: Option<u64>,
    kind: Option<String>,
    text: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawApplyDryRun {
    source: Option<String>,
    worktree: Option<String>,
    applied: Option<Vec<String>>,
    deleted: Option<Vec<String>>,
    conflicts: Option<Vec<String>>,
    gate_blockers: Option<Vec<String>>,
}

/// Generate a 32-char lowercase hex ID for idempotent CLI requests (`send --id`, `answer --idempotency-key`).
pub fn make_request_id(salt: &str) -> String {
    let ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let pid = std::process::id();
    let digest = sha256_hex(format!("{ns}:{pid}:{salt}").as_bytes());
    digest.chars().take(32).collect()
}

/// Resolve the Claive CLI binary path (`CLAIVE_BIN` or `claive` on PATH).
pub fn resolve_claive_bin(explicit: Option<&str>) -> String {
    if let Some(p) = explicit {
        let trimmed = p.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }
    if let Ok(val) = std::env::var("CLAIVE_BIN") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }
    "claive".to_string()
}

/// Run a child command with bounded stdout/stderr draining and TERM -> KILL timeout reaping.
pub fn run_bounded_command(
    bin: &str,
    args: &[&str],
    claive_dir: &Path,
    timeout: Duration,
) -> Result<(i32, Vec<u8>, Vec<u8>), String> {
    let mut cmd = Command::new(bin);
    cmd.args(args)
        .env("CLAIVE_DIR", claive_dir)
        .env_remove("CLAIVE_WORKER_ID")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn {bin}: {e}"))?;

    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();

    let out_handle = thread::spawn(move || drain_capped(stdout_pipe, MAX_CMD_OUTPUT_BYTES));
    let err_handle = thread::spawn(move || drain_capped(stderr_pipe, MAX_CMD_OUTPUT_BYTES));

    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Ok(st),
            Ok(None) => {
                if started.elapsed() >= timeout {
                    // Graceful TERM, short grace, then KILL, always reaped.
                    let pid = child.id() as libc::pid_t;
                    unsafe {
                        libc::kill(pid, libc::SIGTERM);
                    }
                    let grace_start = Instant::now();
                    let mut exited = None;
                    while grace_start.elapsed() < Duration::from_secs(1) {
                        if let Ok(Some(st)) = child.try_wait() {
                            exited = Some(st);
                            break;
                        }
                        thread::sleep(Duration::from_millis(25));
                    }
                    if exited.is_none() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    break Err(format!("command timed out after {}s", timeout.as_secs()));
                }
                thread::sleep(Duration::from_millis(20));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                break Err(format!("wait failed: {e}"));
            }
        }
    };

    let stdout = out_handle.join().unwrap_or_default();
    let stderr = err_handle.join().unwrap_or_default();
    let st = status?;
    Ok((st.code().unwrap_or(-1), stdout, stderr))
}

/// Run a mutation command with a soft timeout that emits a warning and retains the busy gate
/// instead of blindly killing partial file-copy (`apply`) or external-session (`verify`) runs.
fn run_mutation_command(
    bin: &str,
    args: &[&str],
    claive_dir: &Path,
    soft_timeout: Duration,
    preserve_on_timeout: bool,
    label: &str,
    event_tx: &mpsc::SyncSender<MutationEvent>,
) -> Result<(i32, Vec<u8>, Vec<u8>), String> {
    let mut cmd = Command::new(bin);
    cmd.args(args)
        .env("CLAIVE_DIR", claive_dir)
        .env_remove("CLAIVE_WORKER_ID")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn {bin}: {e}"))?;

    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();

    let out_handle = thread::spawn(move || drain_capped(stdout_pipe, MAX_CMD_OUTPUT_BYTES));
    let err_handle = thread::spawn(move || drain_capped(stderr_pipe, MAX_CMD_OUTPUT_BYTES));

    let started = Instant::now();
    let mut warned = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Ok(st),
            Ok(None) => {
                let elapsed = started.elapsed();
                if !warned && elapsed >= soft_timeout {
                    warned = true;
                    let _ = event_tx.try_send(MutationEvent::SlowWarning {
                        label: label.to_string(),
                        elapsed_secs: elapsed.as_secs(),
                    });
                    if !preserve_on_timeout {
                        let pid = child.id() as libc::pid_t;
                        unsafe {
                            libc::kill(pid, libc::SIGTERM);
                        }
                        let grace = Instant::now();
                        let mut exited = None;
                        while grace.elapsed() < Duration::from_secs(2) {
                            if let Ok(Some(st)) = child.try_wait() {
                                exited = Some(st);
                                break;
                            }
                            thread::sleep(Duration::from_millis(25));
                        }
                        if exited.is_none() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                        break Err(format!(
                            "command timed out after {}s; reconcile state before retrying",
                            soft_timeout.as_secs()
                        ));
                    }
                }
                thread::sleep(Duration::from_millis(25));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                break Err(format!("wait failed: {e}"));
            }
        }
    };

    let stdout = out_handle.join().unwrap_or_default();
    let stderr = err_handle.join().unwrap_or_default();
    let st = status?;
    Ok((st.code().unwrap_or(-1), stdout, stderr))
}

fn drain_capped<R: Read>(stream: Option<R>, cap: usize) -> Vec<u8> {
    let Some(mut reader) = stream else {
        return Vec::new();
    };
    let mut out = Vec::with_capacity(4096);
    let mut chunk = [0u8; 4096];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                if out.len() < cap {
                    let take = n.min(cap - out.len());
                    out.extend_from_slice(&chunk[..take]);
                }
            }
            Err(_) => break,
        }
    }
    out
}

/// Read the bounded tail of a worker log file strictly read-only.
pub fn read_log_tail(job_dir: &Path, kind: LogKind) -> (String, bool) {
    read_log_tail_with_prompt(job_dir, kind, None)
}

fn resolve_prompt_path(job_dir: &Path, prompt_file: Option<&str>) -> PathBuf {
    if let Some(pf) = prompt_file {
        let trimmed = pf.trim();
        if !trimmed.is_empty() {
            let p = PathBuf::from(trimmed);
            let candidate = if p.is_absolute() { p } else { job_dir.join(p) };
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    let default_p = job_dir.join("prompt-0001.md");
    if default_p.is_file() {
        return default_p;
    }
    // Fall back to the first matching prompt-*.md file if present.
    if let Ok(rd) = std::fs::read_dir(job_dir) {
        let mut prompts: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.file_name()
                        .and_then(|n| n.to_str())
                        .map(|n| n.starts_with("prompt-") && n.ends_with(".md"))
                        .unwrap_or(false)
            })
            .collect();
        prompts.sort();
        if let Some(first) = prompts.into_iter().next() {
            return first;
        }
    }
    default_p
}

pub fn read_log_tail_with_prompt(
    job_dir: &Path,
    kind: LogKind,
    prompt_file: Option<&str>,
) -> (String, bool) {
    let path = if kind == LogKind::Prompt {
        resolve_prompt_path(job_dir, prompt_file)
    } else {
        job_dir.join(kind.filename())
    };
    let display_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(kind.filename());
    let Ok(mut file) = File::open(&path) else {
        return (format!("({display_name} not present yet)"), false);
    };
    let Ok(meta) = file.metadata() else {
        return (format!("(cannot stat {display_name})"), false);
    };
    let len = meta.len();
    if len == 0 {
        return (format!("({display_name} is empty)"), false);
    }
    let truncated = len > MAX_LOG_TAIL_BYTES;
    if truncated && file.seek(SeekFrom::Start(len - MAX_LOG_TAIL_BYTES)).is_err() {
        return (format!("(cannot seek {display_name})"), false);
    }
    let mut buf = Vec::with_capacity(len.min(MAX_LOG_TAIL_BYTES) as usize);
    if file.take(MAX_LOG_TAIL_BYTES).read_to_end(&mut buf).is_err() {
        return (format!("(cannot read {display_name})"), false);
    }
    let raw = String::from_utf8_lossy(&buf);
    let trimmed = if truncated {
        raw.split_once('\n').map(|(_, rest)| rest).unwrap_or(&raw)
    } else {
        &raw
    };
    (multiline(trimmed), truncated)
}

/// Off-thread read lane with per-worker peek cooldown + events_sig deduplication.
pub struct ReadLane {
    tx: mpsc::SyncSender<ReadRequest>,
    rx: mpsc::Receiver<ReadResponse>,
    last_peek_at: HashMap<String, Instant>,
    last_peek_sig: HashMap<String, Option<(u64, i64)>>,
}

impl ReadLane {
    pub fn spawn() -> Self {
        let (req_tx, req_rx) = mpsc::sync_channel::<ReadRequest>(12);
        let (resp_tx, resp_rx) = mpsc::sync_channel::<ReadResponse>(16);

        thread::Builder::new()
            .name("claive-read-lane".to_string())
            .spawn(move || {
                let mut tailers: HashMap<String, EventsTailer> = HashMap::new();
                while let Ok(req) = req_rx.recv() {
                    match req {
                        ReadRequest::Peek {
                            worker_id,
                            job_dir,
                            engine,
                            workspace,
                            steps,
                            claive_bin,
                            claive_dir,
                            events_sig,
                            ..
                        } => {
                            let res = if supports_native_engine(&engine) {
                                let needs_reset = tailers
                                    .get(&worker_id)
                                    .map(|t| !t.matches_target(&job_dir, &engine))
                                    .unwrap_or(true);
                                if needs_reset {
                                    tailers.insert(
                                        worker_id.clone(),
                                        EventsTailer::new(&job_dir, &engine, workspace),
                                    );
                                }
                                let tailer = tailers.get_mut(&worker_id).expect("tailer inserted");
                                match tailer.refresh() {
                                    Ok(()) => tailer.snapshot(&worker_id, steps, events_sig),
                                    Err(err) => PeekResult {
                                        worker_id: worker_id.clone(),
                                        total: 0,
                                        intent: None,
                                        steps: Vec::new(),
                                        actions: Vec::new(),
                                        error: Some(err),
                                        events_sig,
                                    },
                                }
                            } else {
                                execute_peek(
                                    &worker_id,
                                    steps,
                                    &claive_bin,
                                    &claive_dir,
                                    events_sig,
                                )
                            };
                            let _ = resp_tx.try_send(ReadResponse::Peek(res));
                        }
                        ReadRequest::ReadLog {
                            worker_id,
                            job_dir,
                            kind,
                            prompt_file,
                        } => {
                            let (content, truncated) =
                                read_log_tail_with_prompt(&job_dir, kind, prompt_file.as_deref());
                            let _ = resp_tx.try_send(ReadResponse::Log(LogResult {
                                worker_id,
                                kind,
                                content,
                                truncated,
                            }));
                        }
                        ReadRequest::ApplyDryRun {
                            worker_id,
                            claive_bin,
                            claive_dir,
                        } => {
                            let res = execute_apply_dry_run(&worker_id, &claive_bin, &claive_dir);
                            let _ = resp_tx.try_send(ReadResponse::ApplyDryRun(res));
                        }
                    }
                }
            })
            .expect("spawn claive-read-lane thread");

        Self {
            tx: req_tx,
            rx: resp_rx,
            last_peek_at: HashMap::new(),
            last_peek_sig: HashMap::new(),
        }
    }

    /// Request worker activity gated by cooldown and `events_sig` change (unless `force`).
    /// Supported engines (`codex`, `pi`, `claude`, `agy`) are tailed natively in Rust;
    /// other engines fall back to `claive peek ID --json --structured`.
    #[allow(clippy::too_many_arguments)]
    pub fn maybe_request_peek(
        &mut self,
        worker_id: &str,
        job_dir: &Path,
        engine: &str,
        workspace: Option<&str>,
        steps: usize,
        claive_bin: &str,
        claive_dir: &Path,
        events_sig: Option<(u64, i64)>,
        force: bool,
    ) -> bool {
        let cooldown = if supports_native_engine(engine) {
            NATIVE_PEEK_COOLDOWN
        } else {
            PEEK_COOLDOWN
        };
        if !force {
            let sig_same = self
                .last_peek_sig
                .get(worker_id)
                .map(|prev| *prev == events_sig)
                .unwrap_or(false);
            if sig_same {
                return false;
            }
            let in_cooldown = self
                .last_peek_at
                .get(worker_id)
                .map(|t| t.elapsed() < cooldown)
                .unwrap_or(false);
            if in_cooldown {
                return false;
            }
        }

        let req = ReadRequest::Peek {
            worker_id: worker_id.to_string(),
            job_dir: job_dir.to_path_buf(),
            engine: engine.to_string(),
            workspace: workspace.map(str::to_string),
            steps,
            claive_bin: claive_bin.to_string(),
            claive_dir: claive_dir.to_path_buf(),
            events_sig,
            force,
        };
        if self.tx.try_send(req).is_ok() {
            self.last_peek_at.insert(worker_id.to_string(), Instant::now());
            self.last_peek_sig.insert(worker_id.to_string(), events_sig);
            true
        } else {
            false
        }
    }

    pub fn request_log(
        &self,
        worker_id: &str,
        job_dir: &Path,
        kind: LogKind,
        prompt_file: Option<String>,
    ) {
        let _ = self.tx.try_send(ReadRequest::ReadLog {
            worker_id: worker_id.to_string(),
            job_dir: job_dir.to_path_buf(),
            kind,
            prompt_file,
        });
    }

    pub fn request_apply_dry_run(&self, worker_id: &str, claive_bin: &str, claive_dir: &Path) -> bool {
        self.tx
            .try_send(ReadRequest::ApplyDryRun {
                worker_id: worker_id.to_string(),
                claive_bin: claive_bin.to_string(),
                claive_dir: claive_dir.to_path_buf(),
            })
            .is_ok()
    }

    pub fn drain_responses(&self) -> Vec<ReadResponse> {
        let mut out = Vec::new();
        while let Ok(r) = self.rx.try_recv() {
            out.push(r);
        }
        out
    }
}

/// Strictly single-flight mutation lane: at most ONE write CLI command runs at a time.
pub struct MutationLane {
    tx: mpsc::SyncSender<MutationRequest>,
    rx: mpsc::Receiver<MutationEvent>,
    in_flight: Arc<AtomicBool>,
}

impl MutationLane {
    pub fn spawn() -> Self {
        let (req_tx, req_rx) = mpsc::sync_channel::<MutationRequest>(1);
        let (ev_tx, ev_rx) = mpsc::sync_channel::<MutationEvent>(8);
        let in_flight = Arc::new(AtomicBool::new(false));
        let busy_flag = Arc::clone(&in_flight);

        thread::Builder::new()
            .name("claive-mutation-lane".to_string())
            .spawn(move || {
                while let Ok(req) = req_rx.recv() {
                    let outcome = execute_mutation(&req, &ev_tx);
                    busy_flag.store(false, Ordering::SeqCst);
                    let _ = ev_tx.try_send(outcome);
                }
            })
            .expect("spawn claive-mutation-lane thread");

        Self {
            tx: req_tx,
            rx: ev_rx,
            in_flight,
        }
    }

    pub fn is_busy(&self) -> bool {
        self.in_flight.load(Ordering::SeqCst)
    }

    /// Submit a mutation if the lane is idle. Returns `Err` if another mutation is still in flight.
    pub fn submit(&self, req: MutationRequest) -> Result<(), String> {
        if self
            .in_flight
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("another action is still running; wait for it to finish".to_string());
        }
        if self.tx.try_send(req).is_err() {
            self.in_flight.store(false, Ordering::SeqCst);
            return Err("mutation queue is full".to_string());
        }
        Ok(())
    }

    pub fn drain_events(&self) -> Vec<MutationEvent> {
        let mut out = Vec::new();
        while let Ok(ev) = self.rx.try_recv() {
            out.push(ev);
        }
        out
    }
}

fn write_temp_message(text: &str) -> Result<NamedTempFile, String> {
    let mut f = NamedTempFile::new().map_err(|e| format!("create temp message file failed: {e}"))?;
    f.write_all(text.as_bytes())
        .map_err(|e| format!("write temp message file failed: {e}"))?;
    f.flush()
        .map_err(|e| format!("flush temp message file failed: {e}"))?;
    Ok(f)
}

fn first_line_or_code(stdout: &[u8], stderr: &[u8], code: i32) -> String {
    let err = single_line(&String::from_utf8_lossy(stderr));
    if !err.is_empty() {
        return err;
    }
    let out = single_line(&String::from_utf8_lossy(stdout));
    if !out.is_empty() {
        return out;
    }
    format!("exited with code {code}")
}

/// Execute one mutation request synchronously (used inside `MutationLane` and directly in tests).
pub fn execute_mutation(
    req: &MutationRequest,
    event_tx: &mpsc::SyncSender<MutationEvent>,
) -> MutationEvent {
    let label = req.kind.summary_label();
    let worker_id = req.kind.worker_id().to_string();
    let preserve = req.kind.preserve_on_soft_timeout();
    let soft_timeout = if preserve {
        LONG_MUTATION_SOFT_TIMEOUT
    } else {
        MUTATION_SOFT_TIMEOUT
    };

    let mut _temp_file: Option<NamedTempFile> = None;
    let mut owned_args: Vec<String> = Vec::new();

    match &req.kind {
        MutationKind::Answer {
            worker_id,
            expected_turn,
            expected_question_hash,
            idempotency_key,
            message,
            is_goal,
        } => {
            if *is_goal {
                owned_args.extend([
                    "goal".to_string(),
                    "answer".to_string(),
                    worker_id.clone(),
                    "--message".to_string(),
                    message.clone(),
                ]);
            } else {
                match write_temp_message(message) {
                    Ok(f) => {
                        let path_str = f.path().to_string_lossy().into_owned();
                        _temp_file = Some(f);
                        owned_args.extend([
                            "answer".to_string(),
                            worker_id.clone(),
                            "--message-file".to_string(),
                            path_str,
                            "--expected-turn".to_string(),
                            expected_turn.to_string(),
                            "--idempotency-key".to_string(),
                            idempotency_key.clone(),
                        ]);
                        if let Some(hash) = expected_question_hash {
                            let prefix: String = hash.chars().take(16).collect();
                            if prefix.len() >= 8 {
                                owned_args.push("--expected-question-hash".to_string());
                                owned_args.push(prefix);
                            }
                        }
                    }
                    Err(e) => {
                        return MutationEvent::Finished {
                            worker_id: worker_id.clone(),
                            label,
                            ok: false,
                            message: e,
                        };
                    }
                }
            }
        }
        MutationKind::Send {
            worker_id,
            mode,
            message,
            message_id,
        } => match write_temp_message(message) {
            Ok(f) => {
                let path_str = f.path().to_string_lossy().into_owned();
                _temp_file = Some(f);
                owned_args.extend([
                    "send".to_string(),
                    worker_id.clone(),
                    "--message-file".to_string(),
                    path_str,
                    "--mode".to_string(),
                    mode.as_cli_str().to_string(),
                    "--id".to_string(),
                    message_id.clone(),
                    "--json".to_string(),
                ]);
            }
            Err(e) => {
                return MutationEvent::Finished {
                    worker_id: worker_id.clone(),
                    label,
                    ok: false,
                    message: e,
                };
            }
        },
        MutationKind::Steer { worker_id, message } => match write_temp_message(message) {
            Ok(f) => {
                let path_str = f.path().to_string_lossy().into_owned();
                _temp_file = Some(f);
                owned_args.extend([
                    "steer".to_string(),
                    worker_id.clone(),
                    "--message-file".to_string(),
                    path_str,
                    "--wait".to_string(),
                    "10".to_string(),
                    "--json".to_string(),
                ]);
            }
            Err(e) => {
                return MutationEvent::Finished {
                    worker_id: worker_id.clone(),
                    label,
                    ok: false,
                    message: e,
                };
            }
        },
        MutationKind::Park { worker_id, unpark } => {
            owned_args.extend(["park".to_string(), worker_id.clone(), "--json".to_string()]);
            if *unpark {
                owned_args.push("--unpark".to_string());
            }
        }
        MutationKind::Verify { worker_id } => {
            owned_args.extend(["verify".to_string(), worker_id.clone()]);
        }
        MutationKind::Cancel { worker_id } => {
            owned_args.extend(["cancel".to_string(), worker_id.clone()]);
        }
        MutationKind::Close { worker_id } => {
            owned_args.extend(["close".to_string(), worker_id.clone()]);
        }
        MutationKind::Apply {
            worker_id,
            close_after,
        } => {
            owned_args.extend(["apply".to_string(), worker_id.clone(), "--json".to_string()]);
            if *close_after {
                owned_args.push("--close".to_string());
            }
        }
    }

    let arg_refs: Vec<&str> = owned_args.iter().map(|s| s.as_str()).collect();
    match run_mutation_command(
        &req.claive_bin,
        &arg_refs,
        &req.claive_dir,
        soft_timeout,
        preserve,
        &label,
        event_tx,
    ) {
        Ok((code, stdout, stderr)) => {
            let (ok, message) = format_mutation_receipt(&req.kind, code, &stdout, &stderr);
            MutationEvent::Finished {
                worker_id,
                label,
                ok,
                message,
            }
        }
        Err(msg) => MutationEvent::Finished {
            worker_id,
            label,
            ok: false,
            message: single_line(&msg),
        },
    }
}

fn format_mutation_receipt(
    kind: &MutationKind,
    code: i32,
    stdout: &[u8],
    stderr: &[u8],
) -> (bool, String) {
    match kind {
        MutationKind::Send { mode, .. } if code == 0 => {
            if let Ok(val) = serde_json::from_slice::<Value>(stdout) {
                let result = val
                    .get("result")
                    .and_then(|v| v.as_str())
                    .map(single_line)
                    .unwrap_or_else(|| "sent".to_string());
                let dup = val.get("duplicate").and_then(|v| v.as_bool()).unwrap_or(false);
                let fallback = val
                    .get("fallback_from")
                    .and_then(|v| v.as_str())
                    .map(single_line);
                let mut msg = format!("Send ({}): {result}", mode.as_cli_str());
                if let Some(fb) = fallback {
                    msg.push_str(&format!(" [fallback from {fb}]"));
                } else if *mode == SendMode::Now && result.to_lowercase().contains("wake") {
                    msg.push_str(" [now -> wake fallback]");
                }
                if dup {
                    msg.push_str(" (duplicate)");
                }
                return (true, msg);
            }
            (true, first_line_or_code(stdout, stderr, code))
        }
        MutationKind::Steer { .. } => {
            if let Ok(val) = serde_json::from_slice::<Value>(stdout) {
                let status = val
                    .get("status")
                    .and_then(|v| v.as_str())
                    .map(single_line)
                    .unwrap_or_else(|| if code == 0 { "queued".into() } else { "failed".into() });
                let disp = val
                    .get("disposition")
                    .or_else(|| val.get("detail"))
                    .or_else(|| val.get("error"))
                    .and_then(|v| v.as_str())
                    .map(single_line)
                    .filter(|s| !s.is_empty());
                let msg = match disp {
                    Some(d) => format!("Steer {status}: {d}"),
                    None => format!("Steer {status}"),
                };
                return (code == 0, msg);
            }
            (code == 0, first_line_or_code(stdout, stderr, code))
        }
        MutationKind::Park { unpark, worker_id } if code == 0 => {
            let verb = if *unpark { "Unparked" } else { "Parked" };
            (true, format!("{verb} {worker_id}"))
        }
        MutationKind::Verify { worker_id } => {
            if let Ok(val) = serde_json::from_slice::<Value>(stdout) {
                let status = val
                    .get("status")
                    .and_then(|v| v.as_str())
                    .map(single_line)
                    .unwrap_or_else(|| if code == 0 { "passed".into() } else { "failed".into() });
                return (code == 0, format!("Verify {worker_id}: {status}"));
            }
            (code == 0, first_line_or_code(stdout, stderr, code))
        }
        MutationKind::Apply { worker_id, .. } => {
            if let Ok(val) = serde_json::from_slice::<Value>(stdout) {
                let status = val
                    .get("status")
                    .and_then(|v| v.as_str())
                    .map(single_line)
                    .unwrap_or_else(|| "done".to_string());
                let applied = val
                    .get("applied")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                let deleted = val
                    .get("deleted")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                let closed = val.get("closed").and_then(|v| v.as_bool()).unwrap_or(false);
                if code == 0 {
                    let close_note = if closed { " · closed" } else { "" };
                    return (
                        true,
                        format!(
                            "Apply {worker_id} ({status}): {applied} applied, {deleted} deleted{close_note}"
                        ),
                    );
                }
            }
            (code == 0, first_line_or_code(stdout, stderr, code))
        }
        _ => (code == 0, first_line_or_code(stdout, stderr, code)),
    }
}

pub fn execute_apply_dry_run(
    worker_id: &str,
    claive_bin: &str,
    claive_dir: &Path,
) -> ApplyDryRunResult {
    let args = ["apply", worker_id, "--dry-run", "--json"];
    match run_bounded_command(claive_bin, &args, claive_dir, DRY_RUN_TIMEOUT) {
        Ok((code, stdout, stderr)) => {
            if let Ok(parsed) = serde_json::from_slice::<RawApplyDryRun>(&stdout) {
                let applied = parsed
                    .applied
                    .unwrap_or_default()
                    .into_iter()
                    .map(|s| single_line(&s))
                    .collect();
                let deleted = parsed
                    .deleted
                    .unwrap_or_default()
                    .into_iter()
                    .map(|s| single_line(&s))
                    .collect();
                let conflicts = parsed
                    .conflicts
                    .unwrap_or_default()
                    .into_iter()
                    .map(|s| single_line(&s))
                    .collect();
                let gate_blockers = parsed
                    .gate_blockers
                    .unwrap_or_default()
                    .into_iter()
                    .map(|s| single_line(&s))
                    .collect();
                let err = if code != 0 && stderr.iter().any(|b| !b.is_ascii_whitespace()) {
                    Some(single_line(&String::from_utf8_lossy(&stderr)))
                } else {
                    None
                };
                return ApplyDryRunResult {
                    worker_id: worker_id.to_string(),
                    source: parsed.source.as_deref().map(single_line).unwrap_or_default(),
                    worktree: parsed.worktree.as_deref().map(single_line).unwrap_or_default(),
                    applied,
                    deleted,
                    conflicts,
                    gate_blockers,
                    error: err,
                };
            }
            ApplyDryRunResult {
                worker_id: worker_id.to_string(),
                source: String::new(),
                worktree: String::new(),
                applied: Vec::new(),
                deleted: Vec::new(),
                conflicts: Vec::new(),
                gate_blockers: Vec::new(),
                error: Some(first_line_or_code(&stdout, &stderr, code)),
            }
        }
        Err(msg) => ApplyDryRunResult {
            worker_id: worker_id.to_string(),
            source: String::new(),
            worktree: String::new(),
            applied: Vec::new(),
            deleted: Vec::new(),
            conflicts: Vec::new(),
            gate_blockers: Vec::new(),
            error: Some(single_line(&msg)),
        },
    }
}

pub fn execute_peek(
    worker_id: &str,
    steps: usize,
    claive_bin: &str,
    claive_dir: &Path,
    events_sig: Option<(u64, i64)>,
) -> PeekResult {
    let steps_str = steps.clamp(1, 50).to_string();
    let structured_args = ["peek", worker_id, "--steps", &steps_str, "--json", "--structured"];
    let mut run_res = run_bounded_command(claive_bin, &structured_args, claive_dir, CMD_TIMEOUT);
    // Graceful fallback if an older/fake CLI doesn't recognize `--structured`.
    if let Ok((code, _, ref stderr)) = run_res
        && code != 0
        && String::from_utf8_lossy(stderr).contains("--structured")
    {
        let legacy_args = ["peek", worker_id, "--steps", &steps_str, "--json"];
        run_res = run_bounded_command(claive_bin, &legacy_args, claive_dir, CMD_TIMEOUT);
    }
    match run_res {
        Ok((0, stdout, _)) => match serde_json::from_slice::<RawPeekOutput>(&stdout) {
            Ok(parsed) => {
                let steps = parsed
                    .steps
                    .unwrap_or_default()
                    .into_iter()
                    .map(|s| PeekStep {
                        index: s.index.unwrap_or(0),
                        kind: s
                            .kind
                            .as_deref()
                            .map(single_line)
                            .unwrap_or_else(|| "step".to_string()),
                        // peek --json intentionally preserves raw controls; sanitize every step
                        // while keeping newlines intact for progressive-disclosure expansion.
                        text: s.text.as_deref().map(multiline).unwrap_or_default(),
                    })
                    .collect();
                let actions = parsed
                    .actions
                    .unwrap_or_default()
                    .into_iter()
                    .map(|a| StructuredAction {
                        id: a.id.unwrap_or(0),
                        kind: a
                            .kind
                            .as_deref()
                            .map(single_line)
                            .unwrap_or_else(|| "action".to_string()),
                        category: a
                            .category
                            .as_deref()
                            .map(single_line)
                            .unwrap_or_else(|| "tool".to_string()),
                        stage: a
                            .stage
                            .as_deref()
                            .map(single_line)
                            .unwrap_or_else(|| "running".to_string()),
                        tool: a
                            .tool
                            .as_deref()
                            .map(single_line)
                            .filter(|s| !s.is_empty()),
                        target: a.target.as_deref().map(single_line).unwrap_or_default(),
                        summary: a.summary.as_deref().map(single_line).unwrap_or_default(),
                        outcome: a
                            .outcome
                            .as_deref()
                            .map(single_line)
                            .filter(|s| !s.is_empty()),
                        status: a
                            .status
                            .as_deref()
                            .map(single_line)
                            .unwrap_or_else(|| "ok".to_string()),
                        exit_code: a.exit_code,
                        command: a
                            .command
                            .as_deref()
                            .map(multiline)
                            .filter(|s| !s.trim().is_empty()),
                        output: a
                            .output
                            .as_deref()
                            .map(multiline)
                            .filter(|s| !s.trim().is_empty()),
                        truncated: a.truncated.unwrap_or(false),
                    })
                    .collect();
                PeekResult {
                    worker_id: worker_id.to_string(),
                    total: parsed.total.unwrap_or(0),
                    intent: parsed
                        .intent
                        .as_deref()
                        .map(single_line)
                        .filter(|s| !s.is_empty()),
                    steps,
                    actions,
                    error: None,
                    events_sig,
                }
            }
            Err(e) => PeekResult {
                worker_id: worker_id.to_string(),
                total: 0,
                intent: None,
                steps: Vec::new(),
                actions: Vec::new(),
                error: Some(format!("invalid peek JSON: {e}")),
                events_sig,
            },
        },
        Ok((code, _, stderr)) => {
            let err_text = single_line(&String::from_utf8_lossy(&stderr));
            let msg = if err_text.is_empty() {
                format!("peek exited with code {code}")
            } else {
                err_text
            };
            PeekResult {
                worker_id: worker_id.to_string(),
                total: 0,
                intent: None,
                steps: Vec::new(),
                actions: Vec::new(),
                error: Some(msg),
                events_sig,
            }
        }
        Err(msg) => PeekResult {
            worker_id: worker_id.to_string(),
            total: 0,
            intent: None,
            steps: Vec::new(),
            actions: Vec::new(),
            error: Some(msg),
            events_sig,
        },
    }
}
