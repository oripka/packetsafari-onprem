# Application generation updates

`deployment_proxy.py` controls a digest-pinned NGINX proxy. `rolling_update.py`
owns shared topology, activation and retained legacy recovery. Two procedures use
that runtime and one transaction journal: `fleet_update.py` replaces backend,
Celery worker, Agent stream gateway, Agent runner and Sharkd together;
`maintenance_update.py` drains and pauses the fleet for schema/shared-service changes.
New API-only transactions are retired; historical journals remain recoverable.

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

The normal builder now records an image-derived `runtimeContract` in the signed
manifest. Ops 0.2.40 selects full-generation updates for this contract on an
activated host. It refuses partial promotion or silent maintenance fallback.
This implementation has local deterministic and real Celery transaction tests;
it has **not** been deployed or qualified on production.

| Component | Implemented behavior | Boundary |
| --- | --- | --- |
| Backend API | Candidate readiness, proxy switch, connection drain, retirement. | Same schema and task protocol required. |
| Worker, Agent runner, Sharkd | Both generations run; TERM stops old Celery intake and waits for every worker child. Old runner/socket/workspaces and Sharkd remain until jobs and proxy connections drain. | No forced termination on timeout. External unmanaged consumers are outside this ownership contract. |
| Agent stream gateway | Separate generation; old gateway remains while old jobs and proxy connections exist. | Real Agent model-backed continuity remains unqualified. |
| DNS, outbound proxy, firewall, deployment proxy | Reused when unchanged; changed image/configuration is rejected. | No overlap contract; explicit maintenance transition required. |
| Database, Redis, migrations | Shared and unchanged; no migrations in a generation update. | Schema drift is rejected, not declared compatible automatically. |
| Static frontend / CloudFront | Published by `--fast`, separately from host update. | Not atomic with host promotion; frontend/API overlap must remain compatible. Container-fronted on-prem fleets are rejected. |

All changed application images are prepared and identity-checked before promotion;
changes outside the supported cohort fail before switching. Old job failure,
restart or OOM is not successful drain. Exit 3 means pending drain: repeat the
same update to resume, without rebuilding/replacing the retained generations.
Exit 1 with `rolled_back` means verification failed and traffic returned to the
retained generation. Neither outcome advances installed release metadata.
Signed bundled intelligence is activated through the existing idempotent content
bootstrap after drain, before metadata promotion. The full storage initializer
and migrations are not run during overlap. No three-second downtime guarantee
has been established.

Full-release blue/green must drain every replaced container that owns active
work or connections, not only the HTTP backend. Stop assigning new work to the
retiring generation, verify replacements, and keep old workers, Agent runners,
Sharkd sessions, gateway streams and their required dependencies alive until
their work completes. HTTP connection drain is not evidence that background work
has finished. Stop old containers only after service-specific drain checks pass.
A timeout leaves them running and the release pending; do not silently fall back
to maintenance or force-stop tasks. Services without a safe overlap/drain contract,
including incompatible database changes, require an explicitly separate maintenance
decision. The default dev rebuild uses this same generation transaction when
shared configuration is unchanged; shared dev changes may use maintenance.

Before production activation, qualify the normal signed release/update path,
compatible and maintenance changes, failed readiness, failed post-switch checks,
interrupted recovery, uploads/streams and real continuing jobs, plus resource
headroom and ingress trust. Keep this boundary current when those checks pass;
local transport tests do not substitute for full-release qualification.

## Development

From the app repository:

```bash
./packetsafari dev rolling-enable   # one-time port transfer; briefly interrupts API
./packetsafari dev rebuild          # build/apply all regular dev images
./packetsafari dev update           # current images, no build
./packetsafari dev update --image backend-dev:latest
./packetsafari dev rolling-status
./packetsafari dev rolling-recover  # roll back an unfinished transaction
```

The proxy owns the existing API port 8080 and, after full-generation bootstrap,
Sharkd port 4448. The frontend's normal addresses stay unchanged. A legacy
API-only dev activation gets its full-generation topology through one maintenance
`dev rebuild`. Subsequent compatible application updates exercise the same
transaction as the host updater. `dev restart` also uses the update transaction.

For dependency/image changes, use one command:

```bash
./packetsafari dev update --build        # same as dev rebuild
./packetsafari dev update --build --all  # compatibility alias; --all unnecessary
```

