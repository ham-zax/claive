//! Interactive modals: Help, Composer (answer/send/steer), Confirm (cancel/close), and Apply preview.

use ratatui::layout::{Constraint, Flex, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Padding, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::{
    ActiveModal, ApplyPreviewModal, ComposerKind, ComposerModal, ConfirmKind, ConfirmModal,
};

use super::{
    kv_line, COLOR_ACCENT, COLOR_ASK, COLOR_BRIGHT, COLOR_FAIL, COLOR_MUTED, COLOR_NOMINAL,
    COLOR_REVIEW, COLOR_WARN,
};

fn centered_rect(area: Rect, max_w: u16, max_h: u16) -> Rect {
    let popup_w = area.width.min(max_w);
    let popup_h = area.height.min(max_h);
    let vert = Layout::vertical([Constraint::Length(popup_h)])
        .flex(Flex::Center)
        .split(area);
    let horiz = Layout::horizontal([Constraint::Length(popup_w)])
        .flex(Flex::Center)
        .split(vert[0]);
    horiz[0]
}

pub(super) fn render_help_modal(frame: &mut Frame, area: Rect, actions_enabled: bool) {
    let popup = centered_rect(area, 74, if actions_enabled { 26 } else { 20 });

    let mode_title = if actions_enabled {
        "Claive Triage Dashboard — Interactive Mode (--actions)"
    } else {
        "Claive Triage Dashboard — Read-Only Mode (use --actions to enable controls)"
    };
    let mut lines = vec![
        Line::from(Span::styled(
            mode_title,
            Style::default()
                .fg(COLOR_ACCENT)
                .add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from("  Click / Scroll  Click repo/view chips, worker rows, tabs; wheel scrolls panes"),
        Line::from("  j / k / ↑ / ↓   Move selection or scroll active pane"),
        Line::from("  Home / End / G  Jump to top / bottom worker"),
        Line::from("  Tab / Shift-Tab Cycle focus (Workers / Inspector / Drawer)"),
        Line::from("  h / l / 1-4     Switch Inspector or Drawer tabs"),
        Line::from("  Enter / Esc     Open detail & mark settled seen / Back"),
        Line::from("  z / Z           Archive/restore selected agent / Archive all settled & stale"),
        Line::from("  o               Toggle sort: Forum (Updated) vs Severity (Priority)"),
        Line::from("  i               Toggle Inbox / Receipts drawer"),
        Line::from("  t / g           Toggle Needs You filter / group by parent session"),
        Line::from("  s / w / r       Cycle session / repo filter / All vs Active vs Archived"),
        Line::from("  /               Filter by alias, ID, label, workspace, model"),
        Line::from("  y / W / D       Copy remediation / cd worktree / git diff (OSC 52)"),
        Line::from("  L / P / R       Cycle logs / Jump to Prompt / Force refresh"),
        Line::from("  Space/e / E / f Activity: expand / expand all / cycle filter (Actions/Milestones/All)"),
        Line::from("  [ / ]           Activity: move cursor without switching pane focus"),
        Line::from("  q / Ctrl-C      Quit dashboard (workers keep running)"),
    ];
    if actions_enabled {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "  Operator Actions (--actions):",
            Style::default()
                .fg(COLOR_REVIEW)
                .add_modifier(Modifier::BOLD),
        )));
        lines.push(Line::from("  a / m / S       Answer ASK / Send (wake|now|note) / Steer"));
        lines.push(Line::from("  p / v           Toggle park / Run independent verify"));
        lines.push(Line::from("  A / x / c       Apply worktree (dry-run preview) / Cancel / Close"));
    }

    frame.render_widget(Clear, popup);
    let help = Paragraph::new(lines)
        .block(
            Block::default()
                .title(" Help (Esc/? to close) ")
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .padding(Padding::horizontal(1))
                .border_style(Style::default().fg(COLOR_ACCENT)),
        )
        .wrap(Wrap { trim: false });
    frame.render_widget(help, popup);
}

pub(super) fn render_action_modal(frame: &mut Frame, area: Rect, modal: &ActiveModal) {
    match modal {
        ActiveModal::Composer(comp) => render_composer_modal(frame, area, comp),
        ActiveModal::Confirm(conf) => render_confirm_modal(frame, area, conf),
        ActiveModal::ApplyPreview(prev) => render_apply_preview_modal(frame, area, prev),
    }
}

