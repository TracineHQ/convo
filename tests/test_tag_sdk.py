"""`convo tag-sdk`: entrypoints from transcripts on disk, and the sdk-inferred heuristic."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from convo.intake.tag_sdk import template_key
from tests._seed import build_db_at_version

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_NOW = "2026-04-29T00:00:00.000Z"
_PIPE = "/work/pipe"
_OTHER = "/work/other"
_TEMPLATED = 25


class _Corpus:
    """Builds sessions straight into a fresh DB; transcripts are absent unless written."""

    def __init__(self, conn: sqlite3.Connection, root: Path) -> None:
        self.conn = conn
        self.root = root

    def add(
        self,
        sid: str,
        prompt: str,
        *,
        project: str = _PIPE,
        messages: int = 4,
        entrypoint: str | None = None,
        transcript: str | None = None,
    ) -> None:
        path = self.root / "projects" / f"{sid}.jsonl"
        if transcript is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = {"type": "user", "entrypoint": transcript, "message": {"content": prompt}}
            path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        cur = self.conn.execute(
            "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at) VALUES (?, 1, 1, ?)",
            (str(path), _NOW),
        )
        self.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, entrypoint) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, cur.lastrowid, project, _NOW, entrypoint),
        )
        for i in range(messages):
            role = "assistant" if i % 2 else "user"
            content = prompt if i == 0 else f"turn {i}"
            self.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (f"{sid}-m{i}", sid, role, i, _NOW, content),
            )


@pytest.fixture(autouse=True)
def backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "backups"
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(target))
    return target


@pytest.fixture
def corpus_db(db_path: Path, tmp_path: Path) -> Path:
    with Database(db_path) as db:
        assert db.conn is not None
        c = _Corpus(db.conn, tmp_path)
        for i in range(_TEMPLATED):
            c.add(f"url-{i:02d}", f"URL: https://example.com/page/{i}\nExtract the title.")
        # Same template, too long to be a pipeline job.
        c.add("url-long", "URL: https://example.com/page/long", messages=15)
        # Same template, transcript still on disk: read, not inferred.
        c.add("url-on-disk", "URL: https://example.com/page/disk", transcript="sdk-py")
        # A human-looking prompt repeated too few times.
        for i in range(3):
            c.add(f"fix-{i}", f"Fix the login bug in module {i}")
        # A template a known interactive session also uses.
        for i in range(_TEMPLATED):
            c.add(f"sum-{i:02d}", f"Summarize: report number {i}")
        c.add("sum-cli", "Summarize: report on my week", entrypoint="cli")
        # Slash-command wrappers only interactive use writes.
        for i in range(_TEMPLATED):
            c.add(f"cmd-{i:02d}", f"<command-message>clear</command-message> run {i}")
        # The same pipeline in another project is out of scope.
        for i in range(_TEMPLATED):
            c.add(f"other-{i:02d}", f"URL: https://example.com/page/{i}", project=_OTHER)
        db.conn.commit()
    return db_path


def _entrypoints(db_path: Path) -> dict[str, str | None]:
    with Database(db_path, migrate=False) as db:
        assert db.conn is not None
        return {str(r[0]): r[1] for r in db.conn.execute("SELECT id, entrypoint FROM sessions")}


def _counts(db_path: Path) -> list[int]:
    with Database(db_path, migrate=False) as db:
        assert db.conn is not None
        return [
            db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("source_files", "sessions", "messages")
        ]


def _tag(db_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, object]:
    assert main(["--db", str(db_path), "tag-sdk", "--json", *argv]) == 0
    body: dict[str, object] = json.loads(capsys.readouterr().out)["tag_sdk"]
    return body


@pytest.mark.parametrize(
    ("prompt", "key"),
    [
        ("URL: https://a.example/x", "URL: https:/"),
        ("  URL:\n  https://a.example/x  ", "URL: https:/"),
        ("short", None),
        ("exactly12chr", None),
        ("<command-message>clear</command-message>", None),
        ("<local-command-stdout>ok</local-command-stdout>", None),
        (None, None),
    ],
)
def test_template_key(prompt: str | None, key: str | None) -> None:
    assert template_key(prompt, 12) == key


def test_dry_run_reports_without_writing(
    corpus_db: Path, backup_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _entrypoints(corpus_db)
    body = _tag(corpus_db, capsys, "--project", "pipe", "--dry-run")

    assert body["status"] == "dry_run"
    assert body["project"] == _PIPE
    assert body["criteria"] == {"max_messages": 10, "min_shared": 20, "prefix_chars": 12}
    from_files = body["from_files"]
    assert isinstance(from_files, dict)
    assert from_files["count"] == 1
    assert from_files["by_entrypoint"] == {"sdk-py": 1}
    assert from_files["sample_ids"] == ["url-on-disk"]
    inferred = body["inferred"]
    assert isinstance(inferred, dict)
    assert inferred["ran"] is True
    assert inferred["count"] == _TEMPLATED
    assert [t["prefix"] for t in inferred["templates"]] == ["URL: https:/"]
    assert inferred["sample_ids"] == [f"url-{i:02d}" for i in range(5)]
    assert [t["prefix"] for t in inferred["skipped_cli_templates"]] == ["Summarize: r"]
    assert body["updated"] == 0
    assert body["backup_path"] is None

    assert _entrypoints(corpus_db) == before
    assert not backup_dir.exists()


def test_dry_run_prose_lists_counts_and_samples(
    corpus_db: Path, backup_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--db", str(corpus_db), "tag-sdk", "--project", _PIPE, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "'URL: https:/'  25 sessions" in out
    assert "sample: url-00, url-01, url-02, url-03, url-04" in out
    assert "skipped 'Summarize: r' (25 sessions): a cli session uses it" in out
    assert "would tag 25 sessions sdk-inferred" in out
    assert "Dry run: nothing was written." in out
    assert not backup_dir.exists()


def test_tag_writes_snapshot_then_tags(
    corpus_db: Path, backup_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = _counts(corpus_db)
    body = _tag(corpus_db, capsys, "--project", _PIPE)

    assert body["status"] == "tagged"
    assert body["updated"] == _TEMPLATED + 1
    snapshots = list(backup_dir.glob("convo-*.db"))
    assert [str(p) for p in snapshots] == [body["backup_path"]]
    got = _entrypoints(corpus_db)
    assert {sid for sid, ep in got.items() if ep == "sdk-inferred"} == {
        f"url-{i:02d}" for i in range(_TEMPLATED)
    }
    assert got["url-on-disk"] == "sdk-py"
    assert got["url-long"] is None
    assert got["sum-cli"] == "cli"
    assert all(got[sid] is None for sid in got if sid.startswith(("fix-", "sum-0", "sum-1")))
    assert all(got[sid] is None for sid in got if sid.startswith(("cmd-", "other-")))
    # Nothing deleted; the snapshot holds the pre-tag state.
    assert _counts(corpus_db) == counts
    assert set(_entrypoints(snapshots[0]).values()) == {None, "cli"}


def test_tagged_sessions_drop_out_of_default_stats(
    corpus_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tag(corpus_db, capsys, "--project", _PIPE)
    argv = ["--db", str(corpus_db), "stats", "sessions", "--project", _PIPE, "--json"]
    assert main(argv) == 0
    default = json.loads(capsys.readouterr().out)["stats"]["total"]
    assert main([*argv, "--include-sdk"]) == 0
    everything = json.loads(capsys.readouterr().out)["stats"]["total"]
    assert everything - default == _TEMPLATED + 1


def test_second_run_has_nothing_to_tag(
    corpus_db: Path, backup_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tag(corpus_db, capsys, "--project", _PIPE)
    body = _tag(corpus_db, capsys, "--project", _PIPE)
    assert body["status"] == "nothing_to_tag"
    assert body["backup_path"] is None
    assert len(list(backup_dir.glob("convo-*.db"))) == 1


def test_without_project_only_reads_transcripts(
    corpus_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _tag(corpus_db, capsys)
    inferred = body["inferred"]
    assert isinstance(inferred, dict)
    assert inferred["ran"] is False
    assert inferred["count"] == 0
    got = _entrypoints(corpus_db)
    assert got["url-on-disk"] == "sdk-py"
    assert "sdk-inferred" not in got.values()


@pytest.mark.parametrize(
    "flags",
    [["--min-shared", "26"], ["--max-messages", "3"], ["--prefix-chars", "60"]],
    ids=["min-shared", "max-messages", "prefix-chars"],
)
def test_thresholds_are_flags(
    corpus_db: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    body = _tag(corpus_db, capsys, "--project", _PIPE, "--dry-run", *flags)
    inferred = body["inferred"]
    assert isinstance(inferred, dict)
    assert inferred["count"] == 0


def test_lower_min_shared_reaches_smaller_templates(
    corpus_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _tag(corpus_db, capsys, "--project", _PIPE, "--dry-run", "--min-shared", "3")
    inferred = body["inferred"]
    assert isinstance(inferred, dict)
    assert {t["prefix"] for t in inferred["templates"]} == {"URL: https:/", "Fix the logi"}


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_threshold_flags_reject_non_positive(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["tag-sdk", "--min-shared", value])
    assert exc.value.code == 2


def test_transcript_on_disk_replaces_a_heuristic_tag(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        _Corpus(db.conn, tmp_path).add("s", "anything", entrypoint="sdk-inferred", transcript="cli")
        db.conn.commit()
    body = _tag(db_path, capsys)
    assert body["status"] == "tagged"
    assert _entrypoints(db_path) == {"s": "cli"}


def test_needs_the_entrypoint_schema(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_db_at_version(db_path, 6).close()
    assert main(["--db", str(db_path), "tag-sdk", "--dry-run", "--json"]) == 1
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "convo migrate" in message
    with Database(db_path, migrate=False) as db:
        assert db.schema_version() == 6
