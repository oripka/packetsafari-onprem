from __future__ import annotations

import fcntl
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .envfile import parse_env_file, quote_env_value, write_env_file

DEFAULT_RUNTIME_ROOT = "/opt/packetsafari"
DEFAULT_CONTAINER_RUNTIME_ROOT = "/storage/onprem"
DEFAULT_API_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_DATA_ROOT = str(Path.home() / "packetsafari-data")
DEPLOYMENT_PROFILES = {"onprem", "saas"}
BACKUP_MODES = {"inline", "require-recent", "skip"}
MIB = 1024 * 1024
GIB = 1024 * MIB
SIZING_PROFILES = {"auto", "small", "medium", "large", "none"}

DEFAULT_LOGGING_VALUES = {
    "AUDIT_LOG_ENABLED": "true",
    "AUDIT_LOG_PERSIST": "true",
    "AUDIT_RETENTION_DAYS": "365",
    "AUDIT_FORWARDING_MODE": "stdout_json",
    "AUDIT_FORWARDER_TYPE": "none",
}


@dataclass(slots=True)
class RuntimeLayout:
    runtime_root: Path
    container_runtime_root: Path

    @property
    def kind(self) -> str:
        if (self.runtime_root / "env" / "active" / "runtime").exists():
            return "local-data-root"
        return "onprem-runtime-root"

    @property
    def state_dir(self) -> Path:
        if self.kind == "local-data-root":
            return self.runtime_root / "state"
        return self.runtime_root / "state"

    @property
    def compose_dir(self) -> Path:
        if self.kind == "local-data-root":
            return self.runtime_root / "compose"
        return self.runtime_root / "compose"

    @property
    def env_dir(self) -> Path:
        if self.kind == "local-data-root":
            return self.runtime_root / "env" / "active" / "runtime"
        return self.runtime_root / "env"

    @property
    def secrets_dir(self) -> Path:
        return self.runtime_root / "secrets"

    @property
    def backup_dir(self) -> Path:
        return self.runtime_root / "backups"

    @property
    def tmp_dir(self) -> Path:
        return self.runtime_root / "tmp"

    @property
    def logging_dir(self) -> Path:
        return self.runtime_root / "logging" / "vector"

    @property
    def tooling_root(self) -> Path:
        return self.runtime_root / "tooling" / "onprem"

    @property
    def bin_dir(self) -> Path:
        return self.runtime_root / "bin"

    @property
    def runtime_env_path(self) -> Path:
        if self.kind == "local-data-root":
            return self.env_dir / ".env.production"
        return self.env_dir / "runtime.env"

    @property
    def deployment_state_path(self) -> Path:
        return self.state_dir / "deployment-state.json"

    @property
    def release_manifest_path(self) -> Path:
        return self.state_dir / "release-manifest.json"

    @property
    def target_release_manifest_path(self) -> Path:
        return self.state_dir / "target-release-manifest.json"

    @property
    def license_token_path(self) -> Path:
        return self.state_dir / "license-token.json"

    @property
    def license_public_key_path(self) -> Path:
        return self.state_dir / "license-public.pem"

    @property
    def release_public_key_path(self) -> Path:
        return self.state_dir / "release-public.pem"

    @property
    def upgrade_lock_path(self) -> Path:
        return self.state_dir / "upgrade.lock"

    @property
    def helper_status_path(self) -> Path:
        return self.state_dir / "helper-status.json"

    @property
    def compose_file(self) -> Path:
        return self.compose_dir / "docker-compose.onprem.yml"

    @property
    def compose_sizing_file(self) -> Path:
        return self.compose_dir / "docker-compose.sizing.yml"

    @property
    def runtime_sizing_env_path(self) -> Path:
        return self.env_dir / "runtime-sizing.env"

    @property
    def sizing_state_path(self) -> Path:
        return self.state_dir / "sizing.json"

    @property
    def wrapper_path(self) -> Path:
        return self.bin_dir / "packetsafari-ops"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bundle_root() -> Path:
    return Path(__file__).resolve().parents[1]


def version() -> str:
    return __version__


def runtime_layout(runtime_root: str = DEFAULT_RUNTIME_ROOT, container_runtime_root: str = DEFAULT_CONTAINER_RUNTIME_ROOT) -> RuntimeLayout:
    return RuntimeLayout(Path(runtime_root).expanduser(), Path(container_runtime_root).expanduser())


