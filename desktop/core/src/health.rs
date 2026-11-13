//! Real health checks (spec Part 5: "verify service health through actual health checks, not just process
//! existence").
//!
//! The probe speaks HTTP/1.1 to **loopback only** (127.0.0.1); it cannot be pointed at another host. The
//! engine's `/api/health` answers `200 {"status":"ok"}` only after a successful `select 1` on PostgreSQL and a
//! Redis `PING`, so one successful probe through the local gateway proves gateway + API + PostgreSQL + Redis.

use serde::Serialize;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::time::{Duration, Instant};

const MAX_RESPONSE: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HealthState {
    Healthy,
    /// Reachable but the check failed (wrong status, wrong body, dependency down).
    Unhealthy,
    /// Nothing answered (not started, crashed, port closed).
    Unreachable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProbeResult {
    pub target: &'static str,
    pub state: HealthState,
    pub detail: String,
    pub latency_ms: Option<u64>,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ProbeError {
    #[error("invalid probe path")]
    BadPath,
    #[error("connection failed: {0}")]
    Connect(String),
    #[error("i/o error: {0}")]
    Io(String),
    #[error("malformed HTTP response")]
    Malformed,
    #[error("response too large")]
    TooLarge,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub body: String,
}

fn valid_path(path: &str) -> bool {
    path.starts_with('/') && path.len() <= 256 && path.bytes().all(|b| b.is_ascii_graphic())
}

/// `GET http://127.0.0.1:{port}{path}` with a hard timeout.
pub fn http_get_loopback(port: u16, path: &str, timeout: Duration) -> Result<HttpResponse, ProbeError> {
    http_request_loopback(port, "GET", path, &[], b"", timeout)
}

fn header_ok(s: &str) -> bool {
    !s.is_empty() && s.len() <= 512 && s.bytes().all(|b| (0x20..0x7f).contains(&b))
}

/// Minimal HTTP/1.1 request to **127.0.0.1 only**. Method, path and headers are validated (no CR/LF injection).
pub fn http_request_loopback(port: u16, method: &str, path: &str, headers: &[(String, String)], body: &[u8],
                             timeout: Duration) -> Result<HttpResponse, ProbeError> {
    if !valid_path(path) || !matches!(method, "GET" | "POST") {
        return Err(ProbeError::BadPath);
    }
    if headers.iter().any(|(k, v)| !header_ok(k) || k.contains(':') || k.contains(' ') || !header_ok(v)) {
        return Err(ProbeError::BadPath);
    }
    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut s = TcpStream::connect_timeout(&addr, timeout).map_err(|e| ProbeError::Connect(e.kind().to_string()))?;
    let io = |e: std::io::Error| ProbeError::Io(e.kind().to_string());
    s.set_read_timeout(Some(timeout)).map_err(io)?;
    s.set_write_timeout(Some(timeout)).map_err(io)?;
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUser-Agent: rr-trading-one-supervisor\r\nAccept: */*\r\nConnection: close\r\n"
    );
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    if method == "POST" {
        req.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    req.push_str("\r\n");
    s.write_all(req.as_bytes()).map_err(io)?;
    s.write_all(body).map_err(io)?;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let n = s.read(&mut chunk).map_err(io)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > MAX_RESPONSE {
            return Err(ProbeError::TooLarge);
        }
    }
    parse_response(&buf)
}

fn parse_response(buf: &[u8]) -> Result<HttpResponse, ProbeError> {
    let split = buf.windows(4).position(|w| w == b"\r\n\r\n").ok_or(ProbeError::Malformed)?;
    let head = std::str::from_utf8(&buf[..split]).map_err(|_| ProbeError::Malformed)?;
    let raw_body = &buf[split + 4..];
    let mut lines = head.split("\r\n");
    let status_line = lines.next().ok_or(ProbeError::Malformed)?;
    let mut parts = status_line.splitn(3, ' ');
    let ver = parts.next().unwrap_or("");
    if !ver.starts_with("HTTP/1.") {
        return Err(ProbeError::Malformed);
    }
    let status: u16 = parts.next().and_then(|c| c.parse().ok()).ok_or(ProbeError::Malformed)?;
    let chunked = lines.any(|l| {
        let l = l.to_ascii_lowercase();
        l.starts_with("transfer-encoding:") && l.contains("chunked")
    });
    let body = if chunked { dechunk(raw_body)? } else { raw_body.to_vec() };
    Ok(HttpResponse { status, body: String::from_utf8_lossy(&body).into_owned() })
}

fn dechunk(mut b: &[u8]) -> Result<Vec<u8>, ProbeError> {
    let mut out = Vec::new();
    loop {
        let eol = b.windows(2).position(|w| w == b"\r\n").ok_or(ProbeError::Malformed)?;
        let size_str = std::str::from_utf8(&b[..eol]).map_err(|_| ProbeError::Malformed)?;
        let size = usize::from_str_radix(size_str.split(';').next().unwrap_or("").trim(), 16)
            .map_err(|_| ProbeError::Malformed)?;
        b = &b[eol + 2..];
        if size == 0 {
            return Ok(out);
        }
        if b.len() < size + 2 {
            return Err(ProbeError::Malformed);
        }
        out.extend_from_slice(&b[..size]);
        b = &b[size + 2..];
    }
}

fn timed(target: &'static str, f: impl FnOnce() -> Result<HttpResponse, ProbeError>,
         judge: impl FnOnce(&HttpResponse) -> Result<String, String>) -> ProbeResult {
    let t0 = Instant::now();
    match f() {
        Ok(resp) => {
            let latency = Some(t0.elapsed().as_millis() as u64);
            match judge(&resp) {
                Ok(detail) => ProbeResult { target, state: HealthState::Healthy, detail, latency_ms: latency },
                Err(detail) => ProbeResult { target, state: HealthState::Unhealthy, detail, latency_ms: latency },
            }
        }
        Err(ProbeError::Connect(e)) => ProbeResult {
            target, state: HealthState::Unreachable, detail: format!("not reachable ({e})"), latency_ms: None,
        },
        Err(e) => ProbeResult { target, state: HealthState::Unhealthy, detail: e.to_string(), latency_ms: None },
    }
}

/// Engine API + PostgreSQL + Redis, through the local gateway.
pub fn probe_engine(port: u16, timeout: Duration) -> ProbeResult {
    timed("engine", || http_get_loopback(port, "/api/health", timeout), |r| {
        let ok_body = serde_json::from_str::<serde_json::Value>(&r.body)
            .map(|v| v.get("status").and_then(|s| s.as_str()) == Some("ok"))
            .unwrap_or(false);
        match (r.status, ok_body) {
            (200, true) => Ok("API, PostgreSQL and Redis responding".into()),
            (200, false) => Err("unexpected health response body".into()),
            (s, _) => Err(format!("health endpoint returned HTTP {s} (API up, a dependency is failing)")),
        }
    })
}

/// The web interface (Next.js behind the gateway). A redirect to /login counts as up.
pub fn probe_web(port: u16, timeout: Duration) -> ProbeResult {
    timed("web", || http_get_loopback(port, "/login", timeout), |r| match r.status {
        200..=399 => Ok(format!("HTTP {}", r.status)),
        s => Err(format!("HTTP {s}")),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    /// One-shot loopback server returning `resp`; returns (port, handle with the received request).
    fn serve(resp: &'static [u8]) -> (u16, thread::JoinHandle<String>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let h = thread::spawn(move || {
            let (mut c, _) = l.accept().unwrap();
            let mut buf = [0u8; 4096];
            let n = c.read(&mut buf).unwrap();
            c.write_all(resp).unwrap();
            String::from_utf8_lossy(&buf[..n]).into_owned()
        });
        (port, h)
    }

    fn free_port() -> u16 {
        TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
    }

    const T: Duration = Duration::from_secs(2);

    #[test]
    fn healthy_engine() {
        let (port, h) = serve(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}");
        let r = probe_engine(port, T);
        assert_eq!(r.state, HealthState::Healthy, "{r:?}");
        let req = h.join().unwrap();
        assert!(req.starts_with("GET /api/health HTTP/1.1\r\n"));
        assert!(req.contains(&format!("Host: 127.0.0.1:{port}")));
    }

    #[test]
    fn dependency_failure_is_unhealthy_not_healthy() {
        let (port, _h) = serve(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 2\r\n\r\n{}");
        let r = probe_engine(port, T);
        assert_eq!(r.state, HealthState::Unhealthy);
        assert!(r.detail.contains("500"));
    }

    #[test]
    fn wrong_body_with_200_is_unhealthy() {
        let (port, _h) = serve(b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n<html></html>");
        assert_eq!(probe_engine(port, T).state, HealthState::Unhealthy);
    }

    #[test]
    fn closed_port_is_unreachable() {
        let r = probe_engine(free_port(), Duration::from_millis(500));
        assert_eq!(r.state, HealthState::Unreachable, "{r:?}");
    }

    #[test]
    fn chunked_body_is_decoded() {
        let (port, _h) = serve(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nf\r\n{\"status\":\"ok\"}\r\n0\r\n\r\n");
        assert_eq!(probe_engine(port, T).state, HealthState::Healthy);
    }

    #[test]
    fn web_redirect_counts_as_up() {
        let (port, _h) = serve(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: /login\r\nContent-Length: 0\r\n\r\n");
        assert_eq!(probe_web(port, T).state, HealthState::Healthy);
    }

    #[test]
    fn header_injection_paths_rejected() {
        for p in ["api/health", "/a b", "/a\r\nX-Evil: 1", "/\u{e9}"] {
            assert_eq!(http_get_loopback(1, p, T), Err(ProbeError::BadPath), "{p:?}");
        }
    }

    #[test]
    fn garbage_response_is_malformed() {
        assert_eq!(parse_response(b"hello"), Err(ProbeError::Malformed));
        assert_eq!(parse_response(b"SSH-2.0-x\r\n\r\n"), Err(ProbeError::Malformed));
    }
}
