//! Responsive Ratatui 0.30.2 renderer with calm Dark Cockpit visual discipline.
//! Handles >=150x32 3-col split, >=110x32 wide, >=80x24 medium, narrow single-pane, and <6-row `--compact`.

mod activity;
mod briefing;
mod modals;
mod table;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph};
use ratatui::Frame;

use crate::app::{
    classify_worker, format_duration, format_relative_ago, App, DrawerTab, FocusPane, InspectorTab,
    TriageBadge,
};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

// Calm Dark Cockpit palette: no screaming neon background bricks; anomalies pop via crisp color + icons.
pub(super) const COLOR_MUTED: Color = Color::DarkGray;
pub(super) const COLOR_NOMINAL: Color = Color::Gray;
pub(super) const COLOR_BRIGHT: Color = Color::White;
pub(super) const COLOR_ACCENT: Color = Color::Cyan;
pub(super) const COLOR_ASK: Color = Color::Magenta;
pub(super) const COLOR_WARN: Color = Color::Yellow;
pub(super) const COLOR_FAIL: Color = Color::Red;
pub(super) const COLOR_REVIEW: Color = Color::Green;
pub(super) const COLOR_ROW_SEL_BG: Color = Color::Indexed(235);

pub fn render(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    if area.width == 0 || area.height == 0 {
        return;
    }

    if app.config.compact || area.height < 6 {
        render_compact(frame, area, app);
        return;
    }

    if area.width >= 110 && area.height >= 32 {
        render_wide(frame, area, app);
    } else if area.width >= 80 && area.height >= 24 {
        render_medium(frame, area, app);
    } else {
        render_narrow(frame, area, app);
    }

    if app.help_open {
        modals::render_help_modal(frame, area, app.config.actions_enabled());
    } else if let Some(ref modal) = app.modal {
        modals::render_action_modal(frame, area, modal);
    }
}

/// Clean foreground-only badge style (zero solid background blocks that fuse adjacent rows).
pub(super) fn badge_style(badge: TriageBadge) -> Style {
    match badge {
        TriageBadge::Ask => Style::default()
            .fg(COLOR_ASK)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Unwatched | TriageBadge::WaitInTurn => Style::default()
            .fg(COLOR_WARN)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Lost | TriageBadge::Fail => Style::default()
            .fg(COLOR_FAIL)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Review => Style::default()
            .fg(COLOR_REVIEW)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Quiet => Style::default()
            .fg(COLOR_WARN)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Run => Style::default()
            .fg(COLOR_ACCENT)
            .add_modifier(Modifier::BOLD),
        TriageBadge::Parked => Style::default().fg(COLOR_WARN),
        TriageBadge::Done => Style::default().fg(COLOR_MUTED),
        TriageBadge::Idle | TriageBadge::Stop => Style::default().fg(COLOR_MUTED),
    }
}

/// Format a status badge as crisp `icon LABEL` spans (for table cells and Briefing strip).
pub(super) fn badge_spans(badge: TriageBadge, unseen: bool) -> Vec<Span<'static>> {
    let icon_style = match badge {
        TriageBadge::Done => Style::default().fg(COLOR_REVIEW),
        _ => badge_style(badge),
    };
    let mut spans = Vec::new();
    if unseen {
        spans.push(Span::styled(
            "•",
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        ));
    }
    spans.push(Span::styled(format!("{} ", badge.icon()), icon_style));
    spans.push(Span::styled(badge.short_label(), badge_style(badge)));
    spans
}

pub(super) fn row_text_style(badge: TriageBadge) -> Style {
    if badge.is_anomalous() || badge == TriageBadge::Run {
        Style::default()
            .fg(COLOR_BRIGHT)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(COLOR_NOMINAL)
    }
}

pub(super) fn kv_line(k: &str, v: impl Into<String>, val_style: Style) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{k:<14}"), Style::default().fg(COLOR_MUTED)),
        Span::styled(v.into(), val_style),
    ])
}

