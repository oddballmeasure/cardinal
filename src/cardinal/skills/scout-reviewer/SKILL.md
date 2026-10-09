---
name: scout-reviewer
description: Independently re-derive one scout proposal from the code and decide whether it should be filed.
---

# Scout reviewer

Input: `/context/proposal.json`, `/context/issues.json` and the repository at `/repo/`
(read-only). Output: one `ProposalReview`. Assume the proposal is wrong until the code shows
otherwise; do not trust its quotes or line numbers.

Verdicts:
- `confirmed`: you traced the code path yourself and the problem, expected behaviour and
  acceptance criteria all hold. Return the evidence you found (exact quotes, correct lines).
- `wrong`: the code does not behave as claimed, or the "defect" is intended behaviour.
- `product_decision`: the proposal asks what the product should do, not what is broken.
- `too_big`: more than one behaviour, or beyond a small change with the repository's own tests.
- `duplicate`: an open issue in `issues.json` already covers it. A closed issue whose behaviour
  is still in the code is a regression, not a duplicate.

Documentation that contradicts the code is a real defect; a docs-only fix needs no test.

`reason`: one or two sentences a person can check, naming the file and line that decided it.
