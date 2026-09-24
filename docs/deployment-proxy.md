# Rolling API updates

`deployment_proxy.py` controls a digest-pinned NGINX proxy. `rolling_update.py`
adds a two-slot Compose transaction used by normal dev commands and, after
explicit activation, the signed host updater. This is API-only rolling support;
worker, schema, gateway and shared-service replacements require maintenance.

## Normal release workflow integration

The operator interface remains the existing two commands:

```bash
# App repository, local workstation: builds/publishes runtime and static frontend
./packetsafari-saas-release --fast
# Production host: applies the published signed release
sudo env HOME=/root packetsafari-ops update --backup-mode skip --allow-unbacked-upgrade
```

The backup flags explicitly acknowledge proceeding without a data backup made
by the updater. They do not certify an external backup. The app-owned
[SaaS release runbook](../../packetsafari/documentation/internal/5.release-and-deployment/3.saas-build-push-and-update.md)
owns publication, signatures, frontend/CloudFront and host update procedures.
This document owns the proxy transaction and its integration boundary.

**Current status:** full releases use the existing maintenance updater on hosts
without proxy activation. On an activated host, the updater enters the strict
API-only branch. It rejects unsupported full-release changes; it does not apply
only the API and silently promote a partially updated release. The normal release
builder mirrors the proxy image but does not produce `rollingUpdate.compatibleFrom`.
The full-release workflow is therefore not yet integrated with blue/green.

| Component | Implemented behavior | Required before integrated full releases |
| --- | --- | --- |
| Backend API | Two slots; readiness, switch, connection drain, commit and old-slot stop. | Signed compatibility from the normal builder, automatic supported strategy selection and qualified host activation. |
| Worker, Agent runner, Sharkd | Retained during API rolling updates. | Stop new work reaching retiring processes; retain their process-affine jobs and dependencies until completion; verify replacement and cleanup. |
| Agent stream gateway | Retained; legacy backend-image inheritance must be explicitly frozen. | Version compatibility and live-stream continuity across replacement. |
| DNS, outbound proxy, firewall, deployment proxy | Retained; changed images/configuration are outside the API transaction. | Ordered replacement, ingress/egress checks and explicit interruption reporting. |
| Database, Redis, migrations | Shared; no migrations in an API rolling update. | Compatible migration strategy or a clearly reported maintenance transaction; preserve recovery/backup semantics. |
| Static frontend / CloudFront | Published locally by `--fast`, separately from the host update. | Qualify frontend/API compatibility during version overlap; publication is not atomic with host promotion. |

The integrated updater must apply **every changed image** before declaring the
release successful, show each service as reused/replaced/draining/failed, and
retain actionable logs and a recovery receipt. Operators should not manually
select backend versus full-stack deployment. These are remaining delivery
requirements, not implemented guarantees. A drain timeout must not become an
unreported task kill. No full-release three-second downtime target has been
qualified; running two APIs alone cannot provide it for shared dependencies.

Before production activation, qualify the normal signed release/update path,
compatible and maintenance changes, failed readiness, failed post-switch checks,
interrupted recovery, uploads/streams and real continuing jobs, plus resource
headroom and ingress trust. Keep this boundary current when those checks pass;
local transport tests do not substitute for full-release qualification.

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

For dependency/image changes, use one command:

```bash
./packetsafari dev update --build        # build images, replace API only
./packetsafari dev update --build --all  # build and apply the regular dev stack
```

`--all` is maintenance, not blue/green for all containers: it stops services,
recreates them from resolved image IDs, runs initialization/migrations and verifies
image identity and configured health. Jobs/connections may be interrupted.
Optional fixture/test profiles are excluded. Pinned third-party images are reused,
not rebuilt. Repeat the same command after failure to resume its saved plan;
`rolling-recover` is for API transactions, not maintenance or database restoration.
The [development guide](../../packetsafari/documentation/DEVELOPMENT.md#development-updates)
owns the command reference and current full-stack test boundary. Real full-stack
dev replacement has not yet been qualified end to end.

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
packetsafari-ops deployment-proxy enable --profile saas --manifest /path/to/signed-installed-manifest.json --ingress-policy /path/to/ingress.json
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

### Trusted ingress

Host activation requires an explicit JSON policy. Direct on-prem ingress uses
`{"mode":"direct","trustedCidrs":[]}`. An on-prem TLS terminator uses
`{"mode":"forwarded","trustedCidrs":["192.0.2.10/32"]}` (replace the example
with its actual immediate-peer address). That terminator must overwrite
X-Forwarded-Proto with exactly `http` or `https` and append the actual client IP
to X-Forwarded-For. Multi-hop recursive trust is deliberately not inferred.

SaaS requires `mode: "cloudfront-https"`, explicit current CloudFront
origin-facing CIDRs, and `viewerHttpsOnly: true`. Verify the distribution's
HTTPS-only/redirect-to-HTTPS viewer policy before setting that assertion. This
mode obtains the viewer IP from CloudFront's appended last X-Forwarded-For value
and normalizes the public scheme to HTTPS without depending on a new CloudFront
header policy. Preserve the viewer Host through the configured origin path.
Keep the existing origin firewall/access restrictions; trusting AWS ranges is
not authentication of a particular distribution. Review range changes before
updating the policy; no network lookup silently expands trust during deployment.

Untrusted peers cannot supply forwarded IP, scheme, host, port or alternate
client-address headers. Missing/malformed trusted addresses and invalid/missing
on-prem schemes fail with HTTP 400. No default-route trust CIDRs are accepted.
Policy is persisted with routing state and retained through cutover and recovery.
The backend's Agent gateway route preserves normalized metadata only from the
managed proxy address `.26`; include the updated backend NGINX template in the
release. Dev/direct mode needs no trusted addresses. Existing direct dev state
is upgraded safely on the next cutover; changing an existing host trust policy
requires explicit maintenance reconciliation, not hand-editing state files.

Do not enable this on production until the
[normal release integration prerequisites](#normal-release-workflow-integration)
have been met. Retaining the existing origin address/port avoids an origin-port
change in CloudFront; it does not remove the ingress-policy verification above
or establish frontend/API compatibility.

## Verification

```bash
python3 -m pytest tests/test_rolling_update.py tests/test_deployment_proxy.py tests/test_render_compose.py tests/test_update_channel.py
# From the app workspace, where local test ports are allowed:
python3 ../packetsafari-onprem/scripts/test_deployment_proxy_local.py
python3 ../packetsafari-onprem/scripts/test_deployment_ingress_local.py
python3 scripts/test_dev_rolling_update.py
```

The transport fixture checks complete SSE/WebSocket sequences, a byte-verified
slow upload, readiness rejection, drain protection, retained-instance rollback
and proxy restart. The real dev test checks alternating cold updates, post-switch
failure rollback, interrupted staging/reload recovery and unchanged shared-service
start times. It samples the normal API port and retains receipts and availability
records under the data root. It does not consume models or deploy production.
