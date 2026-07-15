"""Tests for the job store."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    # CODEX_STATE_DIR is absent from .env, so the live-reload in Config._get
    # will not clobber this override.
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "state"))
    # Tests stand in for a live worker using their own pid, which is not
    # actually running worker.py. Treat this process as a legitimate worker so
    # the identity check doesn't reconcile every simulated job away.
    monkeypatch.setattr(jobs, "_is_our_worker", lambda pid: pid == os.getpid())
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
    def test_terminate_tree_refuses_an_unknown_pid(self, monkeypatch):
        # PID reuse: signalling a pid we cannot confirm is ours would kill an
        # unrelated process group.
        monkeypatch.setattr(jobs, "_is_our_worker", lambda pid: False)
        killed = []
        monkeypatch.setattr(jobs.os, "killpg", lambda *a: killed.append(a))
        jobs.terminate_tree(4242)
        assert killed == []

    def test_pid_alive_false_when_identity_unconfirmed(self, monkeypatch):
        monkeypatch.setattr(jobs, "_is_our_worker", lambda pid: False)
        assert jobs._pid_alive(os.getpid()) is False


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
