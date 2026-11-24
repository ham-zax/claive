//! Native incremental `events.jsonl` tailer, credential redactor, and provider-ID-paired
//! action extractor for `claive-tui` (parity with `bin/claivelib/{progress,redaction,intent}.py`).

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{BufRead, BufReader, ErrorKind, Seek, SeekFrom};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::commands::{PeekResult, StructuredAction};
use crate::format::strip_inline_markdown;
use crate::sanitize::{multiline, single_line, strip_controls_opts};

pub const MAX_COMMAND_CHARS: usize = 2000;
pub const MAX_OUTPUT_CHARS: usize = 2400;
pub const MAX_SUMMARY_CHARS: usize = 140;
pub const MAX_TARGET_CHARS: usize = 72;
pub const MAX_OPEN_CALLS: usize = 64;
pub const MAX_STORED_ACTIONS: usize = 160;
const INTENT_MAX_CHARS: usize = 120;
const REDACTED: &str = "[redacted]";

const EDIT_TOOLS: &[&str] = &[
    "edit",
    "write",
    "create",
    "replace",
    "patch",
    "apply_patch",
    "file_change",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "dev_edit",
    "dev_write",
    "edit_file",
    "write_file",
];
const READ_TOOLS: &[&str] = &[
    "read",
    "view",
    "read_file",
    "view_file",
    "list_directory",
    "list_dir",
    "dev_read",
    "dev_list_directory",
    "ls",
    "cat",
];
const SEARCH_TOOLS: &[&str] = &[
    "search",
    "grep",
    "rg",
    "find",
    "grep_search",
    "find_by_name",
    "dev_search",
    "glob",
    "web_search",
    "websearch",
];
const SHELL_TOOLS: &[&str] = &[
    "shell",
    "bash",
    "exec",
    "command_execution",
    "run_command",
    "terminal",
    "dev_exec",
    "dev_bash",
];
const TEST_PATTERNS: &[&str] = &[
    "pytest",
    "unittest",
    "vitest",
    "jest",
    "mocha",
    "cargo test",
    "go test",
    "npm test",
    "pnpm test",
    "yarn test",
    "just test",
    "just dash-test",
    "node --test",
    "node --import tsx --test",
    "tsx --test",
    "rspec",
];
const BUILD_PATTERNS: &[&str] = &[
    "cargo build",
    "cargo check",
    "cargo clippy",
    "tsc",
    "oxlint",
    "eslint",
    "ruff",
    "shellcheck",
    "mypy",
    "pyright",
    "pnpm run check",
    "npm run check",
    "just check",
    "just lint",
    "just dash-build",
    " build",
    " lint",
    "compileall",
];

/// Clean, human-readable 1-line summary of a `last_tool` record (strips shell wrappers, quotes, and pipe soup).
pub fn summarize_tool_human(tool: &str, status: &str, detail: &str) -> String {
    let cleaned_detail = unwrap_shell(detail);
    if cleaned_detail.is_empty() {
        return format!("{tool} ({status})");
    }
    let tool_low = tool.trim().to_ascii_lowercase();
    let is_shell = SHELL_TOOLS.contains(&tool_low.as_str()) || tool_low == "run";
    let cmd_arg = if is_shell {
        Some(cleaned_detail.as_str())
    } else {
        None
    };
    let path_arg = if !is_shell {
        Some(cleaned_detail.as_str())
    } else {
        None
    };
    let info = classify_tool_action(tool, cmd_arg, path_arg, None, None);
    let verb = match info.category {
        "test" => "Tested",
        "build" => "Built",
        "edit" => "Edited",
        "read" => "Inspected",
        "search" => "Searched",
        "run" => "Ran",
        _ => "Used",
    };
    // Keep only the first clean command segment so chained `; nl -ba ... | sed -n ...` doesn't clutter the row.
    let short_summary = split_first_shell_segment(&info.summary);
    let body = if short_summary.is_empty() {
        info.summary.as_str()
    } else {
        short_summary
    };
    let failed = matches!(status.to_ascii_lowercase().as_str(), "error" | "failed");
    if failed {
        format!("{verb} {} (failed)", clean_line(body, 68))
    } else {
        format!("{verb} {}", clean_line(body, 74))
    }
}

/// Returns true for engines supported natively by `EventsTailer`.
pub fn supports_native_engine(engine: &str) -> bool {
    matches!(
        engine.trim().to_ascii_lowercase().as_str(),
        "codex" | "pi" | "claude" | "agy"
    )
}

// ---------------------------------------------------------------------------
// Credential redaction (pure Rust parity with `bin/claivelib/redaction.py`)
// ---------------------------------------------------------------------------

fn contains_secret_key_name(key: &str) -> bool {
    let low = key.to_ascii_lowercase();
    low.contains("secret")
        || low.contains("token")
        || low.contains("password")
        || low.contains("passwd")
        || low.contains("api_key")
        || low.contains("apikey")
}

fn should_mask_named_value(val: &str) -> bool {
    val.len() >= 8 && !val.bytes().all(|b| b.is_ascii_digit())
}

fn redact_private_keys(input: &str) -> String {
    let lower = input.to_ascii_lowercase();
    let mut out = String::with_capacity(input.len());
    let mut pos = 0usize;
    while let Some(rel) = lower[pos..].find("-----begin ") {
        let start = pos + rel;
        let after_begin = start + "-----begin ".len();
        if let Some(dash_rel) = lower[after_begin..].find("-----") {
            let label = &lower[after_begin..after_begin + dash_rel];
            if label.ends_with("private key")
                && label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b' ')
            {
                let end_marker = format!("-----end {label}-----");
                let search_from = after_begin + dash_rel + 5;
                if let Some(end_rel) = lower[search_from..].find(&end_marker) {
                    out.push_str(&input[pos..start]);
                    out.push_str(REDACTED);
                    pos = search_from + end_rel + end_marker.len();
                    continue;
                }
            }
        }
        out.push_str(&input[pos..after_begin]);
        pos = after_begin;
    }
    out.push_str(&input[pos..]);
    out
}

fn scan_quoted_end(bytes: &[u8], open_idx: usize) -> Option<usize> {
    let quote = bytes[open_idx];
    let mut i = open_idx + 1;
    while i < bytes.len() {
        if bytes[i] == b'\\' {
            i += 2;
            continue;
        }
        if bytes[i] == quote {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn redact_json_named_values(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;
    let mut last_copied = 0usize;

    while i < bytes.len() {
        if bytes[i] == b'"'
            && let Some(key_end) = scan_quoted_end(bytes, i)
        {
            let key = &input[i + 1..key_end];
            if contains_secret_key_name(key) {
                let mut j = key_end + 1;
                while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                    j += 1;
                }
                if j < bytes.len() && bytes[j] == b':' {
                    j += 1;
                    while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                        j += 1;
                    }
                    if j < bytes.len()
                        && bytes[j] == b'"'
                        && let Some(val_end) = scan_quoted_end(bytes, j)
                    {
                        let val = &input[j + 1..val_end];
                        if should_mask_named_value(val) {
                            out.push_str(&input[last_copied..j + 1]);
                            out.push_str(REDACTED);
                            out.push('"');
                            last_copied = val_end + 1;
                            i = val_end + 1;
                            continue;
                        }
                        i = val_end + 1;
                        continue;
                    }
                }
            }
            i = key_end + 1;
            continue;
        }
        i += 1;
    }
    out.push_str(&input[last_copied..]);
    out
}

fn is_ident_key_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-')
}

