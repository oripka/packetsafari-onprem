# PacketSafari On-Prem

Customer-facing Python-native installer, operator CLI, and simple interactive menu for PacketSafari on-prem.

## Layout

- `bootstrap.sh` - pinned/download-host bootstrap shim that downloads the bundle and launches the Python CLI
- `packetsafari_onprem/` - Python control plane for install, status, onboarding, upgrade, rollback, diagnostics, and the interactive operator menu
- `scripts/license_*.py` - offline entitlement token tooling
- `docs/license-claims.md` - signed entitlement claim schema and internal issuance commands
- `docs/upgrade-runbook.md` - connected and air-gapped upgrade/rollback runbook
- `scripts/render_compose.py` - render pinned on-prem compose files from release manifests
- `scripts/build_offline_bundle.py` - build signed USB/offline install/upgrade bundles from a release manifest
- `scripts/build_local_release.py` - build a locally hosted on-prem release directory for VM validation
- `scripts/configure_logging.py` - audit logging defaults helper
- `scripts/render_logging_config.py` - render Vector config for optional audit log forwarding
- `templates/docker-compose.onprem.yml.tpl` - compose template rendered during install and upgrade

## Bootstrap And Updates

Bootstrap is for first install and recovery. Once `/usr/local/bin/packetsafari-ops`
exists, normal connected and SaaS updates should use `packetsafari-ops update`;
that command can now update the ops tooling first and then continue the app
release update.

Supported customer install entrypoints:

```bash
curl -fsSL "https://<portal-presigned-url>/bootstrap.sh" | bash -s -- install --license /path/to/license-token.json --manifest ./release-manifest.json
```

```bash
curl -fsSL "https://<portal-presigned-url>/bootstrap.sh" | bash -s -- install --bundle ./packetsafari-10.0.1-offline.tar.zst
```

```bash
packetsafari-ops tui
```

```bash
packetsafari-ops update
```

```bash
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
```

```bash
packetsafari-ops rollback
```

For recovery, or before the installed wrapper exists, the same actions can be
launched through bootstrap:

```bash
curl -fsSL "https://<portal-presigned-url>/bootstrap.sh" | sudo -E bash -s -- update
```

```bash
curl -fsSL "https://<portal-presigned-url>/bootstrap.sh" | sudo -E bash -s -- upgrade --bundle ./packetsafari-10.0.1-offline.tar.zst
```

`bootstrap.sh` first looks for `bootstrap-manifest.json` and
`packetsafari-onprem.tar.gz` next to the script. If those files are present, it
uses them without reaching GitHub or another public location. Otherwise it
downloads the full on-prem bundle, verifies the on-prem tooling archive and
Python CLI checksums from `bootstrap-manifest.json`, and launches the operator
CLI directly with `python3`.

The repository defaults point at the public GitHub repo for development and
emergency recovery. Production installers should be pinned to an authenticated
release location, not a mutable public branch. The SaaS customer portal mints
short-lived download URLs for `bootstrap.sh`, `packetsafari-onprem.tar.gz`,
release manifests, verification material, and offline bundles.

A self-contained release directory for local HTTP, private S3, or removable
media should contain at least:

```text
bootstrap.sh
bootstrap-manifest.json
packetsafari-onprem.tar.gz
release-manifest.json
release-public.pem
packetsafari-<version>-offline.tar.zst
```

Bootstrap supports private HTTP/HTTPS download sources through environment variables:

```bash
export PACKETSAFARI_ONPREM_RAW_BASE=https://downloads.example.com/packetsafari/onprem/10.0.1
export PACKETSAFARI_ONPREM_ARCHIVE_URL=https://downloads.example.com/packetsafari/onprem/10.0.1/packetsafari-onprem.tar.gz
export PACKETSAFARI_ONPREM_BOOTSTRAP_MANIFEST_URL=https://downloads.example.com/packetsafari/onprem/10.0.1/bootstrap-manifest.json
export PACKETSAFARI_ONPREM_BEARER_TOKEN="$TOKEN"
curl -fsSL "$PACKETSAFARI_ONPREM_RAW_BASE/bootstrap.sh" | sudo -E bash -s -- install \
  --bundle https://downloads.example.com/packetsafari/releases/packetsafari-10.0.1-offline.tar.zst \
  --download-bearer-token "$TOKEN"
```

