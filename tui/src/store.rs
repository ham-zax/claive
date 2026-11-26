//! Read-only $CLAIVE_DIR state ingestion, inotify coalescing, and bounded JSONL tailing.
//! Strictly read-only: NEVER creates directories, NEVER acquires locks, NEVER prunes watchers.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use notify::{Config as NotifyConfig, Event, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Deserialize;
use serde_json::Value;

use crate::proc::{check_worker_liveness, verify_watcher_alive, LivenessOutcome};
use crate::sanitize::{sha256_hex, single_line};

pub const MAX_JSON_BYTES: u64 = 1024 * 1024; // 1 MiB read cap
pub const MAX_JSONL_INITIAL_TAIL_BYTES: u64 = 2 * 1024 * 1024; // 2 MiB tail window on cold start
pub const MAX_JSONL_LINE_BYTES: usize = 256 * 1024;
pub const MAX_DISPLAY_CHANGED_FILES: usize = 64;
pub const MAX_INBOX_ITEMS: usize = 200;
pub const MAX_RECEIPT_ITEMS: usize = 100;
pub const COALESCE_WINDOW: Duration = Duration::from_millis(150);
pub const RECONCILE_INTERVAL: Duration = Duration::from_secs(2);

pub fn now_unix_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Resolve `$CLAIVE_DIR` matching `bin/claivelib/cli.py:root(create=False)`.
pub fn resolve_claive_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(p) = explicit {
        return p.to_path_buf();
    }
    if let Ok(val) = std::env::var("CLAIVE_DIR") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    let state_home = std::env::var("XDG_STATE_HOME")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
            PathBuf::from(home).join(".local/state")
        });
    state_home.join("claive")
}

/// Auto-detect parent session from harness environment variables matching `inbox.py` / `hooks.py`.
pub fn detect_env_session() -> Option<String> {
    if let Ok(explicit) = std::env::var("CLAIVE_PARENT_SESSION") {
        let s = sanitize_session_token(&explicit);
        if !s.is_empty() {
            return Some(s);
        }
    }
    let prefixes = [
        ("CLAUDE_SESSION_ID", "claude"),
        ("CLAUDE_CODE_SESSION_ID", "claude"),
        ("CODEX_THREAD_ID", "codex"),
        ("ANTIGRAVITY_CONVERSATION_ID", "agy"),
        ("AGY_CONVERSATION_ID", "agy"),
        ("OPENCODE_SESSION_ID", "opencode"),
        ("PI_SESSION_ID", "pi"),
    ];
    for (var, prefix) in prefixes {
        if let Ok(raw) = std::env::var(var) {
            let tok = sanitize_session_token(&raw);
            if !tok.is_empty() {
                let prefixed = format!("{prefix}-{tok}");
                return Some(prefixed.chars().take(64).collect());
            }
        }
    }
    None
}

fn sanitize_session_token(raw: &str) -> String {
    raw.trim()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
        .take(64)
        .collect()
}

