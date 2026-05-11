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
packetsafari-ops install --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops upgrade --manifest ./release-manifest.json
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops rollback
```

Use `--manifest` when the host can reach the image registry. Use `--bundle`
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
`/app/account/releases`. The portal returns short-lived presigned URLs for
private S3 release artifacts. Customers can download the files first and copy
them to the on-prem host, or pass the presigned HTTPS URL directly to
`packetsafari-ops` when the host has outbound access. Do not use public S3
objects or a mutable GitHub branch as the production install source.

## Single-Host SaaS Commands

For the current SaaS deployment model where PacketSafari runs on one EC2 host,
use the same release artifacts but select the SaaS profile:

```bash
packetsafari-ops config check-env --profile saas --manifest ./release-manifest.json
packetsafari-ops config prompt-env --profile saas --manifest ./release-manifest.json
packetsafari-ops upgrade --profile saas --manifest ./release-manifest.json
packetsafari-ops rollback --profile saas
```

The SaaS profile skips customer license entitlement checks only after the host
proves it is a PacketSafari-operated SaaS deployment. Install a high-entropy
operator token at `/opt/packetsafari/secrets/saas-operator-token`, then put its
SHA-256 digest in either the release manifest or the host environment:

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
