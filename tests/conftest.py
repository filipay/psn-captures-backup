"""Shared test fixtures.

The test suite must never make live PSN calls or pick up a developer's real
credentials. ``cli.main`` calls ``load_dotenv()``, which reads ``.env`` from the
working directory; when a developer has a populated ``.env`` (real ``NPSSO``),
that would silently un-isolate tests that rely on the environment being empty.
Neutralise it for the whole suite.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("psn_captures_backup.cli.load_dotenv", lambda *args, **kwargs: False)
