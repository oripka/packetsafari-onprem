# Component inventory for the administration UI

`packetsafari-ops inventory` records a bounded Docker observation in
`state/component-inventory.json` using the selected runtime-root options. It does
not rebuild, restart, update, or execute model inference. Normal successful
application updates also record this snapshot after the deployment receipt.

The file is an allowlisted projection readable by the unprivileged API (0644).
Raw inspect data, environment variables, credentials, and health logs are never
written. Image versions come from OCI labels or known baked version constants;
image references remain visible when no version is reported. Codex metadata is
read from its build metadata file in running backend/worker/runner containers.

The UI flags observations older than five minutes and differentiates release
manifest declarations from observed containers. Refreshing the browser reads
the last observation; rerun inventory to observe the host again. Failed collection
replaces old observations with unavailable state rather than retaining apparent
health. This is a snapshot, not a background monitoring service.

Use together with the app's admin component-inventory API. Existing installed
Ops versions do not support the new command until upgraded. No deployment is
implied by the source commits.

Deterministic checks:
`python3 -B -m unittest discover -s tests -p test_component_inventory.py`
and the existing `test_release_observability.py` tests.
