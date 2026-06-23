from __future__ import annotations

import fcntl
import base64
import getpass
import hashlib
import hmac
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
SAAS_REQUIRED_ENV_KEYS = [
    "PACKETSAFARI_PUBLIC_BASE_URL",
    "OPENAI_API_KEY",
    "PACKETSAFARI_PADDLE_API_KEY",
    "PACKETSAFARI_PADDLE_WEBHOOK_SECRET",
]
IRONPROXY_UPSTREAM_SECRET_KEYS = {
    "OPENAI_API_KEY",
    "PACKETSAFARI_PADDLE_API_KEY",
}
IRONPROXY_PLACEHOLDER_VALUES = {
    "OPENAI_API_KEY": "ps_proxy_openai_api_key",
    "PACKETSAFARI_PADDLE_API_KEY": "ps_proxy_paddle_api_key",
}
INVALID_REQUIRED_ENV_VALUES = {
    "",
    "changeme",
    "change-me",
    "todo",
    "replace-me",
    "example",
    "example-secret",
    "ps_proxy_openai_api_key",
    "ps_proxy_paddle_api_key",
}
BACKUP_MODES = {"inline", "require-recent", "skip"}
UPGRADE_SIMULATION_PHASES = {"preflight", "compose", "migration", "healthcheck", "promote"}
MIB = 1024 * 1024
GIB = 1024 * MIB
SIZING_PROFILES = {"auto", "small", "medium", "large", "none"}
DEFAULT_IMAGE_RETENTION_KEEP_DEPLOYMENTS = 2

DEFAULT_LOGGING_VALUES = {
    "AUDIT_LOG_ENABLED": "true",
    "AUDIT_LOG_PERSIST": "true",
    "AUDIT_RETENTION_DAYS": "365",
    "AUDIT_FORWARDING_MODE": "stdout_json",
    "AUDIT_FORWARDER_TYPE": "none",
}
GLOBAL_WRAPPER_PATH = Path("/usr/local/bin/packetsafari-ops")
DEFAULT_UPDATE_PLATFORM = "linux-arm64"
DEFAULT_UPDATE_BASE_URL = "https://releases.packetsafari.com"
DEFAULT_SAAS_UPDATE_BUCKET = "packetsafari-release-channels-166826692770"
DEFAULT_SAAS_UPDATE_REGION = "eu-central-1"
JOURNALD_RETENTION_CONFIG_PATH = Path("/etc/systemd/journald.conf.d/packetsafari.conf")
JOURNALD_RETENTION_CONFIG = """# Managed by packetsafari-ops. Keeps container logs after Docker recreates containers.
[Journal]
Storage=persistent
SystemMaxUse=2G
SystemKeepFree=5G
MaxRetentionSec=7day
MaxFileSec=1day
Compress=yes
"""


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
    def configuration_dir(self) -> Path:
        return self.runtime_root / "configuration"

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
    def ironproxy_env_path(self) -> Path:
        return self.env_dir / "ironproxy.env"

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


def _manifest_tooling_requirements(manifest: dict) -> dict:
    tooling = manifest.get("tooling")
    return tooling if isinstance(tooling, dict) else {}


def required_ops_version(manifest: dict) -> str:
    tooling = _manifest_tooling_requirements(manifest)
    return str(tooling.get("minOpsVersion") or tooling.get("minimumOpsVersion") or "").strip()


def tooling_update_status(manifest: dict) -> dict:
    required = required_ops_version(manifest)
    tooling = _manifest_tooling_requirements(manifest)
    target = str(tooling.get("version") or required or "").strip()
    current = version()
    if not required:
        return {
            "required": False,
            "currentVersion": current,
            "requiredVersion": "",
            "targetVersion": target,
            "available": bool(target and _version_key(current) < _version_key(target)),
            "status": "not_required",
            "message": "Release manifest does not declare a minimum packetsafari-ops version.",
        }
    if _version_key(current) < _version_key(required):
        return {
            "required": True,
            "currentVersion": current,
            "requiredVersion": required,
            "targetVersion": target or required,
            "available": True,
            "status": "upgrade_required",
            "message": (
                f"This release requires packetsafari-ops {required} or newer; "
                f"this host is running {current}. Update the on-prem tooling before applying the release."
            ),
        }
    return {
        "required": True,
        "currentVersion": current,
        "requiredVersion": required,
        "targetVersion": target or required,
        "available": bool(target and _version_key(current) < _version_key(target)),
        "status": "ok",
        "message": f"packetsafari-ops {current} satisfies release requirement {required}.",
    }


def validate_tooling_requirement(manifest: dict) -> None:
    status = tooling_update_status(manifest)
    if status["status"] == "upgrade_required":
        raise RuntimeError(str(status["message"]))


def _tooling_archive_source(manifest: dict, bundle_dir: Path | None = None) -> str:
    tooling = _manifest_tooling_requirements(manifest)
    archive_path = str(tooling.get("archivePath") or tooling.get("archive") or "").strip()
    if archive_path:
        path = Path(archive_path)
        if not path.is_absolute() and bundle_dir is not None:
            path = bundle_dir / path
        return str(path)
    return str(tooling.get("archiveUrl") or tooling.get("url") or "").strip()


def _verify_sha256(path: Path, expected: str, label: str) -> None:
    expected = str(expected or "").strip().lower()
    if not expected:
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeError(f"{label} checksum mismatch: expected {expected}, got {actual}.")


def _find_tooling_root(extract_dir: Path) -> Path:
    candidates = [extract_dir, *[path for path in extract_dir.iterdir() if path.is_dir()]]
    for candidate in candidates:
        if (candidate / "packetsafari_onprem" / "cli.py").exists():
            return candidate
    raise RuntimeError("Downloaded packetsafari-ops archive does not contain packetsafari_onprem/cli.py.")


def _copy_tooling_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".guard",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.pyc",
            "*.egg-info",
            "build",
            "dist",
        ),
    )


def _install_tooling_archive(layout: RuntimeLayout, archive: Path) -> None:
    parent = layout.tooling_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="packetsafari-ops-tooling-") as tmp:
        extract_dir = Path(tmp) / "extract"
        extract_dir.mkdir(parents=True)
        shutil.unpack_archive(str(archive), str(extract_dir))
        source_root = _find_tooling_root(extract_dir)
        new_root = parent / f".onprem-new-{os.getpid()}"
        backup_root = parent / f".onprem-previous-{utc_now().replace(':', '').replace('+', '-')}"
        shutil.rmtree(new_root, ignore_errors=True)
        _copy_tooling_tree(source_root, new_root)
        try:
            if layout.tooling_root.exists():
                os.replace(layout.tooling_root, backup_root)
            os.replace(new_root, layout.tooling_root)
            install_wrapper(layout)
            shutil.rmtree(backup_root, ignore_errors=True)
        except Exception:
            if layout.tooling_root.exists():
                shutil.rmtree(layout.tooling_root, ignore_errors=True)
            if backup_root.exists():
                os.replace(backup_root, layout.tooling_root)
            raise
        finally:
            shutil.rmtree(new_root, ignore_errors=True)


def maybe_self_update_tooling(args, layout: RuntimeLayout, manifest: dict, *, bundle_dir: Path | None = None) -> dict:
    status = tooling_update_status(manifest)
    if not bool(status.get("available")):
        return {"updated": False, **status}
    if layout.kind != "onprem-runtime-root":
        return {"updated": False, "skipped": True, "reason": "local_data_root", **status}
    source = _tooling_archive_source(manifest, bundle_dir=bundle_dir)
    if not source:
        if status.get("status") == "upgrade_required":
            raise RuntimeError(f"{status['message']} The release manifest does not provide a tooling archive.")
        return {"updated": False, "skipped": True, "reason": "missing_archive", **status}

    archive = materialize_source(
        source,
        layout.tmp_dir / "downloads",
        "packetsafari-ops archive",
        args,
        default_name="packetsafari-onprem.tar.gz",
    )
    tooling = _manifest_tooling_requirements(manifest)
    _verify_sha256(archive, str(tooling.get("sha256") or ""), "packetsafari-ops archive")
    write_helper_status(layout, status="upgrading", message=f"Updating packetsafari-ops {version()} -> {status.get('targetVersion')}.")
    _install_tooling_archive(layout, archive)
    write_helper_status(layout, status="ok", message=f"packetsafari-ops updated to {status.get('targetVersion')}.")
    if _truthy(os.getenv("PACKETSAFARI_OPS_SELF_UPDATE_NO_REEXEC")):
        return {"updated": True, "reexec": False, **status}
    cli_path = layout.tooling_root / "packetsafari_onprem" / "cli.py"
    os.execv(sys.executable, [sys.executable, str(cli_path), *sys.argv[1:]])
    raise RuntimeError("Failed to re-exec updated packetsafari-ops.")


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
        layout.configuration_dir,
        layout.tooling_root,
        layout.bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def supports_onprem_host_actions(layout: RuntimeLayout) -> bool:
    return layout.kind == "onprem-runtime-root"


