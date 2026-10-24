"""The profiling face — the whole session under cProfile (ADR-0026, #99).

``uv run python -m net2board.examples.profile [ecc83|tiny] [--budget N]``
runs that fixture's session — ``run_session`` / ``run_tiny_session``, the
acceptance path itself, so there is no second harness to drift
(ADR-0019) — under cProfile and prints the top functions by own time
with call counts. Counts are the actionable currency (ADR-0026: the
fast tier's count-assertion culture): an ``ncalls`` that scales wrong is
the finding ("this is called n² times"); the µs ranking is only the
lead, because cProfile's per-call overhead inflates exactly the
Evaluator's many-tiny-calls hot loop. If the ranking ever misdirects,
that is the recorded trigger to add a sampling profiler (pyinstrument)
as one deliberate pin bump — never speculative machinery.

``--budget N`` overrides the pinned budget — ``BUDGET_MAIN`` on ecc83,
``BUDGET_TINY`` on tiny — for a smaller/faster run; the default is the
pinned constant. ``BUDGET_UNDERCUT`` is never overridden: its beat
expects exhaustion at 1,600, and either direction would break that
beat's meaning. Below a step's convergence point the session's pinned
beats no longer hold (a truncated step stops ``BUDGET_EXHAUSTED`` where
it expects ``NO_FURTHER_IMPROVEMENT``); the tool notes that on stderr
and profiles the work that ran — that is the point of the override, so
the run still exits 0. Tiny at the pinned budget is a minutes-scale run;
``--budget`` is the way to keep it cheap.

``--scope SCOPE`` prices and profiles one scope of the tiny session's
searched board: a move-mix bucket by label (``1-2 pads``, ``3-8 pads``,
``9+ pads``) or any single unlocked ref by name. The bucket selection is
``harness.perf.bucket_refs``' own and the probe wall is ``_probe_wall``'s
pairing — the full scoped solve minus its budget-zero fixed cost — so a
scope reading pairs with the perf JSON's bucket rows; the same solve then
runs under cProfile, tops on stdout. Tiny-only, and ``--budget`` is
refused: the searched board costs the pinned headline solve (a
minutes-scale run, detached per ``docs/agents/long-runs.md``), and a
cheaper headline is a different board whose readings pair with nothing.

``--bench N`` is the quick per-probe reading (``docs/agents/long-runs.md``'s
bench-first rule): one solve of the all-unplaced tiny entry board at an
``N``-probe budget, wall-timed, µs/probe on stdout — no cProfile, no
session. Tiny-only: the reading exists to catch per-probe slowdowns on
the real fixture's big parts, which the fast tier's small-board walls
never touch (#91's 20× per-probe regression landed unseen). Report-only
(ADR-0026): the number goes in a ticket or receipt; nothing asserts on
it.

Dev tool only, never collected by pytest (ADR-0026) — the receipt is a
hand run.
"""

from __future__ import annotations

import cProfile
import pstats
import sys
from collections.abc import Callable
from time import perf_counter

from net2board.harness import perf, session
from net2board.harness.session import SessionBeatFailed
from net2board.solver import solve

_USAGE = (
    "usage: python -m net2board.examples.profile"
    " [ecc83|tiny] [--budget N | --bench N | --scope SCOPE]"
)


def _parse_flag(
    flag: str, args: list[str], cast: Callable[[str], int | str]
) -> tuple[int | str | None, list[str]]:
    """Pull ``--flag VALUE`` (or ``--flag=VALUE``) out of the argument
    list and cast the value — the argument shapes this face takes,
    parsed by hand like every example CLI. A missing or non-castable
    value prints the usage and exits 2."""
    prefix = flag + "="
    for index, arg in enumerate(args):
        if arg == flag:
            if index + 1 >= len(args):
                print(_USAGE, file=sys.stderr)
                raise SystemExit(2)
            raw, rest = args[index + 1], [*args[:index], *args[index + 2 :]]
        elif arg.startswith(prefix):
            raw, rest = (
                arg.removeprefix(prefix),
                [*args[:index], *args[index + 1 :]],
            )
        else:
            continue
        try:
            return cast(raw), rest
        except ValueError:
            print(_USAGE, file=sys.stderr)
            raise SystemExit(2) from None
    return None, args


def _bench(probes: int) -> int:
    """One solve of the all-unplaced tiny entry board at an ``N``-probe
    budget, wall-timed, µs/probe on stdout. The budget is counted in
    candidates evaluated (one probe each), so a stop of
    ``BUDGET_EXHAUSTED`` means exactly ``probes`` were spent and the
    division is exact; an earlier convergence prints a note — the
    reading then divides by the budget and overstates. Report-only
    (ADR-0026): the number is for a ticket or receipt, nothing fails on
    it, and the readings that matter are the comparisons against the
    latest receipt's number, not any absolute."""
    if probes <= 0:
        print(_USAGE, file=sys.stderr)
        return 2
    entry = session._tiny_entry(session.SessionPaths.repo_default())
    start = perf_counter()
    result = solve(entry, budget=probes)
    wall_s = perf_counter() - start
    stop = result.stop_reason.name
    if stop != "BUDGET_EXHAUSTED":
        print(
            f"bench: stopped {stop} — the board converged before the budget, "
            f"so the µs/probe reading divides by the budget and overstates",
            file=sys.stderr,
        )
    print(
        f"bench: {probes:,} probes on the tiny entry board in {wall_s:.2f}s"
        f" — {wall_s / probes * 1e6:.0f} µs/probe ({stop})"
    )
    return 0


