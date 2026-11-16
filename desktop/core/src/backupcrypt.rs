//! Encrypted backups (RR Trading One Phase 21, specification Part 21).
//!
//! A backup is a PostgreSQL dump encrypted with AES-256-GCM in 1 MiB chunks (the STREAM construction: each chunk's
//! nonce carries its position and whether it is the last one, so chunks cannot be reordered, dropped or the file
//! truncated without decryption failing). The file header (magic, key id, nonce prefix) is authenticated with every
//! chunk.
//!
//! ```text
//! "RRBK1\0" | key id (8 bytes) | nonce prefix (7 bytes) | chunk 1 (<= 1 MiB + 16-byte tag) | ... | last chunk
//! ```
//!
//! The key is the installation's `backup_key` (stack secrets, DPAPI-protected). Because DPAPI ties it to this Windows
//! account, the key can be exported as text (`RRBK1-…`) and kept offline; another installation that is given it can
//! restore these backups (disaster recovery on a new PC). The key id lets a restore say which key a backup needs.

use std::io::{Read, Write};
use std::path::Path;

use aes_gcm::aead::stream::{DecryptorBE32, EncryptorBE32};
use aes_gcm::aead::{KeyInit, Payload};
use aes_gcm::Aes256Gcm;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use sha2::{Digest, Sha256};

pub const MAGIC: &[u8; 6] = b"RRBK1\0";
pub const EXPORT_PREFIX: &str = "RRBK1-";
const CHUNK: usize = 1 << 20;
const TAG: usize = 16;
const NONCE_PREFIX: usize = 7;
pub const HEADER_LEN: usize = MAGIC.len() + 8 + NONCE_PREFIX;

#[derive(Clone, PartialEq, Eq)]
pub struct BackupKey([u8; 32]);

impl std::fmt::Debug for BackupKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "BackupKey({})", self.id_hex())
    }
}

impl BackupKey {
    pub fn generate() -> Result<Self, String> {
        let mut k = [0u8; 32];
        getrandom::fill(&mut k).map_err(|e| e.to_string())?;
        Ok(BackupKey(k))
    }

    /// From the value stored in the stack secrets (url-safe base64 of 32 bytes).
    pub fn from_stored(text: &str) -> Result<Self, String> {
        let v = URL_SAFE_NO_PAD.decode(text.trim()).map_err(|_| "backup key is not valid base64")?;
        let k: [u8; 32] = v.try_into().map_err(|_| "backup key must be 32 bytes")?;
        Ok(BackupKey(k))
    }

    pub fn stored(&self) -> String {
        URL_SAFE_NO_PAD.encode(self.0)
    }

    /// Text for the user to keep offline: `RRBK1-<key>`.
    pub fn export(&self) -> String {
        format!("{EXPORT_PREFIX}{}", self.stored())
    }

    pub fn parse_export(text: &str) -> Result<Self, String> {
        let t = text.trim();
        let body = t.strip_prefix(EXPORT_PREFIX).ok_or("a backup key starts with RRBK1-")?;
        Self::from_stored(body).map_err(|_| "this is not a valid RR Trading One backup key".to_string())
    }

    pub fn id(&self) -> [u8; 8] {
        let d = Sha256::new().chain_update(b"rr-trading-one backup key id\0").chain_update(self.0).finalize();
        d[..8].try_into().expect("8 bytes")
    }

    pub fn id_hex(&self) -> String {
        hex(&self.id())
    }