fn render_composer_modal(frame: &mut Frame, area: Rect, comp: &ComposerModal) {
    let popup = centered_rect(area, 76, 18);
    let (title, hint) = match &comp.kind {
        ComposerKind::Answer {
            expected_turn,
            is_goal,
            ..
        } => (
            format!(
                " {} {} (turn {}) ",
                if *is_goal { "Goal Answer" } else { "Answer ASK" },
                comp.worker_display,
                expected_turn
            ),
            "Enter: newline · Ctrl-D: submit · Esc: cancel",
        ),
        ComposerKind::Send { mode } => (
            format!(
                " Send Message to {} [mode: {}] ",
                comp.worker_display,
                mode.as_cli_str()
            ),
            "Ctrl-T: cycle wake/now/note · Enter: newline · Ctrl-D: submit · Esc: cancel",
        ),
        ComposerKind::Steer => (
            format!(" Steer Running Worker {} ", comp.worker_display),
            "Enter: newline · Ctrl-D: submit · Esc: cancel",
        ),
    };

    let mut lines: Vec<Line> = Vec::new();
    if let ComposerKind::Answer {
        question_preview, ..
    } = &comp.kind
    {
        lines.push(Line::from(Span::styled(
            format!("Question: {question_preview}"),
            Style::default()
                .fg(COLOR_ASK)
                .add_modifier(Modifier::BOLD),
        )));
    }
    if !comp.watched_by.is_empty() {
        let warn = if comp.hijack_confirmed {
            format!(
                "[ok] Hijack confirmed (WATCHED BY {})",
                comp.watched_by.join(", ")
            )
        } else {
            format!(
                "[!] WATCHED BY {} -- live parent waiting! Press Ctrl-Y to confirm hijack",
                comp.watched_by.join(", ")
            )
        };
        lines.push(Line::from(Span::styled(
            warn,
            Style::default()
                .fg(if comp.hijack_confirmed {
                    COLOR_REVIEW
                } else {
                    COLOR_WARN
                })
                .add_modifier(Modifier::BOLD),
        )));
    }
    lines.push(Line::from(Span::styled(
        hint,
        Style::default().fg(COLOR_MUTED),
    )));
    lines.push(Line::from("─".repeat(popup.width.saturating_sub(2) as usize)));

    let body = format!("{}_", comp.buffer);
    for l in body.split('\n') {
        lines.push(Line::from(Span::styled(
            l.to_string(),
            Style::default().fg(COLOR_BRIGHT),
        )));
    }

    frame.render_widget(Clear, popup);
    let p = Paragraph::new(lines)
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .padding(Padding::horizontal(1))
                .border_style(Style::default().fg(COLOR_ACCENT)),
        )
        .wrap(Wrap { trim: false });
    frame.render_widget(p, popup);
}

fn render_confirm_modal(frame: &mut Frame, area: Rect, conf: &ConfirmModal) {
    let popup = centered_rect(area, 72, 13);
    let (title, accent, effect) = match conf.kind {
        ConfirmKind::Cancel => (
            format!(" Confirm CANCEL {} ({}) ", conf.worker_display, conf.worker_id),
            COLOR_FAIL,
            "Immediately stops the worker process group (SIGTERM, then SIGKILL after 3s). The active turn is aborted.",
        ),
        ConfirmKind::Close => (
            format!(" Confirm CLOSE {} ({}) ", conf.worker_display, conf.worker_id),
            COLOR_WARN,
            "Queues close.request to drain already-assigned turns before ending the reusable supervisor. Session history is retained.",
        ),
    };

    let mut lines = vec![
        Line::from(Span::styled(effect, Style::default().fg(COLOR_BRIGHT))),
        Line::from(""),
    ];
    if !conf.watched_by.is_empty() {
        let warn = if conf.hijack_confirmed {
            format!(
                "[ok] Hijack confirmed (WATCHED BY {})",
                conf.watched_by.join(", ")
            )
        } else {
            format!(
                "[!] WATCHED BY {} -- live parent waiting! Press [H] to confirm hijack first.",
                conf.watched_by.join(", ")
            )
        };
        lines.push(Line::from(Span::styled(
            warn,
            Style::default()
                .fg(if conf.hijack_confirmed {
                    COLOR_REVIEW
                } else {
                    COLOR_WARN
                })
                .add_modifier(Modifier::BOLD),
        )));
        lines.push(Line::from(""));
    }

    let confirm_ready = conf.watched_by.is_empty() || conf.hijack_confirmed;
    let prompt = if confirm_ready {
        "[y / Enter] Confirm   [n / Esc] Abort"
    } else {
        "[H] Confirm hijack   [n / Esc] Abort"
    };
    lines.push(Line::from(Span::styled(
        prompt,
        Style::default().fg(accent).add_modifier(Modifier::BOLD),
    )));

    frame.render_widget(Clear, popup);
    let p = Paragraph::new(lines)
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .padding(Padding::horizontal(1))
                .border_style(Style::default().fg(accent)),
        )
        .wrap(Wrap { trim: false });
    frame.render_widget(p, popup);
}

