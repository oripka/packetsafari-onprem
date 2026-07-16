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
| `max_agent_runs_per_month` | Maximum weighted Agent units per licensed deployment and calendar month. `-1` means unlimited. The legacy claim name is retained for runtime compatibility. |
| `offline_expiry` | Hard offline expiry timestamp. |
| `customer_id` | Stable PacketSafari customer identifier. |
| `deployment_id` | Stable deployment identifier. |
| `support_tier` | Support tier, usually `standard`, `priority`, or `enterprise`. |

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
  --max-agent-units-per-month 5000 \
  --agent-enabled \
  --channel stable \
  --allowed-version 10.0.1 \
  --days 365 \
  --output /tmp/packetsafari-license-token.json
```

The unit schedule is Agent = 1, Agent Deep = 2, Agent Max = 2, and Agent Max Deep = 4. The default token allowance is 5,000 units per calendar month for each licensed deployment.

Use `--no-agent` to disable Agent analysis. Use `-1` for unlimited user or Agent-unit limits. The older `--max-agent-runs-per-month` command-line option remains accepted for compatibility, but new operational procedures should use `--max-agent-units-per-month`.

Renewing a token preserves its Agent-unit allowance, including zero, and its exact `allowed_versions` list:

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
