"""Opt-in Docker/NGINX cutover. Does not stop backends or run migrations."""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
import hashlib
import json
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


def configuration(target: str | None, generation: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9-]+", generation):
        raise ValueError("Invalid generation")
    if target is not None and not re.fullmatch(r"[0-9.]+:[0-9]+", target):
        raise ValueError("Target must be a resolved IPv4 address and port")
    route = "return 503;" if target is None else f"""
        proxy_pass http://{target};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_next_upstream off;
        proxy_connect_timeout 3s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    """
    return f"""worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr notice;
events {{ worker_connections 2048; }}
http {{
    access_log /dev/stdout;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path /tmp/proxy;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' ''; }}
    server {{
        listen 8080;
        client_max_body_size 12g;
        location / {{ {route} }}
    }}
    server {{
        listen 127.0.0.1:8099;
        access_log off;
        location = /generation {{ return 200 '{generation}'; }}
    }}
}}
"""


def initialize(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / "nginx.conf").exists():
            raise ValueError("Proxy configuration already exists; refusing to overwrite")
        config = configuration(None, "unconfigured")
        atomic_write(directory / "nginx.conf", config)
        atomic_write(directory / "state.json", json.dumps({"generation": "unconfigured", "retiringWorkers": [],
                     "configSha256": hashlib.sha256(config.encode()).hexdigest()}))


def workers(proxy: str) -> set[str]:
    lines = docker("top", proxy, "-eo", "pid,args").stdout.splitlines()
    return {line.split()[0] for line in lines if "nginx: worker process" in line}


def generation(proxy: str) -> str:
    return docker("exec", proxy, "wget", "-qO-", "-T", "2", "http://127.0.0.1:8099/generation").stdout.strip()


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
           drain_timeout: float = 120) -> dict:
    if not re.fullmatch(r"/[a-zA-Z0-9/_-]*", health_path):
        raise ValueError("Health path must be a literal absolute path")
    if not 0 < ready_timeout <= 600 or not 0 <= drain_timeout <= 3600:
        raise ValueError("Invalid readiness/drain timeout")
    directory = directory.resolve()
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        if set(state.get("retiringWorkers", [])) & workers(proxy):
            raise ValueError("Previous backend still draining; keep both slots running")
        endpoint = address(proxy_info, target_info, port)
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
        atomic_write(candidate, configuration(endpoint, next_generation))
        docker("exec", proxy, "nginx", "-t", "-c", "/etc/packetsafari-proxy/candidate.conf")
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
        state = {"generation": next_generation, "target": target, "endpoint": endpoint,
                 "switchedAt": datetime.now(timezone.utc).isoformat(),
                 "configSha256": hashlib.sha256((directory / "nginx.conf").read_bytes()).hexdigest(),
                 "containerId": target_info["Id"], "imageId": target_info["Image"],
                 "previousTarget": state.get("target"), "retiringWorkers": sorted(old_workers),
                 "readinessAndSwitchSeconds": round(time.monotonic() - started, 3)}
        # Persist before waiting: an interrupted CLI must remember pending drain.
        atomic_write(directory / "state.json", json.dumps(state, indent=2))
        until = time.monotonic() + drain_timeout
        while old_workers & workers(proxy) and time.monotonic() < until:
            time.sleep(0.1)
        state["retiringWorkers"] = sorted(old_workers & workers(proxy))
        state["status"] = "draining" if state["retiringWorkers"] else "drained"
        state["totalSeconds"] = round(time.monotonic() - started, 3)
        atomic_write(directory / "state.json", json.dumps(state, indent=2))
        atomic_write(directory / f"receipt-{next_generation}.json", json.dumps(state, indent=2))
        return state
