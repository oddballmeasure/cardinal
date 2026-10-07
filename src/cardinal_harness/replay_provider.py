"""Scripted models for the product's offline end-to-end runs, served through CARDINAL_MODEL_PROVIDER.

The product calls `provider(context)` once per agent call with the stage context it would give a
live model. Each script drives the real Deep Agents graph and the product's real tools; it proves
wiring and gates, never model reasoning. Scripts are keyed on the ledger fixture's issue titles
(lessons/profile-replay-specificity.md).
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from cardinal.contracts.intake import IntakeDecision, Requirement, Ticket
from cardinal.contracts.monitor import IssueDraft
from cardinal.contracts.profile import ProfileChunk, ProfileFile
from cardinal.contracts.verdict import VerifierAssessment

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "tests" / "replay"
FIXTURES = ROOT / "tests" / "fixtures"


class Scripted(GenericFakeChatModel):
    """A fixed sequence of tool calls that still runs through the real agent graph."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001, ANN201
        return self


class Reactive(Scripted):
    """Chooses each next call from the tool results so far, like a model reading its tools."""

    _policy: object = PrivateAttr()

    def __init__(self, policy) -> None:  # noqa: ANN001
        super().__init__(messages=iter([]))
        self._policy = policy

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN201
        return ChatResult(generations=[ChatGeneration(message=self._policy(messages))])


def call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def last_result(messages: list) -> dict | list | str:
    latest = next((message for message in reversed(messages) if isinstance(message, ToolMessage)), None)
    if latest is None:
        return {}
    try:
        return json.loads(latest.content)
    except (TypeError, ValueError):
        return str(latest.content)


def provider(context: dict):
    stage = context["stage"]
    land_elsewhere(context)
    if stage == "monitor":
        return monitor(context)
    title = context["issue"]["title"]
    return {"profiler": profiler, "orchestrator": orchestrator, "coder": coder, "verifier": verifier,
            "pr_manager": pr_manager, "deployer": deployer}[stage](context, title)


# --- the world moving on -----------------------------------------------------------------------

def land_elsewhere(context: dict) -> None:
    """Plays another PR merging into main while this run works: each landing in the JSON file named by
    CARDINAL_REPLAY_LANDINGS is committed on the remote's main once, when its issue reaches its stage.
    Landings: [{"issue": 11, "stage": "coder", "message": "...", "append": {path: text}, "write": {path: text}}]."""
    path = os.environ.get("CARDINAL_REPLAY_LANDINGS")
    if not path or "worktree" not in context:
        return
    landings = json.loads(Path(path).read_text())
    due = [item for item in landings if not item.get("landed") and item["issue"] == context["issue"]["number"]
           and item["stage"] == context["stage"]]
    if not due:
        return
    run = lambda *args, cwd=None: subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,  # noqa: E731
                                                 check=True).stdout.strip()
    bare = run("-C", context["worktree"], "remote", "get-url", "origin")
    for landing in due:
        with tempfile.TemporaryDirectory() as work:
            run("clone", "-q", "--branch", "main", bare, work)
            for name, text in landing.get("append", {}).items():
                with open(Path(work) / name, "a") as handle:
                    handle.write(text)
            for name, text in landing.get("write", {}).items():
                (Path(work) / name).write_text(text)
            run("add", "-A", cwd=work)
            run("-c", "user.name=Elsewhere", "-c", "user.email=elsewhere@example.invalid", "commit", "-qm",
                landing["message"], cwd=work)
            run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
            landing["landed"] = run("rev-parse", "HEAD", cwd=work)
    Path(path).write_text(json.dumps(landings, indent=2))


# --- profiler -----------------------------------------------------------------------------------

LEDGER_CAPABILITIES = {
    "ledger/query.py": (["transaction filtering", "merchant selection", "date filter target"], "active"),
    "ledger/formatters.py": (["active JSON transaction output", "CSV export target"], "active"),
    "ledger/legacy_csv.py": (["unused legacy CSV prototype"], "legacy"),
    "ledger/__main__.py": (["transaction CLI options", "input and output wiring", "total flag target"], "active"),
}


