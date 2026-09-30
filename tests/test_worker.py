"""Tests for the worker's choice of transport and its Daybreak verification."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import jobs
import worker


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_ENV_FILE", tmp_path / "empty.env")
    monkeypatch.setattr(config, "_overridden", {})
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "state"))
    yield


@pytest.fixture
def runners(monkeypatch):
    """Record which transport the worker picks, and with what."""
    calls = {}

    def fake(name):
        def run(**kwargs):
            calls[name] = kwargs
            return {"thread_id": "t-1", "usage": None, "output": "done",
                    "structured_output": None, "timed_out": False, "exit_code": 0,
                    "rollout_path": "/rollout.jsonl"}
        return run

    monkeypatch.setattr(worker, "run_codex", fake("exec"))
    monkeypatch.setattr(worker, "run_codex_app_server", fake("app_server"))
    verified = {}
    monkeypatch.setattr(worker.verify, "verify",
                        lambda **kwargs: verified.update(kwargs) or {"verified": True})
    calls["verify"] = verified
    return calls


def _job(tmp_path, **request):
    return jobs.create_job("delegate", {
        "kind": "delegate", "task": "do x", "project_dir": str(tmp_path),
        "model": "gpt-6-sol", "effort": "high", "sandbox": "read-only",
        "write": False, "timeout": 60, **request,
    })["id"]


def test_plain_job_runs_on_exec(tmp_path, runners):
    worker.run(_job(tmp_path, cyber_access_program=None))
    assert "exec" in runners and "app_server" not in runners
    assert runners["verify"]["cyber_access_program"] is None


def test_daybreak_job_runs_on_app_server_and_is_verified(tmp_path, runners):
    job_id = _job(tmp_path, cyber_access_program="daybreak_blue")
    worker.run(job_id)
    assert "app_server" in runners and "exec" not in runners
    assert runners["app_server"]["cyber_access_program"] == "daybreak_blue"
    # The rollout, not the request, is what verification checks.
    assert runners["verify"]["cyber_access_program"] == "daybreak_blue"
    assert runners["verify"]["rollout_path"] == "/rollout.jsonl"
    assert jobs.read_job(job_id, reconcile_state=False)["status"] == "completed"


def test_codex_start_token_is_recorded_at_spawn(tmp_path, monkeypatch, runners):
    # app-server's argv names no job, so cancel and reaping identify codex by
    # its start time.
    def run(**kwargs):
        kwargs["on_spawn"](os.getpid())
        return {"thread_id": "t-1", "usage": None, "output": "", "structured_output": None,
                "timed_out": False, "exit_code": 0, "rollout_path": None}

    monkeypatch.setattr(worker, "run_codex_app_server", run)
    job_id = _job(tmp_path, cyber_access_program="daybreak_blue")
    worker.run(job_id)
    record = jobs.read_job(job_id, reconcile_state=False)
    assert record["codex_pid"] == os.getpid()
    assert record["codex_token"] == jobs.process_start_token(os.getpid())
