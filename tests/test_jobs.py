"""Tests for the job store."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import jobs

# Captured before the autouse fixture stubs it out, so the tests that exercise
# the identity check itself can put the real implementation back.
_REAL_IS_OUR_WORKER = jobs._is_our_worker


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    # Point the live-reload at an empty file first. Config._get re-reads .env on
    # every access with override=True, so a developer who uncommented the
    # CODEX_STATE_DIR line .env.example ships would have it win over this
    # setenv — and test_prune_deletes_every_sidecar_file would then delete every
    # terminal job in their real store.
    monkeypatch.setattr(config, "_ENV_FILE", tmp_path / "empty.env")
    monkeypatch.setattr(config, "_overridden", {})
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "state"))
    # Tests stand in for a live worker using their own pid, which is not
    # actually running worker.py. Treat this process as a legitimate worker so
    # the identity check doesn't reconcile every simulated job away.
    monkeypatch.setattr(
        jobs, "_is_our_worker", lambda pid, job_id="", token="": pid == os.getpid()
    )
    yield


def _make(job_class="delegate", **request):
    base = {"project_dir": "/tmp", "model": "gpt-5.6-terra", "effort": "xhigh"}
    base.update(request)
    return jobs.create_job(job_class, base)


class TestJobIds:
    def test_ids_are_unique(self):
        ids = {jobs.new_job_id() for _ in range(200)}
        assert len(ids) == 200

    def test_id_carries_prefix(self):
        assert jobs.new_job_id("review").startswith("review-")

    def test_ids_sort_in_creation_order(self):
        first = jobs.new_job_id()
        time.sleep(0.002)
        second = jobs.new_job_id()
        # Time-ordered base36 stamp keeps ids sortable.
        assert first.split("-")[1] <= second.split("-")[1]


class TestCreateAndRead:
    def test_round_trip(self):
        record = _make(task="do the thing")
        loaded = jobs.read_job(record["id"])
        assert loaded["id"] == record["id"]
        assert loaded["status"] == "queued"
        assert loaded["request"]["task"] == "do the thing"

    def test_missing_job_returns_none(self):
        assert jobs.read_job("task-nope-000000") is None

    def test_update_persists(self):
        record = _make()
        jobs.update_job(record["id"], status="running", phase="editing")
        loaded = jobs.read_job(record["id"], reconcile_state=False)
        assert loaded["status"] == "running"
        assert loaded["phase"] == "editing"

    def test_corrupt_record_returns_none_not_raises(self):
        record = _make()
        jobs.job_path(record["id"]).write_text("{not json")
        assert jobs.read_job(record["id"]) is None


class TestResolveJobId:
    def test_exact_id(self):
        record = _make()
        assert jobs.resolve_job_id(record["id"]) == record["id"]

    def test_unique_prefix(self):
        record = _make()
        prefix = record["id"][:14]
        assert jobs.resolve_job_id(prefix) == record["id"]

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="No job matching"):
            jobs.resolve_job_id("task-zzzz")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="No job id given"):
            jobs.resolve_job_id("")

    def test_ambiguous_prefix_raises_rather_than_guessing(self):
        _make()
        _make()
        with pytest.raises(ValueError, match="ambiguous"):
            jobs.resolve_job_id("delegate-")


class TestReconcile:
    def test_running_job_with_dead_worker_becomes_failed(self):
        record = _make()
        # PID 999999 is not a live process.
        jobs.update_job(record["id"], status="running", worker_pid=999999)
        loaded = jobs.read_job(record["id"])
        assert loaded["status"] == "failed"
        assert "without recording a result" in loaded["error"]

    def test_cancelled_flag_reconciles_to_cancelled(self):
        record = _make()
        jobs.update_job(
            record["id"], status="running", worker_pid=999999, cancel_requested=True
        )
        assert jobs.read_job(record["id"])["status"] == "cancelled"

    def test_cancel_survives_a_concurrent_record_write(self):
        # The worker rewrites the record on every phase change. If cancellation
        # lived only in the record, that write would clobber it and the job
        # would settle as "failed" instead of "cancelled".
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=999999)
        jobs.request_cancel(record["id"])
        # Simulate the worker's read-modify-write landing after the cancel,
        # carrying a stale (False) copy of the field.
        jobs.update_job(record["id"], phase="editing", cancel_requested=False)
        assert jobs.read_job(record["id"])["status"] == "cancelled"

    def test_is_cancel_requested_defaults_false(self):
        record = _make()
        assert jobs.is_cancel_requested(record["id"]) is False

    def test_running_job_with_live_worker_is_untouched(self):
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=os.getpid())
        assert jobs.read_job(record["id"])["status"] == "running"

    def test_queued_job_without_worker_is_untouched(self):
        record = _make()
        assert jobs.read_job(record["id"])["status"] == "queued"

    def test_terminal_job_is_untouched(self):
        record = _make()
        jobs.update_job(record["id"], status="completed", worker_pid=None)
        assert jobs.read_job(record["id"])["status"] == "completed"

    def test_reconcile_is_persisted(self):
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=999999)
        jobs.read_job(record["id"])
        # Second read sees the settled state on disk, not a re-derivation.
        assert jobs.read_job(record["id"], reconcile_state=False)["status"] == "failed"


class TestJobIdValidation:
    def test_generated_ids_are_valid(self):
        assert jobs.is_valid_job_id(jobs.new_job_id("delegate"))

    @pytest.mark.parametrize("bad", [
        "../../etc/passwd",
        "/etc/passwd",
        "foo/bar",
        "task-abc-../x",
        "",
    ])
    def test_path_traversal_references_are_rejected(self, bad):
        # job_path() must never build a path out of raw caller input.
        assert not jobs.is_valid_job_id(bad)
        with pytest.raises(ValueError):
            jobs.job_path(bad)

    def test_traversal_reference_resolves_to_nothing(self, tmp_path):
        (tmp_path / "secret.json").write_text('{"id": "secret"}')
        with pytest.raises(ValueError, match="No job matching"):
            jobs.resolve_job_id("../secret")


class TestWorkerIdentity:
    @pytest.fixture(autouse=True)
    def real_identity_check(self, monkeypatch):
        # This class tests _is_our_worker itself, so undo the module-wide stub.
        # Without this, `_is_our_worker(1, ...) is False` would pass simply
        # because 1 != os.getpid() — asserting nothing about the real logic.
        monkeypatch.setattr(jobs, "_is_our_worker", _REAL_IS_OUR_WORKER)
        yield

    def test_terminate_tree_refuses_an_unconfirmed_pid(self, monkeypatch):
        # PID reuse: signalling a pid we cannot confirm is ours would kill an
        # unrelated process group.
        monkeypatch.setattr(
            jobs, "_is_our_worker", lambda pid, job_id="", token="": False
        )
        killed = []
        monkeypatch.setattr(jobs.os, "killpg", lambda *a: killed.append(a))
        jobs.terminate_tree({"id": "x", "worker_pid": 4242})
        assert killed == []

    def test_worker_alive_false_when_identity_unconfirmed(self, monkeypatch):
        monkeypatch.setattr(
            jobs, "_is_our_worker", lambda pid, job_id="", token="": False
        )
        assert jobs._worker_alive({"id": "x", "worker_pid": os.getpid()}) is False

    def test_identity_requires_worker_py_in_command(self, monkeypatch):
        monkeypatch.setattr(
            jobs, "_process_identity", lambda pid: ("Wed Jul 15 10:00:00 2026", "vim")
        )
        assert jobs._is_our_worker(1, "task-a-000000") is False

    def test_identity_requires_the_matching_job_id(self, monkeypatch):
        # A recycled pid running *another job's* worker must not match: the
        # command-line check alone would pass here.
        monkeypatch.setattr(jobs, "_is_our_worker", _REAL_IS_OUR_WORKER)
        monkeypatch.setattr(
            jobs, "_process_identity",
            lambda pid: ("Wed Jul 15 10:00:00 2026", "python worker.py task-b-111111"),
        )
        assert jobs._is_our_worker(1, "task-a-000000") is False
        assert jobs._is_our_worker(1, "task-b-111111") is True

    def test_identity_requires_the_matching_start_time(self, monkeypatch):
        # Same pid, same argv, but restarted: a different start time proves it
        # is a different process.
        monkeypatch.setattr(jobs, "_is_our_worker", _REAL_IS_OUR_WORKER)
        monkeypatch.setattr(
            jobs, "_process_identity",
            lambda pid: ("Wed Jul 15 10:00:00 2026", "python worker.py task-a-000000"),
        )
        assert jobs._is_our_worker(1, "task-a-000000", "Wed Jul 15 09:00:00 2026") is False
        assert jobs._is_our_worker(1, "task-a-000000", "Wed Jul 15 10:00:00 2026") is True

    def test_identity_fails_closed_when_ps_unreadable(self, monkeypatch):
        monkeypatch.setattr(jobs, "_process_identity", lambda pid: None)
        assert jobs._is_our_worker(1, "task-a-000000") is False

    def test_start_token_of_this_process_is_readable(self):
        # Guards the ps -o lstart= parsing against a format surprise.
        token = jobs.process_start_token(os.getpid())
        assert token and len(token) > 10


class TestOrphanReaping:
    def test_reaps_codex_left_by_a_dead_worker(self, monkeypatch):
        # A SIGKILLed worker cannot stop codex; nothing else ever would.
        killed = []
        monkeypatch.setattr(jobs, "_is_our_codex", lambda pid, job_id: True)
        monkeypatch.setattr(jobs.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: killed.append(pgid))
        assert jobs.reap_orphan_codex({"id": "task-a-000000", "codex_pid": 5150})
        assert killed == [5150]

    def test_does_not_reap_an_unrelated_codex(self, monkeypatch):
        # The user's own codex session must never be killed.
        killed = []
        monkeypatch.setattr(
            jobs, "_process_identity",
            lambda pid: ("Wed Jul 15 10:00:00 2026", "codex exec -o /x/other-job.out"),
        )
        monkeypatch.setattr(jobs.os, "killpg", lambda *a: killed.append(a))
        assert not jobs.reap_orphan_codex({"id": "task-a-000000", "codex_pid": 5150})
        assert killed == []

    def test_no_codex_pid_is_a_no_op(self):
        assert not jobs.reap_orphan_codex({"id": "task-a-000000"})

    def test_reconcile_reaps_and_reports(self, monkeypatch):
        reaped = []
        monkeypatch.setattr(
            jobs, "reap_orphan_codex", lambda r: bool(reaped.append(r["id"])) or True
        )
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=999999, codex_pid=1)
        loaded = jobs.read_job(record["id"])
        assert reaped == [record["id"]]
        assert "orphaned codex process was terminated" in loaded["error"]


class TestRepoLock:
    def _active(self, project_dir, write):
        record = jobs.create_job(
            "delegate",
            {"project_dir": project_dir, "write": write, "model": "m", "effort": "e"},
        )
        jobs.update_job(record["id"], status="running", worker_pid=os.getpid())
        return record

    def test_writer_blocks_writer(self, tmp_path):
        self._active(str(tmp_path), write=True)
        assert jobs.find_conflict(str(tmp_path), write=True) is not None

    def test_writer_blocks_reader(self, tmp_path):
        self._active(str(tmp_path), write=True)
        assert jobs.find_conflict(str(tmp_path), write=False) is not None

    def test_reader_blocks_writer(self, tmp_path):
        self._active(str(tmp_path), write=False)
        assert jobs.find_conflict(str(tmp_path), write=True) is not None

    def test_readers_may_share_a_tree(self, tmp_path):
        # Readers change nothing, so there is nothing to corrupt or misattribute.
        self._active(str(tmp_path), write=False)
        assert jobs.find_conflict(str(tmp_path), write=False) is None

    def test_different_repos_never_conflict(self, tmp_path):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        self._active(str(one), write=True)
        assert jobs.find_conflict(str(two), write=True) is None

    def test_subdirectory_of_a_busy_tree_conflicts(self, tmp_path):
        # Regression: comparing project_dir strings let /repo and /repo/src
        # both claim one working tree, so two writers started against it and
        # interleaved their edits — with no repo_busy shown and no race needed.
        sub = tmp_path / "src"
        sub.mkdir()
        self._active(str(tmp_path), write=True)
        conflict = jobs.find_conflict(str(sub), write=True)
        assert conflict is not None

    def test_terminal_job_releases_the_lock(self, tmp_path):
        record = self._active(str(tmp_path), write=True)
        jobs.update_job(record["id"], status="completed", worker_pid=None)
        assert jobs.find_conflict(str(tmp_path), write=True) is None

    def test_dead_worker_releases_the_lock(self, tmp_path):
        # Reconciliation runs inside find_conflict's list_jobs(), so a crashed
        # job cannot hold a tree hostage.
        record = self._active(str(tmp_path), write=True)
        jobs.update_job(record["id"], worker_pid=999999)
        assert jobs.find_conflict(str(tmp_path), write=True) is None

    def test_paths_are_compared_canonically(self, tmp_path):
        self._active(str(tmp_path), write=True)
        messy = str(tmp_path) + "/./"
        assert jobs.find_conflict(messy, write=True) is not None


class TestLaunchFailure:
    def test_queued_job_past_grace_is_failed(self, monkeypatch):
        # Reconcile skips pid-less queued jobs and prune spares active ones, so
        # a job whose worker never launched would otherwise linger forever.
        record = _make()
        jobs.update_job(
            record["id"], created_at=time.time() - jobs._LAUNCH_GRACE_SECONDS - 1
        )
        loaded = jobs.read_job(record["id"])
        assert loaded["status"] == "failed"
        assert "never started" in loaded["error"]

    def test_queued_job_within_grace_is_left_alone(self):
        record = _make()
        assert jobs.read_job(record["id"])["status"] == "queued"

    def test_cancelled_queued_job_becomes_cancelled_not_failed(self):
        record = _make()
        jobs.request_cancel(record["id"])
        assert jobs.read_job(record["id"])["status"] == "cancelled"


class TestListAndPrune:
    def test_list_is_newest_first(self):
        first = _make()
        time.sleep(0.01)
        second = _make()
        listed = [r["id"] for r in jobs.list_jobs()]
        assert listed.index(second["id"]) < listed.index(first["id"])

    def test_prune_removes_oldest_terminal_jobs(self):
        made = []
        for _ in range(6):
            record = _make()
            jobs.update_job(record["id"], status="completed")
            made.append(record["id"])
            time.sleep(0.002)
        removed = jobs.prune(max_jobs=3)
        assert removed == 3
        assert len(jobs.list_jobs()) == 3

    def test_prune_spares_active_jobs(self):
        for _ in range(5):
            record = _make()
            jobs.update_job(record["id"], status="running", worker_pid=os.getpid())
            time.sleep(0.002)
        jobs.prune(max_jobs=1)
        # Running jobs must never be pruned out from under their worker.
        assert len(jobs.list_jobs()) == 5

    def test_prune_deletes_every_sidecar_file(self):
        # The prompt file can carry proprietary context, and any artifact not
        # listed here outlives CODEX_MAX_JOBS forever.
        record = _make()
        jobs.update_job(record["id"], status="completed")
        # Everything except the record itself, which must stay valid JSON for
        # prune() to enumerate it.
        for path in jobs.sidecar_paths(record["id"]):
            if path != jobs.job_path(record["id"]):
                path.write_text("x")
        jobs.prune(max_jobs=0)
        for path in jobs.sidecar_paths(record["id"]):
            assert not path.exists(), f"{path.name} survived pruning"

    def test_sidecars_cover_what_the_worker_writes(self):
        # Guard against a new artifact being added without pruning it.
        job_id = "delegate-abc123-0f0f0f"
        names = {p.name for p in jobs.sidecar_paths(job_id)}
        for suffix in (".json", ".out", ".log", ".prompt.txt", ".stderr.txt",
                       ".schema.json", ".cancel"):
            assert f"{job_id}{suffix}" in names


class TestLogs:
    def test_append_and_tail(self):
        record = _make()
        for i in range(10):
            jobs.append_log(record["id"], f"line {i}")
        tail = jobs.read_log(record["id"], tail_lines=3)
        assert len(tail) == 3
        assert "line 9" in tail[-1]

    def test_missing_log_is_empty(self):
        assert jobs.read_log("task-none-000000") == []


class TestLatest:
    def test_latest_filters_by_class(self):
        _make("delegate")
        time.sleep(0.01)
        review = _make("review")
        assert jobs.latest_job()["id"] == review["id"]
        assert jobs.latest_job("review")["id"] == review["id"]
        assert jobs.latest_job("delegate")["job_class"] == "delegate"

    def test_latest_with_no_jobs(self):
        assert jobs.latest_job("nothing") is None


class TestCancelDoesNotClobber:
    def test_cancel_racing_completion_keeps_the_finished_result(self):
        # The P0 this guards: codex_cancel used to read the record, and the
        # worker's terminal write could land before the cancel wrote its stale
        # copy back — destroying the output, usage and verification of a run
        # that had already finished, then settling it as "cancelled" with
        # nothing to show for 40 minutes of work.
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=999999)
        stale = jobs.read_job(record["id"], reconcile_state=False)

        jobs.update_job(
            record["id"],
            status="completed",
            phase="done",
            output="THE REPORT",
            usage={"tokens": 120000},
            verification={"verified": True},
            completed_at=time.time(),
            worker_pid=None,
        )

        # The cancel proceeds holding the snapshot it read a moment earlier.
        real_read = jobs.read_job
        jobs.read_job = lambda job_id, reconcile_state=True: dict(stale)
        try:
            jobs.request_cancel(record["id"])
        finally:
            jobs.read_job = real_read

        final = jobs.read_job(record["id"])
        assert final["status"] == "completed"
        assert final["output"] == "THE REPORT"
        assert final["usage"] == {"tokens": 120000}
        assert final["verification"] == {"verified": True}

    def test_cancel_still_registers_for_an_active_job(self):
        record = _make()
        jobs.update_job(record["id"], status="running", worker_pid=999999)
        jobs.request_cancel(record["id"])
        assert jobs.read_job(record["id"])["status"] == "cancelled"


class TestSweepOrphans:
    def test_counts_what_it_actually_settled(self):
        # Regression: list_jobs() reconciles on the way out, so counting its
        # results counted the jobs that were still healthy — the exact inverse
        # of the number, reported at startup as "settled N unfinished job(s)".
        # Create every record first: create_job prunes, prune lists, and
        # listing reconciles — so a job left dead here would already be
        # settled by the next _make() and never reach the sweep.
        dead = _make()
        alive = _make()
        _make()  # queued, inside its launch grace period

        jobs.update_job(dead["id"], status="running", worker_pid=999999)
        jobs.update_job(alive["id"], status="running", worker_pid=os.getpid())

        assert jobs.sweep_orphans() == 1
        assert jobs.read_job(dead["id"])["status"] == "failed"
        assert jobs.read_job(alive["id"])["status"] == "running"

    def test_nothing_to_settle_is_zero(self):
        record = _make()
        jobs.update_job(record["id"], status="completed", completed_at=time.time())
        assert jobs.sweep_orphans() == 0


class TestLaunchLock:
    def test_is_exclusive_across_processes(self):
        # A threading.Lock cannot do this job: every Claude Code session runs
        # its own server process against one job store on disk, so the two
        # launches most likely to race are in different processes.
        import subprocess
        import textwrap

        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                import jobs
                with jobs.launch_lock():
                    print("held", flush=True)
                    time.sleep(3)
            """)],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "CODEX_STATE_DIR": str(jobs.jobs_dir().parent)},
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            with pytest.raises(TimeoutError):
                with jobs.launch_lock(timeout=0.5):
                    pass
        finally:
            holder.kill()
            holder.wait()

    def test_is_reentrant_across_sequential_calls(self):
        with jobs.launch_lock():
            pass
        with jobs.launch_lock(timeout=1.0):
            pass
