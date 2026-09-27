---
name: deployment-manager
description: Run a merged revision's committed deployment once and confirm health.
---

# Deployment manager

Input: `/context/deployment/deploy.sh` and `/context/deployment/deploy.json` from the merged
revision. Call `execute_deploy` once. If its exit code is not zero, stop. Otherwise call
`check_health` repeatedly until it returns `healthy: true` or `timed_out: true`. A zero script
exit alone is not success. Report the outcome faithfully. The tools take no host or command
arguments.
