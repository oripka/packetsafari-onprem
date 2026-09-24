"""Opt-in Docker/NGINX cutover. Does not stop backends or run migrations."""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
import hashlib
import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

PROXY_IMAGE = "nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check, timeout=15)


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name).stdout)[0]


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def ingress_policy(value=None):
    value = {'mode': 'direct', 'trustedCidrs': []} if value is None else value
    if not isinstance(value, dict) or set(value) - {'mode', 'trustedCidrs', 'viewerHttpsOnly'}:
        raise ValueError('Invalid ingress policy fields')
    mode = value.get('mode')
    cidrs = value.get('trustedCidrs', [])
    if mode not in ('direct', 'forwarded', 'cloudfront-https') or not isinstance(cidrs, list):
        raise ValueError('Ingress mode must be direct, forwarded or cloudfront-https')
    networks = [ipaddress.ip_network(cidr, strict=True) for cidr in cidrs if isinstance(cidr, str)]
    if len(networks) != len(cidrs) or any(net.prefixlen == 0 for net in networks):
        raise ValueError('Ingress requires explicit trusted CIDRs, never a default route')
    if (mode == 'direct' and cidrs) or (mode != 'direct' and not cidrs):
        raise ValueError('Only forwarded ingress modes require trusted CIDRs')
    if mode == 'cloudfront-https' and value.get('viewerHttpsOnly') is not True:
        raise ValueError('CloudFront mode requires a verified HTTPS-only or redirect-to-HTTPS viewer policy')
    return {'mode': mode, 'trustedCidrs': [str(net) for net in networks],
            'viewerHttpsOnly': value.get('viewerHttpsOnly', False)}


def ingress_configuration(policy):
    policy = ingress_policy(policy)
    trusted = '\n'.join(f'        {cidr} 1;' for cidr in policy['trustedCidrs'])
    realip = '\n'.join(f'    set_real_ip_from {cidr};' for cidr in policy['trustedCidrs'])
    if policy['mode'] == 'forwarded':
        scheme = '''map "$trusted_ingress:$http_x_forwarded_proto" $ingress_scheme {
        default ""; ~^0: $scheme; "1:http" http; "1:https" https;
    }'''
    else:
        scheme = 'map $trusted_ingress $ingress_scheme { default $scheme; 1 https; }'
    return f'''{realip}
    real_ip_header X-Forwarded-For;
    real_ip_recursive off;
    geo $realip_remote_addr $trusted_ingress {{ default 0;
{trusted}
    }}
    {scheme}
    map $trusted_ingress $unresolved_client {{ default ""; 1 $realip_remote_addr; }}
    map "$trusted_ingress:$http_x_forwarded_for" $missing_client {{ default 0; "1:" 1; }}'''


def configuration(target: str | None, generation: str, policy=None, sharkd_target=None) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9-]+", generation):
        raise ValueError("Invalid generation")
    if target is not None and not re.fullmatch(r"[0-9.]+:[0-9]+", target):
        raise ValueError("Target must be a resolved IPv4 address and port")
    if sharkd_target is not None and not re.fullmatch(r"[0-9.]+:[0-9]+", sharkd_target):
        raise ValueError("Sharkd target must be a resolved IPv4 address and port")
    route = "return 503;" if target is None else f"""
        proxy_pass http://{target};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $ingress_scheme;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Port "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Forwarded "";
        proxy_set_header CF-Connecting-IP "";
        proxy_set_header CloudFront-Forwarded-Proto "";
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_next_upstream off;
        proxy_connect_timeout 3s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    """
    sharkd_route = route.replace(f'http://{target};', f'http://{sharkd_target};') if target and sharkd_target else 'return 503;'
    return f"""worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr notice;
events {{ worker_connections 2048; }}
http {{
    {ingress_configuration(policy)}
    access_log /dev/stdout;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path /tmp/proxy;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' ''; }}
    server {{
        listen 8080;
        client_max_body_size 12g;
        if ($ingress_scheme = "") {{ return 400; }}
        if ($missing_client = 1) {{ return 400; }}
        if ($remote_addr = $unresolved_client) {{ return 400; }}
        location / {{ {route} }}
    }}
    server {{
        listen 4448;
        if ($ingress_scheme = "") {{ return 400; }}
        if ($missing_client = 1) {{ return 400; }}
        if ($remote_addr = $unresolved_client) {{ return 400; }}
        location / {{ {sharkd_route} }}
    }}
    server {{
        listen 127.0.0.1:8099;
        access_log off;
        location = /generation {{ return 200 '{generation}'; }}
    }}
}}
"""