    fn cipher(&self) -> Aes256Gcm {
        Aes256Gcm::new(&self.0.into())
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Read until `buf` is full or the reader is exhausted; returns the number of bytes read.
fn read_full(r: &mut impl Read, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match r.read(&mut buf[n..])? {
            0 => break,
            k => n += k,
        }
    }
    Ok(n)
}

/// Key id in a file's header, or None when the file is not an encrypted backup.
pub fn key_id_of(path: &Path) -> Option<[u8; 8]> {
    let mut h = [0u8; HEADER_LEN];
    let mut f = std::fs::File::open(path).ok()?;
    if read_full(&mut f, &mut h).ok()? != HEADER_LEN || &h[..MAGIC.len()] != MAGIC {
        return None;
    }
    h[MAGIC.len()..MAGIC.len() + 8].try_into().ok()
}

/// Encrypt `src` into `dst` (written as `dst` + ".part", then renamed). Returns the encrypted size.
pub fn encrypt_file(src: &Path, dst: &Path, key: &BackupKey) -> Result<u64, String> {
    let part = dst.with_extension("part");
    let res = (|| -> Result<u64, String> {
        let mut prefix = [0u8; NONCE_PREFIX];
        getrandom::fill(&mut prefix).map_err(|e| e.to_string())?;
        let mut header = Vec::with_capacity(HEADER_LEN);
        header.extend_from_slice(MAGIC);
        header.extend_from_slice(&key.id());
        header.extend_from_slice(&prefix);
        let mut input = std::fs::File::open(src).map_err(|e| format!("cannot read the dump: {e}"))?;
        let mut out = std::io::BufWriter::new(std::fs::File::create(&part).map_err(|e| format!("cannot write the backup: {e}"))?);
        out.write_all(&header).map_err(|e| e.to_string())?;
        let mut enc = Some(EncryptorBE32::from_aead(key.cipher(), prefix.as_slice().into()));
        let mut cur = vec![0u8; CHUNK];
        let mut n = read_full(&mut input, &mut cur).map_err(|e| e.to_string())?;
        let mut next = vec![0u8; CHUNK];
        loop {
            let m = read_full(&mut input, &mut next).map_err(|e| e.to_string())?;
            let payload = Payload { msg: &cur[..n], aad: &header };
            let ct = if m == 0 {
                enc.take().expect("encryptor").encrypt_last(payload)
            } else {
                enc.as_mut().expect("encryptor").encrypt_next(payload)
            }
            .map_err(|_| "encryption failed".to_string())?;
            out.write_all(&ct).map_err(|e| e.to_string())?;
            if m == 0 {
                break;
            }
            std::mem::swap(&mut cur, &mut next);
            n = m;
        }
        let f = out.into_inner().map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
        drop(f);
        std::fs::rename(&part, dst).map_err(|e| e.to_string())?;
        Ok(std::fs::metadata(dst).map(|m| m.len()).unwrap_or(0))
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&part);
    }
    res
}

#[derive(Debug, PartialEq, Eq)]
pub enum DecryptError {
    NotEncrypted,
    /// The backup was encrypted with a key this installation does not hold (id in hex).
    UnknownKey(String),
    /// Wrong key material, a modified or truncated file.
    Corrupt,
    Io(String),
}

impl std::fmt::Display for DecryptError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DecryptError::NotEncrypted => f.write_str("not an encrypted RR Trading One backup"),
            DecryptError::UnknownKey(id) => write!(f, "this backup was encrypted by another installation (backup key id {id}); \
                add that installation's backup key in Backup and Recovery, then restore again"),
            DecryptError::Corrupt => f.write_str("the backup failed its integrity check (modified, truncated or damaged); \
                nothing was restored"),
            DecryptError::Io(e) => write!(f, "cannot read or write the backup: {e}"),
        }
    }
}

