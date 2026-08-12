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
