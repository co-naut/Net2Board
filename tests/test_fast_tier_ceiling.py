"""The fast-tier ceiling tripwire (``tests/conftest.py``): a wall over
the 60 s ceiling prints a warning in the run's own summary — reported,
never failed (ADR-0026: the escalation is a ticket). The first version
of the hook never fired in anger because it read markers before the
``-m`` deselection and mistook every fast run for a gated tier — these
tests exist so a silent tripwire stays inconceivable: fake sessions,
fake walls, the real hook."""

import time

import pytest

import conftest


class _FakeReporter:
    def __init__(self):
        self.lines = []

    def write_line(self, line: str):
        self.lines.append(line)


class _FakeItem:
    def __init__(self, *markers: str):
        self.keywords = frozenset(markers)


class _FakePluginManager:
    def __init__(self, reporter):
        self._reporter = reporter

    def get_plugin(self, name):
        return self._reporter if name == "terminalreporter" else None


class _FakeConfig:
    def __init__(self, reporter):
        self.pluginmanager = _FakePluginManager(reporter)


class _FakeSession:
    def __init__(self, items):
        self.items = items
        self.config = _FakeConfig(_FakeReporter())
        self.stash = pytest.Stash()

    @property
    def warned(self):
        return any(
            "WARNING" in line for line in self.config.pluginmanager._reporter.lines
        )


def _aged_session(items, seconds):
    """A session whose wall started ``seconds`` ago — the fake-wall seam
    (why that seam is a stash: see ``_FAST_TIER_STARTED`` in conftest)."""
    session = _FakeSession(items)
    session.stash[conftest._FAST_TIER_STARTED] = time.perf_counter() - seconds
    return session


def test_over_the_ceiling_warns():
    session = _aged_session([_FakeItem()], seconds=conftest._FAST_TIER_CEILING_S + 5)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    assert session.warned


def test_under_the_ceiling_is_silent():
    session = _aged_session([_FakeItem()], seconds=5)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    assert not session.warned


def test_a_slow_tier_run_is_let_to_its_hours():
    session = _aged_session([_FakeItem("slow")], seconds=10_000)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    assert not session.warned


def test_a_route_tier_run_is_let_to_its_hours():
    session = _aged_session([_FakeItem("route"), _FakeItem()], seconds=10_000)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    assert not session.warned


def test_a_faked_wall_cannot_leak_between_sessions():
    """Unit tests fake walls by writing a session's stash — and when that
    storage was one shared module global (#109), a fake outlived its test
    into the real run's sessionfinish and fabricated a 10 000 s warning
    on every fast run. This replays the live wiring in the real order —
    sessionstart, then some session's fake wall, then sessionfinish —
    and pins that a wall faked onto any other storage stays invisible
    here: the hook reads only this session's stash (see the
    ``_FAST_TIER_STARTED`` story in ``tests/conftest.py``)."""
    other = _FakeSession([_FakeItem()])
    other.stash[conftest._FAST_TIER_STARTED] = time.perf_counter() - 10_000
    session = _FakeSession([_FakeItem()])
    conftest.pytest_sessionstart(session)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    assert not session.warned
