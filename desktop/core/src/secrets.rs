//! Local stack secrets: PostgreSQL and Redis passwords, the engine's JWT secret, its Fernet encryption key and
//! the bridge secret.
//!
//! * Generated once from the OS CSPRNG.
//! * Stored in `ProgramData\RR Trading One\secrets\stack.secrets`, encrypted with Windows DPAPI (current-user
//!   scope); the directory ACL is restricted separately by the supervisor. On non-Windows development machines
//!   the file is plaintext with mode 0600 in a 0700 directory.
//! * Handed to Docker Compose only through the child process environment, never through argv or an `.env` file.
//! * **Never regenerated** when an existing file cannot be read or decrypted: new passwords would lock the
//!   existing database out and a new Fernet key would make stored broker credentials unreadable. That situation
//!   is reported for recovery from backup instead.

use base64::Engine;
use serde::{Deserialize, Serialize};
use std::fmt;
use std::path::{Path, PathBuf};

pub const FILE_NAME: &str = "stack.secrets";
const FORMAT_VERSION: u32 = 1;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StackSecrets {
    version: u32,
    db_password: String,
    redis_password: String,
    jwt_secret: String,
    /// Fernet key (url-safe base64 of 32 bytes). Losing it makes stored broker credentials unrecoverable.
    encryption_key: String,
    bridge_secret: String,
}