def ensure_journald_retention_config() -> dict[str, object]:
    path = JOURNALD_RETENTION_CONFIG_PATH
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == JOURNALD_RETENTION_CONFIG:
        return {"path": str(path), "changed": False, "restarted": False}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(JOURNALD_RETENTION_CONFIG, encoding="utf-8")
    restarted = False
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "restart", "systemd-journald"], check=True)
        restarted = True
    return {"path": str(path), "changed": True, "restarted": restarted}


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
    if mode == "skip" and not (
        bool(getattr(args, "allow_unbacked_upgrade", False))
        or _truthy(os.getenv("PACKETSAFARI_ALLOW_UNBACKED_UPGRADE"))
    ):
        raise RuntimeError(
            "Unbacked upgrades are disabled. Pass --allow-unbacked-upgrade only for container-only releases "
            "or disposable development hosts."
        )
    return mode


def requested_upgrade_simulation_phase(args) -> str:
    phase = str(getattr(args, "simulate_failure_phase", "") or "").strip().lower()
    if not phase:
        return ""
    if phase not in UPGRADE_SIMULATION_PHASES:
        raise RuntimeError(f"Unsupported upgrade simulation failure phase: {phase}")
    if not _truthy(os.getenv("PACKETSAFARI_ENABLE_UPGRADE_SIMULATION")):
        raise RuntimeError("Upgrade failure simulation is disabled. Set PACKETSAFARI_ENABLE_UPGRADE_SIMULATION=true only on disposable test hosts.")
    return phase


def maybe_fail_upgrade_simulation(layout: RuntimeLayout, args, phase: str) -> None:
    requested = requested_upgrade_simulation_phase(args)
    if requested != phase:
        return
    if phase in {"migration", "healthcheck", "promote"}:
        postgres = _postgres_env(layout)
        env = {"PGPASSWORD": postgres["POSTGRES_PASSWORD"]}
        docker_compose_exec(
            layout,
            "postgres",
            [
                "psql",
                "-U",
                postgres["POSTGRES_USER"],
                "-d",
                postgres["POSTGRES_DB"],
                "-c",
                "create table if not exists packetsafari_upgrade_simulated_corruption(id integer primary key, phase text); insert into packetsafari_upgrade_simulated_corruption(id, phase) values (1, 'simulation') on conflict (id) do update set phase = excluded.phase;",
            ],
            env=env,
        )
        docker_compose_run(
            layout,
            "backend",
            ["sh", "-c", "printf simulation > /storage/upgrade-simulated-corruption.txt"],
        )
    raise RuntimeError(f"Simulated upgrade failure during {phase}.")


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


def _local_saas_operator_token_hash(layout: RuntimeLayout) -> str:
    token_path = layout.secrets_dir / "saas-operator-token"
    if not token_path.exists():
        return ""
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest().lower()


