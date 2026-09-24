# Local blue/green API deployment proxy

The opt-in `deployment-proxy` command uses a digest-pinned NGINX container and
ordinary Docker Compose. It is locally tested development functionality, not
part of `packetsafari-ops update`, install, or the production Compose template.
Do not put production behind it and then run the existing stop-first updater.

## Local development

Run from the sibling app repository with its development backend already running:

```bash
python3 scripts/dev_deployment_proxy.py up \
  --directory /Users/otr/packetsafari-data/runs/deployment-proxy/my-dev-cutover
python3 scripts/dev_deployment_proxy.py switch --slot green \
  --directory /Users/otr/packetsafari-data/runs/deployment-proxy/my-dev-cutover
python3 scripts/dev_deployment_proxy.py switch --slot blue \
  --directory /Users/otr/packetsafari-data/runs/deployment-proxy/my-dev-cutover
```

The new endpoint is `http://127.0.0.1:18080`. Existing port 8080 and the current
frontend remain unchanged. Normally one slot runs; switching starts the other
and stops the previous slot only after the proxy workers finish draining.
`status` shows the containers and receipt; `down` removes only this project's
containers, leaving shared storage, caches, and retained evidence in place.

Both local slots use the existing backend's immutable image ID and source
mounts, with one uWSGI process each. They share its network namespace on ports
18081/18082, preserving the current egress identity instead of adding an
uncontrolled backend IP. They share PostgreSQL, Redis, storage, and the existing
stream gateway. This does not copy captures/databases or run migrations or AI.
The original backend must remain running; rebuilding it requires recreating the
local proxy experiment in a new directory. This dev namespace arrangement is
not the proposed production topology.

The generated Compose file contains the existing private runtime environment.
It is created with mode 0600 in a private directory outside Git. Do not attach
or publish that file. Receipts contain image/container identities and timing,
not environment values. Logs may contain request paths; retain them privately.

## Cutover contract

1. Check that the target is running and shares exactly one network with the proxy.
2. Probe `/api/v2/health` from the proxy network until it returns exactly HTTP 200.
   Redirects and errors do not qualify. The caller remains responsible for
   application-specific readiness beyond this HTTP contract.
3. Validate the candidate NGINX configuration before changing the active file.
4. Atomically replace the active file, gracefully reload, and read back the new
   generation through the proxy's loopback-only control listener.
5. Persist the target container/image, timestamp, configuration hash, and old
   worker PIDs before waiting for drain. Configuration drift or conflicting
   operations fail closed. A subsequent cutover is blocked while old workers live.
6. Return `drained` or `draining`. A drain wait expiring does **not** kill the old
   worker or backend. SSE, WebSocket, and upload requests may need longer than a
   normal request. The caller must retain the old slot while draining.

There is no forced worker-shutdown deadline, no upload/response buffering, and
no automatic retry that could replay an application write. Inactivity timeouts
are 3600 seconds; this does not promise indefinite survival for idle connections.
The cutover primitive never stops containers. Rollback is a readiness-checked
switch to the retained compatible backend. Post-cutover application failures do
not trigger automatic rollback. Proxy restarts recover the on-disk routing but
interrupt current connections; single-host/proxy failure is not high availability.

For an explicitly prepared proxy container mounting the initialized directory
at `/etc/packetsafari-proxy`, the lower-level entry points are:

```bash
packetsafari-ops deployment-proxy init --state-dir /path/to/private/proxy
packetsafari-ops deployment-proxy switch --state-dir /path/to/private/proxy \
  --proxy-container example-proxy --target-container example-green \
  --target-port 80 --ready-timeout 120 --drain-timeout 120
```

Initialization creates configuration only. The app dev helper creates and starts
the proxy container with the pinned image from `deployment_proxy.py`.
A failed/uncertain reload leaves both backends running. If a process is interrupted
between config activation and receipt persistence, generation/hash mismatch
blocks another operation; inspect live generation, config and containers before
reconciling. Do not simply delete the receipt to silence the disagreement.

## Verification

Deterministic checks, from this repository:

```bash
python3 -m unittest discover -s tests -p test_deployment_proxy.py -v
python3 scripts/test_deployment_proxy_local.py
```

The Docker test creates a unique project and records evidence beneath
`$PACKETSAFARI_DATA_ROOT/runs/deployment-proxy/<run>/`. It tests a refused unhealthy
candidate, successful cutover, a byte-verified slow upload, complete SSE and
WebSocket sequences across cutover, refusal to reuse a draining slot, rollback,
and proxy restart recovery. Availability polling excludes the deliberate proxy
restart. It removes only its own fixture containers/network. If a local execution
policy restricts loopback ports, run it from the app workspace where local dev
ports are allowed; do not disable security controls.

From the app repository, with the local helper initialized:

```bash
python3 scripts/test_dev_deployment_proxy.py \
  --directory /Users/otr/packetsafari-data/runs/deployment-proxy/my-dev-cutover
```

This tests cold starts and cutover using the real development API, stops the old
slot to prove traffic moved, switches back, and retains availability samples.
It leaves blue serving through the proxy and green stopped. It does not create
an investigation or establish signed-release compatibility: both slots use the
same development image/source mounts. Synthetic transport coverage does not
qualify authenticated production uploads or ongoing Agent investigations.

## Production integration boundary

Before enabling the normal host update transaction, integrate signed target
selection and proxy image delivery with the manifest, choose an egress-governed
slot topology, validate trusted ingress/TLS headers, and implement recovery of
interrupted update transactions. First installation requires moving the existing
public port; that is separate from subsequent graceful cutovers. Database changes
must permit version overlap, and worker/stream-gateway lifecycle must preserve
active investigations. Worker draining, incompatible migrations, CloudFront
publication, and production timing are not covered by this local API feature.
