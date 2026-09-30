"""Run a Codex turn over `codex app-server`, for Daybreak jobs only.

`codex exec` cannot request a cyber access program for a turn; the app-server
protocol can (`turn/start.cyberAccessProgram`). That field is experimental and
needs the `experimentalApi` capability, so this transport is confined to jobs
that need it (models.DAYBREAK_PROGRAM_MODELS). Everything else stays on exec.

The module is a translator, not a second runner. It speaks just enough JSON-RPC
to start or resume a thread and run one turn, and turns the server's
notifications into the same `codex exec --json` events codex_runner already
understands. Supervision, event handling, and failure classification are
codex_runner's, so the two transports cannot drift apart. If `exec` ever gains
an access-program flag, delete this module.

Verified against codex-cli 0.159.2.
"""

import json
import subprocess

from codex_runner import (
    CodexError,
    apply_event,
    build_env,
    find_codex_binary,
    finish_run,
    new_run_state,
    supervise,
)

# The protocol spells programs in camelCase; the catalog, the rollout and the
# API docs use snake_case, which is what the rest of this server carries.
_PROTOCOL_PROGRAMS = {
    "daybreak_blue": "daybreakBlue",
}

# app-server item types → the `codex exec --json` item types codex_runner reads.
_ITEM_TYPES = {
    "agentMessage": "agent_message",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "mcpToolCall": "mcp_tool_call",
    "reasoning": "reasoning",
    "webSearch": "web_search",
}


class _Closed(Exception):
    """app-server's stdout closed: it exited, or the watchdog killed it."""


class _Connection:
    """A JSON-RPC client over app-server's stdio."""

    def __init__(self, proc: subprocess.Popen, on_notification):
        self._proc = proc
        self._on_notification = on_notification
        self._next_id = 0

    def _send(self, message: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise _Closed() from exc

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict) -> dict:
        """Send a request and pump messages until its response arrives.

        An error response is how protocol drift shows up — a renamed method or
        field after a CLI upgrade — so it fails the job with the server's own
        words instead of being worked around.
        """
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params})
        while True:
            message = self.read()
            if message and message.get("id") == request_id:
                if "error" in message:
                    raise CodexError(
                        f"codex app-server rejected {method}: "
                        f"{json.dumps(message['error'])[:500]}"
                    )
                return message.get("result") or {}

    def read(self) -> dict | None:
        """Handle one message. Returns it if it is a response, else None.

        One message per call, never "until a response": the caller's loop
        condition often depends on a notification (turn/completed), and a read
        that swallowed it and kept waiting would block until the timeout.
        """
        line = self._proc.stdout.readline()
        if not line:
            raise _Closed()
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return None
        if "method" in message and "id" in message:
            # A server-to-client request: an approval prompt or an MCP
            # elicitation. The run is non-interactive, as under exec, so
            # nobody is there to answer.
            self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32601,
                "message": "Non-interactive client; nothing can answer this.",
            }})
            return None
        if "method" in message:
            self._on_notification(message["method"], message.get("params") or {})
            return None
        return message


def _snake_usage(breakdown: dict) -> dict:
    """tokenUsage.total, in `codex exec`'s turn.completed usage spelling.

    The thread's running total, as exec reports it too: a follow-up's usage
    includes the turns before it.
    """
    return {
        "input_tokens": breakdown.get("inputTokens", 0),
        "cached_input_tokens": breakdown.get("cachedInputTokens", 0),
        "cache_write_input_tokens": breakdown.get("cacheWriteInputTokens", 0),
        "output_tokens": breakdown.get("outputTokens", 0),
        "reasoning_output_tokens": breakdown.get("reasoningOutputTokens", 0),
    }


def _exec_item(item: dict) -> dict:
    translated = {"type": _ITEM_TYPES.get(item.get("type"), item.get("type"))}
    if item.get("type") == "commandExecution":
        translated["command"] = item.get("command", "")
        translated["exit_code"] = item.get("exitCode")
    return translated