def profiler(context: dict, title: str):
    repo = Path(context["repo_path"])
    entries = []
    for path in context["batch"]:
        source = repo / path
        text = "" if source.is_symlink() or source.is_dir() else source.read_text(errors="replace")[:2048]
        first = (text.splitlines() or ["empty file"])[0].strip('"# ') or path
        capabilities, status = LEDGER_CAPABILITIES.get(path, ([first], "support" if path.startswith(("tests/", "data/", "deploy")) or not path.endswith(".py") else "active"))
        entries.append(ProfileFile(path=path, summary=first, capabilities=capabilities, status=status))
    return Scripted(messages=iter([
        call("read_file", {"file_path": "/skills/profiler/SKILL.md"}, "skill"),
        call("read_file", {"file_path": "/context/profile_batch.json"}, "manifest"),
        *(call("read_repo_file", {"path": path}, f"read-{index}") for index, path in enumerate(context["batch"])),
        call("ProfileChunk", ProfileChunk(files=entries).model_dump(), "chunk"),
    ]))


# --- orchestrator -------------------------------------------------------------------------------

def ticket(name: str, number: int, **changes) -> Ticket:
    return Ticket.model_validate({**json.loads((FIXTURES / name).read_text()), "source_issue": number, **changes})


def requirements(body: str) -> list[Requirement]:
    found = []
    for line in body.splitlines():
        line = line.strip().lstrip("- ")
        if line[:1] == "R" and ":" in line[:4]:
            key, _, text = line.partition(":")
            found.append(Requirement(id=key.strip(), text=text.strip()))
    return found


def decision_for(context: dict) -> IntakeDecision:
    issue = context["issue"]
    number, title = issue["number"], issue["title"]
    reqs = requirements(issue["body"])
    common = {"issue_number": number, "size": "s", "risks": [], "requirements": reqs}
    if title.startswith("Filter transactions by date and export CSV"):
        return IntakeDecision(kind="feature", reason="Two dependent CLI behaviors", tickets=[
            ticket("date_ticket.json", number), ticket("csv_ticket.json", number)], **common)
    if title.startswith("Filter transactions by inclusive date"):
        return IntakeDecision(kind="feature", reason="One CLI option", tickets=[
            ticket("date_ticket.json", number, id="DATE", covers=["R1", "R2"])], **common)
    if title.startswith("Export transactions as CSV"):
        return IntakeDecision(kind="feature", reason="Builds on the merged date filter", tickets=[
            ticket("csv_ticket.json", number, id="CSV", covers=["R1", "R2", "R3"], depends_on=[])], **common)
    if title.startswith("Purge transactions"):
        if context.get("human_answer"):
            return IntakeDecision(kind="reject", reason="The operator declined data deletion", **{**common, "requirements": []})
        return IntakeDecision(kind="needs_human", size="m", risks=["deletes user data"], requirements=[], tickets=[],
                              reason="This deletes transaction data. Should Cardinal delete records permanently?",
                              issue_number=number)
    if title.startswith("Archive transactions"):
        if not context.get("human_answer"):
            return IntakeDecision(kind="needs_human", size="s", risks=["deletes user data"], requirements=[], tickets=[],
                                  reason="Should archived transactions be removed from the main file?", issue_number=number)
        return IntakeDecision(kind="feature", reason=f"Operator answered: {context['human_answer']}", tickets=[Ticket(
            id="ARCHIVE", source_issue=number, title="Cover the archive behavior", task="Keep records; add a check.",
            acceptance_criteria=["The default listing is unchanged"], covers=["R1"], depends_on=[],
            target_files=["ledger/__main__.py"])], **common)
    if title.startswith("Summarise spending by merchant"):
        raise KeyError("merchant_totals")  # stands in for a defect in Cardinal itself
    if title.startswith("Ledger totals crash"):
        return IntakeDecision(kind="bug", reason="The traceback names the failing division", tickets=[Ticket(
            id="EMPTY-TOTAL", source_issue=number, title="Guard the empty total", task="Return 0 for no transactions.",
            acceptance_criteria=["An empty input totals 0"], covers=["R1"], depends_on=[],
            target_files=["ledger/query.py"])], **{**common, "requirements": [
                Requirement(id="R1", text="Totalling no transactions returns 0 instead of raising")]})
    if title.startswith("Intake crashes"):
        return IntakeDecision(kind="needs_human", size="m", risks=[], requirements=[], tickets=[], issue_number=number,
                              reason="The crash comes from the model provider hook; which component owns it?")
    if title.startswith("CI failed on main after"):
        return IntakeDecision(kind="bug", reason="A check failed on main after a merge", tickets=[Ticket(
            id="POST-MERGE-CI", source_issue=number, title="Fix the check that failed on main",
            task="Make the failing check pass at its cause.", acceptance_criteria=["The check passes"], covers=["R1"],
            depends_on=[], target_files=["tests/test_cli.py"])], **{**common, "requirements": [
                Requirement(id="R1", text="The check that failed after the merge passes on main")]})
    if title.startswith("Note "):  # parallel scenario: each appends one line to the README
        return IntakeDecision(kind="chore", reason="One README line", tickets=[Ticket(
            id=f"NOTE-{number}", source_issue=number, title=title, task="Append the line to README.md.",
            acceptance_criteria=["README.md ends with the line"], covers=["R1"], depends_on=[],
            target_files=["README.md"])], **{**common, "requirements": [Requirement(id="R1", text=title)]})
    if title.startswith("Add a total flag"):
        return IntakeDecision(kind="feature", reason="One CLI flag", tickets=[Ticket(
            id="TOTAL", source_issue=number, title="Print a total", task="Add --total printing the amount sum.",
            acceptance_criteria=["--total prints the sum"], covers=["R1"], depends_on=[],
            target_files=["ledger/__main__.py"])], **common)
    raise ValueError(f"No replay plan for issue {title!r}")


