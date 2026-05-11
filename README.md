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

## Bootstrap

Supported customer entrypoints:

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
packetsafari-ops upgrade --manifest ./release-manifest.json
```

```bash
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
```

```bash
packetsafari-ops rollback
```

`bootstrap.sh` downloads the full on-prem bundle, verifies the Python CLI checksum from `bootstrap-manifest.json`, and then launches the operator menu directly with `python3`.

Production installers should be pinned to an authenticated release location, not a mutable public branch. The SaaS customer portal mints short-lived download URLs for `bootstrap.sh`, `packetsafari-onprem.tar.gz`, release manifests, verification material, and offline bundles.

Bootstrap supports private HTTP/HTTPS download sources through environment variables:

```bash
export PACKETSAFARI_ONPREM_RAW_BASE=https://downloads.example.com/packetsafari/onprem/10.0.1
export PACKETSAFARI_ONPREM_ARCHIVE_URL=https://downloads.example.com/packetsafari/onprem/10.0.1/packetsafari-onprem.tar.gz
export PACKETSAFARI_ONPREM_BEARER_TOKEN="$TOKEN"
curl -fsSL "$PACKETSAFARI_ONPREM_RAW_BASE/bootstrap.sh" | sudo -E bash -s -- install \
  --bundle https://downloads.example.com/packetsafari/releases/packetsafari-10.0.1-offline.tar.zst \
  --download-bearer-token "$TOKEN"
```

Use `PACKETSAFARI_ONPREM_BASIC_AUTH=user:pass` or `PACKETSAFARI_ONPREM_DOWNLOAD_HEADER='x-api-key: ...'` when the hosting layer uses basic auth or a custom header. The same request options are available to `packetsafari-ops install` and `upgrade` as `--download-basic`, `--download-bearer-token`, and repeatable `--download-header`.

## Host Prerequisites

- Linux host with Docker and the Docker Compose plugin installed
- `curl`, `python3`, `tar`, `openssl`, and `zstd`/GNU tar zstd support available on the host
- write access to the managed runtime root (default: `/opt/packetsafari`)
- network access to pull the release images referenced by the selected manifest
- `python3` available on the host for the bootstrap shim and installed operator wrapper

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
packetsafari-ops install --license /path/to/license-token.json --manifest ./release-manifest.json --non-interactive
packetsafari-ops install --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops status --json
packetsafari-ops tui
packetsafari-ops upgrade --manifest ./release-manifest.json
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
- `upgrade --manifest` runs a connected upgrade: validate the manifest, verify the license, stop app services, back up PostgreSQL and `/storage`, pull the target images, render Compose, run migrations from the target backend image, start with `--pull never`, run health checks, and promote only after success.
- `upgrade --bundle` runs the same transaction without network access: verify `checksums.txt.sig`, verify all file checksums, load Docker images from the bundle, retag them as local offline images, render Compose to those local refs, and start with `--pull never`.
- `rollback` restores the latest full snapshot, including PostgreSQL and `/storage`. Legacy metadata-only snapshots are still supported but are reported as metadata-only restores.

## Single-Host SaaS Upgrade Profile

`packetsafari-ops` can also run the same transaction engine for the current
single-EC2 SaaS deployment:

```bash
packetsafari-ops upgrade --profile saas --manifest ./release-manifest.json
packetsafari-ops rollback --profile saas
```

The SaaS profile is intentionally different from on-prem:

- it skips customer entitlement checks because our SaaS host is operated by us
- it still validates the release manifest, upgrade path, required env, migrations, and health checks
- it defaults to `--backup-mode require-recent`, meaning it requires proof of a fresh external backup before migrations
- it records only metadata snapshots unless `--backup-mode inline` is selected

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

On-prem licenses are signed offline entitlement tokens. Current tokens carry explicit claims for `agent_enabled`, `max_users`, `max_agent_runs_per_month`, `offline_expiry`, `customer_id`, `deployment_id`, and `support_tier`.

Create and verify tokens with:

```bash
python3 scripts/license_create.py --private-key keys/license-private.pem --customer-id customer-acme --customer-email security@example.com --license-id lic-acme-001 --deployment-id dep-acme-prod --support-tier standard --max-users 25 --max-agent-runs-per-month 1000 --agent-enabled --days 365 --output /tmp/packetsafari-license-token.json
python3 scripts/license_verify.py --token /tmp/packetsafari-license-token.json --public-key keys/license-public.pem
```

See `docs/license-claims.md` for the full claim schema. The private signing key is internal-only and must never be installed on a customer host.

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
    "sharkd": "registry.example.com/packetsafari/sharkd@sha256:..."
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
    redis.tar.zst
    postgres.tar.zst
  image-metadata.json
  checksums.txt
  checksums.txt.sig
  license-token.json       # optional, for fresh install bundles
  license-public.pem       # development/local bundles only, never the production trust root
  release-public.pem       # optional copy of the verification public key
  sbom/
  release-notes.md
```

Large bundles may be split and copied as `packetsafari-10.0.1-offline.tar.zst.part-aa`, `.part-ab`, and so on. Pass any local part path to `packetsafari-ops upgrade --bundle`; the tool reassembles all matching parts, verifies the signature and checksums, then proceeds. Remote HTTP/HTTPS bundle URLs must point to the complete reassembled archive.

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
  --output-dir /Users/otr/packetsafari-data/releases/local/10.0.0-beta.9
```

Then serve the generated directory:

```bash
cd /Users/otr/packetsafari-data/releases/local/10.0.0-beta.9
python3 -m http.server 9000 --bind 0.0.0.0
```

On the Ubuntu VM:

```bash
export PACKETSAFARI_ONPREM_RAW_BASE=http://<mac-ip>:9000
export PACKETSAFARI_ONPREM_ARCHIVE_URL=http://<mac-ip>:9000/packetsafari-onprem.tar.gz
curl -fsSL http://<mac-ip>:9000/bootstrap.sh | sudo -E bash -s -- install \
  --bundle http://<mac-ip>:9000/packetsafari-10.0.0-beta.9-offline.tar.zst \
  --bundle-public-key http://<mac-ip>:9000/release-public.pem \
  --allow-bundled-license-public-key
```

The local release helper intentionally generates development-only license and release signing keys in the output directory. That is suitable for VM validation only. Customer releases must use PacketSafari-controlled signing keys and a customer-specific entitlement token from an authenticated distribution channel.
