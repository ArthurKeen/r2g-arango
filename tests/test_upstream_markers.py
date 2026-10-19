"""A workaround for a library we own names its upstream issue
(`.cursor/rules/fix-upstream-dont-fork.mdc`).

A gap in an owned library is fixed or filed in that library. When this repo
cannot wait for the release, the workaround is allowed only while it is marked
where it lives::

    # upstream: <owner>/<repo>#<n> — remove when <condition>

so the pin bump that ships the fix can find it. This holds the marker's shape in
every scanned file: an issue a person can open, and a condition someone can
check. Whether that issue is still open needs the network, so it is not checked
here.

Shared source: arango-shared-memory ``templates/tests/test_upstream_markers.py``.
Copy changes back there so every repository keeps the same check.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / ".cursor" / "rules" / "fix-upstream-dont-fork.mdc"

#: Files whose comments start with ``#``.
SCANNED_SUFFIXES = frozenset({".py", ".sh", ".yml", ".yaml", ".toml", ".cfg"})
SCANNED_NAMES = frozenset({"Makefile", "Dockerfile"})
#: Directories that hold no code of ours. Hidden directories are skipped too,
#: except ``.github``, where CI steps can work around a library.
SKIPPED_DIRS = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)
KEPT_HIDDEN_DIRS = frozenset({".github"})

#: Any comment that claims to be a marker, however it is written.
MARKER = re.compile(r"#\s*upstream\s*:(?P<rest>.*)$", re.IGNORECASE)
#: What a marker must say: an issue (owner/repo#n), then when to remove it.
WELL_FORMED = re.compile(
    r"^\s*(?P<repo>[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)#(?P<issue>[1-9]\d*)"
    r"\s+(?:—|–|--?)\s+remove when\s+(?P<when>\S.*?)\s*$"
)


def marker_problems(text: str, where: str) -> list[str]:
    """Each malformed ``upstream:`` marker in ``text``, as ``where:line: why``."""
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        found = MARKER.search(line)
        if found is None:
            continue
        if WELL_FORMED.match(found.group("rest")) is None:
            problems.append(
                f"{where}:{number}: {line.strip()!r} — write it as "
                "'# upstream: <owner>/<repo>#<n> — remove when <condition>'"
            )
    return problems


def _skipped_dir(name: str) -> bool:
    if name in SKIPPED_DIRS or name.endswith(".egg-info"):
        return True
    return name.startswith(".") and name not in KEPT_HIDDEN_DIRS


def _skipped(relative: Path) -> bool:
    return any(_skipped_dir(part) for part in relative.parts[:-1])


def _scanned_files() -> list[Path]:
    files = []
    # os.walk with pruning, so .git, virtualenvs and node_modules are never entered.
    for current, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(name for name in dirs if not _skipped_dir(name))
        for name in sorted(names):
            path = Path(current) / name
            if path.suffix in SCANNED_SUFFIXES or name in SCANNED_NAMES:
                files.append(path)
    # This file states malformed markers on purpose, to prove they are caught.
    return [path for path in files if path.resolve() != Path(__file__).resolve()]


# -- the tree --------------------------------------------------------------------


def test_every_upstream_marker_names_an_issue_and_when_to_remove_it() -> None:
    problems = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        problems.extend(marker_problems(text, str(path.relative_to(ROOT))))
    assert not problems, "malformed upstream markers:\n  " + "\n  ".join(problems)


def test_the_scan_reaches_this_repositorys_code() -> None:
    """A scan that silently finds nothing would pass forever."""
    scanned = {path.relative_to(ROOT).parts[0] for path in _scanned_files()}
    assert "tests" in scanned
    assert len(_scanned_files()) > 1


@pytest.mark.parametrize(
    ("relative", "skipped"),
    [
        ("src/pkg/module.py", False),
        (".github/workflows/ci.yml", False),
        (".venv/lib/python3.11/site-packages/x.py", True),
        ("pkg.egg-info/PKG-INFO.py", True),
        ("frontend/node_modules/x/setup.py", True),
        (".claude/hooks/x.py", True),
    ],
)
def test_the_scan_skips_only_what_is_not_ours(relative: str, skipped: bool) -> None:
    assert _skipped(Path(relative)) is skipped


# -- the check itself ------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "# upstream: ArthurKeen/arango-sparql-py#123 — remove when the engine takes a validator",
        "x = build()  # upstream: arango-solutions-org/r2g-arango#7 -- remove when 0.7 ships",
        "    # upstream: org/arango-query-core#42 – remove when LabelIndex accepts aliases",
        "  run: make gate  # upstream: org/arango-cypher#9 - remove when the pin moves",
    ],
)
def test_a_well_formed_marker_passes(line: str) -> None:
    assert marker_problems(line, "example") == []


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        ("# upstream: TODO file an issue", "no issue at all"),
        ("# upstream: arango-sparql-py#12 — remove when fixed", "no owner"),
        ("# upstream: org/arango-sparql-py — remove when fixed", "no issue number"),
        ("# upstream: org/arango-sparql-py#0 — remove when fixed", "issue zero"),
        ("# upstream: org/arango-sparql-py#12", "no removal condition"),
        ("# upstream: org/arango-sparql-py#12 — remove when ", "an empty condition"),
        ("# Upstream: org/arango-sparql-py#12 remove when fixed", "no separator"),
        ("#upstream:https://github.com/org/arango-sparql-py/issues/12", "a URL, not owner/repo#n"),
    ],
)
def test_a_malformed_marker_is_named_with_where_it_is(line: str, missing: str) -> None:
    problems = marker_problems(f"first line\n{line}\n", "src/example.py")
    assert len(problems) == 1, f"{missing}: {problems}"
    assert problems[0].startswith("src/example.py:2: ")


def test_a_line_that_only_mentions_upstream_is_not_a_marker() -> None:
    text = "# fixed upstream in 0.6.1\n# the upstream repository leads\nupstream = 'x'\n"
    assert marker_problems(text, "example") == []


# -- the rule says what this test holds ------------------------------------------


def test_the_rule_names_this_test_and_the_marker_format() -> None:
    rule = RULE.read_text(encoding="utf-8")
    assert "tests/test_upstream_markers.py" in rule
    assert "# upstream: <org>/" in rule and "remove when" in rule
