//! Small OS queries used by prerequisite checks.

use std::path::Path;

/// Bytes available to the current user on the volume holding `path` (the nearest existing ancestor is used).
pub fn available_space(path: &Path) -> std::io::Result<u64> {
    let mut p = path;
    while !p.exists() {
        p = p.parent().ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "no existing ancestor"))?;
    }
    imp(p)
}

#[cfg(unix)]
fn imp(p: &Path) -> std::io::Result<u64> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(p.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "path contains NUL"))?;
    // SAFETY: c is a valid NUL-terminated path and st is a properly sized out-parameter.
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    #[allow(clippy::unnecessary_cast)]
    Ok(st.f_bavail as u64 * st.f_frsize as u64)
}

#[cfg(windows)]
fn imp(p: &Path) -> std::io::Result<u64> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    let wide: Vec<u16> = p.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let mut avail: u64 = 0;
    // SAFETY: wide is NUL-terminated; the out-pointer is valid; unused outputs are null.
    let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut avail, std::ptr::null_mut(), std::ptr::null_mut()) };
    if ok == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(avail)
}

#[cfg(test)]
mod tests {
    #[test]
    fn reports_space_for_missing_path_via_ancestor() {
        let t = tempfile::tempdir().unwrap();
        let n = super::available_space(&t.path().join("a/b/c")).unwrap();
        assert!(n > 0);
    }
}
