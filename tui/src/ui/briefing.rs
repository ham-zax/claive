//! Inspector shell, Briefing tab/pane, Evidence tab, and Summary/Logs tab.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph, Tabs, Wrap};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

use crate::app::{
    classify_worker, format_duration, format_launched_at, format_relative_ago, format_tokens,
    is_review_pending, short_workspace, worker_last_updated, App, ClickAction, ClickRegion,
    FocusPane, InspectorTab, TriageBadge,
};
use crate::commands::LogKind;
use crate::format::{
    render_markdown_lines, strip_inline_markdown, summarize_tool_detail, StepCategory,
};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

use super::activity::build_steps_lines;
use super::{
    badge_spans, kv_line, push_chip, COLOR_ACCENT, COLOR_ASK, COLOR_BRIGHT, COLOR_FAIL,
    COLOR_MUTED, COLOR_NOMINAL, COLOR_REVIEW, COLOR_WARN,
};

pub(super) fn render_briefing_pane(frame: &mut Frame, area: Rect, app: &App) {
    let title = match app.selected_worker() {
        Some(w) => format!(" Briefing: {} ", w.display_name()),
        None => " Briefing ".to_string(),
    };
    let block = Block::default()
        .title(clip_width(&title, area.width.saturating_sub(2) as usize))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .border_style(Style::default().fg(COLOR_MUTED));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height == 0 || inner.width < 4 {
        return;
    }
    let lines = match app.selected_worker() {
        Some(w) => build_overview_lines(app, w, inner.width as usize),
        None => vec![Line::from(Span::styled(
            "Select a worker to inspect.",
            Style::default().fg(COLOR_MUTED),
        ))],
    };
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

pub(super) fn render_inspector(frame: &mut Frame, area: Rect, app: &mut App) {
    app.inspector_area = area;
    let focused = app.focus_pane == FocusPane::Inspector;
    let border_style = if focused {
        Style::default().fg(COLOR_ACCENT)
    } else {
        Style::default().fg(COLOR_MUTED)
    };

    let title = match app.selected_worker() {
        Some(w) => format!(" Inspector: {} ({}) ", w.display_name(), w.id),
        None => " Inspector ".to_string(),
    };
    let block = Block::default()
        .title(clip_width(&title, area.width.saturating_sub(2) as usize))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .border_style(border_style);

    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height < 2 || inner.width < 4 {
        return;
    }

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(1)])
        .split(inner);

    let tab_titles: Vec<Line> = InspectorTab::ALL
        .iter()
        .map(|t| Line::from(t.label()))
        .collect();
    let selected_tab_idx = InspectorTab::ALL
        .iter()
        .position(|t| *t == app.inspector_tab)
        .unwrap_or(0);
    let tabs = Tabs::new(tab_titles)
        .select(selected_tab_idx)
        .style(Style::default().fg(COLOR_MUTED))
        .highlight_style(
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
        )
        .divider("│");
    frame.render_widget(tabs, chunks[0]);

    // Register mouse click regions for each Inspector tab (Tabs renders ` label ` + `│`).
    let mut tab_x = chunks[0].x;
    let tab_max_x = chunks[0].x.saturating_add(chunks[0].width);
    for (idx, tab) in InspectorTab::ALL.iter().enumerate() {
        let w = (tab.label().width() as u16).saturating_add(2);
        if tab_x >= tab_max_x {
            break;
        }
        let clamped_w = w.min(tab_max_x.saturating_sub(tab_x));
        app.click_regions.push(ClickRegion {
            rect: Rect::new(tab_x, chunks[0].y, clamped_w, 1),
            action: ClickAction::SelectInspectorTab(*tab),
        });
        tab_x = tab_x.saturating_add(w);
        if idx + 1 < InspectorTab::ALL.len() {
            tab_x = tab_x.saturating_add(1); // divider
        }
    }

    if app.inspector_tab == InspectorTab::Logs {
        if app.selected_worker().is_none() {
            let empty = Paragraph::new("Select a worker to inspect.")
                .style(Style::default().fg(COLOR_MUTED));
            frame.render_widget(empty, chunks[1]);
            return;
        }
        let log_chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Min(1)])
            .split(chunks[1]);
        render_log_selector_bar(frame, log_chunks[0], app);
        let Some(w) = app.selected_worker() else {
            return;
        };
        let body_lines = build_logs_lines(app, w);
        let body = Paragraph::new(body_lines)
            .wrap(Wrap { trim: false })
            .scroll((app.inspector_scroll, 0));
        frame.render_widget(body, log_chunks[1]);
        return;
    }

    let Some(w) = app.selected_worker() else {
        let empty = Paragraph::new("Select a worker to inspect.")
            .style(Style::default().fg(COLOR_MUTED));
        frame.render_widget(empty, chunks[1]);
        return;
    };

    let body_width = chunks[1].width as usize;
    let body_height = chunks[1].height as usize;
    let (body_lines, effective_scroll) = match app.inspector_tab {
        InspectorTab::Overview => (build_overview_lines(app, w, body_width), app.inspector_scroll),
        InspectorTab::Evidence => (build_evidence_lines(w), app.inspector_scroll),
        InspectorTab::Steps => build_steps_lines(app, w, body_width, body_height),
        InspectorTab::Logs => unreachable!(),
    };

    let body = Paragraph::new(body_lines)
        .wrap(Wrap { trim: false })
        .scroll((effective_scroll, 0));
    frame.render_widget(body, chunks[1]);
}

