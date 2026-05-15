"""Shared test fakes — importable from any test module.

Keeping fakes here (rather than in one specific unit test file)
avoids the anti-pattern of ``from tests.unit.test_foo import
SomeFake`` that couples unrelated tests together and breaks if
the test file is renamed.

The Dhan adapter fakes will be added here in Phase 2 of the Track 1
build plan (see ``docs/TRACK1_PHASE_PLAN.md``).
"""
