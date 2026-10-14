"""A stand-in for the `claude` CLI, for offline runs of the `claude-code:` engine.

It speaks what the claude_agent_sdk speaks over stdin/stdout: stream-json lines, plus control requests
and responses. It answers `initialize`, announces a session on the subscription (`apiKeySource`
"none"), then plays one scripted agent per stage: every file and role tool call is a real `mcp_message`
control request to the SDK's in-process `cardinal` MCP server, with the assistant `tool_use` and user
`tool_result` messages a real session would stream. It ends with a `result` message that carries
`structured_output` when the call asked for a --json-schema. Scripts prove wiring, never model
reasoning (like replay_provider); they are keyed on the stage named in the prompt ("Use the X skill").

Every invocation appends to the JSONL file named by CARDINAL_FAKE_CLAUDE_LOG: the model, the stage, the
--json-schema it received, the MCP tool list it was served and each tool call it made, plus a `fault`
record (written before anything else) when a scripted fault fires. CARDINAL_FAKE_CLAUDE_HIGH_STAGE names a
stage whose first session reports the five-hour window 60% used, resetting two seconds ahead; every later
stage session reports 10%, so a call stopped at an operator ceiling of 0.5 succeeds once it is retried.
The product reaches this file through CARDINAL_CLAUDE_CLI_PATH (the SDK ships a bundled CLI and would
otherwise ignore PATH). Only the standard library is used, so the stand-in starts fast.
"""

import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

SERVER = "cardinal"
PREFIX = f"mcp__{SERVER}__"
SESSION = "fake-claude-session"
README_LINE = "Use `{command}` to view usage information."


class Channel:
    """stdin and stdout of the SDK's stream-json protocol."""

    def __init__(self) -> None:
        self.users: queue.Queue = queue.Queue()
        self.waiting: dict[str, queue.Queue] = {}
        self.write_lock = threading.Lock()
        self.counter = 0
        self.closed = threading.Event()  # set when the SDK closes stdin
        threading.Thread(target=self.read, daemon=True).start()

    def send(self, message: dict) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def read(self) -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "control_request":
                self.answer(message)
            elif kind == "control_response":
                response = message.get("response") or {}
                waiter = self.waiting.get(response.get("request_id"))
                if waiter is not None:
                    waiter.put(response)
            elif kind == "user":
                self.users.put(message)
        self.users.put(None)
        self.closed.set()
        for waiter in list(self.waiting.values()):
            waiter.put({"subtype": "error", "error": "stdin closed"})

    def answer(self, message: dict) -> None:
        payload: dict = {}
        if (message.get("request") or {}).get("subtype") == "initialize":
            payload = {"commands": [], "output_style": "default", "available_output_styles": ["default"],
                       "models": [], "account": {}}
        self.send({"type": "control_response", "response": {
            "subtype": "success", "request_id": message.get("request_id"), "response": payload}})

    def mcp(self, method: str, params: dict | None = None, timeout: float = 7200) -> dict:
        """One JSON-RPC request to the SDK's in-process MCP server; returns its result."""
        self.counter += 1
        request_id = f"fake_{self.counter}"
        waiter: queue.Queue = queue.Queue()
        self.waiting[request_id] = waiter
        self.send({"type": "control_request", "request_id": request_id, "request": {
            "subtype": "mcp_message", "server_name": SERVER,
            "message": {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}}}})
        response = waiter.get(timeout=timeout)
        del self.waiting[request_id]
        if response.get("subtype") == "error":
            raise RuntimeError(f"{method}: {response.get('error')}")
        payload = response.get("response") or {}
        reply = payload.get("mcp_response", payload)
        if reply.get("error"):
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result") or {}