fn render_compact(frame: &mut Frame, area: Rect, app: &App) {
    let width = area.width as usize;
    let max_rows = area.height as usize;
    let mut lines: Vec<Line> = Vec::with_capacity(max_rows);

    let scope = if app.workspace_scope.label() != "all" {
        app.workspace_scope.label()
    } else if app.session_scope.label() != "all" {
        app.session_scope.label()
    } else {
        "all".to_string()
    };
    let risk = app.counts.unwatched + app.counts.wait_in_turn + app.counts.lost + app.counts.fail;
    let mut header_spans = vec![
        Span::styled(
            "◆ claive",
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!(" · {scope}"), Style::default().fg(COLOR_MUTED)),
    ];
    if app.counts.ask > 0 {
        header_spans.push(Span::styled(
            format!(" · ● {} ASK", app.counts.ask),
            Style::default().fg(COLOR_ASK).add_modifier(Modifier::BOLD),
        ));
    }
    if risk > 0 {
        header_spans.push(Span::styled(
            format!(" · ✖ {risk} risk"),
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        ));
    }
    if app.counts.running > 0 {
        header_spans.push(Span::styled(
            format!(" · ▸ {} running", app.counts.running),
            Style::default().fg(COLOR_ACCENT),
        ));
    }
    if app.counts.review > 0 {
        header_spans.push(Span::styled(
            format!(" · ★ {} review", app.counts.review),
            Style::default().fg(COLOR_REVIEW),
        ));
    }
    if app.snapshot.stale_reason.is_some() {
        header_spans.push(Span::styled(
            " · STALE",
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        ));
    }
    lines.push(Line::from(header_spans));

    let slot_count = max_rows.saturating_sub(2).min(3);
    if app.visible_workers.is_empty() && slot_count > 0 {
        lines.push(Line::from(Span::styled(
            clip_width("All clear — no active workers", width),
            Style::default().fg(COLOR_MUTED),
        )));
    } else {
        for &idx in app.visible_workers.iter().take(slot_count) {
            let w = &app.snapshot.workers[idx];
            let badge = classify_worker(w);
            let name = clip_width(&w.display_name(), 14);
            let detail = compact_detail(w, badge);
            let remaining = width.saturating_sub(12 + 15 + 2);
            let clipped_detail = clip_width(&detail, remaining);
            let tag = format!("{} {:<8}", badge.icon(), badge.short_label());
            lines.push(Line::from(vec![
                Span::styled(tag, badge_style(badge)),
                Span::raw(" "),
                Span::styled(format!("{:<14}", name), row_text_style(badge)),
                Span::raw(" "),
                Span::styled(clipped_detail, Style::default().fg(COLOR_NOMINAL)),
            ]));
        }
    }

    while lines.len() + 1 < max_rows {
        lines.push(Line::from(""));
    }

    if max_rows >= 2 {
        let extra = app.visible_workers.len().saturating_sub(slot_count);
        let footer = if extra > 0 {
            format!("+{extra} more · claive dash")
        } else {
            format!(
                "{} shown · {} done · claive dash",
                app.visible_workers.len(),
                app.counts.done
            )
        };
        lines.push(Line::from(Span::styled(
            clip_width(&footer, width),
            Style::default().fg(COLOR_MUTED),
        )));
    }

    frame.render_widget(Paragraph::new(lines), area);
}

