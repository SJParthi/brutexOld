"""The Pin D1-1 write-domain refusal battery (DEC-STAGED-SEQUENCER-SEAMS-001).

ADR-0021 §2.5 Pin D1-1 + Decision 4's WRITE-DOMAIN TYPING law — the REQUIRED
negative-pin battery: the nested-root false-pass fixture, the
UNREGISTERED-feeds fixtures (fold #4, NG3-EDGE), both cross-domain directions,
the typed refusal, the symlink fixtures, and the mount-shape PASS case (the
DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 R6 precedent: a lake root reached via a
symlink still passes — both sides resolve consistently). Fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.data.errors import DataLakeError
from brutex.data.write_domain import (
    FEEDS_COMPONENT,
    PRIMARY_DOMAIN_NAME,
    WriteDomain,
    WriteDomainError,
    assert_write_domain,
    feeds_write_domain,
    primary_write_domain,
)

#: The second-vendor feed subtree token used across the battery — a
#: placeholder vendor dir (the concrete vendor binding is WS-C/WS-D).
_FEED = "feeds/vendor2"
_OTHER_FEED = "feeds/vendor9"


def _lake(tmp_path: Path) -> Path:
    """A real lake root with primary trees + two feed subtrees on disk."""
    root = tmp_path / "lake"
    for sub in (
        "bars/NSE/INDEX/NIFTY/1minute/2024",
        "options/S1",
        f"{_FEED}/bars",
        f"{_OTHER_FEED}/bars",
    ):
        (root / sub).mkdir(parents=True)
    return root


class TestConstruction:
    """Registration-time laws: resolve-once, validated subtrees, fail-loud junk."""

    def test_primary_domain_resolves_root_once_at_registration(self, tmp_path: Path) -> None:
        root = _lake(tmp_path)
        domain = primary_write_domain(root)
        assert domain.name == PRIMARY_DOMAIN_NAME
        assert domain.primary is True
        assert domain.resolved_root == root.resolve()
        assert domain.allowed_parts == ()

    def test_feeds_domain_carries_its_own_subtree_only(self, tmp_path: Path) -> None:
        domain = feeds_write_domain(_lake(tmp_path), _FEED)
        assert domain.name == _FEED
        assert domain.primary is False
        assert domain.allowed_parts == ((FEEDS_COMPONENT, "vendor2"),)

    def test_feeds_domain_foreign_belt_excludes_own_subtree(self, tmp_path: Path) -> None:
        domain = feeds_write_domain(
            _lake(tmp_path), _FEED, foreign_feed_subtrees=(_FEED, _OTHER_FEED)
        )
        assert domain.foreign_parts == ((FEEDS_COMPONENT, "vendor9"),)

    def test_feeds_domain_outside_feeds_prefix_refused(self, tmp_path: Path) -> None:
        with pytest.raises(WriteDomainError, match="must sit under"):
            feeds_write_domain(_lake(tmp_path), "bars/NSE")

    def test_feeds_domain_bare_feeds_component_refused(self, tmp_path: Path) -> None:
        # `feeds/` alone claims EVERY vendor's subtree — a mis-registration.
        with pytest.raises(WriteDomainError, match="must sit under"):
            feeds_write_domain(_lake(tmp_path), "feeds")

    @pytest.mark.parametrize("junk", ["", "/abs/path", "feeds/../bars"])
    def test_junk_subtree_tokens_refused(self, tmp_path: Path, junk: str) -> None:
        with pytest.raises(WriteDomainError):
            feeds_write_domain(_lake(tmp_path), junk)

    def test_junk_foreign_subtree_refused_on_primary(self, tmp_path: Path) -> None:
        with pytest.raises(WriteDomainError, match="RELATIVE"):
            primary_write_domain(_lake(tmp_path), registered_feed_subtrees=("/abs",))

    def test_unresolvable_root_is_a_loud_registration_refusal(self, tmp_path: Path) -> None:
        loop = tmp_path / "loop"
        loop.symlink_to(loop)  # a self-loop the resolve cannot terminate
        with pytest.raises(WriteDomainError, match="cannot resolve lake root"):
            primary_write_domain(loop / "lake")

    def test_error_is_a_typed_data_lake_error(self) -> None:
        # The Pin D1-1 refusal is TYPED — a DataLakeError member, never a bare
        # ValueError a writer could conflate with a data defect.
        assert issubclass(WriteDomainError, DataLakeError)


class TestPrimaryDomainRefusals:
    """The primary profile: bars/options pass; the WHOLE feeds/ prefix refuses."""

    def test_primary_bars_dest_passes(self, tmp_path: Path) -> None:
        root = _lake(tmp_path)
        domain = primary_write_domain(root, registered_feed_subtrees=(_FEED,))
        assert_write_domain(root / "bars/NSE/INDEX/NIFTY/1minute/2024/01.parquet", domain)

    def test_primary_options_dest_passes(self, tmp_path: Path) -> None:
        root = _lake(tmp_path)
        assert_write_domain(root / "options/S1/2024-01.parquet", primary_write_domain(root))

    def test_nested_root_false_pass_refused(self, tmp_path: Path) -> None:
        """The NG-3 fixture: a REGISTERED feed dest NESTS under the primary root.

        A bare realpath-STARTSWITH check against the root would FALSE-PASS
        exactly this dest — the refuted single-``Path`` form. The allowed-set
        + whole-``feeds/``-prefix law refuses it.
        """
        root = _lake(tmp_path)
        domain = primary_write_domain(root, registered_feed_subtrees=(_FEED,))
        dest = root / _FEED / "bars/NSE/INDEX/NIFTY/1minute/2024/01.parquet"
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            assert_write_domain(dest, domain)

    def test_unregistered_feeds_dest_refused(self, tmp_path: Path) -> None:
        """The fold-#4 NG3-EDGE fixture: an UNREGISTERED feeds/<other>/ dest.

        The registered-subtrees-COMPLEMENT form would include this dest in the
        primary domain (false pass); the structural whole-prefix exclusion is
        registration-INDEPENDENT — no feed subtree is registered here at all.
        """
        root = _lake(tmp_path)
        domain = primary_write_domain(root)  # NOTHING registered
        with pytest.raises(WriteDomainError, match="registration-independent"):
            assert_write_domain(root / "feeds/unregistered/bars/x.parquet", domain)

    def test_outside_lake_dest_refused(self, tmp_path: Path) -> None:
        root = _lake(tmp_path)
        with pytest.raises(WriteDomainError, match="OUTSIDE the lake root"):
            assert_write_domain(tmp_path / "elsewhere/x.parquet", primary_write_domain(root))


class TestFeedsDomainRefusals:
    """The feeds profile: own subtree passes; primary trees + foreign feeds refuse."""

    def test_own_subtree_dest_passes(self, tmp_path: Path) -> None:
        root = _lake(tmp_path)
        domain = feeds_write_domain(root, _FEED, foreign_feed_subtrees=(_OTHER_FEED,))
        assert_write_domain(root / _FEED / "bars/NSE/INDEX/NIFTY/1m/2024/01.parquet", domain)

    def test_primary_bars_dest_refused_for_feeds_profile(self, tmp_path: Path) -> None:
        """The other direction of the both-directions law: feeds → primary refuses."""
        root = _lake(tmp_path)
        domain = feeds_write_domain(root, _FEED)
        with pytest.raises(WriteDomainError, match="cross-domain writes are refused"):
            assert_write_domain(root / "bars/NSE/INDEX/NIFTY/1minute/2024/01.parquet", domain)

    def test_foreign_feed_subtree_refused(self, tmp_path: Path) -> None:
        """A feeds-profile dest under a FOREIGN feeds/<other>/ subtree refuses."""
        root = _lake(tmp_path)
        domain = feeds_write_domain(root, _FEED, foreign_feed_subtrees=(_OTHER_FEED,))
        with pytest.raises(WriteDomainError, match="cross-domain"):
            assert_write_domain(root / _OTHER_FEED / "bars/x.parquet", domain)

    def test_unregistered_foreign_feed_refused_structurally(self, tmp_path: Path) -> None:
        """Even with NO foreign belt, a foreign feed dest is not under the own
        subtree — the allowed-SET form refuses it structurally."""
        root = _lake(tmp_path)
        domain = feeds_write_domain(root, _FEED)  # no foreign belt registered
        with pytest.raises(WriteDomainError, match="falls outside"):
            assert_write_domain(root / "feeds/unregistered/bars/x.parquet", domain)

    def test_explicit_foreign_belt_arm_fires_after_allowed_match(self, tmp_path: Path) -> None:
        """The belt arm: a dest matching an allowed subtree that is ALSO inside a
        registered foreign subtree refuses (future overlapping-domain shapes)."""
        root = _lake(tmp_path)
        # A deliberately-overlapping registration: own subtree feeds/, foreign
        # feeds/vendor9 — only constructible via the dataclass directly (the
        # constructors validate today's shapes; the belt guards future ones).
        domain = WriteDomain(
            name="feeds-overlap",
            resolved_root=root.resolve(),
            allowed_parts=((FEEDS_COMPONENT,),),
            foreign_parts=((FEEDS_COMPONENT, "vendor9"),),
            primary=False,
        )
        assert_write_domain(root / _FEED / "bars/x.parquet", domain)  # allowed, not foreign
        with pytest.raises(WriteDomainError, match="FOREIGN registered subtree"):
            assert_write_domain(root / _OTHER_FEED / "bars/x.parquet", domain)


class TestSymlinkAndMountShapes:
    """The R6 precedent: resolve the DEST + the ROOT, never the subtree components."""

    def test_symlinked_dest_escaping_the_domain_refused(self, tmp_path: Path) -> None:
        """A dest behind a symlink whose TARGET escapes the lake refuses —
        the POST-realpath containment law."""
        root = _lake(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "bars" / "linked"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(WriteDomainError, match="OUTSIDE the lake root"):
            assert_write_domain(link / "x.parquet", primary_write_domain(root))

    def test_symlinked_feed_dest_presented_to_primary_refused(self, tmp_path: Path) -> None:
        """A symlink from a primary-looking path INTO the feed subtree refuses
        for the PRIMARY profile — the resolved dest sits under feeds/."""
        root = _lake(tmp_path)
        link = root / "bars" / "sneaky"
        link.symlink_to(root / _FEED / "bars", target_is_directory=True)
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            assert_write_domain(link / "x.parquet", primary_write_domain(root))

    def test_unresolvable_dest_is_the_same_typed_refusal(self, tmp_path: Path) -> None:
        """A symlink-loop dest cannot resolve — the guarded-resolve refusal
        (the R5-fold idiom), never a batch-aborting bare RuntimeError."""
        root = _lake(tmp_path)
        loop = root / "bars" / "loop"
        loop.symlink_to(loop)
        with pytest.raises(WriteDomainError, match="cannot resolve write dest"):
            assert_write_domain(loop / "x.parquet", primary_write_domain(root))

    def test_mount_shape_lake_root_via_symlink_passes(self, tmp_path: Path) -> None:
        """The legitimate mount shape: the lake ROOT reached via a symlink still
        PASSES — both sides resolve the root consistently (the R6 law: the
        final subtree components are appended literally, the ROOT resolves)."""
        real_root = _lake(tmp_path)
        mount = tmp_path / "mnt-lake"
        mount.symlink_to(real_root, target_is_directory=True)
        domain = primary_write_domain(mount, registered_feed_subtrees=(_FEED,))
        # In-domain dest addressed THROUGH the symlinked root: passes.
        assert_write_domain(mount / "bars/NSE/INDEX/NIFTY/1minute/2024/01.parquet", domain)
        # And the structural feeds/ exclusion still holds through the mount.
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            assert_write_domain(mount / _FEED / "bars/x.parquet", domain)
        feed_domain = feeds_write_domain(mount, _FEED)
        assert_write_domain(mount / _FEED / "bars/x.parquet", feed_domain)

    def test_root_itself_in_primary_domain(self, tmp_path: Path) -> None:
        """The root itself (rel parts = ()) is primary-domain territory — a
        root-level sidecar write passes the primary check."""
        root = _lake(tmp_path)
        assert_write_domain(root / ".pull_intent.json", primary_write_domain(root))
