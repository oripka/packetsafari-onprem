# PacketSafari On-Prem Upgrade Runbook

## Customer Commands

```bash
packetsafari-ops status
packetsafari-ops upgrade --manifest ./release-manifest.json
packetsafari-ops upgrade --bundle /media/usb/packetsafari-10.0.1-offline.tar.zst
packetsafari-ops rollback
```

Use `--manifest` when the host can reach the image registry. Use `--bundle`
when the host is air-gapped. Split bundles are accepted by passing any
`.part-*` file.

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
  sbom/
  release-notes.md
```

The host must already have the PacketSafari release public key at
`/opt/packetsafari/state/release-public.pem`, `/opt/packetsafari/secrets/release-public.pem`,
or the path passed with `--bundle-public-key`.

## Transaction Flow

1. Acquire an exclusive upgrade lock.
2. Verify the bundle signature and checksums, or copy the connected manifest.
3. Verify license expiry and release channel/version eligibility.
4. Check `minUpgradeableFrom`, `upgradeableFrom`, and required env keys.
5. Stop frontend, backend, worker, and sharkd.
6. Back up metadata, PostgreSQL, and `/storage`.
7. Pull connected images, or load offline image archives and retag local refs.
8. Render the target Compose file.
9. Start PostgreSQL/Redis and run migrations from the target backend image.
10. Start all services with `docker compose up -d --pull never`.
11. Poll `http://127.0.0.1:8080/api/v2/health`.
12. Promote the release manifest only after health checks pass.

## Failure Behavior

- Before migration: restore manifest, env, state, and Compose files, then
  restart the old release.
- During or after migration: restore PostgreSQL and `/storage`, then restart
  the old release.
- After a successful upgrade: `rollback` restores the latest full snapshot.
  Do not assume old containers can safely run against a newer schema.

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
