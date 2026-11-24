//! Dashboard application state, priority triage sorting, filtering, and key handling.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::widgets::TableState;

use crate::commands::{
    make_request_id, ApplyDryRunResult, LogKind, LogResult, MutationEvent, MutationKind,
    PeekResult, ReadLane, ReadResponse, SendMode,
};
use crate::format::{format_step, format_structured_action, is_noise_step, FormattedStep, StepCategory};
use crate::sanitize::single_line;
use crate::store::{
    detect_env_session, now_unix_secs, InboxItem, ReceiptItem, RegistrySnapshot, WorkerRecord,
};

pub const QUIET_THRESHOLD_SECS: f64 = 60.0;
pub const RECENT_SETTLED_LIMIT: usize = 20;
pub const TOAST_TTL: Duration = Duration::from_secs(4);

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub claive_dir: PathBuf,
    pub claive_bin: String,
    pub compact: bool,
    pub triage: bool,
    pub all_sessions: bool,
    pub session: Option<String>,
    pub workspace: Option<String>,
    pub focus: Option<String>,
    pub actions: bool,
    pub read_only: bool,
}

impl AppConfig {
    pub fn actions_enabled(&self) -> bool {
        self.actions && !self.read_only && !self.compact
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum TriageBadge {
    Ask,
    Unwatched,
    WaitInTurn,
    Lost,
    Fail,
    Review,
    Quiet,
    Run,
    Parked,
    Idle,
    Done,
    Stop,
}

impl TriageBadge {
    pub fn rank(self) -> u8 {
        match self {
            TriageBadge::Ask => 0,
            TriageBadge::Unwatched => 1,
            TriageBadge::WaitInTurn => 1,
            TriageBadge::Lost => 2,
            TriageBadge::Fail => 3,
            TriageBadge::Review => 4,
            TriageBadge::Quiet => 5,
            TriageBadge::Run => 6,
            TriageBadge::Parked => 7,
            TriageBadge::Idle => 8,
            TriageBadge::Done => 9,
            TriageBadge::Stop => 10,
        }
    }

    pub fn short_label(self) -> &'static str {
        match self {
            TriageBadge::Ask => "ASK",
            TriageBadge::Unwatched => "UNWATCHED",
            TriageBadge::WaitInTurn => "WAIT-TURN",
            TriageBadge::Lost => "LOST",
            TriageBadge::Fail => "FAIL",
            TriageBadge::Review => "REVIEW",
            TriageBadge::Quiet => "QUIET",
            TriageBadge::Run => "RUN",
            TriageBadge::Parked => "PARKED",
            TriageBadge::Idle => "IDLE",
            TriageBadge::Done => "DONE",
            TriageBadge::Stop => "STOP",
        }
    }

    pub fn icon(self) -> &'static str {
        match self {
            TriageBadge::Ask => "●",
            TriageBadge::Unwatched | TriageBadge::WaitInTurn => "▲",
            TriageBadge::Lost | TriageBadge::Fail => "✖",
            TriageBadge::Review => "★",
            TriageBadge::Quiet => "◷",
            TriageBadge::Run => "▸",
            TriageBadge::Parked => "⏸",
            TriageBadge::Idle => "○",
            TriageBadge::Done => "✓",
            TriageBadge::Stop => "■",
        }
    }

    pub fn is_actionable(self) -> bool {
        matches!(
            self,
            TriageBadge::Ask
                | TriageBadge::Unwatched
                | TriageBadge::WaitInTurn
                | TriageBadge::Lost
                | TriageBadge::Fail
                | TriageBadge::Review
        )
    }

    pub fn is_oldest_first(self) -> bool {
        matches!(
            self,
            TriageBadge::Ask
                | TriageBadge::Unwatched
                | TriageBadge::WaitInTurn
                | TriageBadge::Lost
        )
    }

    pub fn is_anomalous(self) -> bool {
        self.is_actionable() || self == TriageBadge::Quiet
    }
}

/// Compute the highest-priority triage badge for a worker.
/// Mirrors co-stimulated `QUIET` detection (exempts active local tools).
pub fn classify_worker(w: &WorkerRecord) -> TriageBadge {
    if w.ask_eligible {
        return TriageBadge::Ask;
    }
    if w.is_active_turn() && !w.parked {
        if w.is_codex_parent() {
            return TriageBadge::WaitInTurn;
        }
        if w.watched_by.is_empty() {
            return TriageBadge::Unwatched;
        }
    }
    if w.liveness_lost || w.effective_status == "interrupted" {
        return TriageBadge::Lost;
    }
    if w.effective_status == "failed" {
        return TriageBadge::Fail;
    }
    if is_review_pending(w) {
        return TriageBadge::Review;
    }
    if w.is_active_turn() {
        if w.parked {
            return TriageBadge::Parked;
        }
        let silent = w.active_tools.is_empty();
        let age = w.last_event_age_s.unwrap_or(0.0);
        if silent && age >= QUIET_THRESHOLD_SECS {
            return TriageBadge::Quiet;
        }
        return TriageBadge::Run;
    }
    if w.parked {
        return TriageBadge::Parked;
    }
    match w.effective_status.as_str() {
        "idle" => TriageBadge::Idle,
        "completed" => TriageBadge::Done,
        "cancelled" => TriageBadge::Stop,
        _ => TriageBadge::Stop,
    }
}

pub fn is_review_pending(w: &WorkerRecord) -> bool {
    w.has_managed_worktree
        && w.retained_worktree.is_some()
        && matches!(w.effective_status.as_str(), "completed" | "idle")
        && w.acceptance_status == "pending"
}

pub fn is_settled_status(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "interrupted" | "cancelled")
}

