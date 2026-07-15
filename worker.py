"""Detached worker that runs exactly one delegated Codex job.

Spawned by the MCP server as a separate, session-leading process so that a
long-running Codex task survives the MCP server restarting, and so that
cancelling a job can signal the whole process group (worker + codex + any
shells codex spawned) rather than orphaning a run that keeps burning quota.

Invoked as:  python3 worker.py <job_id>
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs
import verify
from codex_runner import (
    CodexError,
    build_delegate_prompt,
    build_fix_prompt,
    build_follow_up_prompt,
    build_review_and_fix_prompt,
    build_review_prompt,
    run_codex,
)
from config import Config


def _build_prompt(request: dict) -> str:
    kind = request.get("kind", "delegate")
    write = request.get("write", False)

    if kind == "delegate":
        return build_delegate_prompt(
            task=request["task"],
            project_dir=request["project_dir"],
            write=write,
            context=request.get("context", ""),
            has_schema=bool(request.get("result_schema")),
        )
    if kind == "follow_up":
        return build_follow_up_prompt(task=request["task"], write=write)
    if kind == "review":
        return build_review_prompt(
            base_branch=request.get("base_branch", "main"),
            focus=request.get("focus", "all"),
            context=request.get("context", ""),
        )
    if kind == "review_and_fix":
        return build_review_and_fix_prompt(
            base_branch=request.get("base_branch", "main"),
            focus=request.get("focus", "all"),
            context=request.get("context", ""),
        )
    if kind == "fix":
        return build_fix_prompt(
            findings=request["findings"],
            context=request.get("context", ""),
        )
    raise CodexError(f"Unknown job kind: {kind}")


def run(job_id: str) -> None:
    record = jobs.read_job(job_id, reconcile_state=False)
    if record is None:
        return
    if record.get("status") != "queued":
        return

    request = record["request"]
    project_dir = request["project_dir"]

    # A cancel can land between job creation and this point, while there is no
    # pid to signal. Without this check the job would run to completion and
    # report "completed" despite having been cancelled.
    if jobs.is_cancel_requested(job_id):
        jobs.update_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            error="Cancelled before the task started.",
            completed_at=time.time(),
            worker_pid=None,
        )
        return

    jobs.update_job(
        job_id,
        status="running",
        phase="starting",
        worker_pid=os.getpid(),
        started_at=time.time(),
    )

    # Re-check: a cancel racing the pid publish above could have found no pid
    # to signal, so honour the sentinel before spending any quota.
    if jobs.is_cancel_requested(job_id):
        jobs.update_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            error="Cancelled before the task started.",
            completed_at=time.time(),
            worker_pid=None,
        )
        return
    jobs.append_log(
        job_id,
        f"Started {request.get('kind')} on {request.get('model')} "
        f"(effort={request.get('effort')}, sandbox={request.get('sandbox')})",
    )

    schema_file = None
    if request.get("result_schema"):
        schema_file = str(jobs.schema_path(job_id))
        with open(schema_file, "w") as handle:
            json.dump(request["result_schema"], handle)

    before = verify.snapshot(project_dir)

    last_phase = {"value": "starting"}

    def on_event(event, state):
        # Persist only on a phase transition — an event-rate write would
        # hammer the disk on a chatty run for no added signal.
        if state["phase"] != last_phase["value"]:
            last_phase["value"] = state["phase"]
            jobs.update_job(job_id, phase=state["phase"])
            jobs.append_log(job_id, f"phase → {state['phase']}")
        if event.get("type") == "thread.started":
            jobs.update_job(job_id, thread_id=event.get("thread_id"))
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "command_execution":
                command = str(item.get("command", ""))[:160]
                jobs.append_log(job_id, f"ran: {command} (exit {item.get('exit_code')})")

    try:
        result = run_codex(
            project_dir=project_dir,
            prompt=_build_prompt(request),
            model=request["model"],
            effort=request["effort"],
            sandbox=request["sandbox"],
            output_file=str(jobs.output_path(job_id)),
            prompt_file=str(jobs.prompt_path(job_id)),
            stderr_file=str(jobs.stderr_path(job_id)),
            timeout=request["timeout"],
            schema_file=schema_file,
            resume_thread_id=request.get("resume_thread_id"),
            on_event=on_event,
        )
    except CodexError as exc:
        # The sentinel file, not the record field — a concurrent phase write
        # can clobber the field, but not the file.
        status = "cancelled" if jobs.is_cancel_requested(job_id) else "failed"
        jobs.update_job(
            job_id,
            status=status,
            phase=status,
            error=str(exc),
            error_type=type(exc).__name__,
            completed_at=time.time(),
            worker_pid=None,
        )
        jobs.append_log(job_id, f"{status}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - worker must never die silently
        jobs.update_job(
            job_id,
            status="failed",
            phase="failed",
            error=f"Worker error: {exc}",
            error_type=type(exc).__name__,
            completed_at=time.time(),
            worker_pid=None,
        )
        jobs.append_log(job_id, f"worker error: {exc}")
        return

    jobs.append_log(job_id, "codex finished; verifying")
    try:
        verification = verify.verify(
            project_dir=project_dir,
            before=before,
            write=request.get("write", False),
            verify_command=request.get("verify_command", ""),
            verify_timeout=request.get("verify_timeout", 900),
        )
    except Exception as exc:  # noqa: BLE001
        verification = {
            "verified": False,
            "checks": [{
                "name": "verification_error",
                "status": "fail",
                "detail": f"Verification could not run: {exc}",
            }],
            "warnings": [],
        }

    status = "timeout" if result["timed_out"] else "completed"
    jobs.update_job(
        job_id,
        status=status,
        phase="done" if status == "completed" else "timeout",
        output=result["output"],
        structured_output=result["structured_output"],
        thread_id=result["thread_id"],
        usage=result["usage"],
        verification=verification,
        completed_at=time.time(),
        worker_pid=None,
    )
    jobs.append_log(
        job_id,
        f"{status}: verified={verification.get('verified')} "
        f"files_changed={len(verification.get('git', {}).get('files_changed', []))}",
    )


def _daemonize() -> None:
    """Fork and exit the parent so the real worker is reparented to init.

    This is a correctness fix, not tidiness. The server launches us as its own
    child; an exited-but-unreaped child is a zombie, and a zombie still answers
    kill(pid, 0). So a worker that crashed without recording its result would
    look alive to jobs.reconcile(), pinning its job at "running" indefinitely —
    until some unrelated Popen happened to reap it.

    The server starts us with start_new_session, so our parent is a session and
    process-group leader. Forking here and exiting the parent leaves us
    orphaned to init (which reaps us the instant we die, making PID liveness
    truthful) while keeping us in that same process group — which is what lets
    codex_cancel signal the whole tree.
    """
    if os.fork() > 0:
        os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: worker.py <job_id>", file=sys.stderr)
        sys.exit(2)
    _daemonize()
    run(sys.argv[1])
