//! The `brutex/config/paths.py` pure path-derivation laws (milestone 3).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). Two laws, byte-locked
//! to the CPython `pathlib.PurePosixPath` oracle:
//!
//! * [`lake_root`] — the `resolve_lake_root` DECISION law: a truthy
//!   (non-empty) `BRUTEX_LAKE_ROOT` override wins verbatim, else
//!   `<home>/.brutex/lake`. The Python facade gates `~`-BEARING
//!   overrides to the oracle (`Path.expanduser()` needs the OS user
//!   database — never ported; r2 M1: `expanduser` acts on the
//!   NORMALIZED first component, so `./~user` expands too — hence
//!   tilde-ANYWHERE, not tilde-prefix), so on every crossing input
//!   `Path(x).expanduser()` is the identity and the whole law reduces to
//!   `str(PurePosixPath(...))` normalization (probed 2026-07-17, pinned
//!   below).
//! * [`volume_parent`] — the `lake_root_identity` G1 volume-parent
//!   derivation: `parts[0] == "/" and parts[1] == "Volumes" and
//!   len(parts) >= 3` → `"/Volumes/<name>"`, else the empty string (the
//!   facade's no-parent sentinel — an `Option` return would collide with
//!   the shared adopt-core's `None` = serve-the-oracle contract).
//!
//! ## The PurePosixPath normalization law (probed CPython 3.12)
//!
//! `str(PurePosixPath(s))`: split on `/`; drop empty and `"."`
//! components (`".."` and `"..."` KEPT); root from the LEADING slash
//! count — 0 → relative, 1 → `/`, exactly 2 → `//` (the POSIX
//! implementation-defined special), 3+ → `/`; no root + no components →
//! `"."`. Pinned: `""`→`"."` · `"///a"`→`"/a"` · `"//a//b/"`→`"//a/b"` ·
//! `"a/./b/"`→`"a/b"` · `"./x"`→`"x"` · `"../x"`→`"../x"`.
//!
//! The join arm mirrors `Path(home) / ".brutex" / "lake"` by
//! concatenating with a single `/` (none when *home* already ends with
//! one — the root/`//` cases) then normalizing; *home* crosses as
//! `str(Path)` (already normalized), so the re-normalization only ever
//! collapses the `"."`-home case (`"." + "/.brutex/lake"` →
//! `".brutex/lake"`, the probed `Path(".").parts == ()` law).
//!
//! ONLY strings cross (both directions — the milestone-1 law); the
//! facade gates non-ASCII and `~`-prefixed inputs to the oracle, rebuilds
//! `Path(...)` from the returned string, and every `Err` maps to
//! `ValueError` at the FFI shell (a facade DEGRADE member).

/// Defensive input-length cap (PATH_MAX-scale; the facade gates far below).
pub const MAX_PATH_LEN: usize = 4096;

/// Reject a crossing that violates the facade's declared envelope.
fn check_input(name: &str, s: &str) -> Result<(), String> {
    if s.len() > MAX_PATH_LEN {
        return Err(format!(
            "{name}: input exceeds {MAX_PATH_LEN} bytes ({} bytes)",
            s.len()
        ));
    }
    if !s.is_ascii() {
        return Err(format!(
            "{name}: non-ASCII input (the facade eligibility gate routes these to the oracle)"
        ));
    }
    Ok(())
}

/// `str(PurePosixPath(s))` — the probed CPython normalization law.
fn normalize_posix(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut lead = 0usize;
    while lead < bytes.len() && bytes[lead] == b'/' {
        lead += 1;
    }
    let root = match lead {
        0 => "",
        1 => "/",
        2 => "//",
        _ => "/",
    };
    let comps: Vec<&str> = s[lead..]
        .split('/')
        .filter(|c| !c.is_empty() && *c != ".")
        .collect();
    if comps.is_empty() {
        if root.is_empty() {
            ".".to_string()
        } else {
            root.to_string()
        }
    } else {
        format!("{root}{}", comps.join("/"))
    }
}

/// The `resolve_lake_root` decision law (module docs). `override_value`
/// is `None`/empty for the default arm (Python truthiness — no strip).
pub fn lake_root(override_value: Option<&str>, home: &str) -> Result<String, String> {
    if let Some(value) = override_value {
        check_input("lake_root override", value)?;
        if !value.is_empty() {
            if value.contains('~') {
                // The facade gates ALL '~'-bearing overrides to the
                // oracle (r2 M1: CPython `expanduser` acts on the
                // NORMALIZED first component, so "./~user" expands too —
                // a prefix-only check under-refused); a crossing one is
                // a contract breach. Defense-in-depth: refuse ANY '~'.
                return Err(
                    "lake_root: '~'-bearing override must take the Python oracle".to_string(),
                );
            }
            return Ok(normalize_posix(value));
        }
    }
    check_input("lake_root home", home)?;
    let joined = if home.ends_with('/') {
        format!("{home}.brutex/lake")
    } else {
        format!("{home}/.brutex/lake")
    };
    Ok(normalize_posix(&joined))
}

