"""Shared test fakes — importable from any test module.

Keeping fakes here (rather than in one specific unit test file)
avoids the anti-pattern of ``from tests.unit.test_foo import
SomeFake`` that couples unrelated tests together and breaks if
the test file is renamed.

Current modules:

* :mod:`tests.fakes.groww` — Groww SDK fakes (FakeGrowwClient,
  FakeDataFrame, synthetic exception classes)
"""
