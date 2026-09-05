# PacketSafari On-Prem — Working Notes

## Product Vision

- Before planning or implementing product or delivery work, read the canonical
  PacketSafari vision at `../packetsafari/VISION.md` and use it as the decision
  framework for scope, tradeoffs, and prioritization.
- On-prem work should advance the shared product vision without silently
  weakening evidence quality, reliability, privacy, security, operability, or
  commercial viability.

## Scope

- This repo owns the customer-facing on-prem bootstrap, signed entitlement tooling, release manifest handling, compose rendering, and host install / upgrade / rollback operations.
- The main app repo lives at `../packetsafari` and owns backend/frontend behavior
  and Docker image builds. PacketSafari-operated SaaS Compose is rendered by
  `packetsafari-ops`; the app repo's `docker-compose-production.yml` is not that
  production runtime. Follow
  `../packetsafari/documentation/internal/5.release-and-deployment/3.saas-build-push-and-update.md`
  for SaaS releases.

## Compose Templates

- On-prem compose is rendered from `templates/docker-compose.onprem.yml.tpl`. Do not assume changes to `../packetsafari/docker-compose-production.yml` automatically apply to on-prem installs.
- When changing service definitions that affect on-prem behavior, update `templates/docker-compose.onprem.yml.tpl` in the same pass.
- Host-mounted egress config templates live under `templates/egress-config/`. Keep these aligned with the main app repo's `configuration/egress-*` files when firewall, proxy, DNS, or allowlist behavior changes.
- Validate compose template changes by rendering with `scripts/render_compose.py` and then running `docker compose config` against the rendered file. Provide dummy env files/secrets as needed for local validation.

## Release Boundary

- Keep ECR for images and release manifests, not for hosting raw installer scripts.
- The release manifest pins image references. This repo renders those pinned images into on-prem compose rather than building app images itself.
