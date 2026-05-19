"""Deterministic empty-result suggestions.

Generated from the failure pattern (the agent's existing filters + query),
no LLM involvement. Suggestions are stable across runs so agents learn
predictable retry patterns.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_TOKENS_FOR_RARE_DROP = 3
_WIDEN_TABLE: dict[str, str] = {
    "1d": "7d",
    "7d": "30d",
    "30d": "90d",
    "90d": "1y",
    "1y": "5y",
}


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One actionable suggestion for an empty-result query.

    `kind` is one of: ``split_hyphens``, ``widen_since``, ``drop_project``,
    ``drop_tool``, ``drop_session``, ``drop_rare_token``.

    The optional fields are populated only for the kind they apply to.
    """

    kind: str
    description: str
    from_value: str | None = None
    to_value: str | None = None
    drop_token: str | None = None


def generate_suggestions(  # noqa: PLR0913
    *,
    query: str,
    since_span: str | None,
    project: str | None,
    tool: str | None,
    session: str | None,
    total_doc_freq: dict[str, int],
) -> list[Suggestion]:
    """Produce ordered suggestions for an empty-result query.

    ``total_doc_freq`` maps each query token to its FTS5 doc-frequency.
    A frequency of 0 means the token never appears anywhere in the index.

    The caller fills ``total_doc_freq`` from a separate FTS5 query before
    calling this function. Two-step rather than one-step so this module
    stays free of SQLite imports.
    """
    out: list[Suggestion] = []

    if "-" in query:
        no_hyphen = query.replace("-", " ")
        out.append(
            Suggestion(
                kind="split_hyphens",
                description=f"hyphens are tokenized literally; try {no_hyphen!r}",
                from_value=query,
                to_value=no_hyphen,
            )
        )

    if since_span is not None and since_span in _WIDEN_TABLE:
        wider = _WIDEN_TABLE[since_span]
        out.append(
            Suggestion(
                kind="widen_since",
                description=f"widen --since from {since_span} to {wider}",
                from_value=since_span,
                to_value=wider,
            )
        )

    if project is not None:
        out.append(
            Suggestion(
                kind="drop_project",
                description=f"try without --project {project}",
                from_value=project,
            )
        )

    if tool is not None:
        out.append(
            Suggestion(
                kind="drop_tool",
                description=f"try without --tool {tool}",
                from_value=tool,
            )
        )

    if session is not None:
        out.append(
            Suggestion(
                kind="drop_session",
                description=f"try without --session {session}",
                from_value=session,
            )
        )

    tokens = [t.strip() for t in query.split() if t.strip()]
    if len(tokens) >= _MIN_TOKENS_FOR_RARE_DROP and total_doc_freq:
        with_freq = [(t, total_doc_freq.get(t, 0)) for t in tokens]
        with_freq.sort(key=lambda pair: pair[1])
        rarest = with_freq[0][0]
        remaining = " ".join(t for t in tokens if t != rarest)
        out.append(
            Suggestion(
                kind="drop_rare_token",
                description=f"drop rare token {rarest!r}; try {remaining!r}",
                drop_token=rarest,
                to_value=remaining,
            )
        )

    return out