pub fn is_worker_id(name: &str) -> bool {
    name.len() == 12
        && name
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

#[derive(Debug, Clone, PartialEq)]
pub struct ToolInfo {
    pub tool: String,
    pub status: String,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActivityInfo {
    pub stage: String,
    pub label: String,
    pub targets: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LastCheckInfo {
    pub command: String,
    pub status: String,
    pub summary: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NeedsParentInfo {
    pub kind: Option<String>,
    pub question: String,
    /// SHA-256 hex digest of the exact raw `needs_parent.question` string (for `--expected-question-hash`).
    pub raw_question_sha256: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct WorkerRecord {
    pub id: String,
    pub alias: Option<String>,
    pub label: String,
    pub engine: String,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub reasoning_effort: Option<String>,
    pub workspace: Option<String>,
    pub actual_workspace: Option<String>,
    pub retained_worktree: Option<String>,
    pub has_managed_worktree: bool,
    pub worktree_branch: Option<String>,
    pub worktree_base_commit: Option<String>,
    pub reusable: bool,
    pub parent_session: Option<String>,
    pub mission: Option<String>,
    pub batch: Option<String>,
    pub parked: bool,
    pub close_requested: bool,
    pub queue_depth: usize,
    pub turn: u64,
    pub raw_status: String,
    pub effective_status: String,
    pub liveness_lost: bool,
    pub phase: String,
    pub step_count: u64,
    pub model_steps: u64,
    pub started_at: Option<f64>,
    pub ended_at: Option<f64>,
    pub turn_started_at: Option<f64>,
    pub last_activity: Option<f64>,
    pub elapsed_s: Option<f64>,
    pub turn_elapsed_s: Option<f64>,
    pub last_event_age_s: Option<f64>,
    pub active_tools: Vec<String>,
    pub last_tool: Option<ToolInfo>,
    pub latest_intent: Option<String>,
    pub activity: Option<ActivityInfo>,
    pub last_check: Option<LastCheckInfo>,
    pub prompt_file: Option<String>,
    pub turn_prompt_file: Option<String>,
    pub prompt_preview: Option<String>,
    pub prompt_excerpt: Vec<String>,
    pub has_result_file: bool,
    pub task_failures: u64,
    pub needs_parent: Option<NeedsParentInfo>,
    pub ask_eligible: bool,
    pub failure_kind: Option<String>,
    pub error: Option<String>,
    pub stderr_hint: Option<String>,
    pub exit_code: Option<i64>,
    pub changed_files: Vec<String>,
    pub changed_files_total: usize,
    pub changed_files_truncated: bool,
    pub owned_paths: Vec<String>,
    pub delta_added: Option<usize>,
    pub delta_deleted: Option<usize>,
    pub delta_untracked: Option<usize>,
    pub diff_stat: Option<String>,
    pub verification_status: String,
    pub acceptance_status: String,
    pub report_state: Option<String>,
    pub report_summary: Option<String>,
    pub report_risks: Vec<String>,
    pub native_cache_read_tokens: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
    pub context_tokens: Option<u64>,
    pub watched_by: Vec<String>,
    pub status_projection_stale: bool,
    pub events_sig: Option<(u64, i64)>,
    pub job_dir: PathBuf,
}

impl WorkerRecord {
    pub fn display_name(&self) -> String {
        match &self.alias {
            Some(a) if !a.is_empty() => format!("@{a}"),
            _ => self.id.clone(),
        }
    }

    pub fn is_active_turn(&self) -> bool {
        matches!(
            self.effective_status.as_str(),
            "starting" | "running" | "cancelling"
        ) || (self.effective_status == "idle" && self.queue_depth > 0)
    }

    pub fn is_codex_parent(&self) -> bool {
        self.parent_session
            .as_deref()
            .map(|s| s.starts_with("codex-"))
            .unwrap_or(false)
    }

    /// True when `label` is empty, generic `"prompt-input"`, or just echoes the worker's alias/id.
    pub fn is_alias_like_label(&self) -> bool {
        let trimmed = self.label.trim();
        if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("prompt-input") {
            return true;
        }
        let norm = trimmed.strip_prefix('@').unwrap_or(trimmed);
        if norm.eq_ignore_ascii_case(&self.id) {
            return true;
        }
        if let Some(a) = self.alias.as_deref() {
            let a_norm = a.trim().strip_prefix('@').unwrap_or(a.trim());
            if !a_norm.is_empty() && norm.eq_ignore_ascii_case(a_norm) {
                return true;
            }
        }
        false
    }

    /// Prefer the parsed `stderr.log` root cause when `self.error` is just a generic wrapper exit string.
    pub fn best_error_summary(&self) -> Option<String> {
        let is_generic = self
            .error
            .as_deref()
            .map(|e| e.starts_with("exit=") || e.contains("terminal=failed"))
            .unwrap_or(true);
        if is_generic && let Some(ref hint) = self.stderr_hint {
            return Some(hint.clone());
        }
        self.error.clone().or_else(|| self.stderr_hint.clone())
    }

    /// True if `rel_path` matches at least one pattern in `owned_paths` (or if no `owned_paths` are declared).
    pub fn is_path_in_owned_scope(&self, rel_path: &str) -> bool {
        if self.owned_paths.is_empty() {
            return true;
        }
        let clean_path = rel_path.trim().trim_start_matches("./");
        self.owned_paths
            .iter()
            .any(|pat| glob_match(pat.trim().trim_start_matches("./"), clean_path))
    }
}

fn glob_match(pattern: &str, text: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = text.chars().collect();
    glob_match_slice(&p, &t)
}

fn glob_match_slice(p: &[char], t: &[char]) -> bool {
    if p.is_empty() {
        return t.is_empty();
    }
    if p.len() >= 2 && p[0] == '*' && p[1] == '*' {
        let mut rest = &p[2..];
        if rest.first() == Some(&'/') {
            rest = &rest[1..];
        }
        if glob_match_slice(rest, t) {
            return true;
        }
        for i in 1..=t.len() {
            if glob_match_slice(rest, &t[i..]) {
                return true;
            }
        }
        return false;
    }
    if p[0] == '*' {
        if glob_match_slice(&p[1..], t) {
            return true;
        }
        for (i, ch) in t.iter().enumerate() {
            if *ch == '/' {
                break;
            }
            if glob_match_slice(&p[1..], &t[i + 1..]) {
                return true;
            }
        }
        return false;
    }
    if t.is_empty() {
        return false;
    }
    if p[0] == '?' || p[0] == t[0] {
        return glob_match_slice(&p[1..], &t[1..]);
    }
    false
}

#[derive(Debug, Clone, PartialEq)]
pub struct WatcherRecord {
    pub id: String,
    pub pid: i32,
    pub worker_ids: Vec<String>,
    pub inbox_consumer: Option<String>,
    pub parent_session: Option<String>,
    pub started_at: Option<f64>,
}

impl WatcherRecord {
    /// Coverage parity with `bin/claivelib/watchers.py:covers_worker`.
    pub fn covers(&self, worker_id: &str, worker_session: Option<&str>) -> bool {
        if self.worker_ids.iter().any(|id| id == worker_id) {
            return true;
        }
        if let (Some(ws), Some(ps)) = (self.parent_session.as_deref(), worker_session)
            && ws == ps
        {
            return true;
        }
        if self.inbox_consumer.is_some()
            && self.parent_session.is_none()
            && worker_session.is_none()
        {
            return true;
        }
        false
    }

    pub fn label(&self) -> String {
        if let Some(s) = &self.parent_session {
            s.clone()
        } else if let Some(c) = &self.inbox_consumer {
            format!("inbox:{c}")
        } else {
            format!("pid:{}", self.pid)
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct InboxItem {
    pub seq: Option<u64>,
    pub at: String,
    pub event_type: String,
    pub worker_id: String,
    pub alias: Option<String>,
    pub status: String,
    pub code: Option<i64>,
    pub parent_session: Option<String>,
    pub mission: Option<String>,
    pub wake: bool,
    pub summary: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ReceiptItem {
    pub seq: Option<u64>,
    pub at: String,
    pub event_id: String,
    pub event_type: String,
    pub sink: String,
    pub kind: String,
    pub status: String,
    pub exit_code: Option<i64>,
    pub duration_ms: Option<u64>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct WorktreeEntry {
    pub path: String,
    pub job: Option<String>,
    pub branch: Option<String>,
    pub source: Option<String>,
    pub base_commit: Option<String>,
    pub event: String,
}

#[derive(Debug, Clone)]
pub struct RegistrySnapshot {
    pub workers: Vec<WorkerRecord>,
    pub live_watchers: Vec<WatcherRecord>,
    pub inbox: Vec<InboxItem>,
    pub receipts: Vec<ReceiptItem>,
    pub refreshed_at: f64,
    pub stale_reason: Option<String>,
    pub claive_dir: PathBuf,
}

// --- Open-string serde DTOs (unknown fields/variants never drop records) ---

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawState {
    id: Option<String>,
    label: Option<String>,
    engine: Option<String>,
    model: Option<String>,
    reasoning_effort: Option<String>,
    workspace: Option<String>,
    actual_workspace: Option<String>,
    retained_worktree: Option<String>,
    reusable: Option<bool>,
    parent_session: Option<String>,
    mission: Option<String>,
    batch: Option<String>,
    parked: Option<bool>,
    turn: Option<u64>,
    status: Option<String>,
    phase: Option<String>,
    event_steps: Option<u64>,
    steps: Option<u64>,
    started_at: Option<f64>,
    ended_at: Option<f64>,
    turn_started_at: Option<f64>,
    last_activity: Option<f64>,
    supervisor_pid: Option<i32>,
    supervisor_identity: Option<String>,
    active_tools: Option<Value>,
    last_tool: Option<Value>,
    latest_intent: Option<String>,
    activity: Option<Value>,
    last_check: Option<Value>,
    prompt_file: Option<String>,
    turn_prompt_file: Option<String>,
    turn_task_failures: Option<u64>,
    task_failures: Option<u64>,
    needs_parent: Option<Value>,
    failure_kind: Option<String>,
    error: Option<String>,
    exit_code: Option<i64>,
    touched_files: Option<Vec<String>>,
    report_state: Option<String>,
    report: Option<Value>,
    turn_usage: Option<Value>,
    context_tokens: Option<u64>,
    acceptance_contract: Option<Value>,
    workspace_delta: Option<Value>,
    launch: Option<Value>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawStatus {
    id: Option<String>,
    alias: Option<String>,
    label: Option<String>,
    engine: Option<String>,
    provider: Option<String>,
    model: Option<String>,
    reasoning_effort: Option<String>,
    workspace: Option<String>,
    actual_workspace: Option<String>,
    retained_worktree: Option<String>,
    reusable: Option<bool>,
    parent_session: Option<String>,
    parked: Option<bool>,
    turn: Option<u64>,
    status: Option<String>,
    phase: Option<String>,
    step_count: Option<u64>,
    model_steps: Option<u64>,
    updated_at: Option<f64>,
    elapsed_s: Option<f64>,
    turn_elapsed_s: Option<f64>,
    last_event_age_s: Option<f64>,
    active_tools: Option<Value>,
    last_tool: Option<Value>,
    latest_intent: Option<String>,
    activity: Option<Value>,
    last_check: Option<Value>,
    task_failures: Option<u64>,
    needs_parent: Option<Value>,
    failure_kind: Option<String>,
    error: Option<String>,
    exit_code: Option<i64>,
    changed_files: Option<Vec<String>>,
    changed_files_total: Option<usize>,
    changed_files_truncated: Option<bool>,
    verification_status: Option<String>,
    acceptance_status: Option<String>,
    report_state: Option<String>,
    report: Option<Value>,
    native_cache_read_tokens: Option<u64>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawWatcher {
    id: Option<String>,
    pid: Option<i32>,
    pid_identity: Option<String>,
    worker_ids: Option<Vec<String>>,
    inbox_consumer: Option<String>,
    parent_session: Option<String>,
    started_at: Option<f64>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawWorktreeLine {
    event: Option<String>,
    job: Option<String>,
    path: Option<String>,
    branch: Option<String>,
    source: Option<String>,
    base_commit: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct RawEvidenceStatus {
    status: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileSig {
    ino: u64,
    len: u64,
    mtime_sec: i64,
    mtime_nsec: i64,
}

impl FileSig {
    fn from_meta(m: &fs::Metadata) -> Self {
        Self {
            ino: m.ino(),
            len: m.len(),
            mtime_sec: m.mtime(),
            mtime_nsec: m.mtime_nsec(),
        }
    }
}

/// Incremental read-only JSONL tailer: commits offsets strictly on `\n`, skips malformed lines,
/// resets cleanly on inode replacement or truncation (e.g. `_repair_trailing_torn_line`).
pub struct JsonlTailer<T> {
    path: PathBuf,
    ino: Option<u64>,
    offset: u64,
    items: VecDeque<T>,
    max_items: usize,
    parser: fn(&str) -> Option<T>,
}

impl<T: Clone> JsonlTailer<T> {
    pub fn new(path: PathBuf, max_items: usize, parser: fn(&str) -> Option<T>) -> Self {
        Self {
            path,
            ino: None,
            offset: 0,
            items: VecDeque::with_capacity(max_items.min(256)),
            max_items,
            parser,
        }
    }

    pub fn refresh(&mut self) {
        let Ok(meta) = fs::metadata(&self.path) else {
            self.ino = None;
            self.offset = 0;
            self.items.clear();
            return;
        };
        if !meta.is_file() {
            return;
        }
        let ino = meta.ino();
        let len = meta.len();

        // Reset on inode change or truncation.
        if self.ino != Some(ino) || len < self.offset {
            self.ino = Some(ino);
            self.items.clear();
            if len > MAX_JSONL_INITIAL_TAIL_BYTES {
                self.offset = len - MAX_JSONL_INITIAL_TAIL_BYTES;
            } else {
                self.offset = 0;
            }
        }

        if len == self.offset {
            return;
        }

        let Ok(mut file) = File::open(&self.path) else {
            return;
        };
        let started_mid_file = self.offset > 0 && self.items.is_empty();
        if file.seek(SeekFrom::Start(self.offset)).is_err() {
            return;
        }

        let mut reader = BufReader::new(file);
        let mut buf = Vec::with_capacity(4096);
        let mut first_line = started_mid_file;

        loop {
            buf.clear();
            let Ok(n) = reader.read_until(b'\n', &mut buf) else {
                break;
            };
            if n == 0 {
                break;
            }
            // Do NOT advance offset past a torn trailing line without '\n'.
            if *buf.last().unwrap_or(&0) != b'\n' {
                break;
            }
            self.offset += n as u64;
            if first_line {
                // We sought into the middle of a large file; discard the partial first line.
                first_line = false;
                continue;
            }
            if buf.len() > MAX_JSONL_LINE_BYTES {
                continue;
            }
            let line = String::from_utf8_lossy(&buf);
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            if let Some(item) = (self.parser)(trimmed) {
                if self.items.len() >= self.max_items {
                    self.items.pop_front();
                }
                self.items.push_back(item);
            }
        }
    }

    pub fn snapshot(&self) -> Vec<T> {
        self.items.iter().cloned().collect()
    }
}

fn read_capped_string(path: &Path, max_bytes: u64) -> Option<String> {
    let file = File::open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.len() > max_bytes {
        return None;
    }
    let mut s = String::with_capacity(meta.len().min(65536) as usize);
    file.take(max_bytes).read_to_string(&mut s).ok()?;
    Some(s)
}

fn parse_active_tools(val: Option<&Value>) -> Vec<String> {
    let Some(Value::Object(map)) = val else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for (k, v) in map {
        let name = v
            .as_object()
            .and_then(|o| o.get("tool"))
            .and_then(|t| t.as_str())
            .or_else(|| v.as_str())
            .unwrap_or(k.as_str());
        let cleaned = single_line(name);
        if !cleaned.is_empty() {
            out.push(cleaned);
        }
    }
    out
}

fn parse_last_tool(val: Option<&Value>) -> Option<ToolInfo> {
    let obj = val?.as_object()?;
    let tool = obj
        .get("tool")
        .or_else(|| obj.get("name"))
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_else(|| "tool".to_string());
    let status = obj
        .get("status")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_else(|| "done".to_string());
    let detail = obj
        .get("detail")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    Some(ToolInfo {
        tool,
        status,
        detail,
    })
}

fn parse_activity(val: Option<&Value>) -> Option<ActivityInfo> {
    let obj = val?.as_object()?;
    let stage = obj
        .get("stage")
        .and_then(|v| v.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty())?;
    let label = obj
        .get("label")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    let targets = obj
        .get("targets")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|t| t.as_str().map(single_line))
                .filter(|s| !s.is_empty())
                .take(3)
                .collect()
        })
        .unwrap_or_default();
    Some(ActivityInfo {
        stage,
        label,
        targets,
    })
}

fn parse_last_check(val: Option<&Value>) -> Option<LastCheckInfo> {
    let obj = val?.as_object()?;
    let status = obj
        .get("status")
        .and_then(|v| v.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty())?;
    let command = obj
        .get("command")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    let summary = obj
        .get("summary")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    Some(LastCheckInfo {
        command,
        status,
        summary,
    })
}

fn parse_needs_parent(val: Option<&Value>) -> Option<NeedsParentInfo> {
    let v = val?;
    if v.is_null() {
        return None;
    }
    if let Some(obj) = v.as_object() {
        let kind = obj
            .get("kind")
            .and_then(|k| k.as_str())
            .map(single_line)
            .filter(|s| !s.is_empty());
        let raw_q = obj.get("question").and_then(|q| q.as_str()).unwrap_or("");
        let raw_question_sha256 = Some(sha256_hex(raw_q.as_bytes()));
        let question = obj
            .get("question")
            .or_else(|| obj.get("message"))
            .or_else(|| obj.get("reason"))
            .and_then(|q| q.as_str())
            .map(single_line)
            .unwrap_or_default();
        return Some(NeedsParentInfo {
            kind,
            question,
            raw_question_sha256,
        });
    }
    if let Some(s) = v.as_str() {
        let cleaned = single_line(s);
        if !cleaned.is_empty() {
            return Some(NeedsParentInfo {
                kind: None,
                question: cleaned,
                raw_question_sha256: Some(sha256_hex(s.as_bytes())),
            });
        }
    }
    None
}

fn parse_report_fields(val: Option<&Value>) -> (Option<String>, Vec<String>) {
    let Some(obj) = val.and_then(|v| v.as_object()) else {
        return (None, Vec::new());
    };
    let summary = obj
        .get("summary")
        .and_then(|s| s.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty());
    let risks = obj
        .get("residual_risks")
        .or_else(|| obj.get("risks"))
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str())
                .map(single_line)
                .filter(|s| !s.is_empty())
                .take(12)
                .collect()
        })
        .unwrap_or_default();
    (summary, risks)
}

fn parse_inbox_line(line: &str) -> Option<InboxItem> {
    let val: Value = serde_json::from_str(line).ok()?;
    let obj = val.as_object()?;
    let seq = obj.get("seq").and_then(|v| v.as_u64());
    let at = obj
        .get("at")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    let event_type = obj
        .get("type")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_else(|| "event".to_string());
    let worker_id = obj
        .get("id")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    let alias = obj
        .get("digest")
        .and_then(|d| d.get("alias"))
        .or_else(|| obj.get("alias"))
        .and_then(|v| v.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty());
    let status = obj
        .get("status")
        .and_then(|v| v.as_str())
        .map(single_line)
        .unwrap_or_default();
    let code = obj.get("code").and_then(|v| v.as_i64());
    let parent_session = obj
        .get("parent_session")
        .and_then(|v| v.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty());
    let mission = obj
        .get("mission")
        .and_then(|v| v.as_str())
        .map(single_line)
        .filter(|s| !s.is_empty());

    // Parity with inbox.py:_is_wake_event
    let wake = match obj.get("wake").and_then(|v| v.as_bool()) {
        Some(b) => b,
        None => {
            event_type != "budget_handoff"
                || obj
                    .get("needs_parent")
                    .map(|v| !v.is_null())
                    .unwrap_or(false)
        }
    };

    let summary = obj
        .get("digest")
        .and_then(|d| {
            d.get("ask_question")
                .or_else(|| d.get("report_headline"))
                .and_then(|v| v.as_str())
        })
        .or_else(|| {
            obj.get("needs_parent")
                .and_then(|n| n.get("question"))
                .and_then(|v| v.as_str())
        })
        .or_else(|| obj.get("label").and_then(|v| v.as_str()))
        .map(single_line)
        .unwrap_or_default();

    Some(InboxItem {
        seq,
        at,
        event_type,
        worker_id,
        alias,
        status,
        code,
        parent_session,
        mission,
        wake,
        summary,
    })
}

fn parse_receipt_line(line: &str) -> Option<ReceiptItem> {
    let val: Value = serde_json::from_str(line).ok()?;
    let obj = val.as_object()?;
    Some(ReceiptItem {
        seq: obj.get("seq").and_then(|v| v.as_u64()),
        at: obj
            .get("at")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        event_id: obj
            .get("event_id")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        event_type: obj
            .get("event_type")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        sink: obj
            .get("sink")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        kind: obj
            .get("kind")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        status: obj
            .get("status")
            .and_then(|v| v.as_str())
            .map(single_line)
            .unwrap_or_default(),
        exit_code: obj.get("exit_code").and_then(|v| v.as_i64()),
        duration_ms: obj.get("duration_ms").and_then(|v| v.as_u64()),
        error: obj
            .get("error")
            .and_then(|v| v.as_str())
            .map(single_line)
            .filter(|s| !s.is_empty()),
    })
}

fn read_aliases(root: &Path) -> HashMap<String, String> {
    let mut by_worker: HashMap<String, Vec<String>> = HashMap::new();
    let Some(raw) = read_capped_string(&root.join("aliases.json"), MAX_JSON_BYTES) else {
        return HashMap::new();
    };
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) else {
        return HashMap::new();
    };
    for (alias, target) in map {
        if let Some(worker_id) = target.as_str() {
            by_worker
                .entry(worker_id.to_string())
                .or_default()
                .push(single_line(&alias));
        }
    }
    by_worker
        .into_iter()
        .filter_map(|(wid, mut names)| {
            names.sort();
            names.into_iter().next().map(|first| (wid, first))
        })
        .collect()
}

fn read_live_watchers(root: &Path) -> Vec<WatcherRecord> {
    let dir = root.join("watchers");
    let Ok(entries) = fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut live = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(fname) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if fname.starts_with('.') || !fname.ends_with(".json") {
            continue;
        }
        let Some(raw) = read_capped_string(&path, MAX_JSON_BYTES) else {
            continue;
        };
        let Ok(rec) = serde_json::from_str::<RawWatcher>(&raw) else {
            continue;
        };
        if !verify_watcher_alive(rec.pid, rec.pid_identity.as_deref()) {
            // Strictly read-only: NEVER unlink dead watcher files.
            continue;
        }
        live.push(WatcherRecord {
            id: rec.id.unwrap_or_else(|| fname.to_string()),
            pid: rec.pid.unwrap_or(0),
            worker_ids: rec.worker_ids.unwrap_or_default(),
            inbox_consumer: rec.inbox_consumer.map(|s| single_line(&s)),
            parent_session: rec.parent_session.map(|s| single_line(&s)),
            started_at: rec.started_at,
        });
    }
    live.sort_by(|a, b| {
        a.started_at
            .unwrap_or(0.0)
            .total_cmp(&b.started_at.unwrap_or(0.0))
            .then_with(|| a.id.cmp(&b.id))
    });
    live
}

/// Reduce `worktrees.jsonl` by `path`, honoring `"removed"`/`"cleaned"` and path existence,
/// matching `bin/claivelib/worktrees.py:current()`.
fn read_active_worktrees(root: &Path) -> HashMap<String, WorktreeEntry> {
    let path = root.join("worktrees.jsonl");
    let Ok(file) = File::open(&path) else {
        return HashMap::new();
    };
    let reader = BufReader::new(file);
    let mut latest_by_path: BTreeMap<String, WorktreeEntry> = BTreeMap::new();
    for line in reader.lines().take(50_000).flatten() {
        if line.len() > MAX_JSONL_LINE_BYTES {
            continue;
        }
        let Ok(parsed) = serde_json::from_str::<RawWorktreeLine>(&line) else {
            continue;
        };
        let Some(wt_path) = parsed.path.filter(|p| !p.is_empty()) else {
            continue;
        };
        latest_by_path.insert(
            wt_path.clone(),
            WorktreeEntry {
                path: wt_path,
                job: parsed.job,
                branch: parsed.branch.map(|s| single_line(&s)),
                source: parsed.source.map(|s| single_line(&s)),
                base_commit: parsed.base_commit.map(|s| single_line(&s)),
                event: parsed.event.unwrap_or_default(),
            },
        );
    }

    let mut by_job = HashMap::new();
    for entry in latest_by_path.into_values() {
        if matches!(entry.event.as_str(), "removed" | "cleaned") {
            continue;
        }
        if !Path::new(&entry.path).exists() {
            continue;
        }
        if let Some(job) = entry.job.clone() {
            by_job.insert(job, entry);
        }
    }
    by_job
}

struct CachedWorkerFiles {
    state_sig: Option<FileSig>,
    state: Option<RawState>,
    status_sig: Option<FileSig>,
    status: Option<RawStatus>,
    prompt_preview_loaded: bool,
    prompt_preview: Option<String>,
    prompt_excerpt: Vec<String>,
    stderr_sig: Option<FileSig>,
    stderr_hint: Option<String>,
}

fn read_stderr_hint(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let len = file.metadata().ok()?.len();
    if len == 0 {
        return None;
    }
    let max_bytes: u64 = 4096;
    if len > max_bytes {
        file.seek(SeekFrom::Start(len - max_bytes)).ok()?;
    }
    let mut buf = Vec::with_capacity(len.min(max_bytes) as usize);
    file.take(max_bytes).read_to_end(&mut buf).ok()?;
    let raw = String::from_utf8_lossy(&buf);

    // 1. Prefer structured AGY_ERROR JSON `short_error` / `message` if present.
    for line in raw.lines().rev() {
        if let Some((_, json_part)) = line.split_once("AGY_ERROR:")
            && let Ok(val) = serde_json::from_str::<Value>(json_part.trim())
            && let Some(msg) = val
                .get("short_error")
                .or_else(|| val.get("message"))
                .and_then(|v| v.as_str())
        {
            let clean = single_line(msg);
            if !clean.is_empty() {
                return Some(clean);
            }
        }
    }

    // 2. Otherwise scan from bottom for a high-signal error line.
    let mut last_non_empty: Option<String> = None;
    for line in raw.lines().rev() {
        let clean = single_line(line);
        if clean.is_empty() {
            continue;
        }
        if last_non_empty.is_none() {
            last_non_empty = Some(clean.clone());
        }
        let low = clean.to_ascii_lowercase();
        if low.contains("error")
            || low.contains("exception")
            || low.contains("fatal")
            || low.contains("failed")
            || low.contains("panic")
            || low.contains("quota")
            || low.contains("exhausted")
            || low.contains("denied")
            || low.contains("timeout")
            || low.contains("cannot find")
            || low.contains("enoent")
        {
            return Some(clean);
        }
    }
    last_non_empty
}

fn is_prompt_boilerplate(low: &str) -> bool {
    low.starts_with("you are a worker")
        || low.starts_with("you are a claive")
        || low.starts_with("read /")
        || low.starts_with("do not commit")
        || low.starts_with("end your final answer with")
        || low.starts_with("use needs_decision instead of guessing")
        || matches!(
            low,
            "mission" | "task" | "goal" | "summary" | "result" | "overview" | "context"
        )
}

fn read_prompt_summary(job_dir: &Path, prompt_file: Option<&str>) -> (Option<String>, Vec<String>) {
    let path = prompt_file
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|pf| {
            let p = PathBuf::from(pf);
            if p.is_absolute() { p } else { job_dir.join(p) }
        })
        .filter(|p| p.is_file())
        .unwrap_or_else(|| job_dir.join("prompt-0001.md"));
    let Some(raw) = read_capped_string(&path, 4096) else {
        return (None, Vec::new());
    };
    let mut title: Option<String> = None;
    let mut excerpt: Vec<String> = Vec::new();
    for raw_line in raw.lines() {
        let line = single_line(raw_line);
        let trimmed = line.trim_start_matches('#').trim();
        if trimmed.is_empty() {
            continue;
        }
        let low = trimmed.to_ascii_lowercase();
        if is_prompt_boilerplate(&low) {
            continue;
        }
        let cleaned = trimmed
            .strip_prefix("Task:")
            .or_else(|| trimmed.strip_prefix("Goal:"))
            .or_else(|| trimmed.strip_prefix("Mission:"))
            .map(str::trim)
            .unwrap_or(trimmed);
        if cleaned.is_empty() {
            continue;
        }
        if title.is_none() {
            title = Some(cleaned.to_string());
        } else if excerpt.len() < 4 {
            excerpt.push(cleaned.to_string());
        } else {
            break;
        }
    }
    (title, excerpt)
}

/// Stateful read-only registry scanner with per-worker `(ino, len, mtime)` caching
/// and incremental JSONL tailers.
pub struct RegistryReader {
    root: PathBuf,
    cache: HashMap<String, CachedWorkerFiles>,
    inbox: JsonlTailer<InboxItem>,
    receipts: JsonlTailer<ReceiptItem>,
}

impl RegistryReader {
    pub fn new(root: PathBuf) -> Self {
        let inbox = JsonlTailer::new(root.join("inbox.jsonl"), MAX_INBOX_ITEMS, parse_inbox_line);
        let receipts = JsonlTailer::new(
            root.join("notifications").join("receipts.jsonl"),
            MAX_RECEIPT_ITEMS,
            parse_receipt_line,
        );
        Self {
            root,
            cache: HashMap::new(),
            inbox,
            receipts,
        }
    }

    pub fn scan(&mut self, now: f64) -> RegistrySnapshot {
        self.inbox.refresh();
        self.receipts.refresh();

        let aliases = read_aliases(&self.root);
        let live_watchers = read_live_watchers(&self.root);
        let active_worktrees = read_active_worktrees(&self.root);

        let entries = match fs::read_dir(&self.root) {
            Ok(rd) => rd,
            Err(err) => {
                return RegistrySnapshot {
                    workers: Vec::new(),
                    live_watchers,
                    inbox: self.inbox.snapshot(),
                    receipts: self.receipts.snapshot(),
                    refreshed_at: now,
                    stale_reason: Some(format!("Cannot read {}: {err}", self.root.display())),
                    claive_dir: self.root.clone(),
                };
            }
        };

        let mut seen_ids = HashSet::new();
        let mut workers = Vec::new();

        for entry in entries.flatten() {
            let fname = entry.file_name();
            let Some(id) = fname.to_str() else {
                continue;
            };
            if !is_worker_id(id) {
                continue;
            }
            let job_dir = entry.path();
            if !job_dir.is_dir() {
                continue;
            }
            let state_path = job_dir.join("state.json");
            let status_path = job_dir.join("status.json");
            if !state_path.is_file() && !status_path.is_file() {
                continue;
            }
            seen_ids.insert(id.to_string());

            let cached = self
                .cache
                .entry(id.to_string())
                .or_insert_with(|| CachedWorkerFiles {
                    state_sig: None,
                    state: None,
                    status_sig: None,
                    status: None,
                    prompt_preview_loaded: false,
                    prompt_preview: None,
                    prompt_excerpt: Vec::new(),
                    stderr_sig: None,
                    stderr_hint: None,
                });

            // Re-read state.json only when (ino, len, mtime) changes.
            let cur_state_sig = fs::metadata(&state_path).ok().map(|m| FileSig::from_meta(&m));
            if cur_state_sig != cached.state_sig {
                cached.state_sig = cur_state_sig;
                cached.state = cur_state_sig.and_then(|_| {
                    read_capped_string(&state_path, MAX_JSON_BYTES)
                        .and_then(|raw| serde_json::from_str::<RawState>(&raw).ok())
                });
            }

            // Re-read status.json only when (ino, len, mtime) changes.
            let cur_status_sig = fs::metadata(&status_path).ok().map(|m| FileSig::from_meta(&m));
            if cur_status_sig != cached.status_sig {
                cached.status_sig = cur_status_sig;
                cached.status = cur_status_sig.and_then(|_| {
                    read_capped_string(&status_path, MAX_JSON_BYTES)
                        .and_then(|raw| serde_json::from_str::<RawStatus>(&raw).ok())
                });
            }

            if cached.state.is_none() && cached.status.is_none() {
                continue;
            }

            // Always lazily load the initial prompt title & compact excerpt once per worker.
            if !cached.prompt_preview_loaded {
                let pf = cached.state.as_ref().and_then(|s| s.prompt_file.as_deref());
                let (preview, excerpt) = read_prompt_summary(&job_dir, pf);
                cached.prompt_preview = preview;
                cached.prompt_excerpt = excerpt;
                cached.prompt_preview_loaded = true;
            }

            // Lazily read a root-cause hint from stderr.log for failed/errored workers when stderr changes.
            let has_failure = cached
                .state
                .as_ref()
                .map(|s| {
                    matches!(s.status.as_deref(), Some("failed" | "interrupted"))
                        || s.exit_code.map(|c| c != 0).unwrap_or(false)
                        || s.error.is_some()
                })
                .unwrap_or(false);
            if has_failure {
                let stderr_path = job_dir.join("stderr.log");
                let cur_stderr_sig = fs::metadata(&stderr_path).ok().map(|m| FileSig::from_meta(&m));
                if cur_stderr_sig != cached.stderr_sig {
                    cached.stderr_sig = cur_stderr_sig;
                    cached.stderr_hint = cur_stderr_sig.and_then(|_| read_stderr_hint(&stderr_path));
                }
            }

            let record = build_worker_record(
                id,
                &job_dir,
                cached.state.as_ref(),
                cached.status.as_ref(),
                cached.prompt_preview.clone(),
                cached.prompt_excerpt.clone(),
                cached.stderr_hint.clone(),
                &aliases,
                &live_watchers,
                active_worktrees.get(id),
                now,
            );
            workers.push(record);
        }

        self.cache.retain(|k, _| seen_ids.contains(k));

        RegistrySnapshot {
            workers,
            live_watchers,
            inbox: self.inbox.snapshot(),
            receipts: self.receipts.snapshot(),
            refreshed_at: now,
            stale_reason: None,
            claive_dir: self.root.clone(),
        }
    }
}

/// Convenience one-shot snapshot loader (great for tests and initial cold-start render).
pub fn load_snapshot(root: &Path, now: f64) -> RegistrySnapshot {
    let mut reader = RegistryReader::new(root.to_path_buf());
    reader.scan(now)
}

#[allow(clippy::too_many_arguments)]
fn build_worker_record(
    id: &str,
    job_dir: &Path,
    state: Option<&RawState>,
    status: Option<&RawStatus>,
    prompt_preview: Option<String>,
    prompt_excerpt: Vec<String>,
    stderr_hint: Option<String>,
    aliases: &HashMap<String, String>,
    live_watchers: &[WatcherRecord],
    wt_entry: Option<&WorktreeEntry>,
    now: f64,
) -> WorkerRecord {
    // Determine whether status.json is fresh relative to state.json.
    let status_fresh = match (state, status) {
        (Some(st), Some(sp)) => {
            let st_turn = st.turn.unwrap_or(1);
            let sp_turn = sp.turn.unwrap_or(1);
            let status_match = st.status.as_deref() == sp.status.as_deref();
            sp_turn >= st_turn && status_match
        }
        (None, Some(_)) => true,
        _ => false,
    };
    let status_projection_stale = status.is_none() || !status_fresh;
    let sp = if status_fresh { status } else { None };

    let raw_status = state
        .and_then(|s| s.status.as_deref())
        .or_else(|| status.and_then(|s| s.status.as_deref()))
        .unwrap_or("unknown")
        .to_string();

    let started_at = state.and_then(|s| s.started_at);
    let ended_at = state.and_then(|s| s.ended_at);
    let turn_started_at = state.and_then(|s| s.turn_started_at).or(started_at);
    let last_activity = state.and_then(|s| s.last_activity);

    let liveness = check_worker_liveness(
        job_dir,
        &raw_status,
        state.and_then(|s| s.supervisor_pid),
        state.and_then(|s| s.supervisor_identity.as_deref()),
        started_at,
        now,
    );

    let (effective_status, liveness_lost, liveness_err, liveness_failure_kind) = match liveness {
        LivenessOutcome::Alive => (raw_status.clone(), false, None, None),
        LivenessOutcome::Interrupted(msg) => (
            "interrupted".to_string(),
            true,
            Some(msg.to_string()),
            Some("interrupted".to_string()),
        ),
        LivenessOutcome::Cancelled(msg) => (
            "cancelled".to_string(),
            true,
            Some(msg.to_string()),
            None,
        ),
    };

    let turn = state
        .and_then(|s| s.turn)
        .or_else(|| sp.and_then(|s| s.turn))
        .unwrap_or(1);

    let phase = sp
        .and_then(|s| s.phase.as_deref())
        .or_else(|| state.and_then(|s| s.phase.as_deref()))
        .map(single_line)
        .unwrap_or_default();

    let alias = aliases
        .get(id)
        .cloned()
        .or_else(|| status.and_then(|s| s.alias.as_deref()).map(single_line));

    let label = state
        .and_then(|s| s.label.as_deref())
        .or_else(|| status.and_then(|s| s.label.as_deref()))
        .map(single_line)
        .unwrap_or_default();

    let engine = state
        .and_then(|s| s.engine.as_deref())
        .or_else(|| status.and_then(|s| s.engine.as_deref()))
        .map(single_line)
        .unwrap_or_else(|| "unknown".to_string());

    let provider = sp
        .and_then(|s| s.provider.as_deref())
        .or_else(|| {
            state
                .and_then(|s| s.launch.as_ref())
                .and_then(|l| l.get("provider"))
                .and_then(|v| v.as_str())
        })
        .map(single_line);

    let model = state
        .and_then(|s| s.model.as_deref())
        .or_else(|| status.and_then(|s| s.model.as_deref()))
        .map(single_line);

    let reasoning_effort = state
        .and_then(|s| s.reasoning_effort.as_deref())
        .or_else(|| status.and_then(|s| s.reasoning_effort.as_deref()))
        .map(single_line);

    let workspace = state
        .and_then(|s| s.workspace.as_deref())
        .or_else(|| status.and_then(|s| s.workspace.as_deref()))
        .map(single_line);

    let actual_workspace = state
        .and_then(|s| s.actual_workspace.as_deref())
        .or_else(|| status.and_then(|s| s.actual_workspace.as_deref()))
        .map(single_line);

    let retained_worktree = state
        .and_then(|s| s.retained_worktree.as_deref())
        .or_else(|| status.and_then(|s| s.retained_worktree.as_deref()))
        .map(single_line);

    let launch_isolation_owner = state
        .and_then(|s| s.launch.as_ref())
        .and_then(|l| l.get("isolation"))
        .and_then(|iso| iso.get("owner"))
        .and_then(|v| v.as_str());

    let has_managed_worktree = wt_entry.is_some()
        || (launch_isolation_owner == Some("claive")
            && retained_worktree
                .as_deref()
                .map(|p| Path::new(p).is_dir())
                .unwrap_or(false));

    let reusable = state
        .and_then(|s| s.reusable)
        .or_else(|| status.and_then(|s| s.reusable))
        .unwrap_or(false);

    let parent_session = state
        .and_then(|s| s.parent_session.as_deref())
        .or_else(|| status.and_then(|s| s.parent_session.as_deref()))
        .map(single_line)
        .filter(|s| !s.is_empty());

    let mission = state
        .and_then(|s| s.mission.as_deref())
        .map(single_line)
        .filter(|s| !s.is_empty());

    let batch = state
        .and_then(|s| s.batch.as_deref())
        .map(single_line)
        .filter(|s| !s.is_empty());

    let parked = job_dir.join("parked.json").is_file()
        || state.and_then(|s| s.parked).unwrap_or(false)
        || sp.and_then(|s| s.parked).unwrap_or(false);

    let close_requested = job_dir.join("close.request").exists();

    let queue_depth = fs::read_dir(job_dir.join("requests"))
        .ok()
        .map(|rd| {
            rd.flatten()
                .filter(|e| {
                    e.file_name()
                        .to_str()
                        .map(|n| n.ends_with(".json") && !n.starts_with('.'))
                        .unwrap_or(false)
                })
                .count()
        })
        .unwrap_or(0);

    let is_settled_or_lost = liveness_lost
        || matches!(
            effective_status.as_str(),
            "completed" | "failed" | "interrupted" | "cancelled"
        );
    let end_ref = ended_at.unwrap_or_else(|| {
        if is_settled_or_lost {
            last_activity
                .or(turn_started_at)
                .or(started_at)
                .unwrap_or(now)
        } else {
            now
        }
    });
    let elapsed_s = started_at
        .map(|s| (end_ref - s).max(0.0))
        .or_else(|| sp.and_then(|s| s.elapsed_s));
    let turn_elapsed_s = turn_started_at
        .map(|s| (end_ref - s).max(0.0))
        .or_else(|| sp.and_then(|s| s.turn_elapsed_s))
        .or(elapsed_s);
    let last_event_age_s = last_activity
        .map(|la| (now - la).max(0.0))
        .or_else(|| {
            sp.and_then(|s| match (s.last_event_age_s, s.updated_at) {
                (Some(age), Some(upd)) => Some((age + (now - upd).max(0.0)).max(0.0)),
                (Some(age), None) => Some(age.max(0.0)),
                _ => None,
            })
        });

    let active_tools = {
        let from_sp = parse_active_tools(sp.and_then(|s| s.active_tools.as_ref()));
        if !from_sp.is_empty() || sp.is_some() {
            from_sp
        } else {
            parse_active_tools(state.and_then(|s| s.active_tools.as_ref()))
        }
    };

    let last_tool = parse_last_tool(sp.and_then(|s| s.last_tool.as_ref()))
        .or_else(|| parse_last_tool(state.and_then(|s| s.last_tool.as_ref())));

    let latest_intent = sp
        .and_then(|s| s.latest_intent.as_deref())
        .or_else(|| state.and_then(|s| s.latest_intent.as_deref()))
        .map(single_line)
        .filter(|s| !s.is_empty());

    let activity = parse_activity(sp.and_then(|s| s.activity.as_ref()))
        .or_else(|| parse_activity(state.and_then(|s| s.activity.as_ref())));

    let last_check = parse_last_check(sp.and_then(|s| s.last_check.as_ref()))
        .or_else(|| parse_last_check(state.and_then(|s| s.last_check.as_ref())));

    let prompt_file = state
        .and_then(|s| s.prompt_file.as_deref())
        .map(single_line)
        .filter(|s| !s.is_empty());

    let needs_parent = parse_needs_parent(state.and_then(|s| s.needs_parent.as_ref()))
        .or_else(|| parse_needs_parent(sp.and_then(|s| s.needs_parent.as_ref())));

    // Exact hook eligibility parity with `bin/claivelib/hooks.py:_open_asks`.
    let ask_eligible = needs_parent.is_some()
        && phase != "session closed"
        && !matches!(effective_status.as_str(), "cancelled" | "interrupted")
        && !close_requested;

    // Changed files: prefer fresh status.json, fall back to state.json touched_files.
    let (changed_files, changed_files_total, changed_files_truncated) =
        if let Some(files) = sp.and_then(|s| s.changed_files.as_ref()) {
            let total = sp
                .and_then(|s| s.changed_files_total)
                .unwrap_or(files.len())
                .max(files.len());
            let capped: Vec<String> = files
                .iter()
                .take(MAX_DISPLAY_CHANGED_FILES)
                .map(|p| single_line(p))
                .collect();
            let trunc = sp
                .and_then(|s| s.changed_files_truncated)
                .unwrap_or(capped.len() < total);
            (capped, total, trunc)
        } else if let Some(files) = state.and_then(|s| s.touched_files.as_ref()) {
            let total = files.len();
            let capped: Vec<String> = files
                .iter()
                .take(MAX_DISPLAY_CHANGED_FILES)
                .map(|p| single_line(p))
                .collect();
            let trunc = capped.len() < total;
            (capped, total, trunc)
        } else {
            (Vec::new(), 0, false)
        };

    // Verification & acceptance status: prefer status.json, else read small evidence files directly.
    let (verification_status, acceptance_status) = if let Some(s) = sp {
        (
            s.verification_status
                .as_deref()
                .map(single_line)
                .unwrap_or_else(|| "not_run".to_string()),
            s.acceptance_status
                .as_deref()
                .map(single_line)
                .unwrap_or_else(|| "pending".to_string()),
        )
    } else {
        let ev_dir = job_dir.join("evidence").join(format!("turn-{turn:04}"));
        let ver = read_capped_string(&ev_dir.join("verification.json"), MAX_JSON_BYTES)
            .and_then(|raw| serde_json::from_str::<RawEvidenceStatus>(&raw).ok())
            .and_then(|r| r.status)
            .map(|s| single_line(&s))
            .unwrap_or_else(|| "not_run".to_string());
        let acc = read_capped_string(&ev_dir.join("acceptance.json"), MAX_JSON_BYTES)
            .and_then(|raw| serde_json::from_str::<RawEvidenceStatus>(&raw).ok())
            .and_then(|r| r.status)
            .map(|s| single_line(&s))
            .unwrap_or_else(|| "pending".to_string());
        (ver, acc)
    };

    let (report_summary, report_risks) = parse_report_fields(
        sp.and_then(|s| s.report.as_ref())
            .or_else(|| state.and_then(|s| s.report.as_ref())),
    );

    let native_cache_read_tokens = sp
        .and_then(|s| s.native_cache_read_tokens)
        .or_else(|| {
            state
                .and_then(|s| s.turn_usage.as_ref())
                .and_then(|u| u.get("native_cache_read_tokens"))
                .and_then(|v| v.as_u64())
        });

    let turn_usage = state.and_then(|s| s.turn_usage.as_ref());
    let input_tokens = turn_usage
        .and_then(|u| u.get("input_tokens"))
        .and_then(|v| v.as_u64());
    let output_tokens = turn_usage
        .and_then(|u| u.get("output_tokens"))
        .and_then(|v| v.as_u64());
    let reasoning_tokens = turn_usage
        .and_then(|u| u.get("reasoning_tokens"))
        .and_then(|v| v.as_u64());
    let total_tokens = turn_usage
        .and_then(|u| u.get("total_tokens"))
        .and_then(|v| v.as_u64());
    let context_tokens = state.and_then(|s| s.context_tokens);

    let owned_paths: Vec<String> = state
        .and_then(|s| s.acceptance_contract.as_ref())
        .and_then(|c| c.get("owned_paths"))
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.as_str().map(single_line))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();

    let delta = state.and_then(|s| s.workspace_delta.as_ref());
    let delta_added = delta
        .and_then(|d| d.get("added"))
        .and_then(|v| v.as_u64())
        .map(|n| n as usize);
    let delta_deleted = delta
        .and_then(|d| d.get("deleted"))
        .and_then(|v| v.as_u64())
        .map(|n| n as usize);
    let delta_untracked = delta
        .and_then(|d| d.get("untracked"))
        .and_then(|v| v.as_u64())
        .map(|n| n as usize);
    let diff_stat = delta
        .and_then(|d| d.get("diff_stat"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(str::to_string);

    let watched_by: Vec<String> = live_watchers
        .iter()
        .filter(|w| w.covers(id, parent_session.as_deref()))
        .map(WatcherRecord::label)
        .collect();

    let events_sig = fs::metadata(job_dir.join("events.jsonl"))
        .ok()
        .map(|m| (m.len(), m.mtime()));

    let turn_prompt_file = state
        .and_then(|s| s.turn_prompt_file.as_deref())
        .map(single_line)
        .filter(|s| !s.is_empty());

    let has_result_file = fs::metadata(job_dir.join("result.txt"))
        .map(|m| m.is_file() && m.len() > 0)
        .unwrap_or(false);

    WorkerRecord {
        id: id.to_string(),
        alias,
        label,
        engine,
        provider,
        model,
        reasoning_effort,
        workspace,
        actual_workspace,
        retained_worktree,
        has_managed_worktree,
        worktree_branch: wt_entry.and_then(|w| w.branch.clone()),
        worktree_base_commit: wt_entry.and_then(|w| w.base_commit.clone()),
        reusable,
        parent_session,
        mission,
        batch,
        parked,
        close_requested,
        queue_depth,
        turn,
        raw_status,
        effective_status,
        liveness_lost,
        phase,
        step_count: sp
            .and_then(|s| s.step_count)
            .or_else(|| state.and_then(|s| s.event_steps))
            .unwrap_or(0),
        model_steps: sp
            .and_then(|s| s.model_steps)
            .or_else(|| state.and_then(|s| s.steps))
            .unwrap_or(0),
        started_at,
        ended_at,
        turn_started_at,
        last_activity,
        elapsed_s,
        turn_elapsed_s,
        last_event_age_s,
        active_tools,
        last_tool,
        latest_intent,
        activity,
        last_check,
        prompt_file,
        turn_prompt_file,
        prompt_preview,
        prompt_excerpt,
        has_result_file,
        task_failures: sp
            .and_then(|s| s.task_failures)
            .or_else(|| state.and_then(|s| s.turn_task_failures.or(s.task_failures)))
            .unwrap_or(0),
        needs_parent,
        ask_eligible,
        failure_kind: liveness_failure_kind.or_else(|| {
            state
                .and_then(|s| s.failure_kind.as_deref())
                .or_else(|| sp.and_then(|s| s.failure_kind.as_deref()))
                .map(single_line)
        }),
        error: liveness_err.or_else(|| {
            state
                .and_then(|s| s.error.as_deref())
                .or_else(|| sp.and_then(|s| s.error.as_deref()))
                .map(single_line)
        }),
        stderr_hint,
        exit_code: state
            .and_then(|s| s.exit_code)
            .or_else(|| sp.and_then(|s| s.exit_code)),
        changed_files,
        changed_files_total,
        changed_files_truncated,
        owned_paths,
        delta_added,
        delta_deleted,
        delta_untracked,
        diff_stat,
        verification_status,
        acceptance_status,
        report_state: sp
            .and_then(|s| s.report_state.as_deref())
            .or_else(|| state.and_then(|s| s.report_state.as_deref()))
            .map(single_line),
        report_summary,
        report_risks,
        native_cache_read_tokens,
        input_tokens,
        output_tokens,
        reasoning_tokens,
        total_tokens,
        context_tokens,
        watched_by,
        status_projection_stale,
        events_sig,
        job_dir: job_dir.to_path_buf(),
    }
}

/// Background `inotify` watcher + coalesced snapshot producer.
pub struct StoreWatcher {
    rx: mpsc::Receiver<RegistrySnapshot>,
    refresh_tx: mpsc::SyncSender<()>,
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<()>>,
}

impl StoreWatcher {
    pub fn spawn(root: PathBuf) -> Self {
        let (snap_tx, snap_rx) = mpsc::sync_channel::<RegistrySnapshot>(2);
        let (refresh_tx, refresh_rx) = mpsc::sync_channel::<()>(2);
        let stop = Arc::new(AtomicBool::new(false));
        let stop_flag = Arc::clone(&stop);

        let handle = thread::Builder::new()
            .name("claive-store".to_string())
            .spawn(move || run_watcher_loop(root, snap_tx, refresh_rx, stop_flag))
            .expect("spawn claive-store thread");

        Self {
            rx: snap_rx,
            refresh_tx,
            stop,
            handle: Some(handle),
        }
    }

    pub fn try_recv_latest(&self) -> Option<RegistrySnapshot> {
        let mut latest = None;
        while let Ok(snap) = self.rx.try_recv() {
            latest = Some(snap);
        }
        latest
    }

    /// Block briefly for the first snapshot so Frame 1 never flashes an empty registry.
    pub fn recv_initial(&self, timeout: Duration) -> Option<RegistrySnapshot> {
        let first = self.rx.recv_timeout(timeout).ok()?;
        let mut latest = first;
        while let Ok(snap) = self.rx.try_recv() {
            latest = snap;
        }
        Some(latest)
    }

    pub fn request_refresh(&self) {
        let _ = self.refresh_tx.try_send(());
    }
}

impl Drop for StoreWatcher {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = self.refresh_tx.try_send(());
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

fn watch_dirs(
    watcher: &mut Option<RecommendedWatcher>,
    watched: &mut HashSet<PathBuf>,
    root: &Path,
) {
    let Some(w) = watcher.as_mut() else {
        return;
    };
    let mut targets = vec![
        root.to_path_buf(),
        root.join("watchers"),
        root.join("notifications"),
    ];
    if let Ok(rd) = fs::read_dir(root) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .map(is_worker_id)
                    .unwrap_or(false)
            {
                targets.push(p);
            }
        }
    }
    for dir in targets {
        if dir.is_dir() && !watched.contains(&dir) && w.watch(&dir, RecursiveMode::NonRecursive).is_ok() {
            watched.insert(dir);
        }
    }
}

fn run_watcher_loop(
    root: PathBuf,
    snap_tx: mpsc::SyncSender<RegistrySnapshot>,
    refresh_rx: mpsc::Receiver<()>,
    stop: Arc<AtomicBool>,
) {
    let (fs_tx, fs_rx) = mpsc::sync_channel::<()>(16);
    let mut watcher = RecommendedWatcher::new(
        move |res: notify::Result<Event>| {
            if res.is_ok() {
                let _ = fs_tx.try_send(());
            } else {
                // Overflow or error: still trigger a reconciliation pass.
                let _ = fs_tx.try_send(());
            }
        },
        NotifyConfig::default(),
    )
    .ok();

    let mut watched_dirs = HashSet::new();
    // Watch parent directories BEFORE initial scan to close startup rename races.
    watch_dirs(&mut watcher, &mut watched_dirs, &root);

    let mut reader = RegistryReader::new(root.clone());
    let initial = reader.scan(now_unix_secs());
    let _ = snap_tx.try_send(initial);

    let mut last_scan = Instant::now();
    let mut dirty_since: Option<Instant> = None;
    let mut pending_force = false;

    while !stop.load(Ordering::Relaxed) {
        let mut force_now = pending_force;
        pending_force = false;
        if refresh_rx.try_recv().is_ok() {
            while refresh_rx.try_recv().is_ok() {}
            force_now = true;
        }

        if fs_rx.try_recv().is_ok() {
            while fs_rx.try_recv().is_ok() {}
            dirty_since.get_or_insert_with(Instant::now);
        }

        let coalesce_ready = dirty_since
            .map(|since| since.elapsed() >= COALESCE_WINDOW)
            .unwrap_or(false);
        let periodic_due = last_scan.elapsed() >= RECONCILE_INTERVAL;

        if force_now || coalesce_ready || periodic_due {
            watch_dirs(&mut watcher, &mut watched_dirs, &root);
            let snap = reader.scan(now_unix_secs());
            let _ = snap_tx.try_send(snap);
            last_scan = Instant::now();
            dirty_since = None;
        }

        // Sleep briefly so stop/refresh stays snappy while idle CPU stays near 0%.
        let wait_ms = if dirty_since.is_some() { 30 } else { 100 };
        if refresh_rx.recv_timeout(Duration::from_millis(wait_ms)).is_ok() {
            pending_force = true;
        }
    }
}