Use `PACKETSAFARI_ONPREM_BASIC_AUTH=user:pass` or `PACKETSAFARI_ONPREM_DOWNLOAD_HEADER='x-api-key: ...'` when the hosting layer uses basic auth or a custom header. The same request options are available to `packetsafari-ops install` and `upgrade` as `--download-basic`, `--download-bearer-token`, and repeatable `--download-header`.

For signed HTTP(S) URLs, set `PACKETSAFARI_ONPREM_ARCHIVE_URL` and
`PACKETSAFARI_ONPREM_BOOTSTRAP_MANIFEST_URL` to the individual signed object
URLs. The bootstrap script does not require a public bucket; production release
downloads should come from the authenticated PacketSafari portal and
CloudFront-signed URLs, not raw S3 object access.

## Host Prerequisites

- Ubuntu Server 24.04 LTS or newer
- Docker and the Docker Compose plugin installed
- `curl`, `python3`, `tar`, `openssl`, and `zstd` available on the host
- write access to the managed runtime root (default: `/opt/packetsafari`)
- network access to the authenticated release URLs for connected installs, or local/USB access to the offline bundle
- a customer-managed compatible AI endpoint, credentials, and compute for PacketSafari Agent operation
- enough disk for the bundle, image load, database, captures, and rollback snapshots

Supported single-node floor:

- CPU: 2 vCPU
- RAM: 16 GiB
- Disk: 120 GiB root or data volume
- Architecture: must match the release artifact, for example `linux-arm64` requires an ARM64 host

Recommended starting point for small production deployments:

- CPU: 4 vCPU
- RAM: 16 GiB
- Disk: 200 GiB root or data volume

Heavy PCAP ingestion benefits directly from CPU and memory. For frequent large captures or multi-user analysis, start at 4-8 vCPU and 32 GiB RAM, then use `packetsafari-ops tune --apply` after resizing so worker, sharkd, Redis, and PostgreSQL limits match the host. `packetsafari-ops status`, `update`, `upgrade`, and `tune` report a stale-sizing warning when the saved runtime sizing was generated for a different CPU or RAM shape.

Bootstrap installs these packages automatically on Ubuntu when it is run as root. To install them manually, or to disable automatic package installation with `PACKETSAFARI_ONPREM_INSTALL_HOST_DEPS=false`, use:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 zstd tar openssl curl python3
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in after adding the user to the `docker` group, or run
deployment commands with `sudo`.

Expose only the ports needed for the deployment:

- `22/tcp` for operator SSH
- `3000/tcp` for the frontend if accessed directly
- `8080/tcp` for backend health/API if accessed directly
- `4448/tcp` for direct browser-to-sharkd WebSocket traffic

The frontend intentionally connects directly to sharkd for low-latency packet views. If DNS, NAT, or a load balancer changes the externally reachable sharkd address, set `NUXT_PUBLIC_SHARKD_WS_URL` to that explicit `ws://` or `wss://` URL.

## Runtime Layout

The installer manages host state under `/opt/packetsafari` by default:

- `state/` - deployment state, release manifests, license token, helper status
- `env/` - managed `runtime.env`
- `compose/` - rendered compose bundle
- `secrets/` - reserved for host-managed secret material and future file-backed workflows
- `backups/` - upgrade and rollback snapshots
- `tmp/` - temporary offline bundle extraction and split-file reassembly
- `tooling/onprem/` - installed Python operator bundle
- `bin/packetsafari-ops` - stable local command that runs the installed operator tool through `python3`

That host runtime root is bind-mounted into the app containers at `/storage/onprem`, so host tooling and the running PacketSafari services operate on the same state files.

## Python CLI / Menu

Primary commands:

```bash
packetsafari-ops install --license /path/to/license-token.json --non-interactive
packetsafari-ops install --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops status --json
packetsafari-ops tui
packetsafari-ops upgrade
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops rollback
packetsafari-ops onboard schema
packetsafari-ops config show
packetsafari-ops iam show-initial-admin-command --email admin@example.com
packetsafari-ops diagnostics restart
```

The installed wrapper is written to `/opt/packetsafari/bin/packetsafari-ops` during install and does not require the operator to create a venv manually.

## Operator Workflow