fn render_log_selector_bar(frame: &mut Frame, area: Rect, app: &mut App) {
    let mut spans = Vec::new();
    let mut cx = area.x;
    let max_x = area.x.saturating_add(area.width);
    for kind in LogKind::ALL {
        let active = app.log_kind == kind;
        let accent = match kind {
            LogKind::Stderr => COLOR_FAIL,
            LogKind::Prompt => COLOR_ACCENT,
            _ => COLOR_NOMINAL,
        };
        push_chip(
            &mut spans,
            &mut cx,
            area.y,
            max_x,
            kind.short_label().to_string(),
            active,
            accent,
            ClickAction::SelectLogKind(kind),
            &mut app.click_regions,
        );
    }
    let hint = " [L] cycle · [P] prompt";
    if cx.saturating_add(hint.width() as u16) <= max_x {
        spans.push(Span::styled(hint, Style::default().fg(COLOR_MUTED)));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn build_overview_lines(app: &App, w: &WorkerRecord, body_width: usize) -> Vec<Line<'static>> {
    let badge = classify_worker(w);
    let mut lines = Vec::new();

    // 1. Clean Identity & Timing strip (human launch time + runtime, zero neon blocks).
    let model_short = match (&w.model, &w.reasoning_effort) {
        (Some(m), Some(e)) => format!("{}/{m} ({e})", w.engine),
        (Some(m), None) => format!("{}/{m}", w.engine),
        _ => w.engine.clone(),
    };
    let ws_short = w
        .workspace
        .as_deref()
        .map(short_workspace)
        .unwrap_or_else(|| "-".to_string());
    let mut id_spans = badge_spans(badge, false);
    id_spans.push(Span::styled("  ·  ", Style::default().fg(COLOR_MUTED)));
    id_spans.push(Span::styled(
        format!("{model_short} · {ws_short}"),
        Style::default().fg(COLOR_BRIGHT),
    ));
    if !w.watched_by.is_empty() {
        id_spans.push(Span::styled(
            format!(" · watched({})", w.watched_by.join(",")),
            Style::default().fg(COLOR_ACCENT),
        ));
    } else if w.parked {
        id_spans.push(Span::styled(" · parked", Style::default().fg(COLOR_WARN)));
    }
    lines.push(Line::from(id_spans));

    let now = app.snapshot.refreshed_at;
    let upd_ts = worker_last_updated(w);
    let upd_ago = format_relative_ago(upd_ts.map(|t| (now - t).max(0.0)).or(w.last_event_age_s));
    let upd_clock = format_launched_at(upd_ts, now);
    let launched_str = format_launched_at(w.started_at, now);
    let live_run_secs = if w.is_active_turn() {
        w.turn_started_at
            .or(w.started_at)
            .map(|s| (now - s).max(0.0))
            .or(w.turn_elapsed_s)
            .or(w.elapsed_s)
    } else {
        w.turn_elapsed_s.or(w.elapsed_s)
    };
    let run_str = format_duration(live_run_secs);
    let timing_line = if w.is_active_turn() {
        format!("Updated {upd_ago} ({upd_clock}) · Launched {launched_str} · Running {run_str} · Turn {}", w.turn)
    } else {
        format!("Updated {upd_ago} ({upd_clock}) · Launched {launched_str} · Ran {run_str} · Turn {}", w.turn)
    };
    lines.push(Line::from(Span::styled(
        timing_line,
        Style::default().fg(COLOR_MUTED),
    )));

    if let Some(sess) = &w.parent_session {
        let watch_note = if !w.watched_by.is_empty() {
            format!(" · watched by {}", w.watched_by.join(", "))
        } else if w.is_active_turn() && !w.parked {
            " · unwatched".to_string()
        } else {
            String::new()
        };
        lines.push(kv_line(
            "Session",
            format!("{sess}{watch_note}"),
            Style::default().fg(COLOR_NOMINAL),
        ));
    }

    // 2. Blocker / Attention banner (ASK, UNWATCHED, WAIT-IN-TURN, Error, Drain).
    if let Some(ask) = &w.needs_parent {
        let kind = ask.kind.as_deref().unwrap_or("needs_parent");
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            format!("[ASK] NEEDS PARENT ({kind}):"),
            Style::default()
                .fg(COLOR_ASK)
                .add_modifier(Modifier::BOLD),
        )));
        lines.push(Line::from(Span::styled(
            format!("  {}", ask.question),
            Style::default().fg(COLOR_BRIGHT),
        )));
        lines.push(Line::from(Span::styled(
            format!("  [a] answer  |  [y] copy: {}", app.remediation_command(w)),
            Style::default().fg(COLOR_ACCENT),
        )));
    }
    if badge == TriageBadge::Unwatched {
        lines.push(Line::from(Span::styled(
            "[!] UNWATCHED — no live `claive wait` covering this worker",
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        )));
    } else if badge == TriageBadge::WaitInTurn {
        lines.push(Line::from(Span::styled(
            "[!] CODEX PARENT — wait in-turn or press [p] to park",
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        )));
    }
    if w.close_requested {
        lines.push(Line::from(Span::styled(
            "[!] DRAINING — close.request pending",
            Style::default().fg(COLOR_WARN),
        )));
    }
    if let Some(best_err) = w.best_error_summary() {
        lines.push(Line::from(Span::styled(
            format!(
                "[!] ERROR ({}): {best_err}",
                w.failure_kind.as_deref().unwrap_or("error")
            ),
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        )));
        if let Some(raw_err) = &w.error
            && raw_err != &best_err
        {
            lines.push(Line::from(Span::styled(
                format!("    ({raw_err} · [4] stderr)"),
                Style::default().fg(COLOR_MUTED),
            )));
        }
    }

    // 3. Mission & Initial Prompt snippet.
    let mission_text = w
        .mission
        .as_deref()
        .filter(|s| !s.is_empty())
        .or_else(|| (!w.is_alias_like_label()).then_some(w.label.as_str()))
        .or(w.prompt_preview.as_deref())
        .or_else(|| (!w.label.is_empty()).then_some(w.label.as_str()));
    if let Some(m) = mission_text {
        lines.push(Line::from(""));
        lines.push(Line::from(vec![
            Span::styled(
                "Mission",
                Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
            ),
            Span::styled("  ([P] full prompt)", Style::default().fg(COLOR_MUTED)),
        ]));
        lines.push(Line::from(Span::styled(
            strip_inline_markdown(m),
            Style::default().fg(COLOR_BRIGHT),
        )));
    }
    // Show a compact 3-line prompt preview when the prompt has additional detail beyond Mission.
    let mut prompt_lines: Vec<&str> = Vec::new();
    if let Some(prev) = w.prompt_preview.as_deref()
        && mission_text != Some(prev)
    {
        prompt_lines.push(prev);
    }
    for ex in &w.prompt_excerpt {
        if prompt_lines.len() >= 3 {
            break;
        }
        if mission_text != Some(ex.as_str()) {
            prompt_lines.push(ex.as_str());
        }
    }
    if !prompt_lines.is_empty() {
        let cap = body_width.saturating_sub(2).max(32);
        for pl in prompt_lines {
            lines.push(Line::from(Span::styled(
                clip_width(&strip_inline_markdown(pl), cap),
                Style::default().fg(COLOR_NOMINAL),
            )));
        }
    }

    // 4. Now & Area.
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Now & Area",
        Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
    )));
    if let Some(act) = &w.activity {
        let stage_tag = format!("[{}]", act.stage.to_uppercase());
        lines.push(Line::from(vec![
            Span::styled(
                format!("{stage_tag} "),
                Style::default()
                    .fg(COLOR_ACCENT)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(act.label.clone(), Style::default().fg(COLOR_BRIGHT)),
        ]));
        if !act.targets.is_empty() {
            lines.push(kv_line(
                "Targets",
                act.targets.join(", "),
                Style::default().fg(COLOR_NOMINAL),
            ));
        }
    } else if !w.active_tools.is_empty() {
        lines.push(kv_line(
            "Active tool",
            format!("BUSY: {}", w.active_tools.join(", ")),
            Style::default().fg(COLOR_BRIGHT),
        ));
    } else if let Some(lt) = &w.last_tool {
        lines.push(kv_line(
            "Last tool",
            summarize_tool_detail(&lt.tool, &lt.status, &lt.detail),
            Style::default().fg(COLOR_NOMINAL),
        ));
    }
    if let Some(intent) = w
        .latest_intent
        .as_deref()
        .or_else(|| app.peek_cache.get(&w.id).and_then(|p| p.intent.as_deref()))
    {
        lines.push(kv_line(
            "Intent",
            strip_inline_markdown(intent),
            Style::default().fg(COLOR_NOMINAL),
        ));
    }
    if let Some(chk) = &w.last_check {
        let passed = matches!(chk.status.to_ascii_lowercase().as_str(), "pass" | "passed" | "ok");
        let chk_style = if passed {
            Style::default().fg(COLOR_REVIEW)
        } else {
            Style::default().fg(COLOR_FAIL)
        };
        let icon = if passed { "✓" } else { "✖" };
        let sum_trimmed = chk.summary.trim();
        let cmd_trimmed = chk.command.trim();
        let redundant = sum_trimmed.eq_ignore_ascii_case(&chk.status)
            || (passed && matches!(sum_trimmed.to_ascii_lowercase().as_str(), "pass" | "passed" | "ok"));
        let detail = if !sum_trimmed.is_empty() && !redundant {
            format!("{icon} {sum_trimmed}")
        } else if !cmd_trimmed.is_empty() && !cmd_trimmed.eq_ignore_ascii_case(&chk.status) {
            format!("{icon} {} ({cmd_trimmed})", if passed { "passed" } else { chk.status.as_str() })
        } else {
            format!("{icon} {}", if passed { "passed" } else { chk.status.as_str() })
        };
        lines.push(kv_line("Last check", detail, chk_style));
    }
    let f_word = if w.changed_files_total == 1 { "file" } else { "files" };
    lines.push(kv_line(
        "Workspace",
        format!(
            "{} {f_word} · verify={} · accept={}{}",
            w.changed_files_total,
            w.verification_status,
            w.acceptance_status,
            if is_review_pending(w) { " [REVIEW]" } else { "" }
        ),
        if is_review_pending(w) {
            Style::default()
                .fg(COLOR_REVIEW)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(COLOR_NOMINAL)
        },
    ));

    // 5. Recent Milestones (auto-loaded off-thread via native events tailer or peek).
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Recent Milestones  ([3] full activity)",
        Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
    )));
    let milestones = app.recent_milestones_for(&w.id, 5);
    if !milestones.is_empty() {
        let budget = body_width.saturating_sub(10).max(20);
        for m in milestones {
            let mut spans = vec![Span::styled(
                format!("{} ", m.category.tag()),
                m.category.style(),
            )];
            if let Some(ref out) = m.outcome {
                let combined_budget = budget
                    .saturating_sub(out.chars().count().min(32) + 4)
                    .max(12);
                spans.push(Span::styled(
                    clip_width(&m.summary, combined_budget),
                    Style::default().fg(COLOR_NOMINAL),
                ));
                let out_style = match m.status.as_deref() {
                    Some("error") => Style::default().fg(COLOR_FAIL),
                    Some("running") => Style::default().fg(COLOR_WARN),
                    _ => Style::default().fg(COLOR_REVIEW),
                };
                spans.push(Span::styled(" -> ", Style::default().fg(COLOR_MUTED)));
                spans.push(Span::styled(clip_width(out, 32), out_style));
            } else {
                spans.push(Span::styled(
                    clip_width(&m.summary, budget),
                    if m.category == StepCategory::Say {
                        Style::default().fg(COLOR_BRIGHT)
                    } else if m.category == StepCategory::Err {
                        Style::default().fg(COLOR_FAIL)
                    } else {
                        Style::default().fg(COLOR_NOMINAL)
                    },
                ));
            }
            lines.push(Line::from(spans));
        }
    } else if app.peek_loading.contains(&w.id) {
        lines.push(Line::from(Span::styled(
            "  Loading recent milestones...",
            Style::default().fg(COLOR_ACCENT),
        )));
    } else {
        lines.push(Line::from(Span::styled(
            "  No milestones recorded yet.",
            Style::default().fg(COLOR_MUTED),
        )));
    }

    // 6. Outcome & Remediation.
    if let Some(summary) = &w.report_summary {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Outcome",
            Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
        )));
        lines.push(Line::from(Span::styled(
            strip_inline_markdown(summary),
            Style::default().fg(COLOR_NOMINAL),
        )));
    }

    if !w.report_risks.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Reported risks",
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        )));
        for r in &w.report_risks {
            lines.push(Line::from(Span::styled(
                format!("  - {r}"),
                Style::default().fg(COLOR_NOMINAL),
            )));
        }
    }

    let mut usage_parts: Vec<String> = Vec::new();
    if let Some(inp) = w.input_tokens
        && inp > 0
    {
        usage_parts.push(format!("in {}", format_tokens(inp)));
    }
    if let Some(out) = w.output_tokens
        && out > 0
    {
        usage_parts.push(format!("out {}", format_tokens(out)));
    }
    if let Some(rsn) = w.reasoning_tokens
        && rsn > 0
    {
        usage_parts.push(format!("think {}", format_tokens(rsn)));
    }
    if let Some(cache) = w.native_cache_read_tokens
        && (cache > 0 || !usage_parts.is_empty() || w.step_count > 0)
    {
        let pct = w
            .input_tokens
            .filter(|&i| i > 0)
            .map(|i| format!(" ({}%)", ((cache * 100) / (i + cache)).min(100)))
            .unwrap_or_default();
        usage_parts.push(format!("cache {}{pct}", format_tokens(cache)));
    }
    if let Some(ctx) = w.context_tokens
        && ctx > 0
    {
        usage_parts.push(format!("ctx {}", format_tokens(ctx)));
    }
    if !usage_parts.is_empty() || w.step_count > 0 {
        usage_parts.push(format!("{} steps", w.step_count));
        lines.push(Line::from(""));
        lines.push(kv_line(
            "Usage",
            usage_parts.join(" · "),
            Style::default().fg(COLOR_MUTED),
        ));
    }

    if w.status_projection_stale {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Note: status.json missing or lagging state.json; showing state.json fallback.",
            Style::default().fg(COLOR_WARN),
        )));
    }

    lines.push(Line::from(""));
    lines.push(kv_line(
        "Remediation",
        format!("[y] {}", app.remediation_command(w)),
        Style::default().fg(COLOR_ACCENT),
    ));

    lines
}

