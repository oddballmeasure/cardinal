---
name: scout-planner
description: Split a repository into 3-6 survey areas, each small enough to read closely in one sitting.
---

# Scout planner

Input: the repository at `/repo/` (read-only) and its profile at `/context/repo_profile.json`.
Output: one `SurveyPlan`.

- Use `find_repo_context` to locate behaviour; read `/repo/` to confirm what owns it.
- Each area is one coherent part of the running product: an endpoint group, a CLI command, a
  module and the code it calls. Name the files or directories, and say what to look for.
- Prefer active code that users reach. Skip vendored, generated, legacy and unused code.
- Paths must be tracked files or directories in `/repo/`. Never list the whole repository.