def initialize(directory: Path, policy=None) -> None:
    policy = ingress_policy(policy)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / "nginx.conf").exists():
            raise ValueError("Proxy configuration already exists; refusing to overwrite")
        config = configuration(None, "unconfigured", policy)
        atomic_write(directory / "nginx.conf", config)
        atomic_write(directory / "state.json", json.dumps({"generation": "unconfigured", "retiringWorkers": [], "ingressPolicy": policy,
                     "configSha256": hashlib.sha256(config.encode()).hexdigest()}))


def workers(proxy: str) -> set[str]:
    lines = docker("top", proxy, "-eo", "pid,args").stdout.splitlines()
    return {line.split()[0] for line in lines if "nginx: worker process" in line}


def generation(proxy: str) -> str:
    return docker("exec", proxy, "wget", "-qO-", "-T", "2", "http://127.0.0.1:8099/generation").stdout.strip()


def reconcile(directory: Path, proxy: str) -> None:
    """Caller holds proxy lock. Resolve a process death around NGINX reload."""
    pending_path = directory / 'pending.json'
    if not pending_path.exists():
        return
    pending = json.loads(pending_path.read_text())
    live = generation(proxy)
    if live not in (pending['next']['generation'], pending['previous']['generation']):
        raise ValueError('Interrupted reload has an unknown live generation; keep both backends running')
    # Complete the already-validated reload. A prior SIGHUP may still be queued,
    # so observing the old generation once is not proof that reload was aborted.
    atomic_write(directory / 'nginx.conf', pending['nextConfig'])
    docker('exec', proxy, 'nginx', '-s', 'reload', '-c', '/etc/packetsafari-proxy/nginx.conf')
    deadline = time.monotonic() + 10
    while generation(proxy) != pending['next']['generation']:
        if time.monotonic() >= deadline:
            raise RuntimeError('Interrupted reload did not settle; retain both backends')
        time.sleep(.05)
    atomic_write(directory / 'state.json', json.dumps(pending['next'], indent=2))
    pending_path.unlink()


def address(proxy_info: dict, target_info: dict, port: int) -> str:
    if not target_info["State"]["Running"] or not 1 <= port <= 65535:
        raise ValueError("Target must be running and port must be valid")
    networking = target_info
    # Local dev slots can share the existing backend's governed network namespace.
    mode = target_info["HostConfig"].get("NetworkMode", "")
    if mode.startswith("container:"):
        networking = inspect(mode.removeprefix("container:"))
    common = set(proxy_info["NetworkSettings"]["Networks"]) & set(networking["NetworkSettings"]["Networks"])
    if len(common) != 1:
        raise ValueError("Proxy and target must share exactly one Docker network")
    ip = networking["NetworkSettings"]["Networks"][common.pop()]["IPAddress"]
    if not re.fullmatch(r"[0-9.]+", ip):
        raise ValueError("No target IPv4 address")
    return f"{ip}:{port}"


