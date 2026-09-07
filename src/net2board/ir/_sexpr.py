"""A minimal KiCad s-expression reader — private to the ``ir`` package.

Atoms stay plain strings — quoted and bare alike — so coordinate tokens never
touch a float before their exact ``Decimal`` → integer-nanometre conversion
(ADR-0005). A document is a single top-level form; anything else is malformed.
"""

from __future__ import annotations

__all__ = ["read_sexpr"]


def read_sexpr(text: str) -> list | str:
    """Read one s-expression form; trailing garbage is an error."""
    tokens = _tokenize(text)
    form, pos = _read_form(tokens, 0)
    if pos != len(tokens):
        raise ValueError(f"unexpected {tokens[pos][0]!r} after the top-level form")
    return form


def _tokenize(text: str) -> list[tuple[str, int]]:
    tokens: list[tuple[str, int]] = []
    pos, end, line = 0, len(text), 1
    while pos < end:
        char = text[pos]
        if char == "\n":
            line += 1
            pos += 1
        elif char in " \t\r":
            pos += 1
        elif char in "()":
            tokens.append((char, line))
            pos += 1
        elif char == '"':
            pos += 1
            start_line = line
            parts: list[str] = []
            while pos < end and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < end:
                    parts.append(text[pos + 1])
                    pos += 2
                else:
                    if text[pos] == "\n":
                        line += 1
                    parts.append(text[pos])
                    pos += 1
            if pos >= end:
                raise ValueError(f"unterminated string starting at line {start_line}")
            pos += 1
            tokens.append(("".join(parts), start_line))
        else:
            start = pos
            while pos < end and text[pos] not in ' \t\r\n()"':
                pos += 1
            tokens.append((text[start:pos], line))
    return tokens


def _read_form(tokens: list[tuple[str, int]], pos: int) -> tuple[list | str, int]:
    token, line = tokens[pos]
    if token == "(":
        pos += 1
        items: list = []
        while True:
            if pos >= len(tokens):
                raise ValueError(f"unclosed '(' opened at line {line}")
            if tokens[pos][0] == ")":
                return items, pos + 1
            form, pos = _read_form(tokens, pos)
            items.append(form)
    if token == ")":
        raise ValueError(f"unexpected ')' at line {line}")
    return token, pos + 1
