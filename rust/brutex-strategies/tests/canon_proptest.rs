//! Property invariants over the pure render core
//! (DEC-STRATEGIES-RUST-COMBO-001; determinism + dict-order-insensitivity +
//! the ensure_ascii guarantee). The CPython byte-EQUALITY property (render
//! ≡ `json.dumps`) needs a live interpreter and lives in the wheel-ON
//! hypothesis differential (`tests/property/test_combo_rust_differential.py`,
//! phase I2) — these cargo properties pin the interpreter-free invariants.

use brutex_strategies::canon::{Node, render};
use proptest::prelude::*;

/// Arbitrary lowered-tree nodes: str / int / bool / None leaves, list /
/// dict branches — exactly the vocabulary that can legally cross the seam.
/// Keys deliberately include sort adversaries (empty, non-ASCII, non-BMP).
fn node_strategy() -> impl Strategy<Value = Node> {
    let leaf = prop_oneof![
        Just(Node::Null),
        any::<bool>().prop_map(Node::Bool),
        any::<i128>().prop_map(Node::Int),
        "[\\x00-\\x7f\u{e9}\u{20b9}\u{ffff}\u{10000}\u{1f600}]{0,12}".prop_map(Node::Str),
    ];
    leaf.prop_recursive(4, 64, 6, |inner| {
        prop_oneof![
            prop::collection::vec(inner.clone(), 0..6).prop_map(Node::List),
            prop::collection::btree_map("[\\x00-\\x7f\u{e9}\u{ffff}\u{10000}]{0,8}", inner, 0..6)
                .prop_map(|m| Node::Dict(m.into_iter().collect())),
        ]
    })
}

proptest! {
    /// Rendering is pure and deterministic — two renders of the same tree
    /// are byte-identical.
    #[test]
    fn render_is_deterministic(node in node_strategy()) {
        prop_assert_eq!(render(&node), render(&node));
    }

    /// `ensure_ascii=True` guarantee: the output is ALWAYS pure ASCII, for
    /// every input tree (the `PyResult<String>`-is-faithful argument of
    /// design §8-6 rests on this).
    #[test]
    fn output_is_always_pure_ascii(node in node_strategy()) {
        prop_assert!(render(&node).is_ascii());
    }

    /// Dict INPUT pair order never affects the output — the render sorts by
    /// key (the `sort_keys=True` law), so reversing the extracted pair
    /// order is invisible.
    #[test]
    fn dict_input_order_is_invisible(node in node_strategy()) {
        fn reverse_dicts(node: &Node) -> Node {
            match node {
                Node::List(items) => Node::List(items.iter().map(reverse_dicts).collect()),
                Node::Dict(pairs) => Node::Dict(
                    pairs
                        .iter()
                        .rev()
                        .map(|(k, v)| (k.clone(), reverse_dicts(v)))
                        .collect(),
                ),
                other => other.clone(),
            }
        }
        prop_assert_eq!(render(&node), render(&reverse_dicts(&node)));
    }
}