fn build_evidence_lines(w: &WorkerRecord) -> Vec<Line<'static>> {
    let mut lines = vec![
        kv_line(
            "Verification",
            w.verification_status.clone(),
            if w.verification_status == "passed" {
                Style::default().fg(COLOR_REVIEW)
            } else if w.verification_status == "failed" {
                Style::default().fg(COLOR_FAIL)
            } else {
                Style::default().fg(COLOR_NOMINAL)
            },
        ),
        kv_line(
            "Acceptance",
            w.acceptance_status.clone(),
            if w.acceptance_status == "accepted" {
                Style::default().fg(COLOR_REVIEW)
            } else {
                Style::default().fg(COLOR_NOMINAL)
            },
        ),
        kv_line(
            "Cmd failures",
            w.task_failures.to_string(),
            if w.task_failures > 0 {
                Style::default().fg(COLOR_WARN)
            } else {
                Style::default().fg(COLOR_MUTED)
            },
        ),
    ];

    if let Some(wt) = &w.retained_worktree {
        lines.push(kv_line("Worktree", wt.clone(), Style::default().fg(COLOR_NOMINAL)));
        lines.push(kv_line(
            "Shortcuts",
            "[W] copy cd worktree · [D] copy git diff",
            Style::default().fg(COLOR_ACCENT),
        ));
    } else if let Some(ws) = w.actual_workspace.as_deref().or(w.workspace.as_deref()) {
        lines.push(kv_line("Workspace", ws.to_string(), Style::default().fg(COLOR_NOMINAL)));
        lines.push(kv_line(
            "Shortcuts",
            "[W] copy cd workspace · [D] copy git diff",
            Style::default().fg(COLOR_ACCENT),
        ));
    }
    if let Some(branch) = &w.worktree_branch {
        lines.push(kv_line("Branch", branch.clone(), Style::default().fg(COLOR_NOMINAL)));
    }
    if let Some(base) = &w.worktree_base_commit {
        lines.push(kv_line(
            "Base commit",
            base.chars().take(12).collect::<String>(),
            Style::default().fg(COLOR_MUTED),
        ));
    }

    if w.delta_added.is_some() || w.delta_deleted.is_some() || w.delta_untracked.is_some() {
        let mut dparts = Vec::new();
        if let Some(a) = w.delta_added {
            dparts.push(format!("+{a} added"));
        }
        if let Some(d) = w.delta_deleted {
            dparts.push(format!("-{d} deleted"));
        }
        if let Some(u) = w.delta_untracked {
            dparts.push(format!("{u} untracked"));
        }
        lines.push(kv_line("Delta", dparts.join(" · "), Style::default().fg(COLOR_NOMINAL)));
    }

    if !w.owned_paths.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            format!("Owned paths ({}):", w.owned_paths.len()),
            Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
        )));
        for pat in w.owned_paths.iter().take(12) {
            lines.push(Line::from(Span::styled(
                format!("  {pat}"),
                Style::default().fg(COLOR_MUTED),
            )));
        }
        if w.owned_paths.len() > 12 {
            lines.push(Line::from(Span::styled(
                format!("  ... +{} more", w.owned_paths.len() - 12),
                Style::default().fg(COLOR_MUTED),
            )));
        }
    }

    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            "Changed files ({}{}):",
            w.changed_files_total,
            if w.changed_files_truncated {
                format!(", showing first {}", w.changed_files.len())
            } else {
                String::new()
            }
        ),
        Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
    )));

    if w.changed_files.is_empty() {
        lines.push(Line::from(Span::styled(
            "  (none recorded)",
            Style::default().fg(COLOR_MUTED),
        )));
    } else {
        for path in &w.changed_files {
            if w.is_path_in_owned_scope(path) {
                lines.push(Line::from(Span::styled(
                    format!("  {path}"),
                    Style::default().fg(COLOR_NOMINAL),
                )));
            } else {
                lines.push(Line::from(vec![
                    Span::styled(format!("  {path}"), Style::default().fg(COLOR_WARN)),
                    Span::styled(
                        "  [OUT OF SCOPE]",
                        Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
                    ),
                ]));
            }
        }
        if w.changed_files_total > w.changed_files.len() {
            lines.push(Line::from(Span::styled(
                format!(
                    "  ... +{} more (capped in status.json)",
                    w.changed_files_total - w.changed_files.len()
                ),
                Style::default().fg(COLOR_MUTED),
            )));
        }
    }

    if let Some(ds) = &w.diff_stat {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Diff stat:",
            Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
        )));
        for line in ds.lines().take(12) {
            lines.push(Line::from(Span::styled(
                format!("  {line}"),
                Style::default().fg(COLOR_NOMINAL),
            )));
        }
    }

    lines
}

