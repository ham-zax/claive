//! Inspector shell, Briefing tab/pane, Evidence tab, and Summary/Logs tab.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph, Tabs, Wrap};
use ratatui::Frame;

use crate::app::{
    classify_worker, format_duration, format_launched_at, format_relative_ago, format_tokens,
    is_review_pending, short_workspace, App, FocusPane, InspectorTab, TriageBadge,
};
use crate::commands::LogKind;
use crate::format::{
    render_markdown_lines, strip_inline_markdown, summarize_tool_detail, StepCategory,
};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

use super::activity::build_steps_lines;
use super::{
    badge_spans, kv_line, COLOR_ACCENT, COLOR_ASK, COLOR_BRIGHT, COLOR_FAIL, COLOR_MUTED,
    COLOR_NOMINAL, COLOR_REVIEW, COLOR_WARN,
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

pub(super) fn render_inspector(frame: &mut Frame, area: Rect, app: &App) {
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
        InspectorTab::Logs => (build_logs_lines(app, w), app.inspector_scroll),
    };

    let body = Paragraph::new(body_lines)
        .wrap(Wrap { trim: false })
        .scroll((effective_scroll, 0));
    frame.render_widget(body, chunks[1]);
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
    let launched_str = format_launched_at(w.started_at, now);
    let launched_ago = format_relative_ago(w.started_at.map(|s| (now - s).max(0.0)));
    let run_str = format_duration(w.turn_elapsed_s.or(w.elapsed_s));
    let timing_line = if w.is_active_turn() {
        format!(
            "Launched {launched_str} ({launched_ago}) · Running {run_str} · Last event {} · Turn {}",
            format_relative_ago(w.last_event_age_s),
            w.turn
        )
    } else {
        format!("Launched {launched_str} ({launched_ago}) · Ran {run_str} · Turn {}", w.turn)
    };
    lines.push(Line::from(Span::styled(
        timing_line,
        Style::default().fg(COLOR_MUTED),
    )));

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
    if let Some(err) = &w.error {
        lines.push(Line::from(Span::styled(
            format!(
                "[!] ERROR ({}): {err}",
                w.failure_kind.as_deref().unwrap_or("error")
            ),
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        )));
    }

    // 3. Mission.
    let mission_text = w
        .mission
        .as_deref()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            (!w.label.is_empty() && !w.label.eq_ignore_ascii_case("prompt-input"))
                .then_some(w.label.as_str())
        })
        .or(w.prompt_preview.as_deref());
    if let Some(m) = mission_text {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Mission",
            Style::default().fg(COLOR_MUTED).add_modifier(Modifier::BOLD),
        )));
        lines.push(Line::from(Span::styled(
            strip_inline_markdown(m),
            Style::default().fg(COLOR_BRIGHT),
        )));
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
        let chk_style = if chk.status == "passed" {
            Style::default().fg(COLOR_REVIEW)
        } else {
            Style::default().fg(COLOR_FAIL)
        };
        let detail = if !chk.summary.is_empty() {
            format!("{} — {}", chk.status, chk.summary)
        } else {
            format!("{} — {}", chk.status, chk.command)
        };
        lines.push(kv_line("Last check", detail, chk_style));
    }
    lines.push(kv_line(
        "Workspace",
        format!(
            "{} files · verify={} · accept={}{}",
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

    if let Some(cache) = w.native_cache_read_tokens {
        lines.push(Line::from(""));
        lines.push(kv_line(
            "Cache read",
            format!("{} tokens · {} steps", format_tokens(cache), w.step_count),
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
            lines.push(Line::from(Span::styled(
                format!("  {path}"),
                Style::default().fg(COLOR_NOMINAL),
            )));
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

    lines
}

fn build_logs_lines(app: &App, w: &WorkerRecord) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled(
        format!(
            "View: {}  ([L] cycle Summary/Prompt/Stderr/Supervisor · [Tab] focus to scroll)",
            app.log_kind.label()
        ),
        Style::default().fg(COLOR_ACCENT),
    ))];

    if let Some(log) = app.log_cache.get(&(w.id.clone(), app.log_kind)) {
        if log.truncated {
            lines.push(Line::from(Span::styled(
                "(showing bounded tail -- last 64 KiB)",
                Style::default().fg(COLOR_MUTED),
            )));
        }
        lines.push(Line::from(""));
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
            "Loading bounded log tail...",
            Style::default().fg(COLOR_MUTED),
        )));
    }

    lines
}