fn redact_assigned_named_values(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;
    let mut last_copied = 0usize;

    while i < bytes.len() {
        if is_ident_key_byte(bytes[i]) && (i == 0 || !is_ident_key_byte(bytes[i - 1])) {
            let key_start = i;
            let mut key_end = i;
            while key_end < bytes.len() && is_ident_key_byte(bytes[key_end]) {
                key_end += 1;
            }
            let key = &input[key_start..key_end];
            if contains_secret_key_name(key) {
                let mut j = key_end;
                while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                    j += 1;
                }
                if j < bytes.len() && bytes[j] == b'=' {
                    j += 1;
                    while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                        j += 1;
                    }
                    if j < bytes.len() {
                        if matches!(bytes[j], b'"' | b'\'')
                            && let Some(val_end) = scan_quoted_end(bytes, j)
                        {
                            let val = &input[j + 1..val_end];
                            if should_mask_named_value(val) {
                                out.push_str(&input[last_copied..j + 1]);
                                out.push_str(REDACTED);
                                out.push(bytes[j] as char);
                                last_copied = val_end + 1;
                                i = val_end + 1;
                                continue;
                            }
                            i = val_end + 1;
                            continue;
                        } else {
                            let val_start = j;
                            let mut val_end = j;
                            while val_end < bytes.len()
                                && !bytes[val_end].is_ascii_whitespace()
                                && !matches!(bytes[val_end], b',' | b';' | b'&')
                            {
                                val_end += 1;
                            }
                            let val = &input[val_start..val_end];
                            if should_mask_named_value(val) {
                                out.push_str(&input[last_copied..val_start]);
                                out.push_str(REDACTED);
                                last_copied = val_end;
                                i = val_end;
                                continue;
                            }
                        }
                    }
                }
            }
            i = key_end;
            continue;
        }
        i += 1;
    }
    out.push_str(&input[last_copied..]);
    out
}

fn is_bearer_token_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'~' | b'+' | b'/' | b'-' | b'.' | b'=')
}

fn redact_bearer_tokens(input: &str) -> String {
    let bytes = input.as_bytes();
    let lower = input.to_ascii_lowercase();
    let mut out = String::with_capacity(input.len());
    let mut pos = 0usize;

    while let Some(rel) = lower[pos..].find("bearer") {
        let start = pos + rel;
        let word_before = start > 0 && (bytes[start - 1].is_ascii_alphanumeric() || bytes[start - 1] == b'_');
        let after = start + 6;
        if !word_before && after < bytes.len() && matches!(bytes[after], b' ' | b'\t') {
            let mut j = after;
            while j < bytes.len() && matches!(bytes[j], b' ' | b'\t') {
                j += 1;
            }
            let tok_start = j;
            while j < bytes.len() && is_bearer_token_byte(bytes[j]) {
                j += 1;
            }
            if j > tok_start {
                out.push_str(&input[pos..tok_start]);
                out.push_str(REDACTED);
                pos = j;
                continue;
            }
        }
        out.push_str(&input[pos..after]);
        pos = after;
    }
    out.push_str(&input[pos..]);
    out
}

fn redact_issuer_tokens(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = String::with_capacity(input.len());
    let mut i = 0usize;
    let mut last_copied = 0usize;

    while i < bytes.len() {
        let prev_alnum_us =
            i > 0 && (bytes[i - 1].is_ascii_alphanumeric() || bytes[i - 1] == b'_');
        let prev_alnum = i > 0 && bytes[i - 1].is_ascii_alphanumeric();

        // sk-[A-Za-z0-9_-]{16,}
        if !prev_alnum_us && bytes[i..].starts_with(b"sk-") {
            let mut j = i + 3;
            while j < bytes.len()
                && (bytes[j].is_ascii_alphanumeric() || matches!(bytes[j], b'_' | b'-'))
            {
                j += 1;
            }
            if j - (i + 3) >= 16 {
                out.push_str(&input[last_copied..i]);
                out.push_str(REDACTED);
                last_copied = j;
                i = j;
                continue;
            }
        }

        // ghp_ / gho_ / ghs_ / github_pat_ + [A-Za-z0-9_]{16,}
        if !prev_alnum_us {
            let gh_prefix: Option<&[u8]> = [
                b"github_pat_".as_slice(),
                b"ghp_".as_slice(),
                b"gho_".as_slice(),
                b"ghs_".as_slice(),
            ]
            .into_iter()
            .find(|p| bytes[i..].starts_with(p));
            if let Some(pfx) = gh_prefix {
                let mut j = i + pfx.len();
                while j < bytes.len() && (bytes[j].is_ascii_alphanumeric() || bytes[j] == b'_') {
                    j += 1;
                }
                if j - (i + pfx.len()) >= 16 {
                    out.push_str(&input[last_copied..i]);
                    out.push_str(REDACTED);
                    last_copied = j;
                    i = j;
                    continue;
                }
            }
        }

        // xox[abpr]-[A-Za-z0-9-]{16,}
        if !prev_alnum
            && i + 5 <= bytes.len()
            && bytes[i..].starts_with(b"xox")
            && matches!(bytes[i + 3], b'a' | b'b' | b'p' | b'r')
            && bytes[i + 4] == b'-'
        {
            let mut j = i + 5;
            while j < bytes.len() && (bytes[j].is_ascii_alphanumeric() || bytes[j] == b'-') {
                j += 1;
            }
            if j - (i + 5) >= 16 {
                out.push_str(&input[last_copied..i]);
                out.push_str(REDACTED);
                last_copied = j;
                i = j;
                continue;
            }
        }

        // AKIA[0-9A-Z]{16}
        if !prev_alnum && i + 20 <= bytes.len() && bytes[i..].starts_with(b"AKIA") {
            let body_ok = bytes[i + 4..i + 20]
                .iter()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit());
            let next_ok = i + 20 == bytes.len()
                || !(bytes[i + 20].is_ascii_uppercase() || bytes[i + 20].is_ascii_digit());
            if body_ok && next_ok {
                out.push_str(&input[last_copied..i]);
                out.push_str(REDACTED);
                last_copied = i + 20;
                i += 20;
                continue;
            }
        }

        i += 1;
    }

    out.push_str(&input[last_copied..]);
    out
}

/// Mask private keys, JSON/assigned secret fields, Bearer headers, and common issuer tokens.
pub fn redact_secrets(text: &str) -> String {
    let s1 = redact_private_keys(text);
    let s2 = redact_json_named_values(&s1);
    let s3 = redact_assigned_named_values(&s2);
    let s4 = redact_bearer_tokens(&s3);
    redact_issuer_tokens(&s4)
}

// ---------------------------------------------------------------------------
// Text clipping, path shortening, shell unwrapping & semantic classification
// ---------------------------------------------------------------------------

fn clean_line(text: &str, limit: usize) -> String {
    let cleaned = single_line(text);
    let count = cleaned.chars().count();
    if count <= limit {
        return cleaned;
    }
    let keep = limit.saturating_sub(1).max(1);
    let head: String = cleaned.chars().take(keep).collect();
    format!("{}…", head.trim_end())
}

fn clip_multiline(text: &str, limit: usize) -> (String, bool) {
    let cleaned = multiline(text).trim().to_string();
    let total = cleaned.chars().count();
    if total <= limit {
        return (cleaned, false);
    }
    let head_len = limit * 2 / 3;
    let tail_len = limit - head_len;
    let elided = total - limit;
    let head: String = cleaned.chars().take(head_len).collect();
    let tail: String = cleaned
        .chars()
        .skip(total.saturating_sub(tail_len))
        .collect();
    (
        format!(
            "{}\n[… {elided} chars elided …]\n{}",
            head.trim_end(),
            tail.trim_start()
        ),
        true,
    )
}

fn shorten_path(raw: &str, workspace: Option<&str>) -> String {
    let mut candidate = raw
        .trim()
        .trim_matches(|c| c == '\'' || c == '"')
        .to_string();
    if candidate.is_empty() {
        return String::new();
    }
    if let Some(ws) = workspace
        && Path::new(&candidate).is_absolute()
    {
        let ws_clean = ws.trim_end_matches('/');
        if let Some(rest) = candidate.strip_prefix(ws_clean)
            && let Some(rel) = rest.strip_prefix('/')
        {
            candidate = rel.to_string();
        }
    }
    if candidate.chars().count() <= MAX_TARGET_CHARS {
        return candidate;
    }
    let parts: Vec<&str> = candidate.split('/').filter(|p| !p.is_empty()).collect();
    if parts.len() >= 3 {
        let short = format!(".../{}", parts[parts.len() - 3..].join("/"));
        if short.chars().count() <= MAX_TARGET_CHARS {
            return short;
        }
    }
    let keep = MAX_TARGET_CHARS.saturating_sub(1);
    let total = candidate.chars().count();
    let tail: String = candidate.chars().skip(total.saturating_sub(keep)).collect();
    format!("…{tail}")
}

