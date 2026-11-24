//! Read-only Linux /proc PID identity & worker supervisor liveness verification.
//! Mirrors `bin/claivelib/cli.py:identity` / `load` and `bin/claivelib/watchers.py:_watcher_alive`.

use std::fs;
use std::path::Path;
use std::sync::OnceLock;

use serde::Deserialize;

static BOOT_ID: OnceLock<Option<String>> = OnceLock::new();

pub const ACTIVE_STATUSES: &[&str] = &["starting", "running", "cancelling", "idle"];
pub const SPAWN_GRACE_SECS: f64 = 300.0;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LivenessOutcome {
    Alive,
    Interrupted(&'static str),
    Cancelled(&'static str),
}

#[derive(Debug, Deserialize)]
struct SpawnRecord {
    pid: Option<i32>,
    identity: Option<String>,
}

/// Read `/proc/sys/kernel/random/boot_id` once per process.
pub fn boot_id() -> Option<&'static str> {
    BOOT_ID
        .get_or_init(|| {
            fs::read_to_string("/proc/sys/kernel/random/boot_id")
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        })
        .as_deref()
}

/// Parse `/proc/<pid>/stat` safely by splitting after the LAST `)` so comm names containing
/// spaces or parentheses cannot misalign fields. Returns `(state_char, field_22_starttime)`.
pub fn parse_proc_stat(content: &str) -> Option<(char, &str)> {
    let (_, rest) = content.rsplit_once(')')?;
    let mut parts = rest.split_whitespace();
    let state = parts.next()?.chars().next()?;
    // After ')': index 0 is field 3 (state); field 22 (starttime) is index 19.
    let starttime = parts.nth(18)?;
    if starttime.is_empty() || !starttime.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((state, starttime))
}

/// Build `<boot_id>:<starttime>` matching Python `cli.identity(pid)`. Returns `None` for zombies.
pub fn pid_identity_with_boot(pid: i32, boot: &str) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    let content = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (state, starttime) = parse_proc_stat(&content)?;
    if state == 'Z' {
        return None;
    }
    Some(format!("{boot}:{starttime}"))
}

/// Read the live kernel identity for `pid`. Returns `None` if gone, zombie, or unreadable.
pub fn pid_identity(pid: i32) -> Option<String> {
    let boot = boot_id()?;
    pid_identity_with_boot(pid, boot)
}

/// Unverified legacy existence check (`kill(pid, 0)`), matching `watchers.py` and `spawn.json` fallback.
pub fn pid_exists_unverified(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    let err = std::io::Error::last_os_error().raw_os_error();
    err == Some(libc::EPERM)
}

/// Check if a watcher record in `$CLAIVE_DIR/watchers/*.json` is alive.
pub fn verify_watcher_alive(pid: Option<i32>, recorded_identity: Option<&str>) -> bool {
    let Some(pid) = pid else {
        return false;
    };
    if pid <= 0 {
        return false;
    }
    match recorded_identity {
        Some(expected) => pid_identity(pid).as_deref() == Some(expected),
        None => pid_exists_unverified(pid),
    }
}

/// Read-only supervisor liveness check mirroring `bin/claivelib/cli.py:load()`.
pub fn check_worker_liveness(
    job_dir: &Path,
    status: &str,
    supervisor_pid: Option<i32>,
    supervisor_identity: Option<&str>,
    started_at: Option<f64>,
    now: f64,
) -> LivenessOutcome {
    if !ACTIVE_STATUSES.contains(&status) {
        return LivenessOutcome::Alive;
    }

    if let Some(pid) = supervisor_pid {
        if pid_identity(pid).as_deref() != supervisor_identity {
            return LivenessOutcome::Interrupted("worker supervisor exited without a final result");
        }
        return LivenessOutcome::Alive;
    }

    // No supervisor_pid recorded yet: check spawn.json or 300s start grace.
    let spawn_path = job_dir.join("spawn.json");
    let gone = if spawn_path.is_file() {
        match fs::read_to_string(&spawn_path)
            .ok()
            .and_then(|raw| serde_json::from_str::<SpawnRecord>(&raw).ok())
        {
            Some(spawn) => match (spawn.pid, spawn.identity.as_deref()) {
                (Some(pid), None) if pid > 0 => !pid_exists_unverified(pid),
                (Some(pid), Some(expected)) => pid_identity(pid).as_deref() != Some(expected),
                _ => true,
            },
            None => started_at
                .map(|s| now - s > SPAWN_GRACE_SECS)
                .unwrap_or(false),
        }
    } else {
        started_at
            .map(|s| now - s > SPAWN_GRACE_SECS)
            .unwrap_or(false)
    };

    if gone {
        if job_dir.join("cancel.request").exists() {
            LivenessOutcome::Cancelled("worker cancelled before its supervisor started")
        } else {
            LivenessOutcome::Interrupted("worker supervisor did not start")
        }
    } else {
        LivenessOutcome::Alive
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn parse_proc_stat_handles_parens_and_spaces_in_comm() {
        // Real Linux /proc/pid/stat shape with weird comm containing spaces & ')'
        let stat = "12345 (my (weird) proc) S 1 12345 12345 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 987654321 100000 50 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0";
        let (state, start) = parse_proc_stat(stat).expect("should parse stat");
        assert_eq!(state, 'S');
        assert_eq!(start, "987654321");
    }

    #[test]
    fn parse_proc_stat_detects_zombie_state() {
        let stat = "99 (defunct) Z 1 99 99 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 424242 0 0 0";
        let (state, start) = parse_proc_stat(stat).expect("should parse zombie");
        assert_eq!(state, 'Z');
        assert_eq!(start, "424242");
    }

    #[test]
    fn live_child_identity_matches_while_alive_and_disappears_after_exit() {
        let mut child = Command::new("sleep")
            .arg("5")
            .spawn()
            .expect("spawn sleep");
        let pid = child.id() as i32;
        let id = pid_identity(pid).expect("live child must have identity");
        assert!(verify_watcher_alive(Some(pid), Some(&id)));
        assert!(!verify_watcher_alive(Some(pid), Some("wrong-boot:999")));

        let _ = child.kill();
        let _ = child.wait();
        assert!(pid_identity(pid).is_none());
        assert!(!verify_watcher_alive(Some(pid), Some(&id)));
    }

    #[test]
    fn worker_liveness_honors_spawn_grace_and_cancel_request() {
        let tmp = tempfile::tempdir().unwrap();
        // Within 300s grace -> Alive
        assert_eq!(
            check_worker_liveness(tmp.path(), "starting", None, None, Some(1000.0), 1200.0),
            LivenessOutcome::Alive
        );
        // Past 300s grace without cancel.request -> Interrupted
        assert!(matches!(
            check_worker_liveness(tmp.path(), "starting", None, None, Some(1000.0), 1301.0),
            LivenessOutcome::Interrupted(_)
        ));
        // Past 300s grace with cancel.request -> Cancelled
        fs::write(tmp.path().join("cancel.request"), "{}").unwrap();
        assert!(matches!(
            check_worker_liveness(tmp.path(), "starting", None, None, Some(1000.0), 1301.0),
            LivenessOutcome::Cancelled(_)
        ));
    }
}
