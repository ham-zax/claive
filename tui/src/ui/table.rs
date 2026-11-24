//! Clean, self-explanatory Workers table:
//! `STATUS | AGENT | TASK & STATUS | LAUNCHED | RUNTIME`
//! Every row uses predictable 2-line hierarchy (Mission on top, Live Status/Outcome underneath)
//! and human-readable local launch timestamps + runtime durations.

use ratatui::layout::{Constraint, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Cell, Padding, Paragraph, Row, Table, Wrap};
use ratatui::Frame;

use crate::app::{
    format_duration, format_launched_at, format_relative_ago, short_workspace, App, DisplayRow,
    FocusPane, TriageBadge,
};
use crate::format::{strip_inline_markdown, summarize_tool_detail};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

use super::{
    badge_spans, row_text_style, COLOR_ACCENT, COLOR_ASK, COLOR_FAIL, COLOR_MUTED, COLOR_NOMINAL,
    COLOR_REVIEW, COLOR_ROW_SEL_BG, COLOR_WARN,
};

fn clean_task_candidate(raw: &str) -> Option<String> {
    let trimmed = raw.trim().trim_start_matches('#').trim();
    let clean = strip_inline_markdown(trimmed);
    if clean.is_empty() {
        return None;
    }
    let low = clean.to_ascii_lowercase();
    if matches!(
        low.as_str(),
        "prompt-input" | "summary" | "result" | "report" | "overview" | "output" | "mission" | "task" | "goal"
    ) {
        return None;
    }
    Some(clean)
}

fn worker_task_title(w: &WorkerRecord) -> String {
    if let Some(t) = w.mission.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    if let Some(t) = clean_task_candidate(&w.label) {
        return t;
    }
    if let Some(t) = w.prompt_preview.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    if let Some(t) = w.report_summary.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    if let Some(t) = w.latest_intent.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    format!("{} worker", w.engine)
}

/// Produce a clean, human-readable 2nd-line status / blocker / outcome with semantic color.
fn worker_status_subtitle(w: &WorkerRecord, badge: TriageBadge, title: &str) -> (String, Style) {
    if let Some(ask) = &w.needs_parent {
        return (
            format!("? {}", ask.question),
            Style::default().fg(COLOR_ASK),
        );
    }
    if badge == TriageBadge::Unwatched {
        return (
            "! No live `claive wait` watching this worker".to_string(),
            Style::default().fg(COLOR_WARN),
        );
    }
    if badge == TriageBadge::WaitInTurn {
        return (
            "! Codex parent — wait in-turn or press [p] to park".to_string(),
            Style::default().fg(COLOR_WARN),
        );
    }
    if (badge == TriageBadge::Fail || badge == TriageBadge::Lost)
        && let Some(err) = &w.error
    {
        return (format!("! {err}"), Style::default().fg(COLOR_FAIL));
    }
    if badge == TriageBadge::Review {
        return (
            format!(
                "★ Ready for review · {} files · verify {}",
                w.changed_files_total, w.verification_status
            ),
            Style::default().fg(COLOR_REVIEW),
        );
    }
    if matches!(badge, TriageBadge::Run | TriageBadge::Quiet) {
        if let Some(act) = &w.activity {
            let text = if !act.label.is_empty() {
                format!("▸ [{}] {}", act.stage.to_uppercase(), act.label)
            } else {
                format!("▸ {}", act.stage)
            };
            let style = if badge == TriageBadge::Quiet {
                Style::default().fg(COLOR_WARN)
            } else {
                Style::default().fg(COLOR_ACCENT)
            };
            return (text, style);
        }
        if let Some(intent) = &w.latest_intent {
            return (
                format!("▸ {}", strip_inline_markdown(intent)),
                Style::default().fg(COLOR_ACCENT),
            );
        }
        if !w.active_tools.is_empty() {
            return (
                format!("▸ Running {}", w.active_tools.join(", ")),
                Style::default().fg(COLOR_ACCENT),
            );
        }
    }

    // Settled / Parked / Idle: prefer clean human summary over raw tool command.
    if let Some(clean) = w.report_summary.as_deref().and_then(clean_task_candidate)
        && clean != title
    {
        return (clean, Style::default().fg(COLOR_MUTED));
    }
    if let Some(clean) = w.latest_intent.as_deref().and_then(clean_task_candidate)
        && clean != title
    {
        return (clean, Style::default().fg(COLOR_MUTED));
    }
    if let Some(chk) = &w.last_check {
        let icon = if chk.status == "passed" { "✓" } else { "✖" };
        let style = if chk.status == "passed" {
            Style::default().fg(COLOR_REVIEW)
        } else {
            Style::default().fg(COLOR_FAIL)
        };
        let msg = if !chk.summary.is_empty() {
            format!("{icon} {}", chk.summary)
        } else {
            format!("{icon} {}", chk.command)
        };
        return (msg, style);
    }
    if let Some(lt) = &w.last_tool {
        let clean = summarize_tool_detail(&lt.tool, &lt.status, &lt.detail);
        let style = if matches!(lt.status.as_str(), "error" | "failed") {
            Style::default().fg(COLOR_WARN)
        } else {
            Style::default().fg(COLOR_MUTED)
        };
        return (clean, style);
    }
    if w.changed_files_total > 0 {
        return (
            format!(
                "{} files changed · verify {}",
                w.changed_files_total, w.verification_status
            ),
            Style::default().fg(COLOR_MUTED),
        );
    }
    (w.phase.clone(), Style::default().fg(COLOR_MUTED))
}

