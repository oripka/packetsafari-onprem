# PacketSafari On-Prem Upgrade Runbook

## Host Prerequisites

Use Ubuntu Server 24.04 LTS or newer for fresh install and deployment drills.

Minimum single-node sizing:

- CPU: 4 vCPU
- RAM: 16 GiB
- Disk: 120 GiB root or data volume
- Architecture: match the release artifact, for example `linux-arm64` requires an ARM64 host

Install required packages:

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

## Customer Commands

```bash
packetsafari-ops status
packetsafari-ops update check
packetsafari-ops update apply
packetsafari-ops install --license ./license-token.json
packetsafari-ops install --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops upgrade
packetsafari-ops upgrade --manifest ./release-manifest.json
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops rollback
```

`update check`, `update apply`, `install --license`, and bare `upgrade` are the
normal connected operator flow. The tool still uses a release manifest
internally, but operators do not need to pass one each time. By default, the
tool resolves the PacketSafari release channel:

```text
https://releases.packetsafari.com/channels/<profile>/<channel>/<platform>/release-manifest.json
```

For PacketSafari-operated SaaS hosts, `packetsafari-ops update` is designed to
be a one-command host update:

```bash
sudo env HOME=/root packetsafari-ops update
```

The host infers the `saas` profile from the installed SaaS operator token or
release manifest and reads the private release manifest from S3 with the EC2
instance role:

```text
s3://packetsafari-release-channels-166826692770/channels/saas/stable/linux-arm64/release-manifest.json
```

This requires AWS CLI on the host and the root Docker ECR credential helper.
Do not use workstation AWS keys, public SaaS manifests, or copied CloudFront
signing material to simplify this path.

Use these environment variables only when testing a private/staged channel or a
customer-specific manifest:

```bash
export PACKETSAFARI_UPDATE_MANIFEST_URL='https://<portal-presigned-url>/release-manifest.json'
# or
export PACKETSAFARI_UPDATE_BASE_URL='https://downloads.example.com/packetsafari'
```

With a custom `PACKETSAFARI_UPDATE_BASE_URL`, the tool resolves:

```text
<base>/channels/<profile>/<channel>/<platform>/release-manifest.json
```

Examples:

```bash
packetsafari-ops update check --profile onprem --channel stable
packetsafari-ops update apply --profile onprem --channel stable
packetsafari-ops update apply --profile saas --channel stable --backup-mode require-recent
```

For a deliberate container-only fast path, use `--backup-mode skip` together
with the explicit safety acknowledgement:

```bash
packetsafari-ops update apply \
  --profile saas \
  --backup-mode skip \
  --allow-unbacked-upgrade
```

Only use unbacked updates for releases that are known not to require schema or
storage migrations, or on disposable development hosts. If migrations run,
rollback may require restoring PostgreSQL and `/storage` from an external
backup.

Use `upgrade --manifest` when the host can reach the image registry and you want
to apply a specific manifest manually. Use `upgrade --bundle`
when the host is air-gapped. Split bundles are accepted by passing any
local `.part-*` file. HTTP/HTTPS sources are also accepted for complete
manifest and bundle archives:

```bash
packetsafari-ops upgrade \
  --manifest https://downloads.example.com/packetsafari/release-manifest.json \
  --download-bearer-token "$TOKEN"

packetsafari-ops install \
  --bundle https://downloads.example.com/packetsafari/packetsafari-10.0.1-offline.tar.zst \
  --bundle-public-key https://downloads.example.com/packetsafari/release-public.pem \
  --download-header "x-api-key: $TOKEN"
```

The preferred customer source is the authenticated PacketSafari portal at
`/app/account/releases`. The portal returns short-lived CloudFront signed URLs
for private release artifacts; the S3 bucket remains private and blocked from
public access. Customers can download the files first and copy them to the
on-prem host, or pass the signed HTTPS URL directly to `packetsafari-ops` when
the host has outbound access. Do not use public S3 objects or a mutable GitHub
branch as the production install source.

