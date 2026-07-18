//! Byte-law tests for the canonical-JSON render core
//! (DEC-STRATEGIES-RUST-COMBO-001 design §2.4).
//!
//! Every `expected` literal below is CPython-derived: computed with
//! `json.dumps(..., sort_keys=True, separators=(",", ":"),
//! ensure_ascii=True)` on CPython 3.12 and hardcoded — the cross-language
//! byte oracle (ADR-0020 F5 discipline: frozen goldens are legal here, the
//! bytes are stdlib-rendered, no third-party encoder).

use brutex_strategies::canon::{Node, render};

fn s(v: &str) -> Node {
    Node::Str(v.to_string())
}

fn dict(pairs: &[(&str, Node)]) -> Node {
    Node::Dict(
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), v.clone()))
            .collect(),
    )
}

/// The SHARED-LITERAL cross-crate parity corpus (design §2.3): the
/// `rust/brutex-lake/src/stats_shard.rs` `escaper_matrix_matches_cpython_ensure_ascii`
/// table duplicated VERBATIM, driven through this crate's escaper via
/// `render(Node::Str(..))` — so the two independent Rust copies of the
/// CPython `py_encode_basestring_ascii` law can never drift silently
/// (the `spec_canonical_golden.rs` shared-literal precedent; the Python
/// side pins the same literals a third time against `json.dumps`).
/// Consolidation onto a shared include is the named follow-up
/// DEC-RUST-JSON-ESCAPE-COMMON-001.
#[test]
fn escaper_matrix_matches_cpython_ensure_ascii_shared_literals() {
    let cases: &[(&str, &str)] = &[
        ("plain", "\"plain\""),
        ("a\"b\\c", "\"a\\\"b\\\\c\""),
        ("\u{08}\t\n\u{0c}\r", "\"\\b\\t\\n\\f\\r\""),
        ("\u{0b}\u{1b}\u{00}", "\"\\u000b\\u001b\\u0000\""),
        ("\u{7f}", "\"\\u007f\""),
        ("é", "\"\\u00e9\""),
        ("₹", "\"\\u20b9\""),
        ("😀", "\"\\ud83d\\ude00\""),
        ("a/b", "\"a/b\""),
        (
            "٢٠٢٣-١٢.parquet",
            "\"\\u0662\\u0660\\u0662\\u0663-\\u0661\\u0662.parquet\"",
        ),
    ];
    for (input, expected) in cases {
        assert_eq!(
            render(&s(input)),
            *expected,
            "escaper diverged for {input:?}"
        );
    }
}

/// Sorted-key law over the code-point adversary set (design §2.4):
/// `"" < "Z" < "a" < "é" < "\u{ffff}" < "\u{10000}"` — Rust byte-wise
/// UTF-8 `String` ordering == Unicode code-point order == CPython's
/// exact-`str` `sort_keys` comparison, INCLUDING the non-BMP boundary
/// (U+10000 sorts after U+FFFF). CPython oracle:
/// `{"":6,"Z":1,"a":2,"é":3,"￿":4,"𐀀":5}`.
#[test]
fn sorted_key_law_code_point_adversaries_including_non_bmp() {
    // Deliberately UNSORTED input pair order.
    let node = dict(&[
        ("é", Node::Int(3)),
        ("\u{10000}", Node::Int(5)),
        ("a", Node::Int(2)),
        ("", Node::Int(6)),
        ("\u{ffff}", Node::Int(4)),
        ("Z", Node::Int(1)),
    ]);
    assert_eq!(
        render(&node),
        "{\"\":6,\"Z\":1,\"a\":2,\"\\u00e9\":3,\"\\uffff\":4,\"\\ud800\\udc00\":5}"
    );
}

/// Extraction-ORDER value-render pin (design §2.4, round-1 fold R1-NIT-2):
/// a bool NODE renders the LITERAL `true`/`false` (never `1`/`0`) and None
/// renders `null` — asserted as OUTPUT BYTES over the exact
/// `[None, True, False, 1, 0]` corpus, so a downcast-ladder reorder in the
/// FFI walk (or a render regression folding Bool into Int) cannot pass
/// silently. Production combo params carry no bool — the runtime burn-in
/// would NOT catch this; this pin is the tooth.
#[test]
fn value_render_order_pin_null_true_false_one_zero() {
    let node = Node::List(vec![
        Node::Null,
        Node::Bool(true),
        Node::Bool(false),
        Node::Int(1),
        Node::Int(0),
    ]);
    assert_eq!(render(&node), "[null,true,false,1,0]");
}

/// The Rust-unit-level analog of the round-2 `Weird(int)` TRUE-VALUE pin
/// (design §2.4, R2-B-NIT-1): the pure core renders the CARRIED value —
/// `Node::Int(42)` renders `42`, no other channel exists. The FFI-level
/// half (a Python int subclass whose `__index__` diverges from its true
/// PyLong value must extract by TRUE value, 42 never 999) requires a live
/// interpreter and lives in the wheel-ON differential
/// (`tests/property/test_combo_rust_differential.py`, phase I2) — cargo
/// tests link no libpython by design, so the extraction half is not
/// pinnable here (the "where applicable" scoping).
#[test]
fn int_node_renders_true_carried_value() {
    assert_eq!(render(&Node::Int(42)), "42");
    assert_eq!(render(&Node::Int(-42)), "-42");
}

