//! Human-scannable formatting for worker steps (progressive disclosure) and Markdown logs.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use serde_json::Value;

use crate::commands::{PeekStep, StructuredAction};
use crate::sanitize::{multiline, single_line};

const COLOR_MUTED: Color = Color::DarkGray;
const COLOR_NOMINAL: Color = Color::Gray;
const COLOR_BRIGHT: Color = Color::White;
const COLOR_ACCENT: Color = Color::Cyan;
const COLOR_WARN: Color = Color::Yellow;
const COLOR_FAIL: Color = Color::Red;
const COLOR_OK: Color = Color::Green;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepCategory {
    Test,
    Build,
    Run,
    Edit,
    Read,
    Search,
    Tool,
    Ok,
    Err,
    Say,
    Think,
    Event,
}

impl StepCategory {
    pub fn tag(self) -> &'static str {
        match self {
            StepCategory::Test => "TEST ",
            StepCategory::Build => "BUILD",
            StepCategory::Run => "RUN  ",
            StepCategory::Edit => "EDIT ",
            StepCategory::Read => "READ ",
            StepCategory::Search => "FIND ",
            StepCategory::Tool => "TOOL ",
            StepCategory::Ok => "OK   ",
            StepCategory::Err => "ERR  ",
            StepCategory::Say => "SAY  ",
            StepCategory::Think => "THINK",
            StepCategory::Event => "EVT  ",
        }
    }

    pub fn style(self) -> Style {
        match self {
            StepCategory::Test => Style::default().fg(COLOR_OK).add_modifier(Modifier::BOLD),
            StepCategory::Build => Style::default().fg(COLOR_ACCENT).add_modifier(Modifier::BOLD),
            StepCategory::Run => Style::default().fg(COLOR_ACCENT).add_modifier(Modifier::BOLD),
            StepCategory::Edit => Style::default().fg(COLOR_WARN).add_modifier(Modifier::BOLD),
            StepCategory::Read | StepCategory::Search => Style::default().fg(COLOR_ACCENT),
            StepCategory::Tool => Style::default().fg(COLOR_NOMINAL).add_modifier(Modifier::BOLD),
            StepCategory::Ok => Style::default().fg(COLOR_OK),
            StepCategory::Err => Style::default().fg(COLOR_FAIL).add_modifier(Modifier::BOLD),
            StepCategory::Say => Style::default().fg(COLOR_BRIGHT).add_modifier(Modifier::BOLD),
            StepCategory::Think | StepCategory::Event => Style::default().fg(COLOR_MUTED),
        }
    }
}

#[derive(Debug, Clone)]
pub struct FormattedStep {
    pub index: u64,
    pub category: StepCategory,
    pub summary: String,
    pub outcome: Option<String>,
    pub status: Option<String>,
    pub detail_lines: Vec<String>,
    pub is_markdown: bool,
    pub is_noise: bool,
}

