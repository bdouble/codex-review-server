"""Verification of delegated work.

Codex saying "done" is a claim, not a result. Both of the reference
implementations for this pattern either trust the model's self-report or
mandate verification in prose and never enforce it. This module checks the
claim against the repository: what git says actually changed, and whether the
project's own test command still passes.

Every check is grounded in observable state, never in Codex's narration.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from config import subprocess_env

# Ceiling for a caller's verify_command when they do not set one. Deliberately
# well under CODEX_TIMEOUT's 4500s: this runs a test suite, not an agent. Any
# caller whose suite needs longer passes verify_timeout — before that existed,
# this was an unreachable default that silently failed slow suites.
DEFAULT_VERIFY_TIMEOUT = 900


def _git(args: list[str], cwd: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a git command. Returns (ok, raw stdout). Never uses a shell.

    stdout is returned unstripped on purpose. `git status --porcelain` encodes
    status in the first two columns, so an unstaged edit begins with a space
    (" M calc.py"). Stripping here would eat that column and shift every path
    parse by one character.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    return result.returncode == 0, result.stdout


# Untracked files are listed individually rather than collapsed to their
# directory. Without this git prints one "?? drafts/" entry no matter what
# happens inside, so a task that created or rewrote files under an
# already-untracked directory registers as no change at all.
_STATUS_ARGS = ["status", "--porcelain", "-z", "--untracked-files=all"]


def _split_z(text: str) -> list[str]:
    """Fields of a NUL-separated git output, minus the trailing empty."""
    return [field for field in text.split("\0") if field]


def _parse_porcelain(text: str) -> dict[str, str]:
    """Parse `git status --porcelain -z` into {path: status}.

    -z is load-bearing, not a detail. Without it git C-quotes any path that is
    not plain ASCII — `café.py` prints as `"caf\\303\\251.py"` — and the escaped
    name matches nothing on disk, so the content hash reads `<absent>` both
    before and after and a real edit becomes invisible. -z emits raw,
    NUL-terminated paths that are never quoted.

    Rename and copy entries carry their source path as an extra field, which
    must be consumed or it parses as a bogus entry of its own.
    """
    fields = _split_z(text)
    entries = {}
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        status, path = field[:2], field[3:]
        if status[0] in "RC" or status[1] in "RC":
            # Source path follows; the destination is what exists now.
            index += 1
        entries[path] = status
    return entries


def _numstat(cwd: str) -> dict[str, str]:
    """Per-file {path: "added,deleted"} for tracked changes against HEAD.

    -z for the same reason as _parse_porcelain. A rename renders as an empty
    path field followed by the source and destination as two further fields.
    """
    ok, out = _git(["diff", "HEAD", "--numstat", "-z"], cwd)
    return _numstat_fields(out) if ok else {}


def _numstat_fields(text: str) -> dict[str, str]:
    """Parse `git diff --numstat -z` output. Split out so it can be tested
    against exact bytes rather than whatever git happens to emit."""
    fields = _split_z(text)
    stats = {}
    index = 0
    while index < len(fields):
        # maxsplit: the path is the rest of the field, tabs and all. A
        # bare split() turns "ta\tb.py" into a phantom entry named "ta".
        parts = fields[index].split("\t", 2)
        index += 1
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        if not path:
            path = fields[index + 1] if index + 1 < len(fields) else ""
            index += 2
        if path:
            stats[path] = f"{added},{deleted}"
    return stats


def _hash_tree(root: Path) -> str:
    """One hash over every file beneath `root` — relative path and bytes both.

    --untracked-files=all lists untracked files individually, so a directory
    entry now only reaches _hash_files for a tree git will not look inside: an
    untracked *embedded repository*, which porcelain collapses to `?? vendor/`
    however much changes in it. Calling that `<absent>`, as a plain "not a
    file" branch does, is the same blind spot in a smaller room — a task can
    rewrite every file in there and no signal moves.

    Walking it is affordable precisely because it is rare: anything large
    enough to hurt is normally gitignored, and gitignored paths never appear
    in porcelain at all.
    """
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            entry = Path(current) / name
            digest.update(str(entry.relative_to(root)).encode())
            try:
                digest.update(hashlib.sha256(entry.read_bytes()).digest())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


def _hash_files(root: str, paths) -> dict[str, str]:
    """Content hashes for the given paths, resolved against the repo root.

    Status and line counts both go blind on a file that was *already* dirty
    before the task: editing it again leaves porcelain at " M", and a
    same-line-count edit leaves numstat identical too. Either way the change
    is invisible — a missed detection plus a false "Codex may be claiming work
    it did not do" warning. Content hashes are the only signal that always
    moves when the bytes move.

    `root` must be the repository root, never the caller's project_dir: git
    prints every path relative to the root regardless of the directory it ran
    in, so joining onto a subdirectory yields /repo/src/src/foo.py and every
    hash silently reads <absent>.

    Only baseline-dirty and untracked paths are hashed, never the whole repo,
    so this stays cheap: a clean file cannot change without git noticing.

    Gitignored paths are a known gap: they never reach porcelain, so an edit to
    `.env` or `build/` is invisible here. Surfacing them would mean --ignored,
    which floods the report with __pycache__ and .venv on every run.
    """
    hashes = {}
    base = Path(root)
    for path in paths:
        target = base / path
        try:
            if target.is_file():
                hashes[path] = hashlib.sha256(target.read_bytes()).hexdigest()
            elif target.is_dir():
                hashes[path] = _hash_tree(target)
            else:
                hashes[path] = "<absent>"
        except OSError:
            hashes[path] = "<unreadable>"
    return hashes


def repo_root(project_dir: str) -> str:
    """Repository root containing project_dir, or "" if it is not in a work tree."""
    ok, out = _git(["rev-parse", "--show-toplevel"], project_dir)
    return out.strip() if ok else ""


def snapshot(project_dir: str) -> dict:
    """Capture repository state before a task runs."""
    is_repo, _ = _git(["rev-parse", "--is-inside-work-tree"], project_dir)
    if not is_repo:
        return {"is_repo": False}

    # Nothing requires project_dir to be the repository root, and every path
    # git reports is root-relative. Resolve the root once and anchor to it.
    root = repo_root(project_dir) or project_dir
    _, head = _git(["rev-parse", "HEAD"], root)
    ok, porcelain = _git(_STATUS_ARGS, root)
    entries = _parse_porcelain(porcelain) if ok else {}
    return {
        "is_repo": True,
        "root": root,
        "head": head.strip(),
        "entries": entries,
        "numstat": _numstat(root),
        "hashes": _hash_files(root, entries.keys()),
        "git_ok": ok,
    }


def _changed_files(root: str, before: dict, after_entries: dict,
                   after_numstat: dict, head_before: str,
                   head_after: str) -> list[str]:
    """Files the task actually touched, per git.

    Four signals, because no single one is sufficient:
      - porcelain delta: new, deleted, and newly-modified files
      - numstat delta: further edits to already-tracked-dirty files
      - content hashes: edits that move no line counts, and edits to files
        that were already untracked (which never appear in numstat at all)
      - HEAD diff: work the task committed itself, which would otherwise leave
        a clean tree and look like nothing happened

    Paths are root-relative throughout, so `root` is the repository root.
    """
    before_entries = before.get("entries", {})
    changed = {
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    }

    before_numstat = before.get("numstat", {})
    changed.update(
        path
        for path in set(before_numstat) | set(after_numstat)
        if before_numstat.get(path) != after_numstat.get(path)
    )

    before_hashes = before.get("hashes", {})
    if before_hashes:
        after_hashes = _hash_files(root, before_hashes.keys())
        changed.update(
            path
            for path in before_hashes
            if before_hashes[path] != after_hashes.get(path)
        )

    if head_before and head_after and head_before != head_after:
        ok, names = _git(
            ["diff", "--name-only", "-z", f"{head_before}..{head_after}"], root
        )
        if ok:
            changed.update(_split_z(names))

    return sorted(changed)


def run_verify_command(command: str, project_dir: str,
                       timeout: int = DEFAULT_VERIFY_TIMEOUT) -> dict:
    """Run the caller's verification command (tests, lint, build).

    Uses a shell so ordinary command lines (`pytest -q && ruff check`) work.

    Two things worth being explicit about:

    - This runs on the host with your permissions, NOT inside Codex's sandbox.
      `write=False` constrains *Codex*; it does not constrain this command. The
      command comes from the caller — the same trust level as the MCP server
      itself — not from the model, so Codex cannot inject one. Don't pass a
      command you wouldn't run yourself.
    - It runs *after* file changes are computed, deliberately. Test commands
      routinely write caches (`__pycache__`, `.pytest_cache`), and attributing
      those to Codex would produce false "read-only violated" reports.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_dir,
            env=subprocess_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "output_tail": f"Verification command timed out after {timeout}s.",
        }
    except OSError as exc:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "output_tail": f"Could not run verification command: {exc}",
        }

    combined = (result.stdout + result.stderr).strip()
    tail = "\n".join(combined.splitlines()[-40:])
    return {
        "command": command,
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "output_tail": tail,
    }


