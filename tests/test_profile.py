"""The profiler face's CLI contract (``docs/agents/profiling.md``): the
``--bench`` quick per-probe reading and ``--scope``'s single-scope
profile. The tests pin the CLI contract — fixture restriction, usage
errors, the output line's shape — never a number: report-only is
ADR-0026, and a timing assertion here would be exactly the sin the face
exists to avoid. ``--scope``'s searched board is stubbed to the entry
board and the budget drops to a few hundred probes: the pinned headline
solve is minutes-scale, and the face's pairing is ``perf``'s own, so the
full machinery is exercised at fast-tier cost."""

from types import SimpleNamespace

from net2board.examples import profile
from net2board.harness import session


class TestBenchCli:
    def test_bench_prints_the_per_probe_reading(self, capsys):
        exit_code = profile.main(["tiny", "--bench", "400"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "400 probes on the tiny entry board" in out
        assert "µs/probe" in out

    def test_bench_is_tiny_only(self, capsys):
        assert profile.main(["ecc83", "--bench", "100"]) == 2
        assert "--bench is tiny-only" in capsys.readouterr().err

    def test_bench_rejects_a_non_positive_budget(self, capsys):
        assert profile.main(["tiny", "--bench", "0"]) == 2

    def test_bench_and_budget_together_are_a_usage_error(self, capsys):
        assert profile.main(["tiny", "--budget", "10", "--bench", "10"]) == 2


class TestScopeCli:
    """``--scope`` prices and profiles one scope of the searched board:
    a bucket by label or a single unlocked ref by name — tiny-only,
    exclusive of the other flags, and authoritative about unknown or
    locked names."""

    @staticmethod
    def _stub_searched_board(monkeypatch) -> None:
        entry = session._tiny_entry(session.SessionPaths.repo_default())
        monkeypatch.setattr(session, "BUDGET_TINY", 300)
        monkeypatch.setattr(
            session,
            "run_tiny_session",
            lambda fixtures=None: SimpleNamespace(
                headline=SimpleNamespace(board=entry)
            ),
        )

    def test_a_bucket_prices_and_profiles(self, capsys, monkeypatch):
        self._stub_searched_board(monkeypatch)
        assert profile.main(["tiny", "--scope", "1-2 pads"]) == 0
        out = capsys.readouterr().out
        assert "movers" in out
        assert "µs/probe" in out

    def test_a_named_ref_prices_and_profiles(self, capsys, monkeypatch):
        self._stub_searched_board(monkeypatch)
        assert profile.main(["tiny", "--scope", "J4"]) == 0
        out = capsys.readouterr().out
        assert "1 mover" in out
        assert "µs/probe" in out

    def test_scope_is_tiny_only(self, capsys):
        assert profile.main(["ecc83", "--scope", "J4"]) == 2
        assert "--scope is tiny-only" in capsys.readouterr().err

    def test_scope_excludes_the_other_flags(self, capsys):
        assert profile.main(["tiny", "--scope", "J4", "--budget", "10"]) == 2
        assert profile.main(["tiny", "--scope", "J4", "--bench", "10"]) == 2

    def test_scope_rejects_an_unknown_ref(self, capsys, monkeypatch):
        self._stub_searched_board(monkeypatch)
        assert profile.main(["tiny", "--scope", "NOPE"]) == 2
        assert "no component 'NOPE'" in capsys.readouterr().err
