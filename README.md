# PacketSafari On-Prem

Customer-facing Python-native installer, operator CLI, and simple interactive menu for PacketSafari on-prem.

## Layout

- `bootstrap.sh` - GitHub-hosted `curl | bash` bootstrap shim that downloads the bundle and launches the Python CLI
- `packetsafari_onprem/` - Python control plane for install, status, onboarding, upgrade, rollback, diagnostics, and the interactive operator menu
- `scripts/license_*.py` - offline entitlement token tooling
- `docs/license-claims.md` - signed entitlement claim schema and internal issuance commands
- `scripts/render_compose.py` - render pinned on-prem compose files from release manifests
- `scripts/configure_logging.py` - audit logging defaults helper
- `scripts/render_logging_config.py` - render Vector config for optional audit log forwarding
- `templates/docker-compose.onprem.yml.tpl` - compose template rendered during install and upgrade

## Bootstrap

Supported customer entrypoints:

```bash
curl -fsSL https://raw.githubusercontent.com/oripka/packetsafari-onprem/main/bootstrap.sh | bash -s -- install --license /path/to/license-token.json --manifest ./release-manifest.json
```

```bash
curl -fsSL https://raw.githubusercontent.com/oripka/packetsafari-onprem/main/bootstrap.sh | bash -s -- tui
```

```bash
curl -fsSL https://raw.githubusercontent.com/oripka/packetsafari-onprem/main/bootstrap.sh | bash -s -- upgrade --manifest ./release-manifest.json
```

```bash
curl -fsSL https://raw.githubusercontent.com/oripka/packetsafari-onprem/main/bootstrap.sh | bash -s -- rollback
```

`bootstrap.sh` downloads the full on-prem bundle, verifies the Python CLI checksum from `bootstrap-manifest.json`, and then launches the operator menu directly with `python3`.

## Host Prerequisites

- Linux host with Docker and the Docker Compose plugin installed
- `curl`, `python3`, and `tar` available on the host
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
- `tooling/onprem/` - installed Python operator bundle
- `bin/packetsafari-ops` - stable local command that runs the installed operator tool through `python3`

That host runtime root is bind-mounted into the app containers at `/storage/onprem`, so host tooling and the running PacketSafari services operate on the same state files.

## Python CLI / Menu

Primary commands:

```bash
packetsafari-ops install --license /path/to/license-token.json --manifest ./release-manifest.json --non-interactive
packetsafari-ops status --json
packetsafari-ops tui
packetsafari-ops upgrade --manifest ./release-manifest.json
packetsafari-ops rollback
packetsafari-ops onboard schema
packetsafari-ops config show
packetsafari-ops iam show-initial-admin-command --email admin@example.com
packetsafari-ops diagnostics restart
```

The installed wrapper is written to `/opt/packetsafari/bin/packetsafari-ops` during install and does not require the operator to create a venv manually.

## Operator Workflow

- `install` validates the signed license token, writes runtime state under the managed root, installs the Python bundle, renders compose and logging config, and starts PacketSafari in onboarding mode.
- `tui` is the primary operator interface. It is a simple menu runner with back navigation.
- When `PACKETSAFARI_DATA_ROOT` or `~/packetsafari-data` exists, the menu defaults to that local dev layout. Otherwise it defaults to `/opt/packetsafari`.
- Native onboarding uses the existing local `/api/v2/onprem/onboarding/*` APIs. The menu can show schema output and validate, save, or finalize pasted draft JSON directly from the terminal.
- Generated-capable internal deployment secrets are now registry-driven. The onboarding schema distinguishes generated-capable platform secrets from manual-only external credentials.
- Finalizing onboarding writes the managed `runtime.env`, flips the deployment out of onboarding mode on the next restart, and then requires manual first-admin creation from inside the backend container.
- `upgrade` snapshots manifest, env, deployment state, and compose files before applying the target release.
- `rollback` restores the latest snapshot and restarts the stack.

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
    "securityworker": "registry.example.com/packetsafari/securityworker@sha256:...",
    "sharkd": "registry.example.com/packetsafari/sharkd@sha256:..."
  }
}
```

`scripts/render_compose.py` also accepts the richer `imageDetails.*.image` form emitted by the app release helper, but the rendered compose bundle always uses plain Docker image references.