First bootstrap transfers ports and may interrupt existing work. Once activated,
shared-service or immutable image schema/protocol changes use the shared maintenance
procedure: build first, pause ingress with HTTP 503, drain connections/jobs, apply
resolved images and migrations, verify and reopen. Force it with
`./packetsafari dev rebuild --maintenance`. Optional fixture/test profiles are
excluded. Pinned third-party images are reused, not rebuilt. Repeat the same
command after failure to resume its saved plan. `rolling-recover` safely aborts
application updates before commit; it cannot restore a database after maintenance.
The [development guide](../../packetsafari/documentation/DEVELOPMENT.md#development-updates)
owns the command reference. On 2026-09-24, the real dev stack completed a full
rebuild/bootstrap and a five-service generation update: 405 health probes saw
zero failures, with a 1.023-second proxy switch receipt. Preparation/drain/cleanup
took 58 seconds in total. This does not qualify production workload continuity.

A normal no-flag dev rebuild uses the managed topology. Do not run
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

A journal tracks preparation, worker start, switch, drain, metadata commit and
cleanup. Repeat the exact update to resume full-generation transactions, including
an interrupted metadata commit or cleanup. Pending connected updates use the saved
exact manifest bytes and detached signature, reverified on each attempt; channel
discovery is deferred until completion. Offline bundles or older transactions
without a saved detached signature require the original signed upgrade/bundle input.
Before metadata commit, recovery can abort: restore original traffic, stop candidate
intake, drain its jobs/connections, then retire dependencies. This also handles
failed readiness after a candidate worker accepted a job. Repeat recovery on exit 3.
Changed container identities fail closed. Once commit begins, finish the update;
use a subsequent signed release for application rollback. Failed post-switch checks
use the same drain path automatically. Drain timeout retains both generations.
Do not delete state files to bypass drift, drain or recovery checks.

There is no automatic HTTP retry, request/response buffering or forced worker
shutdown deadline. Idle stream timeouts are 3600 seconds. Proxy restart preserves
routing but interrupts live connections. This single-host setup is not HA.

## Host activation and release contract

Host activation is opt-in and has not been qualified on production in this work.
It requires the signed installed manifest to include `images.deployment-proxy`
with a registry digest, the image-derived runtime contract, warm-drain worker
support and firewall rules for `.26` through `.29` and `.31`. Existing hosts first
need an explicitly interrupting `update --maintenance` to install these prerequisites
(with their normal backup flags), then proxy activation. Existing API-only host
activations require a reviewed topology migration; do not hand-edit their state.
The API and Sharkd origin ports transfer once; subsequent updates keep them stable.

```bash
packetsafari-ops deployment-proxy enable --profile saas --manifest /path/to/signed-installed-manifest.json --ingress-policy /path/to/ingress.json
# Full-generation recovery: repeat the exact normal update command.
# Or abort an application transaction before metadata commit (repeat on exit 3).
packetsafari-ops deployment-proxy recover --profile saas
```

The normal signature, profile, entitlement, authorization and backup gates still
run before the rolling branch. A signed target must declare:

```json
{"runtimeContract":{"protocolVersion":1,"workerDrainVersion":1,"schemaInputs":"<SHA256 of image Alembic and SQL models>"}}
```

The builder reads this from both backend and worker images and requires equality;
resuming old images without the helper fails. Maintainers must bump protocolVersion
for incompatible task/interprocess messages. Matching hashes do not prove semantic
compatibility by themselves. All five application images must be digest-pinned;
the gateway inherits backend when omitted. Changes outside this cohort, deployment
profiles, required environment, schema or frozen runtime configuration are rejected.
Inline backups and skipped health checks are rejected for overlapping application
updates. For explicit maintenance on an activated fleet, use the usual update
command plus `--maintenance` and the chosen backup policy. The controller pauses
new ingress with HTTP 503, waits for existing proxy connections and warm worker
drain, then runs the existing backup, rendering, migration and verification owners.
Inline backups are supported after drain. It retains generation mode afterward.
Timeout leaves ingress paused and dependencies alive; repeat update to resume.
Startup/migration failures retain their saved plan. Verification failure re-pauses
ingress. Maintenance never automatically rolls back database changes: resume forward
or use the established backup restoration procedure. This is an interruption, not
general on-prem full-stack zero-downtime support.

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
python3 -m pytest tests/test_deployment_lifecycle.py tests/test_fleet_update.py tests/test_rolling_update.py tests/test_deployment_proxy.py tests/test_render_compose.py tests/test_update_channel.py tests/test_signed_release_manifest.py
# From the app workspace, where local test ports are allowed:
python3 ../packetsafari-onprem/scripts/test_deployment_proxy_local.py
python3 ../packetsafari-onprem/scripts/test_deployment_ingress_local.py
python3 ../packetsafari-onprem/scripts/test_workload_drain_local.py
python3 ../packetsafari-onprem/scripts/test_fleet_update_local.py
python3 -m unittest discover -s scripts/tests -p test_dev_maintenance_update.py
```

The transport fixture checks complete SSE/WebSocket sequences, a byte-verified
slow upload, readiness rejection, drain protection, retained-instance rollback
and proxy restart. The shared fleet fixture retains receipts and availability
records under the data root. It does not consume models or deploy production.

The fleet fixture runs real Celery/Redis with synthetic long jobs and separate
runner/Sharkd/gateway fixtures. It covers pending drain and resume, replacement
image identities, post-switch rollback with a candidate job in flight, and early
readiness rejection. It also checks abort after candidate worker readiness failure,
maintenance job drain/HTTP 503, and interrupted maintenance startup resume without
re-preparing. It does not execute a real Agent or Triage analysis, nor
qualify signed ECR delivery, CloudFront, on-prem ingress or production RAM headroom.