For self-contained local HTTP, private S3 directory sync, or removable-media
installs, place `bootstrap.sh`, `bootstrap-manifest.json`, and
`packetsafari-onprem.tar.gz` next to the release files. `bootstrap.sh` prefers
those adjacent files before using configured HTTP/HTTPS URLs, so an operator can
install from a copied release directory without depending on public GitHub.

## Single-Host SaaS Commands

For the current SaaS deployment model where PacketSafari runs on one EC2 host,
use the same release artifacts but select the SaaS profile:

```bash
packetsafari-ops config check-env --profile saas --manifest ./release-manifest.json
packetsafari-ops config prompt-env --profile saas --manifest ./release-manifest.json
packetsafari-ops doctor --profile saas --manifest ./release-manifest.json
packetsafari-ops upgrade --profile saas --manifest ./release-manifest.json
packetsafari-ops rollback --profile saas
```

The SaaS profile skips customer license entitlement checks only after the host
proves it is a PacketSafari-operated SaaS deployment. Install a high-entropy
operator token at `/opt/packetsafari/secrets/saas-operator-token`, then put its
SHA-256 digest in either the release manifest or the host environment:

Before promotion, SaaS upgrades run `doctor --profile saas`. The doctor check
rejects missing `PACKETSAFARI_PUBLIC_BASE_URL`,
`PACKETSAFARI_PADDLE_WEBHOOK_SECRET`, and missing upstream OpenAI/Paddle API
keys. Upstream `OPENAI_API_KEY` and `PACKETSAFARI_PADDLE_API_KEY` belong in
`env/ironproxy.env`, which is mounted only into `egress-ironproxy`; the
backend/worker runtime env should contain the proxy placeholders instead. The
doctor then probes backend health/config, frontend `runtime-config.json`, and
Compose service state.

```json
{
  "deploymentProfiles": {
    "saas": {
      "operatorTokenSha256": "<sha256-of-token>"
    }
  }
}
```

Alternatively:

```bash
export PACKETSAFARI_SAAS_OPERATOR_TOKEN_SHA256="<sha256-of-token>"
```

Do not publish that token or hash to customer artifacts. This guard prevents a
customer from selecting `--profile saas` to bypass the on-prem license path.

The SaaS profile defaults to an out-of-band backup policy. By default, the
upgrade requires a fresh backup proof at
`/opt/packetsafari/state/latest-backup.json` before migrations run:

```json
{
  "provider": "aws-ebs",
  "snapshotId": "snap-0123456789abcdef0",
  "completedAt": "2026-05-11T10:30:00Z",
  "verifiedRestore": true
}
```

The proof file can also be supplied explicitly:

```bash
packetsafari-ops upgrade \
  --profile saas \
  --manifest ./release-manifest.json \
  --backup-proof /var/lib/packetsafari-backups/latest.json \
  --max-backup-age-minutes 120
```

If the EC2 host does not have a recent external snapshot, use an inline backup:

```bash
packetsafari-ops upgrade --profile saas --backup-mode inline --manifest ./release-manifest.json
```

Do not run SaaS upgrades without a data backup. `--backup-mode skip` is blocked
unless `PACKETSAFARI_ALLOW_UNBACKED_UPGRADE=true` is set for disposable
development hosts.

## Offline Bundle Requirements

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
  license-public.pem       # development/local bundles only
  release-public.pem       # optional public verification material
  sbom/
  release-notes.md
```

The host must already have the PacketSafari release public key at
`/opt/packetsafari/state/release-public.pem`, `/opt/packetsafari/secrets/release-public.pem`,
or the path passed with `--bundle-public-key`.

## Transaction Flow

Fresh `install --bundle` uses the same verification and image-loading path as
`upgrade --bundle`, but writes the initial active manifest and starts the stack
in onboarding mode.

1. Acquire an exclusive upgrade lock.
2. Verify the bundle signature and checksums, or copy the connected manifest.
3. Verify license expiry and release channel/version eligibility.
4. Check `minUpgradeableFrom`, `upgradeableFrom`, and required env keys.
5. Stop frontend, backend, worker, and sharkd.
6. Back up metadata, PostgreSQL, and `/storage`.
7. Pull connected images, or load offline image archives and retag them as local `packetsafari/<service>:<version>` refs.
8. Render the target Compose file.
9. Start PostgreSQL/Redis and run migrations from the target backend image.
10. Start all services with `docker compose up -d --pull never`.
11. Poll `http://127.0.0.1:8080/api/v2/health`.
12. Promote the release manifest only after health checks pass.