/// The G1 volume-parent law (module docs). `""` = no volume parent.
pub fn volume_parent(path: &str) -> Result<String, String> {
    check_input("volume_parent path", path)?;
    let normalized = normalize_posix(path);
    let bytes = normalized.as_bytes();
    let mut lead = 0usize;
    while lead < bytes.len() && bytes[lead] == b'/' {
        lead += 1;
    }
    // parts[0] == "/" requires EXACTLY one leading slash ("//x" has
    // parts[0] == "//" — probed; the law then reads False).
    if lead != 1 {
        return Ok(String::new());
    }
    let comps: Vec<&str> = normalized[lead..].split('/').collect();
    if comps.len() >= 2 && comps[0] == "Volumes" && !comps[1].is_empty() {
        return Ok(format!("/Volumes/{}", comps[1]));
    }
    Ok(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalization_matches_probed_purepath_corpus() {
        // The 2026-07-17 CPython 3.12 probe transcript, verbatim.
        let corpus = [
            ("", "."),
            (".", "."),
            ("/", "/"),
            ("//", "//"),
            ("///", "/"),
            ("//a//b/", "//a/b"),
            ("a/./b/", "a/b"),
            ("./x", "x"),
            ("a//b", "a/b"),
            ("..", ".."),
            ("../x", "../x"),
            ("a/...", "a/..."),
            (" /x", " /x"),
            ("/Volumes", "/Volumes"),
            ("/Volumes/", "/Volumes"),
            ("/Volumes/T7/lake", "/Volumes/T7/lake"),
            ("//Volumes/T7", "//Volumes/T7"),
        ];
        for (raw, expected) in corpus {
            assert_eq!(normalize_posix(raw), expected, "input {raw:?}");
        }
    }

    #[test]
    fn lake_root_override_wins_and_normalizes() {
        assert_eq!(
            lake_root(Some("/Volumes/T7//lake/"), "/home/u").unwrap(),
            "/Volumes/T7/lake"
        );
        assert_eq!(
            lake_root(Some("rel/./lake"), "/home/u").unwrap(),
            "rel/lake"
        );
    }

    #[test]
    fn lake_root_default_arm_joins_home() {
        // The probed Path(home)/".brutex"/"lake" corpus.
        let corpus = [
            (".", ".brutex/lake"),
            ("/", "/.brutex/lake"),
            ("//", "//.brutex/lake"),
            ("/home/u", "/home/u/.brutex/lake"),
            ("foo", "foo/.brutex/lake"),
            ("..", "../.brutex/lake"),
        ];
        for (home, expected) in corpus {
            assert_eq!(lake_root(None, home).unwrap(), expected, "home {home:?}");
            assert_eq!(
                lake_root(Some(""), home).unwrap(),
                expected,
                "home {home:?} (empty override)"
            );
        }
    }

    #[test]
    fn lake_root_refuses_tilde_and_oversize_and_non_ascii() {
        assert!(lake_root(Some("~/lake"), "/h").is_err());
        assert!(lake_root(Some("~user/lake"), "/h").is_err());
        // r2 M1: tilde ANYWHERE is refused (the normalized-first-
        // component class "./~user" + the defense-in-depth interior).
        assert!(lake_root(Some("~"), "/h").is_err());
        assert!(lake_root(Some("./~user/lake"), "/h").is_err());
        assert!(lake_root(Some("a/~b"), "/h").is_err());
        assert!(lake_root(Some("/a/b~c"), "/h").is_err());
        assert!(lake_root(Some("/ok"), "/h\u{e9}").is_ok()); // override arm never reads home
        assert!(lake_root(None, "/h\u{e9}").is_err());
        assert!(lake_root(Some("caf\u{e9}"), "/h").is_err());
        let long = "a".repeat(MAX_PATH_LEN + 1);
        assert!(lake_root(Some(&long), "/h").is_err());
        assert!(lake_root(None, &long).is_err());
    }

    #[test]
    fn volume_parent_law_matrix() {
        // The probed parts-law corpus: parts[0] must be exactly "/".
        let corpus = [
            ("/Volumes/T7/lake", "/Volumes/T7"),
            ("/Volumes/T7", "/Volumes/T7"),
            ("/Volumes", ""),     // len(parts) == 2
            ("/Volumes/", ""),    // normalizes to "/Volumes"
            ("//Volumes/T7", ""), // parts[0] == "//" — probed False
            ("/home/u/lake", ""),
            ("Volumes/T7", ""),  // relative
            ("/volumes/T7", ""), // case-sensitive
            ("/Volumes/name/deep/er", "/Volumes/name"),
        ];
        for (path, expected) in corpus {
            assert_eq!(volume_parent(path).unwrap(), expected, "path {path:?}");
        }
    }

    #[test]
    fn volume_parent_refuses_non_ascii_and_oversize() {
        assert!(volume_parent("/Volumes/caf\u{e9}").is_err());
        assert!(volume_parent(&"/".repeat(MAX_PATH_LEN + 1)).is_err());
    }
}