def detect_runtime_root(preferred: str | None = None) -> str:
    if preferred:
        return str(Path(preferred).expanduser())
    configured = str(os.getenv("PACKETSAFARI_ONPREM_RUNTIME_ROOT") or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    data_root = str(os.getenv("PACKETSAFARI_DATA_ROOT") or "").strip()
    if data_root:
        return str(Path(data_root).expanduser())
    home_data_root = Path(DEFAULT_DATA_ROOT).expanduser()
    if home_data_root.exists():
        return str(home_data_root)
    return DEFAULT_RUNTIME_ROOT


def app_repo_root() -> Path | None:
    configured = str(os.getenv("PACKETSAFARI_APP_ROOT") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if (path / "docker-compose-dev.yml").exists():
            return path
    sibling = bundle_root().parents[1] / "packetsafari"
    if (sibling / "docker-compose-dev.yml").exists():
        return sibling
    cwd = Path.cwd()
    if (cwd / "docker-compose-dev.yml").exists():
        return cwd
    return None


def _script_path(root: Path, relative: str) -> Path:
    return root / "scripts" / relative


def _run_script(root: Path, relative: str, args: list[str]) -> None:
    script = _script_path(root, relative)
    subprocess.run([sys.executable, str(script), *args], check=True)


def _read_json(path: Path, default: dict | list | None = None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_runtime_dirs(layout: RuntimeLayout) -> None:
    for path in (
        layout.runtime_root,
        layout.state_dir,
        layout.compose_dir,
        layout.env_dir,
        layout.secrets_dir,
        layout.backup_dir,
        layout.tmp_dir,
        layout.logging_dir,
        layout.tooling_root,
        layout.bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def supports_onprem_host_actions(layout: RuntimeLayout) -> bool:
    return layout.kind == "onprem-runtime-root"


def deployment_profile(args) -> str:
    profile = str(getattr(args, "profile", "onprem") or "onprem").strip().lower()
    if profile not in DEPLOYMENT_PROFILES:
        raise RuntimeError(f"Unsupported deployment profile: {profile}")
    return profile


def supports_upgrade_host_actions(layout: RuntimeLayout, *, profile: str) -> bool:
    if profile == "onprem":
        return supports_onprem_host_actions(layout)
    return layout.kind == "onprem-runtime-root"


def resolve_backup_mode(args, *, profile: str) -> str:
    raw = str(getattr(args, "backup_mode", "") or "").strip().lower()
    mode = raw or ("inline" if profile == "onprem" else "require-recent")
    if mode not in BACKUP_MODES:
        raise RuntimeError(f"Unsupported backup mode: {mode}")
    if profile == "onprem" and mode == "skip":
        raise RuntimeError("onprem upgrades do not support --backup-mode skip.")
    if mode == "skip" and not _truthy(os.getenv("PACKETSAFARI_ALLOW_UNBACKED_UPGRADE")):
        raise RuntimeError("Unbacked upgrades are disabled. Set PACKETSAFARI_ALLOW_UNBACKED_UPGRADE=true only for disposable development hosts.")
    return mode


def _parse_timestamp(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def verify_external_backup_proof(layout: RuntimeLayout, args, *, max_age_minutes: int) -> dict[str, object]:
    configured = str(getattr(args, "backup_proof", "") or os.getenv("PACKETSAFARI_BACKUP_PROOF") or "").strip()
    proof_path = Path(configured).expanduser() if configured else layout.state_dir / "latest-backup.json"
    if not proof_path.exists():
        raise RuntimeError(
            f"External backup proof is required before this upgrade. Create {proof_path} after a verified snapshot, "
            "or pass --backup-mode inline to let packetsafari-ops create a local backup."
        )
    if proof_path.stat().st_size <= 0:
        raise RuntimeError(f"External backup proof is empty: {proof_path}")

    try:
        payload = json.loads(proof_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"External backup proof must be a JSON object: {proof_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"External backup proof must be a JSON object: {proof_path}")

    status = str(payload.get("status") or "").strip().lower()
    if status and status not in {"ok", "complete", "completed", "available", "success", "succeeded"}:
        raise RuntimeError(f"External backup proof does not show a completed backup: status={status!r}.")
    for key in ("verifiedRestore", "restoreVerified", "restorable"):
        if key in payload and not _truthy(payload.get(key)):
            raise RuntimeError(f"External backup proof marks {key}=false: {proof_path}")

    proof: dict[str, object] = {
        **payload,
        "path": str(proof_path),
        "verifiedAt": "",
        "ageSeconds": None,
        "maxAgeMinutes": max_age_minutes,
    }
    timestamp = None
    for key in ("verifiedAt", "completedAt", "createdAt", "timestamp"):
        timestamp = _parse_timestamp(str(payload.get(key) or ""))
        if timestamp is not None:
            proof["verifiedAt"] = timestamp.isoformat()
            break
    if timestamp is None:
        raise RuntimeError(f"External backup proof must include an ISO timestamp: verifiedAt, completedAt, createdAt, or timestamp.")

    age_seconds = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    proof["ageSeconds"] = age_seconds
    if age_seconds > max(1, max_age_minutes) * 60:
        raise RuntimeError(
            f"External backup proof is too old: {proof_path} is {int(age_seconds // 60)} minutes old; "
            f"limit is {max_age_minutes} minutes."
        )
    return proof


def _saas_operator_token_candidates(layout: RuntimeLayout, explicit: str | None = None) -> list[str]:
    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit).strip())
    env_token = str(os.getenv("PACKETSAFARI_SAAS_OPERATOR_TOKEN") or "").strip()
    if env_token:
        candidates.append(env_token)
    token_path = layout.secrets_dir / "saas-operator-token"
    if token_path.exists():
        candidates.append(token_path.read_text(encoding="utf-8").strip())
    return [candidate for candidate in candidates if candidate]


def _manifest_saas_token_hash(manifest: dict) -> str:
    profiles = manifest.get("deploymentProfiles")
    if isinstance(profiles, dict):
        saas = profiles.get("saas")
        if isinstance(saas, dict):
            value = str(saas.get("operatorTokenSha256") or "").strip().lower()
            if value:
                return value
    return str(os.getenv("PACKETSAFARI_SAAS_OPERATOR_TOKEN_SHA256") or "").strip().lower()


def verify_saas_operator_authorization(layout: RuntimeLayout, args, manifest: dict) -> None:
    expected_hash = _manifest_saas_token_hash(manifest)
    if not expected_hash:
        raise RuntimeError(
            "SaaS profile requires an internal operator token hash. Set deploymentProfiles.saas.operatorTokenSha256 "
            "in the manifest or PACKETSAFARI_SAAS_OPERATOR_TOKEN_SHA256 on the host."
        )
    for token in _saas_operator_token_candidates(layout, getattr(args, "saas_operator_token", None)):
        if hashlib.sha256(token.encode("utf-8")).hexdigest().lower() == expected_hash:
            return
    raise RuntimeError(
        "SaaS profile is not authorized on this host. Install /opt/packetsafari/secrets/saas-operator-token "
        "or set PACKETSAFARI_SAAS_OPERATOR_TOKEN for PacketSafari-operated SaaS hosts."
    )


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_url(source: str) -> bool:
    parsed = urllib.parse.urlparse(str(source or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _split_env_headers(value: str) -> list[str]:
    headers: list[str] = []
    for raw_line in value.replace(";;", "\n").splitlines():
        header = raw_line.strip()
        if header:
            headers.append(header)
    return headers


def _download_headers(args=None) -> dict[str, str]:
    headers: dict[str, str] = {"User-Agent": f"packetsafari-ops/{version()}"}
    bearer = str(getattr(args, "download_bearer_token", "") or os.getenv("PACKETSAFARI_DOWNLOAD_BEARER_TOKEN") or "").strip()
    basic = str(getattr(args, "download_basic", "") or os.getenv("PACKETSAFARI_DOWNLOAD_BASIC_AUTH") or "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode("utf-8")).decode("ascii")

    raw_headers: list[str] = []
    raw_headers.extend(getattr(args, "download_header", []) or [])
    raw_headers.extend(_split_env_headers(os.getenv("PACKETSAFARI_DOWNLOAD_HEADER") or ""))
    for raw_header in raw_headers:
        if ":" not in raw_header:
            raise RuntimeError(f"Invalid download header {raw_header!r}; expected 'Name: value'.")
        name, value = raw_header.split(":", 1)
        name = name.strip()
        if not name:
            raise RuntimeError(f"Invalid download header {raw_header!r}; header name is empty.")
        headers[name] = value.strip()
    return headers


def _download_timeout(args=None) -> int:
    raw = getattr(args, "download_timeout", None) or os.getenv("PACKETSAFARI_DOWNLOAD_TIMEOUT") or 300
    return max(1, int(raw))


def _download_context(args=None):
    if _truthy(getattr(args, "allow_insecure_download", False)) or _truthy(os.getenv("PACKETSAFARI_ALLOW_INSECURE_DOWNLOAD")):
        return ssl._create_unverified_context()
    return None


def _safe_download_name(source: str, default_name: str) -> str:
    parsed = urllib.parse.urlparse(source)
    name = Path(parsed.path).name or default_name
    return re.sub(r"[^0-9A-Za-z_.-]+", "-", name)


def materialize_source(source: str | os.PathLike[str], destination_dir: Path, label: str, args=None, *, default_name: str) -> Path:
    raw = str(source or "").strip()
    if not raw:
        raise RuntimeError(f"Missing {label} source.")
    if not _is_url(raw):
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{label.capitalize()} not found: {path}")
        return path

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / _safe_download_name(raw, default_name)
    partial = destination.with_name(f"{destination.name}.download")
    request = urllib.request.Request(raw, headers=_download_headers(args))
    context = _download_context(args)
    try:
        if context is not None and urllib.parse.urlparse(raw).scheme == "https":
            response = urllib.request.urlopen(request, timeout=_download_timeout(args), context=context)
        else:
            response = urllib.request.urlopen(request, timeout=_download_timeout(args))
        with response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        partial.replace(destination)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to download {label} from {raw}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to download {label} from {raw}: {exc.reason}") from exc
    finally:
        partial.unlink(missing_ok=True)
    return destination


def _materialize_license_public_key(layout: RuntimeLayout, args, work_dir: Path, *, bundle_dir: Path | None = None) -> Path:
    explicit = str(getattr(args, "license_public_key", "") or "").strip()
    if explicit:
        return materialize_source(explicit, work_dir, "license public key", args, default_name="license-public.pem")
    bundled = bundle_dir / "license-public.pem" if bundle_dir is not None else None
    if bundled is not None and bundled.exists() and bool(getattr(args, "allow_bundled_license_public_key", False)):
        return bundled
    default = bundle_root() / "keys" / "license-public.pem"
    if default.exists():
        return default
    raise RuntimeError("No PacketSafari license public key found. Pass --license-public-key.")


def _read_host_memory_bytes() -> int:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for raw_line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not raw_line.startswith("MemTotal:"):
                continue
            parts = raw_line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024
                except Exception:
                    break
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 16 * GIB


def _runtime_disk_usage(layout: RuntimeLayout) -> dict[str, int | str]:
    path = layout.runtime_root if layout.runtime_root.exists() else layout.runtime_root.parent
    if not path.exists():
        path = Path("/")
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "totalBytes": int(usage.total),
        "usedBytes": int(usage.used),
        "freeBytes": int(usage.free),
    }


def _host_resource_snapshot(layout: RuntimeLayout) -> dict[str, object]:
    return {
        "vcpus": max(1, int(os.cpu_count() or 1)),
        "memoryBytes": int(_read_host_memory_bytes()),
        "disk": _runtime_disk_usage(layout),
    }


def _auto_sizing_profile(host: dict[str, object]) -> str:
    vcpus = int(host.get("vcpus") or 1)
    memory_gib = float(int(host.get("memoryBytes") or 0)) / float(GIB)
    if vcpus >= 16 and memory_gib >= 64:
        return "large"
    if vcpus >= 8 and memory_gib >= 32:
        return "medium"
    return "small"


def _round_cpu(value: float) -> float:
    return round(max(0.25, float(value)) * 4.0) / 4.0


def _compose_memory(value: int) -> str:
    mib = max(128, int(round(float(value) / float(MIB))))
    if mib >= 1024 and mib % 1024 == 0:
        return f"{mib // 1024}g"
    return f"{mib}m"


def _service_cpu_plan(vcpus: int, profile: str) -> dict[str, float]:
    if profile == "large":
        frontend = 0.75
        redis = 1.0
        postgres = 2.0
        backend = min(3.0, max(1.5, vcpus * 0.15))
        sharkd = min(4.0, max(2.0, vcpus * 0.20))
        worker = min(12.0, max(2.0, vcpus * 0.45))
    elif profile == "medium":
        frontend = 0.5
        redis = 0.75
        postgres = 1.5
        backend = min(2.0, max(1.0, vcpus * 0.15))
        sharkd = min(2.0, max(1.25, vcpus * 0.18))
        worker = min(6.0, max(1.5, vcpus * 0.40))
    else:
        frontend = 0.5
        redis = 0.5
        postgres = 1.0
        backend = min(1.25, max(0.75, vcpus * 0.18))
        sharkd = min(1.5, max(1.0, vcpus * 0.20))
        worker = min(2.0, max(1.0, vcpus * 0.25))

    return {
        "frontend": _round_cpu(frontend),
        "backend": _round_cpu(backend),
        "worker": _round_cpu(worker),
        "redis": _round_cpu(redis),
        "postgres": _round_cpu(postgres),
        "sharkd": _round_cpu(sharkd),
        "audit-forwarder": 0.25,
    }


def _scale_memory_plan(memory_bytes: int, raw: dict[str, int]) -> dict[str, int]:
    reserve = min(max(1 * GIB, int(memory_bytes * 0.12)), 4 * GIB)
    target = max(2 * GIB, memory_bytes - reserve)
    total = sum(raw.values())
    if total <= target:
        return raw
    scale = float(target) / float(total)
    floors = {
        "frontend": 256 * MIB,
        "backend": 768 * MIB,
        "worker": 1536 * MIB,
        "redis": 256 * MIB,
        "postgres": 1024 * MIB,
        "sharkd": 1024 * MIB,
        "audit-forwarder": 128 * MIB,
    }
    return {
        service: max(int(floors.get(service, 256 * MIB)), int(value * scale))
        for service, value in raw.items()
    }


def _service_memory_plan(memory_bytes: int, profile: str) -> dict[str, int]:
    memory_bytes = max(4 * GIB, int(memory_bytes or 16 * GIB))
    max_by_profile = {
        "small": {
            "frontend": 1 * GIB,
            "backend": 3 * GIB,
            "worker": 6 * GIB,
            "redis": 1536 * MIB,
            "postgres": 3 * GIB,
            "sharkd": 4 * GIB,
            "audit-forwarder": 512 * MIB,
        },
        "medium": {
            "frontend": 1536 * MIB,
            "backend": 6 * GIB,
            "worker": 16 * GIB,
            "redis": 3 * GIB,
            "postgres": 6 * GIB,
            "sharkd": 10 * GIB,
            "audit-forwarder": 768 * MIB,
        },
        "large": {
            "frontend": 2 * GIB,
            "backend": 8 * GIB,
            "worker": 32 * GIB,
            "redis": 6 * GIB,
            "postgres": 10 * GIB,
            "sharkd": 20 * GIB,
            "audit-forwarder": 1 * GIB,
        },
    }[profile]
    raw = {
        "frontend": min(max_by_profile["frontend"], max(512 * MIB, int(memory_bytes * 0.03))),
        "backend": min(max_by_profile["backend"], max(1 * GIB, int(memory_bytes * 0.12))),
        "worker": min(max_by_profile["worker"], max(3 * GIB, int(memory_bytes * 0.30))),
        "redis": min(max_by_profile["redis"], max(512 * MIB, int(memory_bytes * 0.06))),
        "postgres": min(max_by_profile["postgres"], max(2 * GIB, int(memory_bytes * 0.12))),
        "sharkd": min(max_by_profile["sharkd"], max(2 * GIB, int(memory_bytes * 0.20))),
        "audit-forwarder": min(max_by_profile["audit-forwarder"], max(256 * MIB, int(memory_bytes * 0.02))),
    }
    return _scale_memory_plan(memory_bytes, raw)


def _build_sizing_plan(layout: RuntimeLayout, requested_profile: str) -> dict[str, object]:
    host = _host_resource_snapshot(layout)
    effective_profile = _auto_sizing_profile(host) if requested_profile == "auto" else requested_profile
    vcpus = int(host.get("vcpus") or 1)
    memory_bytes = int(host.get("memoryBytes") or 16 * GIB)
    cpu_plan = _service_cpu_plan(vcpus, effective_profile)
    memory_plan = _service_memory_plan(memory_bytes, effective_profile)
    services = {
        service: {
            "cpus": _round_cpu(cpu_plan[service]),
            "memoryBytes": int(memory_plan[service]),
            "memLimit": _compose_memory(memory_plan[service]),
        }
        for service in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
    }

    worker_cpu_count = max(1, int(float(services["worker"]["cpus"])))
    index_concurrency = min(
        {
            "small": 1,
            "medium": 6,
            "large": 10,
        }[effective_profile],
        worker_cpu_count,
    )
    aichat_concurrency = {"small": 2, "medium": 2, "large": 3}[effective_profile]
    uwsgi_processes = {
        "small": max(2, min(4, vcpus // 2 or 1)),
        "medium": max(4, min(8, vcpus // 2)),
        "large": max(6, min(12, vcpus // 2)),
    }[effective_profile]
    sharkd_lru_size = {"small": 4, "medium": 10, "large": 16}[effective_profile]
    rule_shard_workers = {"small": 1, "medium": 2, "large": 4}[effective_profile]
    backend_mem_mib = max(768, int(int(services["backend"]["memoryBytes"]) / MIB))
    reload_on_rss = max(512, min(2048, int((backend_mem_mib * 0.70) / max(1, uwsgi_processes))))
    redis_max_bytes = max(128 * MIB, int(int(services["redis"]["memoryBytes"]) * 0.75))
    postgres_mem_bytes = int(services["postgres"]["memoryBytes"])
    postgres_shared_buffers = max(256 * MIB, min(8 * GIB, int(postgres_mem_bytes * 0.25)))
    postgres_effective_cache_size = max(512 * MIB, int(postgres_mem_bytes * 0.65))
    postgres_work_mem = max(4 * MIB, min(64 * MIB, int(postgres_mem_bytes * 0.01)))
    postgres_maintenance_work_mem = max(64 * MIB, min(1 * GIB, int(postgres_mem_bytes * 0.08)))

    env = {
        "PACKETSAFARI_SIZING_PROFILE": effective_profile,
        "PACKETSAFARI_SIZING_REQUESTED_PROFILE": requested_profile,
        "PACKETSAFARI_SIZING_HOST_VCPUS": str(vcpus),
        "PACKETSAFARI_SIZING_HOST_MEMORY_BYTES": str(memory_bytes),
        "CELERY_AICHAT_CONCURRENCY": str(aichat_concurrency),
        "CELERY_INDEX_CONCURRENCY": str(index_concurrency),
        "CELERY_AICHAT_LOGLEVEL": "info",
        "CELERY_INDEX_LOGLEVEL": "info",
        "UWSGI_PROCESSES": str(uwsgi_processes),
        "UWSGI_THREADS": "2",
        "UWSGI_RELOAD_ON_RSS_MB": str(reload_on_rss),
        "PACKETSAFARI_CAPTURE_SHARKD_LRU_SIZE": str(sharkd_lru_size),
        "PACKETSAFARI_SHARKD_PACKETSTATS_RULE_SHARD_WORKERS": str(rule_shard_workers),
        "HEAVY_STAGE_MIN_AVAILABLE_MIB": str({"small": 768, "medium": 1024, "large": 1536}[effective_profile]),
        "HEAVY_STAGE_MEMORY_SOFT_LIMIT_PERCENT": str({"small": 78.0, "medium": 82.0, "large": 84.0}[effective_profile]),
        "HEAVY_STAGE_MEMORY_HARD_LIMIT_PERCENT": "90.0",
        "PACKETSAFARI_REDIS_MAXMEMORY": _compose_memory(redis_max_bytes),
        "POSTGRES_SHARED_BUFFERS": _compose_memory(postgres_shared_buffers),
        "POSTGRES_EFFECTIVE_CACHE_SIZE": _compose_memory(postgres_effective_cache_size),
        "POSTGRES_WORK_MEM": _compose_memory(postgres_work_mem),
        "POSTGRES_MAINTENANCE_WORK_MEM": _compose_memory(postgres_maintenance_work_mem),
    }

    return {
        "schemaVersion": 1,
        "requestedProfile": requested_profile,
        "effectiveProfile": effective_profile,
        "host": host,
        "services": services,
        "env": env,
        "generatedAt": utc_now(),
        "files": {
            "state": str(layout.sizing_state_path),
            "env": str(layout.runtime_sizing_env_path),
            "compose": str(layout.compose_sizing_file),
        },
    }


def _render_sizing_compose(layout: RuntimeLayout, plan: dict[str, object]) -> str:
    services = plan.get("services") if isinstance(plan.get("services"), dict) else {}
    env = plan.get("env") if isinstance(plan.get("env"), dict) else {}

    def service_value(name: str, key: str) -> str:
        service = services.get(name) if isinstance(services.get(name), dict) else {}
        return str(service.get(key) or "")

    def service_block(name: str, *, env_file: bool = False, extra: list[str] | None = None) -> list[str]:
        lines = [
            f"  {name}:",
            f"    cpus: {service_value(name, 'cpus')}",
            f"    mem_limit: {service_value(name, 'memLimit')}",
        ]
        if env_file:
            lines.extend(
                [
                    "    env_file:",
                    f"      - {json.dumps(str(layout.runtime_env_path))}",
                    f"      - {json.dumps(str(layout.runtime_sizing_env_path))}",
                ]
            )
        lines.extend(extra or [])
        return lines

    worker_command = [
        "    environment:",
        "      - PYTHONPATH=/app",
        "    command:",
        "      - /bin/bash",
        "      - -lc",
        "      - |",
        "        set -euo pipefail",
        "        shutdown() {",
        "          kill -TERM \"$${AICHAT_PID:-}\" \"$${INDEX_PID:-}\" 2>/dev/null || true",
        "        }",
        "        trap shutdown TERM INT",
        "",
        "        celery -A packetsafari.celery_app worker \\",
        "          --loglevel=\"$${CELERY_AICHAT_LOGLEVEL:-info}\" \\",
        "          --without-gossip --without-mingle \\",
        "          --concurrency=\"$${CELERY_AICHAT_CONCURRENCY:-2}\" \\",
        "          --queues=aichat \\",
        "          --hostname=aichat@%h &",
        "        AICHAT_PID=$$!",
        "",
        "        celery -A packetsafari.celery_app worker \\",
        "          --loglevel=\"$${CELERY_INDEX_LOGLEVEL:-info}\" \\",
        "          --pool=threads \\",
        "          --without-gossip --without-mingle \\",
        "          --concurrency=\"$${CELERY_INDEX_CONCURRENCY:-1}\" \\",
        "          --queues=index \\",
        "          --hostname=index@%h &",
        "        INDEX_PID=$$!",
        "",
        "        wait -n \"$${AICHAT_PID}\" \"$${INDEX_PID}\"",
        "        EXIT_CODE=$$?",
        "        shutdown",
        "        wait || true",
        "        exit \"$${EXIT_CODE}\"",
    ]
    postgres_command = [
        "    command:",
        "      - postgres",
        "      - -c",
        "      - checkpoint_timeout=15min",
        "      - -c",
        "      - checkpoint_completion_target=0.95",
        "      - -c",
        "      - max_wal_size=4GB",
        "      - -c",
        "      - min_wal_size=1GB",
        "      - -c",
        "      - wal_compression=on",
        "      - -c",
        f"      - shared_buffers={env.get('POSTGRES_SHARED_BUFFERS') or '512m'}",
        "      - -c",
        f"      - effective_cache_size={env.get('POSTGRES_EFFECTIVE_CACHE_SIZE') or '2g'}",
        "      - -c",
        f"      - work_mem={env.get('POSTGRES_WORK_MEM') or '16m'}",
        "      - -c",
        f"      - maintenance_work_mem={env.get('POSTGRES_MAINTENANCE_WORK_MEM') or '256m'}",
        "      - -c",
        "      - bgwriter_delay=50ms",
        "      - -c",
        "      - bgwriter_lru_maxpages=200",
    ]
    redis_command = [
        "    command: >-",
        "      sh -ec 'REDIS_PASSWORD=\"$${REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD:-}}}\"; test -n \"$$REDIS_PASSWORD\"; exec /opt/redis-stack/bin/redis-server --dir /data --save 20 1 --loglevel warning --protected-mode no --requirepass \"$$REDIS_PASSWORD\" --maxmemory \"$${PACKETSAFARI_REDIS_MAXMEMORY:-1g}\" --maxmemory-policy allkeys-lru --loadmodule /opt/redis-stack/lib/rediscompat.so --loadmodule /opt/redis-stack/lib/redisearch.so MAXSEARCHRESULTS 10000 MAXAGGREGATERESULTS 10000 --loadmodule /opt/redis-stack/lib/rejson.so'",
    ]

    lines = [
        "# Generated by packetsafari-ops tune. Do not edit by hand.",
        "services:",
        *service_block("frontend"),
        *service_block("backend", env_file=True),
        *service_block("worker", env_file=True, extra=worker_command),
        *service_block("postgres", env_file=True, extra=postgres_command),
        *service_block("redis", env_file=True, extra=redis_command),
        *service_block("sharkd", env_file=True),
        *service_block("audit-forwarder"),
    ]
    return "\n".join(lines) + "\n"


def write_sizing_profile(layout: RuntimeLayout, *, profile: str) -> dict[str, object]:
    normalized = str(profile or "auto").strip().lower()
    if normalized not in SIZING_PROFILES:
        raise RuntimeError(f"Unsupported sizing profile: {profile}")
    ensure_runtime_dirs(layout)
    if normalized == "none":
        for path in (layout.sizing_state_path, layout.runtime_sizing_env_path, layout.compose_sizing_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return {
            "schemaVersion": 1,
            "requestedProfile": "none",
            "effectiveProfile": "none",
            "generatedAt": utc_now(),
            "filesRemoved": [
                str(layout.sizing_state_path),
                str(layout.runtime_sizing_env_path),
                str(layout.compose_sizing_file),
            ],
        }

    plan = _build_sizing_plan(layout, normalized)
    env = plan.get("env") if isinstance(plan.get("env"), dict) else {}
    write_env_file(
        layout.runtime_sizing_env_path,
        {str(key): str(value) for key, value in env.items()},
        header_lines=[
            "# Managed by PacketSafari on-prem Python operations.",
            "# Generated by packetsafari-ops tune. Do not edit by hand.",
        ],
    )
    layout.compose_sizing_file.write_text(_render_sizing_compose(layout, plan), encoding="utf-8")
    _write_json(layout.sizing_state_path, plan)
    return plan


def resolve_logging_values(args) -> dict[str, str]:
    values = dict(DEFAULT_LOGGING_VALUES)
    def _bool(raw: object, *, default: str) -> str:
        if raw is None or raw == "":
            return default
        return "true" if str(raw).strip().lower() in {"1", "true", "yes", "y", "on"} else "false"

    if getattr(args, "non_interactive", False):
        values["AUDIT_LOG_ENABLED"] = _bool(getattr(args, "audit_log_enabled", None), default=values["AUDIT_LOG_ENABLED"])
        values["AUDIT_LOG_PERSIST"] = _bool(getattr(args, "audit_log_persist", None), default=values["AUDIT_LOG_PERSIST"])
        if getattr(args, "audit_retention_days", None):
            values["AUDIT_RETENTION_DAYS"] = str(max(1, int(args.audit_retention_days)))
        if getattr(args, "audit_forwarding_mode", None):
            values["AUDIT_FORWARDING_MODE"] = str(args.audit_forwarding_mode).strip().lower()
        if getattr(args, "audit_forwarder_type", None):
            values["AUDIT_FORWARDER_TYPE"] = str(args.audit_forwarder_type).strip().lower()
        if values["AUDIT_FORWARDING_MODE"] != "forwarder_profile":
            values["AUDIT_FORWARDER_TYPE"] = "none"
        return values

    print("PacketSafari audit logging configuration")
    values["AUDIT_LOG_ENABLED"] = _prompt_bool("Enable audit logging", values["AUDIT_LOG_ENABLED"])
    values["AUDIT_LOG_PERSIST"] = _prompt_bool("Persist audit events in SQL", values["AUDIT_LOG_PERSIST"])
    values["AUDIT_RETENTION_DAYS"] = _prompt_int("Audit retention in days", values["AUDIT_RETENTION_DAYS"])
    values["AUDIT_FORWARDING_MODE"] = _prompt_choice(
        "Forwarding mode",
        ["stdout_json", "forwarder_profile", "custom_driver"],
        values["AUDIT_FORWARDING_MODE"],
    )
    if values["AUDIT_FORWARDING_MODE"] == "forwarder_profile":
        values["AUDIT_FORWARDER_TYPE"] = _prompt_choice(
            "Forwarder type",
            ["splunk", "elasticsearch", "opensearch", "loki", "syslog"],
            "splunk",
        )
    else:
        values["AUDIT_FORWARDER_TYPE"] = "none"
    return values


def _prompt_bool(label: str, default: str) -> str:
    suffix = "Y/n" if default == "true" else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return "true"
        if raw in {"n", "no"}:
            return "false"


def _prompt_choice(label: str, options: list[str], default: str) -> str:
    prompt = "/".join(options)
    while True:
        raw = input(f"{label} [{default}] ({prompt}): ").strip().lower()
        if not raw:
            return default
        if raw in options:
            return raw


def _prompt_int(label: str, default: str) -> str:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return str(max(1, int(raw)))
        except Exception:
            continue


def sync_bundle(layout: RuntimeLayout) -> None:
    source = bundle_root()
    destination = layout.tooling_root
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        if child.name in {"install.sh", "upgrade.sh", "rollback.sh", "install-helper.sh", ".venv", "build", "packetsafari_onprem.egg-info"}:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(
                child,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
        else:
            shutil.copy2(child, target)
    install_wrapper(layout)


def install_wrapper(layout: RuntimeLayout) -> None:
    cli_path = layout.tooling_root / "packetsafari_onprem" / "cli.py"
    wrapper = f"""#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH={str(layout.tooling_root)!r}${{PYTHONPATH:+:${{PYTHONPATH}}}}
exec python3 {str(cli_path)!r} "$@"
"""
    layout.wrapper_path.write_text(wrapper, encoding="utf-8")
    layout.wrapper_path.chmod(0o755)


def write_helper_status(layout: RuntimeLayout, *, status: str = "ok", message: str = "ready") -> None:
    _write_json(
        layout.helper_status_path,
        {
            "service": "packetsafari-ops",
            "installed": True,
            "commandPath": str(layout.wrapper_path),
            "status": status,
            "message": message,
            "updatedAt": utc_now(),
        },
    )


def render_compose(layout: RuntimeLayout, manifest_path: Path, *, source_root: Path | None = None) -> None:
    root = source_root or layout.tooling_root
    _run_script(
        root,
        "render_compose.py",
        [
            "--manifest",
            str(manifest_path),
            "--template",
            str(root / "templates" / "docker-compose.onprem.yml.tpl"),
            "--runtime-env-path",
            str(layout.runtime_env_path),
            "--host-runtime-root",
            str(layout.runtime_root),
            "--container-runtime-root",
            str(layout.container_runtime_root),
            "--output",
            str(layout.compose_file),
        ],
    )


def render_logging_config(layout: RuntimeLayout, *, source_root: Path | None = None) -> None:
    root = source_root or layout.tooling_root
    _run_script(
        root,
        "render_logging_config.py",
        [
            "--runtime-env",
            str(layout.runtime_env_path),
            "--output",
            str(layout.logging_dir / "vector.toml"),
        ],
    )


def verify_license(token_path: Path, public_key_path: Path, *, source_root: Path | None = None) -> None:
    root = source_root or bundle_root()
    _run_script(
        root,
        "license_verify.py",
        [
            "--token",
            str(token_path),
            "--public-key",
            str(public_key_path),
        ],
    )


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _license_payload(token_path: Path) -> dict:
    token = _read_json(token_path, {})
    payload = token.get("payload")
    if not payload:
        return {}
    return json.loads(_b64url_decode(str(payload)).decode("utf-8"))


def verify_license_allows_release(layout: RuntimeLayout, manifest: dict) -> None:
    verify_license(layout.license_token_path, layout.license_public_key_path)
    payload = _license_payload(layout.license_token_path)
    license_channel = str(payload.get("channel") or "").strip()
    manifest_channel = str(manifest.get("channel") or "").strip()
    if license_channel and manifest_channel and license_channel != manifest_channel:
        raise RuntimeError(f"License channel {license_channel!r} does not allow release channel {manifest_channel!r}.")
    allowed_versions = [str(item).strip() for item in payload.get("allowed_versions", []) if str(item).strip()]
    target_version = str(manifest.get("version") or "").strip()
    if allowed_versions and target_version not in allowed_versions:
        raise RuntimeError(f"License does not allow release version {target_version!r}.")


def _version_key(value: str) -> tuple:
    cleaned = str(value or "").strip().removeprefix("v")
    parts = re.split(r"([0-9]+|[A-Za-z]+)", cleaned)
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part or part in {".", "-"}:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        elif part.isalpha():
            key.append((1, part.lower()))
        else:
            key.append((2, part))
    return tuple(key)


def _current_release_version(layout: RuntimeLayout) -> str:
    state = _read_json(layout.deployment_state_path, {})
    state_version = str(((state.get("deployment") or {}).get("installedVersion") or "")).strip()
    return state_version or _release_version(layout.release_manifest_path)


def validate_upgrade_path(layout: RuntimeLayout, manifest: dict) -> None:
    current = _current_release_version(layout)
    target = str(manifest.get("version") or "").strip()
    if not target:
        raise RuntimeError("Target manifest is missing version.")
    if current and current == target:
        raise RuntimeError(f"Release {target} is already active.")
    minimum = str(manifest.get("minUpgradeableFrom") or "").strip()
    if minimum and current and _version_key(current) < _version_key(minimum):
        raise RuntimeError(f"Current release {current} is older than minimum supported upgrade source {minimum}.")
    allowed_from = [str(item).strip() for item in manifest.get("upgradeableFrom", []) if str(item).strip()]
    if allowed_from and current not in allowed_from:
        raise RuntimeError(f"Current release {current or 'unknown'} is not listed in target upgradeableFrom.")


def validate_required_env(layout: RuntimeLayout, manifest: dict) -> None:
    runtime_env = parse_env_file(layout.runtime_env_path)
    required = [str(item).strip() for item in (manifest.get("requiredEnv") or []) if str(item).strip()]
    missing = [key for key in required if not str(runtime_env.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"Target manifest requires missing runtime env keys: {', '.join(missing)}")


def _required_env_keys(manifest: dict) -> list[str]:
    return [str(item).strip() for item in (manifest.get("requiredEnv") or []) if str(item).strip()]


def _secret_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "API_KEY", "CREDENTIAL"))


def _generated_env_default(key: str) -> str:
    upper = key.upper()
    if upper in {
        "PACKETSAFARI_AUTH_JWT_SECRET_KEY",
        "PACKETSAFARI_CAPTURE_SHARKD_JWT_SECRET",
        "REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD",
    }:
        return secrets.token_urlsafe(48)
    return ""


def configure_required_env(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    manifest_arg = str(getattr(args, "manifest", "") or "").strip()
    if not manifest_arg:
        raise RuntimeError(f"config {getattr(args, 'action', '')} requires --manifest.")
    ensure_runtime_dirs(layout)
    manifest_path = materialize_source(
        manifest_arg,
        layout.tmp_dir / "downloads",
        "release manifest",
        args,
        default_name="release-manifest.json",
    )
    manifest = _read_json(manifest_path, {})
    required = _required_env_keys(manifest)
    env_path = Path(str(getattr(args, "output", "") or "")).expanduser() if getattr(args, "output", None) else layout.runtime_env_path
    existing = parse_env_file(env_path)
    missing = [key for key in required if not str(existing.get(key, "")).strip()]
    action = str(getattr(args, "action", "") or "")

    if action == "check-env":
        return {
            "profile": deployment_profile(args),
            "manifest": str(manifest_path),
            "envPath": str(env_path),
            "required": required,
            "missing": missing,
            "ok": not missing,
        }

    if action != "prompt-env":
        raise RuntimeError(f"Unsupported config action for env configuration: {action}")

    values = dict(existing)
    prompted: list[str] = []
    for key in missing:
        generated_default = _generated_env_default(key)
        if generated_default:
            prompt = f"{key} [press enter to generate]: "
        else:
            prompt = f"{key}: "
        while True:
            if _secret_env_key(key):
                value = getpass.getpass(prompt)
            else:
                value = input(prompt)
            if value.strip():
                values[key] = value.strip()
                prompted.append(key)
                break
            if generated_default:
                values[key] = generated_default
                prompted.append(key)
                break
            print(f"{key} is required.")

    write_env_file(
        env_path,
        values,
        header_lines=[
            "# Managed by PacketSafari ops.",
            "# Generated/updated by packetsafari-ops config prompt-env.",
        ],
    )
    return {
        "profile": deployment_profile(args),
        "manifest": str(manifest_path),
        "envPath": str(env_path),
        "required": required,
        "prompted": prompted,
        "missingBefore": missing,
        "missingAfter": [key for key in required if not str(values.get(key, "")).strip()],
        "ok": True,
    }


def _release_public_key_candidates(layout: RuntimeLayout, explicit: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured = str(os.getenv("PACKETSAFARI_RELEASE_PUBLIC_KEY") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            layout.release_public_key_path,
            layout.secrets_dir / "release-public.pem",
            bundle_root() / "keys" / "release-public.pem",
        ]
    )
    return candidates


def _resolve_release_public_key(layout: RuntimeLayout, explicit: str | None = None) -> Path | None:
    for candidate in _release_public_key_candidates(layout, explicit):
        if candidate.exists():
            return candidate
    return None


def verify_detached_signature(public_key: Path, payload_path: Path, signature_path: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature_path),
            str(payload_path),
        ],
        check=True,
    )


@contextmanager
def upgrade_lock(layout: RuntimeLayout):
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    with layout.upgrade_lock_path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another PacketSafari install, upgrade, or rollback is already running.") from exc
        handle.write(f"{os.getpid()} {utc_now()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _compose_logging_args(runtime_env_path: Path) -> list[str]:
    env_values = parse_env_file(runtime_env_path)
    if str(env_values.get("AUDIT_FORWARDING_MODE") or "").strip().lower() == "forwarder_profile":
        return ["--profile", "logging"]
    return []


def _compose_file_args(layout: RuntimeLayout) -> list[str]:
    args = ["-f", str(layout.compose_file)]
    if layout.compose_sizing_file.exists():
        args.extend(["-f", str(layout.compose_sizing_file)])
    return args


def _compose_base_command(layout: RuntimeLayout) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(layout.runtime_env_path),
        *_compose_file_args(layout),
        *_compose_logging_args(layout.runtime_env_path),
    ]


def docker_compose_up(layout: RuntimeLayout, *, services: list[str] | None = None, pull_policy: str | None = None) -> None:
    if layout.kind == "local-data-root":
        repo_root = app_repo_root()
        if repo_root is None:
            raise RuntimeError("Unable to locate the PacketSafari app repo for local dev compose operations.")
        command = [
            "docker",
            "compose",
            "--env-file",
            str(layout.runtime_env_path),
            "--profile",
            "production",
            "-f",
            str(repo_root / "docker-compose-dev.yml"),
            "up",
            "-d",
        ]
        if pull_policy:
            command.extend(["--pull", pull_policy])
        if services:
            command.extend(services)
        subprocess.run(command, check=True, cwd=repo_root)
        return
    command = [*_compose_base_command(layout), "up", "-d"]
    if pull_policy:
        command.extend(["--pull", pull_policy])
    if services:
        command.extend(services)
    subprocess.run(command, check=True)


def docker_compose_pull(layout: RuntimeLayout) -> None:
    if layout.kind == "local-data-root":
        return
    subprocess.run([*_compose_base_command(layout), "pull"], check=True)


def docker_compose_stop(layout: RuntimeLayout, *, services: list[str] | None = None, timeout: int = 120) -> None:
    if layout.kind == "local-data-root":
        repo_root = app_repo_root()
        if repo_root is None:
            raise RuntimeError("Unable to locate the PacketSafari app repo for local dev compose operations.")
        command = [
            "docker",
            "compose",
            "--env-file",
            str(layout.runtime_env_path),
            "--profile",
            "production",
            "-f",
            str(repo_root / "docker-compose-dev.yml"),
            "stop",
            "-t",
            str(timeout),
        ]
        if services:
            command.extend(services)
        subprocess.run(command, check=True, cwd=repo_root)
        return
    command = [*_compose_base_command(layout), "stop", "-t", str(timeout)]
    if services:
        command.extend(services)
    subprocess.run(command, check=True)


def docker_compose_run(
    layout: RuntimeLayout,
    service: str,
    args: list[str],
    *,
    extra_volumes: list[str] | None = None,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
) -> None:
    command = [
        *_compose_base_command(layout),
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
    ]
    for volume in extra_volumes or []:
        command.extend(["-v", volume])
    command.extend([service, *args])
    stdin = stdin_path.open("rb") if stdin_path else None
    stdout = stdout_path.open("wb") if stdout_path else None
    try:
        subprocess.run(command, check=True, stdin=stdin, stdout=stdout)
    finally:
        if stdin:
            stdin.close()
        if stdout:
            stdout.close()


def docker_compose_exec(
    layout: RuntimeLayout,
    service: str,
    args: list[str],
    *,
    stdout_path: Path | None = None,
    stdin_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    command = [*_compose_base_command(layout), "exec", "-T"]
    for key, value in (env or {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([service, *args])
    stdin = stdin_path.open("rb") if stdin_path else None
    stdout = stdout_path.open("wb") if stdout_path else None
    try:
        subprocess.run(command, check=True, stdin=stdin, stdout=stdout)
    finally:
        if stdin:
            stdin.close()
        if stdout:
            stdout.close()


def docker_compose_restart(layout: RuntimeLayout, *, services: list[str] | None = None) -> None:
    if layout.kind == "local-data-root":
        repo_root = app_repo_root()
        if repo_root is None:
            raise RuntimeError("Unable to locate the PacketSafari app repo for local dev compose operations.")
        command = [
            "docker",
            "compose",
            "--env-file",
            str(layout.runtime_env_path),
            "--profile",
            "production",
            "-f",
            str(repo_root / "docker-compose-dev.yml"),
            "restart",
        ]
        if services:
            command.extend(services)
        subprocess.run(command, check=True, cwd=repo_root)
        return
    command = [
        "docker",
        "compose",
        "--env-file",
        str(layout.runtime_env_path),
        *_compose_file_args(layout),
        *_compose_logging_args(layout.runtime_env_path),
        "restart",
    ]
    if services:
        command.extend(services)
    subprocess.run(command, check=True)


def docker_exec_backend(layout: RuntimeLayout, args: list[str]) -> None:
    subprocess.run(["docker", "exec", "-i", "packetsafari-backend", *args], check=True)


def write_runtime_env(layout: RuntimeLayout, logging_values: dict[str, str], *, onboarding_mode: bool) -> None:
    onboarding_value = quote_env_value("true" if onboarding_mode else "false")
    postgres_db = "packetsafari"
    postgres_user = "packetsafari"
    postgres_password = secrets.token_urlsafe(32)
    redis_password = secrets.token_urlsafe(32)
    jwt_secret = secrets.token_urlsafe(64)
    sharkd_secret = secrets.token_urlsafe(64)
    lines = [
        "# Managed by PacketSafari on-prem Python operations.",
        "# Finalize onboarding writes the managed runtime env, then the first admin is created manually from inside the backend container.",
        f'PACKETSAFARI_ONPREM_ENABLED="true"',
        f"PACKETSAFARI_ONPREM_ONBOARDING_ENABLED={onboarding_value}",
        f'PACKETSAFARI_ONPREM_RUNTIME_DIR={quote_env_value(str(layout.container_runtime_root))}',
        f'PACKETSAFARI_ONPREM_STATE_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "deployment-state.json"))}',
        f'PACKETSAFARI_ONPREM_ENV_PATH={quote_env_value(str(layout.container_runtime_root / "env" / "runtime.env"))}',
        f'PACKETSAFARI_ONPREM_SECRETS_DIR={quote_env_value(str(layout.container_runtime_root / "secrets"))}',
        f'PACKETSAFARI_ONPREM_RELEASE_MANIFEST_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "release-manifest.json"))}',
        f'PACKETSAFARI_ONPREM_TARGET_RELEASE_MANIFEST_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "target-release-manifest.json"))}',
        f'PACKETSAFARI_ONPREM_LICENSE_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "license-token.json"))}',
        f'PACKETSAFARI_ONPREM_LICENSE_PUBLIC_KEY_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "license-public.pem"))}',
        f'PACKETSAFARI_ONPREM_HELPER_STATUS_PATH={quote_env_value(str(layout.container_runtime_root / "state" / "helper-status.json"))}',
        'PACKETSAFARI_ONPREM_RENEWAL_CONTACT="contact@packetsafari.com"',
        f"POSTGRES_DB={quote_env_value(postgres_db)}",
        f"POSTGRES_USER={quote_env_value(postgres_user)}",
        f"POSTGRES_PASSWORD={quote_env_value(postgres_password)}",
        'PACKETSAFARI_RUNTIME_POSTGRES_ENABLED="true"',
        f"PACKETSAFARI_RUNTIME_POSTGRES_URL={quote_env_value(f'postgresql+psycopg2://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}')}",
        f"REDIS_PASSWORD={quote_env_value(redis_password)}",
        f"PACKETSAFARI_RUNTIME_REDIS_PASSWORD={quote_env_value(redis_password)}",
        f"PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD={quote_env_value(redis_password)}",
        f"PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD={quote_env_value(redis_password)}",
        f"PACKETSAFARI_AUTH_JWT_SECRET_KEY={quote_env_value(jwt_secret)}",
        f"PACKETSAFARI_CAPTURE_SHARKD_JWT_SECRET={quote_env_value(sharkd_secret)}",
    ]
    for key, value in logging_values.items():
        lines.append(f"{key}={quote_env_value(value)}")
    lines.extend(
        [
            'PACKETSAFARI_FEATURE_SAAS_PAYWALL_ENABLED="false"',
            'PACKETSAFARI_FEATURE_COGNITO_ENABLED="false"',
        ]
    )
    layout.runtime_env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _release_version(manifest_path: Path) -> str:
    manifest = _read_json(manifest_path, {})
    return str((manifest or {}).get("version") or "")


def write_deployment_state(layout: RuntimeLayout, *, mode: str, action_type: str, action_status: str, action_message: str) -> None:
    payload = {
        "schemaVersion": 1,
        "deployment": {
            "mode": mode,
            "installedVersion": _release_version(layout.release_manifest_path) if layout.release_manifest_path.exists() else "",
            "installedBuild": "",
            "installedAt": "",
        },
        "onboarding": {
            "completed": mode == "normal",
            "completedAt": utc_now() if mode == "normal" else "",
            "draftConfig": {},
        },
        "lastAction": {
            "type": action_type,
            "status": action_status,
            "message": action_message,
            "updatedAt": utc_now(),
        },
        "releaseManifest": {},
        "rollback": {},
    }
    _write_json(layout.deployment_state_path, payload)


def install_release(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        raise RuntimeError("Install is only supported for on-prem runtime roots like /opt/packetsafari, not local packetsafari-data mode.")
    ensure_runtime_dirs(layout)
    with upgrade_lock(layout):
        sync_bundle(layout)

        source = "bundle" if getattr(args, "bundle", None) else "manifest"
        if source == "bundle":
            prepare_offline_bundle(layout, args, manifest_destination=layout.release_manifest_path, install_license=True)
        else:
            if not str(getattr(args, "license", "") or "").strip():
                raise RuntimeError("install --manifest requires --license.")
            prepare_connected_manifest(layout, str(args.manifest), args, destination=layout.release_manifest_path)
            download_dir = layout.tmp_dir / "downloads"
            license_path = materialize_source(args.license, download_dir, "license token", args, default_name="license-token.json")
            public_key_path = _materialize_license_public_key(layout, args, download_dir)
            verify_license(license_path, public_key_path)
            shutil.copy2(license_path, layout.license_token_path)
            shutil.copy2(public_key_path, layout.license_public_key_path)

        release_public_key_path = bundle_root() / "keys" / "release-public.pem"
        if release_public_key_path.exists() and not layout.release_public_key_path.exists():
            shutil.copy2(release_public_key_path, layout.release_public_key_path)

        logging_values = resolve_logging_values(args)
        write_runtime_env(layout, logging_values, onboarding_mode=True)
        sizing = write_sizing_profile(layout, profile=str(getattr(args, "size", "auto") or "auto"))
        render_compose(layout, layout.release_manifest_path, source_root=bundle_root())
        render_logging_config(layout, source_root=bundle_root())
        write_deployment_state(
            layout,
            mode="onboarding",
            action_type="install",
            action_status="pending_restart",
            action_message="Python installer prepared the on-prem stack in onboarding mode.",
        )
        write_helper_status(layout, status="installing", message="Starting PacketSafari in onboarding mode.")
        docker_compose_up(layout, pull_policy="never" if source == "bundle" else None)
        write_helper_status(layout, status="ok", message="On-prem tooling installed.")
        return {
            "runtimeRoot": str(layout.runtime_root),
            "version": version(),
            "source": source,
            "sizing": {
                "profile": sizing.get("effectiveProfile"),
                "state": str(layout.sizing_state_path),
                "env": str(layout.runtime_sizing_env_path),
                "compose": str(layout.compose_sizing_file),
            },
            "message": "PacketSafari on-prem installed in onboarding mode. Finish setup in `packetsafari-ops tui` or open /onprem/onboarding in the local UI.",
        }


def status(layout: RuntimeLayout) -> dict:
    compose_files = [str(layout.compose_file)]
    if layout.compose_sizing_file.exists():
        compose_files.append(str(layout.compose_sizing_file))
    return {
        "layoutKind": layout.kind,
        "installerVersion": version(),
        "runtimeRoot": str(layout.runtime_root),
        "state": _read_json(layout.deployment_state_path, {}),
        "helper": _read_json(layout.helper_status_path, {}),
        "runtimeEnvPath": str(layout.runtime_env_path),
        "runtimeSizingEnvPath": str(layout.runtime_sizing_env_path),
        "composeFile": str(layout.compose_file),
        "composeSizingFile": str(layout.compose_sizing_file),
        "composeFiles": compose_files,
        "sizing": _read_json(layout.sizing_state_path, {}),
        "backups": [path.name for path in sorted(layout.backup_dir.glob("*"), reverse=True)[:10]],
    }


def _manifest_images(manifest: dict) -> dict:
    images = manifest.get("images") or {}
    return images if isinstance(images, dict) else {}


def image_ref(images: dict, key: str, default: str = "") -> str:
    value = images.get(key, default)
    if isinstance(value, dict):
        return str(value.get("image") or default)
    return str(value or default)


def snapshot_runtime(layout: RuntimeLayout) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot_dir = layout.backup_dir / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for src, prefix, suffix in (
        (layout.release_manifest_path, "release-manifest", ".json"),
        (layout.runtime_env_path, "runtime", ".env"),
        (layout.deployment_state_path, "deployment-state", ".json"),
        (layout.compose_file, "docker-compose", ".yml"),
        (layout.runtime_sizing_env_path, "runtime-sizing", ".env"),
        (layout.sizing_state_path, "sizing", ".json"),
        (layout.compose_sizing_file, "docker-compose-sizing", ".yml"),
    ):
        if src.exists():
            shutil.copy2(src, snapshot_dir / f"{prefix}{suffix}")
            shutil.copy2(src, layout.backup_dir / f"{prefix}-{stamp}{suffix}")
    _write_json(
        snapshot_dir / "snapshot.json",
        {
            "createdAt": utc_now(),
            "sourceRelease": _release_version(layout.release_manifest_path),
            "runtimeRoot": str(layout.runtime_root),
            "schemaVersion": 1,
        },
    )
    return snapshot_dir


def _postgres_env(layout: RuntimeLayout) -> dict[str, str]:
    env = parse_env_file(layout.runtime_env_path)
    return {
        "POSTGRES_DB": str(env.get("POSTGRES_DB") or "packetsafari"),
        "POSTGRES_USER": str(env.get("POSTGRES_USER") or "packetsafari"),
        "POSTGRES_PASSWORD": str(env.get("POSTGRES_PASSWORD") or env.get("PACKETSAFARI_POSTGRES_PASSWORD") or "packetsafari"),
    }


def backup_postgres(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    postgres = _postgres_env(layout)
    env = {"PGPASSWORD": postgres["POSTGRES_PASSWORD"]}
    docker_compose_exec(
        layout,
        "postgres",
        ["pg_dump", "-U", postgres["POSTGRES_USER"], "-d", postgres["POSTGRES_DB"], "-Fc"],
        stdout_path=snapshot_dir / "postgres.dump",
        env=env,
    )
    docker_compose_exec(
        layout,
        "postgres",
        ["pg_dumpall", "--globals-only", "-U", postgres["POSTGRES_USER"]],
        stdout_path=snapshot_dir / "postgres-globals.sql",
        env=env,
    )


def restore_postgres(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    dump_path = snapshot_dir / "postgres.dump"
    if not dump_path.exists():
        raise RuntimeError(f"Postgres backup missing from snapshot: {dump_path}")
    postgres = _postgres_env(layout)
    env = {"PGPASSWORD": postgres["POSTGRES_PASSWORD"]}
    docker_compose_up(layout, services=["postgres"], pull_policy="never")
    docker_compose_exec(
        layout,
        "postgres",
        ["dropdb", "--if-exists", "--force", "-U", postgres["POSTGRES_USER"], postgres["POSTGRES_DB"]],
        env=env,
    )
    docker_compose_exec(
        layout,
        "postgres",
        ["createdb", "-U", postgres["POSTGRES_USER"], postgres["POSTGRES_DB"]],
        env=env,
    )
    docker_compose_exec(
        layout,
        "postgres",
        ["pg_restore", "-U", postgres["POSTGRES_USER"], "-d", postgres["POSTGRES_DB"]],
        stdin_path=dump_path,
        env=env,
    )


def backup_storage(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    docker_compose_run(
        layout,
        "backend",
        ["sh", "-c", "tar -C /storage -cpf /backup/storage.tar ."],
        extra_volumes=[f"{snapshot_dir}:/backup"],
    )


def restore_storage(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    storage_path = snapshot_dir / "storage.tar"
    if not storage_path.exists():
        raise RuntimeError(f"Storage backup missing from snapshot: {storage_path}")
    docker_compose_run(
        layout,
        "backend",
        [
            "sh",
            "-c",
            "find /storage -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -C /storage -xpf /backup/storage.tar",
        ],
        extra_volumes=[f"{snapshot_dir}:/backup"],
    )


def create_full_backup(layout: RuntimeLayout) -> Path:
    snapshot_dir = snapshot_runtime(layout)
    complete_full_backup(layout, snapshot_dir)
    return snapshot_dir


def complete_full_backup(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    backup_postgres(layout, snapshot_dir)
    backup_storage(layout, snapshot_dir)
    _write_json(
        snapshot_dir / "snapshot.json",
        {
            **_read_json(snapshot_dir / "snapshot.json", {}),
            "postgresBackup": "postgres.dump",
            "storageBackup": "storage.tar",
            "completedAt": utc_now(),
        },
    )


def record_external_backup_proof(snapshot_dir: Path, proof: dict[str, object]) -> None:
    _write_json(
        snapshot_dir / "snapshot.json",
        {
            **_read_json(snapshot_dir / "snapshot.json", {}),
            "externalBackupProof": proof,
            "backupMode": "require-recent",
            "completedAt": utc_now(),
        },
    )


def _restore_metadata_snapshot(layout: RuntimeLayout, snapshot_dir: Path) -> None:
    mapping = {
        "release-manifest.json": layout.release_manifest_path,
        "runtime.env": layout.runtime_env_path,
        "deployment-state.json": layout.deployment_state_path,
        "docker-compose.yml": layout.compose_file,
        "runtime-sizing.env": layout.runtime_sizing_env_path,
        "sizing.json": layout.sizing_state_path,
        "docker-compose-sizing.yml": layout.compose_sizing_file,
    }
    for name, dest in mapping.items():
        src = snapshot_dir / name
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def restore_snapshot(layout: RuntimeLayout, snapshot_dir: Path, *, restore_data: bool) -> None:
    _restore_metadata_snapshot(layout, snapshot_dir)
    render_logging_config(layout)
    if restore_data:
        docker_compose_stop(layout, services=["frontend", "backend", "worker", "sharkd"], timeout=120)
        restore_postgres(layout, snapshot_dir)
        restore_storage(layout, snapshot_dir)
    docker_compose_up(layout, pull_policy="never")


def latest_snapshot_dir(layout: RuntimeLayout) -> Path | None:
    snapshots = [path for path in layout.backup_dir.iterdir() if path.is_dir() and (path / "snapshot.json").exists()]
    if not snapshots:
        return None
    return sorted(snapshots, reverse=True)[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksums(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            raise RuntimeError(f"Invalid checksum line: {raw}")
        digest, rel = parts[0], parts[-1].lstrip("*")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RuntimeError(f"Invalid sha256 digest in checksum line: {raw}")
        expected[rel] = digest.lower()
    return expected


def verify_bundle_checksums(bundle_dir: Path) -> None:
    checksums = bundle_dir / "checksums.txt"
    if not checksums.exists():
        raise RuntimeError("Offline bundle is missing checksums.txt.")
    expected = _parse_checksums(checksums)
    actual_files = {
        str(path.relative_to(bundle_dir))
        for path in bundle_dir.rglob("*")
        if path.is_file() and path.name not in {"checksums.txt", "checksums.txt.sig"}
    }
    missing = sorted(set(expected) - actual_files)
    unlisted = sorted(actual_files - set(expected))
    if missing:
        raise RuntimeError(f"Offline bundle checksum list references missing files: {', '.join(missing)}")
    if unlisted:
        raise RuntimeError(f"Offline bundle contains files missing from checksums.txt: {', '.join(unlisted)}")
    for rel, expected_digest in expected.items():
        actual = _sha256(bundle_dir / rel)
        if actual != expected_digest:
            raise RuntimeError(f"Checksum mismatch for {rel}: expected {expected_digest}, got {actual}")


def verify_bundle_signature(layout: RuntimeLayout, bundle_dir: Path, *, public_key: str | None, allow_unsigned: bool) -> None:
    checksums = bundle_dir / "checksums.txt"
    signature = bundle_dir / "checksums.txt.sig"
    if not signature.exists():
        if allow_unsigned:
            return
        raise RuntimeError("Offline bundle is missing checksums.txt.sig. Refusing unsigned bundle.")
    key = _resolve_release_public_key(layout, public_key)
    if key is None:
        if allow_unsigned:
            return
        candidates = ", ".join(str(path) for path in _release_public_key_candidates(layout, public_key))
        raise RuntimeError(f"No PacketSafari release public key found. Checked: {candidates}")
    verify_detached_signature(key, checksums, signature)


def _bundle_parts(path: Path) -> list[Path]:
    name = path.name
    if ".part-" not in name:
        return []
    prefix = name.split(".part-", 1)[0]
    return sorted(path.parent.glob(f"{prefix}.part-*"))


def _materialize_bundle(bundle_path: Path, work_dir: Path) -> Path:
    parts = _bundle_parts(bundle_path)
    if not parts:
        return bundle_path
    combined = work_dir / parts[0].name.split(".part-", 1)[0]
    with combined.open("wb") as output:
        for part in parts:
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, output, length=1024 * 1024)
    return combined


def _extract_bundle(archive: Path, work_dir: Path) -> Path:
    extract_dir = work_dir / "bundle"
    extract_dir.mkdir(parents=True, exist_ok=True)
    command = ["tar"]
    if archive.name.endswith(".zst"):
        command.append("--zstd")
    command.extend(["-xf", str(archive), "-C", str(extract_dir)])
    subprocess.run(command, check=True)
    if (extract_dir / "release-manifest.json").exists():
        return extract_dir
    children = [path for path in extract_dir.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "release-manifest.json").exists():
        return children[0]
    raise RuntimeError("Offline bundle did not extract to a directory containing release-manifest.json.")


def _image_archive_service(path: Path) -> str:
    name = path.name
    for suffix in (".tar.zst", ".tar.gz", ".tgz", ".tar.xz", ".tar"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _offline_image_ref(service: str, version_value: str) -> str:
    safe_version = re.sub(r"[^0-9A-Za-z_.-]+", "-", version_value.strip() or "release")
    return f"packetsafari-offline/{service}:{safe_version}"


def _inspect_image_id(ref: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _candidate_loaded_refs(output: str) -> list[str]:
    refs: list[str] = []
    for line in output.splitlines():
        if "Loaded image:" in line:
            refs.append(line.split("Loaded image:", 1)[1].strip())
        elif "Loaded image ID:" in line:
            refs.append(line.split("Loaded image ID:", 1)[1].strip())
    return refs


def _tag_loaded_image(service: str, archive: Path, source_ref: str, local_ref: str) -> None:
    result = subprocess.run(
        ["docker", "image", "load", "-i", str(archive)],
        check=True,
        capture_output=True,
        text=True,
    )
    candidates = [source_ref, *_candidate_loaded_refs(result.stdout + "\n" + result.stderr)]
    image_id = ""
    for candidate in [item for item in candidates if item]:
        try:
            image_id = _inspect_image_id(candidate)
            break
        except subprocess.CalledProcessError:
            continue
    if not image_id:
        raise RuntimeError(f"Docker loaded {archive.name} for {service}, but no load output image reference could be inspected.")
    subprocess.run(["docker", "image", "tag", image_id, local_ref], check=True)


def prepare_offline_bundle(
    layout: RuntimeLayout,
    args,
    *,
    manifest_destination: Path | None = None,
    install_license: bool = False,
) -> Path:
    work_parent = layout.tmp_dir
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="upgrade-bundle-", dir=work_parent) as temp_name:
        work_dir = Path(temp_name)
        bundle_source = str(args.bundle)
        if _is_url(bundle_source) and ".part-" in Path(urllib.parse.urlparse(bundle_source).path).name:
            raise RuntimeError("Remote split bundles are not supported. Reassemble before hosting, or pass a local .part-* file.")
        bundle_path = materialize_source(
            bundle_source,
            work_dir / "downloads",
            "offline bundle",
            args,
            default_name="packetsafari-offline.tar.zst",
        )
        release_public_key = None
        if str(getattr(args, "bundle_public_key", "") or "").strip():
            release_public_key = materialize_source(
                args.bundle_public_key,
                work_dir / "downloads",
                "offline bundle public key",
                args,
                default_name="release-public.pem",
            )
        archive = _materialize_bundle(bundle_path, work_dir)
        bundle_dir = _extract_bundle(archive, work_dir)
        verify_bundle_signature(
            layout,
            bundle_dir,
            public_key=str(release_public_key) if release_public_key else None,
            allow_unsigned=bool(getattr(args, "allow_unsigned_bundle", False)),
        )
        verify_bundle_checksums(bundle_dir)

        manifest = _read_json(bundle_dir / "release-manifest.json", {})
        images = _manifest_images(manifest)
        version_value = str(manifest.get("version") or "release")
        image_dir = bundle_dir / "images"
        if not image_dir.exists():
            raise RuntimeError("Offline bundle is missing images/.")
        archives = sorted(
            path
            for path in image_dir.iterdir()
            if path.is_file() and path.name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.zst"))
        )
        if not archives:
            raise RuntimeError("Offline bundle images/ does not contain Docker image archives.")

        local_images = dict(images)
        loaded_services: set[str] = set()
        for archive_path in archives:
            service = _image_archive_service(archive_path)
            source_ref = image_ref(images, service)
            local_ref = _offline_image_ref(service, version_value)
            _tag_loaded_image(service, archive_path, source_ref, local_ref)
            local_images[service] = local_ref
            loaded_services.add(service)

        if "worker" not in loaded_services and local_images.get("backend"):
            local_images["worker"] = local_images["backend"]
        required = [service for service in ("frontend", "backend", "worker", "redis", "postgres", "sharkd") if service in images or service != "worker"]
        missing = [service for service in required if not local_images.get(service)]
        if missing:
            raise RuntimeError(f"Offline bundle missing required local image refs for: {', '.join(missing)}")

        manifest["sourceImages"] = images
        manifest["images"] = local_images
        manifest["offlineBundle"] = {
            "source": bundle_source,
            "sourcePath": str(bundle_path),
            "verifiedAt": utc_now(),
            "checksums": "checksums.txt",
        }
        target = manifest_destination or layout.target_release_manifest_path
        shutil.copy2(bundle_dir / "release-manifest.json", target.with_suffix(".source.json"))
        _write_json(target, manifest)

        if install_license:
            explicit_license = str(getattr(args, "license", "") or "").strip()
            if explicit_license:
                license_path = materialize_source(
                    explicit_license,
                    work_dir / "downloads",
                    "license token",
                    args,
                    default_name="license-token.json",
                )
            else:
                license_path = bundle_dir / "license-token.json"
                if not license_path.exists():
                    raise RuntimeError("Install bundle is missing license-token.json. Pass --license or rebuild the bundle with a license token.")
            license_public_key = _materialize_license_public_key(layout, args, work_dir / "downloads", bundle_dir=bundle_dir)
            verify_license(license_path, license_public_key)
            shutil.copy2(license_path, layout.license_token_path)
            shutil.copy2(license_public_key, layout.license_public_key_path)
            if release_public_key is not None:
                shutil.copy2(release_public_key, layout.release_public_key_path)
            bundled_release_public_key = bundle_dir / "release-public.pem"
            if release_public_key is None and bundled_release_public_key.exists() and not layout.release_public_key_path.exists():
                shutil.copy2(bundled_release_public_key, layout.release_public_key_path)
        elif release_public_key is not None:
            shutil.copy2(release_public_key, layout.release_public_key_path)

        return target


def prepare_connected_manifest(layout: RuntimeLayout, manifest_arg: str, args=None, *, destination: Path | None = None) -> Path:
    manifest_path = materialize_source(
        manifest_arg,
        layout.tmp_dir / "downloads",
        "release manifest",
        args,
        default_name="release-manifest.json",
    )
    target = destination or layout.target_release_manifest_path
    shutil.copy2(manifest_path, target)
    return target


def run_target_migrations(layout: RuntimeLayout) -> None:
    docker_compose_up(layout, services=["postgres", "redis"], pull_policy="never")
    docker_compose_run(
        layout,
        "backend",
        ["python3", "/app/scripts/sql_storage_upgrade.py", "upgrade"],
    )


def wait_for_health(*, url: str = "http://127.0.0.1:8080/api/v2/health", timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(5)
    raise RuntimeError(f"Health check failed at {url}: {last_error}")


def _promote_release(layout: RuntimeLayout, manifest: dict, snapshot_dir: Path, *, source: str, profile: str, backup_mode: str) -> dict:
    shutil.copy2(layout.target_release_manifest_path, layout.release_manifest_path)
    state = _read_json(layout.deployment_state_path, {})
    deployment = state.setdefault("deployment", {})
    deployment["installedVersion"] = str(manifest.get("version") or "")
    deployment["mode"] = "normal" if profile == "onprem" else "saas"
    deployment["installedAt"] = utc_now()
    state["lastAction"] = {
        "type": "upgrade",
        "status": "ok",
        "message": f"Applied release {deployment['installedVersion']}.",
        "updatedAt": utc_now(),
    }
    rollback_note = (
        "If migrations ran, rollback restores the saved Postgres and storage backup before restarting the previous release."
        if backup_mode == "inline"
        else "This upgrade used an external backup proof. Automatic rollback restores runtime metadata only; restore data from the external backup if migrations must be undone."
    )
    state["rollback"] = {
        "latestSnapshot": str(snapshot_dir),
        "rollbackMode": "restore" if backup_mode == "inline" else "external-data-restore",
        "backupMode": backup_mode,
        "note": rollback_note,
    }
    _write_json(layout.deployment_state_path, state)
    write_helper_status(layout, status="ok", message=f"Upgrade to {deployment['installedVersion']} applied.")
    return {
        "message": "Upgrade applied.",
        "version": deployment["installedVersion"],
        "source": source,
        "profile": profile,
        "backupMode": backup_mode,
        "snapshot": str(snapshot_dir),
    }


def upgrade_release(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    profile = deployment_profile(args)
    backup_mode = resolve_backup_mode(args, profile=profile)
    if not supports_upgrade_host_actions(layout, profile=profile):
        raise RuntimeError(f"Upgrade profile {profile!r} is only supported for managed runtime roots like /opt/packetsafari.")
    with upgrade_lock(layout):
        ensure_runtime_dirs(layout)
        sync_bundle(layout)
        source = "bundle" if getattr(args, "bundle", None) else "manifest"
        snapshot_dir: Path | None = None
        phase = "preflight"
        try:
            if source == "bundle":
                target_manifest_path = prepare_offline_bundle(layout, args)
            else:
                manifest_arg = str(getattr(args, "manifest", "") or "").strip()
                if not manifest_arg:
                    raise RuntimeError("upgrade requires --manifest or --bundle.")
                target_manifest_path = prepare_connected_manifest(layout, manifest_arg, args)

            manifest = _read_json(target_manifest_path, {})
            validate_upgrade_path(layout, manifest)
            if profile == "onprem":
                verify_license_allows_release(layout, manifest)
            else:
                verify_saas_operator_authorization(layout, args, manifest)
            validate_required_env(layout, manifest)
            external_backup_proof = None
            if backup_mode == "require-recent":
                external_backup_proof = verify_external_backup_proof(
                    layout,
                    args,
                    max_age_minutes=int(getattr(args, "max_backup_age_minutes", 180) or 180),
                )

            backup_message = {
                "inline": "creating pre-upgrade backup",
                "require-recent": "recording verified external backup proof",
                "skip": "continuing without a data backup",
            }[backup_mode]
            if layout.compose_file.exists():
                write_helper_status(layout, status="upgrading", message=f"Stopping app services and {backup_message}.")
                docker_compose_stop(layout, services=["frontend", "backend", "worker", "sharkd"], timeout=120)
            else:
                write_helper_status(layout, status="upgrading", message=f"No active Compose file found; treating this as a fresh deployment and {backup_message}.")
            snapshot_dir = snapshot_runtime(layout)
            if backup_mode == "inline":
                complete_full_backup(layout, snapshot_dir)
            elif backup_mode == "require-recent" and external_backup_proof is not None:
                record_external_backup_proof(snapshot_dir, external_backup_proof)
            else:
                _write_json(
                    snapshot_dir / "snapshot.json",
                    {
                        **_read_json(snapshot_dir / "snapshot.json", {}),
                        "backupMode": "skip",
                        "warning": "No data backup was captured by packetsafari-ops for this upgrade.",
                    },
                )

            phase = "compose"
            render_compose(layout, target_manifest_path)
            render_logging_config(layout)
            if source == "manifest":
                write_helper_status(layout, status="upgrading", message="Pulling target release images.")
                docker_compose_pull(layout)

            phase = "migration"
            write_helper_status(layout, status="upgrading", message="Running target database migrations.")
            run_target_migrations(layout)

            phase = "healthcheck"
            write_helper_status(layout, status="upgrading", message="Starting target release and running health checks.")
            docker_compose_up(layout, pull_policy="never")
            if not bool(getattr(args, "skip_health_check", False)):
                wait_for_health(timeout_seconds=int(getattr(args, "health_timeout", 180) or 180))

            phase = "promote"
            return _promote_release(layout, manifest, snapshot_dir, source=source, profile=profile, backup_mode=backup_mode)
        except Exception as exc:
            write_helper_status(layout, status="failed", message=f"Upgrade failed during {phase}: {exc}")
            if snapshot_dir is not None:
                data_restore_required = phase in {"migration", "healthcheck", "promote"}
                restore_data = backup_mode == "inline" and data_restore_required
                mode = "full data restore" if restore_data else "metadata restore"
                try:
                    restore_snapshot(layout, snapshot_dir, restore_data=restore_data)
                    write_helper_status(layout, status="rolled_back", message=f"Upgrade failed during {phase}; restored previous release with {mode}.")
                except Exception as restore_exc:
                    raise RuntimeError(
                        f"Upgrade failed during {phase}: {exc}. Automatic restore also failed: {restore_exc}. Snapshot: {snapshot_dir}"
                    ) from restore_exc
                if data_restore_required and backup_mode != "inline":
                    raise RuntimeError(
                        f"Upgrade failed during {phase}: {exc}. Restored previous runtime metadata only. "
                        f"Restore Postgres and storage from the external backup before serving traffic. Snapshot: {snapshot_dir}"
                    ) from exc
                raise RuntimeError(f"Upgrade failed during {phase}: {exc}. Restored previous release with {mode}. Snapshot: {snapshot_dir}") from exc
            raise RuntimeError(f"Upgrade failed during {phase}: {exc}") from exc


def rollback_release(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    profile = deployment_profile(args)
    if not supports_upgrade_host_actions(layout, profile=profile):
        raise RuntimeError(f"Rollback profile {profile!r} is only supported for managed runtime roots like /opt/packetsafari.")
    with upgrade_lock(layout):
        snapshot_dir = latest_snapshot_dir(layout)
        if snapshot_dir is not None:
            snapshot = _read_json(snapshot_dir / "snapshot.json", {})
            has_inline_data = bool(snapshot.get("postgresBackup")) and bool(snapshot.get("storageBackup"))
            restore_data = profile == "onprem" or has_inline_data
            restore_snapshot(layout, snapshot_dir, restore_data=restore_data)
            message = (
                "Rollback restored latest full snapshot."
                if restore_data
                else "Rollback restored latest runtime metadata snapshot. Restore data from the external backup if schema migrations were applied."
            )
            write_helper_status(layout, status="ok", message=message)
            return {
                "message": message,
                "snapshot": str(snapshot_dir),
                "profile": profile,
                "restoredData": restore_data,
            }

    backups = {
        "manifest": sorted(layout.backup_dir.glob("release-manifest-*.json"), reverse=True),
        "env": sorted(
            [path for path in layout.backup_dir.glob("runtime-*.env") if not path.name.startswith("runtime-sizing-")],
            reverse=True,
        ),
        "state": sorted(layout.backup_dir.glob("deployment-state-*.json"), reverse=True),
        "compose": sorted(
            [path for path in layout.backup_dir.glob("docker-compose-*.yml") if not path.name.startswith("docker-compose-sizing-")],
            reverse=True,
        ),
    }
    if not all(backups.values()):
        raise RuntimeError("No rollback backup found.")

    shutil.copy2(backups["manifest"][0], layout.release_manifest_path)
    shutil.copy2(backups["env"][0], layout.runtime_env_path)
    shutil.copy2(backups["state"][0], layout.deployment_state_path)
    shutil.copy2(backups["compose"][0], layout.compose_file)
    stamp = backups["compose"][0].name.removeprefix("docker-compose-").removesuffix(".yml")
    optional_backups = {
        layout.backup_dir / f"runtime-sizing-{stamp}.env": layout.runtime_sizing_env_path,
        layout.backup_dir / f"sizing-{stamp}.json": layout.sizing_state_path,
        layout.backup_dir / f"docker-compose-sizing-{stamp}.yml": layout.compose_sizing_file,
    }
    for src, dest in optional_backups.items():
        if src.exists():
            shutil.copy2(src, dest)
    render_logging_config(layout)
    docker_compose_up(layout, pull_policy="never")
    write_helper_status(layout, status="ok", message="Rollback restored latest snapshot.")
    return {
        "message": "Rollback restored latest metadata snapshot. No Postgres or storage backup was available in the legacy snapshot format.",
        "manifest": str(backups["manifest"][0]),
        "env": str(backups["env"][0]),
        "state": str(backups["state"][0]),
        "compose": str(backups["compose"][0]),
        "restoredData": False,
    }


def tune_runtime(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        raise RuntimeError("Sizing is only supported for on-prem runtime roots like /opt/packetsafari, not local packetsafari-data mode.")
    plan = write_sizing_profile(layout, profile=str(getattr(args, "profile", "auto") or "auto"))
    applied = False
    if bool(getattr(args, "apply", False)):
        docker_compose_up(layout)
        applied = True
    profile = str(plan.get("effectiveProfile") or "none")
    write_helper_status(
        layout,
        status="ok",
        message=(
            f"Sizing profile {profile} generated and applied."
            if applied
            else f"Sizing profile {profile} generated. Run tune --apply to recreate containers with the new limits."
        ),
    )
    return {
        "message": "Sizing profile generated." if not applied else "Sizing profile generated and applied.",
        "applied": applied,
        "profile": profile,
        "requestedProfile": plan.get("requestedProfile"),
        "state": str(layout.sizing_state_path),
        "env": str(layout.runtime_sizing_env_path),
        "compose": str(layout.compose_sizing_file),
        "plan": plan,
    }


def diagnostics_restart(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    services = [args.service] if getattr(args, "service", None) else None
    docker_compose_restart(layout, services=services)
    write_helper_status(layout, status="ok", message="Restart completed.")
    return {"message": "Restart completed.", "service": args.service or "all"}


def diagnostics_logs(args) -> int:
    command = ["docker", "logs", f"--since={args.since}"]
    if args.tail:
        command.append(f"--tail={args.tail}")
    command.append(args.container)
    return subprocess.run(command, check=False).returncode


def show_runtime_env(layout: RuntimeLayout) -> str:
    if not layout.runtime_env_path.exists():
        return ""
    return layout.runtime_env_path.read_text(encoding="utf-8")


def show_initial_admin_command(*, email: str = "admin@example.com") -> str:
    return (
        "docker exec -it packetsafari-backend "
        f"python3 /app/scripts/create_initial_admin.py --email {email}"
    )


def set_password(args) -> dict:
    docker_exec_backend(
        runtime_layout(args.runtime_root, args.container_runtime_root),
        [
            "python3",
            "/app/packetsafari/maintenance/set_new_password.py",
            args.username,
            args.password,
        ],
    )
    return {"message": f"Password updated for {args.username}."}