fn compact_detail(w: &WorkerRecord, badge: TriageBadge) -> String {
    match badge {
        TriageBadge::Ask => w
            .needs_parent
            .as_ref()
            .map(|n| n.question.clone())
            .filter(|q| !q.is_empty())
            .unwrap_or_else(|| "Needs parent decision".to_string()),
        TriageBadge::Unwatched => format!(
            "Running · last event {}",
            format_relative_ago(w.last_event_age_s)
        ),
        TriageBadge::WaitInTurn => format!(
            "Codex wait-in-turn · {}",
            format_duration(w.turn_elapsed_s)
        ),
        TriageBadge::Lost => w
            .error
            .clone()
            .unwrap_or_else(|| "Supervisor lost".to_string()),
        TriageBadge::Fail => w
            .error
            .clone()
            .unwrap_or_else(|| format!("exit {}", w.exit_code.unwrap_or(1))),
        TriageBadge::Review => format!(
            "{} files · verify:{}",
            w.changed_files_total, w.verification_status
        ),
        TriageBadge::Quiet => format!(
            "Quiet {} · {}",
            format_duration(w.last_event_age_s),
            w.phase
        ),
        TriageBadge::Run => {
            if let Some(tool) = w.active_tools.first() {
                format!("Running {tool} · {}", format_duration(w.turn_elapsed_s))
            } else {
                format!("{} · {}", w.phase, format_duration(w.turn_elapsed_s))
            }
        }
        _ => {
            if !w.label.is_empty() && !w.label.eq_ignore_ascii_case("prompt-input") {
                w.label.clone()
            } else if let Some(ref p) = w.prompt_preview {
                p.clone()
            } else {
                w.phase.clone()
            }
        }
    }
}

fn render_wide(frame: &mut Frame, area: Rect, app: &mut App) {
    let mut constraints = vec![Constraint::Length(2), Constraint::Min(10)];
    if app.drawer_open {
        constraints.push(Constraint::Length(7));
    }
    constraints.push(Constraint::Length(1));

    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(area);

    render_header(frame, vertical[0], app, true);

    if area.width >= 150 && area.height >= 32 && app.inspector_tab == InspectorTab::Steps {
        let cols = Layout::default()
            .direction(Direction::Horizontal)
            .spacing(1)
            .constraints([
                Constraint::Percentage(34),
                Constraint::Percentage(38),
                Constraint::Percentage(28),
            ])
            .split(vertical[1]);
        table::render_workers_table(frame, cols[0], app, false);
        briefing::render_inspector(frame, cols[1], app);
        briefing::render_briefing_pane(frame, cols[2], app);
    } else {
        let main_cols = Layout::default()
            .direction(Direction::Horizontal)
            .spacing(1)
            .constraints([Constraint::Percentage(56), Constraint::Percentage(44)])
            .split(vertical[1]);

        table::render_workers_table(frame, main_cols[0], app, true);
        briefing::render_inspector(frame, main_cols[1], app);
    }

    let footer_idx = if app.drawer_open {
        render_drawer(frame, vertical[2], app);
        3
    } else {
        2
    };
    render_footer(frame, vertical[footer_idx], app);
}

fn render_medium(frame: &mut Frame, area: Rect, app: &mut App) {
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),
            Constraint::Percentage(58),
            Constraint::Min(8),
            Constraint::Length(1),
        ])
        .split(area);

    render_header(frame, vertical[0], app, true);
    table::render_workers_table(frame, vertical[1], app, true);
    if app.drawer_open {
        render_drawer(frame, vertical[2], app);
    } else {
        briefing::render_inspector(frame, vertical[2], app);
    }
    render_footer(frame, vertical[3], app);
}

fn render_narrow(frame: &mut Frame, area: Rect, app: &mut App) {
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(4), Constraint::Length(1)])
        .split(area);

    render_header(frame, vertical[0], app, false);
    if app.drawer_open {
        render_drawer(frame, vertical[1], app);
    } else if app.detail_open {
        briefing::render_inspector(frame, vertical[1], app);
    } else {
        table::render_workers_table(frame, vertical[1], app, false);
    }
    render_footer(frame, vertical[2], app);
}

