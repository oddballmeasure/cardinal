Any path that labels an issue must create the label axis first; only the daemon and `repos check` used to.

The first offline monitor run filed Cardinal's own crash on a repository no daemon had served. Triage then failed
with `could not add label: 'cardinal:investigate' not found`, leaving an unlabelled issue that no queue
would pick up. `app.triage` now calls `issues.ensure_labels` before it labels anything. A new entry
point that sets state labels needs the same call.
