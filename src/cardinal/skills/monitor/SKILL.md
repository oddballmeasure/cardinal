---
name: monitor
description: Turn a group of identical error records into a GitHub issue a coding agent can act on.
---

# Monitor

Input: `/context/finding.json`: one fingerprint's error records (count, first and last seen,
a sample record with its traceback, related run ids).
Output: one `IssueDraft`.

- Title: the defect as observed behavior, e.g. "Intake crashes with KeyError on issues without a body".
- Body: what fails, the innermost frame in the repository's own code, how often, and the impact.
  Quote the exception line. Do not guess a fix you cannot see in the traceback.
- State only what the records show. The evidence block is appended for you; do not repeat it.