For `--profile saas --backup-mode require-recent`, step 6 records the verified
external backup proof instead of taking a local PostgreSQL and `/storage` dump.
The migration and promotion gates stay the same.

## Failure Behavior

- Before migration: restore manifest, env, state, and Compose files, then
  restart the old release.
- During or after migration: restore PostgreSQL and `/storage`, then restart
  the old release.
- After a successful upgrade: `rollback` restores the latest full snapshot.
  Do not assume old containers can safely run against a newer schema.
- During or after a SaaS migration with `--backup-mode require-recent`: restore
  runtime metadata automatically, keep traffic stopped, and restore data from
  the external backup before restarting service.

## Upgrade Failure Drills

Run these only on disposable development hosts. The simulation flag is guarded
so it cannot be triggered accidentally in customer environments.

```bash
PACKETSAFARI_ENABLE_UPGRADE_SIMULATION=true \
  packetsafari-ops upgrade \
    --bundle ./packetsafari-10.0.1-offline.tar.zst \
    --bundle-public-key ./release-public.pem \
    --backup-mode inline \
    --simulate-failure-phase migration
```

The target release must be newer than the active release. If the same manifest
or bundle is already active, the upgrade correctly exits during preflight with
`Release is already active`; create a synthetic next-version bundle only on a
disposable host when testing rollback mechanics.

Supported phases are `preflight`, `compose`, `migration`, `healthcheck`, and
`promote`. Migration, health-check, and promotion simulations create a temporary
PostgreSQL table and `/storage/upgrade-simulated-corruption.txt` before failing.
After rollback, verify both are absent and the host is serving the previous
release:

```bash
packetsafari-ops status --json
docker exec packetsafari-postgres psql -U packetsafari -d packetsafari \
  -Atc "select coalesce(to_regclass('public.packetsafari_upgrade_simulated_corruption')::text, 'absent');"
docker exec packetsafari-backend test ! -e /storage/upgrade-simulated-corruption.txt
curl -fsS http://127.0.0.1:3000/api/v2/health
```

Storage backups must not contain `/storage/onprem`; that path is the host
runtime root bind-mounted into the app containers, not customer capture data.
Check the latest snapshot with:

```bash
latest="$(find /opt/packetsafari/backups -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
tar -tf "$latest/storage.tar" | grep -E '(^|/)onprem(/|$)' && echo "invalid backup"
```

If `docker compose stop` hangs on a service, `packetsafari-ops` enforces a
host-side timeout and falls back to `docker compose kill` for the requested
services before continuing rollback or upgrade.

## Current Validation Status

On May 12, 2026, a disposable Ubuntu ARM64 EC2 host completed a fresh
`install --bundle` from a signed `10.0.0-beta.14` offline bundle. The same host
then ran a synthetic `10.0.0-beta.15` migration-failure drill with
`--backup-mode inline`. The simulation created both the PostgreSQL marker table
and `/storage/upgrade-simulated-corruption.txt`, failed during migration,
restored the prior data snapshot, restarted the previous release, and preserved
the already indexed PCAP and post-index artifacts.

## Bundle Build

```bash
python3 scripts/build_offline_bundle.py \
  --manifest ./release-manifest.json \
  --release-notes ./release-notes.md \
  --sbom-dir ./sbom \
  --sign-key /secure/internal/release-private.pem \
  --output ./packetsafari-10.0.1-offline.tar.zst
```

For large media constraints:

```bash
python3 scripts/build_offline_bundle.py \
  --manifest ./release-manifest.json \
  --sign-key /secure/internal/release-private.pem \
  --output ./packetsafari-10.0.1-offline.tar.zst \
  --split-size-mb 3900
```
