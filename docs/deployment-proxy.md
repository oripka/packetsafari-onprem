# Rolling API updates

`deployment_proxy.py` controls a digest-pinned NGINX proxy. `rolling_update.py`
adds a two-slot Compose transaction used by normal dev commands and, after
explicit activation, the signed host updater. This is API-only rolling support;
worker, schema, gateway and shared-service replacements require maintenance.

## Development

From the app repository:

```bash
./packetsafari dev rolling-enable   # one-time port transfer; briefly interrupts API
./packetsafari dev update           # reload mounted source through the other slot
./packetsafari dev update --image backend-dev:latest
./packetsafari dev rolling-status
./packetsafari dev rolling-recover  # roll back an unfinished transaction
```

The proxy owns the existing `127.0.0.1:8080`. Both slots use the real backend
container entrypoint, NGINX and uWSGI. The frontend's normal localhost backend
address therefore stays unchanged. Worker, gateway, database and Sharkd are not
recreated. `dev restart backend` uses this transaction once enabled; an unqualified
`dev restart` still explicitly restarts the worker too.

For dependency/image changes, build without recreating the stack, then update:

```bash
PACKETSAFARI_DEV_BUILD_ONLY=1 ./packetsafari dev rebuild
./packetsafari dev update --image backend-dev:latest
```

A full legacy dev rebuild is blocked while rolling mode is enabled. Do not run
the original Compose file directly against this running project: it would bypass
the proxy topology. The generated runtime is private, mode 0600, under
`$PACKETSAFARI_DATA_ROOT/runs/deployment-proxy/dev-runtime`; it contains resolved
configuration and secrets and must not be committed or attached to reports.
Dev source mounts are shared between slots, so these trials prove deployment
mechanics, not immutable production application-version isolation.

## Transaction

1. Keep the serving slot running while the inactive image starts. Disable automatic
   schema migration and database initialization in both API slots.
2. Compare Alembic and SQL model inputs, then require a direct HTTP 200 from the
   candidate health endpoint. Redirects and failures do not qualify.
3. Validate NGINX configuration, persist pending reload state, reload gracefully,
   and read back its generation through the loopback control listener.
4. Retain both slots until old NGINX workers drain. A timeout keeps the transaction
   pending; it never forcibly terminates an upload or stream. Another replacement
   is blocked. Recovery can return to the exact retained container instance.
5. Run post-switch health/doctor checks before promoting installed release metadata.
   Failed checks roll traffic back. Stop the old slot only after successful commit.

A journal tracks preparation, switch, metadata commit and cleanup. A process death
between NGINX reload and receipt persistence is reconciled from pending state.
Interrupted metadata commits on a host recover to the old release using the saved
metadata snapshot; no database restoration is needed because no migrations ran.
Cleanup failures after commit retain the new release and are retryable via recovery.
Do not delete state files to bypass drift, drain or recovery checks.

There is no automatic HTTP retry, request/response buffering or forced worker
shutdown deadline. Idle stream timeouts are 3600 seconds. Proxy restart preserves
routing but interrupts live connections. This single-host setup is not HA.

## Host activation and release contract

Host activation is opt-in and has not been qualified on production in this work.
It requires the signed installed manifest to include `images.deployment-proxy`
with a registry digest and the installed firewall policy to govern `.26` (proxy)
and `.27` (green backend). The release builder now mirrors the pinned proxy image.
The existing origin port is transferred once; subsequent updates keep it stable.

```bash
packetsafari-ops deployment-proxy enable --profile saas --manifest /path/to/signed-installed-manifest.json
# Subsequent updates use the existing packetsafari-ops update command and its backup policy.
packetsafari-ops deployment-proxy recover --profile saas
```

The normal signature, profile, entitlement, authorization and backup gates still
run before the rolling branch. A signed target must declare:

```json
{"rollingUpdate":{"compatibleFrom":["exact-installed-version"]}}
```

Only the backend image may change, and it must be digest-pinned. The gateway image
must be explicitly retained when legacy manifests inherited it from backend.
Changes to other images, deployment profiles, required environment, schema inputs,
or frozen runtime configuration are rejected. Inline backups and skipped health
checks are rejected. Legacy stop-first updates and legacy rollback are blocked
once activated; there is no silent maintenance fallback.

The normal full-release builder does not yet produce API-only compatibility
manifests. Do not enable this on a production host until that release path, a
maintenance transition, CloudFront/TLS/client-address forwarding, resource headroom,
authenticated uploads and real continuing investigations have been qualified on
a representative host. Local timings are not a production downtime guarantee.
Frontend/CDN publication and worker draining remain separate work.

## Verification

```bash
python3 -m pytest tests/test_rolling_update.py tests/test_deployment_proxy.py tests/test_render_compose.py tests/test_update_channel.py
# From the app workspace, where local test ports are allowed:
python3 ../packetsafari-onprem/scripts/test_deployment_proxy_local.py
python3 scripts/test_dev_rolling_update.py
```

The transport fixture checks complete SSE/WebSocket sequences, a byte-verified
slow upload, readiness rejection, drain protection, retained-instance rollback
and proxy restart. The real dev test checks alternating cold updates, post-switch
failure rollback, interrupted staging/reload recovery and unchanged shared-service
start times. It samples the normal API port and retains receipts and availability
records under the data root. It does not consume models or deploy production.
