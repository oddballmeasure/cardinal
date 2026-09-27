Expected ticket targets must reflect the active code's actual responsibility.

The hard live orchestrator correctly assigned tag filtering to `notes/api.py`,
where GET query handling lives, while leaving ordered storage unchanged. The
scenario had required `notes/store.py` for that requirement and rejected a
valid plan before coding. Matching the active owner prevents that false gate.
