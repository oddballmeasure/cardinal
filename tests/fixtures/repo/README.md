# Ledger fixture

A transaction CLI with separate record loading, selection, and output modules.
Run `python -m ledger --input data/transactions.json --merchant Café`.
`ledger/totals.py` provides a reporting calculation. `ledger/legacy_csv.py` is
an unused prototype, so new CLI output belongs in the active formatter module.
The issue fixture describes the requested enhancement.
The co-located `deploy/deploy.sh` and `deploy/deploy.json` pair provides the
deployment-manager fixture. It reports healthy only after the fake host becomes
ready.
