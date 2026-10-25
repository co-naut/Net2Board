"""Docs hygiene: the checks the pre-push hook cannot make (it reads
Python only). ADR numbers stay hand-allocated and gap-free; amendment
pointers resolve; committed markdown prose stays one paragraph per
physical line — a wrapped paragraph is a prose line ending without
terminal punctuation and more prose on the next physical line, the wrap
a reviewer would flag — and every ``docs/`` or ``src/net2board/`` path
any committed markdown references exists on disk."""

import os
import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"

SKIP_DIRS = {
    ".git",
    ".venv",
    ".opencode",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}

NUMBERED = re.compile(r"^(\d{4})-.*\.md$")
AMENDED_BY = re.compile(r"\(Amended by [^):]*ADR-(\d{4}):")
LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")
PATH_REF = re.compile(r"(?<![\w/.\-])((?:docs|src/net2board)/[A-Za-z0-9_./\-]+)")
TERMINAL = tuple(".!?:;)]}\"'`")


def _numbered_adrs() -> dict[int, list[Path]]:
    found: dict[int, list[Path]] = {}
    for path in ADR_DIR.glob("*.md"):
        match = NUMBERED.match(path.name)
        if match:
            found.setdefault(int(match.group(1)), []).append(path)
    return found


def _markdown_files(root: Path = REPO_ROOT) -> Iterator[Path]:
    """Every committed markdown file under ``root``: the walk skips the
    untracked directories, whose third-party prose is not ours to guard."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _prose_lines(path: Path) -> Iterator[tuple[int, str]]:
    """``(line number, text)`` for the prose lines: outside a fence, not
    blank, not a heading, quote, table row, or list item, and not
    indented (a continuation or code-shaped line). A GLOSSARY-style term
    entry — ``**Board**:`` then its own paragraph — passes: each of its
    lines ends with terminal punctuation, so nothing reads as a wrap."""
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or line[:1] in (" ", "\t"):
            continue
        stripped = line.strip()
        if stripped.startswith(("#", ">", "|")) or LIST_ITEM.match(stripped):
            continue
        yield number, line


def _outside_fence_text(path: Path) -> str:
    """The markdown with fenced code blocks dropped: command examples
    are not path references to verify."""
    kept: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


def test_adr_numbers_are_unique():
    found = _numbered_adrs()
    dupes = {n: [p.name for p in paths] for n, paths in found.items() if len(paths) > 1}
    assert not dupes, f"multiple files claim one ADR number: {dupes}"


def test_adr_numbers_are_gap_free():
    numbers = sorted(_numbered_adrs())
    assert numbers, "no numbered ADR files found"
    gaps = [n for n in range(1, numbers[-1] + 1) if n not in numbers]
    assert not gaps, f"missing from the ADR sequence: {gaps}"


def test_amendment_pointers_target_existing_adrs():
    found = _numbered_adrs()
    dangling = []
    for paths in found.values():
        for path in paths:
            for match in AMENDED_BY.finditer(path.read_text(encoding="utf-8")):
                target = int(match.group(1))
                if target not in found:
                    dangling.append(f"{path.name} -> ADR-{target:04d}")
    assert not dangling, f"amendment pointers target missing ADRs: {dangling}"


def test_markdown_prose_is_unwrapped():
    wrapped = []
    for path in _markdown_files():
        previous: tuple[int, str] | None = None
        for number, line in _prose_lines(path):
            if (
                previous is not None
                and number == previous[0] + 1
                and not previous[1].rstrip().endswith(TERMINAL)
            ):
                wrapped.append(f"{path.relative_to(REPO_ROOT)}:{previous[0]}")
            previous = (number, line)
    assert not wrapped, (
        f"prose wrapped across lines (one paragraph per line): {wrapped}"
    )


def unresolved_path_refs(root: Path) -> list[str]:
    """``file: ref`` for every ``docs/`` or ``src/net2board/`` reference in
    the markdown under ``root`` that does not resolve inside ``root``. The
    carve-export test runs this over the archive — one definition, #65."""
    missing = []
    for path in _markdown_files(root):
        for match in PATH_REF.finditer(_outside_fence_text(path)):
            ref = match.group(1).rstrip(".")
            if not (root / ref).exists():
                missing.append(f"{path.relative_to(root)}: {ref}")
    return missing


def test_referenced_repo_paths_exist():
    missing = unresolved_path_refs(REPO_ROOT)
    assert not missing, f"referenced paths that do not exist: {missing}"