/// Strip raw inline Markdown (`[label](target)`, `**bold**`, `` `code` ``, `_italic_`) for 1-line summaries.
pub fn strip_inline_markdown(input: &str) -> String {
    let chars: Vec<char> = input.chars().collect();
    let mut out = String::with_capacity(input.len());
    let mut i = 0;
    while i < chars.len() {
        // Markdown link: [label](target)
        if chars[i] == '['
            && let Some(close_bracket) = chars[i + 1..].iter().position(|&c| c == ']')
        {
            let b_end = i + 1 + close_bracket;
            if b_end + 1 < chars.len()
                && chars[b_end + 1] == '('
                && let Some(close_paren) = chars[b_end + 2..].iter().position(|&c| c == ')')
            {
                let label: String = chars[i + 1..b_end].iter().collect();
                out.push_str(&strip_inline_markdown(&label));
                i = b_end + 2 + close_paren + 1;
                continue;
            }
        }
        // Inline code: `...`
        if chars[i] == '`'
            && let Some(end) = chars[i + 1..].iter().position(|&c| c == '`')
        {
            let code: String = chars[i + 1..i + 1 + end].iter().collect();
            out.push_str(&code);
            i = i + 2 + end;
            continue;
        }
        // Bold: **...**
        if i + 1 < chars.len()
            && chars[i] == '*'
            && chars[i + 1] == '*'
            && let Some(end) = (i + 2..chars.len().saturating_sub(1))
                .find(|&j| chars[j] == '*' && chars[j + 1] == '*')
        {
            let inner: String = chars[i + 2..end].iter().collect();
            out.push_str(&inner);
            i = end + 2;
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    single_line(&out)
}

/// Convert a structured paired action from `claive peek --json --structured` into a `FormattedStep`.
pub fn format_structured_action(act: &StructuredAction) -> FormattedStep {
    let category = match act.category.as_str() {
        "test" => StepCategory::Test,
        "build" => StepCategory::Build,
        "edit" => StepCategory::Edit,
        "read" => StepCategory::Read,
        "search" => StepCategory::Search,
        "run" => StepCategory::Run,
        "say" => StepCategory::Say,
        "err" => StepCategory::Err,
        _ => StepCategory::Tool,
    };

    if act.kind == "say" {
        let summary = strip_inline_markdown(&act.summary);
        let detail_src = act.output.as_deref().unwrap_or(&act.summary);
        return FormattedStep {
            index: act.id,
            category: StepCategory::Say,
            summary,
            outcome: None,
            status: Some("done".to_string()),
            detail_lines: split_detail_lines(detail_src),
            is_markdown: true,
            is_noise: false,
        };
    }

    if act.kind == "error" {
        let detail_src = act.output.as_deref().unwrap_or(&act.summary);
        return FormattedStep {
            index: act.id,
            category: StepCategory::Err,
            summary: single_line(&act.summary),
            outcome: None,
            status: Some("error".to_string()),
            detail_lines: split_detail_lines(detail_src),
            is_markdown: false,
            is_noise: false,
        };
    }

    let mut detail_lines = Vec::new();
    if let Some(cmd) = &act.command {
        for (i, ln) in split_detail_lines(cmd).into_iter().enumerate() {
            if i == 0 {
                detail_lines.push(format!("$ {ln}"));
            } else {
                detail_lines.push(format!("  {ln}"));
            }
        }
    } else if !act.target.is_empty() {
        let tool = act.tool.as_deref().unwrap_or("tool");
        detail_lines.push(format!("{tool}: {}", act.target));
    }

    if let Some(code) = act.exit_code {
        detail_lines.push(format!("exit {code} ({})", act.status));
    } else if act.status == "running" {
        detail_lines.push("status: running...".to_string());
    } else if act.status == "error" {
        detail_lines.push("status: error".to_string());
    }

    if let Some(out) = &act.output {
        if !detail_lines.is_empty() {
            detail_lines.push(String::new());
        }
        detail_lines.extend(split_detail_lines(out));
    }
    if act.truncated {
        detail_lines.push("(truncated for display)".to_string());
    }

    let outcome = if act.status == "running" {
        Some("running...".to_string())
    } else {
        act.outcome.clone()
    };

    FormattedStep {
        index: act.id,
        category,
        summary: single_line(&act.summary),
        outcome,
        status: Some(act.status.clone()),
        detail_lines,
        is_markdown: false,
        is_noise: false,
    }
}

/// Returns true for low-signal transport events (`response`, `tool_execution_update`, etc.).
pub fn is_noise_step(step: &PeekStep) -> bool {
    if step.kind != "event" {
        return false;
    }
    let t = step.text.trim();
    matches!(
        t,
        "response"
            | "tool_execution_update"
            | "queue_update"
            | "turn_start"
            | "turn_end"
            | "message_start"
            | "message_end"
            | "ping"
            | "heartbeat"
    )
}

/// Strip outer `/bin/bash -lc '...'` or `bash -lc "..."` wrapper for scannable display.
pub fn unwrap_shell_command(raw: &str) -> String {
    let mut s = raw.trim();
    for prefix in [
        "/bin/bash -lc ",
        "/usr/bin/bash -lc ",
        "bash -lc ",
        "/bin/sh -c ",
        "sh -c ",
    ] {
        if let Some(rest) = s.strip_prefix(prefix) {
            s = rest.trim();
            break;
        }
    }
    if s.len() >= 2
        && ((s.starts_with('"') && s.ends_with('"')) || (s.starts_with('\'') && s.ends_with('\'')))
    {
        let inner = &s[1..s.len() - 1];
        return inner.replace("\\\"", "\"").replace("\\n", "\n");
    }
    // Strip unclosed leading quote from truncated shell commands (e.g. `last_tool.detail`).
    if let Some(rest) = s.strip_prefix('"') {
        if !rest.contains('"') || rest.ends_with('\\') {
            return rest.replace("\\\"", "\"").trim().to_string();
        }
    } else if let Some(rest) = s.strip_prefix('\'')
        && !rest.contains('\'')
    {
        return rest.trim().to_string();
    }
    s.to_string()
}

/// Summarize a `last_tool` detail string into a clean 1-line label for Overview & table.
pub fn summarize_tool_detail(tool: &str, status: &str, detail: &str) -> String {
    crate::events::summarize_tool_human(tool, status, detail)
}

/// Parse a `PeekStep` into a scannable 1-line summary + structured expanded detail lines.
pub fn format_step(step: &PeekStep) -> FormattedStep {
    if is_noise_step(step) {
        return FormattedStep {
            index: step.index,
            category: StepCategory::Event,
            summary: single_line(&step.text),
            outcome: None,
            status: None,
            detail_lines: vec![single_line(&step.text)],
            is_markdown: false,
            is_noise: true,
        };
    }

    match step.kind.as_str() {
        "tool_call" => format_tool_call(step),
        "tool_result" => format_tool_result(step),
        "text" => format_text_step(step),
        "thinking" => {
            let summary = first_meaningful_line(&step.text);
            let detail_lines = split_detail_lines(&step.text);
            FormattedStep {
                index: step.index,
                category: StepCategory::Think,
                summary,
                outcome: None,
                status: None,
                detail_lines,
                is_markdown: false,
                is_noise: false,
            }
        }
        _ => FormattedStep {
            index: step.index,
            category: StepCategory::Event,
            summary: first_meaningful_line(&step.text),
            outcome: None,
            status: None,
            detail_lines: split_detail_lines(&step.text),
            is_markdown: false,
            is_noise: false,
        },
    }
}

fn format_tool_call(step: &PeekStep) -> FormattedStep {
    let trimmed = step.text.trim();
    // Common shape from peek.py: `<tool_name> {<json>}`
    let (tool_name, json_str) = match trimmed.find('{') {
        Some(idx) => (trimmed[..idx].trim(), &trimmed[idx..]),
        None => (trimmed, ""),
    };
    let tool_lower = tool_name.to_lowercase();
    let parsed_json: Option<Value> = if !json_str.is_empty() {
        serde_json::from_str(json_str).ok()
    } else {
        None
    };

    if let Some(obj) = parsed_json.as_ref().and_then(|v| v.as_object()) {
        // Shell / bash / command execution
        if let Some(cmd) = obj.get("command").and_then(|v| v.as_str()) {
            let unwrapped = unwrap_shell_command(cmd);
            let first = first_meaningful_line(&unwrapped);
            let mut detail_lines = vec![format!("tool: {tool_name}")];
            detail_lines.push("command:".to_string());
            for l in unwrapped.lines().take(40) {
                detail_lines.push(format!("  {l}"));
            }
            return FormattedStep {
                index: step.index,
                category: StepCategory::Run,
                summary: first,
                outcome: None,
                status: None,
                detail_lines,
                is_markdown: false,
                is_noise: false,
            };
        }

        // File edit / write / read
        if let Some(path) = obj
            .get("path")
            .or_else(|| obj.get("file_path"))
            .or_else(|| obj.get("target_file"))
            .and_then(|v| v.as_str())
        {
            let (cat, extra) = if tool_lower.contains("edit")
                || tool_lower.contains("replace")
                || tool_lower.contains("patch")
            {
                let count = obj
                    .get("edits")
                    .and_then(|v| v.as_array())
                    .map(|a| format!(" ({} edit{})", a.len(), if a.len() == 1 { "" } else { "s" }))
                    .unwrap_or_default();
                (StepCategory::Edit, count)
            } else if tool_lower.contains("write") || tool_lower.contains("create") {
                (StepCategory::Edit, " (write)".to_string())
            } else if tool_lower.contains("read") || tool_lower.contains("view") {
                let range = match (
                    obj.get("offset").and_then(|v| v.as_u64()),
                    obj.get("limit").and_then(|v| v.as_u64()),
                ) {
                    (Some(o), Some(l)) => format!(" (L{o}..{})", o + l),
                    (Some(o), None) => format!(" (from L{o})"),
                    _ => String::new(),
                };
                (StepCategory::Read, range)
            } else if tool_lower.contains("search")
                || tool_lower.contains("grep")
                || tool_lower.contains("find")
            {
                let pat = obj
                    .get("pattern")
                    .or_else(|| obj.get("query"))
                    .and_then(|v| v.as_str())
                    .map(|p| format!(" /{p}/"))
                    .unwrap_or_default();
                (StepCategory::Search, pat)
            } else {
                (StepCategory::Tool, String::new())
            };

            let summary = format!("{}{}", shorten_path(path), extra);
            let pretty = serde_json::to_string_pretty(obj).unwrap_or_default();
            let mut detail_lines = vec![format!("tool: {tool_name}  path: {path}")];
            for l in pretty.lines().take(40) {
                detail_lines.push(format!("  {l}"));
            }
            return FormattedStep {
                index: step.index,
                category: cat,
                summary,
                outcome: None,
                status: None,
                detail_lines,
                is_markdown: false,
                is_noise: false,
            };
        }

        // Search/grep without path
        if let Some(pattern) = obj
            .get("pattern")
            .or_else(|| obj.get("query"))
            .and_then(|v| v.as_str())
        {
            let pretty = serde_json::to_string_pretty(obj).unwrap_or_default();
            let mut detail_lines = vec![format!("tool: {tool_name}")];
            for l in pretty.lines().take(30) {
                detail_lines.push(format!("  {l}"));
            }
            return FormattedStep {
                index: step.index,
                category: StepCategory::Search,
                summary: single_line(pattern),
                outcome: None,
                status: None,
                detail_lines,
                is_markdown: false,
                is_noise: false,
            };
        }
    }

    // Fallback when JSON was elided mid-object (e.g. `[... 259 chars elided ...]`)
    let fallback_summary = extract_elided_tool_summary(tool_name, json_str, trimmed);
    let cat = if matches!(
        tool_lower.as_str(),
        "bash" | "shell" | "exec" | "command_execution"
    ) {
        StepCategory::Run
    } else if tool_lower.contains("edit") || tool_lower.contains("write") {
        StepCategory::Edit
    } else if tool_lower.contains("read") || tool_lower.contains("list") {
        StepCategory::Read
    } else if tool_lower.contains("search") || tool_lower.contains("grep") {
        StepCategory::Search
    } else {
        StepCategory::Tool
    };

    FormattedStep {
        index: step.index,
        category: cat,
        summary: fallback_summary,
        outcome: None,
        status: None,
        detail_lines: split_detail_lines(trimmed),
        is_markdown: false,
        is_noise: false,
    }
}

fn format_tool_result(step: &PeekStep) -> FormattedStep {
    let trimmed = step.text.trim();

    // Codex structured JSON tool_result: {"command":..., "aggregated_output":..., "exit_code":0, "status":"completed"}
    if trimmed.starts_with('{') {
        if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(trimmed) {
            let exit_code = obj.get("exit_code").and_then(|v| v.as_i64());
            let status = obj.get("status").and_then(|v| v.as_str()).unwrap_or("");
            let cmd = obj
                .get("command")
                .and_then(|v| v.as_str())
                .map(unwrap_shell_command);
            let output = obj
                .get("aggregated_output")
                .or_else(|| obj.get("output"))
                .and_then(|v| v.as_str())
                .unwrap_or("");

            let is_err = exit_code.map(|c| c != 0).unwrap_or(false)
                || status == "failed"
                || status == "error";
            let cat = if is_err {
                StepCategory::Err
            } else {
                StepCategory::Ok
            };

            let best_out = pick_best_result_line(output, is_err);
            let exit_tag = exit_code
                .map(|c| if c == 0 { String::new() } else { format!("exit {c}: ") })
                .unwrap_or_default();
            let summary = if !best_out.is_empty() {
                format!("{exit_tag}{best_out}")
            } else if let Some(ref c) = cmd {
                format!("{exit_tag}{} (ok)", first_meaningful_line(c))
            } else if !status.is_empty() {
                format!("{exit_tag}{status}")
            } else {
                "completed".to_string()
            };

            let mut detail_lines = Vec::new();
            if let Some(c) = cmd {
                detail_lines.push(format!("cmd: {}", single_line(&c)));
            }
            if let Some(code) = exit_code {
                detail_lines.push(format!("exit: {code} ({status})"));
            }
            if !output.trim().is_empty() {
                detail_lines.push("output:".to_string());
                for l in output.lines().take(60) {
                    detail_lines.push(format!("  {l}"));
                }
            }
            return FormattedStep {
                index: step.index,
                category: cat,
                summary,
                outcome: None,
                status: Some(if is_err { "error" } else { "ok" }.to_string()),
                detail_lines,
                is_markdown: false,
                is_noise: false,
            };
        }

        // Elided JSON tool_result (e.g. peek.py clipped the middle of aggregated_output)
        let is_err = trimmed.contains("\"exit_code\":1")
            || trimmed.contains("\"exit_code\":2")
            || trimmed.contains("\"status\":\"failed\"");
        let cat = if is_err {
            StepCategory::Err
        } else {
            StepCategory::Ok
        };
        let summary = extract_elided_result_summary(trimmed, is_err);
        return FormattedStep {
            index: step.index,
            category: cat,
            summary,
            outcome: None,
            status: Some(if is_err { "error" } else { "ok" }.to_string()),
            detail_lines: split_detail_lines(trimmed),
            is_markdown: false,
            is_noise: false,
        };
    }

    // Plain-text tool_result (Pi / Claude / AGY)
    let lower = trimmed.to_lowercase();
    let is_err = lower.starts_with("error:")
        || lower.starts_with("err:")
        || lower.contains("command exited with code")
        || lower.contains("failed on line");
    let cat = if is_err {
        StepCategory::Err
    } else {
        StepCategory::Ok
    };
    let line_count = trimmed.lines().filter(|l| !l.trim().is_empty()).count();
    let best = pick_best_result_line(trimmed, is_err);
    let summary = if line_count > 3 && !is_err {
        format!("{best}  ({line_count} lines)")
    } else {
        best
    };

    FormattedStep {
        index: step.index,
        category: cat,
        summary,
        outcome: None,
        status: Some(if is_err { "error" } else { "ok" }.to_string()),
        detail_lines: split_detail_lines(trimmed),
        is_markdown: false,
        is_noise: false,
    }
}

fn format_text_step(step: &PeekStep) -> FormattedStep {
    let summary = strip_inline_markdown(&first_meaningful_line(&step.text));
    let detail_lines = split_detail_lines(&step.text);
    FormattedStep {
        index: step.index,
        category: StepCategory::Say,
        summary,
        outcome: None,
        status: Some("done".to_string()),
        detail_lines,
        is_markdown: true,
        is_noise: false,
    }
}

fn extract_elided_tool_summary(tool_name: &str, json_str: &str, full: &str) -> String {
    if let Some(cmd) = extract_json_string_field(json_str, "command") {
        return first_meaningful_line(&unwrap_shell_command(&cmd));
    }
    if let Some(path) = extract_json_string_field(json_str, "path")
        .or_else(|| extract_json_string_field(json_str, "file_path"))
    {
        return shorten_path(&path);
    }
    if !tool_name.is_empty() && !json_str.is_empty() {
        return format!("{tool_name} {}", single_line(json_str));
    }
    single_line(full)
}

fn extract_elided_result_summary(raw: &str, is_err: bool) -> String {
    if let Some(out) = extract_json_string_field(raw, "aggregated_output") {
        let best = pick_best_result_line(&out, is_err);
        if !best.is_empty() {
            return best;
        }
    }
    if let Some(cmd) = extract_json_string_field(raw, "command") {
        let state = if is_err { "failed" } else { "completed" };
        return format!("{} ({state})", first_meaningful_line(&unwrap_shell_command(&cmd)));
    }
    first_meaningful_line(raw)
}

/// Best-effort extractor for `"key":"value"` even inside truncated/elided JSON snippets.
fn extract_json_string_field(raw: &str, key: &str) -> Option<String> {
    let needle = format!("\"{key}\":\"");
    let start = raw.find(&needle)? + needle.len();
    let mut out = String::new();
    let mut chars = raw[start..].chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push(' '),
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some(other) => out.push(other),
                None => break,
            },
            '"' => break,
            other => out.push(other),
        }
        if out.len() >= 240 {
            break;
        }
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn pick_best_result_line(text: &str, prefer_error: bool) -> String {
    let lines: Vec<String> = text
        .lines()
        .map(single_line)
        .filter(|l| !l.is_empty() && !l.starts_with("[..."))
        .collect();
    if lines.is_empty() {
        return String::new();
    }
    if prefer_error
        && let Some(err_line) = lines.iter().rev().find(|l| {
            let low = l.to_lowercase();
            low.contains("error") || low.contains("failed") || low.contains("exited with code")
        })
    {
        return err_line.clone();
    }
    // Prefer a concise summary line near the end (e.g. "47 passed", "OK", "Successfully...")
    if let Some(summary_line) = lines.iter().rev().take(4).find(|l| {
        let low = l.to_lowercase();
        low.contains("passed")
            || low.contains("failed")
            || low.contains("successfully")
            || low.contains("built in")
            || low.starts_with("ok")
    }) {
        return summary_line.clone();
    }
    lines[0].clone()
}

fn first_meaningful_line(text: &str) -> String {
    for line in text.lines() {
        let cleaned = single_line(line);
        let stripped = cleaned.trim_start_matches('#').trim();
        if !stripped.is_empty() {
            return stripped.to_string();
        }
    }
    single_line(text)
}

fn split_detail_lines(text: &str) -> Vec<String> {
    multiline(text)
        .lines()
        .take(80)
        .map(|l| l.to_string())
        .collect()
}

fn shorten_path(path: &str) -> String {
    let p = path.trim();
    if p.len() <= 48 {
        return p.to_string();
    }
    let parts: Vec<&str> = p.split('/').filter(|s| !s.is_empty()).collect();
    if parts.len() >= 3 {
        format!(".../{}", parts[parts.len() - 3..].join("/"))
    } else {
        p.to_string()
    }
}

/// Render human-scannable Markdown lines for `result.txt` and expanded `text` steps.
pub fn render_markdown_lines(content: &str, indent: &str) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    let mut in_code_block = false;

    for raw in content.lines() {
        let trimmed = raw.trim_start();
        if trimmed.starts_with("```") {
            in_code_block = !in_code_block;
            continue;
        }
        if in_code_block {
            out.push(Line::from(Span::styled(
                format!("{indent}  {raw}"),
                Style::default().fg(COLOR_ACCENT),
            )));
            continue;
        }
        if trimmed.is_empty() {
            out.push(Line::from(""));
            continue;
        }
        if let Some(heading) = strip_heading(trimmed) {
            out.push(Line::from(Span::styled(
                format!("{indent}{heading}"),
                Style::default()
                    .fg(COLOR_ACCENT)
                    .add_modifier(Modifier::BOLD),
            )));
            continue;
        }

        // Bullet or numbered list prefix
        let leading_spaces = raw.len() - trimmed.len();
        let pad = format!("{indent}{}", " ".repeat(leading_spaces.min(8)));
        if let Some(rest) = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
        {
            let mut spans = vec![
                Span::raw(pad),
                Span::styled("- ", Style::default().fg(COLOR_ACCENT)),
            ];
            spans.extend(parse_inline_markdown(rest, Style::default().fg(COLOR_NOMINAL)));
            out.push(Line::from(spans));
            continue;
        }
        if let Some((num_prefix, rest)) = split_numbered_prefix(trimmed) {
            let mut spans = vec![
                Span::raw(pad),
                Span::styled(
                    format!("{num_prefix} "),
                    Style::default()
                        .fg(COLOR_ACCENT)
                        .add_modifier(Modifier::BOLD),
                ),
            ];
            spans.extend(parse_inline_markdown(rest, Style::default().fg(COLOR_BRIGHT)));
            out.push(Line::from(spans));
            continue;
        }

        let mut spans = vec![Span::raw(pad)];
        spans.extend(parse_inline_markdown(
            trimmed,
            Style::default().fg(COLOR_NOMINAL),
        ));
        out.push(Line::from(spans));
    }

    out
}