fn render_apply_preview_modal(frame: &mut Frame, area: Rect, prev: &ApplyPreviewModal) {
    let popup = centered_rect(area, 78, 22);
    let title = format!(
        " Apply Preview: {} ({}) [close={}] ",
        prev.worker_display,
        prev.worker_id,
        if prev.close_after { "yes" } else { "no" }
    );

    let mut lines: Vec<Line> = Vec::new();
    if prev.loading {
        lines.push(Line::from(Span::styled(
            "Running `claive apply --dry-run --json`...",
            Style::default().fg(COLOR_ACCENT),
        )));
    } else if let Some(ref dry) = prev.result {
        if !dry.source.is_empty() {
            lines.push(kv_line(
                "Destination",
                dry.source.clone(),
                Style::default().fg(COLOR_BRIGHT),
            ));
        }
        lines.push(kv_line(
            "Summary",
            format!(
                "{} to apply · {} to delete · {} conflict(s) · {} gate blocker(s)",
                dry.applied.len(),
                dry.deleted.len(),
                dry.conflicts.len(),
                dry.gate_blockers.len()
            ),
            if dry.is_blocked() {
                Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(COLOR_REVIEW).add_modifier(Modifier::BOLD)
            },
        ));
        if let Some(ref err) = dry.error {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("Error: {err}"),
                Style::default().fg(COLOR_FAIL),
            )));
        }
        if !dry.gate_blockers.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                "Gate blockers (Confirm disabled):",
                Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
            )));
            for b in &dry.gate_blockers {
                lines.push(Line::from(Span::styled(
                    format!("  [x] {b}"),
                    Style::default().fg(COLOR_FAIL),
                )));
            }
        }
        if !dry.conflicts.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                "Conflicts (Confirm disabled):",
                Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
            )));
            for c in dry.conflicts.iter().take(6) {
                lines.push(Line::from(Span::styled(
                    format!("  ! {c}"),
                    Style::default().fg(COLOR_FAIL),
                )));
            }
        }
        if !dry.applied.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                "Files to apply:",
                Style::default().fg(COLOR_MUTED),
            )));
            for f in dry.applied.iter().take(6) {
                lines.push(Line::from(Span::styled(
                    format!("  + {f}"),
                    Style::default().fg(COLOR_NOMINAL),
                )));
            }
            if dry.applied.len() > 6 {
                lines.push(Line::from(Span::styled(
                    format!("  ... +{} more", dry.applied.len() - 6),
                    Style::default().fg(COLOR_MUTED),
                )));
            }
        }
        if !dry.deleted.is_empty() {
            lines.push(Line::from(Span::styled(
                "Files to delete:",
                Style::default().fg(COLOR_WARN),
            )));
            for f in dry.deleted.iter().take(4) {
                lines.push(Line::from(Span::styled(
                    format!("  - {f}"),
                    Style::default().fg(COLOR_WARN),
                )));
            }
        }
    }

    if !prev.watched_by.is_empty() {
        lines.push(Line::from(""));
        let warn = if prev.hijack_confirmed {
            format!(
                "[ok] Hijack confirmed (WATCHED BY {})",
                prev.watched_by.join(", ")
            )
        } else {
            format!(
                "[!] WATCHED BY {} -- live parent waiting! Press [H] to confirm hijack.",
                prev.watched_by.join(", ")
            )
        };
        lines.push(Line::from(Span::styled(
            warn,
            Style::default()
                .fg(if prev.hijack_confirmed {
                    COLOR_REVIEW
                } else {
                    COLOR_WARN
                })
                .add_modifier(Modifier::BOLD),
        )));
    }

    lines.push(Line::from(""));
    let blocked = prev.loading || prev.result.as_ref().map(|r| r.is_blocked()).unwrap_or(true);
    let hijack_ok = prev.watched_by.is_empty() || prev.hijack_confirmed;
    let footer = if blocked {
        "[v] Run claive verify first   [C] Toggle --close   [Esc] Abort"
    } else if !hijack_ok {
        "[H] Confirm hijack   [C] Toggle --close   [Esc] Abort"
    } else {
        "[y / Enter] Confirm Apply   [C] Toggle --close   [v] Verify   [Esc] Abort"
    };
    lines.push(Line::from(Span::styled(
        footer,
        Style::default()
            .fg(if blocked { COLOR_WARN } else { COLOR_REVIEW })
            .add_modifier(Modifier::BOLD),
    )));

    frame.render_widget(Clear, popup);
    let border_color = if blocked { COLOR_WARN } else { COLOR_REVIEW };
    let p = Paragraph::new(lines)
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .padding(Padding::horizontal(1))
                .border_style(Style::default().fg(border_color)),
        )
        .wrap(Wrap { trim: false });
    frame.render_widget(p, popup);
}
