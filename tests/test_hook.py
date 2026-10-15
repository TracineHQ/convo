"""`convo hook session-end`, the writer lock, and the hook's scoped index run."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from convo import cli, hook, hook_worker
from convo.cli import main
from convo.db import SCHEMA_VERSION, Database
from convo.hook import child_argv, run_session_end, session_id_from_payload, spawn_detached
from convo.lock import WriterLock
from tests._seed import build_db_at_version
from tests.perf._synth import write_transcript

if TYPE_CHECKING:
    from collections.abc import Iterator

_SID = "5e551011-0000-4000-8000-0000000000aa"
_HOOKS_JSON = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"
# Claude Code's default SessionEnd budget; plugin hook timeouts do not raise it.
_SESSION_END_BUDGET_S = 1.5


def _payload(**overrides: object) -> str:
    body: dict[str, object] = {
        "session_id": _SID,
        "transcript_path": f"/home/u/.claude/projects/-work-x/{_SID}.jsonl",
        "cwd": "/work/x",
        "hook_event_name": "SessionEnd",
        "reason": "exit",
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "convo.db"
    lock = WriterLock(db_path)
    assert lock.acquire(blocking=False)
    try:
        yield db_path
    finally:
        lock.release()


# --- WriterLock --------------------------------------------------------------


def test_writer_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    db_path = tmp_path / "convo.db"
    first, second = WriterLock(db_path), WriterLock(db_path)
    assert first.acquire(blocking=False)
    assert not second.acquire(blocking=False)
    first.release()
    assert second.acquire(blocking=False)
    second.release()
    assert first.path == tmp_path / "convo.db.ingest.lock"


def test_writer_lock_blocking_acquire_waits_for_release(tmp_path: Path) -> None:
    db_path = tmp_path / "convo.db"
    holder = WriterLock(db_path)
    assert holder.acquire(blocking=False)
    threading.Timer(0.3, holder.release).start()
    start = time.monotonic()
    waiter = WriterLock(db_path)
    assert waiter.acquire(blocking=True)
    assert time.monotonic() - start >= 0.2
    waiter.release()


def test_writer_lock_release_is_idempotent(tmp_path: Path) -> None:
    lock = WriterLock(tmp_path / "convo.db")
    lock.release()
    assert lock.acquire(blocking=False)
    lock.release()
    lock.release()


# --- payload parsing ---------------------------------------------------------


def test_session_id_from_payload_prefers_session_id() -> None:
    assert session_id_from_payload(_payload()) == _SID


def test_session_id_from_payload_falls_back_to_transcript_stem() -> None:
    text = json.dumps({"transcript_path": f"/x/projects/slug/{_SID}.jsonl"})
    assert session_id_from_payload(text) == _SID


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"session_id": 7}', "{}"])
def test_session_id_from_payload_rejects_garbage(text: str) -> None:
    assert session_id_from_payload(text) is None


# --- run_session_end ---------------------------------------------------------


class _FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], log_path: Path) -> int:
        self.calls.append((argv, log_path))
        return 4242


def test_hook_spawns_a_scoped_index(tmp_path: Path) -> None:
    db_path = tmp_path / "convo.db"
    spawn = _FakeSpawn()
    outcome = run_session_end(_payload(), db_path=db_path, spawn=spawn)
    assert outcome.status == "spawned"
    assert outcome.session_id == _SID
    assert outcome.pid == 4242
    assert len(spawn.calls) == 1
    argv, log_path = spawn.calls[0]
    assert argv == child_argv(db_path, _SID)
    assert argv[-2:] == [str(db_path), _SID]
    assert log_path == tmp_path / "convo.db.hook.log"
    # The hook itself never opens (and so never creates or migrates) the DB.
    assert not db_path.exists()


def test_hook_hands_the_child_an_absolute_db_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The child runs with cwd = the log's directory, so a relative --db or
    # CONVO_DB would resolve against the wrong place there.
    monkeypatch.chdir(tmp_path)
    spawn = _FakeSpawn()
    run_session_end(_payload(), db_path=Path("sub") / "convo.db", spawn=spawn)
    argv, log_path = spawn.calls[0]
    expected = (tmp_path / "sub" / "convo.db").resolve()
    assert argv[-2] == str(expected)
    assert log_path == expected.with_name("convo.db.hook.log")


def test_hook_spawns_even_while_another_writer_holds_the_lock(held_lock: Path) -> None:
    # A session that ends mid-index must still get indexed: its child queues.
    spawn = _FakeSpawn()
    outcome = run_session_end(_payload(), db_path=held_lock, spawn=spawn)
    assert outcome.status == "spawned"
    assert len(spawn.calls) == 1


@pytest.mark.parametrize("text", ["", "{}", _payload(session_id="../../etc/x")])
def test_hook_skips_without_a_usable_session_id(tmp_path: Path, text: str) -> None:
    spawn = _FakeSpawn()
    outcome = run_session_end(text, db_path=tmp_path / "convo.db", spawn=spawn)
    assert outcome.status == "skipped"
    assert spawn.calls == []


def test_child_argv_runs_this_interpreter_without_cwd_on_path(tmp_path: Path) -> None:
    argv = child_argv(tmp_path / "convo.db", _SID)
    # -X utf8: on Windows a detached run would otherwise write the log in the
    # ANSI code page and fail on non-ASCII paths or transcript text.
    assert argv[:6] == [sys.executable, "-P", "-X", "utf8", "-m", "convo.hook_worker"]


def test_worker_runs_a_plain_scoped_index() -> None:
    # No --no-wait: the run is detached, so it waits for the writer lock for
    # free. No --no-migrate: it migrates outside the hook's budget, under the
    # lock, while readers keep working, so an upgrade never stalls indexing.
    assert hook_worker.index_argv("/x/convo.db", _SID) == [
        "--db",
        "/x/convo.db",
        "index",
        "--session",
        _SID,
    ]


_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (\S+) ")


def test_worker_stamps_every_output_line_with_time_and_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _projects(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    db_path = tmp_path / "convo.db"

    assert hook_worker.main([str(db_path), _SID]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "Inserted: 8 messages" in out
    assert lines
    for line in lines:
        match = _STAMP.match(line)
        assert match is not None, line
        assert match.group(1) == _SID


def test_worker_stamps_stderr_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(_projects(tmp_path)))
    db_path = tmp_path / "convo.db"
    holder = WriterLock(db_path)
    assert holder.acquire(blocking=False)
    threading.Timer(0.3, holder.release).start()

    assert hook_worker.main([str(db_path), _SID]) == 0
    err = capsys.readouterr().err
    assert "waiting for another convo writer" in err
    for line in err.splitlines():
        match = _STAMP.match(line)
        assert match is not None, line
        assert match.group(1) == _SID


@pytest.mark.parametrize("argv", [[], ["only-db"], ["db", "../escape"], ["a", "b", "c"]])
def test_worker_rejects_bad_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert hook_worker.main(argv) == 2
    assert "usage" in capsys.readouterr().err


def test_spawn_detached_redirects_stdio_and_detaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 31337

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen["argv"] = argv
            seen.update(kwargs)
            # Write the way the child does: straight to the descriptor it inherits.
            os.write(kwargs["stdout"], b"this run\n")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    log_path = tmp_path / "logs" / "convo.db.hook.log"
    pid = spawn_detached(["prog", "arg"], log_path)

    assert pid == 31337
    assert seen["argv"] == ["prog", "arg"]
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.STDOUT
    assert seen["close_fds"] is True
    assert log_path.read_bytes() == b"this run\n"
    # A later run appends after it.
    spawn_detached(["prog"], log_path)
    assert log_path.read_bytes() == b"this run\nthis run\n"
    if sys.platform != "win32":
        assert seen["start_new_session"] is True
    assert log_path.exists()


# The real Windows values, so the win32 branch runs (and is checked) on every OS.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def test_spawn_detached_on_windows_hides_the_console_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 1

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            del argv
            seen.update(kwargs)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        _CREATE_NEW_PROCESS_GROUP,
        raising=False,
    )
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS, raising=False)
    # The real Windows log opener needs _winapi; this test is about the flags.
    monkeypatch.setattr(hook, "_open_for_append", lambda path: os.open(path, os.O_WRONLY))
    monkeypatch.setattr(sys, "platform", "win32")
    spawn_detached(["prog"], tmp_path / "convo.db.hook.log")

    # CREATE_NO_WINDOW gives the child a hidden console. DETACHED_PROCESS would
    # override it, and a venv's python.exe launcher would then start the real
    # interpreter in a fresh, visible console window.
    assert seen["creationflags"] == _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in seen


def test_spawn_detached_truncates_an_oversized_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePopen:
        pid = 1

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            del argv, kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    log_path = tmp_path / "convo.db.hook.log"
    log_path.write_bytes(b"x" * (hook.LOG_MAX_BYTES + 1))
    spawn_detached(["prog"], log_path)
    assert log_path.stat().st_size == 0

    log_path.write_bytes(b"keep me\n")
    spawn_detached(["prog"], log_path)
    assert log_path.read_bytes() == b"keep me\n"


# --- CLI ---------------------------------------------------------------------


def test_hook_cli_json_reports_the_spawned_run(
    held_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn = _FakeSpawn()
    real = hook.run_session_end

    def with_fake_spawn(text: str, *, db_path: Path) -> hook.HookOutcome:
        return real(text, db_path=db_path, spawn=spawn)

    monkeypatch.setattr(cli, "run_session_end", with_fake_spawn)
    monkeypatch.setattr(sys, "stdin", _StringStdin(_payload()))
    rc = main(["--db", str(held_lock), "hook", "session-end", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 2,
        "hook": {
            "event": "session-end",
            "status": "spawned",
            "session_id": _SID,
            "pid": 4242,
            "log_path": str(spawn.calls[0][1]),
        },
    }


def test_hook_cli_ignores_a_tty_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", _StringStdin(_payload(), tty=True))
    rc = main(["--db", str(tmp_path / "convo.db"), "hook", "session-end", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["hook"]["status"] == "skipped"


class _StringStdin:
    def __init__(self, text: str, *, tty: bool = False) -> None:
        self._text = text
        self._tty = tty

    def read(self) -> str:
        return self._text

    def isatty(self) -> bool:
        return self._tty


def _plugin_session_end_command() -> str:
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    commands = [h["command"] for entry in config["hooks"]["SessionEnd"] for h in entry["hooks"]]
    assert len(commands) == 1
    command: str = commands[0]
    return command


def test_plugin_session_end_hook_runs_the_hook_command() -> None:
    command = _plugin_session_end_command()
    assert "convo hook session-end" in command
    assert "timeout" not in command


_FAKE_CONVO = """#!/bin/sh
echo "$*" >> "$FAKE_CONVO_CALLS"
if [ "$1" = hook ]; then exit "$FAKE_HOOK_RC"; fi
exit 0
"""


def _run_plugin_hook(tmp_path: Path, *, hook_rc: int | None) -> list[str]:
    """Run the plugin's SessionEnd command under sh with a fake `convo` on PATH.

    `hook_rc` is what the fake returns for `convo hook ...` (2 is argparse's
    exit for an unknown subcommand, i.e. a CLI older than the plugin); None
    leaves `convo` off PATH entirely.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    if hook_rc is not None:
        fake = bin_dir / "convo"
        fake.write_text(_FAKE_CONVO, encoding="utf-8")
        fake.chmod(0o755)
    (tmp_path / ".claude").mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp_path),
        "FAKE_CONVO_CALLS": str(calls),
        "FAKE_HOOK_RC": str(hook_rc),
    }
    proc = subprocess.run(  # noqa: S603 - fixed sh path, command from the repo's hooks.json
        ["/bin/sh", "-c", _plugin_session_end_command()],
        input=_payload(),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


_POSIX_SHELL = pytest.mark.skipif(sys.platform == "win32", reason="runs the hook under /bin/sh")


@_POSIX_SHELL
def test_plugin_hook_uses_only_the_hook_command_on_a_current_cli(tmp_path: Path) -> None:
    assert _run_plugin_hook(tmp_path, hook_rc=0) == ["hook session-end"]


@_POSIX_SHELL
def test_plugin_hook_falls_back_to_index_on_a_cli_without_hook(tmp_path: Path) -> None:
    # The marketplace ships the plugin from main; the CLI is a separate install
    # and can be older. Its argparse exits 2 on `convo hook`.
    assert _run_plugin_hook(tmp_path, hook_rc=2) == ["hook session-end", "index"]
    errors = (tmp_path / ".claude" / "convo-hook-errors.log").read_text(encoding="utf-8")
    assert errors == ""


@_POSIX_SHELL
def test_plugin_hook_does_not_index_in_budget_when_the_hook_command_fails(
    tmp_path: Path,
) -> None:
    assert _run_plugin_hook(tmp_path, hook_rc=1) == ["hook session-end"]


@_POSIX_SHELL
def test_plugin_hook_is_a_no_op_without_convo_on_path(tmp_path: Path) -> None:
    assert _run_plugin_hook(tmp_path, hook_rc=None) == []


# --- index flags the hook relies on ------------------------------------------


def _projects(tmp_path: Path, records: int = 8) -> Path:
    root = tmp_path / "projects"
    write_transcript(root / "slug" / f"{_SID}.jsonl", _SID, start=0, count=records)
    return root


def test_index_no_wait_skips_when_another_writer_holds_the_lock(
    held_lock: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _projects(tmp_path)
    argv = ["--db", str(held_lock), "index", "--projects-dir", str(root), "--no-wait"]
    assert main([*argv, "--json"]) == 0
    body = json.loads(capsys.readouterr().out)["index"]
    assert body["status"] == "busy"
    assert body["files_seen"] == 0
    assert not held_lock.exists()

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "another convo writer" in out
    # Nothing re-queues these files: say so instead of promising a later run will.
    assert "picks these files up" not in out
    assert "run convo index again" in out


def test_index_waits_for_the_writer_lock_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    root = _projects(tmp_path)
    holder = WriterLock(db_path)
    assert holder.acquire(blocking=False)
    threading.Timer(0.5, holder.release).start()

    start = time.monotonic()
    rc = main(["--db", str(db_path), "index", "--projects-dir", str(root), "--json"])
    waited = time.monotonic() - start

    assert rc == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["index"]["files_indexed"] == 1
    assert waited >= 0.4
    assert "waiting for another convo writer" in out.err


def test_index_dry_run_neither_waits_for_the_lock_nor_migrates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    build_db_at_version(db_path, 4).close()
    root = _projects(tmp_path)
    holder = WriterLock(db_path)
    assert holder.acquire(blocking=False)
    # Fails instead of hanging if --dry-run ever waits on the lock again.
    threading.Timer(2.0, holder.release).start()

    argv = ["--db", str(db_path), "index", "--projects-dir", str(root), "--dry-run", "--json"]
    assert main(argv) == 0
    out = capsys.readouterr()
    assert "waiting for another convo writer" not in out.err
    assert json.loads(out.out)["index"]["files_seen"] == 1
    with Database(db_path, migrate=False) as db:
        assert db.schema_version() == 4


def test_index_no_migrate_refuses_a_db_with_pending_migrations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    build_db_at_version(db_path, 4).close()
    root = _projects(tmp_path)
    argv = ["--db", str(db_path), "index", "--projects-dir", str(root), "--no-migrate", "--json"]
    assert main(argv) == 1
    assert "convo migrate" in json.loads(capsys.readouterr().out)["error"]["message"]
    with Database(db_path, migrate=False) as db:
        assert db.schema_version() == 4
        assert db.conn is not None
        assert db.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_index_no_migrate_initializes_a_fresh_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    root = _projects(tmp_path)
    argv = ["--db", str(db_path), "index", "--projects-dir", str(root), "--no-migrate", "--json"]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["index"]["files_indexed"] == 1
    with Database(db_path, migrate=False) as db:
        assert db.schema_version() == SCHEMA_VERSION


# --- end to end --------------------------------------------------------------


def _wait_for(predicate: Any, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_hook_returns_fast_and_indexes_in_the_background(tmp_path: Path) -> None:
    records = 20_000
    root = _projects(tmp_path, records=records)
    db_path = tmp_path / "convo.db"
    log_path = tmp_path / "convo.db.hook.log"
    env = {**os.environ, "CLAUDE_PROJECTS_DIR": str(root)}
    env.pop("CONVO_DB", None)

    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - sys.executable + literal args, no shell
        [sys.executable, "-m", "convo", "--db", str(db_path), "hook", "session-end", "--json"],
        input=_payload(),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)["hook"]
    assert body["status"] == "spawned"
    assert body["log_path"] == str(log_path)
    assert elapsed < _SESSION_END_BUDGET_S, f"hook took {elapsed:.2f}s"
    if sys.platform != "win32":
        # The index run is its own session leader, outside the hook's process group.
        assert os.getsid(body["pid"]) == body["pid"]

    def done() -> bool:
        return log_path.exists() and "Inserted:" in log_path.read_text(encoding="utf-8")

    assert _wait_for(done, timeout=120), log_path.read_text(encoding="utf-8")
    assert f"Inserted: {records} messages" in log_path.read_text(encoding="utf-8")
    # The child exits right after its summary; wait until it lets go of the lock.
    lock = WriterLock(db_path)
    assert _wait_for(lambda: lock.acquire(blocking=False), timeout=30)
    lock.release()


def _run_hook(db_path: Path, session_id: str, env: dict[str, str]) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603 - sys.executable + literal args, no shell
        [sys.executable, "-m", "convo", "--db", str(db_path), "hook", "session-end", "--json"],
        input=_payload(session_id=session_id),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    body: dict[str, Any] = json.loads(proc.stdout)["hook"]
    return body


def test_hook_runs_queue_behind_a_writer_and_each_indexes_its_session(tmp_path: Path) -> None:
    sids = [f"5e551011-0000-4000-8000-{i:012d}" for i in range(1, 4)]
    root = tmp_path / "projects"
    for sid in sids:
        write_transcript(root / "slug" / f"{sid}.jsonl", sid, start=0, count=8)
    db_path = tmp_path / "convo.db"
    log_path = tmp_path / "convo.db.hook.log"
    env = {**os.environ, "CLAUDE_PROJECTS_DIR": str(root)}
    env.pop("CONVO_DB", None)

    def log_count(needle: str) -> int:
        return log_path.read_text(encoding="utf-8").count(needle) if log_path.exists() else 0

    holder = WriterLock(db_path)
    assert holder.acquire(blocking=False)
    try:
        for sid in sids:
            assert _run_hook(db_path, sid, env)["status"] == "spawned"
        # Every child is parked on the lock; none has given up.
        assert _wait_for(lambda: log_count("waiting for another convo writer") == 3, timeout=60)
        assert log_count("Inserted:") == 0
    finally:
        holder.release()

    assert _wait_for(lambda: log_count("Inserted:") == 3, timeout=120), log_path.read_text(
        encoding="utf-8",
    )
    assert _wait_for(lambda: holder.acquire(blocking=False), timeout=30)
    holder.release()
    with Database(db_path, migrate=False) as db:
        assert db.conn is not None
        indexed = {r[0] for r in db.conn.execute("SELECT id FROM sessions").fetchall()}
    assert indexed == set(sids)
    # The runs share one log and interleave in it; every line says whose it is.
    stamps = [_STAMP.match(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert all(stamps)
    assert {m.group(1) for m in stamps if m} == set(sids)


def test_hook_run_migrates_an_old_db_then_indexes(tmp_path: Path) -> None:
    root = _projects(tmp_path)
    db_path = tmp_path / "convo.db"
    build_db_at_version(db_path, 4).close()
    log_path = tmp_path / "convo.db.hook.log"
    env = {**os.environ, "CLAUDE_PROJECTS_DIR": str(root)}
    env.pop("CONVO_DB", None)

    assert _run_hook(db_path, _SID, env)["status"] == "spawned"

    def done() -> bool:
        return log_path.exists() and "Inserted:" in log_path.read_text(encoding="utf-8")

    assert _wait_for(done, timeout=120), log_path.read_text(encoding="utf-8")
    lock = WriterLock(db_path)
    assert _wait_for(lambda: lock.acquire(blocking=False), timeout=30)
    lock.release()
    with Database(db_path, migrate=False) as db:
        assert db.schema_version() == SCHEMA_VERSION
        assert db.conn is not None
        assert [r[0] for r in db.conn.execute("SELECT id FROM sessions").fetchall()] == [_SID]