def verify(
    project_dir: str,
    before: dict,
    write: bool,
    verify_command: str = "",
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT,
) -> dict:
    """Compare post-task repository state against the pre-task snapshot.

    Returns a report with individual checks and an overall `verified` verdict.
    `verified` is False only when a check actively fails — a warning (e.g. a
    write task that changed nothing) is surfaced but does not fail the job,
    since some write-mode tasks legitimately conclude no change is needed.
    """
    checks = []

    if not before.get("is_repo"):
        # A read-only task in an unversioned directory is constrained by the
        # sandbox, so "could not check" is a fair skip. A write task is not:
        # nothing observed what it did, and git cannot undo any of it either.
        # Reporting verified:true there would be a claim we never checked.
        checks.append({
            "name": "git_tracking",
            "status": "fail" if write else "skip",
            "detail": (
                "Not a git repository, so the files this task changed could "
                "not be verified — and its edits are not recoverable with git. "
                "Treat its file claims as unchecked."
                if write
                else "Not a git repository — file changes could not be verified."
            ),
        })
        report = {"git": {"is_repo": False}, "checks": checks}
    else:
        root = before.get("root") or project_dir
        _, head_after_raw = _git(["rev-parse", "HEAD"], root)
        head_after = head_after_raw.strip()
        ok, porcelain = _git(_STATUS_ARGS, root)
        after_entries = _parse_porcelain(porcelain) if ok else {}
        after_numstat = _numstat(root)
        head_before = before.get("head", "")

        # "Could not inspect" must never silently become "nothing changed" —
        # that would report verified:true for a job we failed to verify at all.
        if not ok or not before.get("git_ok", True):
            checks.append({
                "name": "git_tracking",
                "status": "fail",
                "detail": (
                    "git status failed, so file changes could not be verified. "
                    "Treat this job's file claims as unchecked."
                ),
            })

        files = _changed_files(
            root, before, after_entries, after_numstat,
            head_before, head_after
        )
        committed = bool(head_before and head_after and head_before != head_after)

        diff_stat = ""
        if committed:
            _, diff_stat = _git(
                ["diff", "--stat", f"{head_before}..{head_after}"], root
            )
        elif files:
            _, diff_stat = _git(["diff", "--stat"], root)
        diff_stat = diff_stat.strip()

        report = {
            "git": {
                "is_repo": True,
                "head_before": head_before,
                "head_after": head_after,
                "committed": committed,
                "files_changed": files,
                "diff_stat": diff_stat,
            },
            "checks": checks,
        }

        if write:
            if files:
                checks.append({
                    "name": "files_changed",
                    "status": "pass",
                    "detail": f"{len(files)} file(s) changed per git.",
                })
            else:
                checks.append({
                    "name": "files_changed",
                    "status": "warn",
                    "detail": (
                        "Task ran with write access but git shows no changes. "
                        "Either it concluded no change was needed, or it reported "
                        "work it did not do — read the output before trusting it."
                    ),
                })
        else:
            if files:
                checks.append({
                    "name": "read_only_respected",
                    "status": "fail",
                    "detail": (
                        f"Task ran read-only but {len(files)} file(s) changed: "
                        f"{', '.join(files[:10])}. Investigate before proceeding."
                    ),
                })
            else:
                checks.append({
                    "name": "read_only_respected",
                    "status": "pass",
                    "detail": "No files changed, as expected for a read-only task.",
                })

    if verify_command:
        outcome = run_verify_command(verify_command, project_dir, verify_timeout)
        checks.append({
            "name": "verify_command",
            "status": "pass" if outcome["passed"] else "fail",
            "detail": f"`{verify_command}` exited {outcome['exit_code']}.",
            "output_tail": outcome["output_tail"],
        })

    report["verified"] = not any(c["status"] == "fail" for c in checks)
    report["warnings"] = [c["detail"] for c in checks if c["status"] == "warn"]
    return report