/// Strip outer `/bin/bash -lc '...'` or `sh -c ...` wrappers cleanly.
pub fn unwrap_shell(raw: &str) -> String {
    let mut s = raw.trim().to_string();
    for prefix in [
        "/bin/bash -lc ",
        "/usr/bin/bash -lc ",
        "bash -lc ",
        "/bin/sh -c ",
        "sh -c ",
    ] {
        if let Some(rest) = s.strip_prefix(prefix) {
            s = rest.trim().to_string();
            break;
        }
    }
    if s.len() >= 2 {
        let first = s.as_bytes()[0];
        let last = s.as_bytes()[s.len() - 1];
        if first == last && (first == b'\'' || first == b'"') {
            let mut inner = s[1..s.len() - 1].to_string();
            if first == b'"' {
                inner = inner
                    .replace("\\\"", "\"")
                    .replace("\\n", "\n")
                    .replace("\\t", "\t");
            }
            return inner.trim().to_string();
        }
    }
    if let Some(first) = s.as_bytes().first().copied()
        && (first == b'\'' || first == b'"')
    {
        let quote_char = first as char;
        let qcount = s.chars().filter(|&c| c == quote_char).count();
        if qcount == 1 || (first == b'"' && s.ends_with('\\')) {
            s = s[1..].to_string();
        }
    }
    s.trim().to_string()
}

fn shell_split_simple(head: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut cur = String::new();
    let mut in_single = false;
    let mut in_double = false;
    let mut chars = head.chars().peekable();
    while let Some(c) = chars.next() {
        if in_single {
            if c == '\'' {
                in_single = false;
            } else {
                cur.push(c);
            }
        } else if in_double {
            if c == '\\' {
                if let Some(next) = chars.next() {
                    cur.push(next);
                }
            } else if c == '"' {
                in_double = false;
            } else {
                cur.push(c);
            }
        } else {
            match c {
                '\'' => in_single = true,
                '"' => in_double = true,
                c if c.is_whitespace() => {
                    if !cur.is_empty() {
                        tokens.push(std::mem::take(&mut cur));
                    }
                }
                _ => cur.push(c),
            }
        }
    }
    if !cur.is_empty() {
        tokens.push(cur);
    }
    tokens
}

fn split_first_shell_segment(line: &str) -> &str {
    let bytes = line.as_bytes();
    let mut in_single = false;
    let mut in_double = false;
    let mut i = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' if !in_double => in_single = !in_single,
            b'"' if !in_single => in_double = !in_double,
            b'\\' if in_double => {
                i += 1;
            }
            b';' | b'|' if !in_single && !in_double => return line[..i].trim(),
            b'&' if !in_single && !in_double && i + 1 < bytes.len() && bytes[i + 1] == b'&' => {
                return line[..i].trim();
            }
            _ => {}
        }
        i += 1;
    }
    line.trim()
}