def orchestrator(context: dict, title: str):
    decision = decision_for(context)
    queries = ["inclusive date filtering transactions merchant selection", "CSV export transactions active JSON formatter"]
    return Scripted(messages=iter([
        call("read_file", {"file_path": "/skills/orchestrator/SKILL.md"}, "skill"),
        call("read_file", {"file_path": "/context/issue.json"}, "issue"),
        *(call("find_repo_context", {"query": query}, f"context-{index}") for index, query in enumerate(queries)),
        call("IntakeDecision", decision.model_dump(), "decision"),
    ]))


# --- monitor ------------------------------------------------------------------------------------

MONITOR_TITLES = {"example/ledger": "Ledger totals crash with ZeroDivisionError on empty input",
                  "example/cardinal": "Intake crashes with KeyError merchant_totals"}


def monitor(context: dict):
    draft = IssueDraft(title=MONITOR_TITLES[context["repo"]],
                       body="Replay draft: the grouped records show the same exception repeating; see the evidence.")
    return Scripted(messages=iter([
        call("read_file", {"file_path": "/skills/monitor/SKILL.md"}, "skill"),
        call("read_file", {"file_path": "/context/finding.json"}, "finding"),
        call("IssueDraft", draft.model_dump(), "draft"),
    ]))


# --- coder --------------------------------------------------------------------------------------

def writes_for(ticket_id: str) -> dict[str, str]:
    read = lambda name: (CANDIDATES / name).read_text()  # noqa: E731
    if ticket_id in {"ISSUE-104-DATE", "DATE"}:
        return {"ledger/query.py": read("query_date.py"), "ledger/__main__.py": read("date_only.py"),
                "tests/test_cli.py": read("date_tests.py")}
    if ticket_id in {"ISSUE-104-CSV", "CSV"}:
        return {"ledger/formatters.py": read("formatters_full.py"), "ledger/__main__.py": read("full.py"),
                "tests/test_cli.py": read("full_tests.py")}
    if ticket_id == "ARCHIVE":
        return {"tests/test_archive.py": (
            "import json\nimport subprocess\nimport sys\n\n\n"
            "def test_listing_keeps_every_record():\n"
            "    result = subprocess.run([sys.executable, '-m', 'ledger', '--input', 'data/transactions.json'],\n"
            "                            text=True, capture_output=True, check=True)\n"
            "    assert len(json.loads(result.stdout)) == 3\n")}
    if ticket_id.startswith("CI-REPAIR-"):
        return {"tests/test_ci_repair.py": "def test_ci_environment_fix_is_covered():\n    assert True\n"}
    if ticket_id == "POST-MERGE-CI":
        return {"tests/test_post_merge.py": "def test_browser_flow_is_stable():\n    assert True\n"}
    if ticket_id == "TOTAL":
        return {"tests/test_total.py": "def test_total_flag_is_printed():\n    assert False, 'total flag missing'\n"}
    raise ValueError(f"No replay candidate for ticket {ticket_id}")


CONFLICT = re.compile(r"^<<<<<<< [^\n]*\n(.*?)(?:^\|{7}[^\n]*\n.*?)?^=======\n(.*?)^>>>>>>> [^\n]*\n",
                      re.MULTILINE | re.DOTALL)