def verify_saas_operator_authorization(layout: RuntimeLayout, args, manifest: dict) -> None:
    expected_hash = _manifest_saas_token_hash(manifest) or _local_saas_operator_token_hash(layout)
    if not expected_hash:
        raise RuntimeError(
            "SaaS profile requires an internal operator token hash. Set deploymentProfiles.saas.operatorTokenSha256 "
            "in the manifest, PACKETSAFARI_SAAS_OPERATOR_TOKEN_SHA256 on the host, or install "
            "/opt/packetsafari/secrets/saas-operator-token."
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


def _profile_config(manifest: dict, profile: str) -> dict:
    profiles = manifest.get("deploymentProfiles")
    if isinstance(profiles, dict):
        config = profiles.get(profile)
        if isinstance(config, dict):
            return config
    return {}


def _profile_uses_static_frontend(manifest: dict, profile: str) -> bool:
    config = _profile_config(manifest, profile)
    return _truthy(config.get("staticFrontend")) or _truthy(config.get("omitFrontendService"))


def _is_url(source: str) -> bool:
    parsed = urllib.parse.urlparse(str(source or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_s3_uri(source: str) -> bool:
    parsed = urllib.parse.urlparse(str(source or ""))
    return parsed.scheme == "s3" and bool(parsed.netloc) and bool(parsed.path.strip("/"))


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


def _aws_quote(value: str, *, safe: str = "") -> str:
    return urllib.parse.quote(value, safe=safe)


def _http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, timeout: int = 3) -> dict:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _aws_credentials_from_env() -> dict[str, str]:
    access_key = str(os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = str(os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    session_token = str(os.getenv("AWS_SESSION_TOKEN") or "").strip()
    if access_key and secret_key:
        return {
            "AccessKeyId": access_key,
            "SecretAccessKey": secret_key,
            "Token": session_token,
        }
    return {}


def _aws_credentials_from_imds() -> dict[str, str]:
    endpoint = "http://169.254.169.254/latest"
    token = ""
    try:
        request = urllib.request.Request(
            f"{endpoint}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            token = response.read().decode("utf-8")
    except Exception:
        token = ""
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    try:
        role_request = urllib.request.Request(f"{endpoint}/meta-data/iam/security-credentials/", headers=headers)
        with urllib.request.urlopen(role_request, timeout=2) as response:
            role_name = response.read().decode("utf-8").splitlines()[0].strip()
        if not role_name:
            return {}
        return _http_json(
            f"{endpoint}/meta-data/iam/security-credentials/{_aws_quote(role_name)}",
            headers=headers,
            timeout=2,
        )
    except Exception:
        return {}


def _aws_credentials() -> dict[str, str]:
    credentials = _aws_credentials_from_env() or _aws_credentials_from_imds()
    if not credentials.get("AccessKeyId") or not credentials.get("SecretAccessKey"):
        raise RuntimeError("No AWS credentials available from env or EC2 instance metadata.")
    return credentials


def _aws_signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    def sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    k_date = sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = sign(k_date, region)
    k_service = sign(k_region, service)
    return sign(k_service, "aws4_request")


def _copy_s3_source_sigv4(source: str, destination: Path, partial: Path) -> None:
    parsed = urllib.parse.urlparse(source)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    region = str(os.getenv("PACKETSAFARI_UPDATE_S3_REGION") or DEFAULT_SAAS_UPDATE_REGION).strip()
    credentials = _aws_credentials()
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    host = f"{bucket}.s3.{region}.amazonaws.com"
    canonical_uri = "/" + _aws_quote(key, safe="/")
    canonical_querystring = ""
    headers = {
        "host": host,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        "x-amz-date": amzdate,
    }
    token = str(credentials.get("Token") or "").strip()
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join([
        "GET",
        canonical_uri,
        canonical_querystring,
        canonical_headers,
        signed_headers,
        "UNSIGNED-PAYLOAD",
    ])
    credential_scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _aws_signing_key(str(credentials["SecretAccessKey"]), datestamp, region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials['AccessKeyId']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    request = urllib.request.Request(f"https://{host}{canonical_uri}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    partial.replace(destination)


def _copy_s3_source(source: str, destination: Path, partial: Path, label: str) -> None:
    aws_bin = shutil.which("aws")
    if aws_bin:
        region = str(os.getenv("PACKETSAFARI_UPDATE_S3_REGION") or DEFAULT_SAAS_UPDATE_REGION).strip()
        cmd = [aws_bin, "s3", "cp", source, str(partial), "--only-show-errors"]
        if region:
            cmd.extend(["--region", region])
        subprocess.run(cmd, check=True)
        partial.replace(destination)
        return
    try:
        _copy_s3_source_sigv4(source, destination, partial)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot fetch {label} from {source} with AWS CLI or instance-role SigV4: {exc}"
        ) from exc


def materialize_source(source: str | os.PathLike[str], destination_dir: Path, label: str, args=None, *, default_name: str) -> Path:
    raw = str(source or "").strip()
    if not raw:
        raise RuntimeError(f"Missing {label} source.")
    if _is_s3_uri(raw):
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / _safe_download_name(raw, default_name)
        partial = destination.with_name(f"{destination.name}.download")
        try:
            _copy_s3_source(raw, destination, partial, label)
        finally:
            partial.unlink(missing_ok=True)
        return destination
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


def _postgres_memory(value: int) -> str:
    mib = max(1, int(round(float(value) / float(MIB))))
    if mib >= 1024 and mib % 1024 == 0:
        return f"{mib // 1024}GB"
    return f"{mib}MB"


def _service_cpu_plan(vcpus: int, profile: str) -> dict[str, float]:
    if profile == "large":
        frontend = 0.75
        redis = 1.0
        postgres = min(4.0, max(2.0, vcpus * 0.10))
        backend = min(6.0, max(1.5, vcpus * 0.15))
        sharkd = min(24.0, max(4.0, vcpus * 0.55))
        worker = min(24.0, max(4.0, vcpus * 0.75))
    elif profile == "medium":
        frontend = 0.5
        redis = 0.75
        postgres = 1.5
        backend = min(2.0, max(1.0, vcpus * 0.15))
        sharkd = min(6.0, max(1.25, vcpus * 0.35))
        worker = min(10.0, max(1.5, vcpus * 0.55))
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
            "worker": 56 * GIB,
            "redis": 6 * GIB,
            "postgres": 10 * GIB,
            "sharkd": 20 * GIB,
            "audit-forwarder": 1 * GIB,
        },
    }[profile]
    if profile == "large":
        weights = {
            "frontend": 0.03,
            "backend": 0.08,
            "worker": 0.60,
            "redis": 0.04,
            "postgres": 0.08,
            "sharkd": 0.18,
            "audit-forwarder": 0.02,
        }
    elif profile == "medium":
        weights = {
            "frontend": 0.03,
            "backend": 0.10,
            "worker": 0.42,
            "redis": 0.05,
            "postgres": 0.10,
            "sharkd": 0.18,
            "audit-forwarder": 0.02,
        }
    else:
        weights = {
            "frontend": 0.03,
            "backend": 0.12,
            "worker": 0.30,
            "redis": 0.06,
            "postgres": 0.12,
            "sharkd": 0.20,
            "audit-forwarder": 0.02,
        }
    raw = {
        "frontend": min(max_by_profile["frontend"], max(512 * MIB, int(memory_bytes * weights["frontend"]))),
        "backend": min(max_by_profile["backend"], max(1 * GIB, int(memory_bytes * weights["backend"]))),
        "worker": min(max_by_profile["worker"], max(3 * GIB, int(memory_bytes * weights["worker"]))),
        "redis": min(max_by_profile["redis"], max(512 * MIB, int(memory_bytes * weights["redis"]))),
        "postgres": min(max_by_profile["postgres"], max(2 * GIB, int(memory_bytes * weights["postgres"]))),
        "sharkd": min(max_by_profile["sharkd"], max(2 * GIB, int(memory_bytes * weights["sharkd"]))),
        "audit-forwarder": min(
            max_by_profile["audit-forwarder"],
            max(256 * MIB, int(memory_bytes * weights["audit-forwarder"])),
        ),
    }
    return _scale_memory_plan(memory_bytes, raw)


def _index_worker_memory_reserve_mib(worker_memory_mib: int) -> int:
    if worker_memory_mib <= 4096:
        return 1024
    if worker_memory_mib <= 12288:
        return 2048
    return 4096


def _index_memory_per_task_mib(*, worker_memory_bytes: int, profile: str) -> int:
    profile_default = {"small": 3072, "medium": 2048, "large": 1536}[profile]
    worker_memory_mib = max(1, int(int(worker_memory_bytes) / MIB))
    reserve_mib = _index_worker_memory_reserve_mib(worker_memory_mib)
    usable_mib = max(512, worker_memory_mib - reserve_mib)
    return max(512, min(int(profile_default), int(usable_mib)))


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
            "memoryLimited": False,
        }
        for service in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
    }

    aichat_concurrency = {"small": 2, "medium": 2, "large": 3}[effective_profile]
    uwsgi_processes = {
        "small": max(4, min(6, vcpus * 2 or 4)),
        "medium": max(4, min(8, vcpus // 2)),
        "large": max(6, min(12, vcpus // 2)),
    }[effective_profile]
    sharkd_lru_size = {"small": 4, "medium": 10, "large": 16}[effective_profile]
    rule_shard_workers = {"small": 1, "medium": 2, "large": 4}[effective_profile]
    index_cpu_fraction = {"small": "0.60", "medium": "0.70", "large": "0.85"}[effective_profile]
    worker_memory_mib = max(1, int(int(services["worker"]["memoryBytes"]) / MIB))
    index_memory_reserve_mib = _index_worker_memory_reserve_mib(worker_memory_mib)
    index_memory_per_task_mib = _index_memory_per_task_mib(
        worker_memory_bytes=int(services["worker"]["memoryBytes"]),
        profile=effective_profile,
    )
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
        "CELERY_INDEX_CONCURRENCY": "auto",
        "PACKETSAFARI_CELERY_INDEX_CONCURRENCY_MAX": str({"small": 4, "medium": 12, "large": 24}[effective_profile]),
        "PACKETSAFARI_CELERY_INDEX_CPU_FRACTION": index_cpu_fraction,
        "PACKETSAFARI_CELERY_INDEX_MEMORY_PER_TASK_MIB": str(index_memory_per_task_mib),
        "PACKETSAFARI_CELERY_INDEX_MEMORY_RESERVE_MIB": str(index_memory_reserve_mib),
        "CELERY_AICHAT_LOGLEVEL": "info",
        "CELERY_INDEX_LOGLEVEL": "info",
        "PACKETSAFARI_UWSGI_PROCESSES": str(uwsgi_processes),
        "PACKETSAFARI_UWSGI_THREADS": "2",
        "PACKETSAFARI_UWSGI_RELOAD_ON_RSS_MB": str(reload_on_rss),
        "PACKETSAFARI_BACKEND_LIVENESS_TIMEOUT_SECONDS": "10",
        "PACKETSAFARI_BACKEND_LIVENESS_FAILURE_THRESHOLD": "6",
        "PACKETSAFARI_BACKEND_LIVENESS_RESTART_COOLDOWN_SECONDS": "60",
        "PACKETSAFARI_ROW_WINDOW_MAX_LIMIT_ROWS": "512",
        "PACKETSAFARI_ROW_WINDOW_INFLIGHT_WAIT_SECONDS": "12",
        "PACKETSAFARI_ROW_WINDOW_LOCK_TTL_SECONDS": "30",
        "MAINTENANCE_STORAGE_CLEANUP_RESCHEDULE_ENABLED": "false",
        "PACKETSAFARI_CAPTURE_SHARKD_LRU_SIZE": str(sharkd_lru_size),
        "PACKETSAFARI_SHARKD_PACKETSTATS_RULE_SHARD_WORKERS": str(rule_shard_workers),
        "HEAVY_STAGE_MIN_AVAILABLE_MIB": str({"small": 768, "medium": 1024, "large": 1536}[effective_profile]),
        "HEAVY_STAGE_MEMORY_SOFT_LIMIT_PERCENT": str({"small": 78, "medium": 82, "large": 84}[effective_profile]),
        "HEAVY_STAGE_MEMORY_HARD_LIMIT_PERCENT": "90",
        "PACKETSAFARI_REDIS_MAXMEMORY": _compose_memory(redis_max_bytes),
        "POSTGRES_SHARED_BUFFERS": _postgres_memory(postgres_shared_buffers),
        "POSTGRES_EFFECTIVE_CACHE_SIZE": _postgres_memory(postgres_effective_cache_size),
        "POSTGRES_WORK_MEM": _postgres_memory(postgres_work_mem),
        "POSTGRES_MAINTENANCE_WORK_MEM": _postgres_memory(postgres_maintenance_work_mem),
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
    include_frontend = "frontend" in _rendered_compose_services(layout)

    def service_value(name: str, key: str) -> str:
        service = services.get(name) if isinstance(services.get(name), dict) else {}
        return str(service.get(key) or "")

    def service_block(
        name: str,
        *,
        env_file: bool = False,
        extra: list[str] | None = None,
    ) -> list[str]:
        lines = [
            f"  {name}:",
            f"    cpus: {service_value(name, 'cpus')}",
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
        "        CELERY_AICHAT_CONCURRENCY=\"$$(python3 /app/scripts/resolve_worker_concurrency.py aichat)\"",
        "        CELERY_INDEX_CONCURRENCY=\"$$(python3 /app/scripts/resolve_worker_concurrency.py index)\"",
        "        export CELERY_AICHAT_CONCURRENCY CELERY_INDEX_CONCURRENCY",
        "        echo \"Resolved Celery worker concurrency: aichat=$$CELERY_AICHAT_CONCURRENCY index=$$CELERY_INDEX_CONCURRENCY\"",
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
        "          --concurrency=\"$${CELERY_INDEX_CONCURRENCY:-auto}\" \\",
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
        f"      - shared_buffers={env.get('POSTGRES_SHARED_BUFFERS') or '512MB'}",
        "      - -c",
        f"      - effective_cache_size={env.get('POSTGRES_EFFECTIVE_CACHE_SIZE') or '2GB'}",
        "      - -c",
        f"      - work_mem={env.get('POSTGRES_WORK_MEM') or '16MB'}",
        "      - -c",
        f"      - maintenance_work_mem={env.get('POSTGRES_MAINTENANCE_WORK_MEM') or '256MB'}",
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
        *([] if not include_frontend else service_block("frontend")),
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

    if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
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
        try:
            if child.resolve() == target.resolve():
                continue
        except FileNotFoundError:
            pass
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
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    cli_path = layout.tooling_root / "packetsafari_onprem" / "cli.py"
    wrapper = f"""#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH={str(layout.tooling_root)!r}${{PYTHONPATH:+:${{PYTHONPATH}}}}
exec python3 {str(cli_path)!r} "$@"
"""
    layout.wrapper_path.write_text(wrapper, encoding="utf-8")
    layout.wrapper_path.chmod(0o755)
    install_global_wrapper(layout)


def install_global_wrapper(layout: RuntimeLayout) -> None:
    if layout.kind != "onprem-runtime-root":
        return
    GLOBAL_WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if GLOBAL_WRAPPER_PATH.is_symlink() or not GLOBAL_WRAPPER_PATH.exists():
        tmp_link = GLOBAL_WRAPPER_PATH.with_name(f".{GLOBAL_WRAPPER_PATH.name}.tmp")
        try:
            tmp_link.unlink()
        except FileNotFoundError:
            pass
        tmp_link.symlink_to(layout.wrapper_path)
        os.replace(tmp_link, GLOBAL_WRAPPER_PATH)
        return
    if GLOBAL_WRAPPER_PATH.is_file():
        existing = GLOBAL_WRAPPER_PATH.read_text(encoding="utf-8", errors="ignore")
        if "packetsafari_onprem" in existing or "packetsafari-ops" in existing:
            GLOBAL_WRAPPER_PATH.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
exec {str(layout.wrapper_path)!r} "$@"
""",
                encoding="utf-8",
            )
            GLOBAL_WRAPPER_PATH.chmod(0o755)
            return
    raise RuntimeError(
        f"Cannot install {GLOBAL_WRAPPER_PATH}: path exists and is not a PacketSafari-managed wrapper."
    )


def write_helper_status(layout: RuntimeLayout, *, status: str = "ok", message: str = "ready") -> None:
    _write_json(
        layout.helper_status_path,
        {
            "service": "packetsafari-ops",
            "installed": True,
            "commandPath": str(layout.wrapper_path),
            "globalCommandPath": str(GLOBAL_WRAPPER_PATH) if layout.kind == "onprem-runtime-root" else "",
            "status": status,
            "message": message,
            "updatedAt": utc_now(),
        },
    )


def render_compose(layout: RuntimeLayout, manifest_path: Path, *, source_root: Path | None = None, profile: str = "onprem") -> None:
    root = source_root or layout.tooling_root
    config_source = root / "templates" / "egress-config"
    if config_source.exists():
        shutil.copytree(config_source, layout.configuration_dir, dirs_exist_ok=True)
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
            "--ironproxy-env-path",
            str(layout.ironproxy_env_path),
            "--host-runtime-root",
            str(layout.runtime_root),
            "--container-runtime-root",
            str(layout.container_runtime_root),
            "--profile",
            profile,
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


def _profile_required_env_keys(profile: str) -> list[str]:
    if profile == "saas":
        return list(SAAS_REQUIRED_ENV_KEYS)
    return []


def _merged_required_env_keys(manifest: dict, *, profile: str) -> list[str]:
    keys: list[str] = []
    profile_required_env = []
    profile_required = manifest.get("profileRequiredEnv")
    if isinstance(profile_required, dict):
        profile_items = profile_required.get(profile)
        if isinstance(profile_items, list):
            profile_required_env = [str(item).strip() for item in profile_items]
    for item in (
        _profile_required_env_keys(profile)
        + profile_required_env
        + [str(item).strip() for item in (manifest.get("requiredEnv") or [])]
    ):
        key = str(item or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _required_env_value_is_valid(key: str, value: str) -> bool:
    normalized = str(value or "").strip().strip("'\"")
    if not normalized:
        return False
    if normalized.lower() in INVALID_REQUIRED_ENV_VALUES:
        return False
    if normalized.startswith("${") and normalized.endswith("}"):
        return False
    if key == "PACKETSAFARI_PUBLIC_BASE_URL":
        return normalized.startswith(("https://", "http://"))
    return True


def _effective_required_env_values(layout: RuntimeLayout) -> dict[str, str]:
    runtime_env = parse_env_file(layout.runtime_env_path)
    ironproxy_env = parse_env_file(layout.ironproxy_env_path)
    values = dict(runtime_env)
    for key in IRONPROXY_UPSTREAM_SECRET_KEYS:
        if key in ironproxy_env:
            values[key] = ironproxy_env[key]
    return values


def validate_required_env(layout: RuntimeLayout, manifest: dict, *, profile: str) -> None:
    runtime_env = _effective_required_env_values(layout)
    required = _merged_required_env_keys(manifest, profile=profile)
    missing = [key for key in required if not _required_env_value_is_valid(key, str(runtime_env.get(key, "")))]
    if missing:
        profile_note = " for SaaS profile" if profile == "saas" else ""
        raise RuntimeError(
            f"Target manifest requires missing or placeholder runtime env keys{profile_note}: {', '.join(missing)}. "
            "Run `packetsafari-ops config prompt-env --profile "
            f"{profile} --manifest <release-manifest>` or update the managed runtime env before deploying."
        )


def _required_env_keys(manifest: dict, *, profile: str = "onprem") -> list[str]:
    return _merged_required_env_keys(manifest, profile=profile)


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
    if upper == "POSTGRES_DB":
        return "packetsafari"
    if upper == "POSTGRES_USER":
        return "packetsafari"
    if upper == "POSTGRES_PASSWORD":
        return secrets.token_urlsafe(48)
    if upper == "PACKETSAFARI_RUNTIME_POSTGRES_ENABLED":
        return "true"
    if upper == "PACKETSAFARI_RUNTIME_POSTGRES_URL":
        postgres_db = os.getenv("POSTGRES_DB", "packetsafari")
        postgres_user = os.getenv("POSTGRES_USER", "packetsafari")
        postgres_password = os.getenv("POSTGRES_PASSWORD", "")
        return f"postgresql+psycopg2://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}"
    if upper in {
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST",
    }:
        return "redis"
    if upper in {
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT",
    }:
        return "6379"
    if upper in {
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_DB",
        "PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB",
    }:
        return "0"
    return ""


def _redis_alias_default(values: dict[str, str]) -> str:
    for key in (
        "REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD",
    ):
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return secrets.token_urlsafe(48)


def _postgres_url_default(values: dict[str, str]) -> str:
    postgres_db = str(values.get("POSTGRES_DB") or "packetsafari").strip()
    postgres_user = str(values.get("POSTGRES_USER") or "packetsafari").strip()
    postgres_password = str(values.get("POSTGRES_PASSWORD") or "").strip()
    if not postgres_password:
        postgres_password = secrets.token_urlsafe(48)
        values["POSTGRES_PASSWORD"] = postgres_password
    values.setdefault("POSTGRES_DB", postgres_db)
    values.setdefault("POSTGRES_USER", postgres_user)
    return f"postgresql+psycopg2://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}"


def _generated_env_default_for_values(key: str, values: dict[str, str]) -> str:
    upper = key.upper()
    if upper in {
        "REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD",
        "PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD",
    }:
        return _redis_alias_default(values)
    if upper == "PACKETSAFARI_RUNTIME_POSTGRES_URL":
        return _postgres_url_default(values)
    return _generated_env_default(key)


def configure_required_env(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    manifest_arg = str(getattr(args, "doctor_manifest", "") or getattr(args, "_doctor_manifest_path", "") or getattr(args, "manifest", "") or "").strip()
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
    profile = deployment_profile(args)
    required = _required_env_keys(manifest, profile=profile)
    env_path = Path(str(getattr(args, "output", "") or "")).expanduser() if getattr(args, "output", None) else layout.runtime_env_path
    split_proxy_env = profile == "saas" and not getattr(args, "output", None)
    runtime_existing = parse_env_file(env_path)
    proxy_existing = parse_env_file(layout.ironproxy_env_path) if split_proxy_env else {}
    existing = dict(runtime_existing)
    if split_proxy_env:
        for key in IRONPROXY_UPSTREAM_SECRET_KEYS:
            if key in proxy_existing:
                existing[key] = proxy_existing[key]
    missing = [key for key in required if not _required_env_value_is_valid(key, str(existing.get(key, "")))]
    action = str(getattr(args, "action", "") or "")

    if action == "check-env":
        return {
            "profile": profile,
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
        generated_default = _generated_env_default_for_values(key, values)
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

    if split_proxy_env:
        runtime_values = dict(runtime_existing)
        proxy_values = dict(proxy_existing)
        for key, value in values.items():
            if key in IRONPROXY_UPSTREAM_SECRET_KEYS:
                proxy_values[key] = value
                runtime_values[key] = IRONPROXY_PLACEHOLDER_VALUES[key]
            else:
                runtime_values[key] = value
        write_env_file(
            env_path,
            runtime_values,
            header_lines=[
                "# Managed by PacketSafari ops.",
                "# Generated/updated by packetsafari-ops config prompt-env.",
                "# Upstream OpenAI/Paddle API secrets are stored in ironproxy.env.",
            ],
        )
        write_env_file(
            layout.ironproxy_env_path,
            proxy_values,
            header_lines=[
                "# Managed by PacketSafari ops.",
                "# Upstream egress proxy secrets. Mount only into egress-ironproxy.",
            ],
        )
    else:
        write_env_file(
            env_path,
            values,
            header_lines=[
                "# Managed by PacketSafari ops.",
                "# Generated/updated by packetsafari-ops config prompt-env.",
            ],
        )
    return {
        "profile": profile,
        "manifest": str(manifest_path),
        "envPath": str(env_path),
        "required": required,
        "prompted": prompted,
        "missingBefore": missing,
        "missingAfter": [key for key in required if not _required_env_value_is_valid(key, str(values.get(key, "")))],
        "ok": True,
    }


def _validate_upstream_proxy_url(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"{field} cannot be empty.")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{field} must be an http:// or https:// proxy URL with a host.")
    return text


def configure_upstream_proxy(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    proxy_url = str(getattr(args, "proxy_url", "") or "").strip()
    http_proxy = str(getattr(args, "http_proxy", "") or "").strip()
    https_proxy = str(getattr(args, "https_proxy", "") or "").strip()
    no_proxy = getattr(args, "no_proxy", None)
    clear = bool(getattr(args, "clear", False))
    restart = bool(getattr(args, "restart", False))

    if clear and any([proxy_url, http_proxy, https_proxy, no_proxy is not None]):
        raise RuntimeError("--clear cannot be combined with proxy values.")
    if proxy_url and (http_proxy or https_proxy):
        raise RuntimeError("--proxy-url cannot be combined with --http-proxy or --https-proxy.")
    if not clear and not any([proxy_url, http_proxy, https_proxy, no_proxy is not None]):
        raise RuntimeError("Provide --proxy-url, --http-proxy/--https-proxy, --no-proxy, or --clear.")

    values = parse_env_file(layout.ironproxy_env_path)
    changed_keys: list[str] = []

    def set_or_remove(key: str, value: str | None) -> None:
        previous = values.get(key)
        if value is None:
            if key in values:
                values.pop(key, None)
                changed_keys.append(key)
            return
        if previous != value:
            values[key] = value
            changed_keys.append(key)

    if clear:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            set_or_remove(key, None)
    else:
        if proxy_url:
            validated = _validate_upstream_proxy_url(proxy_url, field="--proxy-url")
            set_or_remove("HTTP_PROXY", validated)
            set_or_remove("HTTPS_PROXY", validated)
        else:
            if http_proxy:
                set_or_remove("HTTP_PROXY", _validate_upstream_proxy_url(http_proxy, field="--http-proxy"))
            if https_proxy:
                set_or_remove("HTTPS_PROXY", _validate_upstream_proxy_url(https_proxy, field="--https-proxy"))
        if no_proxy is not None:
            set_or_remove("NO_PROXY", str(no_proxy).strip())

    write_env_file(
        layout.ironproxy_env_path,
        values,
        header_lines=[
            "# Managed by PacketSafari ops.",
            "# Upstream egress proxy secrets and corporate proxy settings.",
            "# Mounted only into egress-ironproxy.",
        ],
    )

    restarted = False
    if restart:
        restart_args = type(
            "RestartArgs",
            (),
            {
                "runtime_root": args.runtime_root,
                "container_runtime_root": args.container_runtime_root,
                "service": "egress-ironproxy",
            },
        )()
        diagnostics_restart(restart_args)
        restarted = True

    return {
        "ok": True,
        "envPath": str(layout.ironproxy_env_path),
        "changed": bool(changed_keys),
        "changedKeys": changed_keys,
        "cleared": clear,
        "restartRequested": restart,
        "restarted": restarted,
        "proxy": {
            "HTTP_PROXY": values.get("HTTP_PROXY", ""),
            "HTTPS_PROXY": values.get("HTTPS_PROXY", ""),
            "NO_PROXY": values.get("NO_PROXY", ""),
        },
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


def _compose_env_file_args(layout: RuntimeLayout) -> list[str]:
    args = ["--env-file", str(layout.runtime_env_path)]
    if layout.runtime_sizing_env_path.exists():
        args.extend(["--env-file", str(layout.runtime_sizing_env_path)])
    return args


def _compose_base_command(layout: RuntimeLayout) -> list[str]:
    return [
        "docker",
        "compose",
        *_compose_env_file_args(layout),
        *_compose_file_args(layout),
        *_compose_logging_args(layout.runtime_env_path),
    ]


ECR_REGISTRY_RE = re.compile(r"^\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$")


def _image_registry(image: str) -> str:
    first = str(image or "").split("/", 1)[0].strip()
    if "." in first or ":" in first or first == "localhost":
        return first
    return ""


def _rendered_compose_images(layout: RuntimeLayout) -> set[str]:
    if layout.kind == "local-data-root" or not layout.compose_file.exists():
        return set()
    try:
        result = subprocess.run(
            [*_compose_base_command(layout), "config", "--images"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        pass

    images: set[str] = set()
    for path in (layout.compose_file, layout.compose_sizing_file):
        if not path.exists():
            continue
        for match in re.finditer(r"^\s*image:\s*['\"]?([^'\"\s#]+)", path.read_text(encoding="utf-8"), re.MULTILINE):
            images.add(match.group(1).strip())
    return images


def _run_text(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _format_bytes(value: object) -> str:
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if size < 1000 or unit == units[-1]:
            break
        size /= 1000
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def _docker_image_id(ref: str) -> str:
    raw = str(ref or "").strip()
    if not raw:
        return ""
    result = _run_text(["docker", "image", "inspect", raw, "--format", "{{.Id}}"])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _docker_container_image_ids() -> set[str]:
    ps = _run_text(["docker", "ps", "-q"])
    if ps.returncode != 0:
        return set()
    container_ids = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
    if not container_ids:
        return set()
    inspect = _run_text(["docker", "inspect", "--format", "{{.Image}}", *container_ids])
    if inspect.returncode != 0:
        return set()
    return {line.strip() for line in inspect.stdout.splitlines() if line.strip()}


def _image_refs_from_manifest(manifest: dict) -> set[str]:
    refs: set[str] = set()
    for value in _manifest_images(manifest).values():
        if isinstance(value, dict):
            ref = str(value.get("image") or "").strip()
        else:
            ref = str(value or "").strip()
        if ref:
            refs.add(ref)
    return refs


def _active_image_refs(layout: RuntimeLayout) -> set[str]:
    refs = set(_rendered_compose_images(layout))
    refs.update(_image_refs_from_manifest(_read_json(layout.release_manifest_path, {})))
    return refs


def _record_current_image_set(layout: RuntimeLayout, manifest: dict) -> dict[str, object]:
    refs = _image_refs_from_manifest(manifest) or _active_image_refs(layout)
    images: dict[str, dict[str, str]] = {}
    service_refs = _manifest_service_image_refs(manifest)
    if service_refs:
        for service, ref in service_refs.items():
            if not ref:
                continue
            image_id = _docker_image_id(ref)
            images[service] = {"ref": ref, "id": image_id}
    else:
        for index, ref in enumerate(sorted(refs)):
            image_id = _docker_image_id(ref)
            images[f"image_{index + 1}"] = {"ref": ref, "id": image_id}

    entry = {
        "version": str(manifest.get("version") or ""),
        "recordedAt": utc_now(),
        "images": images,
    }
    state = _read_json(layout.deployment_state_path, {})
    retention = state.setdefault("imageRetention", {})
    history = retention.setdefault("history", [])
    if not isinstance(history, list):
        history = []
    version_value = str(entry.get("version") or "")
    history = [
        item
        for item in history
        if not (isinstance(item, dict) and version_value and str(item.get("version") or "") == version_value)
    ]
    history.append(entry)
    retention["history"] = history[-12:]
    retention["updatedAt"] = utc_now()
    _write_json(layout.deployment_state_path, state)
    return entry


def _protected_image_ids(layout: RuntimeLayout, *, keep_deployments: int) -> tuple[set[str], int]:
    protected: set[str] = set(_docker_container_image_ids())
    for ref in _active_image_refs(layout):
        image_id = _docker_image_id(ref)
        if image_id:
            protected.add(image_id)

    state = _read_json(layout.deployment_state_path, {})
    history = ((state.get("imageRetention") or {}).get("history") or []) if isinstance(state, dict) else []
    if not isinstance(history, list):
        history = []
    retained_history = [item for item in history if isinstance(item, dict)][-max(0, int(keep_deployments)) :]
    for item in retained_history:
        images = item.get("images") if isinstance(item.get("images"), dict) else {}
        for image in images.values():
            if isinstance(image, dict):
                image_id = str(image.get("id") or "").strip()
                if image_id:
                    protected.add(image_id)
    return protected, len([item for item in history if isinstance(item, dict)])


def _parse_docker_size(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)$", raw, flags=re.IGNORECASE)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}.get(unit, 1)
    return int(amount * multiplier)


def _dangling_docker_images() -> list[dict[str, object]]:
    result = _run_text(["docker", "image", "ls", "--no-trunc", "--filter", "dangling=true", "--format", "{{json .}}"])
    if result.returncode != 0:
        return []
    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        image_id = str(parsed.get("ID") or "").strip()
        if not image_id:
            continue
        rows.append(
            {
                "id": image_id,
                "repository": str(parsed.get("Repository") or ""),
                "tag": str(parsed.get("Tag") or ""),
                "createdAt": str(parsed.get("CreatedAt") or ""),
                "createdSince": str(parsed.get("CreatedSince") or ""),
                "size": str(parsed.get("Size") or ""),
                "sizeBytes": _parse_docker_size(parsed.get("Size")),
            }
        )
    return rows


def docker_image_retention_health(layout: RuntimeLayout, *, keep_deployments: int = DEFAULT_IMAGE_RETENTION_KEEP_DEPLOYMENTS) -> dict[str, object]:
    if shutil.which("docker") is None:
        return {"ok": True, "skipped": True, "reason": "docker_missing", "candidates": []}
    keep = max(0, int(keep_deployments))
    protected, recorded_deployments = _protected_image_ids(layout, keep_deployments=keep)
    dangling = _dangling_docker_images()
    candidates = [row for row in dangling if str(row.get("id") or "") not in protected]
    total_bytes = sum(int(row.get("sizeBytes") or 0) for row in candidates)
    safe_to_prune = recorded_deployments >= keep + 1
    return {
        "ok": not candidates,
        "danglingCount": len(dangling),
        "candidateCount": len(candidates),
        "candidateBytes": total_bytes,
        "candidateSize": _format_bytes(total_bytes),
        "keepDeployments": keep,
        "recordedDeployments": recorded_deployments,
        "safeToPrune": safe_to_prune,
        "message": (
            f"{len(candidates)} old dangling Docker images can be removed while keeping the current plus last {keep} recorded deployments."
            if candidates and safe_to_prune
            else (
                f"{len(candidates)} dangling Docker images were found, but packetsafari-ops has only {recorded_deployments} recorded deployment image sets; recording more upgrades before automatic pruning is safer."
                if candidates
                else "No old dangling Docker images need cleanup."
            )
        ),
        "candidateIds": [str(row.get("id") or "") for row in candidates if str(row.get("id") or "")],
        "candidates": candidates[:50],
    }


def prune_old_docker_images(layout: RuntimeLayout, *, keep_deployments: int = DEFAULT_IMAGE_RETENTION_KEEP_DEPLOYMENTS) -> dict[str, object]:
    health = docker_image_retention_health(layout, keep_deployments=keep_deployments)
    if not health.get("candidateCount"):
        return {"status": "noop", **health}
    if not health.get("safeToPrune"):
        return {"status": "blocked", **health}
    ids = [str(value or "") for value in health.get("candidateIds", []) if str(value or "")]
    if not ids:
        return {"status": "noop", **health}
    result = _run_text(["docker", "image", "rm", *ids])
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        **health,
        "removedIds": ids,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def maybe_offer_docker_image_prune(args, layout: RuntimeLayout) -> dict[str, object]:
    if bool(getattr(args, "skip_image_retention_check", False)):
        return {"skipped": True, "reason": "disabled"}
    keep = max(0, int(getattr(args, "image_retention_keep", DEFAULT_IMAGE_RETENTION_KEEP_DEPLOYMENTS) or 0))
    health = docker_image_retention_health(layout, keep_deployments=keep)
    if bool(getattr(args, "prune_old_images", False)):
        return prune_old_docker_images(layout, keep_deployments=keep)
    if not health.get("candidateCount"):
        return health
    if not health.get("safeToPrune"):
        print(f"PacketSafari image cleanup: {health.get('message')}", file=sys.stderr)
        return health
    print("", file=sys.stderr)
    print("PacketSafari image cleanup opportunity:", file=sys.stderr)
    print(f"  {health.get('candidateCount')} old dangling Docker images ({health.get('candidateSize')}) are outside the current + last {keep} deployment keep set.", file=sys.stderr)
    print("  These images are not used by running containers and can usually be removed after a healthy update.", file=sys.stderr)
    print("  To run without prompting next time, pass --prune-old-images; to only report, press Enter or answer no.", file=sys.stderr)
    if not sys.stdin.isatty():
        print("  Non-interactive shell detected; leaving images in place.", file=sys.stderr)
        return health
    answer = input("Remove old dangling PacketSafari images now? [y/N]: ").strip().lower()
    if answer in {"y", "yes"}:
        return prune_old_docker_images(layout, keep_deployments=keep)
    return {"status": "skipped", **health}


def healthcheck_deployment(args) -> dict[str, object]:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    profile = str(getattr(args, "profile", "") or _active_deployment_profile(layout))
    setattr(args, "profile", profile)
    doctor = doctor_deployment(args)
    image_retention = maybe_offer_docker_image_prune(args, layout)
    ok = bool(doctor.get("ok")) and not (image_retention.get("status") == "failed")
    return {
        "ok": ok,
        "profile": profile,
        "runtimeRoot": str(layout.runtime_root),
        "doctor": doctor,
        "imageRetention": image_retention,
    }


def format_healthcheck_report(payload: dict[str, object]) -> str:
    lines = [
        "PacketSafari healthcheck",
        f"Profile: {payload.get('profile')}",
        f"Runtime root: {payload.get('runtimeRoot')}",
        "",
    ]
    doctor = payload.get("doctor") if isinstance(payload.get("doctor"), dict) else {}
    checks = doctor.get("checks") if isinstance(doctor.get("checks"), list) else []
    lines.append("Checks:")
    for check in checks:
        if not isinstance(check, dict):
            continue
        marker = "ok" if check.get("ok") else "fail"
        detail = str(check.get("message") or check.get("error") or "").strip()
        suffix = f" - {detail}" if detail else ""
        lines.append(f"  [{marker}] {check.get('name')}{suffix}")
    image_retention = payload.get("imageRetention") if isinstance(payload.get("imageRetention"), dict) else {}
    lines.append("")
    lines.append("Docker image retention:")
    lines.append(f"  {image_retention.get('message') or 'No image retention data.'}")
    if image_retention.get("candidateCount"):
        lines.append(f"  Candidates: {image_retention.get('candidateCount')} ({image_retention.get('candidateSize')})")
        lines.append(f"  Keep policy: current + last {image_retention.get('keepDeployments')} recorded deployments")
        if not image_retention.get("safeToPrune"):
            lines.append("  Action: report only until enough deployment image history has been recorded.")
    lines.append("")
    lines.append(f"Overall: {'ok' if payload.get('ok') else 'needs attention'}")
    return "\n".join(lines)


def ensure_ecr_credential_helper_ready(layout: RuntimeLayout) -> None:
    ecr_registries = sorted(
        registry
        for registry in {_image_registry(image) for image in _rendered_compose_images(layout)}
        if ECR_REGISTRY_RE.match(registry)
    )
    if not ecr_registries:
        return

    docker_config_path = Path(os.getenv("DOCKER_CONFIG") or "/root/.docker") / "config.json"
    if shutil.which("docker-credential-ecr-login") is None:
        raise RuntimeError(
            "SaaS image pull requires Docker ECR authentication, but docker-credential-ecr-login is not installed. "
            "Install the amazon-ecr-credential-helper package and configure /root/.docker/config.json before retrying."
        )
    if not docker_config_path.exists():
        raise RuntimeError(
            f"SaaS image pull requires Docker ECR authentication for {', '.join(ecr_registries)}, but "
            f"{docker_config_path} is missing. Install amazon-ecr-credential-helper and configure "
            '/root/.docker/config.json with {"credsStore":"ecr-login"} or per-registry credHelpers.'
        )
    try:
        docker_config = json.loads(docker_config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SaaS image pull requires Docker ECR authentication, but {docker_config_path} is not valid JSON. "
            "Install amazon-ecr-credential-helper and fix /root/.docker/config.json before retrying."
        ) from exc

    cred_helpers = docker_config.get("credHelpers")
    if str(docker_config.get("credsStore") or "").strip() == "ecr-login":
        return
    if isinstance(cred_helpers, dict):
        missing = [registry for registry in ecr_registries if str(cred_helpers.get(registry) or "").strip() != "ecr-login"]
    else:
        missing = ecr_registries
    if missing:
        raise RuntimeError(
            "SaaS image pull requires Docker ECR authentication via amazon-ecr-credential-helper. "
            f"Configure /root/.docker/config.json with credsStore=ecr-login or credHelpers for: {', '.join(missing)}."
        )


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
    ensure_journald_retention_config()
    subprocess.run(command, check=True)


def docker_compose_pull(layout: RuntimeLayout) -> None:
    if layout.kind == "local-data-root":
        return
    subprocess.run([*_compose_base_command(layout), "pull"], check=True)


def _rendered_compose_services(layout: RuntimeLayout) -> set[str]:
    if not layout.compose_file.exists():
        return set()
    services: set[str] = set()
    in_services = False
    for raw_line in layout.compose_file.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line == "services:":
            in_services = True
            continue
        if in_services and raw_line and not raw_line.startswith(" "):
            break
        if not in_services:
            continue
        match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", raw_line)
        if match:
            services.add(match.group(1))
    return services


def _present_compose_services(layout: RuntimeLayout, services: list[str] | None) -> list[str] | None:
    if not services or layout.kind == "local-data-root":
        return services
    present = _rendered_compose_services(layout)
    if not present:
        return services
    return [service for service in services if service in present]


def docker_compose_stop(layout: RuntimeLayout, *, services: list[str] | None = None, timeout: int = 120) -> None:
    services = _present_compose_services(layout, services)
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
    try:
        subprocess.run(command, check=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        kill_command = [*_compose_base_command(layout), "kill"]
        if services:
            kill_command.extend(services)
        subprocess.run(kill_command, check=True)


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
        'PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST="redis"',
        'PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT="6379"',
        'PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB="0"',
        f"PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD={quote_env_value(redis_password)}",
        'PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST="redis"',
        'PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT="6379"',
        'PACKETSAFARI_RUNTIME_CACHE_REDIS_DB="0"',
        f"PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD={quote_env_value(redis_password)}",
        'PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB="0"',
        'PACKETSAFARI_CAPTURE_SHARKD_HOST="sharkd"',
        'PACKETSAFARI_CAPTURE_SHARKD_PORT="4448"',
        'PACKETSAFARI_CAPTURE_SHARKD_PROTOCOL="ws"',
        'PACKETSAFARI_PUBLIC_BASE_URL=""',
        'CORS_ALLOWED_ORIGINS=""',
        # Fresh on-prem installs commonly start over plain HTTP on an appliance
        # IP address. Secure cookies would not be sent by browsers in that mode.
        # Operators should set this to true when publishing PacketSafari behind
        # HTTPS.
        'PACKETSAFARI_AUTH_COOKIE_SECURE="false"',
        'NUXT_PUBLIC_API_BASE="/api/v2/"',
        'NUXT_PUBLIC_SHARKD_WS_URL=""',
        f"PACKETSAFARI_AUTH_JWT_SECRET_KEY={quote_env_value(jwt_secret)}",
        f"PACKETSAFARI_CAPTURE_SHARKD_JWT_SECRET={quote_env_value(sharkd_secret)}",
        f"SHARKD_JWT_SECRET={quote_env_value(sharkd_secret)}",
    ]
    for key, value in logging_values.items():
        lines.append(f"{key}={quote_env_value(value)}")
    lines.extend(
        [
        'PACKETSAFARI_FEATURE_SAAS_PAYWALL_ENABLED="false"',
    ]
    )
    layout.runtime_env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _release_version(manifest_path: Path) -> str:
    manifest = _read_json(manifest_path, {})
    return str((manifest or {}).get("version") or "")


def _active_deployment_profile(layout: RuntimeLayout) -> str:
    state = _read_json(layout.deployment_state_path, {})
    mode = str(((state.get("deployment") or {}).get("mode") or "")).strip().lower()
    if mode == "saas":
        return "saas"
    if (layout.secrets_dir / "saas-operator-token").exists():
        return "saas"
    active_manifest = _read_json(layout.release_manifest_path, {})
    profiles = active_manifest.get("deploymentProfiles")
    if isinstance(profiles, dict) and isinstance(profiles.get("saas"), dict):
        return "saas"
    return "onprem"


def _requested_or_active_profile(args, layout: RuntimeLayout) -> str:
    raw = str(getattr(args, "profile", "") or "").strip().lower()
    if raw:
        if raw not in DEPLOYMENT_PROFILES:
            raise RuntimeError(f"Unsupported deployment profile: {raw}")
        return raw
    return _active_deployment_profile(layout)


def _update_manifest_source(args, layout: RuntimeLayout) -> str:
    explicit = str(getattr(args, "manifest_url", "") or "").strip()
    if explicit:
        return explicit
    for key in ("PACKETSAFARI_UPDATE_MANIFEST_URL", "PACKETSAFARI_RELEASE_MANIFEST_URL"):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    active_manifest = _read_json(layout.release_manifest_path, {})
    update_value = str((active_manifest.get("update") or {}).get("manifestUrl") or "").strip()
    if update_value:
        return update_value
    profile = _requested_or_active_profile(args, layout)
    channel = str(getattr(args, "channel", "") or active_manifest.get("channel") or "stable").strip()
    platform = str(getattr(args, "platform", "") or DEFAULT_UPDATE_PLATFORM).strip()
    if profile == "saas":
        bucket = str(os.getenv("PACKETSAFARI_SAAS_UPDATE_BUCKET") or DEFAULT_SAAS_UPDATE_BUCKET).strip()
        return f"s3://{bucket}/channels/{profile}/{channel}/{platform}/release-manifest.json"
    base = str(
        os.getenv("PACKETSAFARI_UPDATE_BASE_URL")
        or os.getenv("PACKETSAFARI_RELEASE_CHANNEL_BASE_URL")
        or DEFAULT_UPDATE_BASE_URL
    ).strip()
    return f"{base.rstrip('/')}/channels/{profile}/{channel}/{platform}/release-manifest.json"


def _download_update_manifest(args, layout: RuntimeLayout) -> Path:
    source = _update_manifest_source(args, layout)
    return materialize_source(
        source,
        layout.tmp_dir / "downloads",
        "update manifest",
        args,
        default_name="release-manifest.json",
    )


def check_for_update(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    ensure_runtime_dirs(layout)
    manifest_path = _download_update_manifest(args, layout)
    return _update_check_payload(args, layout, manifest_path)


def _update_check_payload(args, layout: RuntimeLayout, manifest_path: Path) -> dict:
    manifest = _read_json(manifest_path, {})
    current = _current_release_version(layout)
    target = str(manifest.get("version") or "").strip()
    if not target:
        raise RuntimeError("Update manifest is missing version.")
    if current and current == target:
        available = False
        reason = "current"
    elif current and _version_key(target) <= _version_key(current):
        available = False
        reason = "not_newer"
    else:
        available = True
        reason = "newer"
    app = {
        "available": available,
        "reason": reason,
        "currentVersion": current,
        "targetVersion": target,
    }
    ops = tooling_update_status(manifest)
    return {
        **app,
        "channel": str(manifest.get("channel") or ""),
        "profile": _requested_or_active_profile(args, layout),
        "manifest": str(manifest_path),
        "source": _update_manifest_source(args, layout),
        "backupMode": resolve_backup_mode(args, profile=_requested_or_active_profile(args, layout)),
        "app": app,
        "ops": ops,
        "tooling": ops,
    }


def apply_update(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    ensure_runtime_dirs(layout)
    manifest_path = _download_update_manifest(args, layout)
    check_payload = _update_check_payload(args, layout, manifest_path)
    manifest = _read_json(manifest_path, {})
    maybe_self_update_tooling(args, layout, manifest)
    if not check_payload["available"] and not bool(getattr(args, "force", False)):
        return {"status": "noop", **check_payload}
    setattr(args, "manifest", str(manifest_path))
    setattr(args, "bundle", None)
    if not str(getattr(args, "profile", "") or "").strip():
        setattr(args, "profile", _active_deployment_profile(layout))
    result = upgrade_release(args)
    result["imageRetention"] = maybe_offer_docker_image_prune(args, layout)
    return result


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
                raise RuntimeError("connected install requires --license.")
            manifest_arg = str(getattr(args, "manifest", "") or "").strip()
            if not manifest_arg:
                manifest_arg = str(_download_update_manifest(args, layout))
            prepare_connected_manifest(layout, manifest_arg, args, destination=layout.release_manifest_path)
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
        render_compose(layout, layout.release_manifest_path, source_root=bundle_root(), profile="onprem")
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
        "backups": [path.name for path in sorted(layout.backup_dir.glob("*"), reverse=True) if path.is_dir()][:10],
    }


def _manifest_images(manifest: dict) -> dict:
    images = manifest.get("images") or {}
    return images if isinstance(images, dict) else {}


def image_ref(images: dict, key: str, default: str = "") -> str:
    value = images.get(key, default)
    if isinstance(value, dict):
        return str(value.get("image") or default)
    return str(value or default)


UPGRADE_RECREATED_IMAGE_SERVICES = [
    "frontend",
    "backend",
    "worker",
    "sharkd",
    "egress-firewall",
    "egress-ironproxy",
    "egress-dns",
]


def _manifest_service_image_refs(manifest: dict) -> dict[str, str]:
    images = _manifest_images(manifest)
    backend_image = image_ref(images, "backend")
    return {
        "frontend": image_ref(images, "frontend"),
        "backend": backend_image,
        "worker": image_ref(images, "worker", backend_image),
        "sharkd": image_ref(images, "sharkd"),
        "egress-firewall": image_ref(images, "egress-firewall"),
        "egress-ironproxy": image_ref(images, "egress-ironproxy"),
        "egress-dns": image_ref(
            images,
            "egress-dns",
            "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e",
        ),
    }


def _services_with_changed_images(active_manifest: dict, target_manifest: dict) -> list[str]:
    active_images = _manifest_service_image_refs(active_manifest)
    target_images = _manifest_service_image_refs(target_manifest)
    changed: list[str] = []
    for service in UPGRADE_RECREATED_IMAGE_SERVICES:
        target_ref = str(target_images.get(service) or "").strip()
        active_ref = str(active_images.get(service) or "").strip()
        if target_ref and target_ref != active_ref:
            changed.append(service)
    return changed


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
        ["sh", "-c", "tar -C /storage --exclude=./onprem -cpf /backup/storage.tar ."],
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
            "find /storage -mindepth 1 -maxdepth 1 ! -name onprem -exec rm -rf -- {} + && tar -C /storage --exclude=./onprem -xpf /backup/storage.tar",
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
        docker_compose_stop(layout, services=["frontend", "backend", "worker", "sharkd", "egress-firewall", "egress-ironproxy", "egress-dns"], timeout=120)
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
    return f"packetsafari/{service}:{safe_version}"


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
        maybe_self_update_tooling(args, layout, manifest, bundle_dir=bundle_dir)
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
        required = [
            service
            for service in (
                "frontend",
                "backend",
                "worker",
                "redis",
                "postgres",
                "sharkd",
                "egress-dns",
                "egress-ironproxy",
                "egress-firewall",
            )
            if service in images or service != "worker"
        ]
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
        ["sh", "-c", "PACKETSAFARI_SKIP_SERVICE_INIT=true python3 /app/scripts/sql_storage_upgrade.py upgrade"],
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


def _http_probe(url: str, *, timeout: int = 8) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(65536)
            content_type = response.headers.get("content-type", "")
            payload: object | None = None
            if "json" in content_type.lower():
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    payload = None
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "contentType": content_type,
                "payload": payload,
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": None, "error": str(exc)}


def _frontend_probe_base(local_api_base: str, public_base_url: str) -> str:
    if public_base_url:
        return public_base_url.rstrip("/")
    parsed = urllib.parse.urlparse(local_api_base)
    if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 8080:
        return urllib.parse.urlunparse((parsed.scheme or "http", f"{parsed.hostname}:3000", "", "", "", "")).rstrip("/")
    return local_api_base.rstrip("/")


def _compose_service_status(layout: RuntimeLayout) -> dict[str, object]:
    if not layout.compose_file.exists():
        return {"ok": False, "error": "compose_file_missing"}
    command = [*_compose_base_command(layout), "ps", "--format", "json"]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or result.stdout.strip() or f"docker compose ps exited {result.returncode}"}
    rows: list[dict[str, object]] = []
    raw_output = result.stdout.strip()
    try:
        parsed_output = json.loads(raw_output) if raw_output else None
        if isinstance(parsed_output, list):
            rows.extend([row for row in parsed_output if isinstance(row, dict)])
        elif isinstance(parsed_output, dict):
            rows.append(parsed_output)
    except Exception:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    unhealthy = [
        row.get("Service") or row.get("Name")
        for row in rows
        if str(row.get("State") or row.get("Status") or "").lower() not in {"running", "healthy"}
        and "running" not in str(row.get("State") or row.get("Status") or "").lower()
    ]
    return {"ok": not unhealthy and bool(rows), "services": rows, "unhealthy": unhealthy}


def _backend_sharkd_probe(layout: RuntimeLayout) -> dict[str, object]:
    if not layout.compose_file.exists():
        return {"ok": False, "error": "compose_file_missing"}
    probe = """
import json
import sys

try:
    from packetsafari.common.connect import get_sharkcdm

    raw = get_sharkcdm().dispatch("info")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    print(json.dumps({
        "ok": True,
        "responseKeys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
    sys.exit(1)
""".strip()
    command = [*_compose_base_command(layout), "exec", "-T", "backend", "python3", "-c", probe]
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe_timeout"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    payload: dict[str, object] = {}
    raw_output = result.stdout.strip()
    if raw_output:
        try:
            parsed = json.loads(raw_output.splitlines()[-1])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {"stdout": raw_output}
    if result.returncode != 0:
        return {
            **payload,
            "ok": False,
            "error": str(payload.get("error") or result.stderr.strip() or raw_output or f"probe exited {result.returncode}"),
        }
    return {**payload, "ok": bool(payload.get("ok", True))}


def doctor_deployment(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    profile = deployment_profile(args)
    manifest_arg = str(getattr(args, "manifest", "") or "").strip()
    manifest = _read_json(Path(manifest_arg), {}) if manifest_arg else _read_json(layout.release_manifest_path, {})
    runtime_env = _effective_required_env_values(layout) if layout.runtime_env_path.exists() else {}
    checks: list[dict[str, object]] = []

    def add_check(name: str, check_ok: bool, **details: object) -> None:
        details.pop("ok", None)
        checks.append({"name": name, "ok": bool(check_ok), **details})

    try:
        required = _merged_required_env_keys(manifest, profile=profile)
        missing = [key for key in required if not _required_env_value_is_valid(key, str(runtime_env.get(key, "")))]
        add_check("required_env", not missing, required=required, missing=missing)
    except Exception as exc:
        add_check("required_env", False, error=str(exc))

    public_base_url = str(runtime_env.get("PACKETSAFARI_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    cookie_secure = str(runtime_env.get("PACKETSAFARI_AUTH_COOKIE_SECURE") or "true").strip().lower() not in {"0", "false", "no", "off"}
    if profile == "saas":
        add_check("saas_public_base_url", public_base_url.startswith(("https://", "http://")), value=public_base_url)
        add_check(
            "saas_secure_cookie_scheme",
            (public_base_url.startswith("https://") and cookie_secure) or not public_base_url,
            publicBaseUrl=public_base_url,
            cookieSecure=cookie_secure,
            message="SaaS should use HTTPS with secure cookies. Use HTTP only on disposable development hosts.",
        )

    static_frontend = profile == "saas" and _profile_uses_static_frontend(manifest, profile)
    default_api_base = "http://127.0.0.1:8080" if static_frontend else DEFAULT_API_BASE_URL
    local_api_base = str(getattr(args, "api_base_url", "") or default_api_base).rstrip("/")
    health = _http_probe(f"{local_api_base}/api/v2/health")
    add_check("backend_health", bool(health.get("ok")), **health)

    config_info = _http_probe(f"{local_api_base}/api/v2/config/info")
    add_check("backend_config_info", bool(config_info.get("ok")), **config_info)

    if static_frontend:
        add_check(
            "frontend_runtime_config",
            True,
            skipped=True,
            reason="static_frontend_profile",
            publicBaseUrl=public_base_url,
            message="Static frontend is validated after S3/CloudFront publishing.",
        )
    else:
        frontend_base = _frontend_probe_base(local_api_base, public_base_url)
        runtime_config = _http_probe(f"{frontend_base}/runtime-config.json")
        add_check("frontend_runtime_config", bool(runtime_config.get("ok")), **runtime_config)

    compose = _compose_service_status(layout)
    add_check("compose_services", bool(compose.get("ok")), **compose)

    sharkd = _backend_sharkd_probe(layout)
    add_check("backend_sharkd", bool(sharkd.get("ok")), **sharkd)

    ok = all(bool(check.get("ok")) for check in checks)
    return {
        "ok": ok,
        "profile": profile,
        "runtimeRoot": str(layout.runtime_root),
        "manifest": manifest_arg or str(layout.release_manifest_path),
        "checks": checks,
    }


def assert_doctor_ok(args) -> dict:
    payload = doctor_deployment(args)
    if not payload.get("ok"):
        failed = [str(check.get("name")) for check in payload.get("checks", []) if not check.get("ok")]
        raise RuntimeError(f"Deployment readiness checks failed: {', '.join(failed)}")
    return payload


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
    image_set = _record_current_image_set(layout, manifest)
    write_helper_status(layout, status="ok", message=f"Upgrade to {deployment['installedVersion']} applied.")
    return {
        "message": "Upgrade applied.",
        "version": deployment["installedVersion"],
        "source": source,
        "profile": profile,
        "backupMode": backup_mode,
        "snapshot": str(snapshot_dir),
        "imageSet": image_set,
    }


def record_upgrade_rollback_state(layout: RuntimeLayout, *, phase: str, mode: str, snapshot_dir: Path) -> None:
    state = _read_json(layout.deployment_state_path, {})
    state["lastAction"] = {
        "type": "upgrade",
        "status": "rolled_back",
        "message": f"Upgrade failed during {phase}; restored previous release with {mode}.",
        "updatedAt": utc_now(),
    }
    rollback = state.setdefault("rollback", {})
    rollback["latestSnapshot"] = str(snapshot_dir)
    rollback["rollbackMode"] = "restore"
    rollback["lastFailurePhase"] = phase
    rollback["lastRestoreMode"] = mode
    _write_json(layout.deployment_state_path, state)


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
                    manifest_arg = str(_download_update_manifest(args, layout))
                target_manifest_path = prepare_connected_manifest(layout, manifest_arg, args)
            setattr(args, "_doctor_manifest_path", str(target_manifest_path))
            maybe_fail_upgrade_simulation(layout, args, "preflight")

            manifest = _read_json(target_manifest_path, {})
            maybe_self_update_tooling(args, layout, manifest)
            validate_tooling_requirement(manifest)
            validate_upgrade_path(layout, manifest)
            if profile == "onprem":
                verify_license_allows_release(layout, manifest)
            else:
                verify_saas_operator_authorization(layout, args, manifest)
            validate_required_env(layout, manifest, profile=profile)
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
            services_to_recreate: list[str] = []
            had_active_compose = layout.compose_file.exists()
            if had_active_compose:
                services_to_recreate = _services_with_changed_images(_read_json(layout.release_manifest_path, {}), manifest)
            snapshot_dir = snapshot_runtime(layout)

            phase = "compose"
            render_compose(layout, target_manifest_path, profile=profile)
            render_logging_config(layout)
            maybe_fail_upgrade_simulation(layout, args, "compose")
            if source == "manifest" and not bool(getattr(args, "skip_image_pull", False)):
                write_helper_status(layout, status="upgrading", message="Pulling target release images before stopping running services.")
                if profile == "saas":
                    ensure_ecr_credential_helper_ready(layout)
                docker_compose_pull(layout)
            elif source == "manifest":
                write_helper_status(layout, status="upgrading", message="Skipping image pull; using images already present on this host.")

            if had_active_compose:
                if services_to_recreate:
                    write_helper_status(
                        layout,
                        status="upgrading",
                        message=(
                            f"Stopping services with changed images ({', '.join(services_to_recreate)}) and "
                            f"{backup_message}."
                        ),
                    )
                    docker_compose_stop(layout, services=services_to_recreate, timeout=120)
                else:
                    write_helper_status(
                        layout,
                        status="upgrading",
                        message=f"No running service images changed; leaving app services up and {backup_message}.",
                    )
            else:
                write_helper_status(layout, status="upgrading", message=f"No active Compose file found; treating this as a fresh deployment and {backup_message}.")
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

            phase = "migration"
            write_helper_status(layout, status="upgrading", message="Running target database migrations.")
            run_target_migrations(layout)
            maybe_fail_upgrade_simulation(layout, args, "migration")

            phase = "healthcheck"
            write_helper_status(layout, status="upgrading", message="Starting target release and running health checks.")
            docker_compose_up(layout, pull_policy="never")
            maybe_fail_upgrade_simulation(layout, args, "healthcheck")
            if not bool(getattr(args, "skip_health_check", False)):
                wait_for_health(timeout_seconds=int(getattr(args, "health_timeout", 180) or 180))
                if profile == "saas":
                    assert_doctor_ok(args)

            phase = "promote"
            maybe_fail_upgrade_simulation(layout, args, "promote")
            return _promote_release(layout, manifest, snapshot_dir, source=source, profile=profile, backup_mode=backup_mode)
        except Exception as exc:
            write_helper_status(layout, status="failed", message=f"Upgrade failed during {phase}: {exc}")
            if snapshot_dir is not None:
                data_restore_required = phase in {"migration", "healthcheck", "promote"}
                restore_data = backup_mode == "inline" and data_restore_required
                mode = "full data restore" if restore_data else "metadata restore"
                try:
                    restore_snapshot(layout, snapshot_dir, restore_data=restore_data)
                    record_upgrade_rollback_state(layout, phase=phase, mode=mode, snapshot_dir=snapshot_dir)
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