- `install` validates the signed license token, writes runtime state under the managed root, installs the Python bundle, renders compose and logging config, and starts PacketSafari in onboarding mode.
- `install --bundle` performs a fresh air-gapped install from a signed bundle, loads image archives locally, and starts Compose with `--pull never`.
- `tui` is the primary operator interface. It is a simple menu runner with back navigation.
- When `PACKETSAFARI_DATA_ROOT` or `~/packetsafari-data` exists, the menu defaults to that local dev layout. Otherwise it defaults to `/opt/packetsafari`.
- Native onboarding uses the existing local `/api/v2/onprem/onboarding/*` APIs. The menu can show schema output and validate, save, or finalize pasted draft JSON directly from the terminal.
- Generated-capable internal deployment secrets are now registry-driven. The onboarding schema distinguishes generated-capable platform secrets from manual-only external credentials.
- Finalizing onboarding writes the managed `runtime.env`, flips the deployment out of onboarding mode on the next restart, and then requires manual first-admin creation from inside the backend container.
- `update check` discovers the configured release-channel manifest and reports app and ops tooling availability.
- `update` downloads the configured release-channel manifest, self-updates `packetsafari-ops` when the manifest advertises newer tooling, re-execs the updated CLI, and then runs the same transaction as `upgrade --manifest`.
- `healthcheck` runs deployment readiness checks and Docker image-retention guidance without applying a release.
- `install --license` and bare `upgrade` use the same default release-channel
  manifest discovery. Pass `--manifest` only for a pinned file/URL, staging
  channel, or customer-specific manifest.
- `upgrade --manifest` runs a connected upgrade: validate the manifest, verify the license, stop only services whose manifest image changed, back up PostgreSQL and `/storage`, pull the target images, render Compose, run migrations from the target backend image, start with `--pull never`, run health checks, and promote only after success.
- `upgrade --bundle` runs the same transaction without network access: verify `checksums.txt.sig`, verify all file checksums, load Docker images from the bundle, retag them as local `packetsafari/<service>:<version>` images, render Compose to those local refs, and start with `--pull never`.
- `rollback` restores the latest full snapshot, including PostgreSQL and `/storage`. Legacy metadata-only snapshots are still supported but are reported as metadata-only restores.

## Current Validation Status

As of May 12, 2026, the on-prem path has been validated on disposable Ubuntu
ARM64 EC2 hosts:

- fresh `install --bundle` from a local signed `10.0.0-beta.14` offline bundle
- automatic Ubuntu Docker/Compose host dependency installation through bootstrap
- onboarding, first admin creation, browser login, UI upload, complete indexing,
  packet stats, enriched connections, upload insights, and security scan output
- synthetic `10.0.0-beta.15` `upgrade --bundle` migration-failure simulation
  with inline backup and automatic restore of PostgreSQL and `/storage`

The `10.0.0-beta.15` bundle was generated only to exercise rollback mechanics.
It was not a product release. Repeat the failure drill for each real customer
release candidate.

## Single-Host SaaS Upgrade Profile

`packetsafari-ops` also runs the transaction engine for PacketSafari-operated
single-EC2 SaaS hosts. The normal SaaS host update is one command:

```bash
sudo env HOME=/root packetsafari-ops update
```

On a SaaS host, the command infers `profile=saas`, downloads the private release
manifest from
`s3://packetsafari-release-channels-166826692770/channels/saas/stable/linux-arm64/release-manifest.json`
with the EC2 instance role, updates `packetsafari-ops` from the manifest's
`tooling.archiveUrl` when needed, pulls ECR images through root Docker's ECR
credential helper, applies migrations, restarts services, health-checks, and
promotes.

SaaS-specific safety rules:

- `/opt/packetsafari/secrets/saas-operator-token` must be installed.
- The EC2 role must have private release-channel `s3:GetObject` access.
- No long-lived AWS keys, signing keys, or upstream API secrets should be copied to the host.
- `--backup-mode require-recent` is the default for SaaS.
- `--backup-mode skip --allow-unbacked-upgrade` is only for disposable hosts or known container-only updates.

Useful SaaS commands:

```bash
sudo env HOME=/root packetsafari-ops update check
sudo env HOME=/root packetsafari-ops update
sudo env HOME=/root packetsafari-ops healthcheck --profile saas
sudo env HOME=/root packetsafari-ops rollback --profile saas
```

For on-prem connected updates, the public release channel remains:

```text
https://releases.packetsafari.com/channels/<profile>/<channel>/<platform>/release-manifest.json
```

Override `PACKETSAFARI_UPDATE_MANIFEST_URL` or `PACKETSAFARI_UPDATE_BASE_URL`
only for staged/private/customer-specific manifests.

