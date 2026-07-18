//! Lock/atomic-replace primitives for the BruteX data lake (ADR-0020 PR-4).
//!
//! DEC-LAKE-RUST-LOCKS-ATOMIC-001 (MIGRATION.md #1912). Three pure-Rust
//! syscall primitives behind the default-OFF `BRUTEX_LAKE_RUST_LOCKS`
//! facade (`brutex/data/lake_rust_locks.py`):
//!
//! * [`try_lock_exclusive_impl`] — ONE non-blocking `flock(LOCK_EX | LOCK_NB)`
//!   attempt on a CALLER-OWNED raw fd. The bounded retry loop, the poll
//!   sleep, the progress-beat emission and the `PartitionLockTimeout`
//!   deadline law all STAY PYTHON (`partition_lock._acquire_bounded`) — a
//!   loop-in-Rust shape would silently drop the watchdog liveness beats a
//!   contending pull must keep firing (DEC-PULL-CONTENTION-ANTISTALL-001
//!   GAP #15), so the FFI grain is the single attempt.
//! * [`unlock_close_impl`] — best-effort `flock(LOCK_UN)` then `close(fd)`
//!   (the Python arm's `os.close` twin — closing the fd releases the
//!   advisory lock either way; the explicit unlock is belt).
//! * [`commit_replace_impl`] — the chmod→`fsync`→`rename` COMMIT half of
//!   the atomic-write dance (`atomic_io._atomic_write_core`). Temp-file
//!   CREATION stays Python (`tempfile.mkstemp` — the `.brutex-write-`
//!   prefix law + the mkstemp-ENOSPC guard law stay byte-identical), the
//!   payload write stays Python (an arbitrary `write_to` callback), and
//!   the error-path temp CLEANUP stays Python (the core's `finally`).
//!
//! Error law: every failure surfaces as `std::io::Error` WITH ITS ERRNO
//! INTACT; the `#[pyfunction]` wrappers in `lib.rs` map it to a real
//! Python `OSError(errno, …)` so the EXISTING `lake_write_guard` /
//! except-arm taxonomy (`ENOSPC`/`EDQUOT` → `LakeWriteHalt("disk_full")`,
//! everything else raw) stays the SINGLE error authority — this crate
//! never mints a new exception class.
//!
//! Locking model (the #1785 G10 lesson, unit-pinned below): an `flock` is
//! held per OPEN FILE DESCRIPTION, so two distinct `open()`s of one lock
//! file contend even inside one process — which is exactly why the Python
//! facade's in-process `BoundedLockRegistry` stays the OUTER layer
//! (registry `threading.Lock` first, THEN the flock; ADR-0020 PR-4:
//! "in-process lock acquisition REMAINS on the Python facade").
//!
//! Honest envelope (Golden Rule #10): these are the SAME syscalls the
//! Python arm issues — a conversion for ADR-0020 uniformity, not speed
//! (expected payoff ≈ 0); a wedged `fsync` on a dead volume is equally
//! unkillable from either arm (the #999 zombie envelope — the
//! write-deadline daemon above these primitives is the containment).

use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

/// One non-blocking exclusive `flock` attempt on *fd*.
///
/// Returns `Ok(true)` when the lock was taken, `Ok(false)` when a peer
/// holds it (`EWOULDBLOCK` / `EAGAIN` — the Python arm's
/// `BlockingIOError` shape), and `Err` with the errno intact for every
/// other failure (`EBADF`, …) exactly as `fcntl.flock` raises them.
/// `EINTR` is retried internally (CPython's `fcntl.flock` retries
/// interrupted syscalls via `PyErr_CheckSignals` too — parity).
#[allow(unsafe_code)] // SAFETY: `libc::flock` on a caller-owned raw fd — no
// pointers, no buffers, no ownership crosses; the fd's validity is the
// caller's precondition exactly as it is for `fcntl.flock(fd, …)`.
pub fn try_lock_exclusive_impl(fd: i32) -> io::Result<bool> {
    loop {
        let rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if rc == 0 {
            return Ok(true);
        }
        let err = io::Error::last_os_error();
        match err.raw_os_error() {
            // EWOULDBLOCK == EAGAIN on Linux/macOS; match both defensively.
            Some(code) if code == libc::EWOULDBLOCK || code == libc::EAGAIN => {
                return Ok(false);
            }
            Some(code) if code == libc::EINTR => continue,
            _ => return Err(err),
        }
    }
}

