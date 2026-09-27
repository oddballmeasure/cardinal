Keep standalone verdict fixtures schema-complete and bind them to the run's branch and head.

The first standalone PR checks stopped at parsing because their synthetic
verification fixtures lacked `tests_are_e2e`. Keeping fixtures aligned with the
real verifier result ensures those checks exercise PR behavior instead of failing
before the PR manager runs.
The default passing fixture also needs branch and head placeholders; a fixed branch
silently makes `--branch` unusable for otherwise valid local PR tests.