`doctor --profile saas` checks product readiness, not just Docker liveness. It
verifies required SaaS env such as `PACKETSAFARI_PUBLIC_BASE_URL` and
`PACKETSAFARI_PADDLE_WEBHOOK_SECRET`, plus upstream OpenAI/Paddle keys from
`env/ironproxy.env`. `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and `PACKETSAFARI_PADDLE_API_KEY` should
be real only in that ironproxy env file; backend/worker should receive proxy
placeholders from `env/runtime.env`. The doctor also probes backend
health/config, checks frontend `runtime-config.json`, and inspects Compose
service state. SaaS upgrades run this readiness check after startup and before
release promotion.

Successful updates also record the deployed image ids. When old dangling Docker
images are detected and enough deployment history exists, `update` explains the
space impact and asks whether to remove images outside the current plus last two
recorded deployment image sets. Pressing Enter keeps them. Non-interactive runs
only report the condition unless `--prune-old-images` is passed. Use
`packetsafari-ops healthcheck --json` for automation-friendly reporting.

Install the operator token at `/opt/packetsafari/secrets/saas-operator-token`
and set the expected SHA-256 digest in the manifest at
`deploymentProfiles.saas.operatorTokenSha256`, or in
`PACKETSAFARI_SAAS_OPERATOR_TOKEN_SHA256`. Do not publish this material to
customer release artifacts.

The default external backup proof path is `/opt/packetsafari/state/latest-backup.json`.
An EC2 backup job should write this file only after the backup is complete and
restorable. A minimal proof file is:

```json
{
  "provider": "aws-ebs",
  "snapshotId": "snap-0123456789abcdef0",
  "completedAt": "2026-05-11T10:30:00Z",
  "verifiedRestore": true
}
```

The upgrade refuses to proceed when the proof is missing, empty, or older than
`--max-backup-age-minutes` (default: 180). To force the ops tool to take its own
PostgreSQL and `/storage` backup instead, run:

```bash
packetsafari-ops upgrade --profile saas --backup-mode inline --manifest ./release-manifest.json
```

Failure behavior is phase-aware:

- before migrations: restore the previous manifest, env, state, and compose files, then restart the previous release
- during or after migrations: restore PostgreSQL and `/storage` from the snapshot, then restart the previous release
- after a successful migration: rollback is treated as a restore operation unless a release has an explicitly tested down-migration path

For SaaS upgrades that use `--backup-mode require-recent`, automatic rollback
after a migration restores runtime metadata only. The operator must restore
PostgreSQL and `/storage` from the external backup before serving traffic.

## First Admin Creation

The first admin is not created during onboarding finalize.

Create it manually from inside the backend container after finalize:

```bash
docker exec -it packetsafari-backend python3 /app/scripts/create_initial_admin.py --email admin@example.com
```

## License Claims

On-prem licenses are signed offline entitlement tokens. Current tokens carry explicit claims for `agent_enabled`, `max_users`, `max_agent_runs_per_month`, `offline_expiry`, `customer_id`, `deployment_id`, and `support_tier`. The established `max_agent_runs_per_month` claim now represents weighted Agent units per licensed deployment; the claim name remains unchanged so existing runtimes continue to verify new tokens.

Create and verify tokens with:

```bash
python3 scripts/license_create.py --private-key keys/license-private.pem --customer-id customer-acme --customer-email security@example.com --license-id lic-acme-001 --deployment-id dep-acme-prod --support-tier enterprise --max-users 25 --max-agent-units-per-month 5000 --agent-enabled --days 365 --output /tmp/packetsafari-license-token.json
python3 scripts/license_verify.py --token /tmp/packetsafari-license-token.json --public-key keys/license-public.pem
```

The default enterprise entitlement is 25 enabled named users and 5,000 weighted Agent units per licensed deployment and calendar month. Agent operation uses the customer-managed compatible AI endpoint, credentials, and compute; neither the license token nor an offline bundle supplies upstream AI capacity. See `docs/license-claims.md` for the unit schedule and full claim schema. The private signing key is internal-only and must never be installed on a customer host.

## Audit Logging Modes

Supported defaults:

- `stdout_json` - default customer ingestion path
- `forwarder_profile` - starts the optional `audit-forwarder` service with a rendered Vector config
- `custom_driver` - leaves routing to customer-managed Docker logging configuration

For automated installs, pass `--non-interactive` plus the audit logging flags to `packetsafari-ops install`.

## Release Manifest Shape

The installer expects `images` to be a flat map of digest-pinned image references:

```json
{
  "version": "10.0.1",
  "images": {
    "frontend": "registry.example.com/packetsafari/frontend@sha256:...",
    "backend": "registry.example.com/packetsafari/backend@sha256:...",
    "worker": "registry.example.com/packetsafari/backend@sha256:...",
    "redis": "registry.example.com/packetsafari/redis-stack-server@sha256:...",
    "postgres": "registry.example.com/packetsafari/postgres@sha256:...",
    "sharkd": "registry.example.com/packetsafari/sharkd@sha256:...",
    "egress-dns": "registry.example.com/packetsafari/egress-dns@sha256:...",
    "egress-ironproxy": "registry.example.com/packetsafari/egress-ironproxy@sha256:...",
    "egress-firewall": "registry.example.com/packetsafari/egress-firewall@sha256:..."
  }
}
```

`scripts/render_compose.py` also accepts the richer `imageDetails.*.image` form emitted by the app release helper, but the rendered compose bundle always uses plain Docker image references.

## Offline Bundle Shape

Air-gapped upgrades use one signed archive copied to the customer host:

```text
packetsafari-10.0.1-offline.tar.zst
  release-manifest.json
  images/
    frontend.tar.zst
    backend.tar.zst
    sharkd.tar.zst
    egress-dns.tar.zst
    egress-ironproxy.tar.zst
    egress-firewall.tar.zst
    redis.tar.zst
    postgres.tar.zst
  image-metadata.json
  checksums.txt
  checksums.txt.sig
  license-token.json       # optional, for fresh install bundles
  license-public.pem       # development/local bundles only, never the production trust root
  release-public.pem       # optional copy of the verification public key
  tooling/
    packetsafari-onprem-<ops-version>.tar.gz
  sbom/
  release-notes.md