/// Decrypt `src` into `dst` with whichever of `keys` the header names. On any failure `dst` is removed.
pub fn decrypt_file(src: &Path, dst: &Path, keys: &[BackupKey]) -> Result<u64, DecryptError> {
    let io = |e: std::io::Error| DecryptError::Io(e.to_string());
    let mut input = std::fs::File::open(src).map_err(io)?;
    let mut header = [0u8; HEADER_LEN];
    if read_full(&mut input, &mut header).map_err(io)? != HEADER_LEN || &header[..MAGIC.len()] != MAGIC {
        return Err(DecryptError::NotEncrypted);
    }
    let id: [u8; 8] = header[MAGIC.len()..MAGIC.len() + 8].try_into().expect("8 bytes");
    let key = keys.iter().find(|k| k.id() == id).ok_or_else(|| DecryptError::UnknownKey(hex(&id)))?;
    let prefix = &header[MAGIC.len() + 8..];
    let res = (|| -> Result<u64, DecryptError> {
        let mut out = std::io::BufWriter::new(std::fs::File::create(dst).map_err(io)?);
        let mut dec = Some(DecryptorBE32::from_aead(key.cipher(), prefix.into()));
        let mut cur = vec![0u8; CHUNK + TAG];
        let mut n = read_full(&mut input, &mut cur).map_err(io)?;
        if n < TAG {
            return Err(DecryptError::Corrupt);
        }
        let mut next = vec![0u8; CHUNK + TAG];
        let mut total = 0u64;
        loop {
            let m = read_full(&mut input, &mut next).map_err(io)?;
            let payload = Payload { msg: &cur[..n], aad: &header };
            let pt = if m == 0 {
                dec.take().expect("decryptor").decrypt_last(payload)
            } else {
                dec.as_mut().expect("decryptor").decrypt_next(payload)
            }
            .map_err(|_| DecryptError::Corrupt)?;
            out.write_all(&pt).map_err(io)?;
            total += pt.len() as u64;
            if m == 0 {
                break;
            }
            std::mem::swap(&mut cur, &mut next);
            n = m;
        }
        out.flush().map_err(io)?;
        Ok(total)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(dst);
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(len: usize) {
        let t = tempfile::tempdir().unwrap();
        let src = t.path().join("a.dump");
        let data: Vec<u8> = (0..len).map(|i| (i * 31 % 251) as u8).collect();
        std::fs::write(&src, &data).unwrap();
        let k = BackupKey::generate().unwrap();
        let enc = t.path().join("a.rrbk");
        encrypt_file(&src, &enc, &k).unwrap();
        let raw = std::fs::read(&enc).unwrap();
        assert!(raw.starts_with(MAGIC) && raw.len() == HEADER_LEN + len + TAG * len.div_ceil(CHUNK).max(1));
        assert!(len < 64 || !raw.windows(64).any(|w| w == &data[..64]), "no plaintext in the file");
        let out = t.path().join("b.dump");
        assert_eq!(decrypt_file(&enc, &out, &[k]).unwrap(), len as u64);
        assert_eq!(std::fs::read(&out).unwrap(), data);
    }

    #[test]
    fn round_trips_at_chunk_boundaries() {
        for len in [0, 1, 1000, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK, 2 * CHUNK + 7] {
            roundtrip(len);
        }
    }

    #[test]
    fn tampering_truncation_and_wrong_keys_are_refused() {
        let t = tempfile::tempdir().unwrap();
        let src = t.path().join("a.dump");
        std::fs::write(&src, vec![7u8; 2 * CHUNK + 100]).unwrap();
        let k = BackupKey::generate().unwrap();
        let enc = t.path().join("a.rrbk");
        encrypt_file(&src, &enc, &k).unwrap();
        let good = std::fs::read(&enc).unwrap();
        let out = t.path().join("out.dump");
        let try_with = |bytes: &[u8], keys: &[BackupKey]| {
            let p = t.path().join("x.rrbk");
            std::fs::write(&p, bytes).unwrap();
            let _ = std::fs::remove_file(&out);
            let r = decrypt_file(&p, &out, keys);
            assert!(r.is_ok() || !out.exists(), "a failed decryption leaves no partial output");
            r
        };
        let mut flipped = good.clone();
        flipped[HEADER_LEN + 10] ^= 1;
        assert_eq!(try_with(&flipped, std::slice::from_ref(&k)), Err(DecryptError::Corrupt));
        // drop the last chunk: the new last chunk was not encrypted as "last"
        assert_eq!(try_with(&good[..HEADER_LEN + 2 * (CHUNK + TAG)], std::slice::from_ref(&k)), Err(DecryptError::Corrupt));
        let mut header_edit = good.clone();
        header_edit[HEADER_LEN - 1] ^= 1;                    // nonce prefix: authenticated
        assert_eq!(try_with(&header_edit, std::slice::from_ref(&k)), Err(DecryptError::Corrupt));
        let other = BackupKey::generate().unwrap();
        assert_eq!(try_with(&good, std::slice::from_ref(&other)), Err(DecryptError::UnknownKey(k.id_hex())));
        assert!(try_with(&good, &[other, k.clone()]).is_ok(), "any held key with the right id works");
        assert_eq!(try_with(b"PGDMP plain", &[k]), Err(DecryptError::NotEncrypted));
    }

    #[test]
    fn export_round_trips_and_is_validated() {
        let k = BackupKey::generate().unwrap();
        let text = k.export();
        assert!(text.starts_with("RRBK1-") && text.len() == 6 + 43);
        assert_eq!(BackupKey::parse_export(&format!("  {text}\n")).unwrap(), k);
        assert!(BackupKey::parse_export("RRBK1-short").is_err() && BackupKey::parse_export(&k.stored()).is_err());
        assert_eq!(BackupKey::from_stored(&k.stored()).unwrap(), k);
        assert!(!format!("{k:?}").contains(&k.stored()), "Debug shows only the id");
    }
}
