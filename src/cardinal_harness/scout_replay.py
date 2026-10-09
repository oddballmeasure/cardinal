"""Scripted scout models for the offline `scout` scenario, keyed on the ledger fixture's areas and
proposal titles (lessons/profile-replay-specificity.md). Each proposal stands for one gate: a real
defect, a fabricated quote, a duplicate, a reviewer refusal, a product decision, and a bug whose
"failing" test passes on the base branch."""

from cardinal.contracts.scout import Area, Evidence, Proposal, ProposalReview, ReproResult, ScoutFindings, SurveyPlan
from cardinal_harness.replay_provider import Scripted, call

AREAS = [Area(name="input loading", paths=["ledger/records.py"], focus="How --input files are read"),
         Area(name="merchant filter", paths=["ledger/query.py"], focus="Selecting transactions by merchant"),
         Area(name="CLI entry point", paths=["ledger/__main__.py"], focus="Options, errors and output")]
RUN_MAIN = "ledger/__main__.py"
LOAD = Evidence(path="ledger/records.py", line_start=8, line_end=8, quote="return json.loads(path.read_text())")
CALL_LOAD = Evidence(path=RUN_MAIN, line_start=16, line_end=16, quote="rows = load_transactions(args.input)")
PRINT = Evidence(path=RUN_MAIN, line_start=17, line_end=17, quote="print(render_json(filter_merchant(rows, args.merchant)))")
MATCH = Evidence(path="ledger/query.py", line_start=7, line_end=7, quote='return [row for row in rows if row["merchant"] == merchant]')


def proposal(category: str, title: str, evidence: list[Evidence], acceptance: list[str], files: list[str]) -> Proposal:
    return Proposal(category=category, title=title, evidence=evidence, acceptance=acceptance, files=files,
                    problem=f"Replay finding: {title}. The cited lines show the behaviour today.",
                    expected="The CLI reports the case plainly and keeps its JSON output contract.")


MISSING_INPUT = proposal("bug", "Missing --input file exits with a Python traceback", [LOAD, CALL_LOAD],
                         ["`python -m ledger --input missing.json` exits with status 2",
                          'stderr names "missing.json" and contains no "Traceback"'], ["ledger/records.py", RUN_MAIN])
FABRICATED = proposal("bug", "Empty --merchant value is accepted silently",
                      [Evidence(path=RUN_MAIN, line_start=14, line_end=14, quote='parser.add_argument("--merchant", required=True)')],
                      ["`--merchant ''` exits with status 2"], [RUN_MAIN])
DUPLICATE = proposal("feature", "Add a --total flag", [PRINT], ['`--total` prints "total: 34.75"'], [RUN_MAIN])
CASE = proposal("bug", "Merchant filter misses differently cased merchant names", [MATCH],
                ['`--merchant cafe` prints 1 row with id "t-002"'], ["ledger/query.py"])
MIN_AMOUNT = proposal("feature", "Add --min-amount to keep transactions at or above an amount", [MATCH],
                      ['`--min-amount 10` prints 2 rows, ids "t-001" and "t-003"'], ["ledger/query.py", RUN_MAIN])
ACCENTS = proposal("feature", "Merchant matching ignores accents", [MATCH],
                   ['`--merchant Cafe` prints the row with id "t-002"'], ["ledger/query.py"])
MALFORMED = proposal("bug", "Malformed JSON input crashes ledger instead of reporting the parse error", [CALL_LOAD],
                     ["An --input file holding `{not json` exits with status 2",
                      'stderr contains "invalid JSON" and no "Traceback"'], ["ledger/records.py", RUN_MAIN])
UNKNOWN = proposal("bug", "Unknown merchant prints nothing instead of an empty JSON list", [PRINT],
                   ['`--merchant Nobody` prints "[]"'], [RUN_MAIN])
COUNT = proposal("feature", "Print the number of matched transactions with --count", [PRINT],
                 ['`--count` prints "3" for data/transactions.json'], [RUN_MAIN])