```

Large bundles may be split and copied as `packetsafari-10.0.1-offline.tar.zst.part-aa`, `.part-ab`, and so on. Pass any local part path to `packetsafari-ops upgrade --bundle`; the tool reassembles all matching parts, verifies the signature and checksums, then proceeds. Remote HTTP/HTTPS bundle URLs must point to the complete reassembled archive.

Offline bundles embed the on-prem operator tooling under `tooling/` and record
its path and checksum in `release-manifest.json`. Existing hosts can run
`packetsafari-ops upgrade --bundle ...` directly; the installed CLI updates
itself from the signed bundle before interpreting newer bundle semantics. Keep
the adjacent `bootstrap.sh` and `packetsafari-onprem.tar.gz` files for fresh
installs and recovery when the installed wrapper is missing or broken.

Build a bundle on a connected release workstation:

```bash
python3 scripts/build_offline_bundle.py \
  --manifest ./release-manifest.json \
  --release-notes ./release-notes.md \
  --sbom-dir ./sbom \
  --sign-key /secure/internal/release-private.pem \
  --output ./packetsafari-10.0.1-offline.tar.zst
```

## Local VM Release

For a Mac-hosted Ubuntu VM validation release from the app repo `main` branch:

```bash
python3 scripts/build_local_release.py \
  --app-root ../packetsafari \
  --output-dir /Users/otr/packetsafari-data/releases/local/10.0.0-beta.14
```

Then serve the generated directory:

```bash
cd /Users/otr/packetsafari-data/releases/local/10.0.0-beta.14
python3 -m http.server 9000 --bind 0.0.0.0
```

On the Ubuntu VM:

```bash
export PACKETSAFARI_ONPREM_RAW_BASE=http://<mac-ip>:9000
export PACKETSAFARI_ONPREM_ARCHIVE_URL=http://<mac-ip>:9000/packetsafari-onprem.tar.gz
export PACKETSAFARI_ONPREM_BOOTSTRAP_MANIFEST_URL=http://<mac-ip>:9000/bootstrap-manifest.json
curl -fsSL http://<mac-ip>:9000/bootstrap.sh | sudo -E bash -s -- install \
  --bundle http://<mac-ip>:9000/packetsafari-10.0.0-beta.14-offline.tar.zst \
  --bundle-public-key http://<mac-ip>:9000/release-public.pem \
  --allow-bundled-license-public-key
```

The local release helper intentionally generates development-only license and release signing keys under `/Users/otr/packetsafari-data/release-keys` by default, outside the distributable release directory. That is suitable for VM validation only. Customer releases must use PacketSafari-controlled signing keys and a customer-specific entitlement token from an authenticated distribution channel.