def _scope(scope: str) -> int:
    """One scope of the tiny session's searched board, priced and
    profiled: a move-mix bucket by label — the selection is
    ``harness.perf.bucket_refs``' own, so the reading pairs with the
    perf JSON's bucket rows — or any single unlocked ref by name. The
    probe wall is ``_probe_wall``'s pairing (full scoped solve minus the
    budget-zero fixed cost), report-only like every number here
    (ADR-0026); the cProfile tops that follow are the lead, read like
    the whole-session face's. A locked or unknown name fails as a usage
    error — the searched board is the authority on both."""
    record = session.run_tiny_session()
    board = record.headline.board
    refs = perf.bucket_refs(board).get(scope)
    if refs is None:
        locked = {placement.ref for placement in board.placements if placement.locked}
        if scope in locked:
            print(
                f"profile: {scope} is locked — a scope names unlocked movers",
                file=sys.stderr,
            )
            return 2
        if scope not in {component.ref for component in board.components}:
            print(
                f"profile: no component '{scope}' on the searched board",
                file=sys.stderr,
            )
            return 2
        refs = (scope,)
    if not refs:
        print(
            f"profile: the '{scope}' bucket holds no movers on the searched board",
            file=sys.stderr,
        )
        return 2
    wall_s, probes = perf._probe_wall(
        board, refs, ceiling=session.BUDGET_TINY, clock=perf_counter
    )
    movers = "1 mover" if len(refs) == 1 else f"{len(refs)} movers"
    reading = f" — {wall_s / probes * 1e6:.1f} µs/probe" if probes else ""
    print(
        f"scope: {scope} ({movers}) — {probes:,} probes in {wall_s:.2f}s"
        f" probe wall{reading}"
    )
    profiler = cProfile.Profile()
    profiler.enable()
    solve(board, refs, budget=session.BUDGET_TINY)
    profiler.disable()
    stats = pstats.Stats(profiler).strip_dirs()
    stats.sort_stats("tottime").print_stats(25)
    stats.sort_stats("cumtime").print_stats(25)
    return 0


def main(argv: list[str] | None = None) -> int:
    """The CLI face: pick the fixture, apply any budget override to the
    session module's pinned constants (the acceptance path's own code
    stays untouched — the tool patches, the harness doesn't grow a
    parameter), and run it under cProfile — or, with ``--bench``, skip
    the profiler and print the quick per-probe reading instead. Exit 0
    even when a truncated budget broke a pinned beat — the note on
    stderr carries that news; a missing fixture root fails as the named
    ``SessionFixturesMissing``."""
    args = list(sys.argv[1:] if argv is None else argv)
    budget, args = _parse_flag("--budget", args, int)
    bench, args = _parse_flag("--bench", args, int)
    scope, args = _parse_flag("--scope", args, str)
    if sum(value is not None for value in (budget, bench, scope)) > 1:
        print(_USAGE, file=sys.stderr)
        return 2
    if len(args) != 1 or args[0] not in ("ecc83", "tiny"):
        print(_USAGE, file=sys.stderr)
        return 2
    fixture = args[0]
    if bench is not None:
        if fixture != "tiny":
            print(
                "profile: --bench is tiny-only — the per-probe reading lives "
                "on tiny_tapeout's entry board",
                file=sys.stderr,
            )
            return 2
        return _bench(bench)
    if scope is not None:
        if fixture != "tiny":
            print(
                "profile: --scope is tiny-only — the searched board is tiny_tapeout's",
                file=sys.stderr,
            )
            return 2
        return _scope(scope)
    if fixture == "ecc83":
        budget_attr, run = "BUDGET_MAIN", session.run_session
    else:
        budget_attr, run = "BUDGET_TINY", session.run_tiny_session

    pinned = getattr(session, budget_attr)
    if budget is None:
        budget = pinned
        print(f"profile: {fixture} under {budget_attr}={budget:,} (pinned)")
    else:
        setattr(session, budget_attr, budget)
        print(f"profile: {fixture} under {budget_attr}={budget:,} (pinned {pinned:,})")

    profiler = cProfile.Profile()
    profiler.enable()
    try:
        run()
    except SessionBeatFailed as error:
        print(
            f"profile: {budget_attr}={budget:,} truncated the session — pinned "
            f"beats not held ({error}); profiling the work that ran",
            file=sys.stderr,
        )
    finally:
        profiler.disable()

    pstats.Stats(profiler).strip_dirs().sort_stats("tottime").print_stats(25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
