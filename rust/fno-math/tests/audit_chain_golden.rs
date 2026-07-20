//! Native replay of the frozen audit-chain parity goldens
//! (DEC-AUDIT-CHAIN-RUST-PORT-001, ADR-0019 / MIGRATION.md #1727).
//!
//! Replays BOTH frozen fixtures — the 10-entry real `AuditWriter` chain
//! (`audit_chain_parity_golden.jsonl`) and the scalar unit-vector table
//! (`audit_chain_parity_scalars.json`) — through the PURE Rust serializer
//! (`fno_math::audit_chain`), asserting byte-for-byte parity: recomputed
//! `entry_hash`, re-rendered line bytes, `str(float)` repr vectors and
//! `_canonical_json` vectors. The trading-core `golden_vectors.rs` pattern:
//! the fixture is the language-neutral answer key; NO Python in the loop.
//!
//! Fixture floats are parsed with `serde_json`, whose f64 parsing is
//! correctly rounded (and preserves `-0.0`), so the parsed doubles are
//! bit-identical to the Python writer's.

use fno_math::audit_chain::{Value, canonical_json, entry_hash, entry_line, py_float_repr};

fn repo_root() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/fno-math → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture_path(name: &str) -> std::path::PathBuf {
    repo_root().join("tests/golden/fixtures").join(name)
}

/// serde_json → the bounded audit value model. The fixtures carry only
/// in-model values by construction (the chain was written by the real
/// Python `AuditWriter`), so every arm is total here.
fn to_value(v: &serde_json::Value) -> Value {
    match v {
        serde_json::Value::Null => Value::Null,
        serde_json::Value::Bool(b) => Value::Bool(*b),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Value::Int(i)
            } else {
                Value::Float(n.as_f64().expect("fixture number is i64 or f64"))
            }
        }
        serde_json::Value::String(s) => Value::Str(s.clone()),
        serde_json::Value::Array(items) => Value::List(items.iter().map(to_value).collect()),
        serde_json::Value::Object(map) => {
            Value::Map(map.iter().map(|(k, v)| (k.clone(), to_value(v))).collect())
        }
    }
}

/// One parsed fixture entry (chain line or scalar vector — same fields).
struct Entry {
    seq: i64,
    prev_hash: String,
    ts_utc: f64,
    actor: String,
    action: String,
    subject: String,
    payload: Value,
    entry_hash: String,
}

fn parse_entry(record: &serde_json::Value, hash_key: &str) -> Entry {
    Entry {
        seq: record["seq"].as_i64().expect("seq"),
        prev_hash: record["prev_hash"].as_str().expect("prev_hash").to_string(),
        ts_utc: record["ts_utc"].as_f64().expect("ts_utc"),
        actor: record["actor"].as_str().expect("actor").to_string(),
        action: record["action"].as_str().expect("action").to_string(),
        subject: record["subject"].as_str().expect("subject").to_string(),
        payload: to_value(&record["payload"]),
        entry_hash: record[hash_key].as_str().expect("entry hash").to_string(),
    }
}

fn recompute_hash(entry: &Entry) -> String {
    entry_hash(
        entry.seq,
        &entry.prev_hash,
        entry.ts_utc,
        &entry.actor,
        &entry.action,
        &entry.subject,
        &entry.payload,
    )
    .expect("fixture entry is in-model")
}

fn render_line(entry: &Entry) -> String {
    entry_line(
        entry.seq,
        &entry.prev_hash,
        entry.ts_utc,
        &entry.actor,
        &entry.action,
        &entry.subject,
        &entry.payload,
        &entry.entry_hash,
    )
    .expect("fixture entry is in-model")
}

#[test]
fn chain_fixture_replays_bit_for_bit() {
    let raw = std::fs::read_to_string(fixture_path("audit_chain_parity_golden.jsonl"))
        .expect("chain fixture readable");
    let lines: Vec<&str> = raw.lines().collect();
    assert_eq!(lines.len(), 10, "the frozen chain carries 10 entries");
    let mut prev = "0".repeat(64);
    for (expected_seq, line) in lines.iter().enumerate() {
        let record: serde_json::Value = serde_json::from_str(line).expect("valid JSONL line");
        let entry = parse_entry(&record, "entry_hash");
        assert_eq!(entry.seq, expected_seq as i64, "seq order");
        assert_eq!(entry.prev_hash, prev, "chain linkage at seq {expected_seq}");
        // The recomputed hash matches the stored hash…
        assert_eq!(
            recompute_hash(&entry),
            entry.entry_hash,
            "hash parity at seq {expected_seq}"
        );
        // …and the re-rendered line matches the on-disk bytes exactly.
        assert_eq!(
            render_line(&entry),
            *line,
            "line-byte parity at seq {expected_seq}"
        );
        prev = entry.entry_hash;
    }
}

#[test]
fn scalar_entry_vectors_replay() {
    let raw = std::fs::read_to_string(fixture_path("audit_chain_parity_scalars.json"))
        .expect("scalars fixture readable");
    let doc: serde_json::Value = serde_json::from_str(&raw).expect("valid JSON");
    let mut vectors: Vec<&serde_json::Value> =
        doc["entries"].as_array().expect("entries").iter().collect();
    vectors.extend(
        doc["standalone_entries"]
            .as_array()
            .expect("standalone")
            .iter(),
    );
    assert_eq!(
        vectors.len(),
        11,
        "10 chain vectors + 1 standalone actor vector"
    );
    for vector in vectors {
        let entry = parse_entry(vector, "expected_entry_hash");
        assert_eq!(
            recompute_hash(&entry),
            entry.entry_hash,
            "hash vector at seq {} action {}",
            entry.seq,
            entry.action
        );
        assert_eq!(
            render_line(&entry),
            vector["expected_line"].as_str().expect("expected_line"),
            "line vector at seq {} action {}",
            entry.seq,
            entry.action
        );
    }
}

#[test]
fn float_repr_vectors_replay() {
    let raw = std::fs::read_to_string(fixture_path("audit_chain_parity_scalars.json"))
        .expect("scalars fixture readable");
    let doc: serde_json::Value = serde_json::from_str(&raw).expect("valid JSON");
    let vectors = doc["float_repr_vectors"]
        .as_array()
        .expect("float_repr_vectors");
    assert!(vectors.len() >= 14);
    let mut saw_neg_zero = false;
    for vector in vectors {
        let value = vector["value"].as_f64().expect("value");
        let expected = vector["expected_str"].as_str().expect("expected_str");
        assert_eq!(
            py_float_repr(value).expect("finite"),
            expected,
            "repr({value:?})"
        );
        if expected == "-0.0" {
            saw_neg_zero = true;
            assert!(value.is_sign_negative(), "-0.0 must parse sign-negative");
        }
    }
    assert!(
        saw_neg_zero,
        "the signed-zero vector must be present and distinguishable"
    );
}

#[test]
fn canonical_json_vectors_replay() {
    let raw = std::fs::read_to_string(fixture_path("audit_chain_parity_scalars.json"))
        .expect("scalars fixture readable");
    let doc: serde_json::Value = serde_json::from_str(&raw).expect("valid JSON");
    let vectors = doc["canonical_json_vectors"]
        .as_array()
        .expect("canonical_json_vectors");
    assert!(vectors.len() >= 6);
    for vector in vectors {
        let payload = to_value(&vector["payload"]);
        let expected = vector["expected_json"].as_str().expect("expected_json");
        assert_eq!(canonical_json(&payload).expect("in-model"), expected);
    }
}
