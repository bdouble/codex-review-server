"""Codex Delegation MCP Server.

Delegates arbitrary engineering and writing work to OpenAI's Codex CLI, so
Claude Code can orchestrate Codex the same way it orchestrates its own
subagents. Codex runs as a full agent inside the target repository with
complete file access — the same as running it manually.

Every task is a background job. Tools that start work return a job_id
immediately; poll with codex_status (or codex_status(wait=True)) and collect
with codex_result. This keeps a 20-minute `ultra` run from blocking the
client, and lets several Codex tasks run in parallel.

Install:
    claude mcp add --scope user codex-review-server -- python3 /path/to/server.py
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import jobs
import models
import verify
from codex_runner import CodexNotFoundError, find_codex_binary
from config import Config

server = FastMCP("codex-review-server")

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")

# Kinds whose prompt is built entirely around `git diff <base>...HEAD`.
#
# codex refuses to run outside a git repository by default; we pass
# --skip-git-repo-check to lift that, deliberately, so writing and research
# tasks can target an ordinary directory. These two are the case where lifting
# it can only misfire: with no repo there is no diff, so codex has nothing to
# review — and review_and_fix would go on to edit unversioned files under
# workspace-write, with nothing to justify the edits and no way to undo them.
_GIT_REQUIRED_KINDS = {"review", "review_and_fix"}

# Directory names that are a home or system root rather than a project.
_UNSAFE_ROOTS = (
    "/", "~",
    "/Users", "/home", "/root",
    "/etc", "/usr", "/var", "/opt", "/tmp", "/bin", "/sbin",
    "/Library", "/System", "/Applications", "/private",
    "/mnt", "/media",
)


def _unsafe_roots() -> set[str]:
    """Directories no task should ever be pointed *at*.

    project_dir's only gate is os.path.isdir, so `/Users/brian` — one
    tab-completion short of `/Users/brian/Documents/second-brain` — is accepted
    as a project. Both sandboxes then do exactly what they promise, over the
    wrong tree: workspace-write grants Codex every file in the home directory,
    `.ssh` and browser profiles included, with no undo outside a repo; and
    read-only, which destroys nothing, still reads that home directory and
    sends what it reads to OpenAI. One typo, two different bad outcomes, so the
    guard covers both rather than only the loud one.

    Not a security boundary — codex is not an adversary, and this would be a
    poor one. It catches the realistic accident: a path one segment too short.
    Membership is by equality, never containment: the point is to reject a root
    that was named directly, while every real project *inside* these roots
    stays unaffected.

    Resolved per call rather than at import: HOME is environment-dependent, and
    realpath matters because /tmp is /private/tmp on macOS.
    """
    return {
        os.path.realpath(os.path.expanduser(path)) for path in _UNSAFE_ROOTS
    }


def _error(error_type: str, message: str, **extra) -> str:
    return json.dumps({"error": error_type, "message": message, **extra}, indent=2)


def _spawn_worker(job_id: str) -> None:
    """Launch the detached worker for a job.

    start_new_session creates a fresh process group, which is what lets
    codex_cancel later signal the worker and codex together.

    The process spawned here immediately forks the real worker and exits (see
    worker._daemonize), so this wait returns in milliseconds and reaps it. The
    wait is deliberate: an unreaped zombie answers kill(pid, 0) and would
    defeat jobs.reconcile(). The detached worker records its own pid into the
    job file, so that is where the real pid comes from.
    """
    proc = subprocess.Popen(
        [sys.executable, _WORKER, job_id],
        cwd=os.path.dirname(_WORKER),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        # The launcher should exit at once; never block a tool call on it.
        pass


def _conflict_message(conflict: dict, project_dir: str, write: bool) -> str:
    """Explain a repo_busy rejection and what to do about it."""
    other_writes = bool((conflict.get("request") or {}).get("write"))
    if write and other_writes:
        reason = "Two jobs writing the same working tree interleave edits and corrupt each other."
    elif write:
        reason = (
            "Starting a writer while a read-only job is running would make that "
            "job report your edits as its own read-only violation."
        )
    else:
        reason = (
            "A read-only job running alongside a writer sees the writer's edits "
            "and reports them as its own violation."
        )
    return (
        f"{conflict['id']} ({conflict.get('job_class')}, "
        f"{'write' if other_writes else 'read-only'}) is already active in "
        f"{project_dir}. {reason}\n"
        f"Options: wait for it with codex_status('{conflict['id']}', wait=True), "
        f"cancel it with codex_cancel('{conflict['id']}'), or run against a "
        f"different repository."
    )


def _resolve_settings(model: str, effort: str) -> tuple[str, str, str | None]:
    """Resolve model/effort against config and the live catalog."""
    chosen_model = model or Config.MODEL
    chosen_effort = effort or Config.EFFORT
    error = models.validate(chosen_model, chosen_effort, Config.CODEX_HOME)
    return chosen_model, chosen_effort, error


def _launch(kind: str, project_dir: str, model: str, effort: str, write: bool,
            timeout: int, verify_timeout: int = 0, **request_fields) -> str:
    """Validate, create a job, and spawn its worker."""
    try:
        find_codex_binary()
    except CodexNotFoundError as exc:
        return _error("codex_not_found", str(exc))

    if not project_dir:
        return _error("invalid_request", "project_dir is required.")
    project_dir = os.path.abspath(os.path.expanduser(project_dir))
    if not os.path.isdir(project_dir):
        return _error(
            "invalid_request", f"project_dir does not exist: {project_dir}"
        )

    if jobs.canonical_dir(project_dir) in _unsafe_roots():
        return _error(
            "unsafe_project_dir",
            f"Refusing to run a task directly in {project_dir}. That is a home "
            f"or system root: a write task would get every file beneath it, and "
            f"even a read-only one would read your whole home directory and "
            f"send what it reads upstream. Almost always a path one segment "
            f"short of the project you meant — name the directory to work in.",
        )

    if kind in _GIT_REQUIRED_KINDS and not verify.repo_root(project_dir):
        return _error(
            "not_a_repo",
            f"{project_dir} is not a git repository, and {kind} reviews a "
            f"branch diff (`git diff <base>...HEAD`) — there is nothing here "
            f"for it to read. Point it at a repository, or use codex_delegate "
            f"for work in an ordinary directory.",
        )

    chosen_model, chosen_effort, error = _resolve_settings(model, effort)
    if error:
        return _error("invalid_model", error)

    effective_timeout = timeout or Config.TIMEOUT
    if effective_timeout <= 0:
        return _error("invalid_request", "timeout must be positive.")

    if verify_timeout < 0:
        return _error(
            "invalid_request",
            "verify_timeout cannot be negative. Omit it, or pass 0, for "
            f"the default of {verify.DEFAULT_VERIFY_TIMEOUT}s.",
        )

    request = {
        "kind": kind,
        "project_dir": project_dir,
        "model": chosen_model,
        "effort": chosen_effort,
        "sandbox": "workspace-write" if write else "read-only",
        "write": write,
        "timeout": effective_timeout,
        "verify_timeout": verify_timeout or verify.DEFAULT_VERIFY_TIMEOUT,
        **request_fields,
    }

    # Check-and-claim must be atomic, or two concurrent launches both see a
    # free tree and both start. The lock spans processes because the racing
    # launches usually are in different ones — every session runs its own
    # server against the same job store on disk.
    try:
        with jobs.launch_lock():
            conflict = jobs.find_conflict(project_dir, write)
            if conflict:
                return _error(
                    "repo_busy",
                    _conflict_message(conflict, project_dir, write),
                    blocking_job_id=conflict["id"],
                    blocking_job_status=conflict.get("status"),
                )
            record = jobs.create_job(kind, request)
    except TimeoutError as exc:
        return _error("store_busy", f"{exc} Try again in a moment.")

    _spawn_worker(record["id"])

    return json.dumps({
        "status": "started",
        "job_id": record["id"],
        "kind": kind,
        "model": chosen_model,
        "effort": chosen_effort,
        "sandbox": request["sandbox"],
        "project_dir": project_dir,
        "next_step": (
            f"Poll codex_status('{record['id']}') for progress, or "
            f"codex_status('{record['id']}', wait=True) to block until it "
            f"finishes. Collect the result with codex_result('{record['id']}')."
        ),
    }, indent=2)


def _summarize(record: dict) -> dict:
    """Compact job view — no full output, safe to poll repeatedly."""
    elapsed = None
    if record.get("started_at"):
        end = record.get("completed_at") or time.time()
        elapsed = round(end - record["started_at"], 1)
    return {
        "job_id": record["id"],
        "kind": record.get("job_class"),
        "status": record.get("status"),
        "phase": record.get("phase"),
        "model": record.get("model"),
        "effort": record.get("effort"),
        "project_dir": record.get("project_dir"),
        "elapsed_seconds": elapsed,
        "thread_id": record.get("thread_id"),
        "error": record.get("error"),
        # The classification, not just the prose. The worker records why a job
        # failed, but nothing used to return it, so a caller could not tell a
        # quota wall from a bad slug without pattern-matching the message —
        # and the documented response to each is different.
        "error_type": record.get("error_type"),
    }


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


@server.tool()
def codex_delegate(
    task: str,
    project_dir: str,
    model: str = "",
    effort: str = "",
    write: bool = False,
    context: str = "",
    result_schema: dict | None = None,
    verify_command: str = "",
    verify_timeout: int = 0,
    timeout: int = 0,
) -> str:
    """Delegate ANY task to Codex — engineering, research, writing, analysis.

    Codex runs as a full agent in project_dir with complete repository access.
    Use this the way you would use a subagent: give it a self-contained task,
    then poll for the result. Returns immediately with a job_id.

    Runs read-only unless write=True, so it cannot touch your files by default.

    Args:
        task: The complete task. Be specific about what "done" means — Codex
            cannot ask clarifying questions mid-run.
        project_dir: Absolute path to the working directory.
        model: Model slug (e.g. "gpt-5.6-sol"). Defaults to CODEX_MODEL.
            Call codex_models for the live catalog.
        effort: low|medium|high|xhigh|max|ultra. Defaults to CODEX_EFFORT.
            "ultra" (Sol/Terra only) runs four agents in parallel — slow, for
            genuinely hard problems.
        write: True to allow file edits (workspace-write). Default read-only.
        context: Background Codex should have — constraints, prior findings,
            acceptance criteria.
        result_schema: JSON Schema. When set, Codex's final message is a
            validated JSON object matching it, returned as structured_output.
        verify_command: Shell command run after the task to check the work
            (e.g. "pytest -q"). Its real exit code gates the verified verdict.
        verify_timeout: Seconds to allow verify_command (default 900). Raise
            it for a suite slower than that — a timeout is reported as a
            failed verification.
        timeout: Seconds. Defaults to CODEX_TIMEOUT.

    Returns:
        JSON with job_id. Poll codex_status, collect with codex_result.
    """
    if not task or not task.strip():
        return _error("invalid_request", "task is required.")
    return _launch(
        kind="delegate",
        project_dir=project_dir,
        model=model,
        effort=effort,
        write=write,
        timeout=timeout,
        task=task,
        context=context,
        result_schema=result_schema,
        verify_command=verify_command,
        verify_timeout=verify_timeout,
    )


@server.tool()
def codex_follow_up(
    job_id: str,
    task: str,
    write: bool = False,
    model: str = "",
    effort: str = "",
    verify_command: str = "",
    verify_timeout: int = 0,
    timeout: int = 0,
) -> str:
    """Continue an earlier Codex job in its original thread.

    Resumes the existing conversation, so Codex still has everything it read
    and concluded. Always prefer this over a fresh codex_delegate for a
    follow-up: a new session re-pays the whole context-establishing cost and
    loses what the first run learned.

    Args:
        job_id: The job to continue (full id or unique prefix).
        task: The follow-up instruction.
        write: True to allow file edits on this turn.
        model: Override the model (defaults to the original job's).
        effort: Override the effort (defaults to the original job's).
        verify_command: Shell command to check the work afterwards.
        verify_timeout: Seconds to allow verify_command (default 900).
        timeout: Seconds. Defaults to CODEX_TIMEOUT.

    Returns:
        JSON with a new job_id for the follow-up turn.
    """
    if not task or not task.strip():
        return _error("invalid_request", "task is required.")

    try:
        resolved = jobs.resolve_job_id(job_id)
    except ValueError as exc:
        return _error("job_not_found", str(exc))

    record = jobs.read_job(resolved)
    if record is None:
        return _error("job_not_found", f"No job '{resolved}'.")

    thread_id = record.get("thread_id")
    if not thread_id:
        return _error(
            "not_resumable",
            f"Job {resolved} has no thread_id — it failed before Codex started "
            f"a session, so there is nothing to resume. Start a new "
            f"codex_delegate instead.",
        )
    if record.get("status") in jobs.ACTIVE_STATUSES:
        return _error(
            "job_active",
            f"Job {resolved} is still {record['status']}. Wait for it to finish "
            f"before sending a follow-up.",
        )

    return _launch(
        kind="follow_up",
        project_dir=record["project_dir"],
        model=model or record.get("model", ""),
        effort=effort or record.get("effort", ""),
        write=write,
        timeout=timeout,
        task=task,
        resume_thread_id=thread_id,
        parent_job_id=resolved,
        verify_command=verify_command,
        verify_timeout=verify_timeout,
    )


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


@server.tool()
def codex_status(job_id: str = "", wait: bool = False, timeout_seconds: int = 600) -> str:
    """Check delegated job progress. Omit job_id to list all jobs.

    Args:
        job_id: Job id or unique prefix. Empty lists every job, newest first.
        wait: Block until the job reaches a terminal state.
        timeout_seconds: Max seconds to block when wait=True. Returns the
            job's current state on expiry — it keeps running regardless.

    Returns:
        JSON status: phase, elapsed time, and a tail of the activity log.
    """
    if not job_id:
        records = jobs.list_jobs(limit=25)
        return json.dumps({
            "count": len(records),
            "jobs": [_summarize(r) for r in records],
        }, indent=2)

    try:
        resolved = jobs.resolve_job_id(job_id)
    except ValueError as exc:
        return _error("job_not_found", str(exc))

    deadline = time.time() + max(1, timeout_seconds)
    while True:
        record = jobs.read_job(resolved)
        if record is None:
            return _error("job_not_found", f"No job '{resolved}'.")

        done = record.get("status") in jobs.TERMINAL_STATUSES
        if done or not wait or time.time() >= deadline:
            payload = _summarize(record)
            payload["log_tail"] = jobs.read_log(resolved, tail_lines=15)
            if done:
                payload["next_step"] = f"Collect it with codex_result('{resolved}')."
            elif wait:
                payload["note"] = (
                    f"Still {record.get('status')} after {timeout_seconds}s. "
                    f"The job keeps running — poll again."
                )
            return json.dumps(payload, indent=2)

        time.sleep(2)


@server.tool()
def codex_result(job_id: str = "") -> str:
    """Get the full result of a finished job, with verification.

    The verification block is checked against the repository, not against what
    Codex said it did: which files git shows changed, and whether your
    verify_command actually passed. Read it before trusting the output.

    Args:
        job_id: Job id or unique prefix. Empty returns the most recent job.

    Returns:
        JSON with output, structured_output (if a result_schema was set),
        verification, and token usage.
    """
    if not job_id:
        record = jobs.latest_job()
        if record is None:
            return _error("job_not_found", "No jobs yet.")
    else:
        try:
            resolved = jobs.resolve_job_id(job_id)
        except ValueError as exc:
            return _error("job_not_found", str(exc))
        record = jobs.read_job(resolved)
        if record is None:
            return _error("job_not_found", f"No job '{resolved}'.")

    payload = _summarize(record)

    if record.get("status") in jobs.ACTIVE_STATUSES:
        payload["message"] = (
            f"Job is still {record.get('status')} (phase: {record.get('phase')}). "
            f"Use codex_status('{record['id']}', wait=True) to wait for it."
        )
        payload["log_tail"] = jobs.read_log(record["id"], tail_lines=15)
        return json.dumps(payload, indent=2)

    payload.update({
        "output": record.get("output"),
        "structured_output": record.get("structured_output"),
        "verification": record.get("verification"),
        "usage": record.get("usage"),
    })
    if record.get("status") == "timeout":
        payload["message"] = (
            "Job hit its timeout. The output below is partial work salvaged "
            "before the deadline — treat it as incomplete."
        )
    return json.dumps(payload, indent=2)


@server.tool()
def codex_cancel(job_id: str) -> str:
    """Cancel a running job and stop its Codex process.

    Args:
        job_id: Job id or unique prefix.

    Returns:
        JSON confirming cancellation.
    """
    try:
        resolved = jobs.resolve_job_id(job_id)
    except ValueError as exc:
        return _error("job_not_found", str(exc))

    record = jobs.read_job(resolved)
    if record is None:
        return _error("job_not_found", f"No job '{resolved}'.")

    if record.get("status") in jobs.TERMINAL_STATUSES:
        return json.dumps({
            "job_id": resolved,
            "status": record["status"],
            "message": f"Job already finished ({record['status']}) — nothing to cancel.",
        }, indent=2)

    jobs.request_cancel(resolved)
    jobs.terminate_tree(record)
    jobs.reap_orphan_codex(record)
    jobs.append_log(resolved, "cancellation requested")

    # The worker records the terminal state on exit; if it was already gone,
    # reconcile settles it on the next read.
    time.sleep(0.5)
    updated = jobs.read_job(resolved) or {}
    return json.dumps({
        "job_id": resolved,
        "status": updated.get("status", "cancelled"),
        "message": "Cancelled. Any partial work is left in the working tree.",
    }, indent=2)


@server.tool()
def codex_models() -> str:
    """List the Codex models available on your account, with valid efforts.

    Read live from the Codex CLI, so it reflects reality rather than this
    server's assumptions — including models released after this server was
    written. Effort support is per-model: gpt-5.6-luna has no "ultra", and the
    5.4/5.5 family tops out at "xhigh".

    Returns:
        JSON catalog with efforts, defaults, and known-deprecated slugs.
    """
    catalog = models.describe(Config.CODEX_HOME)
    catalog["configured_default"] = {
        "model": Config.MODEL,
        "effort": Config.EFFORT,
    }
    return json.dumps(catalog, indent=2)


# ---------------------------------------------------------------------------
# Review (the original workflow, now on the job system)
# ---------------------------------------------------------------------------


@server.tool()
def codex_review(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
    model: str = "",
    effort: str = "",
    timeout: int = 0,
) -> str:
    """Review-only pass — find and report issues without changing anything.

    Codex runs read-only, reads the full files around each change, and produces
    prioritized findings (P0-P3).

    Args:
        project_dir: Absolute path to the project repository.
        base_branch: Branch or commit to compare against (default: "main").
        focus: "bugs", "security", "performance", or "all".
        context: Additional context (ticket description, acceptance criteria).
        model: Model slug. Defaults to CODEX_MODEL.
        effort: Reasoning effort. Defaults to CODEX_EFFORT.
        timeout: Seconds. Defaults to CODEX_TIMEOUT.

    Returns:
        JSON with job_id — collect the findings with codex_result.
    """
    return _launch(
        kind="review",
        project_dir=project_dir,
        model=model,
        effort=effort,
        write=False,
        timeout=timeout,
        base_branch=base_branch,
        focus=focus or Config.FOCUS,
        context=context,
    )


@server.tool()
def codex_review_and_fix(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
    model: str = "",
    effort: str = "",
    verify_command: str = "",
    verify_timeout: int = 0,
    timeout: int = 0,
) -> str:
    """Review code changes AND auto-fix clear-cut P0-P2 findings in one pass.

    Codex fixes only what it is confident about; anything ambiguous is reported
    with its question instead. P3 findings are never fixed. Changes are left in
    the working tree — Codex does not commit.

    Args:
        project_dir: Absolute path to the project repository.
        base_branch: Branch or commit to compare against (default: "main").
        focus: "bugs", "security", "performance", or "all".
        context: Additional context (ticket description, acceptance criteria).
        model: Model slug. Defaults to CODEX_MODEL.
        effort: Reasoning effort. Defaults to CODEX_EFFORT.
        verify_command: Shell command to run afterwards (e.g. "pytest -q").
        timeout: Seconds. Defaults to CODEX_TIMEOUT.

    Returns:
        JSON with job_id — collect the result with codex_result.
    """
    return _launch(
        kind="review_and_fix",
        project_dir=project_dir,
        model=model,
        effort=effort,
        write=True,
        timeout=timeout,
        base_branch=base_branch,
        focus=focus or Config.FOCUS,
        context=context,
        verify_command=verify_command,
        verify_timeout=verify_timeout,
    )


@server.tool()
def codex_fix(
    project_dir: str,
    findings: str,
    context: str = "",
    model: str = "",
    effort: str = "",
    verify_command: str = "",
    verify_timeout: int = 0,
    timeout: int = 0,
) -> str:
    """Fix specific approved findings (second pass after human review).

    Args:
        project_dir: Absolute path to the project repository.
        findings: The findings to fix (from codex_review, filtered by the user).
        context: Guidance on approach, constraints, or preferences.
        model: Model slug. Defaults to CODEX_MODEL.
        effort: Reasoning effort. Defaults to CODEX_EFFORT.
        verify_command: Shell command to run afterwards (e.g. "pytest -q").
        timeout: Seconds. Defaults to CODEX_TIMEOUT.

    Returns:
        JSON with job_id — collect the result with codex_result.
    """
    if not findings or not findings.strip():
        return _error("invalid_request", "findings is required.")
    return _launch(
        kind="fix",
        project_dir=project_dir,
        model=model,
        effort=effort,
        write=True,
        timeout=timeout,
        findings=findings,
        context=context,
        verify_command=verify_command,
        verify_timeout=verify_timeout,
    )


def _startup_warnings() -> None:
    """Report configuration problems once, at startup.

    Kept out of import for the same reason as _startup_sweep: Config.validate()
    reaches the live model catalog, which shells out to `codex debug models`.
    Merely importing this module — a test, a smoke check — should not spawn a
    subprocess or depend on codex being installed.
    """
    try:
        for warning in Config.validate():
            print(f"[codex-review-server] WARNING: {warning}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never block startup on a warning
        print(
            f"[codex-review-server] WARNING: config check failed: {exc}",
            file=sys.stderr,
        )


def _startup_sweep() -> None:
    """Settle jobs left unfinished by a previous session.

    Startup is when a prior server's workers are most likely to have died, and
    reconciliation both settles their records and reaps any codex they left
    running. Kept out of import so that merely importing this module has no
    side effects on real job state.
    """
    try:
        stale = jobs.sweep_orphans()
        if stale:
            print(
                f"[codex-review-server] settled {stale} unfinished job(s) from "
                f"a previous session",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - never block startup on housekeeping
        print(
            f"[codex-review-server] WARNING: orphan sweep failed: {exc}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _startup_warnings()
    _startup_sweep()
    server.run(transport="stdio")
