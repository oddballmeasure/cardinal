Include required cache settings in a run artifact's rerun command.

The direct E2E command failed when uv tried to use an unwritable default cache,
then passed with `UV_CACHE_DIR` under `/private/tmp`. Recording that setting
keeps the artifact's rerun command executable in the same environment.