def run_codex_app_server(
    project_dir: str,
    prompt: str,
    model: str,
    effort: str,
    sandbox: str,
    output_file: str,
    prompt_file: str,
    stderr_file: str,
    timeout: int,
    cyber_access_program: str,
    schema_file: str | None = None,
    resume_thread_id: str | None = None,
    on_event=None,
    on_spawn=None,
) -> dict:
    """run_codex's contract, over app-server, with a cyber program on the turn.

    The result also carries `rollout_path`, the thread's on-disk record, so the
    caller can confirm from codex's own log that the program was applied: the
    protocol never echoes it back.
    """
    with open(prompt_file, "w") as handle:
        handle.write(prompt)

    state = new_run_state()
    # Keyed by turn id: a fast failure can finish the turn before turn/start's
    # own response arrives, and it must not be missed.
    finished_turns = {}
    run = {"message": "", "rollout_path": None, "turn_status": None}

    def emit(event: dict) -> None:
        apply_event(event, state)
        if on_event:
            on_event(event, state)

    def on_notification(method: str, params: dict) -> None:
        # Sub-agents (`ultra`) run in threads of their own; only ours counts,
        # as under exec.
        if params.get("threadId") not in (None, state["thread_id"]):
            return
        if method == "thread/tokenUsage/updated":
            state["usage"] = _snake_usage(params.get("tokenUsage", {}).get("total", {}))
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                run["message"] = item.get("text", "")
            emit({"type": "item.completed", "item": _exec_item(item)})
        elif method == "error" and not params.get("willRetry"):
            emit({"type": "error", "message": params.get("error", {}).get("message", "")})
        elif method == "turn/completed":
            finished = params.get("turn", {})
            finished_turns[finished.get("id")] = finished

    with open(stderr_file, "w") as err_handle:
        proc = subprocess.Popen(
            [find_codex_binary(), "app-server"],
            cwd=project_dir,
            env=build_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_handle,
            text=True,
            # Codex leads its own process group; see codex_runner.supervise.
            start_new_session=True,
        )
        if on_spawn:
            on_spawn(proc.pid)

        with supervise(proc, timeout, state):
            try:
                conn = _Connection(proc, on_notification)
                conn.request("initialize", {
                    "clientInfo": {"name": "codex-delegate", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                conn.notify("initialized")

                # approvalPolicy "never" is what exec runs with: nobody is
                # there to approve anything.
                thread_params = {"cwd": project_dir, "sandbox": sandbox,
                                 "approvalPolicy": "never", "model": model}
                if resume_thread_id:
                    result = conn.request("thread/resume",
                                          {"threadId": resume_thread_id, **thread_params})
                else:
                    result = conn.request("thread/start", thread_params)
                thread = result.get("thread", {})
                run["rollout_path"] = thread.get("path")
                emit({"type": "thread.started", "thread_id": thread.get("id")})

                turn_params = {
                    "threadId": state["thread_id"],
                    "input": [{"type": "text", "text": prompt}],
                    "model": model,
                    "effort": effort,
                    "cyberAccessProgram": _PROTOCOL_PROGRAMS[cyber_access_program],
                }
                if schema_file:
                    with open(schema_file) as handle:
                        turn_params["outputSchema"] = json.load(handle)
                started = conn.request("turn/start", turn_params)
                turn_id = started.get("turn", {}).get("id")

                while turn_id not in finished_turns:
                    conn.read()
                run["turn_status"] = finished_turns[turn_id].get("status")
                if run["turn_status"] == "completed":
                    emit({"type": "turn.completed", "usage": state["usage"]})
                else:
                    emit({"type": "turn.failed",
                          "error": finished_turns[turn_id].get("error")
                          or {"message": f"Turn {run['turn_status']}."}})
            except _Closed:
                pass  # Settled below: a timeout, or an exit mid-turn.

            # EOF on stdin is app-server's cue to shut down.
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass  # supervise reaps it.

    if run["turn_status"] is not None:
        # The turn finished before the deadline. A watchdog that fired since
        # only cut app-server's shutdown short, which exec has no equivalent
        # of, so it must not turn a finished turn into a timeout.
        state["timed_out"] = False

    # Like exec's -o, the file holds the turn's final message and nothing else:
    # a turn cut off mid-way leaves interim commentary as its last message.
    with open(output_file, "w") as handle:
        handle.write(run["message"] if run["turn_status"] == "completed" else "")

    # app-server exits 0 whether or not the turn worked; the turn's status is
    # the verdict. An exit before the turn finished counts as a failure too.
    exit_code = 0 if run["turn_status"] == "completed" else (proc.returncode or 1)
    result = finish_run(state, exit_code, output_file, stderr_file,
                        schema_file, timeout)
    result["rollout_path"] = run["rollout_path"]
    return result