FINDINGS = {"input loading": [MISSING_INPUT, FABRICATED, DUPLICATE],
            "merchant filter": [CASE, MIN_AMOUNT, ACCENTS],
            "CLI entry point": [MALFORMED, UNKNOWN, COUNT]}
VERDICTS = {CASE.title: ("wrong", "ledger/query.py:7 matches merchants exactly, as test_merchant_selection expects."),
            ACCENTS.title: ("product_decision", "Whether Cafe should match Café is a choice about the product.")}

CLI_TEST = """import subprocess
import sys


def run(*args):
    return subprocess.run([sys.executable, "-m", "ledger", *args], text=True, capture_output=True, check=False)


"""
REPRO_TESTS = {
    MISSING_INPUT.title: ("tests/test_scout_missing_input.py", "test_missing_input_is_reported", CLI_TEST + (
        "def test_missing_input_is_reported():\n"
        "    result = run(\"--input\", \"missing.json\")\n"
        "    assert result.returncode == 2\n"
        "    assert \"missing.json\" in result.stderr and \"Traceback\" not in result.stderr\n")),
    MALFORMED.title: ("tests/test_scout_malformed.py", "test_malformed_input_is_reported", CLI_TEST + (
        "def test_malformed_input_is_reported(tmp_path):\n"
        "    bad = tmp_path / \"bad.json\"\n"
        "    bad.write_text(\"{not json\")\n"
        "    result = run(\"--input\", str(bad))\n"
        "    assert result.returncode == 2 and \"Traceback\" not in result.stderr\n")),
    UNKNOWN.title: ("tests/test_scout_unknown.py", "test_unknown_merchant_prints_empty_list", CLI_TEST + (
        "def test_unknown_merchant_prints_empty_list():\n"
        "    result = run(\"--input\", \"data/transactions.json\", \"--merchant\", \"Nobody\")\n"
        "    assert result.stdout.strip() == \"[]\"\n")),
}


def model(context: dict):
    stage = context["stage"]
    if stage == "scout_planner":
        return Scripted(messages=iter([
            call("read_file", {"file_path": "/skills/scout-planner/SKILL.md"}, "skill"),
            call("find_repo_context", {"query": "transaction input merchant selection CLI options"}, "context"),
            call("SurveyPlan", SurveyPlan(areas=AREAS).model_dump(), "plan"),
        ]))
    if stage == "scout":
        findings = ScoutFindings(proposals=FINDINGS[context["area"]["name"]])
        return Scripted(messages=iter([
            call("read_file", {"file_path": "/skills/scout/SKILL.md"}, "skill"),
            call("read_file", {"file_path": "/context/rejections.json"}, "rejections"),
            *(call("read_file", {"file_path": f"/repo/{path}"}, f"read-{index}")
              for index, path in enumerate(context["area"]["paths"])),
            call("ScoutFindings", findings.model_dump(), "findings"),
        ]))
    title = context["proposal"]["title"]
    if stage == "scout_reviewer":
        verdict, reason = VERDICTS.get(title, ("confirmed", "Traced the cited lines; the behaviour is as stated."))
        evidence = context["proposal"]["evidence"] if verdict == "confirmed" else []
        review = ProposalReview(verdict=verdict, reason=reason, evidence=evidence)
        return Scripted(messages=iter([
            call("read_file", {"file_path": "/skills/scout-reviewer/SKILL.md"}, "skill"),
            call("read_file", {"file_path": "/context/proposal.json"}, "proposal"),
            call("ProposalReview", review.model_dump(), "review"),
        ]))
    if stage == "scout_repro":
        path, name, text = REPRO_TESTS[title]
        return Scripted(messages=iter([
            call("read_file", {"file_path": "/skills/scout-repro/SKILL.md"}, "skill"),
            call("write_file", {"file_path": f"/repo/{path}", "content": text}, "write"),
            call("run_repo_tests", {}, "tests"),
            call("ReproResult", ReproResult(test_path=path, test_name=name, note="Replay test").model_dump(), "result"),
        ]))
    raise ValueError(f"No scout replay for stage {stage}")