/// Int boundaries (design §2.4): `i128::MIN` / `i128::MAX` render full
/// decimal digits (CPython oracle: `str(-2**127)` / `str(2**127-1)`) —
/// `i128::MIN = −2^127` genuinely ROUTES through Rust (the §2.2 range law
/// is exactly `extract::<i128>()`'s native range, no magnitude gate) — and
/// `u64::MAX` (the `prior_n` ceiling) renders exact.
#[test]
fn int_boundaries_render_full_decimal_digits() {
    assert_eq!(
        render(&Node::Int(i128::MIN)),
        "-170141183460469231731687303715884105728"
    );
    assert_eq!(
        render(&Node::Int(i128::MAX)),
        "170141183460469231731687303715884105727"
    );
    assert_eq!(
        render(&Node::Int(i128::from(u64::MAX))),
        "18446744073709551615"
    );
}

/// Nesting laws (design §2.4): dict-in-list-in-dict; empty dict / empty
/// list / empty string; separators with no whitespace anywhere.
/// CPython oracle:
/// `{"outer":[{"b":2,"a":[]},{},[""]],"z":{}}` →
/// `{"outer":[{"a":[],"b":2},{},[""]],"z":{}}`.
#[test]
fn nesting_dict_in_list_in_dict_and_empties() {
    let node = dict(&[
        (
            "outer",
            Node::List(vec![
                dict(&[("b", Node::Int(2)), ("a", Node::List(vec![]))]),
                Node::Dict(vec![]),
                Node::List(vec![s("")]),
            ]),
        ),
        ("z", Node::Dict(vec![])),
    ]);
    assert_eq!(
        render(&node),
        "{\"outer\":[{\"a\":[],\"b\":2},{},[\"\"]],\"z\":{}}"
    );
}

/// A deep 65-level chain renders without stack trouble and with exact
/// bracket bytes — 65 = the deepest tree that can legally cross the seam
/// (the Python canonicaliser rejects depth > 64 BEFORE the FFI, so the
/// crossing tree is ≤ ~65 levels; design §8-13).
#[test]
fn deep_65_level_chain_renders_exact_brackets() {
    let mut node = Node::Int(1);
    for _ in 0..65 {
        node = Node::List(vec![node]);
    }
    let expected = format!("{}1{}", "[".repeat(65), "]".repeat(65));
    assert_eq!(render(&node), expected);
}

/// The frozen whole-payload cross-language golden (design §2.4): the
/// `canonical_trade_params` shape (sl/tp/prior_n/proximity list/fib_anchor/
/// start_equity/fill_mode:"worst_case"). The byte string below is the
/// CPython 3.12 `json.dumps(tree, sort_keys=True, separators=(",", ":"),
/// ensure_ascii=True)` output, hardcoded — the SAME literal tree + bytes
/// are pinned from Python against `canonicalise_params` in the pytest half
/// (phase I2). NEVER regenerated: a diff here is a byte-law break.
#[test]
fn frozen_canonical_trade_params_payload_golden() {
    let node = dict(&[
        ("sl_paisa", Node::Int(2000)),
        ("tp_paisa", Node::Null),
        ("prior_n", Node::Int(20)),
        (
            "proximity_bps",
            Node::List(vec![Node::Int(10), Node::Int(15), Node::Int(25)]),
        ),
        ("fib_anchor", s("prev_day")),
        ("start_equity_paisa", Node::Int(10_000_000)),
        ("fill_mode", s("worst_case")),
    ]);
    assert_eq!(
        render(&node),
        concat!(
            "{\"fib_anchor\":\"prev_day\",\"fill_mode\":\"worst_case\",",
            "\"prior_n\":20,\"proximity_bps\":[10,15,25],\"sl_paisa\":2000,",
            "\"start_equity_paisa\":10000000,\"tp_paisa\":null}",
        )
    );
}

/// Embedded quotes / backslashes / control chars inside dict KEYS escape
/// exactly like values (CPython escapes keys through the same
/// `py_encode_basestring_ascii`). Oracle:
/// `json.dumps({'a"b': 1, "c\\d": 2, "e\x1ff": 3}, ...)` →
/// `{"a\"b":1,"c\\d":2,"ef":3}`.
#[test]
fn keys_escape_like_values() {
    let node = dict(&[
        ("a\"b", Node::Int(1)),
        ("c\\d", Node::Int(2)),
        ("e\u{1f}f", Node::Int(3)),
    ]);
    assert_eq!(render(&node), "{\"a\\\"b\":1,\"c\\\\d\":2,\"e\\u001ff\":3}");
}