fn build_logs_lines(app: &App, w: &WorkerRecord) -> Vec<Line<'static>> {
    let mut lines = Vec::new();

    if let Some(log) = app.log_cache.get(&(w.id.clone(), app.log_kind)) {
        if log.truncated {
            let note = if app.log_kind == LogKind::Prompt {
                "(showing bounded head -- first 64 KiB)"
            } else {
                "(showing bounded tail -- last 64 KiB)"
            };
            lines.push(Line::from(Span::styled(
                note,
                Style::default().fg(COLOR_MUTED),
            )));
            lines.push(Line::from(""));
        }
        if log.content.is_empty() {
            lines.push(Line::from(Span::styled(
                "(empty or file does not exist yet)",
                Style::default().fg(COLOR_MUTED),
            )));
        } else if matches!(app.log_kind, LogKind::Result | LogKind::Prompt) {
            lines.extend(render_markdown_lines(&log.content, ""));
        } else {
            for raw_line in log.content.lines() {
                let low = raw_line.to_lowercase();
                let style = if low.contains("error")
                    || low.contains("traceback")
                    || low.contains("failed")
                {
                    Style::default().fg(COLOR_FAIL)
                } else if low.contains("warn") {
                    Style::default().fg(COLOR_WARN)
                } else {
                    Style::default().fg(COLOR_NOMINAL)
                };
                lines.push(Line::from(Span::styled(raw_line.to_string(), style)));
            }
        }
    } else {
        lines.push(Line::from(Span::styled(
            "Loading bounded log...",
            Style::default().fg(COLOR_MUTED),
        )));
    }

    lines
}
