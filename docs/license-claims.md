# On-Prem License Claims

PacketSafari on-prem license tokens are signed offline entitlement tokens. The installer and app verify the token locally with `keys/license-public.pem`.

The token envelope must declare `alg: RS256`. The installer and runtime both
fail closed when required claims are missing, malformed, expired, signed with a
different algorithm, or signed by a key that does not match the configured
PacketSafari public key.

## Required claims

| Claim | Description |
| --- | --- |
| `agent_enabled` | Enables or disables PacketSafari Agent analysis for the deployment. |
| `max_users` | Maximum enabled named users for the licensed deployment. `-1` means unlimited. |
| `max_analysis_runs_per_month` | Maximum completed full investigations per licensed deployment and calendar month. `-1` means unlimited. |
| `max_quick_questions_per_month` | Shared maximum for Copilot and lightweight standalone Agent-tab questions per licensed deployment and calendar month. `-1` means unlimited. |
| `max_prompt_coach_requests_per_month` | Maximum explicit Prompt Coach requests per licensed deployment and calendar month. `-1` means unlimited. |
| `offline_expiry` | Hard offline expiry timestamp. |
| `customer_id` | Stable PacketSafari customer identifier. |
| `deployment_id` | Stable deployment identifier. |
| `support_tier` | Support tier, usually `standard`, `priority`, or `enterprise`. |

## Analysis capacity

`max_analysis_cpu_slots` licenses concurrent heavy-analysis slots, not physical
CPU cores, total process CPU consumption, or RAM. A positive integer sets the
deployment-wide ceiling; `-1` is unlimited. Existing schema-1 licenses without
this claim remain unlimited. Zero, null, fractional and string values are invalid.

Issue a restricted license with `--max-analysis-cpu-slots 4` in the creation
command below. Restricted licenses use schema 2, which requires this claim;
older installers and runtimes reject schema 2 instead of silently ignoring its
capacity restriction. Upgrade both installer tooling and application before
installing one. Renewal preserves the allowance; legacy renewal stays unlimited.

The application locally verifies the signed license on admission, including
offline expiry. Effective heavy-analysis capacity is the minimum of the signed
allowance, hardware-derived capacity and administrator/team settings. Restoring
automatic settings does not remove the signed ceiling. RAM stays adaptive.
New work queues when licensed slots are occupied or the license is invalid;
running analyses are not terminated. The administrator capacity panel shows
the licensed and effective slots. Fast model work has separate provider limits;
this claim is not a general model-concurrency license or a multi-node CPU quota.

## Maximum host hardware

`max_host_cpus` (logical CPUs) and `max_host_ram_gib` (whole GiB) are optional
signed host ceilings. Each is a positive integer or `-1` for unlimited. Issue
them using `--max-host-cpus 8 --max-host-ram-gib 32`. A finite host ceiling uses
schema 3, which requires both host claims and `max_analysis_cpu_slots`. Older
installers/apps reject schema 3. Upgrade tooling and application together.
Legacy schemas 1 and 2 retain unlimited host size. Renewal preserves both limits.

The backend compares the signed ceilings with the Linux host/VM's logical CPU
count and `/proc/meminfo` total RAM, not the container CPU quota or memory limit.
Equality is allowed. Exceeding either ceiling, or inability to measure a limited
resource, invalidates runtime access. Login, MFA completion, token refresh, SSO,
existing-session API access and new guarded analyses are blocked. Hardware and
license files are read again on subsequent requests; replacement needs no license
cache reset. Existing executing analyses are not forcibly killed.

The pre-authentication `/license-required` page reports detected and licensed
hardware. Its public status endpoint exposes only hardware limits, measurements
and violations, with no customer identity, token, credentials or filesystem paths.
Health, logout and existing loopback-only onboarding recovery endpoints remain
available. A local administrator can verify a replacement token with the updated
`scripts/license_verify.py`, then atomically replace the deployment's configured
`state/license-token.json`, preserving its permissions and configured signing key.
Click **Check again** after replacement. Container quota reductions do not make
an oversized host eligible; use a qualifying host/VM or a broader license.

CLI signature verification validates the claims, not the signing machine's host
size. Runtime hardware enforcement is on the deployment's backend/worker host.
This does not qualify heterogeneous multi-node deployments or physical hardware
outside the Linux VM visible to the application.

## Optional upgrade claims

| Claim | Description |
| --- | --- |
| `channel` | Release channel allowed by the license. If both the license and release manifest set a channel, they must match. |
| `allowed_versions` | Optional exact allow-list of release versions. When present, `packetsafari-ops upgrade` refuses any target version not listed here. |

## Create

```bash
python3 scripts/license_create.py \
  --private-key keys/license-private.pem \
  --customer-id customer-acme \
  --customer-email security@example.com \
  --license-id lic-acme-001 \
  --deployment-id dep-acme-prod \
  --support-tier enterprise \
  --max-users 25 \
  --max-analysis-runs-per-month 100 \
  --max-quick-questions-per-month 100 \
  --max-prompt-coach-requests-per-month 500 \
  --agent-enabled \
  --channel stable \
  --allowed-version 10.0.1 \
  --days 365 \
  --output /tmp/packetsafari-license-token.json
```

The values above are examples; issue all three monthly limits from the signed customer contract. Use `--no-agent` to disable Agent analysis and `-1` only for a user or AI category that is contractually unlimited. Uploads, opening captures, same-investigation follow-ups, report stages, resumes, automatic retries, and internal tool/model calls do not consume these limits.

Renewing a current token preserves all three AI allowances, including zero, and its exact `allowed_versions` list. When renewing an older token, its legacy Agent allowance becomes the Analysis runs allowance; the previously nonexistent Quick questions and Prompt Coach limits become unlimited so renewal does not silently restrict the customer:

```bash
python3 scripts/license_renew.py \
  --token /tmp/packetsafari-license-token.json \
  --private-key keys/license-private.pem \
  --days 365 \
  --output /tmp/packetsafari-license-token-renewed.json
```

## Verify

```bash
python3 scripts/license_verify.py \
  --token /tmp/packetsafari-license-token.json \
  --public-key keys/license-public.pem
```

The private signing key must stay in internal release tooling only. It is never installed on a customer host.
