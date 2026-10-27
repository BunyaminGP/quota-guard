#!/usr/bin/env python3
"""
check_bash32_compat.py — static guard against the bash 3.2 command
substitution parsing bugs that broke cc-run on macOS (see CHANGELOG
0.8.2/0.8.3).

macOS ships /bin/bash 3.2 (the last GPLv2 release), and that is what
`#!/usr/bin/env bash` resolves to on a default Mac. Its command
substitution scanner matches parentheses by naive character counting
instead of using the real shell parser (fixed upstream in bash 4.0), so
two patterns that are perfectly legal to every modern bash make 3.2
reject the entire file as a syntax error before running a single line:

  1. an ODD number of apostrophes anywhere inside a `$( ... )` span —
     e.g. prose like "you'll" or "wasn't" in a string or heredoc inside
     the substitution opens a phantom quote region and swallows the
     closing paren;
  2. a heredoc opened inside `$( ... )` — the scanner doesn't know
     heredocs exist and scans their bodies as if they were code.

This script finds `$( ... )` spans with modern quote-aware matching, then
flags spans exhibiting either poison pattern. It's a heuristic (it doesn't
skip shell comments, so don't put these patterns in comments either — a
cheap price for keeping the checker simple), but it exactly caught the
real regressions after the fact; CI runs it so the next one is caught
before merge, with a message that names the line, instead of a bare
`bash -n` exit 2 from the macOS lane.

Usage: python3 tests/check_bash32_compat.py <script> [<script> ...]
Exits 0 if clean, 1 with per-line findings otherwise.
"""

import sys

BACKSLASH = chr(92)


def find_spans(text):
    issues = []
    i = 0
    n = len(text)
    while i < n - 1:
        if text[i] == "$" and text[i + 1] == "(":
            start = i
            depth = 0
            j = i
            in_s = in_d = False
            while j < n:
                c = text[j]
                if in_s:
                    if c == "'":
                        in_s = False
                elif in_d:
                    if c == '"':
                        in_d = False
                    elif c == BACKSLASH:
                        j += 1
                else:
                    if c == "'":
                        in_s = True
                    elif c == '"':
                        in_d = True
                    elif c == BACKSLASH:
                        j += 1
                    elif c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            span = text[start : j + 1]
            line = text[:start].count("\n") + 1
            if span.count("'") % 2 == 1:
                issues.append((line, "odd apostrophe count inside $() span (bash 3.2 poison)", span[:90]))
            if "<<" in span:
                issues.append((line, "heredoc inside $() (bash 3.2 poison)", span[:90]))
            i = j
        i += 1
    return issues


def main():
    if len(sys.argv) < 2:
        print("usage: check_bash32_compat.py <script> [<script> ...]", file=sys.stderr)
        return 1
    ok = True
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for line, kind, snippet in find_spans(text):
            ok = False
            print(f"{path}:{line}: {kind}: {snippet!r}")
    if ok:
        print("clean: no bash 3.2 poison patterns found")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
