//! Clean, self-explanatory Workers table:
//! `STATUS | AGENT | TASK & STATUS | UPDATED | RUNTIME`
//! - Running agents ALWAYS display `▸ RUN` and their live action (never hidden by `UNWATCHED`).
//! - `UPDATED` shows forum-style recency (`just now`, `2m ago`, `Today 09:33`).
//! - Clicking any worker row selects it immediately.

use ratatui::layout::{Constraint, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Cell, Padding, Paragraph, Row, Table, Wrap};
use ratatui::Frame;

use crate::app::{
    format_duration, format_launched_at, format_relative_ago, short_repo_name, short_session_tag,
    worker_is_stale_ask, worker_last_updated, App, ClickAction, ClickRegion, DisplayRow, FocusPane,
    ListMode, TriageBadge,
};
use crate::format::{strip_inline_markdown, summarize_tool_detail};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

use super::{
    badge_spans, row_text_style, COLOR_ACCENT, COLOR_ASK, COLOR_BRIGHT, COLOR_FAIL, COLOR_MUTED,
    COLOR_NOMINAL, COLOR_REVIEW, COLOR_ROW_SEL_BG, COLOR_WARN,
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
        "prompt-input"
            | "summary"
            | "result"
            | "report"
            | "overview"
            | "output"
            | "mission"
            | "task"
            | "goal"
    ) {
        return None;
    }
    Some(clean)
}

