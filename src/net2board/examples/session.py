"""The example CLI — the one harness's runnable front end (ADR-0019,
amended by #104: the harness lives in ``net2board.harness``; this module
keeps the published command line, nothing more).

``uv run python -m net2board.examples.session [output.kicad_pcb]`` runs
the ecc83 session and writes the searched export; ``--slow`` runs the
tiny_tapeout acceptance face and prints the measurements. The lineage,
the beats, the measurement face, and the oracle live beside the engine
in ``net2board.harness`` — the acceptance path and the replay face are
the same code, so there is no second harness to drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

from net2board.harness.paths import SessionPaths
from net2board.harness.perf import compare_perf_history, render, signoff_date
from net2board.harness.session import measure_acceptance, run_session


def main(argv: list[str] | None = None, fixtures: SessionPaths | None = None) -> int:
    """The CLI face. Fixture roots are stated inputs here too (#68):
    programmatic callers inject ``SessionPaths(...)``; the bare module
    run resolves the developer default."""
    args = sys.argv[1:] if argv is None else argv
    slow = "--slow" in args
    args = [arg for arg in args if arg != "--slow"]
    if len(args) > 1:
        print(
            "usage: python -m net2board.examples.session [--slow] [output.kicad_pcb]",
            file=sys.stderr,
        )
        return 2
    output = Path(args[0]) if args else None
    if fixtures is None:
        fixtures = SessionPaths.repo_default()

    if slow:
        today = signoff_date()
        report = measure_acceptance(fixtures=fixtures, today=today)
        print(render(report), end="")
        for line in compare_perf_history(report, today=today):
            print(line)
        tiny_output = output or fixtures.tiny_default_output
        tiny_output.parent.mkdir(parents=True, exist_ok=True)
        tiny_output.write_text(report.exports.tiny_tapeout)
        ecc83_output = (
            tiny_output.with_name(tiny_output.stem + "-ecc83.kicad_pcb")
            if output
            else fixtures.ecc83_default_output
        )
        ecc83_output.parent.mkdir(parents=True, exist_ok=True)
        ecc83_output.write_text(report.exports.ecc83)
        print(f"export: wrote {tiny_output}")
        print(f"export: wrote {ecc83_output}")
        return 0

    output = output or fixtures.ecc83_default_output
    record = run_session(fixtures)
    print(f"baseline: objective {record.baseline_nm:,} nm (the committed table)")
    for step in record.steps:
        print(
            f"{step.name}: budget {step.budget:,} -> {step.stop_reason.name}, "
            f"unmet {len(step.unmet_constraint_ids)}, "
            f"objective {step.objective.total_nm:,} nm"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(record.export)
    print(f"export: wrote {output} ({len(record.export.encode())} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
