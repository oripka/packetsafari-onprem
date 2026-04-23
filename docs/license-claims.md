# On-Prem License Claims

PacketSafari on-prem license tokens are signed offline entitlement tokens. The installer and app verify the token locally with `keys/license-public.pem`.

## Required claims

| Claim | Description |
| --- | --- |
| `agent_enabled` | Enables or disables PacketSafari Agent runs for the deployment. |
| `max_users` | Maximum registered non-anonymous users. `-1` means unlimited. |
| `max_agent_runs_per_month` | Maximum deployment-wide Agent runs per calendar month. `-1` means unlimited. |
| `offline_expiry` | Hard offline expiry timestamp. |
| `customer_id` | Stable PacketSafari customer identifier. |
| `deployment_id` | Stable deployment identifier. |
| `support_tier` | Support tier, usually `standard`, `priority`, or `enterprise`. |

## Create

```bash
python3 scripts/license_create.py \
  --private-key keys/license-private.pem \
  --customer-id customer-acme \
  --customer-email security@example.com \
  --license-id lic-acme-001 \
  --deployment-id dep-acme-prod \
  --support-tier standard \
  --max-users 25 \
  --max-agent-runs-per-month 1000 \
  --agent-enabled \
  --channel stable \
  --days 365 \
  --output /tmp/packetsafari-license-token.json
```

Use `--no-agent` to issue a license that blocks Agent runs. Use `-1` for unlimited user or Agent-run limits.

## Verify

```bash
python3 scripts/license_verify.py \
  --token /tmp/packetsafari-license-token.json \
  --public-key keys/license-public.pem
```

The private signing key must stay in internal release tooling only. It is never installed on a customer host.