fn worker_task_title(w: &WorkerRecord) -> String {
    if let Some(t) = w.mission.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    if !w.is_alias_like_label()
        && let Some(t) = clean_task_candidate(&w.label)
    {
        return t;
    }
    if let Some(t) = w.prompt_preview.as_deref().and_then(clean_task_candidate) {
        return t;
    }
    if let Some(t) = clean_task_candidate(&w.label) {
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

/// Live or settled 2nd-line subtitle under the task title.
/// Critical DX rule: an actively running worker ALWAYS shows what it is actually doing right now,
/// never replacing live progress with a generic `UNWATCHED` scold.
fn worker_status_subtitle(w: &WorkerRecord, badge: TriageBadge, title: &str) -> (String, Style) {
    if w.is_active_turn() {
        let live_style = if badge == TriageBadge::Quiet {
            Style::default().fg(COLOR_WARN)
        } else {
            Style::default().fg(COLOR_ACCENT)
        };
        if let Some(act) = &w.activity {
            let text = if !act.label.is_empty() {
                format!("▸ [{}] {}", act.stage.to_uppercase(), act.label)
            } else {
                format!("▸ {}", act.stage)
            };
            return (text, live_style);
        }
        if !w.active_tools.is_empty() {
            return (format!("▸ Running {}", w.active_tools.join(", ")), live_style);
        }
        if let Some(intent) = w.latest_intent.as_deref().and_then(clean_task_candidate) {
            return (format!("▸ {intent}"), live_style);
        }
        if let Some(lt) = &w.last_tool {
            return (
                format!("▸ {}", summarize_tool_detail(&lt.tool, &lt.status, &lt.detail)),
                live_style,
            );
        }
        return (format!("▸ {}", w.phase), live_style);
    }

    if let Some(ask) = &w.needs_parent {
        return (format!("? {}", ask.question), Style::default().fg(COLOR_ASK));
    }
    if (badge == TriageBadge::Fail || badge == TriageBadge::Lost)
        && let Some(err) = w.best_error_summary()
    {
        return (format!("! {err}"), Style::default().fg(COLOR_FAIL));
    }
    if badge == TriageBadge::Review {
        let f_word = if w.changed_files_total == 1 { "file" } else { "files" };
        return (
            format!(
                "★ Ready for review · {} {f_word} · verify {}",
                w.changed_files_total, w.verification_status
            ),
            Style::default().fg(COLOR_REVIEW),
        );
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
        let f_word = if w.changed_files_total == 1 { "file" } else { "files" };
        return (
            format!(
                "{} {f_word} changed · verify {}",
                w.changed_files_total, w.verification_status
            ),
            Style::default().fg(COLOR_MUTED),
        );
    }
    (w.phase.clone(), Style::default().fg(COLOR_MUTED))
}

fn status_cell_lines(w: &WorkerRecord, badge: TriageBadge, unseen: bool, now: f64) -> (Line<'static>, Line<'static>) {
    if w.is_active_turn() {
        let top = if badge == TriageBadge::Quiet {
            Line::from(badge_spans(TriageBadge::Quiet, unseen))
        } else {
            Line::from(badge_spans(TriageBadge::Run, unseen))
        };
        let sub = match badge {
            TriageBadge::Unwatched => Line::from(Span::styled(
                "  no wait",
                Style::default().fg(COLOR_WARN),
            )),
            TriageBadge::WaitInTurn => Line::from(Span::styled(
                "  wait-turn",
                Style::default().fg(COLOR_WARN),
            )),
            TriageBadge::Quiet => Line::from(Span::styled(
                "  silent",
                Style::default().fg(COLOR_WARN),
            )),
            _ if !w.watched_by.is_empty() => Line::from(Span::styled(
                "  watched",
                Style::default().fg(COLOR_MUTED),
            )),
            _ => Line::from(""),
        };
        return (top, sub);
    }

    let top = Line::from(badge_spans(badge, unseen));
    let sub = if worker_is_stale_ask(w, now) {
        Line::from(Span::styled("  stale", Style::default().fg(COLOR_MUTED)))
    } else {
        Line::from("")
    };
    (top, sub)
}

pub(super) fn render_workers_table(frame: &mut Frame, area: Rect, app: &mut App, wide: bool) {
    app.workers_area = area;
    let focused = app.focus_pane == FocusPane::Workers;
    let border_style = if focused {
        Style::default().fg(COLOR_ACCENT)
    } else {
        Style::default().fg(COLOR_MUTED)
    };

    let mode_tag = if app.list_mode == ListMode::Archived {
        " archived"
    } else if app.triage_only {
        " needing attention"
    } else if app.list_mode == ListMode::Active {
        " active"
    } else {
        ""
    };
    let title = format!(" Workers ({}{}) ", app.visible_workers.len(), mode_tag);
    let block = Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .border_style(border_style);

    if app.display_rows.is_empty() {
        let msg = if app.list_mode == ListMode::Archived {
            "No archived agents yet. Press [z] on any settled agent (or [Z] for all old ones) to archive it."
                .to_string()
        } else if app.list_mode == ListMode::Active {
            format!(
                "No agents currently running ({} total in scope). Click [All] or press [r].",
                app.counts.total
            )
        } else if app.triage_only {
            format!(
                "All clear — {} running, {} idle, {} done. Press [t] to show all.",
                app.counts.active_total, app.counts.idle, app.counts.done
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

    let status_col_w: u16 = if compact_cols { 9 } else { 10 };
    let agent_col_w: u16 = if compact_cols {
        13
    } else if roomier {
        21
    } else if show_runtime_col {
        18
    } else {
        16
    };
    let updated_col_w: u16 = if show_runtime_col { 12 } else { 9 };
    let runtime_col_w: u16 = if show_runtime_col { 10 } else { 0 };
    let col_spacing: u16 = if roomier { 2 } else { 1 };
    let num_cols: u16 = if show_runtime_col { 5 } else { 4 };
    let fixed_w = status_col_w
        + agent_col_w
        + updated_col_w
        + runtime_col_w
        + 2 // highlight symbol
        + col_spacing * (num_cols - 1);
    let task_budget = (inner_w as usize).saturating_sub(fixed_w as usize).max(12);

    let header_style = Style::default()
        .fg(COLOR_MUTED)
        .add_modifier(Modifier::BOLD);
    let header_cells: Vec<&str> = if show_runtime_col {
        vec!["STATUS", "AGENT", "TASK & STATUS", "UPDATED", "RUNTIME"]
    } else if compact_cols {
        vec!["STATUS", "AGENT", "TASK", "UPD"]
    } else {
        vec!["STATUS", "AGENT", "TASK & STATUS", "UPDATED"]
    };
    let header = Row::new(
        header_cells
            .into_iter()
            .map(|h| Cell::from(h).style(header_style)),
    )
    .height(1)
    .bottom_margin(1);

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

                let upd_ts = worker_last_updated(w);
                let upd_age = upd_ts.map(|ts| (now - ts).max(0.0)).or(w.last_event_age_s);
                let upd_ago = format_relative_ago(upd_age);
                let upd_clock = format_launched_at(upd_ts, now);
                let upd_ago_style = match upd_age {
                    Some(a) if a < 120.0 => Style::default()
                        .fg(COLOR_ACCENT)
                        .add_modifier(Modifier::BOLD),
                    Some(a) if a < 3600.0 => Style::default().fg(COLOR_BRIGHT),
                    _ => Style::default().fg(COLOR_MUTED),
                };

                // Live ticking runtime for active turns.
                let live_run_secs = if w.is_active_turn() {
                    w.turn_started_at
                        .or(w.started_at)
                        .map(|s| (now - s).max(0.0))
                        .or(w.turn_elapsed_s)
                        .or(w.elapsed_s)
                } else {
                    w.turn_elapsed_s.or(w.elapsed_s)
                };
                let run_dur = format_duration(live_run_secs);

                let (status_top, status_sub) = status_cell_lines(w, *badge, *unseen, now);

                if two_line_rows {
                    let repo = w
                        .workspace
                        .as_deref()
                        .map(short_repo_name)
                        .unwrap_or_default();
                    let sess_part = w
                        .parent_session
                        .as_deref()
                        .map(short_session_tag)
                        .filter(|s| !s.is_empty());
                    let agent_sub = match (repo.is_empty(), sess_part) {
                        (false, Some(s)) if agent_col_w >= 18 => {
                            format!("{repo}·{}·{s}", w.engine)
                        }
                        (false, _) => format!("{repo} · {}", w.engine),
                        (true, Some(s)) => format!("{} · {s}", w.engine),
                        (true, None) => w.engine.clone(),
                    };

                    let status_cell = Cell::from(Text::from(vec![status_top, status_sub]));
                    let agent_cell = Cell::from(Text::from(vec![
                        Line::from(Span::styled(name, text_style)),
                        Line::from(Span::styled(
                            clip_width(&agent_sub, agent_col_w as usize),
                            Style::default().fg(COLOR_MUTED),
                        )),
                    ]));
                    let task_cell = Cell::from(Text::from(vec![
                        Line::from(Span::styled(clip_width(&title, task_budget), text_style)),
                        Line::from(Span::styled(
                            clip_width(&subtitle, task_budget),
                            sub_style,
                        )),
                    ]));

                    let mut cells = vec![status_cell, agent_cell, task_cell];
                    if show_runtime_col {
                        let updated_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(upd_ago, upd_ago_style)),
                            Line::from(Span::styled(
                                upd_clock,
                                Style::default().fg(COLOR_MUTED),
                            )),
                        ]));
                        let (rt_top, rt_top_style, rt_bot) = if w.is_active_turn() {
                            (
                                format!("▸ {run_dur}"),
                                Style::default()
                                    .fg(COLOR_ACCENT)
                                    .add_modifier(Modifier::BOLD),
                                format!("turn {}", w.turn),
                            )
                        } else {
                            let meta = if w.changed_files_total > 0 {
                                format!("t{} · {}f", w.turn, w.changed_files_total)
                            } else {
                                format!("turn {}", w.turn)
                            };
                            (run_dur, Style::default().fg(COLOR_NOMINAL), meta)
                        };
                        let runtime_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(rt_top, rt_top_style)),
                            Line::from(Span::styled(rt_bot, Style::default().fg(COLOR_MUTED))),
                        ]));
                        cells.push(updated_cell);
                        cells.push(runtime_cell);
                    } else {
                        let compact_time_cell = Cell::from(Text::from(vec![
                            Line::from(Span::styled(
                                clip_width(&upd_ago, updated_col_w as usize),
                                upd_ago_style,
                            )),
                            Line::from(Span::styled(
                                clip_width(&run_dur, updated_col_w as usize),
                                Style::default().fg(COLOR_MUTED),
                            )),
                        ]));
                        cells.push(compact_time_cell);
                    }

                    Row::new(cells).height(2).bottom_margin(row_gap)
                } else {
                    let single_task = if badge.is_anomalous() || w.is_active_turn() {
                        format!("{title} — {subtitle}")
                    } else {
                        title
                    };
                    let mut cells = vec![
                        Cell::from(status_top),
                        Cell::from(name).style(text_style),
                        Cell::from(clip_width(&single_task, task_budget)).style(text_style),
                        Cell::from(clip_width(&upd_ago, updated_col_w as usize))
                            .style(upd_ago_style),
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
        Constraint::Length(updated_col_w),
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

    // Register clickable row regions using the rendered table scroll offset.
    let inner_top = area.y.saturating_add(1); // top border
    let rows_start_y = inner_top.saturating_add(2); // header(1) + bottom_margin(1)
    let inner_bottom = area.y.saturating_add(area.height).saturating_sub(1);
    let offset = app.table_state.offset();
    let mut y = rows_start_y;
    for (row_idx, row) in app.display_rows.iter().enumerate().skip(offset) {
        if y >= inner_bottom {
            break;
        }
        let h: u16 = match row {
            DisplayRow::GroupHeader { .. } => 1,
            DisplayRow::Worker { .. } => {
                if two_line_rows {
                    2
                } else {
                    1
                }
            }
        };
        if matches!(row, DisplayRow::Worker { .. }) {
            let click_h = h.min(inner_bottom.saturating_sub(y));
            if click_h > 0 {
                app.click_regions.push(ClickRegion {
                    rect: Rect::new(
                        area.x.saturating_add(1),
                        y,
                        area.width.saturating_sub(2),
                        click_h,
                    ),
                    action: ClickAction::SelectWorkerRow(row_idx),
                });
            }
        }
        let stride = match row {
            DisplayRow::GroupHeader { .. } => 1,
            DisplayRow::Worker { .. } => h.saturating_add(if two_line_rows { row_gap } else { 0 }),
        };
        y = y.saturating_add(stride);
    }
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
