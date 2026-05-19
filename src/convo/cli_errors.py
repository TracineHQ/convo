"""Rejection logic for grep-style and invented flags.

argparse's default unknown-arg behavior is too terse and doesn't help
agents discover the right convo flag. We pre-scan argv and emit a helpful
error before argparse runs.
"""

from __future__ import annotations

import sys

_GREP_FLAG_HINTS: dict[str, str] = {
    "-C": "use --excerpt-chars to widen the snippet window",
    "-A": "use `convo inspect --timeline` for context after the match",
    "-B": "use `convo inspect --timeline` for context before the match",
    "-E": "regex is not supported; FTS5 trigram handles substrings",
    "-i": "case-sensitivity is not configurable; FTS5 trigram is case-insensitive by default",
}


_INVENTED_FLAG_HINTS: dict[str, str] = {
    "--quiet": "did you mean to suppress the footer? convo always emits one. drop --quiet.",
    "--no-color": "convo does not emit ANSI by default in prose mode; drop --no-color.",
    "--no-index": "did you mean --no-stream? indexing is automatic.",
    "--by-caller": (
        "this is a `convo stats` feature, not `convo search`. "
        "try `convo stats commands --by-caller`."
    ),
    "--caller": "this is a `convo stats` feature. try `convo stats commands`.",
    "--last": "did you mean --since? use --since 24h for the last day.",
    "--has-newlines": (
        "this filter is not available; use the JSON `excerpt` field and check for `\\n`."
    ),
    "--tree": (
        "this is not yet a feature. `convo inspect --timeline` is the closest existing view."
    ),
    "--output": "use --format=json or --format=prose.",
}


def precheck_argv(argv: list[str]) -> None:
    """Reject grep-style and invented flags with a helpful suggestion.

    Called before argparse parses. Exits the process with code 2 on a match.
    """
    for arg in argv:
        for short, hint in _GREP_FLAG_HINTS.items():
            if arg == short or (arg.startswith(short) and arg[len(short) :].isdigit()):
                sys.stderr.write(f"convo: error: unknown flag {arg!r} -- {hint}\n")
                sys.exit(2)
        if arg in _INVENTED_FLAG_HINTS:
            sys.stderr.write(f"convo: error: unknown flag {arg!r} -- {_INVENTED_FLAG_HINTS[arg]}\n")
            sys.exit(2)
