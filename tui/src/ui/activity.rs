//! Activity timeline (`3 Activity`) with paired 1-row action -> outcome lines and progressive disclosure.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

use crate::app::App;
use crate::format::{render_markdown_lines, strip_inline_markdown, StepCategory};
use crate::sanitize::clip_width;
use crate::store::WorkerRecord;

use super::{
    kv_line, COLOR_ACCENT, COLOR_BRIGHT, COLOR_FAIL, COLOR_MUTED, COLOR_NOMINAL, COLOR_REVIEW,
    COLOR_WARN,
};

pub(super) fn build_steps_lines(
    app: &App,
    w: &WorkerRecord,
    body_width: usize,
    body_height: usize,
) -> (Vec<Line<'static>>, u16) {
    let mut lines = Vec::new();
    if app.peek_loading.contains(&w.id) {
        lines.push(Line::from(Span::styled(
            "Loading activity...",
            Style::default().fg(COLOR_ACCENT),
        )));
    }

    let mut selected_line_y: usize = 0;

    if let Some(res) = app.peek_cache.get(&w.id) {
        if let Some(err) = &res.error {
            lines.push(Line::from(Span::styled(
                format!("peek error: {err}"),
                Style::default().fg(COLOR_FAIL),
            )));
        }
        if let Some(intent) = &res.intent {
            lines.push(kv_line(
                "Now",
                clip_width(
                    &strip_inline_markdown(intent),
                    body_width.saturating_sub(15).max(20),
                ),
                Style::default()
                    .fg(COLOR_BRIGHT)
                    .add_modifier(Modifier::BOLD),
            ));
        }
        let (steps, hidden_count) = app.visible_steps_for(&w.id);
        let filter_hint = if hidden_count > 0 {
            format!(
                "Filter: {} ({hidden_count} hidden · [f] cycle)",
                app.activity_filter.label()
            )
        } else {
            format!("Filter: {} ([f] cycle)", app.activity_filter.label())
        };
        lines.push(Line::from(Span::styled(
            format!(
                "{} items · {filter_hint} · [Space/e] expand · [E] all · [R] refresh",
                steps.len(),
            ),
            Style::default().fg(COLOR_MUTED),
        )));
        lines.push(Line::from(""));

        let sel_idx = if steps.is_empty() {
            0
        } else {
            app.selected_step.min(steps.len() - 1)
        };
        let summary_budget = body_width.saturating_sub(15).max(20);

        for (i, step) in steps.iter().enumerate() {
            let is_selected = i == sel_idx;
            let is_expanded = app.expanded_steps.contains(&step.index);
            if is_selected {
                selected_line_y = lines.len();
            }

            let cursor = if is_selected { ">" } else { " " };
            let exp_icon = if is_expanded { "v" } else { "+" };
            let row_bg = if is_selected {
                Style::default().bg(Color::Indexed(237))
            } else {
                Style::default()
            };

            let summary_style = if is_selected {
                row_bg.fg(COLOR_BRIGHT).add_modifier(Modifier::BOLD)
            } else if step.category == StepCategory::Err || step.status.as_deref() == Some("error")
            {
                Style::default().fg(COLOR_FAIL)
            } else if step.category == StepCategory::Say {
                Style::default().fg(COLOR_BRIGHT)
            } else if step.category == StepCategory::Ok || step.category == StepCategory::Event {
                Style::default().fg(COLOR_MUTED)
            } else {
                Style::default().fg(COLOR_NOMINAL)
            };

            let mut row_spans = vec![
                Span::styled(
                    format!("{cursor}{exp_icon}"),
                    row_bg.fg(if is_selected {
                        COLOR_ACCENT
                    } else {
                        COLOR_MUTED
                    }),
                ),
                Span::styled(format!("{:>3} ", step.index), row_bg.fg(COLOR_MUTED)),
                Span::styled(
                    format!("{} ", step.category.tag()),
                    step.category.style().patch(row_bg),
                ),
            ];

            if let Some(ref out) = step.outcome {
                let out_cap = (summary_budget / 2).clamp(14, 38);
                let out_clipped = clip_width(out, out_cap);
                let main_budget = summary_budget
                    .saturating_sub(out_clipped.chars().count() + 4)
                    .max(12);
                row_spans.push(Span::styled(
                    clip_width(&step.summary, main_budget),
                    summary_style,
                ));
                let out_style = match step.status.as_deref() {
                    Some("error") => row_bg.fg(COLOR_FAIL),
                    Some("running") => row_bg.fg(COLOR_WARN),
                    _ => row_bg.fg(COLOR_REVIEW),
                };
                row_spans.push(Span::styled(" -> ", row_bg.fg(COLOR_MUTED)));
                row_spans.push(Span::styled(out_clipped, out_style));
            } else {
                row_spans.push(Span::styled(
                    clip_width(&step.summary, summary_budget),
                    summary_style,
                ));
            }

            lines.push(Line::from(row_spans));

            if is_expanded {
                if step.is_markdown {
                    let joined = step.detail_lines.join("\n");
                    lines.extend(render_markdown_lines(&joined, "       "));
                } else {
                    for dl in &step.detail_lines {
                        let dl_style = if dl.starts_with("$ ") {
                            Style::default().fg(COLOR_ACCENT)
                        } else if dl.starts_with("exit ") && !dl.starts_with("exit 0") {
                            Style::default().fg(COLOR_FAIL)
                        } else {
                            Style::default().fg(COLOR_NOMINAL)
                        };
                        lines.push(Line::from(Span::styled(format!("       {dl}"), dl_style)));
                    }
                }
                lines.push(Line::from(""));
            }
        }

        if steps.is_empty() && res.error.is_none() {
            lines.push(Line::from(Span::styled(
                "No matching activity items. Press [f] to cycle filter.",
                Style::default().fg(COLOR_MUTED),
            )));
        }
    } else if !app.peek_loading.contains(&w.id) {
        lines.push(Line::from(Span::styled(
            "No activity snapshot yet. Press [R] to fetch.",
            Style::default().fg(COLOR_MUTED),
        )));
    }

    // Keep the selected step comfortably visible in the viewport.
    let scroll = if body_height > 4 && selected_line_y >= body_height.saturating_sub(3) {
        (selected_line_y.saturating_sub(body_height / 2)) as u16
    } else {
        0
    };
    (lines, scroll)
}
