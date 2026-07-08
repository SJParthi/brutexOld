"""Headless-browser dashboard truth suite (DEC-DASHBOARD-BROWSER-TRUTH-001).

Playwright + Chromium rendering the REAL dashboard templates + static JS
against fixture-backed ``build_app`` apps, with real DOM assertions for the
pinned behaviors. Opt-in via ``BRUTEX_BROWSER_TESTS=1`` — run by the dedicated
CI ``browser`` job, never the main test job (see ``conftest.py`` for the
collection guard and ``_guards.py`` for the suite's own discipline rules).
"""