fn extract_heredoc_file_targets(cmd: &str) -> Vec<String> {
    let slice: String = cmd.chars().take(2000).collect();
    let mut found = Vec::new();
    for needle in [
        "open(",
        "Path(",
        "readFileSync(",
        "writeFileSync(",
        "read_text(",
        "write_text(",
    ] {
        let mut pos = 0usize;
        while let Some(rel) = slice[pos..].find(needle) {
            let mut j = pos + rel + needle.len();
            while j < slice.len() && slice.as_bytes()[j].is_ascii_whitespace() {
                j += 1;
            }
            if j < slice.len() && matches!(slice.as_bytes()[j], b'\'' | b'"') {
                let q = slice.as_bytes()[j];
                if let Some(end_rel) = slice[j + 1..].find(q as char) {
                    let cand = &slice[j + 1..j + 1 + end_rel];
                    if (2..=120).contains(&cand.len())
                        && !cand.contains('\n')
                        && !found.iter().any(|x| x == cand)
                    {
                        found.push(cand.to_string());
                        if found.len() >= 2 {
                            return found;
                        }
                    }
                }
            }
            pos = j;
        }
    }
    // Also match `p = '...'` / `p = "..."` assignments (parity with `progress.py`).
    let bytes = slice.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() && found.len() < 2 {
        let boundary = i == 0 || bytes[i - 1].is_ascii_whitespace() || bytes[i - 1] == b';';
        if boundary && bytes[i] == b'p' {
            let mut j = i + 1;
            while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                j += 1;
            }
            if j < bytes.len() && bytes[j] == b'=' {
                j += 1;
                while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                    j += 1;
                }
                if j < bytes.len() && matches!(bytes[j], b'\'' | b'"') {
                    let q = bytes[j];
                    if let Some(end_rel) = slice[j + 1..].find(q as char) {
                        let cand = &slice[j + 1..j + 1 + end_rel];
                        if (2..=120).contains(&cand.len())
                            && !cand.contains('\n')
                            && !found.iter().any(|x| x == cand)
                        {
                            found.push(cand.to_string());
                        }
                        i = j + 1 + end_rel + 1;
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    found
}

fn extract_shell_target(cmd: &str, workspace: Option<&str>) -> (String, String) {
    if cmd.is_empty() {
        return (String::new(), String::new());
    }
    let first_line = cmd
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or(cmd.trim());

    let first_low = first_line.to_ascii_lowercase();
    if first_line.contains("<<")
        && (first_low.contains("python") || first_low.contains("node"))
    {
        let lang = if first_low.contains("node") {
            "Node"
        } else {
            "Python"
        };
        let matches = extract_heredoc_file_targets(cmd);
        if !matches.is_empty() {
            let target = shorten_path(&matches[0], workspace);
            let extra = if matches.len() > 1 {
                format!(" (+{})", matches.len() - 1)
            } else {
                String::new()
            };
            return (format!("inline {lang}: {target}{extra}"), target);
        }
        return (format!("inline {lang}"), String::new());
    }

    let head = split_first_shell_segment(first_line);
    let tokens = shell_split_simple(head);
    if tokens.is_empty() {
        return (clean_line(first_line, MAX_SUMMARY_CHARS), String::new());
    }

    let prog = Path::new(&tokens[0])
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(&tokens[0])
        .to_ascii_lowercase();

    if matches!(prog.as_str(), "sed" | "cat" | "head" | "tail" | "nl" | "wc") {
        let files: Vec<&String> = tokens[1..]
            .iter()
            .filter(|t| !t.starts_with('-') && (t.contains('/') || t.contains('.')))
            .collect();
        if let Some(last) = files.last() {
            let target = shorten_path(last, workspace);
            return (format!("{prog} {target}"), target);
        }
    }

    if prog == "git" && tokens.len() >= 2 {
        let sub = &tokens[1];
        if let Some(dd) = tokens.iter().position(|t| t == "--") {
            let paths: Vec<&String> = tokens[dd + 1..]
                .iter()
                .filter(|t| !t.is_empty() && !t.starts_with('-'))
                .collect();
            if !paths.is_empty() {
                let target = shorten_path(paths[0], workspace);
                let suffix = if paths.len() > 1 {
                    format!(" (+{})", paths.len() - 1)
                } else {
                    String::new()
                };
                return (format!("git {sub} {target}{suffix}"), target);
            }
        }
        return (clean_line(first_line, MAX_SUMMARY_CHARS), String::new());
    }

    if matches!(prog.as_str(), "rg" | "grep") {
        let non_flags: Vec<&String> = tokens[1..]
            .iter()
            .filter(|t| !t.starts_with('-'))
            .collect();
        if non_flags.len() >= 2 {
            let target = shorten_path(non_flags[non_flags.len() - 1], workspace);
            return (format!("{prog} {} in {target}", non_flags[0]), target);
        }
        if let Some(first) = non_flags.first() {
            return (format!("{prog} {first}"), String::new());
        }
    }

    let mut target = String::new();
    for (idx, tok) in tokens.iter().enumerate().skip(1) {
        if matches!(tok.as_str(), "--filter" | "-p" | "--package" | "-C")
            && idx + 1 < tokens.len()
        {
            target = shorten_path(&tokens[idx + 1], workspace);
            break;
        }
        if !tok.starts_with('-')
            && (tok.contains('/')
                || tok.ends_with(".py")
                || tok.ends_with(".rs")
                || tok.ends_with(".ts")
                || tok.ends_with(".tsx")
                || tok.ends_with(".js")
                || tok.ends_with(".mjs")
                || tok.ends_with(".go"))
        {
            target = shorten_path(tok, workspace);
            break;
        }
    }

    (clean_line(first_line, MAX_SUMMARY_CHARS), target)
}

struct ClassifiedAction {
    stage: &'static str,
    category: &'static str,
    target: String,
    summary: String,
    command: Option<String>,
}

fn is_inspect_shell(cmd_low: &str) -> bool {
    let first_tok = cmd_low.split_whitespace().next().unwrap_or("");
    if matches!(
        first_tok,
        "sed" | "cat" | "head" | "tail" | "ls" | "find" | "rg" | "grep"
    ) {
        return true;
    }
    if first_tok == "git" {
        let second = cmd_low.split_whitespace().nth(1).unwrap_or("");
        return matches!(second, "status" | "diff" | "log" | "show");
    }
    false
}

fn classify_tool_action(
    tool_name: &str,
    command: Option<&str>,
    path: Option<&str>,
    args: Option<&Value>,
    workspace: Option<&str>,
) -> ClassifiedAction {
    let tool = if tool_name.trim().is_empty() {
        "tool"
    } else {
        tool_name.trim()
    };
    let tool_low = tool.to_ascii_lowercase();
    let args_obj = args.and_then(|v| v.as_object());

    let raw_path = path
        .filter(|s| !s.trim().is_empty())
        .or_else(|| {
            args_obj.and_then(|obj| {
                ["path", "file_path", "target_file", "Path", "TargetFile", "AbsolutePath"]
                    .iter()
                    .find_map(|k| obj.get(*k).and_then(|v| v.as_str()).filter(|s| !s.trim().is_empty()))
            })
        });

    let raw_cmd = command
        .filter(|s| !s.trim().is_empty())
        .or_else(|| {
            args_obj.and_then(|obj| {
                ["command", "CommandLine"]
                    .iter()
                    .find_map(|k| obj.get(*k).and_then(|v| v.as_str()).filter(|s| !s.trim().is_empty()))
            })
        });

    if raw_cmd.is_some() || SHELL_TOOLS.contains(&tool_low.as_str()) {
        let unwrapped = unwrap_shell(raw_cmd.unwrap_or(if tool.contains(' ') { tool } else { "" }));
        let cmd_low = unwrapped.to_ascii_lowercase();
        let (summary, mut target) = extract_shell_target(&unwrapped, workspace);
        if target.is_empty()
            && let Some(rp) = raw_path
        {
            target = shorten_path(rp, workspace);
        }

        let (stage, category) = if TEST_PATTERNS.iter().any(|p| cmd_low.contains(p)) {
            ("testing", "test")
        } else if BUILD_PATTERNS.iter().any(|p| cmd_low.contains(p)) {
            ("building", "build")
        } else if is_inspect_shell(&cmd_low) {
            ("exploring", "read")
        } else if summary.contains("inline ")
            && (cmd_low.contains(".write(")
                || cmd_low.contains("write_text")
                || cmd_low.contains("writefilesync")
                || cmd_low.contains("replace("))
        {
            ("editing", "edit")
        } else {
            ("running", "run")
        };

        let chosen_summary = if !summary.is_empty() {
            summary
        } else if !unwrapped.is_empty() {
            unwrapped.clone()
        } else {
            tool.to_string()
        };
        return ClassifiedAction {
            stage,
            category,
            target,
            summary: clean_line(&chosen_summary, MAX_SUMMARY_CHARS),
            command: if unwrapped.is_empty() {
                None
            } else {
                Some(unwrapped)
            },
        };
    }

    let mut target = raw_path
        .map(|p| shorten_path(p, workspace))
        .unwrap_or_default();

    if EDIT_TOOLS.contains(&tool_low.as_str())
        || tool_low.contains("edit")
        || tool_low.contains("write")
        || tool_low.contains("patch")
        || tool_low.contains("replace")
    {
        let mut extra = String::new();
        if let Some(edits) = args_obj.and_then(|o| o.get("edits")).and_then(|v| v.as_array())
            && !edits.is_empty()
        {
            extra = format!(
                " ({} edit{})",
                edits.len(),
                if edits.len() == 1 { "" } else { "s" }
            );
        } else if tool_low == "file_change"
            && let Some(changes) = args_obj
                .and_then(|o| o.get("changes"))
                .and_then(|v| v.as_array())
            && let Some(first_path) = changes
                .first()
                .and_then(|c| c.get("path"))
                .and_then(|v| v.as_str())
        {
            target = shorten_path(first_path, workspace);
            if changes.len() > 1 {
                extra = format!(" (+{} files)", changes.len() - 1);
            }
        }
        let base = if target.is_empty() { tool } else { &target };
        let summary = clean_line(&format!("{base}{extra}"), MAX_SUMMARY_CHARS);
        return ClassifiedAction {
            stage: "editing",
            category: "edit",
            target,
            summary,
            command: None,
        };
    }

    if READ_TOOLS.contains(&tool_low.as_str())
        || tool_low.contains("read")
        || tool_low.contains("list")
    {
        let base = if target.is_empty() { tool } else { &target };
        let summary = clean_line(base, MAX_SUMMARY_CHARS);
        return ClassifiedAction {
            stage: "exploring",
            category: "read",
            target,
            summary,
            command: None,
        };
    }

    if SEARCH_TOOLS.contains(&tool_low.as_str())
        || tool_low.contains("search")
        || tool_low.contains("grep")
    {
        let pattern = args_obj
            .and_then(|o| o.get("pattern").or_else(|| o.get("query")))
            .and_then(|v| v.as_str())
            .map(|s| clean_line(s, 48))
            .unwrap_or_default();
        let summary = if !pattern.is_empty() && !target.is_empty() {
            format!("/{pattern}/ in {target}")
        } else if !pattern.is_empty() {
            pattern.clone()
        } else if !target.is_empty() {
            target.clone()
        } else {
            tool.to_string()
        };
        let final_target = if target.is_empty() { pattern } else { target };
        return ClassifiedAction {
            stage: "exploring",
            category: "search",
            target: final_target,
            summary: clean_line(&summary, MAX_SUMMARY_CHARS),
            command: None,
        };
    }

    let summary = clean_line(format!("{tool} {target}").trim(), MAX_SUMMARY_CHARS);
    ClassifiedAction {
        stage: "running",
        category: "tool",
        target,
        summary,
        command: None,
    }
}

fn is_summary_result_line(line: &str) -> bool {
    let low = line.to_ascii_lowercase();
    if low.contains("all checks passed")
        || low.contains("finished")
        || low.contains("built in")
        || low.contains("successfully")
    {
        return true;
    }
    let words: Vec<&str> = low.split_whitespace().collect();
    for pair in words.windows(2) {
        let num = pair[0].trim_matches(|c: char| !c.is_ascii_alphanumeric());
        let label = pair[1].trim_matches(|c: char| !c.is_ascii_alphabetic());
        if !num.is_empty()
            && num.bytes().all(|b| b.is_ascii_digit())
            && matches!(
                label,
                "passed" | "failed" | "ok" | "test" | "tests" | "error" | "errors" | "warning" | "warnings"
            )
        {
            return true;
        }
    }
    false
}

/// Pick the single highest-signal outcome line from tool output (parity with `progress.py`).
pub fn summarize_output(output: &str, exit_code: Option<i64>, is_error: bool) -> String {
    let normalized = strip_controls_opts(&output.replace("\r\n", "\n").replace('\r', "\n"), true);
    let mut lines: Vec<String> = normalized
        .lines()
        .map(|ln| ln.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|ln| !ln.is_empty() && !ln.starts_with("[…") && !ln.starts_with("[..."))
        .collect();

    let non_banner: Vec<String> = lines
        .iter()
        .filter(|ln| !(ln.starts_with("> ") && ln.len() > 2))
        .cloned()
        .collect();
    if !non_banner.is_empty() {
        lines = non_banner;
    } else if !lines.is_empty() && !is_error && matches!(exit_code, None | Some(0)) {
        return "ok".to_string();
    }

    if is_error || exit_code.map(|c| c != 0).unwrap_or(false) {
        let prefix = match exit_code {
            Some(c) if c != 0 => format!("exit {c}: "),
            _ => String::new(),
        };
        for ln in lines.iter().rev() {
            let low = ln.to_ascii_lowercase();
            if low.contains("error")
                || low.contains("failed")
                || low.contains("assertion")
                || low.contains("exception")
                || low.contains("traceback")
                || low.contains("exit status")
            {
                return clean_line(&format!("{prefix}{ln}"), 100);
            }
        }
        if let Some(last) = lines.last() {
            return clean_line(&format!("{prefix}{last}"), 100);
        }
        return match exit_code {
            Some(c) if c != 0 => format!("exit {c}"),
            _ => "failed".to_string(),
        };
    }

    let tail_start = lines.len().saturating_sub(6);
    for ln in lines[tail_start..].iter().rev() {
        if is_summary_result_line(ln) {
            return clean_line(ln, 100);
        }
    }
    if lines.len() == 1 {
        return clean_line(&lines[0], 100);
    }
    if let Some(first) = lines.first() {
        return clean_line(&format!("{first} ({} lines)", lines.len()), 100);
    }
    "done".to_string()
}

fn intent_from_text(text: &str) -> Option<String> {
    for raw_line in text.lines() {
        let stripped = strip_controls_opts(raw_line, false);
        let trimmed = stripped.trim();
        let no_bullet = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
            .or_else(|| trimmed.strip_prefix("+ "))
            .unwrap_or_else(|| {
                let digits = trimmed.bytes().take_while(|b| b.is_ascii_digit()).count();
                if digits > 0
                    && trimmed.len() > digits + 1
                    && matches!(trimmed.as_bytes()[digits], b'.' | b')')
                    && trimmed.as_bytes()[digits + 1] == b' '
                {
                    &trimmed[digits + 2..]
                } else {
                    trimmed
                }
            });
        let clean = strip_inline_markdown(no_bullet);
        if clean.is_empty() {
            continue;
        }
        return Some(clean_line(&clean, INTENT_MAX_CHARS));
    }
    None
}

fn content_text(val: Option<&Value>) -> String {
    let Some(v) = val else {
        return String::new();
    };
    if let Some(s) = v.as_str() {
        return s.to_string();
    }
    if let Some(arr) = v.as_array() {
        let mut out = String::new();
        for block in arr {
            if block.get("type").and_then(|t| t.as_str()) == Some("text")
                && let Some(t) = block.get("text").and_then(|t| t.as_str())
            {
                out.push_str(t);
            }
        }
        return out;
    }
    String::new()
}

// ---------------------------------------------------------------------------
// Incremental EventsTailer
// ---------------------------------------------------------------------------

pub struct EventsTailer {
    path: PathBuf,
    engine: String,
    workspace: Option<String>,
    inode: Option<u64>,
    offset: u64,
    next_id: u64,
    items: VecDeque<StructuredAction>,
    open_calls: VecDeque<(String, u64)>,
    agy_text_buf: HashMap<String, String>,
    agy_text_order: Vec<String>,
    latest_intent: Option<String>,
}

impl EventsTailer {
    pub fn new(job_dir: &Path, engine: &str, workspace: Option<String>) -> Self {
        Self {
            path: job_dir.join("events.jsonl"),
            engine: engine.trim().to_ascii_lowercase(),
            workspace,
            inode: None,
            offset: 0,
            next_id: 1,
            items: VecDeque::new(),
            open_calls: VecDeque::new(),
            agy_text_buf: HashMap::new(),
            agy_text_order: Vec::new(),
            latest_intent: None,
        }
    }

    pub fn matches_target(&self, job_dir: &Path, engine: &str) -> bool {
        self.path == job_dir.join("events.jsonl")
            && self.engine == engine.trim().to_ascii_lowercase()
    }

    fn reset(&mut self) {
        self.inode = None;
        self.offset = 0;
        self.next_id = 1;
        self.items.clear();
        self.open_calls.clear();
        self.agy_text_buf.clear();
        self.agy_text_order.clear();
        self.latest_intent = None;
    }

    /// Incrementally read newly appended complete JSONL lines from `events.jsonl`.
    pub fn refresh(&mut self) -> Result<(), String> {
        let file = match File::open(&self.path) {
            Ok(f) => f,
            Err(e) if e.kind() == ErrorKind::NotFound => {
                self.reset();
                return Ok(());
            }
            Err(e) => return Err(format!("open events.jsonl: {e}")),
        };
        let meta = file.metadata().map_err(|e| format!("stat events.jsonl: {e}"))?;
        let ino = meta.ino();
        let len = meta.len();

        if self.inode != Some(ino) || len < self.offset {
            self.reset();
            self.inode = Some(ino);
        }
        if len == self.offset {
            return Ok(());
        }

        let mut reader = BufReader::new(file);
        reader
            .seek(SeekFrom::Start(self.offset))
            .map_err(|e| format!("seek events.jsonl: {e}"))?;

        let mut buf = Vec::with_capacity(4096);
        loop {
            buf.clear();
            let n = reader
                .read_until(b'\n', &mut buf)
                .map_err(|e| format!("read events.jsonl: {e}"))?;
            if n == 0 {
                break;
            }
            // Do not consume an incomplete trailing line being written by the engine.
            if !buf.ends_with(b"\n") {
                break;
            }
            self.offset += n as u64;
            if let Ok(Value::Object(map)) = serde_json::from_slice::<Value>(&buf) {
                self.process_event(&Value::Object(map));
            }
        }
        Ok(())
    }

    /// Build a bounded `PeekResult` snapshot without losing in-progress AGY text buffers.
    pub fn snapshot(
        &self,
        worker_id: &str,
        count: usize,
        events_sig: Option<(u64, i64)>,
    ) -> PeekResult {
        let cap = count.clamp(1, 50);
        let mut combined: Vec<StructuredAction> = self.items.iter().cloned().collect();
        let mut preview_id = self.next_id;
        let mut intent = self.latest_intent.clone();

        // Append any unflushed AGY text deltas transiently so streaming updates show up live.
        for key in &self.agy_text_order {
            if let Some(raw) = self.agy_text_buf.get(key)
                && let Some(action) = self.build_milestone_item(preview_id, "say", raw, combined.last())
            {
                if let Some(h) = intent_from_text(&redact_secrets(raw)) {
                    intent = Some(h);
                }
                combined.push(action);
                preview_id += 1;
            }
        }

        let total = preview_id.saturating_sub(1);
        let start = combined.len().saturating_sub(cap);
        let actions = combined.split_off(start);

        PeekResult {
            worker_id: worker_id.to_string(),
            total,
            intent,
            steps: Vec::new(),
            actions,
            error: None,
            events_sig,
        }
    }

    fn push_item(&mut self, item: StructuredAction) {
        self.items.push_back(item);
        while self.items.len() > MAX_STORED_ACTIONS {
            self.items.pop_front();
        }
    }

    fn build_milestone_item(
        &self,
        id: u64,
        kind: &str,
        text: &str,
        prev: Option<&StructuredAction>,
    ) -> Option<StructuredAction> {
        let redacted = redact_secrets(text).trim().to_string();
        if redacted.is_empty() {
            return None;
        }
        let (detail, truncated) = clip_multiline(&redacted, MAX_OUTPUT_CHARS);
        if let Some(p) = prev
            && p.kind == kind
            && p.output.as_deref() == Some(detail.as_str())
        {
            return None;
        }
        let headline =
            intent_from_text(&redacted).unwrap_or_else(|| clean_line(&redacted, MAX_SUMMARY_CHARS));
        Some(StructuredAction {
            id,
            kind: kind.to_string(),
            category: if kind == "say" { "say" } else { "err" }.to_string(),
            stage: if kind == "error" {
                "blocked"
            } else {
                "narrative"
            }
            .to_string(),
            tool: None,
            target: String::new(),
            summary: clean_line(&headline, MAX_SUMMARY_CHARS),
            outcome: None,
            status: if kind == "error" { "error" } else { "done" }.to_string(),
            exit_code: None,
            command: None,
            output: Some(detail),
            truncated,
        })
    }

    fn record_milestone(&mut self, kind: &str, text: &str) {
        if let Some(item) = self.build_milestone_item(self.next_id, kind, text, self.items.back()) {
            if kind == "say"
                && let Some(h) = intent_from_text(&redact_secrets(text))
            {
                self.latest_intent = Some(h);
            }
            self.next_id += 1;
            self.push_item(item);
        }
    }

    fn flush_agy_text(&mut self, step_key: Option<&str>) {
        let keys: Vec<String> = match step_key {
            Some(k) => vec![k.to_string()],
            None => std::mem::take(&mut self.agy_text_order),
        };
        for k in keys {
            if let Some(pos) = self.agy_text_order.iter().position(|x| x == &k) {
                self.agy_text_order.remove(pos);
            }
            if let Some(text) = self.agy_text_buf.remove(&k) {
                self.record_milestone("say", &text);
            }
        }
    }

    fn start_call(
        &mut self,
        call_id: Option<&str>,
        tool: &str,
        command: Option<&str>,
        path: Option<&str>,
        args: Option<&Value>,
    ) -> u64 {
        let info = classify_tool_action(tool, command, path, args, self.workspace.as_deref());
        let (cmd_clipped, cmd_trunc) = match info.command {
            Some(c) => {
                let red = redact_secrets(&c);
                let (clipped, trunc) = clip_multiline(&red, MAX_COMMAND_CHARS);
                (Some(clipped), trunc)
            }
            None => (None, false),
        };
        let id = self.next_id;
        self.next_id += 1;
        let action = StructuredAction {
            id,
            kind: "action".to_string(),
            category: info.category.to_string(),
            stage: info.stage.to_string(),
            tool: Some(tool.to_string()),
            target: info.target,
            summary: redact_secrets(&info.summary),
            outcome: None,
            status: "running".to_string(),
            exit_code: None,
            command: cmd_clipped,
            output: None,
            truncated: cmd_trunc,
        };
        self.push_item(action);
        if let Some(cid) = call_id
            && !cid.is_empty()
        {
            if self.open_calls.len() >= MAX_OPEN_CALLS {
                self.open_calls.pop_front();
            }
            self.open_calls.push_back((cid.to_string(), id));
        }
        id
    }

    fn take_open_call(&mut self, call_id: &str) -> Option<u64> {
        let pos = self.open_calls.iter().position(|(k, _)| k == call_id)?;
        self.open_calls.remove(pos).map(|(_, id)| id)
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_call(
        &mut self,
        call_id: Option<&str>,
        tool: &str,
        command: Option<&str>,
        path: Option<&str>,
        args: Option<&Value>,
        output: &str,
        exit_code: Option<i64>,
        is_error: bool,
    ) {
        let existing_id = call_id.and_then(|cid| self.take_open_call(cid));
        let target_id = match existing_id {
            Some(id) if self.items.iter().any(|a| a.id == id) => id,
            _ => self.start_call(None, tool, command, path, args),
        };

        let redacted_out = if output.is_empty() {
            String::new()
        } else {
            redact_secrets(output)
        };
        let (out_clipped, out_trunc) = if redacted_out.is_empty() {
            (None, false)
        } else {
            let (c, t) = clip_multiline(&redacted_out, MAX_OUTPUT_CHARS);
            (if c.is_empty() { None } else { Some(c) }, t)
        };
        let failed = is_error || exit_code.map(|c| c != 0).unwrap_or(false);
        let outcome = summarize_output(&redacted_out, exit_code, failed);

        let fallback_cmd = if command.is_some() {
            let info = classify_tool_action(tool, command, path, args, self.workspace.as_deref());
            info.command.map(|c| clip_multiline(&redact_secrets(&c), MAX_COMMAND_CHARS))
        } else {
            None
        };

        if let Some(action) = self.items.iter_mut().rev().find(|a| a.id == target_id) {
            if action.command.is_none()
                && let Some((cmd_c, cmd_t)) = fallback_cmd
            {
                action.command = Some(cmd_c);
                action.truncated = action.truncated || cmd_t;
            }
            action.status = if failed { "error" } else { "ok" }.to_string();
            action.exit_code = exit_code;
            action.outcome = Some(outcome);
            action.output = out_clipped;
            action.truncated = action.truncated || out_trunc;
        }
    }

    fn process_event(&mut self, event: &Value) {
        match self.engine.as_str() {
            "codex" => self.process_codex(event),
            "pi" => self.process_pi(event),
            "claude" => self.process_claude(event),
            "agy" => self.process_agy(event),
            _ => {}
        }
    }

    fn process_codex(&mut self, event: &Value) {
        let etype = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if let Some(item) = event.get("item").filter(|v| v.is_object()) {
            let itype = item.get("type").and_then(|v| v.as_str()).unwrap_or("");
            let iid = item.get("id").and_then(|v| v.as_str());
            if etype == "item.completed"
                && itype == "agent_message"
                && let Some(text) = item.get("text").and_then(|v| v.as_str())
            {
                self.record_milestone("say", text);
            } else if etype == "item.completed" && itype == "error" {
                let msg = item
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("Codex error");
                self.record_milestone("error", msg);
            } else if matches!(itype, "command_execution" | "mcp_tool_call" | "file_change") {
                let tool = if itype == "command_execution" {
                    "shell"
                } else {
                    item.get("tool").and_then(|v| v.as_str()).unwrap_or(itype)
                };
                let cmd = item.get("command").and_then(|v| v.as_str());
                if etype == "item.started" {
                    self.start_call(iid, tool, cmd, None, Some(item));
                } else if etype == "item.completed" {
                    let exit_code = item.get("exit_code").and_then(|v| v.as_i64());
                    let status_val = item
                        .get("status")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_ascii_lowercase();
                    let is_err = exit_code.map(|c| c != 0).unwrap_or(false)
                        || matches!(status_val.as_str(), "failed" | "error");
                    let mut out_text = item
                        .get("aggregated_output")
                        .or_else(|| item.get("output"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    if out_text.is_empty()
                        && itype == "file_change"
                        && let Some(changes) = item.get("changes").and_then(|v| v.as_array())
                    {
                        out_text = changes
                            .iter()
                            .filter_map(|c| c.as_object())
                            .map(|c| {
                                let k = c.get("kind").and_then(|v| v.as_str()).unwrap_or("change");
                                let p = c.get("path").and_then(|v| v.as_str()).unwrap_or("");
                                format!("{k}: {p}")
                            })
                            .collect::<Vec<_>>()
                            .join("\n");
                    }
                    self.finish_call(iid, tool, cmd, None, Some(item), &out_text, exit_code, is_err);
                }
            }
        } else if etype == "turn.failed" {
            let msg = event
                .get("error")
                .and_then(|e| e.get("message").and_then(|v| v.as_str()).or_else(|| e.as_str()))
                .unwrap_or("Codex turn failed");
            self.record_milestone("error", msg);
        }
    }

    fn process_pi(&mut self, event: &Value) {
        let etype = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match etype {
            "tool_execution_start" => {
                let args = event.get("args").filter(|v| v.is_object());
                let cid = event.get("toolCallId").and_then(|v| v.as_str());
                let tool = event
                    .get("toolName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("tool");
                let cmd = args.and_then(|a| a.get("command")).and_then(|v| v.as_str());
                let path = args.and_then(|a| a.get("path")).and_then(|v| v.as_str());
                self.start_call(cid, tool, cmd, path, args);
            }
            "tool_execution_end" => {
                let cid = event.get("toolCallId").and_then(|v| v.as_str());
                let args = event.get("args").filter(|v| v.is_object());
                let tool = event
                    .get("toolName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("tool");
                let cmd = args.and_then(|a| a.get("command")).and_then(|v| v.as_str());
                let path = args.and_then(|a| a.get("path")).and_then(|v| v.as_str());
                let out_text = event
                    .get("result")
                    .map(|r| content_text(r.get("content")))
                    .unwrap_or_default();
                let is_err = event
                    .get("isError")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                self.finish_call(cid, tool, cmd, path, args, &out_text, None, is_err);
            }
            "message_end" => {
                if let Some(msg) = event.get("message").and_then(|v| v.as_object())
                    && msg.get("role").and_then(|v| v.as_str()) == Some("assistant")
                {
                    let stop = msg.get("stopReason").and_then(|v| v.as_str()).unwrap_or("");
                    if matches!(stop, "error" | "aborted") {
                        let err_msg = msg
                            .get("errorMessage")
                            .and_then(|v| v.as_str())
                            .unwrap_or(stop);
                        self.record_milestone("error", err_msg);
                    } else {
                        let text = content_text(msg.get("content"));
                        if !text.is_empty() {
                            self.record_milestone("say", &text);
                        }
                    }
                }
            }
            "response" if event.get("success").and_then(|v| v.as_bool()) == Some(false) => {
                let err = event
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("Pi prompt rejected");
                self.record_milestone("error", err);
            }
            _ => {}
        }
    }

    fn process_claude(&mut self, event: &Value) {
        let etype = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if matches!(etype, "assistant" | "user") {
            if etype == "user" && event.get("isReplay").and_then(|v| v.as_bool()) == Some(true) {
                return;
            }
            if let Some(blocks) = event
                .get("message")
                .and_then(|m| m.get("content"))
                .and_then(|c| c.as_array())
            {
                for block in blocks {
                    let btype = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
                    if etype == "assistant" && btype == "text" {
                        if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                            self.record_milestone("say", t);
                        }
                    } else if etype == "assistant" && btype == "tool_use" {
                        let inp = block.get("input").filter(|v| v.is_object());
                        let cid = block.get("id").and_then(|v| v.as_str());
                        let tool = block.get("name").and_then(|v| v.as_str()).unwrap_or("tool");
                        let cmd = inp.and_then(|o| o.get("command")).and_then(|v| v.as_str());
                        let path = inp
                            .and_then(|o| o.get("file_path").or_else(|| o.get("path")))
                            .and_then(|v| v.as_str());
                        self.start_call(cid, tool, cmd, path, inp);
                    } else if etype == "user" && btype == "tool_result" {
                        let cid = block.get("tool_use_id").and_then(|v| v.as_str());
                        let out_text = content_text(block.get("content"));
                        let is_err = block
                            .get("is_error")
                            .and_then(|v| v.as_bool())
                            .unwrap_or(false);
                        self.finish_call(cid, "tool", None, None, None, &out_text, None, is_err);
                    }
                }
            }
        } else if etype == "result" {
            let subtype = event.get("subtype").and_then(|v| v.as_str()).unwrap_or("");
            let is_err = event
                .get("is_error")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if subtype != "success" || is_err {
                let msg = event
                    .get("result")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .unwrap_or(if subtype.is_empty() {
                        "Claude failed"
                    } else {
                        subtype
                    });
                self.record_milestone("error", msg);
            }
        }
    }

    fn process_agy(&mut self, event: &Value) {
        let Some(ekind) = event.get("event").and_then(|v| v.as_str()) else {
            return;
        };
        let Some(body) = event.get(ekind).filter(|v| v.is_object()) else {
            return;
        };
        if ekind == "step_update" {
            let conv_id = body
                .get("conversation_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let step_idx = body
                .get("step_index")
                .map(|v| v.to_string())
                .unwrap_or_default();
            let skey = format!("{conv_id}:{step_idx}");

            if let Some(delta) = body.get("text_delta").and_then(|v| v.as_str())
                && !delta.is_empty()
            {
                if !self.agy_text_buf.contains_key(&skey) {
                    self.agy_text_order.push(skey.clone());
                }
                self.agy_text_buf.entry(skey.clone()).or_default().push_str(delta);
            }
            if body.get("step_type").and_then(|v| v.as_str()) == Some("agent_response")
                && body.get("state").and_then(|v| v.as_str()) == Some("DONE")
            {
                self.flush_agy_text(Some(&skey));
            }
            if let Some(tool_name) = body.get("tool_name").and_then(|v| v.as_str())
                && !tool_name.is_empty()
            {
                self.flush_agy_text(None);
                let info = body.get("tool_info").filter(|v| v.is_object());
                let params = info
                    .and_then(|i| i.get("parameters"))
                    .filter(|v| v.is_object());
                let cmd = params
                    .and_then(|p| p.get("CommandLine").or_else(|| p.get("command")))
                    .and_then(|v| v.as_str());
                let path = params
                    .and_then(|p| {
                        p.get("Path")
                            .or_else(|| p.get("TargetFile"))
                            .or_else(|| p.get("AbsolutePath"))
                            .or_else(|| p.get("file_path"))
                    })
                    .and_then(|v| v.as_str());
                match body.get("state").and_then(|v| v.as_str()) {
                    Some("ACTIVE") => {
                        self.start_call(Some(&skey), tool_name, cmd, path, params);
                    }
                    Some("DONE") => {
                        let out_text = info
                            .and_then(|i| i.get("result").or_else(|| i.get("output")))
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        self.finish_call(
                            Some(&skey),
                            tool_name,
                            cmd,
                            path,
                            params,
                            out_text,
                            None,
                            false,
                        );
                    }
                    _ => {}
                }
            }
        } else if ekind == "result" {
            self.flush_agy_text(None);
            if body.get("status").and_then(|v| v.as_str()) == Some("ERROR") {
                let msg = body
                    .get("finished_reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("AGY failed");
                self.record_milestone("error", msg);
            } else if let Some(resp) = body.get("response").and_then(|v| v.as_str())
                && !resp.is_empty()
            {
                self.record_milestone("say", resp);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, OpenOptions};
    use std::io::Write;
    use tempfile::TempDir;

    #[test]
    fn redacts_tokens_keys_bearer_and_named_values_without_false_positives() {
        assert_eq!(redact_secrets("sk-a_b-a_b-a_b-a_b-"), "[redacted]");
        assert_eq!(
            redact_secrets(&format!("ghp_{}", "a".repeat(36))),
            "[redacted]"
        );
        assert_eq!(
            redact_secrets("xoxb-1234567890-abcdefghij"),
            "[redacted]"
        );
        assert_eq!(redact_secrets("AKIAA1A1A1A1A1A1A1A1"), "[redacted]");
        assert_eq!(
            redact_secrets("Authorization: Bearer abc.def_123+/="),
            "Authorization: Bearer [redacted]"
        );
        assert_eq!(
            redact_secrets(
                "-----BEGIN RSA PRIVATE KEY-----\nMIIE-secret\n-----END RSA PRIVATE KEY-----"
            ),
            "[redacted]"
        );
        assert_eq!(
            redact_secrets(r#"{"api_key": "long-value", "input_tokens": "120"}"#),
            r#"{"api_key": "[redacted]", "input_tokens": "120"}"#
        );
        assert_eq!(
            redact_secrets("DB_PASSWORD=sensitive-value token=12345678"),
            "DB_PASSWORD=[redacted] token=12345678"
        );
        let safe = "Commit 0123456789abcdef0123456789abcdef01234567; worker 012345abcdef.";
        assert_eq!(redact_secrets(safe), safe);
        // Multi-byte UTF-8 characters adjacent to secrets must never panic.
        assert_eq!(
            redact_secrets("Status ✅ — résumé… sk-a_b-a_b-a_b-a_b- done"),
            "Status ✅ — résumé… [redacted] done"
        );
    }

    #[test]
    fn extracts_heredoc_p_assignment_targets() {
        let cmd = "python3 - <<'PY'\np = 'packages/cli/src/gc.test.ts'\nprint(p)\nPY";
        let (summary, target) = extract_shell_target(cmd, None);
        assert_eq!(target, "packages/cli/src/gc.test.ts");
        assert_eq!(summary, "inline Python: packages/cli/src/gc.test.ts");
    }

    #[test]
    fn summarizes_last_tool_without_shell_pipe_soup() {
        let s1 = summarize_tool_human(
            "run",
            "ok",
            "git status --short; nl -ba bin/claivelib/cli.py | sed -n '1,10p'",
        );
        assert_eq!(s1, "Inspected git status --short");
        let s2 = summarize_tool_human(
            "run",
            "error",
            "\"rg -n 'retained_worktree|pending' bin/claivelib/cli.py | head -42",
        );
        assert!(s2.starts_with("Inspected rg ") && s2.ends_with("(failed)"));
    }

    #[test]
    fn incremental_tailer_handles_codex_parallel_heredoc_append_and_partial_line() {
        let tmp = TempDir::new().unwrap();
        let events_path = tmp.path().join("events.jsonl");

        let heredoc = "/bin/bash -lc \"python3 - <<'PY'\nfrom pathlib import Path\nPath('src/app.py').write_text('ok')\nPY\"";
        let line1 = serde_json::json!({
            "type": "item.started",
            "item": {"id": "c1", "type": "command_execution", "command": heredoc}
        });
        let line2 = serde_json::json!({
            "type": "item.started",
            "item": {"id": "c2", "type": "command_execution", "command": "/bin/bash -lc 'pytest tests/test_app.py'"}
        });
        let line3 = serde_json::json!({
            "type": "item.completed",
            "item": {"id": "c2", "type": "command_execution", "exit_code": 0, "aggregated_output": "3 passed in 0.12s"}
        });
        // Append an incomplete trailing line (no newline yet) to verify partial-write safety.
        let partial = r#"{"type":"item.completed","item":{"id":"c1","type":"command_execution""#;
        fs::write(&events_path, format!("{line1}\n{line2}\n{line3}\n{partial}")).unwrap();

        let mut tailer = EventsTailer::new(tmp.path(), "codex", None);
        tailer.refresh().unwrap();
        let snap1 = tailer.snapshot("w1", 20, None);
        assert_eq!(snap1.actions.len(), 2);
        assert_eq!(snap1.actions[0].category, "edit");
        assert_eq!(snap1.actions[0].target, "src/app.py");
        assert_eq!(snap1.actions[0].status, "running");
        assert_eq!(snap1.actions[1].category, "test");
        assert_eq!(snap1.actions[1].outcome.as_deref(), Some("3 passed in 0.12s"));

        // Finish writing the partial line + an agent_message with a secret token.
        let mut f = OpenOptions::new().append(true).open(&events_path).unwrap();
        f.write_all(
            br#","exit_code":0,"aggregated_output":"> @pkg build /repo\n> tsc -b\n"}}
"#,
        )
        .unwrap();
        writeln!(
            f,
            "{}",
            serde_json::json!({
                "type": "item.completed",
                "item": {"id": "m1", "type": "agent_message", "text": "Updated [app.py](src/app.py) with sk-a_b-a_b-a_b-a_b-"}
            })
        )
        .unwrap();

        tailer.refresh().unwrap();
        let snap2 = tailer.snapshot("w1", 20, None);
        assert_eq!(snap2.actions.len(), 3);
        assert_eq!(snap2.actions[0].status, "ok");
        assert_eq!(snap2.actions[0].outcome.as_deref(), Some("ok"));
        assert_eq!(snap2.actions[2].kind, "say");
        assert!(snap2.actions[2].summary.contains("[redacted]"));
        assert!(!snap2.actions[2].summary.contains("sk-a_b"));
    }

    #[test]
    fn tailer_pairs_pi_claude_and_agy_events() {
        let tmp = TempDir::new().unwrap();

        // Pi
        let pi_dir = tmp.path().join("pi");
        fs::create_dir(&pi_dir).unwrap();
        fs::write(
            pi_dir.join("events.jsonl"),
            [
                serde_json::json!({"type":"tool_execution_start","toolCallId":"p1","toolName":"edit","args":{"path":"lib/a.mjs","edits":[{"old":"a","new":"b"}]}}).to_string(),
                serde_json::json!({"type":"tool_execution_end","toolCallId":"p1","toolName":"edit","isError":false,"result":{"content":[{"type":"text","text":"applied"}]}}).to_string(),
            ]
            .join("\n")
                + "\n",
        )
        .unwrap();
        let mut pi_tailer = EventsTailer::new(&pi_dir, "pi", None);
        pi_tailer.refresh().unwrap();
        let pi_snap = pi_tailer.snapshot("pi1", 20, None);
        assert_eq!(pi_snap.actions.len(), 1);
        assert_eq!(pi_snap.actions[0].category, "edit");
        assert_eq!(pi_snap.actions[0].target, "lib/a.mjs");
        assert_eq!(pi_snap.actions[0].status, "ok");

        // Claude
        let cl_dir = tmp.path().join("claude");
        fs::create_dir(&cl_dir).unwrap();
        fs::write(
            cl_dir.join("events.jsonl"),
            [
                serde_json::json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Read","input":{"file_path":"/repo/main.py"}}]}}).to_string(),
                serde_json::json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tu1","is_error":false,"content":"print('hi')"}]}}).to_string(),
            ]
            .join("\n")
                + "\n",
        )
        .unwrap();
        let mut cl_tailer = EventsTailer::new(&cl_dir, "claude", Some("/repo".to_string()));
        cl_tailer.refresh().unwrap();
        let cl_snap = cl_tailer.snapshot("cl1", 20, None);
        assert_eq!(cl_snap.actions.len(), 1);
        assert_eq!(cl_snap.actions[0].category, "read");
        assert_eq!(cl_snap.actions[0].target, "main.py");

        // AGY (streaming delta before DONE still visible transiently in snapshot!)
        let agy_dir = tmp.path().join("agy");
        fs::create_dir(&agy_dir).unwrap();
        fs::write(
            agy_dir.join("events.jsonl"),
            [
                serde_json::json!({"event":"step_update","step_update":{"conversation_id":"c1","step_index":1,"step_type":"agent_response","state":"ACTIVE","text_delta":"Checking "}}).to_string(),
                serde_json::json!({"event":"step_update","step_update":{"conversation_id":"c1","step_index":1,"step_type":"agent_response","state":"DONE","text_delta":"files."}}).to_string(),
                serde_json::json!({"event":"step_update","step_update":{"conversation_id":"c1","step_index":2,"tool_name":"run_command","state":"ACTIVE","tool_info":{"parameters":{"CommandLine":"cargo test"}}}}).to_string(),
                serde_json::json!({"event":"step_update","step_update":{"conversation_id":"c1","step_index":2,"tool_name":"run_command","state":"DONE","tool_info":{"output":"12 passed"}}}).to_string(),
            ]
            .join("\n")
                + "\n",
        )
        .unwrap();
        let mut agy_tailer = EventsTailer::new(&agy_dir, "agy", None);
        agy_tailer.refresh().unwrap();
        let agy_snap = agy_tailer.snapshot("agy1", 20, None);
        assert_eq!(agy_snap.actions.len(), 2);
        assert_eq!(agy_snap.actions[0].kind, "say");
        assert_eq!(agy_snap.actions[0].summary, "Checking files.");
        assert_eq!(agy_snap.actions[1].category, "test");
        assert_eq!(agy_snap.actions[1].outcome.as_deref(), Some("12 passed"));
    }

    #[test]
    fn tailer_resets_cleanly_on_file_truncation() {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("events.jsonl");
        fs::write(
            &p,
            format!(
                "{}\n{}\n",
                serde_json::json!({"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"First long message for byte offset"}}),
                serde_json::json!({"type":"item.completed","item":{"id":"m2","type":"agent_message","text":"Second long message for byte offset"}})
            ),
        )
        .unwrap();
        let mut tailer = EventsTailer::new(tmp.path(), "codex", None);
        tailer.refresh().unwrap();
        assert_eq!(tailer.snapshot("w1", 20, None).actions.len(), 2);

        // Truncate/rewrite with a single shorter event.
        fs::write(
            &p,
            format!(
                "{}\n",
                serde_json::json!({"type":"item.completed","item":{"id":"m3","type":"agent_message","text":"Fresh"}})
            ),
        )
        .unwrap();
        tailer.refresh().unwrap();
        let snap = tailer.snapshot("w1", 20, None);
        assert_eq!(snap.actions.len(), 1);
        assert_eq!(snap.actions[0].summary, "Fresh");
    }
}
