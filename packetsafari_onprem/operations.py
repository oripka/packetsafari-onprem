from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .envfile import parse_env_file, quote_env_value

DEFAULT_RUNTIME_ROOT = "/opt/packetsafari"
DEFAULT_CONTAINER_RUNTIME_ROOT = "/storage/onprem"
DEFAULT_API_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_DATA_ROOT = str(Path.home() / "packetsafari-data")

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
    def helper_status_path(self) -> Path:
        return self.state_dir / "helper-status.json"

    @property
    def compose_file(self) -> Path:
        return self.compose_dir / "docker-compose.onprem.yml"

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
        layout.logging_dir,
        layout.tooling_root,
        layout.bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def supports_onprem_host_actions(layout: RuntimeLayout) -> bool:
    return layout.kind == "onprem-runtime-root"


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


def _compose_logging_args(runtime_env_path: Path) -> list[str]:
    env_values = parse_env_file(runtime_env_path)
    if str(env_values.get("AUDIT_FORWARDING_MODE") or "").strip().lower() == "forwarder_profile":
        return ["--profile", "logging"]
    return []


def docker_compose_up(layout: RuntimeLayout, *, services: list[str] | None = None) -> None:
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
        if services:
            command.extend(services)
        subprocess.run(command, check=True, cwd=repo_root)
        return
    command = [
        "docker",
        "compose",
        "--env-file",
        str(layout.runtime_env_path),
        "-f",
        str(layout.compose_file),
        *_compose_logging_args(layout.runtime_env_path),
        "up",
        "-d",
    ]
    if services:
        command.extend(services)
    subprocess.run(command, check=True)


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
        "-f",
        str(layout.compose_file),
        *_compose_logging_args(layout.runtime_env_path),
        "restart",
    ]
    if services:
        command.extend(services)
    subprocess.run(command, check=True)


def docker_exec_backend(layout: RuntimeLayout, args: list[str]) -> None:
    subprocess.run(["docker", "exec", "-i", "packetsafari-backend", *args], check=True)


def write_runtime_env(layout: RuntimeLayout, logging_values: dict[str, str], *, onboarding_mode: bool) -> None:
    lines = [
        "# Managed by PacketSafari on-prem Python operations.",
        "# Finalize onboarding writes the managed runtime env, then the first admin is created manually from inside the backend container.",
        f'PACKETSAFARI_ONPREM_ENABLED="true"',
        f'PACKETSAFARI_ONPREM_ONBOARDING_ENABLED={"\"true\"" if onboarding_mode else "\"false\""}',
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
    sync_bundle(layout)

    license_path = Path(args.license).expanduser()
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    public_key_path = bundle_root() / "keys" / "license-public.pem"

    verify_license(license_path, public_key_path)

    shutil.copy2(license_path, layout.license_token_path)
    shutil.copy2(public_key_path, layout.license_public_key_path)
    shutil.copy2(manifest_path, layout.release_manifest_path)

    logging_values = resolve_logging_values(args)
    write_runtime_env(layout, logging_values, onboarding_mode=True)
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
    docker_compose_up(layout)
    write_helper_status(layout, status="ok", message="On-prem tooling installed.")
    return {
        "runtimeRoot": str(layout.runtime_root),
        "version": version(),
        "message": "PacketSafari on-prem installed in onboarding mode. Finish setup in `packetsafari-ops tui` or open /onprem/onboarding in the local UI.",
    }


def status(layout: RuntimeLayout) -> dict:
    return {
        "layoutKind": layout.kind,
        "installerVersion": version(),
        "runtimeRoot": str(layout.runtime_root),
        "state": _read_json(layout.deployment_state_path, {}),
        "helper": _read_json(layout.helper_status_path, {}),
        "runtimeEnvPath": str(layout.runtime_env_path),
        "composeFile": str(layout.compose_file),
        "backups": [path.name for path in sorted(layout.backup_dir.glob("*"), reverse=True)[:10]],
    }


def snapshot_runtime(layout: RuntimeLayout) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    layout.backup_dir.mkdir(parents=True, exist_ok=True)
    for src, prefix, suffix in (
        (layout.release_manifest_path, "release-manifest", ".json"),
        (layout.runtime_env_path, "runtime", ".env"),
        (layout.deployment_state_path, "deployment-state", ".json"),
        (layout.compose_file, "docker-compose", ".yml"),
    ):
        if src.exists():
            shutil.copy2(src, layout.backup_dir / f"{prefix}-{stamp}{suffix}")
    return stamp


def upgrade_release(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        raise RuntimeError("Upgrade is only supported for on-prem runtime roots like /opt/packetsafari, not local packetsafari-data mode.")
    ensure_runtime_dirs(layout)
    sync_bundle(layout)

    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    snapshot_runtime(layout)
    shutil.copy2(manifest_path, layout.target_release_manifest_path)
    verify_license(layout.license_token_path, layout.license_public_key_path)

    runtime_env = parse_env_file(layout.runtime_env_path)
    manifest = _read_json(layout.target_release_manifest_path, {})
    required = [str(item).strip() for item in (manifest.get("requiredEnv") or []) if str(item).strip()]
    missing = [key for key in required if not str(runtime_env.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"Target manifest requires missing runtime env keys: {', '.join(missing)}")

    render_compose(layout, layout.target_release_manifest_path)
    render_logging_config(layout)
    shutil.copy2(layout.target_release_manifest_path, layout.release_manifest_path)
    docker_compose_up(layout)

    state = _read_json(layout.deployment_state_path, {})
    state.setdefault("deployment", {})["installedVersion"] = _release_version(layout.release_manifest_path)
    state["deployment"]["mode"] = "normal"
    state["lastAction"] = {
        "type": "upgrade",
        "status": "ok",
        "message": f"Applied release {state['deployment']['installedVersion']}.",
        "updatedAt": utc_now(),
    }
    _write_json(layout.deployment_state_path, state)
    write_helper_status(layout, status="ok", message="Upgrade applied.")
    return {"message": "Upgrade applied.", "version": state["deployment"]["installedVersion"]}


def rollback_release(args) -> dict:
    layout = runtime_layout(args.runtime_root, args.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        raise RuntimeError("Rollback is only supported for on-prem runtime roots like /opt/packetsafari, not local packetsafari-data mode.")
    backups = {
        "manifest": sorted(layout.backup_dir.glob("release-manifest-*.json"), reverse=True),
        "env": sorted(layout.backup_dir.glob("runtime-*.env"), reverse=True),
        "state": sorted(layout.backup_dir.glob("deployment-state-*.json"), reverse=True),
        "compose": sorted(layout.backup_dir.glob("docker-compose-*.yml"), reverse=True),
    }
    if not all(backups.values()):
        raise RuntimeError("No rollback backup found.")

    shutil.copy2(backups["manifest"][0], layout.release_manifest_path)
    shutil.copy2(backups["env"][0], layout.runtime_env_path)
    shutil.copy2(backups["state"][0], layout.deployment_state_path)
    shutil.copy2(backups["compose"][0], layout.compose_file)
    render_logging_config(layout)
    docker_compose_up(layout)
    write_helper_status(layout, status="ok", message="Rollback restored latest snapshot.")
    return {
        "message": "Rollback restored latest snapshot.",
        "manifest": str(backups["manifest"][0]),
        "env": str(backups["env"][0]),
        "state": str(backups["state"][0]),
        "compose": str(backups["compose"][0]),
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