def resolve_conflicts(context: dict) -> dict[str, str]:
    """A SYNC ticket's conflicts, resolved as a careful person would in the incident's shape: when the
    other side only appended to the file, keep this side's file and add what was appended. Otherwise
    keep each hunk's two sides in turn."""
    worktree = Path(context["worktree"])
    show = lambda ref: subprocess.run(["git", "show", ref], cwd=worktree, text=True,  # noqa: E731
                                      capture_output=True, check=True).stdout
    base = subprocess.run(["git", "merge-base", "HEAD", "MERGE_HEAD"], cwd=worktree, text=True,
                          capture_output=True, check=True).stdout.strip()
    resolved = {}
    for path in context["ticket"]["target_files"]:
        original, theirs = show(f"{base}:{path}"), show(f"MERGE_HEAD:{path}")
        if theirs.startswith(original):
            resolved[path] = show(f"HEAD:{path}") + theirs[len(original):]
        else:
            resolved[path] = CONFLICT.sub(lambda match: match.group(1) + match.group(2), (worktree / path).read_text())
    return resolved


def coder(context: dict, title: str):
    ticket_id = context["ticket"]["id"]
    if ticket_id.startswith("SYNC-"):
        writes = resolve_conflicts(context)
    elif ticket_id.startswith("NOTE-"):
        readme = Path(context["worktree"]) / "README.md"
        number = context["issue"]["number"]
        writes = {"README.md": readme.read_text() + f"{title}.\n", f"tests/test_note_{number}.py": (
            "from pathlib import Path\n\n\n"
            f"def test_readme_notes_issue_{number}() -> None:\n"
            f"    assert {title + '.'!r} in Path('README.md').read_text().splitlines()\n")}
    else:
        writes = writes_for(ticket_id)
    return Scripted(messages=iter([
        call("read_file", {"file_path": "/skills/coder/SKILL.md"}, "skill"),
        call("read_file", {"file_path": "/context/ticket.json"}, "ticket"),
        *(call("write_file", {"file_path": f"/repo/{path}", "content": content}, f"write-{index}")
          for index, (path, content) in enumerate(writes.items())),
        call("run_repo_tests", {}, "tests"),
        AIMessage(content=f"Implemented {ticket_id} and ran the repository tests."),
    ]))


# --- verifier -----------------------------------------------------------------------------------

def verifier(context: dict, title: str):
    assessment = VerifierAssessment(head_sha=context["head_sha"], approved=True,
                                    requirements={item: True for item in context["requirement_ids"]},
                                    tests_are_e2e=True, findings=[])
    return Scripted(messages=iter([
        call("read_file", {"file_path": "/skills/verifier/SKILL.md"}, "skill"),
        call("read_file", {"file_path": "/context/revision.json"}, "revision"),
        call("read_file", {"file_path": "/context/patch.diff"}, "patch"),
        call("VerifierAssessment", assessment.model_dump(), "assessment"),
    ]))


# --- PR manager ---------------------------------------------------------------------------------

def pr_manager(context: dict, title: str):
    number = context["issue"]["number"]
    state: dict = {"step": 0, "pr": None}

    def policy(messages: list) -> AIMessage:
        step = state["step"]
        state["step"] += 1
        result = last_result(messages)
        if step == 0:
            return call("list_open_prs", {}, "list")
        if step == 1:
            if isinstance(result, list) and result:
                state["pr"] = result[0]["number"]
                return call("wait_for_ci", {"number": state["pr"]}, "ci")
            return call("create_pull_request", {"title": title, "body": f"Closes #{number}\n\nReplay change."}, "create")
        if state["pr"] is None and isinstance(result, dict) and "number" in result:
            state["pr"] = result["number"]
            return call("wait_for_ci", {"number": state["pr"]}, "ci")
        if isinstance(result, dict) and result.get("status") == "success" and not result.get("merged"):
            return call("merge_pull_request", {"number": state["pr"]}, "merge")
        return AIMessage(content=f"Finished with {result}.")

    return Reactive(policy)


# --- deployer -----------------------------------------------------------------------------------

def deployer(context: dict, title: str):
    state = {"step": 0}
    opening = [call("read_file", {"file_path": "/skills/deployment-manager/SKILL.md"}, "skill"),
               call("read_file", {"file_path": "/context/deployment/deploy.json"}, "config"),
               call("execute_deploy", {}, "deploy")]

    def policy(messages: list) -> AIMessage:
        step = state["step"]
        state["step"] += 1
        if step < len(opening):
            return opening[step]
        result = last_result(messages)
        finished = (not isinstance(result, dict) or result.get("healthy") is True or result.get("timed_out") is True
                    or ("healthy" not in result and result.get("exit_code", 0) != 0))
        return AIMessage(content="Recorded the deployment.") if finished else call("check_health", {}, f"health-{step}")

    return Reactive(policy)

