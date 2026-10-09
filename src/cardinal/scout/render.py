"""The issue body, written by code from the structured proposal (Problem / Evidence / Expected /
Acceptance), so every filed issue has the same shape and only checked evidence."""

from cardinal.contracts.scout import Evidence, Proposal

FOOTERS = {
    "proposed": "Proposed by Cardinal's scout. Approve by swapping `{proposed}` for `{ready}`; reject by "
                "closing with a comment saying why. The scout reads both.",
    "auto": "Filed ready by Cardinal's scout: {why}.",
    "needs_human": "Cardinal's scout found a product decision, not work it may file on its own. Swap "
                   "`{needs_human}` for `{ready}` to have Cardinal do it, or close it with a comment.",
}


def fence(text: str, language: str = "") -> str:
    ticks = "```" if "```" not in text else "````"
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def body(proposal: Proposal, evidence: list[Evidence], review: str, mode: str, why: str, labels,
         fingerprint: str, test_diff: str | None) -> str:
    parts = ["## Problem", proposal.problem.strip(), "", "## Evidence"]
    for item in evidence:
        parts += [f"`{item.path}:{item.line_start}`" + (f"-{item.line_end}" if item.line_end != item.line_start else ""),
                  fence(item.quote), ""]
    parts += ["## Expected", proposal.expected.strip(), "", "## Acceptance",
              *(f"- [ ] {item}" for item in proposal.acceptance), "", "## Files", *(f"- `{path}`" for path in proposal.files)]
    if test_diff:
        parts += ["", "## Suggested test", "This test fails on the base branch and states the acceptance criteria.",
                  fence(test_diff, "diff")]
    footer = FOOTERS[mode].format(why=why, proposed=labels.proposed, ready=labels.ready, needs_human=labels.needs_human)
    parts += ["", "---", footer, f"Reviewer: {review.strip()}", f"Category: {proposal.category}",
              f"Cardinal-Scout: {fingerprint}"]
    return "\n".join(parts)
