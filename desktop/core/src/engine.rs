//! Client for the engine's local supervisor API (`/api/v1/local`, see backend `app/api/routes/local.py`).
//!
//! Loopback only, through the local gateway. Every request is signed with the stack's supervisor key:
//! `base64(HMAC-SHA256(key, "RRLOCAL\n{METHOD}\n{path}\n{ts}\n{nonce}\n{sha256(body)}"))`, with a fresh timestamp and
//! a random single-use nonce. The API can read the safety state, create the first administrator and register the
//! DEMO account the connector is logged in to — nothing that trades or changes risk or LIVE settings.

use crate::health::{http_request_loopback, ProbeError};
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const TIMEOUT: Duration = Duration::from_secs(8);

pub fn sign(key: &str, method: &str, path: &str, ts: &str, nonce: &str, body: &[u8]) -> String {
    let digest = hex(&Sha256::digest(body));
    let msg = format!("RRLOCAL\n{}\n{path}\n{ts}\n{nonce}\n{digest}", method.to_ascii_uppercase());
    let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    mac.update(msg.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn nonce() -> String {
    let mut b = [0u8; 12];
    getrandom::fill(&mut b).expect("OS random generator");
    hex(&b)
}

pub struct LocalEngine<'a> {
    pub port: u16,
    pub key: &'a str,
}

impl LocalEngine<'_> {
    fn call(&self, method: &str, path: &str, body: Option<&serde_json::Value>) -> Result<serde_json::Value, String> {
        let raw = body.map(|b| b.to_string().into_bytes()).unwrap_or_default();
        let ts = format!("{:.3}", SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0));
        let n = nonce();
        let mut headers = vec![
            ("X-RR-Local-Ts".to_string(), ts.clone()),
            ("X-RR-Local-Nonce".to_string(), n.clone()),
            ("X-RR-Local-Sig".to_string(), sign(self.key, method, path, &ts, &n, &raw)),
        ];
        if body.is_some() {
            headers.push(("Content-Type".into(), "application/json".into()));
        }
        let r = http_request_loopback(self.port, method, path, &headers, &raw, TIMEOUT).map_err(|e| match e {
            ProbeError::Connect(_) => "the engine is not running".to_string(),
            other => other.to_string(),
        })?;
        let v: serde_json::Value = serde_json::from_str(&r.body).unwrap_or(serde_json::Value::Null);
        if (200..300).contains(&r.status) {
            return Ok(v);
        }
        let detail = match v.get("detail") {
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(other) => other.to_string(),
            None => format!("HTTP {}", r.status),
        };
        Err(detail)
    }

    pub fn status(&self) -> Result<serde_json::Value, String> {
        self.call("GET", "/api/v1/local/status", None)
    }

    pub fn bootstrap_admin(&self, username: &str, password: &str) -> Result<serde_json::Value, String> {
        self.call("POST", "/api/v1/local/bootstrap-admin",
            Some(&serde_json::json!({"username": username, "password": password})))
    }

    pub fn use_demo_account(&self, login: u64, server: &str) -> Result<serde_json::Value, String> {
        self.call("POST", "/api/v1/local/demo-account", Some(&serde_json::json!({"login": login, "server": server})))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    /// Same vector as backend/tests/test_local_api.py: both implementations must agree byte for byte.
    #[test]
    fn signature_matches_python_test_vector() {
        let sig = sign(&"k".repeat(40), "POST", "/api/v1/local/demo-account", "1794580000", "nonce-1",
            br#"{"login":1001,"server":"Demo-Server"}"#);
        assert_eq!(sig, "dd2GgOMPmpjcDzrKCY63qGvkTNxKWny/K9fk9p1q2u8=");
    }

    /// Loopback server that verifies the signature like the engine does and answers `status`/`body`.
    fn serve(key: &'static str, status: u16, body: &'static str) -> (u16, std::thread::JoinHandle<(String, bool)>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let h = std::thread::spawn(move || {
            let (mut c, _) = l.accept().unwrap();
            let mut buf = vec![0u8; 16384];
            let mut n = 0;
            loop {
                n += c.read(&mut buf[n..]).unwrap();
                let text = String::from_utf8_lossy(&buf[..n]).to_string();
                if let Some(pos) = text.find("\r\n\r\n") {
                    let len = text.lines().find_map(|l| l.strip_prefix("Content-Length: ")).map(|v| v.trim().parse::<usize>().unwrap()).unwrap_or(0);
                    if n >= pos + 4 + len {
                        break;
                    }
                }
            }
            let text = String::from_utf8_lossy(&buf[..n]).to_string();
            let (head, req_body) = text.split_once("\r\n\r\n").unwrap();
            let line = head.lines().next().unwrap().to_string();
            let mut parts = line.split(' ');
            let (method, path) = (parts.next().unwrap(), parts.next().unwrap());
            let hv = |k: &str| head.lines().find_map(|l| l.strip_prefix(&format!("{k}: "))).unwrap_or("").to_string();
            let ok = sign(key, method, path, &hv("X-RR-Local-Ts"), &hv("X-RR-Local-Nonce"), req_body.as_bytes()) == hv("X-RR-Local-Sig");
            let resp = format!("HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len());
            c.write_all(resp.as_bytes()).unwrap();
            (format!("{line}\n{req_body}"), ok)
        });
        (port, h)
    }

    #[test]
    fn requests_are_signed_and_parsed() {
        let (port, h) = serve("supervisor-key-123", 200, r#"{"admin_exists":false}"#);
        let e = LocalEngine { port, key: "supervisor-key-123" };
        assert_eq!(e.status().unwrap()["admin_exists"], false);
        let (req, sig_ok) = h.join().unwrap();
        assert!(req.starts_with("GET /api/v1/local/status HTTP/1.1") && sig_ok);
    }

    #[test]
    fn post_body_is_signed_and_errors_surface_the_detail() {
        let (port, h) = serve("k2", 409, r#"{"detail":"the engine already has users; sign in to manage them"}"#);
        let e = LocalEngine { port, key: "k2" };
        let err = e.bootstrap_admin("owner", "Correct-Horse-1").unwrap_err();
        assert!(err.contains("already has users"), "{err}");
        let (req, sig_ok) = h.join().unwrap();
        assert!(sig_ok && req.contains(r#""username":"owner""#));
    }

    #[test]
    fn engine_down_is_reported_plainly() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert_eq!(LocalEngine { port, key: "k" }.status().unwrap_err(), "the engine is not running");
    }
}
