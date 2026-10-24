"""#101 — the acceptance run speaks per phase.

``measure_acceptance`` is minutes of deliberate wall time; until now it
printed nothing until the whole report rendered, so a death at minute 59
looked exactly like one at minute 1 (three silent 2026-10-15 deaths).
The seam under test here is ``PhaseReporter`` — the injected-clock face
whose lines the harness writes per phase: ``[+312s] tiny session done``,
flushed immediately, elapsed measured from the reporter's start. The
slow tier (the ``tiny_acceptance`` fixture) asserts the harness emits
the full phase sequence in order; these fast tests pin the line format,
the accumulation, the flush, and the stderr default.
"""

import io

from net2board.harness.session import PhaseReporter


def fake_clock(readings: list[float]):
    """A clock handing out pre-set readings — one per reporter call."""
    readings = iter(readings)
    return lambda: next(readings)


class FlushSpy:
    """A writer that counts flushes — print()'s flush=True is the point."""

    def __init__(self):
        self.chunks: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1

    def getvalue(self) -> str:
        return "".join(self.chunks)


def test_a_phase_line_carries_elapsed_wall_and_the_label():
    out = io.StringIO()
    reporter = PhaseReporter(out, clock=fake_clock([0.0, 312.0]))
    reporter.phase("tiny session done")
    assert out.getvalue() == "[+312s] tiny session done\n"


def test_lines_accumulate_and_elapsed_runs_from_the_reporter_start():
    out = io.StringIO()
    reporter = PhaseReporter(out, clock=fake_clock([0.0, 0.0, 30.0, 362.0]))
    reporter.phase("acceptance run started")
    reporter.phase("tiny session done")
    reporter.phase("replay done")
    assert out.getvalue() == (
        "[+0s] acceptance run started\n[+30s] tiny session done\n[+362s] replay done\n"
    )


def test_every_line_flushes_immediately():
    out = FlushSpy()
    reporter = PhaseReporter(out, clock=fake_clock([0.0, 5.0]))
    reporter.phase("tiny session done")
    assert out.flushes == 1
    assert out.getvalue() == "[+5s] tiny session done\n"


def test_the_default_out_is_stderr_at_call_time(capsys):
    """``None`` resolves to ``sys.stderr`` when the line is written, not
    when the reporter is built — runtime redirection (a detached run's
    log, pytest's capsys) has to be picked up."""
    reporter = PhaseReporter(clock=fake_clock([0.0, 7.0]))
    reporter.phase("provenance done")
    assert capsys.readouterr().err == "[+7s] provenance done\n"