fn render_header(frame: &mut Frame, area: Rect, app: &App, two_lines: bool) {
    let c = &app.counts;
    let sep = || Span::styled("  ·  ", Style::default().fg(COLOR_MUTED));

    let mut line1 = vec![
        Span::styled(
            " ◆ CLAIVE",
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("   ", Style::default()),
    ];

    let mut has_alert = false;
    if c.ask > 0 {
        line1.push(Span::styled(
            format!("● {} need input", c.ask),
            Style::default().fg(COLOR_ASK).add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }
    if c.unwatched > 0 {
        if has_alert {
            line1.push(sep());
        }
        line1.push(Span::styled(
            format!("▲ {} unwatched", c.unwatched),
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }
    if c.wait_in_turn > 0 {
        if has_alert {
            line1.push(sep());
        }
        line1.push(Span::styled(
            format!("▲ {} wait-turn", c.wait_in_turn),
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }
    if c.lost + c.fail > 0 {
        if has_alert {
            line1.push(sep());
        }
        line1.push(Span::styled(
            format!("✖ {} failed/lost", c.lost + c.fail),
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }
    if c.review > 0 {
        if has_alert {
            line1.push(sep());
        }
        line1.push(Span::styled(
            format!("★ {} review", c.review),
            Style::default()
                .fg(COLOR_REVIEW)
                .add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }
    if c.quiet > 0 {
        if has_alert {
            line1.push(sep());
        }
        line1.push(Span::styled(
            format!("◷ {} quiet", c.quiet),
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
        ));
        has_alert = true;
    }

    if has_alert {
        line1.push(sep());
    }
    if c.running > 0 {
        line1.push(Span::styled(
            format!("▸ {} running", c.running),
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        ));
        line1.push(sep());
    }
    line1.push(Span::styled(
        format!(
            "{} idle · {} parked · {} done",
            c.idle, c.parked, c.done
        ),
        Style::default().fg(COLOR_MUTED),
    ));

    if c.hidden_ask > 0 {
        line1.push(Span::raw("   "));
        line1.push(Span::styled(
            format!("({} need input hidden by filter)", c.hidden_ask),
            Style::default().fg(COLOR_ASK).add_modifier(Modifier::BOLD),
        ));
    }

    if app.snapshot.stale_reason.is_some() {
        line1.push(Span::raw("   "));
        line1.push(Span::styled(
            "✖ STALE SNAPSHOT",
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
        ));
    }

    let mut lines = vec![Line::from(line1)];

    if two_lines {
        let sess = if app.session_scope.label() == "all" {
            "All sessions".to_string()
        } else {
            format!("Session {}", app.session_scope.label())
        };
        let ws = if app.workspace_scope.label() == "all" {
            "All repos".to_string()
        } else {
            app.workspace_scope.label()
        };
        let mode = match app.list_mode.label() {
            "active" => "Active only",
            _ => "Recent",
        };
        let mut line2 = vec![
            Span::styled(format!(" {sess}"), Style::default().fg(COLOR_MUTED)),
            Span::styled(" · ", Style::default().fg(COLOR_MUTED)),
            Span::styled(ws, Style::default().fg(COLOR_MUTED)),
            Span::styled(" · ", Style::default().fg(COLOR_MUTED)),
            Span::styled(mode, Style::default().fg(COLOR_MUTED)),
        ];
        if app.triage_only {
            line2.push(Span::styled(
                " · Actionable only",
                Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
            ));
        }
        if app.group_by_session {
            line2.push(Span::styled(
                " · Grouped",
                Style::default().fg(COLOR_ACCENT),
            ));
        }
        if app.config.actions_enabled() {
            line2.push(Span::styled(
                " · Actions ON",
                Style::default()
                    .fg(COLOR_REVIEW)
                    .add_modifier(Modifier::BOLD),
            ));
        } else {
            line2.push(Span::styled(" · Read-only", Style::default().fg(COLOR_MUTED)));
        }
        if !app.snapshot.live_watchers.is_empty() {
            line2.push(Span::styled(
                format!(" · {} watcher(s)", app.snapshot.live_watchers.len()),
                Style::default().fg(COLOR_ACCENT),
            ));
        }
        if app.search_active || !app.search_query.is_empty() {
            line2.push(Span::styled(
                format!(
                    " · Filter: {}{}",
                    app.search_query,
                    if app.search_active { "_" } else { "" }
                ),
                Style::default()
                    .fg(COLOR_ACCENT)
                    .add_modifier(Modifier::BOLD),
            ));
        }
        lines.push(Line::from(line2));
    }

    frame.render_widget(Paragraph::new(lines), area);
}

fn render_drawer(frame: &mut Frame, area: Rect, app: &App) {
    let focused = app.focus_pane == FocusPane::Drawer;
    let border_style = if focused {
        Style::default().fg(COLOR_ACCENT)
    } else {
        Style::default().fg(COLOR_MUTED)
    };

    let inbox_label = match app.drawer_tab {
        DrawerTab::Inbox => "[Inbox]  Receipts",
        DrawerTab::Receipts => "Inbox  [Receipts]",
    };
    let title = format!(" Drawer: {inbox_label}  (h/l switch · i close) ");
    let block = Block::default()
        .title(clip_width(&title, area.width.saturating_sub(2) as usize))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .border_style(border_style);

    let width = area.width.saturating_sub(4) as usize;
    let lines: Vec<Line> = match app.drawer_tab {
        DrawerTab::Inbox => {
            let items = app.scoped_inbox();
            if items.is_empty() {
                vec![Line::from(Span::styled(
                    "No inbox events in scope.",
                    Style::default().fg(COLOR_MUTED),
                ))]
            } else {
                items
                    .into_iter()
                    .take(40)
                    .map(|ev| {
                        let seq = ev
                            .seq
                            .map(|n| format!("#{n:<4}"))
                            .unwrap_or_else(|| "#-   ".to_string());
                        let time_short = short_iso_time(&ev.at);
                        let who = ev
                            .alias
                            .as_deref()
                            .map(|a| format!("@{a}"))
                            .unwrap_or_else(|| ev.worker_id.clone());
                        let wake_mark = if ev.wake { "●" } else { "·" };
                        let st_style = match ev.status.as_str() {
                            "failed" | "interrupted" => Style::default().fg(COLOR_FAIL),
                            _ if ev.code == Some(3) => Style::default().fg(COLOR_ASK),
                            _ => Style::default().fg(COLOR_NOMINAL),
                        };
                        let summary_budget = width.saturating_sub(36);
                        Line::from(vec![
                            Span::styled(format!("{seq} "), Style::default().fg(COLOR_MUTED)),
                            Span::styled(format!("{wake_mark} "), st_style),
                            Span::styled(format!("{time_short} "), Style::default().fg(COLOR_MUTED)),
                            Span::styled(
                                format!("{:<14} ", clip_width(&who, 14)),
                                Style::default().fg(COLOR_BRIGHT),
                            ),
                            Span::styled(format!("{:<9} ", clip_width(&ev.status, 9)), st_style),
                            Span::styled(
                                clip_width(&ev.summary, summary_budget),
                                Style::default().fg(COLOR_NOMINAL),
                            ),
                        ])
                    })
                    .collect()
            }
        }
        DrawerTab::Receipts => {
            let items = app.recent_receipts();
            if items.is_empty() {
                vec![Line::from(Span::styled(
                    "No push notification receipts recorded yet.",
                    Style::default().fg(COLOR_MUTED),
                ))]
            } else {
                items
                    .into_iter()
                    .take(40)
                    .map(|r| {
                        let time_short = short_iso_time(&r.at);
                        let ok = r.status == "ok" || r.status == "delivered";
                        let st_style = if ok {
                            Style::default().fg(COLOR_NOMINAL)
                        } else {
                            Style::default().fg(COLOR_FAIL)
                        };
                        let dur = r
                            .duration_ms
                            .map(|ms| format!("{ms}ms"))
                            .unwrap_or_else(|| "-".to_string());
                        let detail = r
                            .error
                            .as_deref()
                            .map(|e| format!("err: {e}"))
                            .unwrap_or_else(|| format!("{} {}", r.event_type, r.event_id));
                        let budget = width.saturating_sub(38);
                        Line::from(vec![
                            Span::styled(format!("{time_short} "), Style::default().fg(COLOR_MUTED)),
                            Span::styled(
                                format!("{:<14} ", clip_width(&r.sink, 14)),
                                Style::default().fg(COLOR_BRIGHT),
                            ),
                            Span::styled(format!("{:<9} ", clip_width(&r.status, 9)), st_style),
                            Span::styled(format!("{dur:>6} "), Style::default().fg(COLOR_MUTED)),
                            Span::styled(
                                clip_width(&detail, budget),
                                if ok {
                                    Style::default().fg(COLOR_MUTED)
                                } else {
                                    Style::default().fg(COLOR_FAIL)
                                },
                            ),
                        ])
                    })
                    .collect()
            }
        }
    };

    let p = Paragraph::new(lines)
        .block(block)
        .scroll((app.drawer_scroll, 0));
    frame.render_widget(p, area);
}

fn push_key_hint<'a>(spans: &mut Vec<Span<'a>>, key: &'a str, label: &'a str) {
    if !spans.is_empty() {
        spans.push(Span::styled("  ", Style::default()));
    }
    spans.push(Span::styled(
        key,
        Style::default()
            .fg(COLOR_ACCENT)
            .add_modifier(Modifier::BOLD),
    ));
    spans.push(Span::styled(
        format!(" {label}"),
        Style::default().fg(COLOR_MUTED),
    ));
}

fn render_footer(frame: &mut Frame, area: Rect, app: &App) {
    let width = area.width as usize;
    let line = if let Some(ref flight) = app.in_flight {
        const FRAMES: [&str; 4] = ["⠋", "⠙", "⠹", "⠸"];
        let spin = FRAMES[app.spinner_tick % FRAMES.len()];
        let secs = flight.started_at.elapsed().as_secs();
        let msg = if let Some(ref warn) = flight.slow_warning {
            format!(" {spin} {warn} ")
        } else {
            format!(" {spin} Running: {} ({secs}s) ", flight.label)
        };
        let style = if flight.slow_warning.is_some() {
            Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD)
        };
        Line::from(Span::styled(clip_width(&msg, width), style))
    } else if let Some(toast) = &app.toast {
        let style = if toast.is_error {
            Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD)
        };
        Line::from(Span::styled(
            clip_width(&format!(" ◆ {}", toast.text), width),
            style,
        ))
    } else if app.search_active {
        Line::from(vec![
            Span::styled(
                " / ",
                Style::default()
                    .fg(COLOR_ACCENT)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!("{}_", app.search_query),
                Style::default().fg(COLOR_BRIGHT),
            ),
            Span::styled(
                "   Enter apply · Esc clear",
                Style::default().fg(COLOR_MUTED),
            ),
        ])
    } else {
        let mut spans = vec![Span::raw(" ")];
        if app.config.actions_enabled() {
            push_key_hint(&mut spans, "j/k", "move");
            push_key_hint(&mut spans, "1-4", "tabs");
            push_key_hint(&mut spans, "a", "answer");
            push_key_hint(&mut spans, "m", "send");
            push_key_hint(&mut spans, "S", "steer");
            push_key_hint(&mut spans, "v", "verify");
            push_key_hint(&mut spans, "A", "apply");
            push_key_hint(&mut spans, "t", "actionable");
            push_key_hint(&mut spans, "/", "find");
            push_key_hint(&mut spans, "?", "help");
            push_key_hint(&mut spans, "q", "quit");
        } else {
            push_key_hint(&mut spans, "j/k", "move");
            push_key_hint(&mut spans, "Tab", "focus");
            push_key_hint(&mut spans, "1-4", "tabs");
            push_key_hint(&mut spans, "t", "actionable");
            push_key_hint(&mut spans, "g", "group");
            push_key_hint(&mut spans, "i", "inbox");
            push_key_hint(&mut spans, "y", "copy cmd");
            push_key_hint(&mut spans, "/", "find");
            push_key_hint(&mut spans, "?", "help");
            push_key_hint(&mut spans, "q", "quit");
        }
        Line::from(spans)
    };

    frame.render_widget(Paragraph::new(line), area);
}

fn short_iso_time(iso: &str) -> String {
    if let Some(t_pos) = iso.find('T') {
        let rest = &iso[t_pos + 1..];
        if rest.len() >= 8 {
            return rest[..8].to_string();
        }
    }
    clip_width(iso, 8)
}
