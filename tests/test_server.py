"""Tests for the MCP tool surface.

The worker spawn is stubbed throughout — these cover validation, job wiring,
and response shape, not real Codex runs.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs
import server as server_module
import verify
from server import (
    codex_cancel,
    codex_delegate,
    codex_fix,
    codex_follow_up,
    codex_models,
    codex_result,
    codex_review,
    codex_review_and_fix,
    codex_status,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "_spawn_worker", lambda job_id: None)
    monkeypatch.setattr(server_module, "find_codex_binary", lambda: "/usr/bin/codex")
    # Tests simulate a live worker with their own pid; let the identity check
    # accept it (see tests/test_jobs.py for the same shim).
    monkeypatch.setattr(
        jobs, "_is_our_worker", lambda pid, job_id="", token="": pid == os.getpid()
    )
    yield


@pytest.fixture
def project(tmp_path):
    """A real git repository — what a delegation target normally is."""
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return str(path)


@pytest.fixture
def plain_dir(tmp_path):
    """An ordinary unversioned directory, e.g. a notes folder."""
    path = tmp_path / "notes"
    path.mkdir()
    return str(path)


def _call(fn, **kwargs):
    return json.loads(fn(**kwargs))


class TestDelegateValidation:
    def test_empty_task_rejected(self, project):
        result = _call(codex_delegate, task="  ", project_dir=project)
        assert result["error"] == "invalid_request"

    def test_missing_project_dir_rejected(self):
        result = _call(codex_delegate, task="do x", project_dir="")
        assert result["error"] == "invalid_request"

    def test_nonexistent_project_dir_rejected(self):
        result = _call(codex_delegate, task="do x", project_dir="/no/such/dir")
        assert result["error"] == "invalid_request"
        assert "does not exist" in result["message"]

    def test_deprecated_model_rejected_before_spawning(self, project):
        result = _call(
            codex_delegate, task="do x", project_dir=project, model="gpt-5.3-codex"
        )
        assert result["error"] == "invalid_model"

    def test_effort_unsupported_by_model_rejected(self, project):
        # Catching this here saves a doomed run rather than failing 20 min in.
        result = _call(
            codex_delegate, task="do x", project_dir=project,
            model="gpt-5.6-luna", effort="ultra",
        )
        assert result["error"] == "invalid_model"
        assert "not supported" in result["message"]

    def test_negative_timeout_rejected(self, project):
        result = _call(
            codex_delegate, task="do x", project_dir=project, timeout=-5
        )
        assert result["error"] == "invalid_request"


class TestDelegateLaunch:
    def test_defaults_to_read_only(self, project):
        result = _call(codex_delegate, task="do x", project_dir=project)
        assert result["status"] == "started"
        assert result["sandbox"] == "read-only"

    def test_write_opts_into_workspace_write(self, project):
        result = _call(codex_delegate, task="do x", project_dir=project, write=True)
        assert result["sandbox"] == "workspace-write"

    def test_uses_configured_default_model(self, project):
        result = _call(codex_delegate, task="do x", project_dir=project)
        assert result["model"] == "gpt-5.6-terra"
        assert result["effort"] == "xhigh"

    def test_model_override(self, project):
        result = _call(
            codex_delegate, task="do x", project_dir=project,
            model="gpt-5.6-sol", effort="ultra",
        )
        assert result["model"] == "gpt-5.6-sol"
        assert result["effort"] == "ultra"

    def test_job_is_persisted_with_request(self, project):
        result = _call(
            codex_delegate, task="do x", project_dir=project,
            verify_command="pytest -q", context="ctx",
        )
        record = jobs.read_job(result["job_id"])
        assert record["request"]["task"] == "do x"
        assert record["request"]["verify_command"] == "pytest -q"
        assert record["request"]["context"] == "ctx"
        assert record["status"] == "queued"

    def test_project_dir_is_absolutised(self, project, monkeypatch):
        monkeypatch.chdir(os.path.dirname(project))
        result = _call(
            codex_delegate, task="do x", project_dir=os.path.basename(project)
        )
        assert result["project_dir"] == project

    def test_result_schema_is_stored(self, project):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        result = _call(
            codex_delegate, task="do x", project_dir=project, result_schema=schema
        )
        record = jobs.read_job(result["job_id"])
        assert record["request"]["result_schema"] == schema

    def test_next_step_names_the_job(self, project):
        result = _call(codex_delegate, task="do x", project_dir=project)
        assert result["job_id"] in result["next_step"]


class TestRepoLock:
    def _running(self, project, write):
        started = _call(
            codex_delegate, task="first", project_dir=project, write=write
        )
        jobs.update_job(started["job_id"], status="running", worker_pid=os.getpid())
        return started["job_id"]

    def test_second_writer_is_rejected(self, project):
        first = self._running(project, write=True)
        result = _call(codex_delegate, task="second", project_dir=project, write=True)
        assert result["error"] == "repo_busy"
        assert result["blocking_job_id"] == first
        assert "corrupt each other" in result["message"]

    def test_reader_rejected_while_a_writer_runs(self, project):
        self._running(project, write=True)
        result = _call(codex_delegate, task="second", project_dir=project)
        assert result["error"] == "repo_busy"

    def test_writer_rejected_while_a_reader_runs(self, project):
        self._running(project, write=False)
        result = _call(codex_delegate, task="second", project_dir=project, write=True)
        assert result["error"] == "repo_busy"

    def test_two_readers_are_allowed(self, project):
        self._running(project, write=False)
        result = _call(codex_delegate, task="second", project_dir=project)
        assert result["status"] == "started"

    def test_other_repos_are_unaffected(self, project, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        self._running(project, write=True)
        result = _call(codex_delegate, task="second", project_dir=str(other), write=True)
        assert result["status"] == "started"

    def test_lock_releases_when_the_job_finishes(self, project):
        first = self._running(project, write=True)
        jobs.update_job(first, status="completed", worker_pid=None)
        result = _call(codex_delegate, task="second", project_dir=project, write=True)
        assert result["status"] == "started"

    def test_review_and_fix_takes_the_write_lock(self, project):
        self._running(project, write=False)
        result = _call(codex_review_and_fix, project_dir=project)
        assert result["error"] == "repo_busy"

    def test_message_names_the_remedies(self, project):
        self._running(project, write=True)
        result = _call(codex_delegate, task="second", project_dir=project, write=True)
        assert "codex_status" in result["message"]
        assert "codex_cancel" in result["message"]

    def test_no_job_is_created_when_rejected(self, project):
        self._running(project, write=True)
        _call(codex_delegate, task="second", project_dir=project, write=True)
        # The rejected launch must not leave a phantom record behind.
        assert len(jobs.list_jobs()) == 1


class TestFollowUp:
    def test_unknown_job_rejected(self):
        result = _call(codex_follow_up, job_id="nope", task="more")
        assert result["error"] == "job_not_found"

    def test_job_without_thread_id_is_not_resumable(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(started["job_id"], status="failed", thread_id=None)
        result = _call(codex_follow_up, job_id=started["job_id"], task="more")
        assert result["error"] == "not_resumable"

    def test_active_job_cannot_take_a_follow_up(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(
            started["job_id"], status="running", thread_id="t-1",
            worker_pid=os.getpid(),
        )
        result = _call(codex_follow_up, job_id=started["job_id"], task="more")
        assert result["error"] == "job_active"

    def test_resumes_thread_and_inherits_settings(self, project):
        started = _call(
            codex_delegate, task="do x", project_dir=project, model="gpt-5.6-sol"
        )
        jobs.update_job(
            started["job_id"], status="completed", thread_id="thread-xyz",
            worker_pid=None,
        )
        result = _call(codex_follow_up, job_id=started["job_id"], task="more")
        assert result["status"] == "started"
        record = jobs.read_job(result["job_id"])
        assert record["request"]["resume_thread_id"] == "thread-xyz"
        assert record["request"]["model"] == "gpt-5.6-sol"
        assert record["request"]["parent_job_id"] == started["job_id"]

    def test_empty_task_rejected(self, project):
        result = _call(codex_follow_up, job_id="x", task="")
        assert result["error"] == "invalid_request"


class TestStatus:
    def test_unknown_job(self):
        assert _call(codex_status, job_id="nope")["error"] == "job_not_found"

    def test_lists_all_jobs_when_id_omitted(self, project):
        _call(codex_delegate, task="a", project_dir=project)
        _call(codex_delegate, task="b", project_dir=project)
        result = _call(codex_status)
        assert result["count"] == 2

    def test_reports_phase_and_log(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.append_log(started["job_id"], "phase → editing")
        result = _call(codex_status, job_id=started["job_id"])
        assert result["status"] == "queued"
        assert any("editing" in line for line in result["log_tail"])

    def test_terminal_job_suggests_result(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(started["job_id"], status="completed", worker_pid=None)
        result = _call(codex_status, job_id=started["job_id"], wait=True)
        assert "codex_result" in result["next_step"]

    def test_prefix_lookup_works(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        result = _call(codex_status, job_id=started["job_id"][:14])
        assert result["job_id"] == started["job_id"]


class TestResult:
    def test_active_job_says_not_ready(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(started["job_id"], status="running", worker_pid=os.getpid())
        result = _call(codex_result, job_id=started["job_id"])
        assert "still running" in result["message"]
        assert "output" not in result

    def test_completed_job_returns_output_and_verification(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(
            started["job_id"], status="completed", worker_pid=None,
            output="done!", verification={"verified": True, "checks": []},
            usage={"output_tokens": 5},
        )
        result = _call(codex_result, job_id=started["job_id"])
        assert result["output"] == "done!"
        assert result["verification"]["verified"] is True
        assert result["usage"]["output_tokens"] == 5

    def test_timeout_job_flags_partial_output(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(
            started["job_id"], status="timeout", worker_pid=None, output="partial"
        )
        result = _call(codex_result, job_id=started["job_id"])
        assert "partial" in result["message"]

    def test_defaults_to_latest_job(self, project):
        _call(codex_delegate, task="a", project_dir=project)
        second = _call(codex_delegate, task="b", project_dir=project)
        jobs.update_job(second["job_id"], status="completed", worker_pid=None)
        assert _call(codex_result)["job_id"] == second["job_id"]

    def test_no_jobs_at_all(self):
        assert _call(codex_result)["error"] == "job_not_found"


class TestCancel:
    def test_unknown_job(self):
        assert _call(codex_cancel, job_id="nope")["error"] == "job_not_found"

    def test_terminal_job_is_a_no_op(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(started["job_id"], status="completed", worker_pid=None)
        result = _call(codex_cancel, job_id=started["job_id"])
        assert "already finished" in result["message"]

    def test_cancel_signals_the_live_worker_tree(self, project, monkeypatch):
        killed = []
        monkeypatch.setattr(
            jobs, "terminate_tree", lambda record: killed.append(record["worker_pid"])
        )
        monkeypatch.setattr(jobs, "reap_orphan_codex", lambda record: False)
        started = _call(codex_delegate, task="do x", project_dir=project)
        # A live pid: the only case where there is anything to signal.
        jobs.update_job(started["job_id"], status="running", worker_pid=os.getpid())
        _call(codex_cancel, job_id=started["job_id"])
        assert killed == [os.getpid()]
        assert jobs.is_cancel_requested(started["job_id"])

    def test_cancel_of_dead_worker_reports_settled_state(self, project, monkeypatch):
        killed = []
        monkeypatch.setattr(jobs, "terminate_tree", lambda pid: killed.append(pid))
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(started["job_id"], status="running", worker_pid=999999)
        result = _call(codex_cancel, job_id=started["job_id"])
        # Reconcile settles a dead-worker job on read, so there is nothing left
        # to signal — cancelling it is a no-op report, not a kill.
        assert killed == []
        assert result["status"] in jobs.TERMINAL_STATUSES
        assert "already finished" in result["message"]


class TestReviewTools:
    def test_review_is_read_only(self, project):
        result = _call(codex_review, project_dir=project)
        assert result["sandbox"] == "read-only"
        assert result["kind"] == "review"

    def test_review_and_fix_is_write(self, project):
        result = _call(codex_review_and_fix, project_dir=project)
        assert result["sandbox"] == "workspace-write"

    def test_review_passes_base_branch_and_focus(self, project):
        result = _call(
            codex_review, project_dir=project, base_branch="develop", focus="security"
        )
        record = jobs.read_job(result["job_id"])
        assert record["request"]["base_branch"] == "develop"
        assert record["request"]["focus"] == "security"

    def test_fix_requires_findings(self, project):
        assert _call(codex_fix, project_dir=project, findings="")["error"] == (
            "invalid_request"
        )

    def test_fix_is_write(self, project):
        result = _call(codex_fix, project_dir=project, findings="P1: bug in x")
        assert result["sandbox"] == "workspace-write"


class TestModelsTool:
    def test_lists_models_and_configured_default(self):
        result = _call(codex_models)
        assert "gpt-5.6-terra" in result["models"]
        assert result["configured_default"]["model"] == "gpt-5.6-terra"
        assert "gpt-5.3-codex" in result["deprecated"]


class TestErrorClassification:
    def test_error_type_reaches_the_caller(self, project):
        # The worker records why a job failed, but _summarize used to omit the
        # field — so a caller could not tell a quota wall from a bad slug
        # without pattern-matching prose, and the documented response to each
        # is different. Every read path must carry it.
        started = _call(codex_delegate, task="do x", project_dir=project)
        jobs.update_job(
            started["job_id"],
            status="failed",
            phase="failed",
            error="Codex quota exhausted. Wait and retry...",
            error_type="rate_limit",
            completed_at=1.0,
            worker_pid=None,
        )
        for tool in (codex_status, codex_result):
            payload = _call(tool, job_id=started["job_id"])
            assert payload["error_type"] == "rate_limit", tool.__name__

    def test_error_type_is_absent_on_a_healthy_job(self, project):
        started = _call(codex_delegate, task="do x", project_dir=project)
        assert _call(codex_status, job_id=started["job_id"])["error_type"] is None


class TestVerifyTimeout:
    def test_default_is_recorded_so_the_worker_can_read_it(self, project):
        # Regression: worker.run read verify_timeout from the request, but no
        # tool ever wrote it — so the knob was unreachable and every
        # verify_command was pinned to 900s, a fifth of the job timeout.
        started = _call(
            codex_delegate, task="do x", project_dir=project,
            verify_command="pytest -q",
        )
        record = jobs.read_job(started["job_id"])
        assert record["request"]["verify_timeout"] == verify.DEFAULT_VERIFY_TIMEOUT

    def test_caller_value_reaches_the_request(self, project):
        started = _call(
            codex_delegate, task="do x", project_dir=project,
            verify_command="pytest -q", verify_timeout=3600,
        )
        record = jobs.read_job(started["job_id"])
        assert record["request"]["verify_timeout"] == 3600

    def test_negative_is_rejected(self, project):
        result = _call(
            codex_delegate, task="do x", project_dir=project,
            verify_command="pytest -q", verify_timeout=-1,
        )
        assert result["error"] == "invalid_request"

    @pytest.mark.parametrize("tool,extra", [
        ("codex_review_and_fix", {}),
        ("codex_fix", {"findings": "P1: fix the thing"}),
    ])
    def test_exposed_on_every_tool_that_takes_a_verify_command(self, project, tool, extra):
        fn = getattr(server_module, tool)
        started = _call(
            fn, project_dir=project, verify_command="pytest -q",
            verify_timeout=1234, **extra,
        )
        record = jobs.read_job(started["job_id"])
        assert record["request"]["verify_timeout"] == 1234


class TestGitRequiredKinds:
    def test_review_outside_a_repo_is_refused(self, plain_dir):
        # Both review kinds build their prompt around `git diff <base>...HEAD`.
        # Outside a repo that command fails, so codex has no diff to read.
        result = _call(codex_review, project_dir=plain_dir)
        assert result["error"] == "not_a_repo"
        assert "codex_delegate" in result["message"]

    def test_review_and_fix_outside_a_repo_is_refused(self, plain_dir):
        # The sharp one: with no diff to justify them, it would still edit
        # unversioned files under workspace-write, unrecoverably.
        result = _call(server_module.codex_review_and_fix, project_dir=plain_dir)
        assert result["error"] == "not_a_repo"
        assert jobs.list_jobs() == []

    def test_delegate_outside_a_repo_still_works(self, plain_dir):
        # The case --skip-git-repo-check exists for: writing and research in an
        # ordinary directory. Refusing this would remove the point of the flag.
        result = _call(
            codex_delegate, task="draft the note", project_dir=plain_dir, write=True
        )
        assert result["status"] == "started"

    def test_fix_outside_a_repo_still_works(self, plain_dir):
        result = _call(
            server_module.codex_fix, project_dir=plain_dir, findings="P1: typo"
        )
        assert result["status"] == "started"

    def test_review_inside_a_repo_is_allowed(self, project):
        assert _call(codex_review, project_dir=project)["status"] == "started"

    def test_review_from_a_subdirectory_of_a_repo_is_allowed(self, project):
        sub = Path(project) / "src"
        sub.mkdir()
        assert _call(codex_review, project_dir=str(sub))["status"] == "started"


class TestUnsafeProjectDir:
    def test_write_at_home_is_refused(self, tmp_path, monkeypatch):
        # The realistic accident: /Users/brian instead of
        # /Users/brian/Documents/second-brain. workspace-write scopes codex to
        # everything below project_dir, so this is the whole home directory.
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _call(
            codex_delegate, task="tidy my notes",
            project_dir=str(tmp_path), write=True,
        )
        assert result["error"] == "unsafe_project_dir"
        assert jobs.list_jobs() == []

    def test_write_at_filesystem_root_is_refused(self):
        result = _call(codex_delegate, task="do x", project_dir="/", write=True)
        assert result["error"] == "unsafe_project_dir"

    def test_denied_roots_are_matched_through_symlinks(self):
        # /tmp is a symlink to /private/tmp on macOS; comparing raw strings
        # would let the alias straight through.
        result = _call(codex_delegate, task="do x", project_dir="/tmp", write=True)
        assert result["error"] == "unsafe_project_dir"

    def test_read_only_at_home_is_allowed(self, tmp_path, monkeypatch):
        # Nothing to destroy, and the guard is about blast radius. A read-only
        # run here is unwise, not dangerous — a different conversation.
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _call(codex_delegate, task="what is here", project_dir=str(tmp_path))
        assert result["status"] == "started"

    def test_write_below_home_is_unaffected(self, tmp_path, monkeypatch):
        # Membership is by equality, not containment: every real project lives
        # inside one of these roots and must stay unaffected.
        monkeypatch.setenv("HOME", str(tmp_path))
        notes = tmp_path / "Documents" / "second-brain"
        notes.mkdir(parents=True)
        result = _call(
            codex_delegate, task="draft the note",
            project_dir=str(notes), write=True,
        )
        assert result["status"] == "started"

    def test_review_and_fix_at_home_is_refused_even_if_home_is_a_repo(
        self, tmp_path, monkeypatch
    ):
        # Versioned dotfiles make ~ a git repo, so the not_a_repo check would
        # pass it. Most of ~ is still untracked, so the blast radius argument
        # is unchanged — the two guards are deliberately independent.
        monkeypatch.setenv("HOME", str(tmp_path))
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        result = _call(codex_review_and_fix, project_dir=str(tmp_path))
        assert result["error"] == "unsafe_project_dir"