impl fmt::Debug for StackSecrets {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("StackSecrets { *** }")
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SecretsError {
    #[error("secure random generator failed: {0}")]
    Random(String),
    #[error("secrets file i/o: {0}")]
    Io(#[from] std::io::Error),
    #[error("secrets file exists but could not be decrypted ({0}); it was NOT replaced. Restore it from backup or \
             run the recovery procedure")]
    Undecryptable(String),
    #[error("secrets file is corrupt ({0}); it was NOT replaced")]
    Corrupt(String),
}

const ALNUM: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

fn random_bytes(n: usize) -> Result<Vec<u8>, SecretsError> {
    let mut b = vec![0u8; n];
    getrandom::fill(&mut b).map_err(|e| SecretsError::Random(e.to_string()))?;
    Ok(b)
}

/// Alphanumeric only, so values are safe inside connection URLs without escaping. Rejection sampling avoids
/// modulo bias. 32 chars ≈ 190 bits.
fn random_alnum(len: usize) -> Result<String, SecretsError> {
    let mut out = String::with_capacity(len);
    while out.len() < len {
        for b in random_bytes(64)? {
            if (b as usize) < 248 {
                out.push(ALNUM[(b as usize) % 62] as char);
                if out.len() == len {
                    break;
                }
            }
        }
    }
    Ok(out)
}

impl StackSecrets {
    pub fn generate() -> Result<Self, SecretsError> {
        Ok(StackSecrets {
            version: FORMAT_VERSION,
            db_password: random_alnum(32)?,
            redis_password: random_alnum(32)?,
            jwt_secret: random_alnum(64)?,
            encryption_key: base64::engine::general_purpose::URL_SAFE.encode(random_bytes(32)?),
            bridge_secret: random_alnum(48)?,
        })
    }

    /// Environment for Docker Compose interpolation (see infrastructure/compose.yml).
    pub fn compose_env(&self) -> Vec<(String, String)> {
        vec![
            ("RR_ONE_DB_PASSWORD".into(), self.db_password.clone()),
            ("RR_ONE_REDIS_PASSWORD".into(), self.redis_password.clone()),
            ("RR_ONE_JWT_SECRET".into(), self.jwt_secret.clone()),
            ("RR_ONE_ENCRYPTION_KEY".into(), self.encryption_key.clone()),
            ("RR_ONE_BRIDGE_SECRET".into(), self.bridge_secret.clone()),
        ]
    }

    /// Every secret value, for redacting text that might echo one (e.g. tool error output).
    pub fn values(&self) -> [&str; 5] {
        [&self.db_password, &self.redis_password, &self.jwt_secret, &self.encryption_key, &self.bridge_secret]
    }

    pub fn redact(&self, text: &str) -> String {
        let mut s = text.to_string();
        for v in self.values() {
            if !v.is_empty() {
                s = s.replace(v, "***");
            }
        }
        s
    }
}

/// Encrypts the serialized secrets at rest.
pub trait Protector {
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>, String>;
    fn unprotect(&self, sealed: &[u8]) -> Result<Vec<u8>, String>;
}

/// Development only (non-Windows): no encryption; confidentiality relies on 0600/0700 permissions.
pub struct PlainFileProtector;

impl Protector for PlainFileProtector {
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>, String> {
        Ok(plain.to_vec())
    }
    fn unprotect(&self, sealed: &[u8]) -> Result<Vec<u8>, String> {
        Ok(sealed.to_vec())
    }
}

/// Windows DPAPI, current-user scope, no UI.
#[cfg(windows)]
pub struct DpapiProtector;

#[cfg(windows)]
impl DpapiProtector {
    fn call(input: &[u8], encrypt: bool) -> Result<Vec<u8>, String> {
        use windows_sys::Win32::Foundation::LocalFree;
        use windows_sys::Win32::Security::Cryptography::{
            CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
        };
        let inb = CRYPT_INTEGER_BLOB { cbData: input.len() as u32, pbData: input.as_ptr() as *mut u8 };
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: std::ptr::null_mut() };
        // SAFETY: pointers reference live buffers for the duration of the call; the output buffer is allocated by
        // the OS and released with LocalFree after copying.
        let ok = unsafe {
            if encrypt {
                CryptProtectData(&inb, std::ptr::null(), std::ptr::null(), std::ptr::null(), std::ptr::null(),
                    CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            } else {
                CryptUnprotectData(&inb, std::ptr::null_mut(), std::ptr::null(), std::ptr::null(),
                    std::ptr::null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            }
        };
        if ok == 0 || out.pbData.is_null() {
            return Err(format!("DPAPI {} failed: {}", if encrypt { "protect" } else { "unprotect" },
                std::io::Error::last_os_error()));
        }
        // SAFETY: out.pbData points to out.cbData bytes allocated by DPAPI.
        let v = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec() };
        unsafe { LocalFree(out.pbData as _) };
        Ok(v)
    }
}

#[cfg(windows)]
impl Protector for DpapiProtector {
    fn protect(&self, plain: &[u8]) -> Result<Vec<u8>, String> {
        Self::call(plain, true)
    }
    fn unprotect(&self, sealed: &[u8]) -> Result<Vec<u8>, String> {
        Self::call(sealed, false)
    }
}

/// The protector appropriate for this platform.
pub fn platform_protector() -> Box<dyn Protector + Send + Sync> {
    #[cfg(windows)]
    {
        Box::new(DpapiProtector)
    }
    #[cfg(not(windows))]
    {
        Box::new(PlainFileProtector)
    }
}

pub fn secrets_file(dir: &Path) -> PathBuf {
    dir.join(FILE_NAME)
}

/// Load the stack secrets, creating them on first run. Returns `(secrets, created)`.
pub fn load_or_create(dir: &Path, protector: &dyn Protector) -> Result<(StackSecrets, bool), SecretsError> {
    let path = secrets_file(dir);
    match std::fs::read(&path) {
        Ok(sealed) => {
            let plain = protector.unprotect(&sealed).map_err(SecretsError::Undecryptable)?;
            let s: StackSecrets = serde_json::from_slice(&plain).map_err(|e| SecretsError::Corrupt(e.to_string()))?;
            if s.version != FORMAT_VERSION || s.values().iter().any(|v| v.len() < 16) {
                return Err(SecretsError::Corrupt("unexpected format".into()));
            }
            Ok((s, false))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            create_private_dir(dir)?;
            let s = StackSecrets::generate()?;
            let plain = serde_json::to_vec(&s).expect("serialise secrets");
            let sealed = protector.protect(&plain).map_err(SecretsError::Undecryptable)?;
            write_private_new(&path, &sealed)?;
            Ok((s, true))
        }
        Err(e) => Err(e.into()),
    }
}

fn create_private_dir(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Create the file exclusively (never overwrite) with owner-only permissions where the OS supports it.
fn write_private_new(path: &Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    f.write_all(data)?;
    f.sync_all()
}

#[cfg(test)]
mod tests {
    use super::*;

    struct XorProtector(u8);
    impl Protector for XorProtector {
        fn protect(&self, p: &[u8]) -> Result<Vec<u8>, String> {
            Ok(p.iter().map(|b| b ^ self.0).collect())
        }
        fn unprotect(&self, s: &[u8]) -> Result<Vec<u8>, String> {
            let v: Vec<u8> = s.iter().map(|b| b ^ self.0).collect();
            if v.first() != Some(&b'{') {
                return Err("bad key".into());
            }
            Ok(v)
        }
    }

    #[test]
    fn generated_values_are_strong_and_url_safe() {
        let a = StackSecrets::generate().unwrap();
        let b = StackSecrets::generate().unwrap();
        assert_ne!(a, b);
        assert_eq!(a.db_password.len(), 32);
        assert!(a.db_password.bytes().all(|c| c.is_ascii_alphanumeric()));
        assert_eq!(a.jwt_secret.len(), 64);
        let key = base64::engine::general_purpose::URL_SAFE.decode(&a.encryption_key).unwrap();
        assert_eq!(key.len(), 32, "Fernet key must be 32 bytes");
        assert_eq!(a.encryption_key.len(), 44);
    }

    #[test]
    fn debug_never_prints_values() {
        let s = StackSecrets::generate().unwrap();
        let d = format!("{s:?}");
        assert!(s.values().iter().all(|v| !d.contains(v)));
    }

    #[test]
    fn redact_removes_every_value() {
        let s = StackSecrets::generate().unwrap();
        let text = format!("error: postgresql://rr:{}@postgres key={}", s.db_password, s.encryption_key);
        let r = s.redact(&text);
        assert_eq!(r, "error: postgresql://rr:***@postgres key=***");
    }

    #[test]
    fn create_then_reload_same_values_and_file_is_sealed() {
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("secrets");
        let p = XorProtector(0x5a);
        let (s1, created) = load_or_create(&dir, &p).unwrap();
        assert!(created);
        let raw = std::fs::read(secrets_file(&dir)).unwrap();
        let raw_text = String::from_utf8_lossy(&raw);
        assert!(s1.values().iter().all(|v| !raw_text.contains(v)), "secret visible at rest");
        let (s2, created2) = load_or_create(&dir, &p).unwrap();
        assert!(!created2);
        assert_eq!(s1, s2);
    }

    #[test]
    fn undecryptable_file_is_never_replaced() {
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("secrets");
        load_or_create(&dir, &XorProtector(0x5a)).unwrap();
        let before = std::fs::read(secrets_file(&dir)).unwrap();
        let err = load_or_create(&dir, &XorProtector(0x33)).unwrap_err();
        assert!(matches!(err, SecretsError::Undecryptable(_)), "{err}");
        assert_eq!(std::fs::read(secrets_file(&dir)).unwrap(), before);
    }

    #[test]
    fn corrupt_file_is_never_replaced() {
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("secrets");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(secrets_file(&dir), b"{\"version\":1}").unwrap();
        assert!(matches!(load_or_create(&dir, &PlainFileProtector), Err(SecretsError::Corrupt(_))));
        assert_eq!(std::fs::read(secrets_file(&dir)).unwrap(), b"{\"version\":1}");
    }

    #[cfg(unix)]
    #[test]
    fn unix_permissions_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("secrets");
        load_or_create(&dir, &PlainFileProtector).unwrap();
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(std::fs::metadata(secrets_file(&dir)).unwrap().permissions().mode() & 0o777, 0o600);
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_roundtrip() {
        let sealed = DpapiProtector.protect(b"{\"x\":1}").unwrap();
        assert!(!String::from_utf8_lossy(&sealed).contains("\"x\""));
        assert_eq!(DpapiProtector.unprotect(&sealed).unwrap(), b"{\"x\":1}");
        assert!(DpapiProtector.unprotect(b"garbage").is_err());
    }
}