/// Release the advisory lock held on *fd* and close it.
///
/// The `LOCK_UN` result is DELIBERATELY ignored (belt only — the `close`
/// releases the flock regardless, kernel-guaranteed); a `close` failure
/// surfaces with its errno intact (the Python arm's `os.close` raises
/// too — parity). `close` is never retried on `EINTR` (POSIX leaves the
/// fd state unspecified; CPython's `os.close` does not retry either).
#[allow(unsafe_code)] // SAFETY: `libc::flock`/`libc::close` on a caller-owned
// raw fd — no memory crosses; the caller hands ownership of the fd to this
// call (on return, success or error, the fd must not be reused).
pub fn unlock_close_impl(fd: i32) -> io::Result<()> {
    unsafe {
        // Best-effort explicit unlock; result ignored by design (doc above).
        libc::flock(fd, libc::LOCK_UN);
    }
    let rc = unsafe { libc::close(fd) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// The chmod→`fsync`→`rename` commit of an already-written temp file.
///
/// Mirrors `atomic_io._atomic_write_core`'s commit tail ORDER exactly:
/// optional `chmod(tmp, mode)` FIRST (so *dest* lands at that mode and is
/// never briefly world-readable), then the temp's DATA is `fsync`-ed to
/// stable storage (`File::sync_all` — `os.replace` orders only the
/// metadata, not the page cache), then the atomic `fs::rename` (POSIX
/// `rename(2)` — `os.replace` semantics: atomically replaces an existing
/// *dest*). On ANY error the temp is left IN PLACE for the Python core's
/// `finally` cleanup — this primitive never unlinks.
pub fn commit_replace_impl(tmp: &Path, dest: &Path, chmod: Option<u32>) -> io::Result<()> {
    if let Some(mode) = chmod {
        fs::set_permissions(tmp, fs::Permissions::from_mode(mode))?;
    }
    let flush = fs::File::open(tmp)?; // O_RDONLY — the Python arm's flush fd
    flush.sync_all()?; // fsync(2): data + metadata to stable storage
    drop(flush);
    fs::rename(tmp, dest)?; // atomic replace (same filesystem — same dir)
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::fd::{AsRawFd, IntoRawFd};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static UNIQUE: AtomicU64 = AtomicU64::new(0);

    /// A per-test unique scratch path (no tempfile dev-dep — std only).
    fn scratch(tag: &str) -> PathBuf {
        let n = UNIQUE.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "brutex-lake-locks-test-{}-{tag}-{n}",
            std::process::id()
        ))
    }

    /// Two OPEN FILE DESCRIPTIONS of one lock file contend even in one
    /// process (flock is per-OFD — the #1785 G10 lesson): holder takes it,
    /// waiter reads `false`, release → waiter acquires.
    #[test]
    fn two_open_file_descriptions_mutually_exclude() {
        let path = scratch("mutex");
        let holder = fs::File::create(&path).expect("create lock file");
        let waiter = fs::File::open(&path).expect("second OFD");

        assert!(try_lock_exclusive_impl(holder.as_raw_fd()).expect("holder"));
        // The waiter's DISTINCT open file description must be refused.
        assert!(!try_lock_exclusive_impl(waiter.as_raw_fd()).expect("waiter"));

        // Release via the primitive (hand the fd's ownership over).
        unlock_close_impl(holder.into_raw_fd()).expect("release");
        assert!(try_lock_exclusive_impl(waiter.as_raw_fd()).expect("waiter after release"));
        drop(waiter);
        let _ = fs::remove_file(&path);
    }

    /// Re-locking on the SAME open file description is idempotent (flock
    /// converts in place) — parity with `fcntl.flock` on one fd.
    #[test]
    fn same_ofd_relock_is_idempotent() {
        let path = scratch("relock");
        let f = fs::File::create(&path).expect("create");
        assert!(try_lock_exclusive_impl(f.as_raw_fd()).expect("first"));
        assert!(try_lock_exclusive_impl(f.as_raw_fd()).expect("second"));
        drop(f);
        let _ = fs::remove_file(&path);
    }

    /// A dead fd surfaces the REAL errno (`EBADF`) — never a would-block
    /// `false`, never a panic (the errno-intact law).
    #[test]
    fn invalid_fd_maps_ebadf() {
        let err = try_lock_exclusive_impl(-1).expect_err("must fail");
        assert_eq!(err.raw_os_error(), Some(libc::EBADF));
        let err = unlock_close_impl(-1).expect_err("close must fail");
        assert_eq!(err.raw_os_error(), Some(libc::EBADF));
    }

    /// Commit: payload lands at dest byte-identically, the temp is
    /// CONSUMED by the rename, and an existing dest is atomically replaced.
    #[test]
    fn commit_replace_moves_payload_and_replaces_dest() {
        let dir = scratch("commit-dir");
        fs::create_dir_all(&dir).expect("mkdir");
        let tmp = dir.join(".brutex-write-test.parquet.tmp");
        let dest = dir.join("2024-01.parquet");
        fs::write(&dest, b"stale bytes").expect("seed dest");
        {
            let mut f = fs::File::create(&tmp).expect("tmp");
            f.write_all(b"fresh payload").expect("write");
        }
        commit_replace_impl(&tmp, &dest, None).expect("commit");
        assert_eq!(fs::read(&dest).expect("read dest"), b"fresh payload");
        assert!(!tmp.exists(), "rename must consume the temp");
        let _ = fs::remove_dir_all(&dir);
    }

    /// The chmod arm applies the mode BEFORE the rename so dest lands at it.
    #[test]
    fn commit_replace_chmod_lands_on_dest() {
        let dir = scratch("chmod-dir");
        fs::create_dir_all(&dir).expect("mkdir");
        let tmp = dir.join(".brutex-write-chmod.parquet.tmp");
        let dest = dir.join("credentials.json");
        fs::write(&tmp, b"secret").expect("tmp");
        commit_replace_impl(&tmp, &dest, Some(0o600)).expect("commit");
        let mode = fs::metadata(&dest).expect("stat").permissions().mode() & 0o7777;
        assert_eq!(mode, 0o600);
        let _ = fs::remove_dir_all(&dir);
    }

    /// A missing temp surfaces `ENOENT` with the errno intact; dest is
    /// untouched (the correct-or-absent law).
    #[test]
    fn commit_replace_missing_tmp_is_enoent_dest_untouched() {
        let dir = scratch("enoent-dir");
        fs::create_dir_all(&dir).expect("mkdir");
        let dest = dir.join("dest.parquet");
        fs::write(&dest, b"original").expect("seed");
        let err =
            commit_replace_impl(&dir.join("no-such.tmp"), &dest, None).expect_err("must fail");
        assert_eq!(err.raw_os_error(), Some(libc::ENOENT));
        assert_eq!(fs::read(&dest).expect("read"), b"original");
        let _ = fs::remove_dir_all(&dir);
    }

    /// A chmod failure (missing temp) leaves everything untouched and
    /// carries its errno — the FIRST failing step reports, never a mask.
    #[test]
    fn commit_replace_chmod_failure_surfaces_first() {
        let dir = scratch("chmod-fail");
        fs::create_dir_all(&dir).expect("mkdir");
        let err = commit_replace_impl(&dir.join("gone.tmp"), &dir.join("d.parquet"), Some(0o600))
            .expect_err("must fail");
        assert_eq!(err.raw_os_error(), Some(libc::ENOENT));
        let _ = fs::remove_dir_all(&dir);
    }
}
