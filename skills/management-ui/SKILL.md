---
name: management-ui
description: Define or implement the management interface for repositories, workers, role models, and deployment controls when that component is being built.
---

# Management UI

## Inputs and outputs

Input: the current managed repository and worker configuration, plus an operator
request to add or remove a repository or change a role's model or worker settings.
Output: a visible, reviewable configuration change and its resulting state.

## Tools

Use the application's configuration read and update APIs when they exist. A UI
control must invoke the same API contract as other callers; do not write directly
to an agent's private files.

## Workflow

Expose repositories, coding workers, and separate model choices for orchestration,
coding, verification, and PR management. Show the current values before accepting
changes and surface API failures next to the requested operation. Deployment
controls must identify the supplied deployment file and show execution status.