pub(super) fn render_workers_table(frame: &mut Frame, area: Rect, app: &mut App, wide: bool) {
    let focused = app.focus_pane == FocusPane::Workers;
    let border_style = if focused {
        Style::default().fg(COLOR_ACCENT)
    } else {
        Style::default().fg(COLOR_MUTED)
    };

    let title = format!(
        " Workers ({}{}) ",
        app.visible_workers.len(),
        if app.triage_only { " needing attention" } else { "" }
    );
    let block = Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .border_style(border_style);

    if app.display_rows.is_empty() {
        let msg = if app.triage_only {
            format!(
                "All clear — {} running, {} idle, {} done. Press [t] to show all.",
                app.counts.running, app.counts.idle, app.counts.done
            )
        } else if !app.search_query.is_empty() {
            format!("No agents matching '/{}'. Press Esc to clear.", app.search_query)
        } else {
            format!(
                "No agents in {}. Launch via `claive start` or `claive open`.",
                app.snapshot.claive_dir.display()
            )
        };
        let empty = Paragraph::new(msg)
            .style(Style::default().fg(COLOR_MUTED))
            .wrap(Wrap { trim: true })
            .block(block);
        frame.render_widget(empty, area);
        return;
    }

    let inner_w = area.width.saturating_sub(4);
    let show_runtime_col = wide && inner_w >= 80;
    let compact_cols = inner_w < 58;
    let roomier = inner_w >= 96;

    let has_long_badge = app.display_rows.iter().any(|r| {
        matches!(
            r,
            DisplayRow::Worker {
                badge: TriageBadge::Unwatched | TriageBadge::WaitInTurn,
                ..
            }
        )
    });
    let status_col_w: u16 = if has_long_badge { 11 } else { 9 };
    let agent_col_w: u16 = if compact_cols {
        13
    } else if roomier {
        20
    } else if show_runtime_col {
        18
    } else {
        16
    };
    let launched_col_w: u16 = if show_runtime_col { 12 } else { 9 };
    let runtime_col_w: u16 = if show_runtime_col { 10 } else { 0 };
    let col_spacing: u16 = if roomier { 2 } else { 1 };
    let num_cols: u16 = if show_runtime_col { 5 } else { 4 };
    let fixed_w = status_col_w
        + agent_col_w
        + launched_col_w
        + runtime_col_w
        + 2 // highlight symbol
        + col_spacing * (num_cols - 1);
    let task_budget = (inner_w as usize).saturating_sub(fixed_w as usize).max(12);

    let header_style = Style::default()
        .fg(COLOR_MUTED)
        .add_modifier(Modifier::BOLD);
    let header_cells: Vec<&str> = if show_runtime_col {
        vec!["STATUS", "AGENT", "TASK & STATUS", "LAUNCHED", "RUNTIME"]
    } else if compact_cols {
        vec!["STATUS", "AGENT", "TASK", "AGO"]
    } else {
        vec!["STATUS", "AGENT", "TASK & STATUS", "LAUNCHED"]
    };
    let header = Row::new(
        header_cells
            .into_iter()
            .map(|h| Cell::from(h).style(header_style)),
    )
    .height(1)
    .bottom_margin(1);

    // Use airy 2-line rows with breathing room whenever vertical height permits.
    let two_line_rows = area.height >= 14;
    let row_gap: u16 = if two_line_rows && area.height >= 20 { 1 } else { 0 };
    let now = app.snapshot.refreshed_at;

    let rows: Vec<Row> = app
        .display_rows
        .iter()
        .map(|row| match row {
            DisplayRow::GroupHeader { session, summary } => {
                let text = format!("▾ {session}");
                let style = Style::default()
                    .fg(COLOR_ACCENT)
                    .add_modifier(Modifier::BOLD);
                let mut cells = vec![
                    Cell::from(""),
                    Cell::from(clip_width(&text, agent_col_w as usize)).style(style),
                    Cell::from(clip_width(summary, task_budget))
                        .style(Style::default().fg(COLOR_MUTED)),
                    Cell::from(""),
                ];
                if show_runtime_col {
                    cells.push(Cell::from(""));
                }
                Row::new(cells).height(1)
            }
            DisplayRow::Worker {
                worker_idx,
                badge,
                unseen,
            } => {
                let w = &app.snapshot.workers[*worker_idx];
                let name = clip_width(&w.display_name(), agent_col_w as usize);
                let title = worker_task_title(w);
                let (subtitle, sub_style) = worker_status_subtitle(w, *badge, &title);
                let text_style = row_text_style(*badge);

                let launch_age = w.started_at.map(|s| (now - s).max(0.0));
                let launched_clock = format_launched_at(w.started_at, now);
                let launched_ago = format_relative_ago(launch_age.or(w.last_event_age_s));
                let run_dur = format_duration(w.turn_elapsed_s.or(w.elapsed_s));

                let status_line1 = Line::from(badge_spans(*badge, *unseen));

                if two_line_rows {
                    let ws = w
                        .workspace
                        .as_deref()
                        .map(short_workspace)
                        .unwrap_or_default();
                    let agent_sub = if !ws.is_empty() {
                        format!("{ws} · {}", w.engine)
                    } else {
                        w.engine.clone()
                    };

                    let status_cell = Cell::from(Text::from(vec![status_line1, Line::from("")]));
                    let agent_cell = Cell::from(Text::from(vec![
                        Line::from(Span::styled(name, text_style)),
                        Line::from(Span::styled(
                            clip_width(&agent_sub, agent_col_w as usize),
                            Style::default().fg(COLOR_MUTED),
                        )),
                    ]));
                    let task_cell = Cell::from(Text::from(vec![
                        Line::from(Span::styled(
                            clip_width(&title, task_budget),
                            text_style,
                        )),
                        Line::from(Span::styled(
                            clip_width(&subtitle, task_budget),
                            sub_style,
                        )),
                    ]));

                    let mut cells = vec![status_cell, agent_cell, task_cell];
                    if show_runtime_col {
                        let launched_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(
                                launched_clock,
                                Style::default().fg(COLOR_NOMINAL),
                            )),
                            Line::from(Span::styled(
                                launched_ago,
                                Style::default().fg(COLOR_MUTED),
                            )),
                        ]));
                        let (rt_top, rt_top_style, rt_bot, rt_bot_style) = if w.is_active_turn() {
                            let seen = format_relative_ago(w.last_event_age_s);
                            let bot_s = if *badge == TriageBadge::Quiet {
                                Style::default().fg(COLOR_WARN)
                            } else {
                                Style::default().fg(COLOR_MUTED)
                            };
                            (
                                format!("▸ {run_dur}"),
                                Style::default()
                                    .fg(COLOR_ACCENT)
                                    .add_modifier(Modifier::BOLD),
                                seen,
                                bot_s,
                            )
                        } else {
                            let meta = if w.changed_files_total > 0 {
                                format!("t{} · {}f", w.turn, w.changed_files_total)
                            } else {
                                format!("turn {}", w.turn)
                            };
                            (
                                run_dur,
                                Style::default().fg(COLOR_NOMINAL),
                                meta,
                                Style::default().fg(COLOR_MUTED),
                            )
                        };
                        let runtime_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(rt_top, rt_top_style)),
                            Line::from(Span::styled(rt_bot, rt_bot_style)),
                        ]));
                        cells.push(launched_cell);
                        cells.push(runtime_cell);
                    } else {
                        let compact_time_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(
                                clip_width(&launched_ago, launched_col_w as usize),
                                Style::default().fg(COLOR_NOMINAL),
                            )),
                            Line::from(Span::styled(
                                clip_width(&format!("ran {run_dur}"), launched_col_w as usize),
                                Style::default().fg(COLOR_MUTED),
                            )),
                        ]));
                        cells.push(compact_time_cell);
                    }

                    Row::new(cells).height(2).bottom_margin(row_gap)
                } else {
                    let single_task = if badge.is_anomalous() {
                        format!("{title} — {subtitle}")
                    } else {
                        title
                    };
                    let mut cells = vec![
                        Cell::from(status_line1),
                        Cell::from(name).style(text_style),
                        Cell::from(clip_width(&single_task, task_budget)).style(text_style),
                        Cell::from(clip_width(&launched_ago, launched_col_w as usize))
                            .style(Style::default().fg(COLOR_MUTED)),
                    ];
                    if show_runtime_col {
                        cells.push(Cell::from(run_dur).style(Style::default().fg(COLOR_MUTED)));
                    }
                    Row::new(cells).height(1)
                }
            }
        })
        .collect();

    let mut widths = vec![
        Constraint::Length(status_col_w),
        Constraint::Length(agent_col_w),
        Constraint::Fill(1),
        Constraint::Length(launched_col_w),
    ];
    if show_runtime_col {
        widths.push(Constraint::Length(runtime_col_w));
    }

    let table = Table::new(rows, widths)
        .header(header)
        .column_spacing(col_spacing)
        .block(block)
        .row_highlight_style(Style::default().bg(COLOR_ROW_SEL_BG))
        .highlight_symbol(Span::styled(
            "▎ ",
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        ));

    frame.render_stateful_widget(table, area, &mut app.table_state);
}

#[cfg(test)]
mod tests {
    use super::clean_task_candidate;

    #[test]
    fn strips_markdown_headings_and_skips_generic_headers() {
        assert_eq!(clean_task_candidate("### Summary"), None);
        assert_eq!(clean_task_candidate("## Result"), None);
        assert_eq!(clean_task_candidate("prompt-input"), None);
        assert_eq!(
            clean_task_candidate("### Fix `lock.ts` recovery"),
            Some("Fix lock.ts recovery".to_string())
        );
    }
}