def prefix_items(node, path: str = "") -> list[str]:
    """Where a schema uses `prefixItems`, which the CLI's strict mode refuses."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += [f"{path}/{key}"] if key == "prefixItems" else []
            found += prefix_items(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += prefix_items(value, f"{path}/{index}")
    return found


def scripted_fault(log: Path | None, stage: str) -> str | None:
    """A fault named for this stage by the environment, on the stage's first invocation only: the retry
    of the same call must find the CLI healthy. `api_key` reports an API-key session; `rate_limit` sends a
    rejected rate-limit event whose window reopens in two seconds; `high_usage` sends an allowed event
    with the five-hour window 60% used, resetting in two seconds, and goes on if the engine does not stop."""
    for variable, kind in (("CARDINAL_FAKE_CLAUDE_API_KEY_STAGE", "api_key"), ("CARDINAL_FAKE_CLAUDE_LIMIT_STAGE", "rate_limit"),
                           ("CARDINAL_FAKE_CLAUDE_HIGH_STAGE", "high_usage")):
        if os.environ.get(variable) != stage or log is None:
            continue
        seen = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if not any(item["event"] == "fault" and item["stage"] == stage and item.get("kind") == kind for item in seen):
            return kind
    return None


def flags(argv: list[str]) -> dict:
    found: dict = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--") and index + 1 < len(argv) and not (argv[index + 1].startswith("--") and arg != "--"):
            found[arg] = argv[index + 1]
            index += 2
        else:
            found.setdefault(arg, True)
            index += 1
    return found


def plain(text: str) -> str:
    """A read_file result without its line numbers."""
    if text == "(empty file)":
        return ""
    lines = text.split("\n")
    # Numbers are right-aligned in a fixed-width column followed by one separator character; the
    # first line's "1" and its separator give the width to cut from every line.
    first = re.match(r"^(\s*1)(\D)", lines[0])
    if first is None:
        raise ValueError(f"unrecognised read_file output: {text[:120]!r}")
    cut = len(first.group(1)) + len(first.group(2))
    return "\n".join(line[cut:] for line in lines)


def requirements_in(body: str) -> list[dict]:
    found = []
    for line in body.splitlines():
        line = line.strip().lstrip("- ")
        if line[:1] == "R" and ":" in line[:4]:
            key, _, text = line.partition(":")
            found.append({"id": key.strip(), "text": text.strip()})
    return found


def readme_line(requirements: list[dict], title: str) -> str:
    for item in requirements:
        match = re.search(r"`([^`]+)`", item["text"])
        if match:
            return README_LINE.format(command=match.group(1))
    return title + "."


class Session:
    def __init__(self, channel: Channel, model: str, stage: str, log: Path | None) -> None:
        self.channel, self.model, self.stage, self.log = channel, model, stage, log
        self.turns = 0

    def record(self, event: str, **data) -> None:
        if self.log is not None:
            with self.log.open("a") as handle:
                handle.write(json.dumps({"event": event, "pid": os.getpid(), "stage": self.stage,
                                   "time": time.time(), **data}) + "\n")

    def say(self, message: dict) -> None:
        self.channel.send({**message, "parent_tool_use_id": None, "session_id": SESSION})

    def tool(self, name: str, args: dict | None = None) -> str:
        args = args or {}
        self.turns += 1
        use_id = f"toolu_{self.turns:04d}"
        self.say({"type": "assistant", "message": {"role": "assistant", "model": self.model, "content": [
            {"type": "tool_use", "id": use_id, "name": PREFIX + name, "input": args}]}})
        result = self.channel.mcp("tools/call", {"name": name, "arguments": args})
        text = "".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")
        self.say({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": bool(result.get("isError"))}]}})
        self.record("tool_call", tool=name, args=json.dumps(args)[:300], result=text[:300])
        return text

    def read(self, path: str) -> str:
        return plain(self.tool("read_file", {"file_path": path}))

    def json_file(self, path: str):
        text = self.read(path)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{path} is not JSON ({exc}): {text[:120]!r}") from exc

    def finish(self, text: str, structured: dict | None = None) -> None:
        self.say({"type": "assistant", "message": {"role": "assistant", "model": self.model,
                                                    "content": [{"type": "text", "text": text}]}})
        result = {"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
                  "num_turns": self.turns + 1, "session_id": SESSION, "result": text, "total_cost_usd": 0.0,
                  "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}}
        if structured is not None:
            result["structured_output"] = structured
        self.channel.send(result)


# --- scripted stages: the ledger fixture's README issues -----------------------------------------

def profiler(run: Session) -> None:
    run.read("/skills/profiler/SKILL.md")
    batch = [item["path"] for item in run.json_file("/context/profile_batch.json")]
    files = []
    for path in batch:
        text = run.tool("read_repo_file", {"path": path})
        first = next((line.partition(": ")[2].strip('"# ') for line in text.splitlines() if line.partition(": ")[2].strip('"# ')),
                     path)
        support = path.startswith(("tests/", "data/", "deploy")) or not path.endswith(".py")
        files.append({"path": path, "summary": first, "capabilities": ["documentation and usage" if path == "README.md" else first],
                      "status": "support" if support else "active"})
    run.finish("Profiled the batch.", {"files": files})


def orchestrator(run: Session) -> None:
    run.read("/skills/orchestrator/SKILL.md")
    issue = run.json_file("/context/issue.json")
    run.tool("find_repo_context", {"query": "README documentation usage"})
    reqs = requirements_in(issue["body"])
    ticket = {"id": f"NOTE-{issue['number']}", "source_issue": issue["number"], "title": issue["title"],
              "task": "Append the requested line to README.md.", "acceptance_criteria": ["README.md ends with the line"],
              "covers": [item["id"] for item in reqs], "depends_on": [], "target_files": ["README.md"]}
    run.finish("Planned one README ticket.", {
        "issue_number": issue["number"], "kind": "chore", "reason": "One README line", "size": "s", "risks": [],
        "requirements": reqs, "tickets": [ticket]})


def coder(run: Session) -> None:
    run.read("/skills/coder/SKILL.md")
    run.read("/context/ticket.json")
    issue = run.json_file("/context/issue.json")
    reqs = run.json_file("/context/requirements.json")
    current = run.read("/repo/README.md")
    line = readme_line(reqs, issue["title"])
    seen = [item.strip() for item in current.splitlines() if item.strip()]
    if line not in seen:
        # Append after the last line with an edit, so no line is rewritten; fall back to a whole-file write.
        done = run.tool("edit_file", {"file_path": "/repo/README.md", "old_string": seen[-1],
                                      "new_string": seen[-1] + "\n" + line}) if seen else ""
        if not done.startswith("Edited"):
            run.tool("write_file", {"file_path": "/repo/README.md",
                                    "content": "\n".join(seen + [line]) + "\n"})
    run.tool("run_repo_tests")
    run.finish("Appended the README line and ran the repository tests.")


def verifier(run: Session) -> None:
    run.read("/skills/verifier/SKILL.md")
    revision = run.json_file("/context/revision.json")
    run.read("/context/patch.diff")
    decision = run.json_file("/context/decision.json")
    issue = run.json_file("/context/issue.json")
    readme = run.read("/repo/README.md")
    delivered = readme_line(decision["requirements"], issue["title"]) in [item.strip() for item in readme.splitlines()]
    run.finish("Judged the branch.", {
        "head_sha": revision["head_sha"], "approved": delivered,
        "requirements": {item["id"]: delivered for item in decision["requirements"]}, "tests_are_e2e": False,
        "findings": [] if delivered else ["The requested README line is missing"]})


def pr_manager(run: Session) -> None:
    run.read("/skills/pr-manager/SKILL.md")
    issue = run.json_file("/context/issue.json")
    listed = json.loads(run.tool("list_open_prs"))
    if listed:
        number = listed[0]["number"]
    else:
        number = json.loads(run.tool("create_pull_request", {
            "title": issue["title"], "body": f"Closes #{issue['number']}\n\nREADME change."}))["number"]
    ci = json.loads(run.tool("wait_for_ci", {"number": number}))
    if ci.get("status") == "success":
        run.tool("merge_pull_request", {"number": number})
    run.finish(f"Finished with CI {ci.get('status')}.")


def deployer(run: Session) -> None:
    run.read("/skills/deployment-manager/SKILL.md")
    run.read("/context/deployment/deploy.json")
    result = json.loads(run.tool("execute_deploy"))
    if result.get("exit_code", 0) == 0:  # a failed script is never polled
        while True:
            result = json.loads(run.tool("check_health"))
            if result.get("healthy") or result.get("timed_out"):
                break
    run.finish("Recorded the deployment.")


STAGES = {"profiler": profiler, "orchestrator": orchestrator, "coder": coder, "verifier": verifier,
          "pr-manager": pr_manager, "deployment-manager": deployer}


def prompt_of(channel: Channel, options: dict) -> str:
    if isinstance(options.get("--"), str):
        return options["--"]
    message = channel.users.get(timeout=120)
    content = (message or {}).get("message", {}).get("content", "")
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if block.get("type") == "text")
    return content


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["-v"], ["--version"]):
        print("2.1.0 (Claude Code)")
        return 0
    options = flags(argv)
    schema = options.get("--json-schema")
    if isinstance(schema, str):
        schema = json.loads(schema)
        schema = schema.get("schema", schema) if schema.get("type") == "json_schema" else schema
    model = options.get("--model") if isinstance(options.get("--model"), str) else "fake-model"
    channel = Channel()
    prompt = prompt_of(channel, options)
    found = re.search(r"Use the ([\w-]+) skill", prompt)
    name = found.group(1) if found else "probe"
    log = Path(os.environ["CARDINAL_FAKE_CLAUDE_LOG"]) if os.environ.get("CARDINAL_FAKE_CLAUDE_LOG") else None
    run = Session(channel, model, name.replace("-", "_").replace("deployment_manager", "deployer"), log)
    fault = scripted_fault(log, run.stage)
    resets_at = time.time() + 2
    if fault:
        # Written first: the SDK may end this process as soon as it sees the fault.
        run.record("fault", kind=fault, **({"resets_at": resets_at} if fault == "high_usage" else {}))
    channel.send({"type": "system", "subtype": "init", "cwd": os.getcwd(),
                  "apiKeySource": "ANTHROPIC_API_KEY" if fault == "api_key" else "none",
                  "session_id": SESSION, "tools": [], "mcp_servers": [{"name": SERVER, "status": "connected"}],
                  "model": model, "permissionMode": "default", "slash_commands": [], "output_style": "default"})
    if not fault and os.environ.get("CARDINAL_FAKE_CLAUDE_QUIET_STAGE") != run.stage:
        # What a subscription session reports: an allowed event for the five-hour window at 17% used
        # (10% for stage sessions once a high-usage stage is scripted, so the window has visibly fallen
        # below any ceiling; `repos check`'s probe always sees 17%).
        used = 0.1 if os.environ.get("CARDINAL_FAKE_CLAUDE_HIGH_STAGE") and name != "probe" else 0.17
        channel.send({"type": "rate_limit_event", "uuid": "fake-rate-limit-allowed", "session_id": SESSION,
                      "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "utilization": used,
                                          "unifiedWindows": {"five_hour": {"utilization": used}}}})
    if fault == "rate_limit":
        channel.send({"type": "rate_limit_event", "uuid": "fake-rate-limit", "session_id": SESSION,
                      "rate_limit_info": {"status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour"}})
    if fault == "high_usage":
        channel.send({"type": "rate_limit_event", "uuid": "fake-rate-limit-high", "session_id": SESSION,
                      "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.6,
                                          "unifiedWindows": {"five_hour": {"utilization": 0.6, "resetsAt": resets_at}}}})
        if channel.closed.wait(3):
            return 0  # stopped at the operator's ceiling
        fault = None  # no ceiling: the session carries on as a healthy one
    if fault and channel.closed.wait(15):
        return 0  # the engine stopped the session, as it must, before any tool or result
    try:
        tools = []
        if name != "probe":
            channel.mcp("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "fake-claude", "version": "0"}})
            tools = channel.mcp("tools/list").get("tools", [])
        tool_schemas = {item["name"]: item.get("inputSchema") for item in tools}
        strict = prefix_items({"output_format": schema, **tool_schemas})
        run.record("invocation", model=model, json_schema=schema, prompt=prompt[:200], args=[a for a in argv if a.startswith("--")],
                   tools=[item["name"] for item in tools], tool_schemas=tool_schemas, prefix_items=strict)
        if strict:  # what the real CLI's strict mode does with a schema it cannot represent
            raise ValueError(f"strict schema validation failed: prefixItems is not supported at {strict}")
        if name == "probe":
            run.finish("ok")
        else:
            STAGES[name](run)
    except Exception as exc:  # noqa: BLE001 - reported to the SDK as a failed session
        channel.send({"type": "result", "subtype": "error_during_execution", "duration_ms": 1, "duration_api_ms": 1,
                      "is_error": True, "num_turns": run.turns, "session_id": SESSION,
                      "result": f"fake claude: {type(exc).__name__}: {exc}"})
    return 0


def install(bin_dir: Path) -> Path:
    """Write a `claude` executable that runs this module with the harness's interpreter."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "claude"
    target.write_text(f"#!{sys.executable}\nimport sys\nfrom cardinal_harness.fake_claude import main\nsys.exit(main())\n")
    target.chmod(0o755)
    return target


if __name__ == "__main__":
    sys.exit(main())
