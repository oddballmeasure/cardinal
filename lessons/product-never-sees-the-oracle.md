The acceptance tests that grade a run must be unreachable by the agents being graded.

Before the product split, the live coder's only test tool (`run_fixture_tests`) and the
verifier's `run_issue_acceptance` both ran `tests/acceptance`, the harness's hidden answer key.
A live pass then proved that agents could satisfy the grader's tests when shown them, not that
they could deliver the issue. The product now tests only with the target repository's own
`test_command`; acceptance runs in the harness after the merge, against a fresh clone.

Guard it with a check, not with care: `grep -r "cardinal_harness\|tests/acceptance" src/cardinal`
must find nothing, and replay scripts reach the product only through `CARDINAL_MODEL_PROVIDER`.