def switch(directory: Path, proxy: str, target: str, *, port: int = 80,
           health_path: str = "/api/v2/health", ready_timeout: float = 120,
           drain_timeout: float = 120, sharkd: str | None = None) -> dict:
    if not re.fullmatch(r"/[a-zA-Z0-9/_-]*", health_path):
        raise ValueError("Health path must be a literal absolute path")
    if not 0 < ready_timeout <= 600 or not 0 <= drain_timeout <= 3600:
        raise ValueError("Invalid readiness/drain timeout")
    directory = directory.resolve()
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reconcile(directory, proxy)
        state = json.loads((directory / "state.json").read_text())
        config_hash = hashlib.sha256((directory / "nginx.conf").read_bytes()).hexdigest()
        if state.get("configSha256") != config_hash:
            raise ValueError("Configuration changed outside the cutover transaction; inspect before proceeding")
        proxy_info, target_info = inspect(proxy), inspect(target)
        mounted = any(m["Destination"] == "/etc/packetsafari-proxy" and
                      Path(m["Source"]).resolve() == directory for m in proxy_info["Mounts"])
        if not mounted:
            raise ValueError("Proxy does not mount this configuration directory")
        if generation(proxy) != state["generation"]:
            raise ValueError("Live proxy and receipt disagree; inspect before another cutover")
        pending = set(state.get("retiringWorkers", [])) & workers(proxy)
        returning_to_retained = (target_info['Id'] == state.get('previousContainerId') and
                                 target_info['State']['StartedAt'] == state.get('previousStartedAt'))
        already_serving = (target_info['Id'] == state.get('containerId') and
                           target_info['State']['StartedAt'] == state.get('targetStartedAt'))
        if pending and not returning_to_retained and not already_serving:
            raise ValueError("Previous backend still draining; keep both slots running")
        endpoint = address(proxy_info, target_info, port)
        sharkd_endpoint = (address(proxy_info, inspect(sharkd), 4448) if sharkd else
                          state.get('previousSharkdEndpoint') if returning_to_retained else state.get('sharkdEndpoint'))
        if already_serving and endpoint == state.get('endpoint') and sharkd_endpoint == state.get('sharkdEndpoint'):
            return {**state, 'status': 'draining' if pending else 'drained', 'retiringWorkers': sorted(pending)}
        started = time.monotonic()
        deadline = started + ready_timeout
        while True:
            result = docker("exec", proxy, "wget", "-S", "-O", "/dev/null", "-T", "2",
                            f"http://{endpoint}{health_path}", check=False)
            statuses = re.findall(r"HTTP/\S+\s+(\d{3})", result.stderr)
            if result.returncode == 0 and statuses == ["200"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Candidate failed readiness; active configuration unchanged")
            time.sleep(0.2)
        current_target = inspect(target)
        if (current_target["Id"] != target_info["Id"] or
                current_target["State"]["StartedAt"] != target_info["State"]["StartedAt"] or
                not current_target["State"]["Running"]):
            raise RuntimeError("Target changed during readiness")
        old_workers = workers(proxy)
        old_config = (directory / "nginx.conf").read_text()
        next_generation = uuid.uuid4().hex
        candidate = directory / "candidate.conf"
        policy = ingress_policy(state.get('ingressPolicy'))
        atomic_write(candidate, configuration(endpoint, next_generation, policy, sharkd_endpoint))
        docker("exec", proxy, "nginx", "-t", "-c", "/etc/packetsafari-proxy/candidate.conf")
        next_state = {"generation": next_generation, "target": target, "endpoint": endpoint, "ingressPolicy": policy,
                      "sharkdEndpoint": sharkd_endpoint,
                      "previousSharkdEndpoint": state.get('sharkdEndpoint'),
                      "containerId": target_info['Id'], "imageId": target_info['Image'],
                      "targetStartedAt": target_info['State']['StartedAt'],
                      "previousContainerId": state.get('containerId'),
                      "previousStartedAt": state.get('targetStartedAt'),
                      "previousTarget": state.get('target'), "retiringWorkers": sorted(old_workers),
                      "configSha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
        atomic_write(directory / 'pending.json', json.dumps({'previous': state, 'next': next_state,
                     'previousConfig': old_config, 'nextConfig': candidate.read_text()}))
        atomic_write(directory / "nginx.conf", candidate.read_text())
        try:
            docker("exec", proxy, "nginx", "-s", "reload", "-c", "/etc/packetsafari-proxy/nginx.conf")
            until = time.monotonic() + 10
            while generation(proxy) != next_generation:
                if time.monotonic() > until:
                    raise RuntimeError("Reload not acknowledged; inspect proxy before proceeding")
                time.sleep(0.05)
        except BaseException:
            # Restore restart configuration too. Never stop either backend here.
            atomic_write(directory / "nginx.conf", old_config)
            docker("exec", proxy, "nginx", "-s", "reload", "-c", "/etc/packetsafari-proxy/nginx.conf", check=False)
            raise
        state = {"generation": next_generation, "target": target, "endpoint": endpoint, "ingressPolicy": policy,
                 "switchedAt": datetime.now(timezone.utc).isoformat(),
                 "configSha256": hashlib.sha256((directory / "nginx.conf").read_bytes()).hexdigest(),
                 "containerId": target_info["Id"], "imageId": target_info["Image"],
                 "targetStartedAt": target_info['State']['StartedAt'],
                 "previousContainerId": state.get('containerId'),
                 "previousStartedAt": state.get('targetStartedAt'),
                 "previousTarget": state.get("target"), "retiringWorkers": sorted(old_workers),
                 "readinessAndSwitchSeconds": round(time.monotonic() - started, 3)}
        # Persist before waiting: an interrupted CLI must remember pending drain.
        atomic_write(directory / "state.json", json.dumps(state, indent=2))
        (directory / 'pending.json').unlink()
        until = time.monotonic() + drain_timeout
        while old_workers & workers(proxy) and time.monotonic() < until:
            time.sleep(0.1)
        state["retiringWorkers"] = sorted(old_workers & workers(proxy))
        state["status"] = "draining" if state["retiringWorkers"] else "drained"
        state["totalSeconds"] = round(time.monotonic() - started, 3)
        atomic_write(directory / "state.json", json.dumps(state, indent=2))
        atomic_write(directory / f"receipt-{next_generation}.json", json.dumps(state, indent=2))
        return state