fn strip_heading(line: &str) -> Option<&str> {
    let hashes = line.bytes().take_while(|&b| b == b'#').count();
    if (1..=6).contains(&hashes) {
        let rest = &line[hashes..];
        if let Some(title) = rest.strip_prefix(' ') {
            return Some(title.trim());
        }
    }
    None
}

fn split_numbered_prefix(line: &str) -> Option<(&str, &str)> {
    let digits = line.bytes().take_while(|b| b.is_ascii_digit()).count();
    if digits == 0 || digits > 3 {
        return None;
    }
    let after = &line[digits..];
    let rest = after.strip_prefix(". ")?;
    Some((&line[..digits + 1], rest))
}

/// Parse inline `[label](target)`, `**bold**`, and `` `code` `` spans cleanly.
fn parse_inline_markdown(input: &str, base: Style) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let mut buf = String::new();
    let chars: Vec<char> = input.chars().collect();
    let mut i = 0;

    let flush = |buf: &mut String, style: Style, spans: &mut Vec<Span<'static>>| {
        if !buf.is_empty() {
            spans.push(Span::styled(std::mem::take(buf), style));
        }
    };

    while i < chars.len() {
        // Markdown link: [label](target) -> styled label without raw target URL clutter.
        if chars[i] == '['
            && let Some(close_bracket) = chars[i + 1..].iter().position(|&c| c == ']')
        {
            let b_end = i + 1 + close_bracket;
            if b_end + 1 < chars.len()
                && chars[b_end + 1] == '('
                && let Some(close_paren) = chars[b_end + 2..].iter().position(|&c| c == ')')
            {
                flush(&mut buf, base, &mut spans);
                let label: String = chars[i + 1..b_end].iter().collect();
                let clean_label = strip_inline_markdown(&label);
                spans.push(Span::styled(
                    clean_label,
                    Style::default()
                        .fg(COLOR_ACCENT)
                        .add_modifier(Modifier::UNDERLINED),
                ));
                i = b_end + 2 + close_paren + 1;
                continue;
            }
        }
        // Inline code: `...`
        if chars[i] == '`'
            && let Some(end) = chars[i + 1..].iter().position(|&c| c == '`')
        {
            flush(&mut buf, base, &mut spans);
            let code: String = chars[i + 1..i + 1 + end].iter().collect();
            spans.push(Span::styled(code, Style::default().fg(COLOR_ACCENT)));
            i = i + 2 + end;
            continue;
        }
        // Bold: **...**
        if i + 1 < chars.len()
            && chars[i] == '*'
            && chars[i + 1] == '*'
            && let Some(end) = (i + 2..chars.len().saturating_sub(1))
                .find(|&j| chars[j] == '*' && chars[j + 1] == '*')
        {
            flush(&mut buf, base, &mut spans);
            let bold_text: String = chars[i + 2..end].iter().collect();
            spans.push(Span::styled(
                bold_text,
                Style::default()
                    .fg(COLOR_BRIGHT)
                    .add_modifier(Modifier::BOLD),
            ));
            i = end + 2;
            continue;
        }
        buf.push(chars[i]);
        i += 1;
    }
    flush(&mut buf, base, &mut spans);
    spans
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk_step(index: u64, kind: &str, text: &str) -> PeekStep {
        PeekStep {
            index,
            kind: kind.to_string(),
            text: text.to_string(),
        }
    }

    #[test]
    fn hides_transport_noise_events() {
        assert!(is_noise_step(&mk_step(1, "event", "response")));
        assert!(is_noise_step(&mk_step(2, "event", "tool_execution_update")));
        assert!(is_noise_step(&mk_step(3, "event", "queue_update")));
        assert!(!is_noise_step(&mk_step(4, "tool_call", "bash {}")));
    }

    #[test]
    fn parses_codex_shell_call_and_result_cleanly() {
        let call = mk_step(
            95,
            "tool_call",
            r#"shell {"command":"/bin/bash -lc 'pnpm --filter @satori-code/cli build'","aggregated_output":"","exit_code":null,"status":"in_progress"}"#,
        );
        let f_call = format_step(&call);
        assert_eq!(f_call.category, StepCategory::Run);
        assert_eq!(f_call.summary, "pnpm --filter @satori-code/cli build");

        let res = mk_step(
            94,
            "tool_result",
            r#"{"command":"/bin/bash -lc 'node --test'","aggregated_output":"PASS a\nCore tests passed\n47 passed\n7 files","exit_code":0,"status":"completed"}"#,
        );
        let f_res = format_step(&res);
        assert_eq!(f_res.category, StepCategory::Ok);
        assert!(f_res.summary.contains("47 passed"), "got: {}", f_res.summary);
    }

    #[test]
    fn parses_pi_edit_and_error_result() {
        let edit = mk_step(
            400,
            "tool_call",
            r#"edit {"edits":[{"newText":"a","oldText":"b"}],"path":"justfile"}"#,
        );
        let f_edit = format_step(&edit);
        assert_eq!(f_edit.category, StepCategory::Edit);
        assert_eq!(f_edit.summary, "justfile (1 edit)");

        let err = mk_step(
            391,
            "tool_result",
            "error: test failed\nRecipe `test-fast` failed on line 36 with exit code 1",
        );
        let f_err = format_step(&err);
        assert_eq!(f_err.category, StepCategory::Err);
        assert!(f_err.summary.contains("failed on line 36"));
    }

    #[test]
    fn renders_markdown_headings_bold_and_code() {
        let md = "### Changes by Finding\n1. **Promoted supervision** (`shell.mjs`)\n- Fixed `runCaptured`";
        let lines = render_markdown_lines(md, "");
        assert_eq!(lines.len(), 3);
        let first_text: String = lines[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(first_text, "Changes by Finding");
        let second_text: String = lines[1].spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(second_text, "1. Promoted supervision (shell.mjs)");
    }

    #[test]
    fn renders_markdown_links_and_strips_inline_markdown_for_summaries() {
        let md = "See [progress.py](bin/claivelib/progress.py) and **tests**.";
        let lines = render_markdown_lines(md, "");
        let rendered: String = lines[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(rendered, "See progress.py and tests.");
        assert_eq!(
            strip_inline_markdown("Updated [`store.rs`](tui/src/store.rs) and **ui**"),
            "Updated store.rs and ui"
        );
    }

    #[test]
    fn formats_structured_paired_actions_cleanly() {
        let act = StructuredAction {
            id: 57,
            kind: "action".to_string(),
            category: "test".to_string(),
            stage: "testing".to_string(),
            tool: Some("shell".to_string()),
            target: "tests/test_peek.py".to_string(),
            summary: "pytest tests/test_peek.py".to_string(),
            outcome: Some("37 passed".to_string()),
            status: "ok".to_string(),
            exit_code: Some(0),
            command: Some("pytest tests/test_peek.py".to_string()),
            output: Some("37 passed in 1.2s".to_string()),
            truncated: false,
        };
        let f = format_structured_action(&act);
        assert_eq!(f.category, StepCategory::Test);
        assert_eq!(f.outcome.as_deref(), Some("37 passed"));
        assert!(f.detail_lines.iter().any(|l| l.starts_with("$ pytest")));
        assert!(f.detail_lines.iter().any(|l| l.contains("exit 0 (ok)")));
    }
}
