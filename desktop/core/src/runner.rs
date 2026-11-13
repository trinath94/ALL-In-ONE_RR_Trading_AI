//! Process execution without a shell.
//!
//! A [`CommandSpec`] is a program path plus an argument vector; it is executed directly (no `cmd.exe`, no
//! `sh -c`), so arguments are never re-parsed. Secrets travel only in the child's environment, never in argv, and
//! are redacted from `Debug` output. Every command has a hard timeout after which the child is killed.

use std::fmt;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const MAX_CAPTURE: usize = 256 * 1024;

#[derive(Clone)]
pub struct CommandSpec {
    pub program: PathBuf,
    pub args: Vec<String>,
    /// Extra environment for the child (e.g. stack secrets). Values are never logged.
    pub env: Vec<(String, String)>,
    pub timeout: Duration,
    /// When set, stdout is streamed to this file instead of being captured (used for database dumps).
    pub stdout_file: Option<PathBuf>,
}

impl fmt::Debug for CommandSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CommandSpec")
            .field("program", &self.program)
            .field("args", &self.args)
            .field("env", &self.env.iter().map(|(k, _)| format!("{k}=***")).collect::<Vec<_>>())
            .field("timeout", &self.timeout)
            .field("stdout_file", &self.stdout_file)
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CommandOutput {
    /// Exit code; `None` if killed (timeout) or terminated by a signal.
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
}

impl CommandOutput {
    pub fn success(&self) -> bool {
        self.code == Some(0) && !self.timed_out
    }

    /// Short, single-line description of a failure for reports.
    pub fn failure_summary(&self) -> String {
        if self.timed_out {
            return "timed out".into();
        }
        let msg = self.stderr.trim();
        let msg = if msg.is_empty() { self.stdout.trim() } else { msg };
        let last: Vec<&str> = msg.lines().rev().take(3).collect();
        let joined = last.into_iter().rev().collect::<Vec<_>>().join(" | ");
        format!("exit {}: {}", self.code.map(|c| c.to_string()).unwrap_or_else(|| "?".into()), truncate(&joined, 400))
    }
}

pub fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect::<String>() + "…"
    }
}

pub trait Runner: Send + Sync {
    fn run(&self, spec: &CommandSpec) -> std::io::Result<CommandOutput>;
}

/// Executes real processes.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemRunner;

fn reader<R: Read + Send + 'static>(mut r: R) -> std::thread::JoinHandle<String> {
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 8192];
        while let Ok(n) = r.read(&mut chunk) {
            if n == 0 {
                break;
            }
            if buf.len() < MAX_CAPTURE {
                buf.extend_from_slice(&chunk[..n.min(MAX_CAPTURE - buf.len())]);
            }
        }
        String::from_utf8_lossy(&buf).into_owned()
    })
}

impl Runner for SystemRunner {
    fn run(&self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
        if !spec.program.is_absolute() {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "program path must be absolute"));
        }
        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args).stdin(Stdio::null()).stderr(Stdio::piped());
        for (k, v) in &spec.env {
            cmd.env(k, v);
        }
        match &spec.stdout_file {
            Some(p) => {
                cmd.stdout(Stdio::from(std::fs::File::create(p)?));
            }
            None => {
                cmd.stdout(Stdio::piped());
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd.spawn()?;
        let out_h = child.stdout.take().map(reader);
        let err_h = child.stderr.take().map(reader);
        let start = Instant::now();
        let (code, timed_out) = loop {
            if let Some(st) = child.try_wait()? {
                break (st.code(), false);
            }
            if start.elapsed() >= spec.timeout {
                let _ = child.kill();
                let _ = child.wait();
                break (None, true);
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let stdout = out_h.map(|h| h.join().unwrap_or_default()).unwrap_or_default();
        let stderr = err_h.map(|h| h.join().unwrap_or_default()).unwrap_or_default();
        Ok(CommandOutput { code, stdout, stderr, timed_out })
    }
}

/// Find an executable by name on PATH, returning an absolute path. Never resolves relative to the current
/// directory.
pub fn which(name: &str, extra: &[PathBuf]) -> Option<PathBuf> {
    let exe = if cfg!(windows) && !name.ends_with(".exe") { format!("{name}.exe") } else { name.to_string() };
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .chain(extra.iter().cloned())
        .filter(|d| d.is_absolute())
        .map(|d| d.join(&exe))
        .find(|p| p.is_file())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn spec(program: &str, args: &[&str]) -> CommandSpec {
        CommandSpec {
            program: PathBuf::from(program),
            args: args.iter().map(|s| s.to_string()).collect(),
            env: vec![],
            timeout: Duration::from_secs(5),
            stdout_file: None,
        }
    }

    #[test]
    fn runs_without_shell_interpretation() {
        let out = SystemRunner.run(&spec("/bin/echo", &["a; rm -rf /tmp/x", "$(whoami)", "`id`"])).unwrap();
        assert!(out.success());
        assert_eq!(out.stdout.trim(), "a; rm -rf /tmp/x $(whoami) `id`");
    }

    #[test]
    fn env_reaches_child_and_is_redacted_in_debug() {
        let mut s = spec("/usr/bin/env", &[]);
        s.env.push(("RR_ONE_TEST_SECRET".into(), "s3cr3t-value".into()));
        let out = SystemRunner.run(&s).unwrap();
        assert!(out.stdout.contains("RR_ONE_TEST_SECRET=s3cr3t-value"));
        let dbg = format!("{s:?}");
        assert!(dbg.contains("RR_ONE_TEST_SECRET=***") && !dbg.contains("s3cr3t-value"));
    }

    #[test]
    fn timeout_kills_child() {
        let mut s = spec("/bin/sleep", &["10"]);
        s.timeout = Duration::from_millis(300);
        let t = Instant::now();
        let out = SystemRunner.run(&s).unwrap();
        assert!(out.timed_out && !out.success());
        assert!(t.elapsed() < Duration::from_secs(5));
        assert_eq!(out.failure_summary(), "timed out");
    }

    #[test]
    fn relative_program_rejected() {
        assert!(SystemRunner.run(&spec("echo", &["x"])).is_err());
    }

    #[test]
    fn nonzero_exit_reported() {
        let out = SystemRunner.run(&spec("/bin/sh", &["-c", "echo boom >&2; exit 3"])).unwrap();
        assert_eq!(out.code, Some(3));
        assert_eq!(out.failure_summary(), "exit 3: boom");
    }

    #[test]
    fn stdout_to_file() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("dump.bin");
        let mut s = spec("/bin/echo", &["payload"]);
        s.stdout_file = Some(p.clone());
        assert!(SystemRunner.run(&s).unwrap().success());
        assert_eq!(std::fs::read_to_string(p).unwrap().trim(), "payload");
    }
}