pub fn worker_sort_key(w: &WorkerRecord) -> (u8, i64, String) {
    let badge = classify_worker(w);
    let rank = badge.rank();
    let ts = if badge.is_oldest_first() {
        let anchor = w
            .turn_started_at
            .or(w.started_at)
            .or(w.last_activity)
            .unwrap_or(0.0);
        (anchor * 1000.0) as i64
    } else {
        let anchor = w
            .last_activity
            .or(w.ended_at)
            .or(w.started_at)
            .unwrap_or(0.0);
        -((anchor * 1000.0) as i64)
    };
    (rank, ts, w.id.clone())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SessionScope {
    All,
    Specific(String),
    Untagged,
}

impl SessionScope {
    pub fn label(&self) -> String {
        match self {
            SessionScope::All => "all".to_string(),
            SessionScope::Specific(s) => s.clone(),
            SessionScope::Untagged => "untagged".to_string(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkspaceScope {
    All,
    Specific(String),
}

impl WorkspaceScope {
    pub fn label(&self) -> String {
        match self {
            WorkspaceScope::All => "all".to_string(),
            WorkspaceScope::Specific(w) => short_workspace(w),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListMode {
    Recent,
    Active,
}

impl ListMode {
    pub fn label(self) -> &'static str {
        match self {
            ListMode::Recent => "recent",
            ListMode::Active => "active",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FocusPane {
    Workers,
    Inspector,
    Drawer,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InspectorTab {
    Overview,
    Evidence,
    Steps,
    Logs,
}

impl InspectorTab {
    pub const ALL: [InspectorTab; 4] = [
        InspectorTab::Overview,
        InspectorTab::Evidence,
        InspectorTab::Steps,
        InspectorTab::Logs,
    ];

    pub fn label(self) -> &'static str {
        match self {
            InspectorTab::Overview => "1 Briefing",
            InspectorTab::Evidence => "2 Evidence",
            InspectorTab::Steps => "3 Activity",
            InspectorTab::Logs => "4 Summary/Logs",
        }
    }

    pub fn next(self) -> Self {
        match self {
            InspectorTab::Overview => InspectorTab::Evidence,
            InspectorTab::Evidence => InspectorTab::Steps,
            InspectorTab::Steps => InspectorTab::Logs,
            InspectorTab::Logs => InspectorTab::Overview,
        }
    }

    pub fn prev(self) -> Self {
        match self {
            InspectorTab::Overview => InspectorTab::Logs,
            InspectorTab::Evidence => InspectorTab::Overview,
            InspectorTab::Steps => InspectorTab::Evidence,
            InspectorTab::Logs => InspectorTab::Steps,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActivityFilter {
    Milestones,
    Actions,
    All,
}

impl ActivityFilter {
    pub fn label(self) -> &'static str {
        match self {
            ActivityFilter::Milestones => "Milestones",
            ActivityFilter::Actions => "Actions",
            ActivityFilter::All => "All",
        }
    }

    pub fn next(self) -> Self {
        match self {
            ActivityFilter::Actions => ActivityFilter::Milestones,
            ActivityFilter::Milestones => ActivityFilter::All,
            ActivityFilter::All => ActivityFilter::Actions,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrawerTab {
    Inbox,
    Receipts,
}

impl DrawerTab {
    pub fn toggle(self) -> Self {
        match self {
            DrawerTab::Inbox => DrawerTab::Receipts,
            DrawerTab::Receipts => DrawerTab::Inbox,
        }
    }
}

#[derive(Debug, Clone)]
pub enum DisplayRow {
    GroupHeader {
        session: String,
        summary: String,
    },
    Worker {
        worker_idx: usize,
        badge: TriageBadge,
        unseen: bool,
    },
}

#[derive(Debug, Clone, Default)]
pub struct SummaryCounts {
    pub total: usize,
    pub ask: usize,
    pub unwatched: usize,
    pub wait_in_turn: usize,
    pub lost: usize,
    pub fail: usize,
    pub review: usize,
    pub quiet: usize,
    pub running: usize,
    pub parked: usize,
    pub idle: usize,
    pub done: usize,
    pub hidden_ask: usize,
}

#[derive(Debug, Clone)]
pub struct Toast {
    pub text: String,
    pub is_error: bool,
    pub expires_at: Instant,
}

#[derive(Debug, Clone)]
pub enum ComposerKind {
    Answer {
        expected_turn: u64,
        expected_question_hash: Option<String>,
        question_preview: String,
        is_goal: bool,
    },
    Send {
        mode: SendMode,
    },
    Steer,
}

#[derive(Debug, Clone)]
pub struct ComposerModal {
    pub kind: ComposerKind,
    pub worker_id: String,
    pub worker_display: String,
    pub watched_by: Vec<String>,
    pub hijack_confirmed: bool,
    pub buffer: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfirmKind {
    Cancel,
    Close,
}

#[derive(Debug, Clone)]
pub struct ConfirmModal {
    pub kind: ConfirmKind,
    pub worker_id: String,
    pub worker_display: String,
    pub watched_by: Vec<String>,
    pub hijack_confirmed: bool,
}

#[derive(Debug, Clone)]
pub struct ApplyPreviewModal {
    pub worker_id: String,
    pub worker_display: String,
    pub watched_by: Vec<String>,
    pub hijack_confirmed: bool,
    pub close_after: bool,
    pub loading: bool,
    pub result: Option<ApplyDryRunResult>,
}

#[derive(Debug, Clone)]
pub enum ActiveModal {
    Composer(ComposerModal),
    Confirm(ConfirmModal),
    ApplyPreview(ApplyPreviewModal),
}

#[derive(Debug, Clone)]
pub struct InFlightMutation {
    pub label: String,
    pub started_at: Instant,
    pub slow_warning: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeyOutcome {
    None,
    Quit,
    RequestRefresh,
    CopyClipboard(String),
    RequestApplyDryRun(String),
    SubmitMutation(MutationKind),
}

pub struct App {
    pub config: AppConfig,
    pub snapshot: RegistrySnapshot,
    pub session_scope: SessionScope,
    pub workspace_scope: WorkspaceScope,
    pub list_mode: ListMode,
    pub triage_only: bool,
    pub group_by_session: bool,
    pub search_active: bool,
    pub search_query: String,

    pub focus_pane: FocusPane,
    pub inspector_tab: InspectorTab,
    pub drawer_open: bool,
    pub drawer_tab: DrawerTab,
    pub detail_open: bool,
    pub help_open: bool,
    pub log_kind: LogKind,

    pub inspector_scroll: u16,
    pub drawer_scroll: u16,

    // Activity progressive-disclosure state.
    pub selected_step: usize,
    pub expanded_steps: HashSet<u64>,
    pub show_noise_steps: bool,
    pub activity_filter: ActivityFilter,

    pub selected_id: Option<String>,
    pub table_state: TableState,
    pub visible_workers: Vec<usize>,
    pub display_rows: Vec<DisplayRow>,
    pub counts: SummaryCounts,

    // Unseen-settlement tracking (`worker_id -> (turn, status)`).
    initialized_seen: bool,
    seen_settlements: HashMap<String, (u64, String)>,
    unseen_workers: HashSet<String>,

    // Pending one-shot `--focus` resolution.
    pending_focus: Option<String>,

    // Cached off-thread read outputs.
    pub peek_cache: HashMap<String, PeekResult>,
    pub peek_loading: HashSet<String>,
    pub log_cache: HashMap<(String, LogKind), LogResult>,

    // Phase 2 & 3 interactive modals and single-flight mutation tracker.
    pub modal: Option<ActiveModal>,
    pub in_flight: Option<InFlightMutation>,
    pub spinner_tick: usize,

    pub toast: Option<Toast>,
}

impl App {
    pub fn new(config: AppConfig) -> Self {
        let initial_session = if let Some(s) = &config.session {
            SessionScope::Specific(s.clone())
        } else if config.all_sessions {
            SessionScope::All
        } else if let Some(env_s) = detect_env_session() {
            SessionScope::Specific(env_s)
        } else {
            SessionScope::All
        };

        let initial_workspace = match &config.workspace {
            Some(w) if !w.trim().is_empty() => WorkspaceScope::Specific(w.clone()),
            _ => WorkspaceScope::All,
        };

        let pending_focus = config.focus.clone();
        let triage_only = config.triage;
        let claive_dir = config.claive_dir.clone();

        Self {
            config,
            snapshot: RegistrySnapshot {
                workers: Vec::new(),
                live_watchers: Vec::new(),
                inbox: Vec::new(),
                receipts: Vec::new(),
                refreshed_at: now_unix_secs(),
                stale_reason: None,
                claive_dir,
            },
            session_scope: initial_session,
            workspace_scope: initial_workspace,
            list_mode: ListMode::Recent,
            triage_only,
            group_by_session: false,
            search_active: false,
            search_query: String::new(),
            focus_pane: FocusPane::Workers,
            inspector_tab: InspectorTab::Overview,
            drawer_open: false,
            drawer_tab: DrawerTab::Inbox,
            detail_open: false,
            help_open: false,
            log_kind: LogKind::Result,
            inspector_scroll: 0,
            drawer_scroll: 0,
            selected_step: 0,
            expanded_steps: HashSet::new(),
            show_noise_steps: false,
            activity_filter: ActivityFilter::Actions,
            selected_id: None,
            table_state: TableState::default(),
            visible_workers: Vec::new(),
            display_rows: Vec::new(),
            counts: SummaryCounts::default(),
            initialized_seen: false,
            seen_settlements: HashMap::new(),
            unseen_workers: HashSet::new(),
            pending_focus,
            peek_cache: HashMap::new(),
            peek_loading: HashSet::new(),
            log_cache: HashMap::new(),
            modal: None,
            in_flight: None,
            spinner_tick: 0,
            toast: None,
        }
    }

    /// Apply a freshly ingested registry snapshot while preserving selection by canonical ID.
    pub fn apply_snapshot(&mut self, mut snapshot: RegistrySnapshot) {
        snapshot.workers.sort_by_key(worker_sort_key);

        // Update unseen-settlement markers.
        if !self.initialized_seen {
            for w in &snapshot.workers {
                if is_settled_status(&w.effective_status) {
                    self.seen_settlements
                        .insert(w.id.clone(), (w.turn, w.effective_status.clone()));
                }
            }
            self.initialized_seen = true;
        } else {
            for w in &snapshot.workers {
                if is_settled_status(&w.effective_status) {
                    let key = (w.turn, w.effective_status.clone());
                    let already_seen = self
                        .seen_settlements
                        .get(&w.id)
                        .map(|prev| prev == &key)
                        .unwrap_or(false);
                    if !already_seen {
                        self.unseen_workers.insert(w.id.clone());
                    }
                } else {
                    self.unseen_workers.remove(&w.id);
                }
            }
        }

        // If env auto-scoped to a session that has zero workers and user didn't pass `--session`,
        // fall back cleanly to `All` on initial load if there ARE workers in other sessions.
        if self.config.session.is_none()
            && !self.config.all_sessions
            && !snapshot.workers.is_empty()
            && let SessionScope::Specific(ref s) = self.session_scope
        {
            let any_match = snapshot
                .workers
                .iter()
                .any(|w| w.parent_session.as_deref() == Some(s.as_str()));
            if !any_match && self.selected_id.is_none() {
                self.session_scope = SessionScope::All;
            }
        }

        self.snapshot = snapshot;

        // Resolve pending `--focus` target once matching worker appears.
        if let Some(target) = self.pending_focus.clone() {
            let clean_target = target.trim().trim_start_matches('@');
            if let Some(found) = self
                .snapshot
                .workers
                .iter()
                .find(|w| {
                    w.id == clean_target
                        || w.id.starts_with(clean_target)
                        || w.alias.as_deref() == Some(clean_target)
                })
                .cloned()
            {
                let badge = classify_worker(&found);
                self.selected_id = Some(found.id.clone());
                self.pending_focus = None;
                // Ensure filters do not hide the explicitly focused worker.
                if !self.matches_scope(&found) {
                    self.session_scope = SessionScope::All;
                    self.workspace_scope = WorkspaceScope::All;
                    self.search_query.clear();
                }
                if self.triage_only && !badge.is_actionable() {
                    self.triage_only = false;
                }
                if self.list_mode == ListMode::Active
                    && !(badge.is_actionable() || found.is_active_turn())
                {
                    self.list_mode = ListMode::Recent;
                }
            }
        }

        self.recompute_view();
    }

    pub fn apply_read_response(&mut self, resp: ReadResponse) {
        match resp {
            ReadResponse::Peek(res) => {
                self.peek_loading.remove(&res.worker_id);
                self.peek_cache.insert(res.worker_id.clone(), res);
            }
            ReadResponse::Log(res) => {
                self.log_cache
                    .insert((res.worker_id.clone(), res.kind), res);
            }
            ReadResponse::ApplyDryRun(res) => {
                if let Some(ActiveModal::ApplyPreview(ref mut preview)) = self.modal
                    && preview.worker_id == res.worker_id
                {
                    preview.loading = false;
                    preview.result = Some(res);
                }
            }
        }
    }

    pub fn mark_mutation_started(&mut self, kind: &MutationKind) {
        self.in_flight = Some(InFlightMutation {
            label: kind.summary_label(),
            started_at: Instant::now(),
            slow_warning: None,
        });
    }

    pub fn apply_mutation_event(&mut self, ev: MutationEvent) -> bool {
        match ev {
            MutationEvent::SlowWarning {
                label,
                elapsed_secs,
            } => {
                let msg = format!("{label} still running ({elapsed_secs}s); busy gate held");
                if let Some(ref mut flight) = self.in_flight {
                    flight.slow_warning = Some(msg.clone());
                }
                self.set_toast(msg, true);
                false
            }
            MutationEvent::Finished {
                label, ok, message, ..
            } => {
                self.in_flight = None;
                let toast_msg = if message.is_empty() {
                    if ok {
                        format!("{label}: ok")
                    } else {
                        format!("{label}: failed")
                    }
                } else {
                    message
                };
                self.set_toast(toast_msg, !ok);
                true
            }
        }
    }

    pub fn tick_spinner(&mut self) {
        if self.in_flight.is_some() {
            self.spinner_tick = self.spinner_tick.wrapping_add(1);
        }
    }

    pub fn selected_worker(&self) -> Option<&WorkerRecord> {
        let id = self.selected_id.as_deref()?;
        self.snapshot.workers.iter().find(|w| w.id == id)
    }

    pub fn is_unseen(&self, worker_id: &str) -> bool {
        self.unseen_workers.contains(worker_id)
    }

    pub fn mark_selected_seen(&mut self) {
        if let Some(w) = self.selected_worker().cloned()
            && is_settled_status(&w.effective_status)
        {
            self.seen_settlements
                .insert(w.id.clone(), (w.turn, w.effective_status));
            self.unseen_workers.remove(&w.id);
            self.recompute_view();
        }
    }

    pub fn set_toast(&mut self, text: impl Into<String>, is_error: bool) {
        self.toast = Some(Toast {
            text: single_line(&text.into()),
            is_error,
            expires_at: Instant::now() + TOAST_TTL,
        });
    }

    pub fn expire_toast(&mut self) {
        if let Some(t) = &self.toast
            && Instant::now() >= t.expires_at
        {
            self.toast = None;
        }
    }

    fn matches_scope(&self, w: &WorkerRecord) -> bool {
        match &self.session_scope {
            SessionScope::All => {}
            SessionScope::Specific(s) => {
                if w.parent_session.as_deref() != Some(s.as_str()) {
                    return false;
                }
            }
            SessionScope::Untagged => {
                if w.parent_session.is_some() {
                    return false;
                }
            }
        }

        if let WorkspaceScope::Specific(ws) = &self.workspace_scope
            && w.workspace.as_deref() != Some(ws.as_str())
        {
            return false;
        }

        if !self.search_query.is_empty() {
            let needle = self.search_query.to_lowercase();
            let hay = [
                w.id.as_str(),
                w.alias.as_deref().unwrap_or(""),
                w.label.as_str(),
                w.workspace.as_deref().unwrap_or(""),
                w.engine.as_str(),
                w.model.as_deref().unwrap_or(""),
                w.parent_session.as_deref().unwrap_or(""),
            ];
            if !hay.iter().any(|h| h.to_lowercase().contains(&needle)) {
                return false;
            }
        }

        true
    }

    pub fn recompute_view(&mut self) {
        let mut counts = SummaryCounts::default();
        let mut visible = Vec::new();
        let mut settled_kept = 0usize;

        for (idx, w) in self.snapshot.workers.iter().enumerate() {
            let badge = classify_worker(w);
            let in_scope = self.matches_scope(w);

            if !in_scope {
                if badge == TriageBadge::Ask {
                    counts.hidden_ask += 1;
                }
                continue;
            }

            counts.total += 1;
            match badge {
                TriageBadge::Ask => counts.ask += 1,
                TriageBadge::Unwatched => counts.unwatched += 1,
                TriageBadge::WaitInTurn => counts.wait_in_turn += 1,
                TriageBadge::Lost => counts.lost += 1,
                TriageBadge::Fail => counts.fail += 1,
                TriageBadge::Review => counts.review += 1,
                TriageBadge::Quiet => counts.quiet += 1,
                TriageBadge::Run => counts.running += 1,
                TriageBadge::Parked => counts.parked += 1,
                TriageBadge::Idle => counts.idle += 1,
                TriageBadge::Done | TriageBadge::Stop => counts.done += 1,
            }

            if self.triage_only && !badge.is_actionable() {
                continue;
            }

            let unresolved = badge.is_actionable() || w.is_active_turn();
            if self.list_mode == ListMode::Active && !unresolved {
                continue;
            }

            if !unresolved && is_settled_status(&w.effective_status) {
                let is_selected = self.selected_id.as_deref() == Some(w.id.as_str());
                if settled_kept >= RECENT_SETTLED_LIMIT && !is_selected {
                    continue;
                }
                settled_kept += 1;
            }

            visible.push(idx);
        }

        self.counts = counts;
        self.visible_workers = visible;

        // Build display rows (flat or grouped by parent_session).
        let mut rows = Vec::new();
        if self.group_by_session {
            let mut order: Vec<String> = Vec::new();
            let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
            for &idx in &self.visible_workers {
                let key = self.snapshot.workers[idx]
                    .parent_session
                    .clone()
                    .unwrap_or_else(|| "untagged".to_string());
                if !groups.contains_key(&key) {
                    order.push(key.clone());
                }
                groups.entry(key).or_default().push(idx);
            }
            for sess in order {
                let idxs = &groups[&sess];
                let total = idxs.len();
                let mut done_n = 0;
                let mut ask_n = 0;
                let mut risk_n = 0;
                let mut run_n = 0;
                for &i in idxs {
                    let b = classify_worker(&self.snapshot.workers[i]);
                    match b {
                        TriageBadge::Ask => ask_n += 1,
                        TriageBadge::Unwatched
                        | TriageBadge::WaitInTurn
                        | TriageBadge::Lost
                        | TriageBadge::Fail => risk_n += 1,
                        TriageBadge::Done | TriageBadge::Review => done_n += 1,
                        TriageBadge::Run | TriageBadge::Quiet => run_n += 1,
                        _ => {}
                    }
                }
                let mut parts = vec![format!("{done_n}/{total} done")];
                if run_n > 0 {
                    parts.push(format!("{run_n} run"));
                }
                if ask_n > 0 {
                    parts.push(format!("{ask_n} ASK"));
                }
                if risk_n > 0 {
                    parts.push(format!("{risk_n} RISK"));
                }
                rows.push(DisplayRow::GroupHeader {
                    session: sess,
                    summary: parts.join(" · "),
                });
                for &i in idxs {
                    let w = &self.snapshot.workers[i];
                    rows.push(DisplayRow::Worker {
                        worker_idx: i,
                        badge: classify_worker(w),
                        unseen: self.unseen_workers.contains(&w.id),
                    });
                }
            }
        } else {
            for &i in &self.visible_workers {
                let w = &self.snapshot.workers[i];
                rows.push(DisplayRow::Worker {
                    worker_idx: i,
                    badge: classify_worker(w),
                    unseen: self.unseen_workers.contains(&w.id),
                });
            }
        }

        self.display_rows = rows;
        self.sync_selection();
    }

    fn sync_selection(&mut self) {
        if self.visible_workers.is_empty() {
            self.selected_id = None;
            self.table_state.select(None);
            return;
        }

        // Preserve existing selection by full canonical ID if still visible.
        if let Some(ref cur_id) = self.selected_id
            && let Some(row_pos) = self.display_rows.iter().position(|r| match r {
                DisplayRow::Worker { worker_idx, .. } => {
                    self.snapshot.workers[*worker_idx].id == *cur_id
                }
                DisplayRow::GroupHeader { .. } => false,
            })
        {
            self.table_state.select(Some(row_pos));
            return;
        }

        // Fall back to first visible worker row.
        if let Some((row_pos, worker_idx)) = self.display_rows.iter().enumerate().find_map(|(pos, r)| {
            match r {
                DisplayRow::Worker { worker_idx, .. } => Some((pos, *worker_idx)),
                DisplayRow::GroupHeader { .. } => None,
            }
        }) {
            self.selected_id = Some(self.snapshot.workers[worker_idx].id.clone());
            self.table_state.select(Some(row_pos));
        }
    }

    fn select_step(&mut self, delta: isize) {
        let worker_positions: Vec<(usize, usize)> = self
            .display_rows
            .iter()
            .enumerate()
            .filter_map(|(pos, r)| match r {
                DisplayRow::Worker { worker_idx, .. } => Some((pos, *worker_idx)),
                DisplayRow::GroupHeader { .. } => None,
            })
            .collect();
        if worker_positions.is_empty() {
            return;
        }
        let cur_row = self.table_state.selected().unwrap_or(0);
        let cur_slot = worker_positions
            .iter()
            .position(|(pos, _)| *pos == cur_row)
            .unwrap_or(0);
        let next_slot = if delta < 0 {
            cur_slot.saturating_sub((-delta) as usize)
        } else {
            (cur_slot + delta as usize).min(worker_positions.len() - 1)
        };
        let (row_pos, worker_idx) = worker_positions[next_slot];
        self.selected_id = Some(self.snapshot.workers[worker_idx].id.clone());
        self.table_state.select(Some(row_pos));
        self.inspector_scroll = 0;
        self.selected_step = 0;
        self.expanded_steps.clear();
    }

    fn select_first(&mut self) {
        let first = self.display_rows.iter().enumerate().find_map(|(pos, r)| match r {
            DisplayRow::Worker { worker_idx, .. } => Some((pos, *worker_idx)),
            DisplayRow::GroupHeader { .. } => None,
        });
        if let Some((row_pos, worker_idx)) = first {
            self.selected_id = Some(self.snapshot.workers[worker_idx].id.clone());
            self.table_state.select(Some(row_pos));
            self.inspector_scroll = 0;
            self.selected_step = 0;
            self.expanded_steps.clear();
        }
    }

    fn select_last(&mut self) {
        let last = self
            .display_rows
            .iter()
            .enumerate()
            .rev()
            .find_map(|(pos, r)| match r {
                DisplayRow::Worker { worker_idx, .. } => Some((pos, *worker_idx)),
                DisplayRow::GroupHeader { .. } => None,
            });
        if let Some((row_pos, worker_idx)) = last {
            self.selected_id = Some(self.snapshot.workers[worker_idx].id.clone());
            self.table_state.select(Some(row_pos));
            self.inspector_scroll = 0;
            self.selected_step = 0;
            self.expanded_steps.clear();
        }
    }

    fn cycle_session_scope(&mut self) {
        let mut sessions: BTreeSet<String> = BTreeSet::new();
        let mut has_untagged = false;
        for w in &self.snapshot.workers {
            if let Some(s) = &w.parent_session {
                sessions.insert(s.clone());
            } else {
                has_untagged = true;
            }
        }
        let mut choices = vec![SessionScope::All];
        for s in sessions {
            choices.push(SessionScope::Specific(s));
        }
        if has_untagged {
            choices.push(SessionScope::Untagged);
        }
        let cur = choices
            .iter()
            .position(|c| c == &self.session_scope)
            .unwrap_or(0);
        self.session_scope = choices[(cur + 1) % choices.len()].clone();
        self.recompute_view();
    }

    fn cycle_workspace_scope(&mut self) {
        let mut workspaces: BTreeSet<String> = BTreeSet::new();
        for w in &self.snapshot.workers {
            if let Some(ws) = &w.workspace {
                workspaces.insert(ws.clone());
            }
        }
        let mut choices = vec![WorkspaceScope::All];
        for ws in workspaces {
            choices.push(WorkspaceScope::Specific(ws));
        }
        let cur = choices
            .iter()
            .position(|c| c == &self.workspace_scope)
            .unwrap_or(0);
        self.workspace_scope = choices[(cur + 1) % choices.len()].clone();
        self.recompute_view();
    }

    /// Context-aware remediation command for one-key (`y`) OSC 52 copy.
    pub fn remediation_command(&self, w: &WorkerRecord) -> String {
        let badge = classify_worker(w);
        match badge {
            TriageBadge::Ask => {
                if w.needs_parent
                    .as_ref()
                    .and_then(|n| n.kind.as_deref())
                    .map(|k| k.contains("goal"))
                    .unwrap_or(false)
                {
                    format!("claive goal answer {} --message \"\"", w.id)
                } else {
                    format!("claive answer {} --message \"\"", w.id)
                }
            }
            TriageBadge::Unwatched => {
                let mut ids: Vec<String> = self
                    .snapshot
                    .workers
                    .iter()
                    .filter(|cand| {
                        cand.parent_session == w.parent_session
                            && classify_worker(cand) == TriageBadge::Unwatched
                    })
                    .map(|cand| cand.id.clone())
                    .collect();
                if ids.is_empty() {
                    ids.push(w.id.clone());
                }
                let all_flag = if ids.len() > 1 { " --all" } else { "" };
                format!("claive wait {}{} --heartbeat 600", ids.join(" "), all_flag)
            }
            TriageBadge::WaitInTurn => format!("claive wait {} --timeout 300", w.id),
            TriageBadge::Review => {
                if w.verification_status == "passed" {
                    format!("claive apply {} --close", w.id)
                } else {
                    format!("claive verify {} && claive apply {} --close", w.id, w.id)
                }
            }
            TriageBadge::Quiet | TriageBadge::Run => format!("claive peek {}", w.id),
            TriageBadge::Lost | TriageBadge::Fail => format!("claive show {}", w.id),
            TriageBadge::Parked => format!("claive park {} --unpark", w.id),
            TriageBadge::Idle | TriageBadge::Done | TriageBadge::Stop => {
                format!("claive show {}", w.id)
            }
        }
    }

    /// Scoped inbox items matching active `session_scope`.
    pub fn scoped_inbox(&self) -> Vec<&InboxItem> {
        self.snapshot
            .inbox
            .iter()
            .rev()
            .filter(|item| match &self.session_scope {
                SessionScope::All => true,
                SessionScope::Specific(s) => item.parent_session.as_deref() == Some(s.as_str()),
                SessionScope::Untagged => item.parent_session.is_none(),
            })
            .collect()
    }

    /// Recent notification receipts (newest first; never joined by seq to inbox).
    pub fn recent_receipts(&self) -> Vec<&ReceiptItem> {
        self.snapshot.receipts.iter().rev().collect()
    }

    /// Trigger lazy off-thread reads (`peek --json --structured` or bounded log tail) for the active tab.
    pub fn pump_reads(&mut self, lane: &mut ReadLane, force: bool) {
        if self.config.compact {
            return;
        }
        let Some(w) = self.selected_worker().cloned() else {
            return;
        };
        match self.inspector_tab {
            InspectorTab::Overview | InspectorTab::Steps => {
                let queued = lane.maybe_request_peek(
                    &w.id,
                    &w.job_dir,
                    &w.engine,
                    w.actual_workspace.as_deref().or(w.workspace.as_deref()),
                    30,
                    &self.config.claive_bin,
                    &self.config.claive_dir,
                    w.events_sig,
                    force,
                );
                if queued {
                    self.peek_loading.insert(w.id);
                }
            }
            InspectorTab::Logs => {
                let need = force || !self.log_cache.contains_key(&(w.id.clone(), self.log_kind));
                if need {
                    lane.request_log(&w.id, &w.job_dir, self.log_kind, w.prompt_file.clone());
                }
            }
            InspectorTab::Evidence => {}
        }
    }

    /// Return formatted activity items for the selected worker, preferring paired `actions[]`.
    pub fn visible_steps_for(&self, worker_id: &str) -> (Vec<FormattedStep>, usize) {
        let Some(res) = self.peek_cache.get(worker_id) else {
            return (Vec::new(), 0);
        };
        let mut hidden = 0usize;
        let mut out = Vec::new();
        if !res.actions.is_empty() {
            for act in &res.actions {
                let formatted = format_structured_action(act);
                let keep = match self.activity_filter {
                    ActivityFilter::Milestones => matches!(
                        formatted.category,
                        StepCategory::Say
                            | StepCategory::Err
                            | StepCategory::Test
                            | StepCategory::Build
                            | StepCategory::Edit
                    ),
                    ActivityFilter::Actions => !formatted.is_noise,
                    ActivityFilter::All => true,
                };
                if !keep {
                    hidden += 1;
                    continue;
                }
                out.push(formatted);
            }
            return (out, hidden);
        }

        // Legacy fallback when CLI did not emit `actions[]`.
        for step in &res.steps {
            let noise = is_noise_step(step);
            if !self.show_noise_steps && self.activity_filter != ActivityFilter::All && noise {
                hidden += 1;
                continue;
            }
            let formatted = format_step(step);
            if self.activity_filter == ActivityFilter::Milestones
                && !matches!(
                    formatted.category,
                    StepCategory::Say
                        | StepCategory::Err
                        | StepCategory::Test
                        | StepCategory::Build
                        | StepCategory::Edit
                )
            {
                hidden += 1;
                continue;
            }
            out.push(formatted);
        }
        (out, hidden)
    }

    /// Return the most recent `max_items` high-signal milestones for the Briefing view.
    pub fn recent_milestones_for(&self, worker_id: &str, max_items: usize) -> Vec<FormattedStep> {
        let Some(res) = self.peek_cache.get(worker_id) else {
            return Vec::new();
        };
        let all: Vec<FormattedStep> = if !res.actions.is_empty() {
            res.actions.iter().map(format_structured_action).collect()
        } else {
            res.steps
                .iter()
                .filter(|s| !is_noise_step(s))
                .map(format_step)
                .collect()
        };
        let milestones: Vec<FormattedStep> = all
            .iter()
            .filter(|s| {
                matches!(
                    s.category,
                    StepCategory::Say
                        | StepCategory::Err
                        | StepCategory::Test
                        | StepCategory::Build
                        | StepCategory::Edit
                )
            })
            .cloned()
            .collect();
        let source = if milestones.is_empty() { all } else { milestones };
        let start = source.len().saturating_sub(max_items);
        source[start..].to_vec()
    }

    fn step_move(&mut self, delta: isize) {
        let Some(wid) = self.selected_id.clone() else {
            return;
        };
        let (steps, _) = self.visible_steps_for(&wid);
        if steps.is_empty() {
            self.selected_step = 0;
            return;
        }
        let max_idx = steps.len() - 1;
        self.selected_step = if delta < 0 {
            self.selected_step.saturating_sub((-delta) as usize)
        } else {
            (self.selected_step + delta as usize).min(max_idx)
        };
    }

    fn toggle_selected_step_expand(&mut self) {
        let Some(wid) = self.selected_id.clone() else {
            return;
        };
        let (steps, _) = self.visible_steps_for(&wid);
        if steps.is_empty() {
            return;
        }
        let idx = self.selected_step.min(steps.len() - 1);
        let step_num = steps[idx].index;
        if !self.expanded_steps.remove(&step_num) {
            self.expanded_steps.insert(step_num);
        }
    }

    fn toggle_all_steps_expand(&mut self) {
        let Some(wid) = self.selected_id.clone() else {
            return;
        };
        let (steps, _) = self.visible_steps_for(&wid);
        if steps.is_empty() {
            return;
        }
        if self.expanded_steps.is_empty() {
            for s in steps {
                self.expanded_steps.insert(s.index);
            }
            self.set_toast("Expanded all visible steps", false);
        } else {
            self.expanded_steps.clear();
            self.set_toast("Collapsed all steps", false);
        }
    }

    /// Handle keyboard input and return any external effect to perform.
    pub fn handle_key(&mut self, key: KeyEvent) -> KeyOutcome {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            return KeyOutcome::Quit;
        }

        // Compact pane is strictly passive: only quit or force-refresh.
        if self.config.compact {
            return match key.code {
                KeyCode::Char('q') => KeyOutcome::Quit,
                KeyCode::Char('R') | KeyCode::Char('r') => KeyOutcome::RequestRefresh,
                _ => KeyOutcome::None,
            };
        }

        // Help modal captures Esc/?/q.
        if self.help_open {
            match key.code {
                KeyCode::Esc | KeyCode::Char('?') | KeyCode::Char('q') => {
                    self.help_open = false;
                }
                _ => {}
            }
            return KeyOutcome::None;
        }

        // Active action modal captures all keyboard input.
        if self.modal.is_some() {
            return self.handle_modal_key(key);
        }

        // Inline search mode.
        if self.search_active {
            match key.code {
                KeyCode::Esc => {
                    self.search_active = false;
                    self.search_query.clear();
                    self.recompute_view();
                }
                KeyCode::Enter => {
                    self.search_active = false;
                }
                KeyCode::Backspace => {
                    self.search_query.pop();
                    self.recompute_view();
                }
                KeyCode::Char(c) if !c.is_control() => {
                    self.search_query.push(c);
                    self.recompute_view();
                }
                _ => {}
            }
            return KeyOutcome::None;
        }

        if key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) {
            return KeyOutcome::None;
        }

        match key.code {
            KeyCode::Char('q') => return KeyOutcome::Quit,
            KeyCode::Char('?') => {
                self.help_open = true;
            }
            KeyCode::Char('/') => {
                self.search_active = true;
            }
            KeyCode::Esc => {
                if !self.search_query.is_empty() {
                    self.search_query.clear();
                    self.recompute_view();
                } else if self.detail_open {
                    self.detail_open = false;
                    self.focus_pane = FocusPane::Workers;
                } else if self.drawer_open {
                    self.drawer_open = false;
                    self.focus_pane = FocusPane::Workers;
                }
            }
            KeyCode::Char('j') | KeyCode::Down => match self.focus_pane {
                FocusPane::Workers => self.select_step(1),
                FocusPane::Inspector => {
                    if self.inspector_tab == InspectorTab::Steps {
                        self.step_move(1);
                    } else {
                        self.inspector_scroll = self.inspector_scroll.saturating_add(1);
                    }
                }
                FocusPane::Drawer => {
                    self.drawer_scroll = self.drawer_scroll.saturating_add(1);
                }
            },
            KeyCode::Char('k') | KeyCode::Up => match self.focus_pane {
                FocusPane::Workers => self.select_step(-1),
                FocusPane::Inspector => {
                    if self.inspector_tab == InspectorTab::Steps {
                        self.step_move(-1);
                    } else {
                        self.inspector_scroll = self.inspector_scroll.saturating_sub(1);
                    }
                }
                FocusPane::Drawer => {
                    self.drawer_scroll = self.drawer_scroll.saturating_sub(1);
                }
            },
            KeyCode::Char(']') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.step_move(1);
                }
            }
            KeyCode::Char('[') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.step_move(-1);
                }
            }
            KeyCode::Char(' ') | KeyCode::Char('e') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.toggle_selected_step_expand();
                }
            }
            KeyCode::Char('E') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.toggle_all_steps_expand();
                }
            }
            KeyCode::Char('f') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.activity_filter = self.activity_filter.next();
                    self.show_noise_steps = self.activity_filter == ActivityFilter::All;
                    self.selected_step = 0;
                    self.set_toast(
                        format!("Activity filter: {}", self.activity_filter.label()),
                        false,
                    );
                }
            }
            KeyCode::Char('n') => {
                if self.inspector_tab == InspectorTab::Steps {
                    self.show_noise_steps = !self.show_noise_steps;
                    self.activity_filter = if self.show_noise_steps {
                        ActivityFilter::All
                    } else {
                        ActivityFilter::Actions
                    };
                    self.selected_step = 0;
                    self.set_toast(
                        if self.show_noise_steps {
                            "Showing all raw events"
                        } else {
                            "Showing structured actions"
                        },
                        false,
                    );
                }
            }
            KeyCode::Char('G') => {
                if self.focus_pane == FocusPane::Workers {
                    self.select_last();
                }
            }
            KeyCode::Char('g') => {
                if self.focus_pane == FocusPane::Workers {
                    // Use `G` for bottom, `Home` for top, `g` toggles wave/session grouping
                    // unless immediately followed by another `g`? Keep `g` crisp:
                    // `g` toggles session/wave grouping; `Home` / `End` jump top/bottom,
                    // and `0` jumps to top.
                    self.group_by_session = !self.group_by_session;
                    self.recompute_view();
                    self.set_toast(
                        if self.group_by_session {
                            "Grouped by parent session"
                        } else {
                            "Flat priority triage view"
                        },
                        false,
                    );
                }
            }
            KeyCode::Home => self.select_first(),
            KeyCode::End => self.select_last(),
            KeyCode::Tab => {
                self.focus_pane = match (self.focus_pane, self.drawer_open) {
                    (FocusPane::Workers, _) => FocusPane::Inspector,
                    (FocusPane::Inspector, true) => FocusPane::Drawer,
                    (FocusPane::Inspector, false) => FocusPane::Workers,
                    (FocusPane::Drawer, _) => FocusPane::Workers,
                };
            }
            KeyCode::BackTab => {
                self.focus_pane = match (self.focus_pane, self.drawer_open) {
                    (FocusPane::Workers, true) => FocusPane::Drawer,
                    (FocusPane::Workers, false) => FocusPane::Inspector,
                    (FocusPane::Inspector, _) => FocusPane::Workers,
                    (FocusPane::Drawer, _) => FocusPane::Inspector,
                };
            }
            KeyCode::Char('h') | KeyCode::Left => {
                if self.focus_pane == FocusPane::Drawer && self.drawer_open {
                    self.drawer_tab = self.drawer_tab.toggle();
                    self.drawer_scroll = 0;
                } else {
                    self.inspector_tab = self.inspector_tab.prev();
                    self.inspector_scroll = 0;
                }
            }
            KeyCode::Char('l') | KeyCode::Right => {
                if self.focus_pane == FocusPane::Drawer && self.drawer_open {
                    self.drawer_tab = self.drawer_tab.toggle();
                    self.drawer_scroll = 0;
                } else {
                    self.inspector_tab = self.inspector_tab.next();
                    self.inspector_scroll = 0;
                }
            }
            KeyCode::Char('1') => {
                self.inspector_tab = InspectorTab::Overview;
                self.inspector_scroll = 0;
            }
            KeyCode::Char('2') => {
                self.inspector_tab = InspectorTab::Evidence;
                self.inspector_scroll = 0;
            }
            KeyCode::Char('3') => {
                self.inspector_tab = InspectorTab::Steps;
                self.inspector_scroll = 0;
            }
            KeyCode::Char('4') => {
                self.inspector_tab = InspectorTab::Logs;
                self.inspector_scroll = 0;
            }
            KeyCode::Enter => {
                if self.focus_pane == FocusPane::Inspector
                    && self.inspector_tab == InspectorTab::Steps
                {
                    self.toggle_selected_step_expand();
                } else {
                    self.mark_selected_seen();
                    self.detail_open = true;
                    self.focus_pane = FocusPane::Inspector;
                }
            }
            KeyCode::Char('i') => {
                self.drawer_open = !self.drawer_open;
                if self.drawer_open {
                    self.focus_pane = FocusPane::Drawer;
                    self.drawer_scroll = 0;
                } else if self.focus_pane == FocusPane::Drawer {
                    self.focus_pane = FocusPane::Workers;
                }
            }
            KeyCode::Char('s') => {
                self.cycle_session_scope();
                self.set_toast(format!("Session scope: {}", self.session_scope.label()), false);
            }
            KeyCode::Char('w') => {
                self.cycle_workspace_scope();
                self.set_toast(
                    format!("Workspace scope: {}", self.workspace_scope.label()),
                    false,
                );
            }
            KeyCode::Char('r') => {
                self.list_mode = match self.list_mode {
                    ListMode::Recent => ListMode::Active,
                    ListMode::Active => ListMode::Recent,
                };
                self.recompute_view();
                self.set_toast(format!("List mode: {}", self.list_mode.label()), false);
            }
            KeyCode::Char('t') => {
                self.triage_only = !self.triage_only;
                self.recompute_view();
                self.set_toast(
                    if self.triage_only {
                        "Exception-only triage mode ON"
                    } else {
                        "Showing all scoped workers"
                    },
                    false,
                );
            }
            KeyCode::Char('L') => {
                self.log_kind = self.log_kind.next();
                self.inspector_tab = InspectorTab::Logs;
                self.inspector_scroll = 0;
                self.set_toast(format!("Log view: {}", self.log_kind.label()), false);
            }
            KeyCode::Char('R') => {
                self.set_toast("Refreshing...", false);
                return KeyOutcome::RequestRefresh;
            }
            KeyCode::Char('y') => {
                if let Some(w) = self.selected_worker().cloned() {
                    let cmd = self.remediation_command(&w);
                    self.set_toast(format!("Copied: {cmd}"), false);
                    return KeyOutcome::CopyClipboard(cmd);
                }
            }
            KeyCode::Char('a') => return self.open_answer_modal(),
            KeyCode::Char('m') => return self.open_send_modal(),
            KeyCode::Char('S') => return self.open_steer_modal(),
            KeyCode::Char('p') => return self.trigger_park_toggle(),
            KeyCode::Char('v') => return self.trigger_verify(),
            KeyCode::Char('x') => return self.open_confirm_modal(ConfirmKind::Cancel),
            KeyCode::Char('c') => return self.open_confirm_modal(ConfirmKind::Close),
            KeyCode::Char('A') => return self.open_apply_preview_modal(),
            _ => {}
        }

        KeyOutcome::None
    }

    fn require_actions_ready(&mut self) -> Option<WorkerRecord> {
        if !self.config.actions_enabled() {
            self.set_toast(
                "Read-only mode — launch with `claive dash --actions` (or press [y] to copy)",
                false,
            );
            return None;
        }
        if let Some(ref flight) = self.in_flight {
            self.set_toast(
                format!("Busy: {} still running (single-flight gate)", flight.label),
                true,
            );
            return None;
        }
        let Some(w) = self.selected_worker().cloned() else {
            self.set_toast("No worker selected", true);
            return None;
        };
        Some(w)
    }

    fn open_answer_modal(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if !w.ask_eligible || w.needs_parent.is_none() {
            self.set_toast("Selected worker has no open ASK waiting for answer", true);
            return KeyOutcome::None;
        }
        let ask = w.needs_parent.as_ref().unwrap();
        let is_goal = ask
            .kind
            .as_deref()
            .map(|k| k.contains("goal"))
            .unwrap_or(false);
        self.modal = Some(ActiveModal::Composer(ComposerModal {
            kind: ComposerKind::Answer {
                expected_turn: w.turn,
                expected_question_hash: ask.raw_question_sha256.clone(),
                question_preview: ask.question.clone(),
                is_goal,
            },
            worker_id: w.id.clone(),
            worker_display: w.display_name(),
            watched_by: w.watched_by.clone(),
            hijack_confirmed: w.watched_by.is_empty(),
            buffer: String::new(),
        }));
        KeyOutcome::None
    }

    fn open_send_modal(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if !w.reusable {
            self.set_toast("`claive send` requires a reusable worker", true);
            return KeyOutcome::None;
        }
        let default_mode = if w.is_active_turn() {
            SendMode::Now
        } else {
            SendMode::Wake
        };
        self.modal = Some(ActiveModal::Composer(ComposerModal {
            kind: ComposerKind::Send { mode: default_mode },
            worker_id: w.id.clone(),
            worker_display: w.display_name(),
            watched_by: w.watched_by.clone(),
            hijack_confirmed: w.watched_by.is_empty(),
            buffer: String::new(),
        }));
        KeyOutcome::None
    }

    fn open_steer_modal(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if !w.is_active_turn() {
            self.set_toast("`claive steer` requires a running worker (use [m] to send/wake)", true);
            return KeyOutcome::None;
        }
        self.modal = Some(ActiveModal::Composer(ComposerModal {
            kind: ComposerKind::Steer,
            worker_id: w.id.clone(),
            worker_display: w.display_name(),
            watched_by: w.watched_by.clone(),
            hijack_confirmed: w.watched_by.is_empty(),
            buffer: String::new(),
        }));
        KeyOutcome::None
    }

    fn trigger_park_toggle(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        KeyOutcome::SubmitMutation(MutationKind::Park {
            worker_id: w.id,
            unpark: w.parked,
        })
    }

    fn trigger_verify(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if w.is_active_turn() {
            self.set_toast("`claive verify` requires a settled turn", true);
            return KeyOutcome::None;
        }
        KeyOutcome::SubmitMutation(MutationKind::Verify { worker_id: w.id })
    }

    fn open_confirm_modal(&mut self, kind: ConfirmKind) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if kind == ConfirmKind::Close && !w.reusable {
            self.set_toast("Only reusable workers can be closed", true);
            return KeyOutcome::None;
        }
        self.modal = Some(ActiveModal::Confirm(ConfirmModal {
            kind,
            worker_id: w.id.clone(),
            worker_display: w.display_name(),
            watched_by: w.watched_by.clone(),
            hijack_confirmed: w.watched_by.is_empty(),
        }));
        KeyOutcome::None
    }

    fn open_apply_preview_modal(&mut self) -> KeyOutcome {
        let Some(w) = self.require_actions_ready() else {
            return KeyOutcome::None;
        };
        if w.is_active_turn() {
            self.set_toast("`claive apply` requires a settled worker", true);
            return KeyOutcome::None;
        }
        if !w.has_managed_worktree || w.retained_worktree.is_none() {
            self.set_toast("Selected worker has no retained Claive-managed worktree", true);
            return KeyOutcome::None;
        }
        let wid = w.id.clone();
        self.modal = Some(ActiveModal::ApplyPreview(ApplyPreviewModal {
            worker_id: wid.clone(),
            worker_display: w.display_name(),
            watched_by: w.watched_by.clone(),
            hijack_confirmed: w.watched_by.is_empty(),
            close_after: true,
            loading: true,
            result: None,
        }));
        KeyOutcome::RequestApplyDryRun(wid)
    }

    fn handle_modal_key(&mut self, key: KeyEvent) -> KeyOutcome {
        let Some(mut modal) = self.modal.take() else {
            return KeyOutcome::None;
        };

        match &mut modal {
            ActiveModal::Composer(comp) => match key.code {
                KeyCode::Esc => KeyOutcome::None,
                KeyCode::Char('y') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if !comp.watched_by.is_empty() {
                        comp.hijack_confirmed = !comp.hijack_confirmed;
                    }
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char('t') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if let ComposerKind::Send { ref mut mode } = comp.kind {
                        *mode = mode.next();
                    }
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char('d') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    let text = comp.buffer.trim().to_string();
                    if text.is_empty() {
                        self.set_toast("Message cannot be empty", true);
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    if !comp.watched_by.is_empty() && !comp.hijack_confirmed {
                        self.set_toast(
                            "Worker has a live parent watcher — press Ctrl-Y to confirm hijack first",
                            true,
                        );
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    // Recheck pinned worker state before dispatching.
                    let Some(live_w) = self
                        .snapshot
                        .workers
                        .iter()
                        .find(|w| w.id == comp.worker_id)
                        .cloned()
                    else {
                        self.set_toast("Worker no longer exists in registry", true);
                        return KeyOutcome::None;
                    };

                    let mutation = match &comp.kind {
                        ComposerKind::Answer {
                            expected_turn,
                            expected_question_hash,
                            is_goal,
                            ..
                        } => {
                            if !live_w.ask_eligible || live_w.turn != *expected_turn {
                                self.set_toast(
                                    format!(
                                        "Worker {} ASK state or turn changed (was turn {}, now {}); aborted",
                                        live_w.id, expected_turn, live_w.turn
                                    ),
                                    true,
                                );
                                return KeyOutcome::None;
                            }
                            let live_hash = live_w
                                .needs_parent
                                .as_ref()
                                .and_then(|n| n.raw_question_sha256.clone());
                            if expected_question_hash.is_some() && live_hash != *expected_question_hash {
                                self.set_toast("Worker ASK question changed before submit; aborted", true);
                                return KeyOutcome::None;
                            }
                            MutationKind::Answer {
                                worker_id: comp.worker_id.clone(),
                                expected_turn: *expected_turn,
                                expected_question_hash: expected_question_hash.clone(),
                                idempotency_key: make_request_id(&format!(
                                    "answer:{}:{}",
                                    comp.worker_id, expected_turn
                                )),
                                message: text,
                                is_goal: *is_goal,
                            }
                        }
                        ComposerKind::Send { mode } => MutationKind::Send {
                            worker_id: comp.worker_id.clone(),
                            mode: *mode,
                            message: text,
                            message_id: make_request_id(&format!("send:{}", comp.worker_id)),
                        },
                        ComposerKind::Steer => MutationKind::Steer {
                            worker_id: comp.worker_id.clone(),
                            message: text,
                        },
                    };
                    KeyOutcome::SubmitMutation(mutation)
                }
                KeyCode::Enter => {
                    comp.buffer.push('\n');
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Backspace => {
                    comp.buffer.pop();
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    comp.buffer.push(c);
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                _ => {
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
            },
            ActiveModal::Confirm(conf) => match key.code {
                KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => KeyOutcome::None,
                KeyCode::Char('h') | KeyCode::Char('H') => {
                    if !conf.watched_by.is_empty() {
                        conf.hijack_confirmed = !conf.hijack_confirmed;
                    }
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char('y') | KeyCode::Enter => {
                    if !conf.watched_by.is_empty() && !conf.hijack_confirmed {
                        self.set_toast(
                            "Worker has a live parent watcher — press [H] to confirm hijack first",
                            true,
                        );
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    let mutation = match conf.kind {
                        ConfirmKind::Cancel => MutationKind::Cancel {
                            worker_id: conf.worker_id.clone(),
                        },
                        ConfirmKind::Close => MutationKind::Close {
                            worker_id: conf.worker_id.clone(),
                        },
                    };
                    KeyOutcome::SubmitMutation(mutation)
                }
                _ => {
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
            },
            ActiveModal::ApplyPreview(prev) => match key.code {
                KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => KeyOutcome::None,
                KeyCode::Char('C') => {
                    prev.close_after = !prev.close_after;
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char('H') => {
                    if !prev.watched_by.is_empty() {
                        prev.hijack_confirmed = !prev.hijack_confirmed;
                    }
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
                KeyCode::Char('v') => {
                    let wid = prev.worker_id.clone();
                    KeyOutcome::SubmitMutation(MutationKind::Verify { worker_id: wid })
                }
                KeyCode::Char('y') | KeyCode::Enter => {
                    if prev.loading {
                        self.set_toast("Dry run preview still loading...", false);
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    let Some(ref dry) = prev.result else {
                        self.set_toast("No dry-run preview available", true);
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    };
                    if dry.is_blocked() {
                        self.set_toast(
                            "Apply blocked by conflicts or verification/ownership gates (press [v] to verify)",
                            true,
                        );
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    if !prev.watched_by.is_empty() && !prev.hijack_confirmed {
                        self.set_toast(
                            "Worker has a live parent watcher — press [H] to confirm hijack first",
                            true,
                        );
                        self.modal = Some(modal);
                        return KeyOutcome::None;
                    }
                    KeyOutcome::SubmitMutation(MutationKind::Apply {
                        worker_id: prev.worker_id.clone(),
                        close_after: prev.close_after,
                    })
                }
                _ => {
                    self.modal = Some(modal);
                    KeyOutcome::None
                }
            },
        }
    }
}

pub fn short_workspace(path: &str) -> String {
    let trimmed = path.trim_end_matches('/');
    let parts: Vec<&str> = trimmed.split('/').filter(|p| !p.is_empty()).collect();
    if parts.len() >= 2 {
        format!("{}/{}", parts[parts.len() - 2], parts[parts.len() - 1])
    } else if let Some(last) = parts.last() {
        (*last).to_string()
    } else {
        path.to_string()
    }
}

pub fn format_duration(secs: Option<f64>) -> String {
    let Some(s) = secs else {
        return "-".to_string();
    };
    let s = s.max(0.0) as u64;
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m{:02}s", s / 60, s % 60)
    } else if s < 86_400 {
        format!("{}h{:02}m", s / 3600, (s % 3600) / 60)
    } else {
        format!("{}d{:02}h", s / 86_400, (s % 86_400) / 3600)
    }
}

/// Human-friendly relative time ("just now", "5m ago", "12h ago", "2d ago").
pub fn format_relative_ago(secs: Option<f64>) -> String {
    let Some(s) = secs else {
        return "-".to_string();
    };
    let s = s.max(0.0) as u64;
    if s < 10 {
        "just now".to_string()
    } else if s < 60 {
        format!("{s}s ago")
    } else if s < 3600 {
        format!("{}m ago", s / 60)
    } else if s < 86_400 {
        format!("{}h ago", s / 3600)
    } else {
        format!("{}d ago", s / 86_400)
    }
}

fn local_tm(unix_secs: i64) -> Option<libc::tm> {
    let ts = unix_secs as libc::time_t;
    let mut out = std::mem::MaybeUninit::<libc::tm>::zeroed();
    // SAFETY: `localtime_r` writes into our valid stack `tm` buffer and does not alias.
    let ptr = unsafe { libc::localtime_r(&ts, out.as_mut_ptr()) };
    if ptr.is_null() {
        None
    } else {
        // SAFETY: `localtime_r` succeeded and initialized `out`.
        Some(unsafe { out.assume_init() })
    }
}

/// Format a worker launch timestamp in local time ("Today 08:19", "Yest 19:38", "Nov 21 18:46").
pub fn format_launched_at(started_at: Option<f64>, now: f64) -> String {
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let Some(ts) = started_at else {
        return "-".to_string();
    };
    let (Some(tm), Some(now_tm)) = (local_tm(ts as i64), local_tm(now as i64)) else {
        return format_relative_ago(Some((now - ts).max(0.0)));
    };
    let hh = tm.tm_hour;
    let mm = tm.tm_min;
    if tm.tm_year == now_tm.tm_year && tm.tm_yday == now_tm.tm_yday {
        format!("Today {hh:02}:{mm:02}")
    } else if let Some(yest_tm) = local_tm((now as i64) - 86_400)
        && tm.tm_year == yest_tm.tm_year
        && tm.tm_yday == yest_tm.tm_yday
    {
        format!("Yest  {hh:02}:{mm:02}")
    } else {
        let mon = MONTHS.get(tm.tm_mon as usize).copied().unwrap_or("???");
        format!("{mon} {:02} {hh:02}:{mm:02}", tm.tm_mday)
    }
}

pub fn format_tokens(n: u64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", n as f64 / 1_000.0)
    } else {
        n.to_string()
    }
}
