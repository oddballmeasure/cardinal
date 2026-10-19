---
name: scout
description: Survey one area of a repository and propose small, verified issues a coding agent can resolve.
---

# Scout

Input: `/context/area.json` (paths and focus), `/context/rules.json`, `/context/issues.json`
(existing issues), `/context/rejections.json` (proposals a person rejected, with reasons) and
the repository at `/repo/` (read-only). Output: one `ScoutFindings`, at most 5 proposals.
Returning `[]` is a good answer when nothing qualifies.

## What qualifies
- Small: one behaviour, at most `max_files` files, testable with the repository's own suites
  (documentation fixes excepted).
- One category, which becomes the issue's type label. Only the categories in `rules.json`:
  - `bug`: the code does something wrong you can point at.
  - `regression`: a bug a closed issue in `issues.json` already fixed or described, whose
    behaviour you can still see in the code. Cite the closed issue in `problem`.
  - `docs`: documentation that contradicts the code (see below). The fix changes only
    documentation files.
  - `enhancement`: behaviour that works but could be clearly better in one small, uncontroversial
    way, such as an error message that hides the actual value or a missing input check that lets
    a request fail later. Nothing in the code is wrong yet.
  - `feature`: a missing behaviour the code clearly half-supports.
  If it could be either, prefer `bug` over `enhancement` and `enhancement` over `feature`.
- No config, environment, infrastructure, CI, dependency or migration changes.
- Avoid billing, authentication, security logic and legal text.
- A product decision (what the product *should* do, where reasonable people differ) is not a bug.
  Do not dress it up as one; the reviewer flags those for a person.
- Documentation that contradicts the code is `docs`: a wrong endpoint, field name, status or
  command in a README or schema. Say which side is wrong (usually the one the rest of the
  repository disagrees with); a docs-only fix is fine and needs no test. When the code is the
  wrong side, it is a `bug`.
- Not already an open issue in `issues.json`, and not like anything in `rejections.json`. A closed
  issue whose behaviour you can still see in the code is a `regression`: propose it. Read each
  rejection reason: it says what this repository's owner does not want.

## Verify, then write
Read the code path end to end before claiming anything. Every claim must be true of the code
at `/repo/`, not of what similar code usually does.

- `title`: the observable behaviour, e.g. "GET /notes?tag= returns 500 instead of 400 for an empty tag".
- `problem`: what happens today, with exact status codes, messages, counts or labels.
- `evidence`: `path`, `line_start`, `line_end` and a `quote` copied exactly from those lines.
  Cardinal drops any proposal whose quote is not in the file.
- `expected`: the behaviour that should replace it.
- `acceptance`: each criterion names an exact value: a number (status, exit code, count) or a
  quoted literal (`"message"`, `field`, label). Vague criteria are dropped.
- `files`: the existing files a fix would change, under `paths_off_limits` never.
