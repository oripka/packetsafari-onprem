from __future__ import annotations

import curses
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .api import ApiError, LocalApiClient, detect_api_base_url
from .operations import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CONTAINER_RUNTIME_ROOT,
    check_for_update,
    configuration_overview,
    configure_required_env,
    configure_upstream_proxy,
    detect_runtime_root,
    docker_image_retention_health,
    doctor_deployment,
    format_healthcheck_report,
    operate_security_content,
    runtime_layout,
    set_password,
    show_initial_admin_command,
    show_runtime_env,
    status,
    supports_onprem_host_actions,
)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"

SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "credential",
)


def _read_json_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sanitize_source(value: object) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        source = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return source


def _sanitize_text_urls(value: object) -> str:
    return re.sub(r"https?://[^\s]+", lambda match: _sanitize_source(match.group(0)), str(value or ""))


def _redact_payload(value: object, *, key: str = "") -> object:
    normalized = key.lower().replace("-", "_")
    if key and any(part in normalized for part in SENSITIVE_KEY_PARTS):
        if value is None or value == "" or value == [] or value == {}:
            return value
        return "<redacted>"
    if isinstance(value, str) and "proxy" in normalized and value:
        return "<configured>"
    if isinstance(value, str) and (normalized in {"source", "manifest", "pack"} or normalized.endswith("url")):
        return _sanitize_source(value)
    if isinstance(value, dict):
        return {str(item_key): _redact_payload(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


@dataclass(slots=True)
class MenuContext:
    runtime_root: str
    container_runtime_root: str
    api_base_url: str
    local_status: dict = field(default_factory=dict)
    active_manifest: dict = field(default_factory=dict)
    update_status: dict | None = None
    health_status: dict | None = None
    update_check_state: str = "not_checked"
    health_check_state: str = "not_checked"
    check_generation: int = 0
    notice: str = "Loading local deployment state…"

    @property
    def layout(self):
        return runtime_layout(self.runtime_root, self.container_runtime_root)

    @property
    def deployment(self) -> dict:
        state = self.local_status.get("state")
        if not isinstance(state, dict):
            return {}
        deployment = state.get("deployment")
        return deployment if isinstance(deployment, dict) else {}

    @property
    def profile(self) -> str:
        mode = str(self.deployment.get("mode") or "").strip().lower()
        if mode == "saas":
            return "saas"
        profiles = self.active_manifest.get("deploymentProfiles")
        if isinstance(profiles, dict) and "saas" in profiles and "onprem" not in profiles:
            return "saas"
        return "onprem"

    @property
    def installed_version(self) -> str:
        return str(self.deployment.get("installedVersion") or self.active_manifest.get("version") or "not installed")

    @property
    def ops_version(self) -> str:
        return str(self.local_status.get("installerVersion") or "unknown")

    @property
    def channel(self) -> str:
        return str(self.active_manifest.get("channel") or "stable")

    @property
    def platform(self) -> str:
        return str(self.active_manifest.get("platform") or "linux-arm64")

    def refresh_local(self) -> None:
        self.local_status = status(self.layout)
        self.active_manifest = _read_json_file(self.layout.release_manifest_path)


@dataclass(slots=True)
class MenuItem:
    name: str
    description: str
    action: Callable[[MenuContext], None] | None = None
    submenu_factory: Callable[[MenuContext], list["MenuItem"]] | None = None
    danger: bool = False


def _pause(message: str = "Press Enter to return") -> None:
    input(f"\n{DIM}{message}...{RESET}")


def _clear() -> None:
    print("\033[2J\033[H", end="", flush=True)


def _divider(width: int = 78) -> str:
    return f"{DIM}{'─' * width}{RESET}"


def _terminal_header(title: str, ctx: MenuContext) -> None:
    print()
    print(f"{CYAN}{BOLD}PacketSafari Operations{RESET} {DIM}›{RESET} {BOLD}{title}{RESET}")
    print(
        f"{DIM}Host:{RESET} {ctx.profile} · {ctx.channel} · {ctx.platform}   "
        f"{DIM}App:{RESET} {ctx.installed_version}   {DIM}Ops:{RESET} {ctx.ops_version}"
    )
    print(_divider())


def _print_error(title: str, exc: Exception | str, ctx: MenuContext) -> None:
    _clear()
    _terminal_header(title, ctx)
    print(f"{RED}{BOLD}Action failed{RESET}")
    print()
    print(str(exc))
    _pause()


def _print_payload(title: str, payload: object, ctx: MenuContext, *, pause: bool = True) -> None:
    _clear()
    _terminal_header(title, ctx)
    print(json.dumps(_redact_payload(payload), indent=2))
    if pause:
        _pause()


def _read_json_multiline() -> dict:
    print(f"{DIM}Paste JSON. End with a single line containing only '.'{RESET}")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)
    payload = json.loads("\n".join(lines).strip() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("The onboarding draft must be a JSON object.")
    return payload


def _confirm(prompt: str, *, phrase: str | None = None) -> bool:
    if phrase:
        entered = input(f"{prompt}\n\nType {phrase} to continue: ").strip()
        return entered == phrase
    return input(f"{prompt} [y/N]: ").strip().lower() in {"y", "yes"}


def _operation_args(ctx: MenuContext, **overrides: object) -> Namespace:
    values: dict[str, object] = {
        "runtime_root": ctx.runtime_root,
        "container_runtime_root": ctx.container_runtime_root,
        "api_base_url": ctx.api_base_url,
        "profile": ctx.profile,
        "channel": ctx.channel,
        "platform": ctx.platform,
        "manifest": None,
        "manifest_url": None,
        "manifest_signature": None,
        "backup_mode": None,
        "backup_proof": None,
        "max_backup_age_minutes": 180,
        "saas_operator_token": None,
        "allow_unbacked_upgrade": False,
        "download_header": [],
        "download_bearer_token": None,
        "download_basic": None,
        "download_timeout": 300,
        "allow_insecure_download": False,
        "image_retention_keep": 2,
        "prune_old_images": False,
        "skip_image_retention_check": True,
    }
    values.update(overrides)
    return Namespace(**values)


def _cli_command(ctx: MenuContext, *arguments: str) -> list[str]:
    cli_path = Path(__file__).with_name("cli.py")
    return [
        sys.executable,
        str(cli_path),
        "--runtime-root",
        ctx.runtime_root,
        "--container-runtime-root",
        ctx.container_runtime_root,
        *arguments,
    ]


def _format_command_result(payload: dict) -> list[str]:
    update_summary = payload.get("updateSummary")
    if isinstance(update_summary, dict):
        previous_app = str(update_summary.get("previousApplicationVersion") or "unknown")
        installed_app = str(update_summary.get("installedApplicationVersion") or payload.get("version") or "unknown")
        previous_ops = str(update_summary.get("previousOpsVersion") or "unknown")
        installed_ops = str(update_summary.get("installedOpsVersion") or "unknown")
        changed_services = update_summary.get("changedServices")
        changed_label = ", ".join(str(item) for item in changed_services) if isinstance(changed_services, list) and changed_services else "none"
        reloaded_services = update_summary.get("configurationReloadedServices")
        reloaded_label = ", ".join(str(item) for item in reloaded_services) if isinstance(reloaded_services, list) and reloaded_services else "none"
        backup_retention = payload.get("backupRetention") if isinstance(payload.get("backupRetention"), dict) else {}
        backup_cleanup = str(backup_retention.get("status") or "not run")
        if int(backup_retention.get("removedCount") or 0):
            backup_cleanup += f"; removed {int(backup_retention['removedCount'])} old full backup(s)"
        return [
            f"Application    {previous_app} → {installed_app}",
            f"Ops tool       {previous_ops} → {installed_ops}",
            f"Services       {changed_label}",
            f"Config reloads {reloaded_label}",
            f"Verification   {update_summary.get('verification') or 'unknown'}",
            f"Rollback       {update_summary.get('rollback') or 'unknown'}",
            f"Backup cleanup {backup_cleanup}",
        ]
    lines: list[str] = []
    status_value = payload.get("status")
    if status_value:
        lines.append(f"Status: {status_value}")
    version = payload.get("version") or payload.get("targetVersion")
    if version:
        lines.append(f"Application version: {version}")
    if payload.get("profile"):
        lines.append(f"Profile: {payload['profile']}")
    if payload.get("backupMode"):
        lines.append(f"Backup mode: {payload['backupMode']}")
    if payload.get("message"):
        lines.append(str(payload["message"]))
    image_retention = payload.get("imageRetention")
    if isinstance(image_retention, dict) and image_retention.get("message"):
        lines.extend(["", f"Image retention: {image_retention['message']}"])
    return lines or [json.dumps(_redact_payload(payload), indent=2)]


def _run_cli_action(
    ctx: MenuContext,
    title: str,
    arguments: list[str],
    *,
    restart_ui: bool = False,
) -> bool:
    previous_application = ctx.installed_version
    previous_ops = ctx.ops_version
    managed_arguments = list(arguments)
    if managed_arguments and managed_arguments[0] == "update" and "--json" not in managed_arguments:
        managed_arguments.append("--json")
    _clear()
    _terminal_header(title, ctx)
    print(f"{CYAN}Starting managed operation…{RESET}")
    print(f"{DIM}The existing packetsafari-ops transaction and safety checks remain authoritative.{RESET}\n")
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(
        mode="w+t", encoding="utf-8"
    ) as stderr_file:
        process = subprocess.Popen(
            _cli_command(ctx, *managed_arguments),
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        last_message = ""
        while process.poll() is None:
            helper = _read_json_file(ctx.layout.helper_status_path)
            message = str(helper.get("message") or "").strip()
            if message and message != last_message:
                print(f"  {CYAN}›{RESET} {message}", flush=True)
                last_message = message
            time.sleep(0.35)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    ctx.refresh_local()
    success = process.returncode == 0
    is_update = bool(arguments and arguments[0] in {"update", "upgrade"})
    print()
    if success:
        success_title = "UPDATE SUCCEEDED" if is_update else "OPERATION SUCCEEDED"
        print(f"{GREEN}{BOLD}{success_title}{RESET}")
        try:
            parsed = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            parsed = None
        if is_update and isinstance(parsed, dict) and not isinstance(parsed.get("updateSummary"), dict):
            try:
                installed_ops = (ctx.layout.tooling_root / "VERSION").read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                installed_ops = ctx.ops_version
            rollback_state = (ctx.local_status.get("state") or {}).get("rollback") or {}
            changed_services = (ctx.update_status or {}).get("changedServices") or []
            parsed["updateSummary"] = {
                "previousApplicationVersion": previous_application,
                "installedApplicationVersion": ctx.installed_version,
                "previousOpsVersion": previous_ops,
                "installedOpsVersion": installed_ops,
                "changedServices": changed_services,
                "configurationReloadedServices": parsed.get("configurationReloadedServices") or [],
                "verification": "not needed" if parsed.get("status") == "noop" else "passed",
                "rollback": rollback_state.get("note") or rollback_state.get("rollbackMode") or "unchanged",
            }
        if isinstance(parsed, dict):
            for line in _format_command_result(parsed):
                print(line)
        elif stdout.strip():
            print(stdout.strip())
        if stderr.strip():
            print(f"\n{DIM}Operation notes:{RESET}")
            print(stderr.strip())
        ctx.notice = f"{title} completed successfully."
    else:
        failure_title = "UPDATE FAILED" if is_update else "OPERATION FAILED"
        print(f"{RED}{BOLD}{failure_title}{RESET}")
        output = stderr.strip() or stdout.strip() or f"Command exited with status {process.returncode}."
        if is_update:
            output_lines = [line.strip() for line in output.splitlines() if line.strip()]
            reason = output_lines[-1] if output_lines else output
            if ": " in reason and ("Error:" in reason or "Exception:" in reason):
                reason = reason.split(": ", 1)[1]
            reason = _sanitize_text_urls(reason)
            print(f"Reason: {reason}")
            print("The command exited without reporting successful release promotion.")
        else:
            print(output)
        helper = ctx.local_status.get("helper") if isinstance(ctx.local_status.get("helper"), dict) else {}
        helper_message = str(helper.get("message") or "").strip()
        if helper_message and helper_message not in output:
            print(f"\nLatest recovery state: {helper_message}")
        ctx.notice = f"{title} failed; inspect Recent activity for recovery state."
    if success and restart_ui:
        _pause("Press Enter to reload the operator UI")
        os.execv(sys.executable, _cli_command(ctx))
    _pause()
    return success


def _update_review(payload: dict, ctx: MenuContext, *, title: str = "Update Review") -> None:
    _clear()
    _terminal_header(title, ctx)
    app = payload.get("app") if isinstance(payload.get("app"), dict) else payload
    ops = payload.get("ops") if isinstance(payload.get("ops"), dict) else {}
    available = bool(app.get("available"))
    marker = f"{GREEN}AVAILABLE{RESET}" if available else f"{DIM}CURRENT{RESET}"
    print(f"Application       {app.get('currentVersion') or 'not installed'} → {app.get('targetVersion') or 'unknown'}  {marker}")
    print(
        f"Ops tooling      {ops.get('currentVersion') or ctx.ops_version} → "
        f"{ops.get('targetVersion') or ops.get('requiredVersion') or 'current'}"
        f"  {GREEN + 'AVAILABLE' + RESET if ops.get('available') else ''}"
    )
    print(
        f"Deployment       {payload.get('profile') or ctx.profile} · "
        f"{payload.get('channel') or ctx.channel} · {payload.get('platform') or ctx.platform}"
    )
    changed_services = payload.get("changedServices")
    if isinstance(changed_services, list):
        changed_label = ", ".join(str(item) for item in changed_services) if changed_services else "no image changes detected"
    else:
        changed_label = "resolved during the managed update"
    print(f"Changed services {changed_label}")
    print(f"Backup policy    {payload.get('backupMode') or 'profile default'}")
    backup_label, _backup_attr, backup_ready = _backup_status(ctx)
    if payload.get("backupMode") == "require-recent" and backup_ready is False:
        print(f"{RED}Backup readiness {backup_label}{RESET}")
    else:
        print(f"Backup readiness {backup_label}")
    source = _sanitize_source(payload.get("source"))
    if source:
        print(f"Release source   {source}")
    signature = payload.get("releaseSignature") if isinstance(payload.get("releaseSignature"), dict) else {}
    if signature.get("status") == "verified":
        print(f"Release signature verified · {signature.get('algorithm') or 'signed manifest'}")
    sizing = payload.get("sizingStatus")
    if isinstance(sizing, dict) and sizing.get("stale"):
        print(f"{YELLOW}Sizing warning    {sizing.get('message') or 'Host sizing should be refreshed.'}{RESET}")
    print()
    print(str(ops.get("message") or "Release manifest tooling requirements checked."))


def _check_update(ctx: MenuContext, *, pause: bool = True) -> dict | None:
    if ctx.update_check_state == "checking":
        _clear()
        _terminal_header("Check Release Channel", ctx)
        print("The automatic signed release-channel check is still running in the background.")
        _pause()
        return None
    _clear()
    _terminal_header("Check Release Channel", ctx)
    print("Resolving and verifying the configured release manifest…")
    try:
        ctx.update_check_state = "checking"
        payload = check_for_update(_operation_args(ctx))
    except Exception as exc:
        ctx.update_check_state = "failed"
        ctx.notice = f"Release check failed: {exc}"
        _print_error("Check Release Channel", exc, ctx)
        return None
    ctx.update_status = payload
    ctx.update_check_state = "complete"
    app = payload.get("app") if isinstance(payload.get("app"), dict) else payload
    ops = payload.get("ops") if isinstance(payload.get("ops"), dict) else {}
    if app.get("available"):
        ctx.notice = f"Update available: {app.get('currentVersion') or 'not installed'} → {app.get('targetVersion')}"
    elif ops.get("available"):
        ctx.notice = f"Ops-tool update available: {ops.get('currentVersion')} → {ops.get('targetVersion')}"
    else:
        ctx.notice = f"Release channel checked: {app.get('targetVersion') or 'current version'} is current."
    _update_review(payload, ctx)
    if pause:
        _pause()
    return payload


def _apply_connected_update(ctx: MenuContext, *, backup_mode: str | None = None, unbacked: bool = False) -> None:
    payload = ctx.update_status or _check_update(ctx, pause=False)
    if not payload:
        return
    selected_backup_mode = "skip" if unbacked else (backup_mode or payload.get("backupMode"))
    review_payload = {**payload, "backupMode": selected_backup_mode}
    _update_review(review_payload, ctx, title="Confirm Update")
    app = payload.get("app") if isinstance(payload.get("app"), dict) else payload
    ops = payload.get("ops") if isinstance(payload.get("ops"), dict) else {}
    if not app.get("available") and not ops.get("available"):
        print(f"\n{YELLOW}The selected channel does not contain a newer application or ops-tool release.{RESET}")
        _pause()
        return
    target = str(app.get("targetVersion") or "target release")
    _backup_label, _backup_attr, backup_ready = _backup_status(ctx)
    if selected_backup_mode == "require-recent" and backup_ready is False:
        print(f"\n{RED}A fresh, restore-verified external backup is required before this update can run.{RESET}")
        print("Create or verify the backup proof, or deliberately choose the inline-backup workflow.")
        _pause()
        return
    if unbacked:
        print()
        print(f"{RED}{BOLD}No PacketSafari PostgreSQL or /storage backup will be captured.{RESET}")
        print("If migrations run, data rollback may require a separately managed external backup.")
        if not _confirm("Continue without a PacketSafari backup?", phrase="UNBACKED"):
            return
    elif not _confirm(f"Apply {target} using {selected_backup_mode or 'the profile default'} backup policy?"):
        return
    arguments = ["update"]
    checked_manifest = str(payload.get("manifest") or "").strip()
    if checked_manifest:
        arguments.extend(["--manifest-url", checked_manifest])
    if backup_mode:
        arguments.extend(["--backup-mode", backup_mode])
    if unbacked:
        arguments.extend(["--backup-mode", "skip", "--allow-unbacked-upgrade"])
    _run_cli_action(ctx, f"Update to {target}", arguments, restart_ui=True)


def _apply_recommended_update(ctx: MenuContext) -> None:
    _apply_connected_update(ctx)


def _apply_inline_update(ctx: MenuContext) -> None:
    _apply_connected_update(ctx, backup_mode="inline")


def _apply_unbacked_update(ctx: MenuContext) -> None:
    _apply_connected_update(ctx, unbacked=True)


def _offline_bundle_update(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Signed Offline Bundle", ctx)
    bundle = input("Bundle path (.tar.zst or split .part-* file): ").strip()
    if not bundle:
        return
    print()
    print(f"Profile          {ctx.profile}")
    print(f"Bundle           {bundle}")
    print(f"Backup policy    {'inline' if ctx.profile == 'onprem' else 'require-recent'}")
    print("The bundle signature, checksums, entitlement, upgrade path and required configuration will be verified.")
    if not _confirm("Verify and apply this bundle?"):
        return
    _run_cli_action(
        ctx,
        "Offline Bundle Upgrade",
        ["upgrade", "--profile", ctx.profile, "--bundle", bundle],
        restart_ui=True,
    )


def _rollback(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Rollback", ctx)
    state = ctx.local_status.get("state") if isinstance(ctx.local_status.get("state"), dict) else {}
    rollback = state.get("rollback") if isinstance(state.get("rollback"), dict) else {}
    mode = str(rollback.get("rollbackMode") or "unknown")
    print(f"Latest snapshot  {rollback.get('latestSnapshot') or 'not recorded in deployment state'}")
    print(f"Restore mode     {mode}")
    if mode == "external-data-restore":
        print(f"{YELLOW}Automatic rollback restores runtime metadata only; PostgreSQL and /storage require external restore.{RESET}")
    else:
        print("The latest recorded deployment snapshot will be restored before services restart.")
    if not _confirm("Restore the latest snapshot?", phrase="ROLLBACK"):
        return
    _run_cli_action(ctx, "Rollback", ["rollback", "--profile", ctx.profile])


def _show_deployment_details(ctx: MenuContext) -> None:
    ctx.refresh_local()
    state = ctx.local_status.get("state") if isinstance(ctx.local_status.get("state"), dict) else {}
    payload = {
        "deployment": state.get("deployment") or {},
        "lastAction": state.get("lastAction") or {},
        "rollback": state.get("rollback") or {},
        "helper": ctx.local_status.get("helper") or {},
        "sizingStatus": ctx.local_status.get("sizingStatus") or {},
        "backups": ctx.local_status.get("backups") or [],
        "runtimeRoot": ctx.runtime_root,
        "composeFiles": ctx.local_status.get("composeFiles") or [],
    }
    _print_payload("Deployment Details", payload, ctx)


def _show_activity(ctx: MenuContext) -> None:
    ctx.refresh_local()
    state = ctx.local_status.get("state") if isinstance(ctx.local_status.get("state"), dict) else {}
    payload = {
        "lastAction": state.get("lastAction") or {},
        "helper": ctx.local_status.get("helper") or {},
        "rollback": state.get("rollback") or {},
    }
    _print_payload("Recent Activity", payload, ctx)


def _refresh_local(ctx: MenuContext) -> None:
    ctx.refresh_local()
    ctx.notice = "Local deployment state refreshed."
    _show_deployment_details(ctx)


def _run_healthcheck(ctx: MenuContext) -> None:
    if ctx.health_check_state == "checking":
        _clear()
        _terminal_header("Healthcheck", ctx)
        print("The automatic deployment-health check is still running in the background.")
        _pause()
        return
    _clear()
    _terminal_header("Healthcheck", ctx)
    print("Checking product readiness, services, gateway, sharkd, intelligence updates and image retention…")
    try:
        ctx.health_check_state = "checking"
        doctor = doctor_deployment(_operation_args(ctx))
        image_retention = docker_image_retention_health(ctx.layout, keep_deployments=2)
        payload = {
            "ok": bool(doctor.get("ok")),
            "profile": ctx.profile,
            "runtimeRoot": ctx.runtime_root,
            "doctor": doctor,
            "imageRetention": image_retention,
        }
    except Exception as exc:
        ctx.health_check_state = "failed"
        _print_error("Healthcheck", exc, ctx)
        return
    ctx.health_status = payload
    ctx.health_check_state = "complete"
    ctx.notice = "Healthcheck passed." if payload.get("ok") else "Healthcheck needs attention."
    _clear()
    _terminal_header("Healthcheck", ctx)
    print(format_healthcheck_report(payload))
    _pause()


def _run_doctor(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Deployment Readiness", ctx)
    print("Running deterministic deployment readiness checks…")
    try:
        payload = doctor_deployment(_operation_args(ctx))
    except Exception as exc:
        _print_error("Deployment Readiness", exc, ctx)
        return
    _print_payload("Deployment Readiness", payload, ctx)


def _backend_logs(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Recent Logs", ctx)
    container = input("Container [packetsafari-backend]: ").strip() or "packetsafari-backend"
    since = input("Since [15m]: ").strip() or "15m"
    tail = input("Maximum lines [200]: ").strip() or "200"
    _run_cli_action(ctx, "Recent Logs", ["diagnostics", "logs", "--container", container, "--since", since, "--tail", tail])


def _restart_service(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Restart Service", ctx)
    service = input("Compose service name: ").strip()
    if not service or not _confirm(f"Restart {service}?"):
        return
    _run_cli_action(ctx, f"Restart {service}", ["diagnostics", "restart", "--service", service])


def _restart_stack(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Restart Full Stack", ctx)
    if not _confirm("Restart every service in the active PacketSafari stack?"):
        return
    _run_cli_action(ctx, "Restart Full Stack", ["diagnostics", "restart"])


def _tune_preview(ctx: MenuContext) -> None:
    _run_cli_action(ctx, "Generate Host Sizing", ["tune", "--profile", "auto"])


def _tune_apply(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Apply Host Sizing", ctx)
    print("This regenerates sizing settings from current CPU, memory and storage, then recreates the stack.")
    if not _confirm("Apply automatic host sizing now?"):
        return
    _run_cli_action(ctx, "Apply Host Sizing", ["tune", "--profile", "auto", "--apply"])


def _prune_images(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("PacketSafari Image Cleanup", ctx)
    print("Only unprotected PacketSafari images outside the current plus last two recorded deployments are considered.")
    if not _confirm("Run the managed image-retention cleanup?"):
        return
    _run_cli_action(ctx, "PacketSafari Image Cleanup", ["healthcheck", "--profile", ctx.profile, "--prune-old-images"])


def _show_config_status(ctx: MenuContext) -> None:
    text = show_runtime_env(ctx.layout)
    entries: list[dict[str, object]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        entries.append({"name": key.strip(), "configured": bool(value.strip())})
    _print_payload(
        "Configuration Status",
        {
            "runtimeEnv": str(ctx.layout.runtime_env_path),
            "values": entries,
            "note": "Values are intentionally hidden. Run deployment readiness to validate required configuration.",
        },
        ctx,
    )


def _page_lines(title: str, lines: list[str], ctx: MenuContext) -> None:
    page_size = max(8, shutil.get_terminal_size((100, 30)).lines - 9)
    page_count = max(1, (len(lines) + page_size - 1) // page_size)
    page = 0
    while True:
        _clear()
        _terminal_header(title, ctx)
        start = page * page_size
        print("\n".join(lines[start : start + page_size]))
        if page_count == 1:
            _pause()
            return
        prompt = f"\n{DIM}Page {page + 1}/{page_count} · Enter/n next · p previous · q return:{RESET} "
        choice = input(prompt).strip().lower()
        if choice in {"q", "quit"}:
            return
        if choice in {"p", "prev", "previous"}:
            page = max(0, page - 1)
            continue
        if page >= page_count - 1:
            return
        page += 1


def _show_configuration_overview(ctx: MenuContext) -> None:
    payload = configuration_overview(ctx.layout)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    catalog = payload.get("catalog") if isinstance(payload.get("catalog"), dict) else {}
    egress = payload.get("egress") if isinstance(payload.get("egress"), dict) else {}
    inventory = payload.get("inventory") if isinstance(payload.get("inventory"), list) else []
    feature_flags = [item for item in inventory if str(item.get("type") or "").lower() == "bool"]
    enabled_flags = [item for item in feature_flags if str(item.get("value") or "").strip().lower() in {"1", "true", "yes", "on"}]
    disabled_flags = [item for item in feature_flags if str(item.get("value") or "").strip().lower() in {"0", "false", "no", "off"}]
    secret_items = [item for item in inventory if item.get("secret")]
    configured_secrets = [item for item in secret_items if item.get("state") == "configured"]
    lines = [
        f"{BOLD}Deployment identity{RESET}",
        f"  Ops profile          {payload.get('profile') or ctx.profile}",
        f"  Application mode     {payload.get('deploymentMode') or 'unset'}",
        f"  Configuration catalog {catalog.get('status') or 'partial'}"
        + (f" · v{catalog.get('version')}" if catalog.get("version") is not None else ""),
        f"  Variables            {summary.get('total', 0)} available · "
        f"{summary.get('configured', 0)} configured · {summary.get('defaulted', 0)} defaulted · "
        f"{summary.get('unset', 0)} unset",
        f"  Feature flags        {len(enabled_flags)} enabled · {len(disabled_flags)} disabled · "
        f"{len(feature_flags) - len(enabled_flags) - len(disabled_flags)} unset",
        f"  Secrets              {len(configured_secrets)} configured · values always hidden",
    ]
    missing_required = int(summary.get("missingRequired") or 0)
    if missing_required:
        lines.append(f"  {RED}{BOLD}Required missing      {missing_required}{RESET}")
    if catalog.get("message"):
        lines.append(f"  {DIM}{catalog['message']}{RESET}")
    if enabled_flags:
        lines.extend(["", f"{BOLD}Enabled feature flags{RESET}"])
        lines.extend(f"  {GREEN}✓{RESET} {item.get('key')}" for item in enabled_flags)

    destinations = egress.get("destinations") if isinstance(egress.get("destinations"), list) else []
    proxy_state = "configured" if egress.get("ironProxyConfigured") else "missing"
    upstream_state = "configured" if egress.get("upstreamProxyConfigured") else "not configured · IronProxy connects directly"
    lines.extend(
        [
            "",
            f"{BOLD}Egress and IronProxy{RESET}",
            f"  IronProxy            {proxy_state}",
            f"  Upstream proxy       {upstream_state}",
            f"  Proxy secrets        {egress.get('secretVariablesConfigured', 0)} configured · values hidden",
            f"  Allowlist             {len(destinations)} destinations · "
            f"monitor mode {'enabled' if egress.get('monitorMode') else 'disabled'}",
        ]
    )
    service_modes = egress.get("serviceModes") if isinstance(egress.get("serviceModes"), dict) else {}
    for service, settings in sorted(service_modes.items()):
        mode = settings.get("egress_mode") if isinstance(settings, dict) else settings
        lines.append(f"    {service:<20} {mode or 'unspecified'}")

    lines.extend(["", f"{BOLD}Allowed destinations{RESET}"])
    if destinations:
        current_purpose = ""
        for item in destinations:
            purpose = str(item.get("purpose") or "Other")
            if purpose != current_purpose:
                lines.append(f"  {CYAN}{purpose}{RESET}")
                current_purpose = purpose
            port = f":{item.get('port')}" if item.get("port") else ""
            managed = " · host-approved" if item.get("managed") else ""
            lines.append(f"    {item.get('target')}{port} · {item.get('classification')}{managed}")
    else:
        lines.append(f"  {YELLOW}No installed egress destinations were found.{RESET}")

    lines.extend(["", f"{BOLD}Complete environment inventory{RESET}"])
    current_domain = ""
    for item in inventory:
        domain = str(item.get("domain") or "other")
        if domain != current_domain:
            lines.extend(["", f"{CYAN}{domain.upper()}{RESET}"])
            current_domain = domain
        state = str(item.get("state") or "unset")
        if state == "missing-required":
            marker = f"{RED}! REQUIRED{RESET}"
        elif state == "configured":
            marker = f"{GREEN}✓ SET{RESET}"
        elif state == "defaulted":
            marker = f"{DIM}• DEFAULT{RESET}"
        else:
            marker = f"{YELLOW}○ UNSET{RESET}"
        source = f" · {item.get('source')}" if item.get("source") else ""
        secret = " · secret" if item.get("secret") else ""
        lines.append(f"  {marker:<23} {item.get('key')} = {item.get('value')}{source}{secret}")
        description = textwrap.shorten(str(item.get("description") or ""), width=100, placeholder="…")
        if description:
            lines.append(f"      {DIM}{description}{RESET}")
    _page_lines("Configuration Overview", lines, ctx)


def _prompt_required_config(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Required Configuration", ctx)
    try:
        payload = configure_required_env(
            _operation_args(
                ctx,
                action="prompt-env",
                output=None,
                manifest=str(ctx.layout.release_manifest_path),
            )
        )
    except Exception as exc:
        _print_error("Required Configuration", exc, ctx)
        return
    _print_payload("Required Configuration", payload, ctx)


def _configure_proxy(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Corporate Upstream Proxy", ctx)
    proxy_url = input("Proxy URL (for HTTP and HTTPS): ").strip()
    if not proxy_url:
        return
    no_proxy = input("NO_PROXY value [leave current/default]: ").strip()
    if not _confirm("Save the proxy configuration and restart egress-ironproxy?"):
        return
    try:
        payload = configure_upstream_proxy(
            _operation_args(
                ctx,
                proxy_url=proxy_url,
                http_proxy=None,
                https_proxy=None,
                no_proxy=no_proxy or None,
                clear=False,
                restart=True,
            )
        )
    except Exception as exc:
        _print_error("Corporate Upstream Proxy", exc, ctx)
        return
    _print_payload(
        "Corporate Upstream Proxy",
        {"status": payload.get("status") or "ok", "message": "Corporate upstream proxy configured; values are hidden."},
        ctx,
    )


def _clear_proxy(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Clear Corporate Proxy", ctx)
    if not _confirm("Remove HTTP_PROXY, HTTPS_PROXY and NO_PROXY, then restart egress-ironproxy?"):
        return
    try:
        payload = configure_upstream_proxy(
            _operation_args(
                ctx,
                proxy_url=None,
                http_proxy=None,
                https_proxy=None,
                no_proxy=None,
                clear=True,
                restart=True,
            )
        )
    except Exception as exc:
        _print_error("Clear Corporate Proxy", exc, ctx)
        return
    _print_payload(
        "Clear Corporate Proxy",
        {"status": payload.get("status") or "ok", "message": "Managed upstream proxy values were removed."},
        ctx,
    )


def _content_status(ctx: MenuContext) -> None:
    try:
        payload = operate_security_content(
            _operation_args(ctx, action="status", pack=None, public_key=None, allow_downgrade=False)
        )
    except Exception as exc:
        _print_error("Security Content Status", exc, ctx)
        return
    _print_payload("Security Content Status", payload, ctx)


def _content_pack_action(ctx: MenuContext, action: str) -> None:
    _clear()
    labels = {"check": "Check Content Pack", "apply": "Apply Content Pack", "import": "Import Offline Content Pack"}
    title = labels[action]
    _terminal_header(title, ctx)
    pack = input("Signed content pack path or authenticated URL: ").strip()
    if not pack:
        return
    if action in {"apply", "import"} and not _confirm("Verify and activate this security-content pack?"):
        return
    try:
        payload = operate_security_content(
            _operation_args(ctx, action=action, pack=pack, public_key=None, allow_downgrade=False)
        )
    except Exception as exc:
        _print_error(title, exc, ctx)
        return
    _print_payload(title, payload, ctx)


def _content_check(ctx: MenuContext) -> None:
    _content_pack_action(ctx, "check")


def _content_apply(ctx: MenuContext) -> None:
    _content_pack_action(ctx, "apply")


def _content_import(ctx: MenuContext) -> None:
    _content_pack_action(ctx, "import")


def _content_rollback(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Rollback Security Content", ctx)
    if not _confirm("Reactivate the previous signed security-content version?", phrase="ROLLBACK"):
        return
    try:
        payload = operate_security_content(
            _operation_args(ctx, action="rollback", pack=None, public_key=None, allow_downgrade=False)
        )
    except Exception as exc:
        _print_error("Rollback Security Content", exc, ctx)
        return
    _print_payload("Rollback Security Content", payload, ctx)


def _onboarding_client(ctx: MenuContext) -> LocalApiClient:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    return client


def _show_onboarding_schema(ctx: MenuContext) -> None:
    try:
        payload = _onboarding_client(ctx).onboarding_schema().get("data") or {}
    except ApiError as exc:
        _print_error("Onboarding Schema", exc, ctx)
        return
    _print_payload("Onboarding Schema", payload, ctx)


def _onboarding_draft_action(ctx: MenuContext, action: str) -> None:
    title = {"validate": "Validate Onboarding Draft", "save": "Save Onboarding Draft", "finalize": "Finalize Onboarding"}[action]
    _clear()
    _terminal_header(title, ctx)
    try:
        values = _read_json_multiline()
        if action == "finalize" and not _confirm("Finalize onboarding and write managed runtime settings?", phrase="FINALIZE"):
            return
        client = _onboarding_client(ctx)
        if action == "validate":
            payload = client.onboarding_validate(values)
        elif action == "save":
            payload = client.onboarding_save_draft(values)
        else:
            payload = client.onboarding_finalize(values)
    except (ApiError, json.JSONDecodeError, ValueError) as exc:
        _print_error(title, exc, ctx)
        return
    _print_payload(title, payload, ctx)


def _validate_onboarding(ctx: MenuContext) -> None:
    _onboarding_draft_action(ctx, "validate")


def _save_onboarding(ctx: MenuContext) -> None:
    _onboarding_draft_action(ctx, "save")


def _finalize_onboarding(ctx: MenuContext) -> None:
    _onboarding_draft_action(ctx, "finalize")


def _show_initial_admin(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Initial Administrator", ctx)
    email = input("Initial admin email [admin@example.com]: ").strip() or "admin@example.com"
    print()
    print(show_initial_admin_command(email=email))
    _pause()


def _change_password(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Change Administrator Password", ctx)
    username = input("Username: ").strip()
    if not username:
        return
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        _print_error("Change Administrator Password", "Passwords do not match.", ctx)
        return
    try:
        payload = set_password(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
                username=username,
                password=password,
            )
        )
    except Exception as exc:
        _print_error("Change Administrator Password", exc, ctx)
        return
    _print_payload("Change Administrator Password", payload, ctx)


def _change_runtime_root(ctx: MenuContext) -> None:
    _clear()
    _terminal_header("Runtime Root", ctx)
    default = detect_runtime_root()
    entered = input(f"Runtime root [{default}]: ").strip()
    ctx.runtime_root = detect_runtime_root(entered or default)
    ctx.update_status = None
    ctx.health_status = None
    ctx.refresh_local()
    _start_background_checks(ctx)


def _overview_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Refresh local state", "Reload deployment, helper, sizing, backup and active-manifest state.", action=_refresh_local),
        MenuItem("Deployment details", "Inspect installed version, last action, rollback mode, sizing and managed paths.", action=_show_deployment_details),
        MenuItem("Recent activity", "Inspect the latest operation, helper phase and rollback outcome.", action=_show_activity),
        MenuItem("Check release channel", "Explicitly resolve the configured channel and compare application and ops-tool versions.", action=_check_update),
        MenuItem("Run healthcheck", "Check product readiness, services, gateway, sharkd, intelligence state and image retention.", action=_run_healthcheck),
    ]


def _update_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Check release channel", "No-mutation comparison of installed and available application and ops-tool versions.", action=_check_update),
        MenuItem("Apply recommended update", "Use the active profile's safe backup policy and the configured connected release channel.", action=_apply_recommended_update),
        MenuItem("Update with inline backup", "Capture PacketSafari PostgreSQL and /storage locally before applying the release.", action=_apply_inline_update),
        MenuItem("Update without backup", "Advanced: explicitly acknowledge an unbacked container-only or disposable-host update.", action=_apply_unbacked_update, danger=True),
        MenuItem("Upgrade from signed bundle", "Verify and apply an offline .tar.zst bundle or split .part-* bundle.", action=_offline_bundle_update),
        MenuItem("Rollback latest snapshot", "Restore according to the latest recorded full, metadata-only or external-data rollback mode.", action=_rollback, danger=True),
    ]


def _health_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Run healthcheck", "Readiness checks plus safe PacketSafari image-retention guidance.", action=_run_healthcheck),
        MenuItem("Run deployment doctor", "Inspect required configuration, HTTP readiness, Compose, gateway, sharkd and intelligence checks.", action=_run_doctor),
        MenuItem("View recent logs", "Read bounded recent logs from a selected PacketSafari container.", action=_backend_logs),
        MenuItem("Restart one service", "Restart a selected Compose service.", action=_restart_service),
        MenuItem("Restart full stack", "Restart every service in the detected PacketSafari deployment.", action=_restart_stack, danger=True),
        MenuItem("Generate host sizing", "Refresh deterministic sizing files without recreating containers.", action=_tune_preview),
        MenuItem("Apply host sizing", "Refresh sizing and recreate the stack with the generated resource limits.", action=_tune_apply, danger=True),
        MenuItem("Clean old images", "Remove only safe, unprotected PacketSafari images outside the managed keep set.", action=_prune_images, danger=True),
    ]


def _configuration_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Configuration overview", "Deployment mode, features, configured/defaulted/unset variables, masked secrets and effective egress.", action=_show_configuration_overview),
        MenuItem("Configuration status", "Show configured key names and presence without printing secret values.", action=_show_config_status),
        MenuItem("Complete required configuration", "Prompt for missing manifest-required values using the canonical config workflow.", action=_prompt_required_config),
        MenuItem("Run deployment readiness", "Validate required values and the running deployment without revealing secrets.", action=_run_doctor),
        MenuItem("Configure upstream proxy", "Set the corporate HTTP/HTTPS proxy and restart egress-ironproxy.", action=_configure_proxy),
        MenuItem("Clear upstream proxy", "Remove managed proxy variables and restart egress-ironproxy.", action=_clear_proxy, danger=True),
    ]


def _content_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Content status", "Show active signed content, feed schedules, warnings, failures and rollback availability.", action=_content_status),
        MenuItem("Check signed content pack", "Verify compatibility and authenticity without activating the pack.", action=_content_check),
        MenuItem("Apply connected content pack", "Verify and activate a signed pack from an authenticated path or URL.", action=_content_apply),
        MenuItem("Import offline content pack", "Verify and activate a signed pack transferred to an air-gapped host.", action=_content_import),
        MenuItem("Rollback security content", "Reactivate the previous signed content version independently of the application release.", action=_content_rollback, danger=True),
    ]


def _access_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Show onboarding schema", "Inspect onboarding readiness and schema with sensitive values redacted.", action=_show_onboarding_schema),
        MenuItem("Validate onboarding draft", "Validate pasted JSON without saving or finalizing it.", action=_validate_onboarding),
        MenuItem("Save onboarding draft", "Validate and persist pasted onboarding JSON.", action=_save_onboarding),
        MenuItem("Finalize onboarding", "Finalize onboarding and write managed runtime settings.", action=_finalize_onboarding, danger=True),
        MenuItem("Show initial-admin command", "Print the backend-container command for initial administrator creation.", action=_show_initial_admin),
        MenuItem("Change administrator password", "Enter a password privately and run the backend maintenance helper.", action=_change_password, danger=True),
    ]


def _main_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Overview", "Local deployment identity, versions, activity, backups and explicit release checking.", submenu_factory=_overview_items),
        MenuItem("Updates & recovery", "Connected updates, inline or external backup policies, offline bundles and rollback.", submenu_factory=_update_items),
        MenuItem("Health & services", "Product readiness, bounded logs, service restart, sizing and managed image cleanup.", submenu_factory=_health_items),
        MenuItem("Configuration", "Secret-safe configuration readiness and corporate upstream proxy controls.", submenu_factory=_configuration_items),
        MenuItem("Security content", "Signed content status, connected or offline activation, and independent rollback.", submenu_factory=_content_items),
        MenuItem("Onboarding & access", "Onboarding drafts, first-administrator guidance and private password changes.", submenu_factory=_access_items),
        MenuItem("Change runtime root", "Switch between /opt/packetsafari and a local packetsafari-data layout.", action=_change_runtime_root),
    ]


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = max(0, width - x - 1)
    try:
        stdscr.addnstr(y, x, text, available, attr)
    except curses.error:
        pass


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_BLUE, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_GREEN, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)


def _human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s old"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m old"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h old"
    return f"{hours // 24}d old"


def _backup_status(ctx: MenuContext) -> tuple[str, str, bool | None]:
    proof = _read_json_file(ctx.layout.state_dir / "latest-backup.json")
    if proof:
        provider = str(proof.get("provider") or "external")
        completed = str(proof.get("completedAt") or "").strip()
        verified = bool(proof.get("verifiedRestore"))
        age_seconds: float | None = None
        if completed:
            try:
                completed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc)).total_seconds()
            except ValueError:
                age_seconds = None
        max_age_seconds = 180 * 60
        fresh = verified and age_seconds is not None and 0 <= age_seconds <= max_age_seconds
        if fresh:
            return f"READY · {provider} · {_human_age(age_seconds)}", "good", True
        if age_seconds is not None:
            reason = _human_age(age_seconds)
        elif not completed:
            reason = "completion time missing"
        else:
            reason = "invalid completion time"
        verification = "verified but stale" if verified else "not restore-verified"
        return f"STALE · {provider} · {verification} · {reason}", "bad", False
    state = ctx.local_status.get("state") if isinstance(ctx.local_status.get("state"), dict) else {}
    rollback = state.get("rollback") if isinstance(state.get("rollback"), dict) else {}
    mode = str(rollback.get("rollbackMode") or "")
    backups = ctx.local_status.get("backups") if isinstance(ctx.local_status.get("backups"), list) else []
    if mode:
        return f"{mode} · {len(backups)} local snapshot(s)", "neutral", None
    if backups:
        return f"{len(backups)} local snapshot(s)", "neutral", None
    state = "bad" if ctx.profile == "saas" else "neutral"
    return "no recorded backup", state, False if ctx.profile == "saas" else None


def _recommended_notice(ctx: MenuContext) -> str:
    update = ctx.update_status or {}
    app = update.get("app") if isinstance(update.get("app"), dict) else update
    ops = update.get("ops") if isinstance(update.get("ops"), dict) else {}
    update_available = bool(app.get("available") or ops.get("available"))
    _backup_label, _backup_attr, backup_ready = _backup_status(ctx)
    if update_available and update.get("backupMode") == "require-recent" and backup_ready is False:
        return "Update available, but the recent external-backup requirement is not satisfied."
    if update_available and ctx.health_status is not None and not ctx.health_status.get("ok"):
        return "Update available, but deployment health needs attention before applying it."
    if update_available:
        return "Update available. Open Updates & recovery to review the exact application and ops changes."
    if ctx.update_check_state == "failed":
        return "Release-channel check failed. Open Overview to retry and inspect the error."
    if ctx.health_check_state == "failed":
        return "Automatic deployment-health check failed. Open Health & services to retry."
    if ctx.update_check_state == "checking" or ctx.health_check_state == "checking":
        return "Checking release availability and deployment health…"
    if ctx.profile != "saas" and ctx.update_check_state == "not_checked":
        return "Local health loaded. Release-channel access remains explicit for on-prem and air-gapped hosts."
    return "Application and ops tooling are current; no update action is required."


def _start_background_checks(ctx: MenuContext) -> None:
    ctx.check_generation += 1
    generation = ctx.check_generation
    profile = ctx.profile
    update_args = _operation_args(ctx)
    health_args = _operation_args(ctx)
    if profile == "saas":
        ctx.update_check_state = "checking"
    else:
        ctx.update_check_state = "not_checked"
    ctx.health_check_state = "checking"
    ctx.notice = _recommended_notice(ctx)

    def _worker() -> None:
        update_payload: dict | None = None
        update_failed = False
        if profile == "saas":
            try:
                update_payload = check_for_update(update_args)
            except Exception:
                update_failed = True
        try:
            doctor = doctor_deployment(health_args)
            health_payload: dict | None = {
                "ok": bool(doctor.get("ok")),
                "profile": profile,
                "runtimeRoot": str(getattr(health_args, "runtime_root", "")),
                "doctor": doctor,
            }
            health_failed = False
        except Exception:
            health_payload = None
            health_failed = True
        if ctx.check_generation != generation:
            return
        if profile == "saas":
            ctx.update_status = update_payload
            ctx.update_check_state = "failed" if update_failed else "complete"
        ctx.health_status = health_payload
        ctx.health_check_state = "failed" if health_failed else "complete"
        ctx.notice = _recommended_notice(ctx)

    threading.Thread(target=_worker, name="packetsafari-ops-startup-checks", daemon=True).start()


def _dashboard_lines(ctx: MenuContext) -> list[tuple[str, int]]:
    update = ctx.update_status or {}
    app = update.get("app") if isinstance(update.get("app"), dict) else update
    ops = update.get("ops") if isinstance(update.get("ops"), dict) else {}
    if ctx.update_check_state == "checking":
        target = "checking…"
        target_ops = "checking…"
        app_marker = "release channel"
        ops_marker = "release channel"
    elif ctx.update_check_state == "failed":
        target = "check failed"
        target_ops = "check failed"
        app_marker = "retry available"
        ops_marker = "retry available"
    elif ctx.update_status:
        target = str(app.get("targetVersion") or "unknown")
        target_ops = str(ops.get("targetVersion") or ops.get("requiredVersion") or ctx.ops_version)
        app_marker = "update available" if app.get("available") else "current"
        ops_marker = "update available" if ops.get("available") else "current"
    else:
        target = "manual check"
        target_ops = "manual check"
        app_marker = "offline-safe"
        ops_marker = "offline-safe"
    health_label = "checking…" if ctx.health_check_state == "checking" else "not checked"
    health_attr = curses.A_DIM
    if ctx.health_status is not None:
        if ctx.health_status.get("ok"):
            health_label = "healthy"
            health_attr = curses.color_pair(5) | curses.A_BOLD
        else:
            health_label = "needs attention"
            health_attr = curses.color_pair(4) | curses.A_BOLD
    elif ctx.health_check_state == "failed":
        health_label = "check failed"
        health_attr = curses.color_pair(4) | curses.A_BOLD
    backup_label, backup_state, _backup_ready = _backup_status(ctx)
    backup_attr = {
        "good": curses.color_pair(5) | curses.A_BOLD,
        "bad": curses.color_pair(4) | curses.A_BOLD,
    }.get(backup_state, curses.A_DIM)
    ops_attr = curses.color_pair(5) | curses.A_BOLD if ops.get("available") else 0
    return [
        (f"Host        {ctx.profile} · {ctx.channel} · {ctx.platform} · {ctx.runtime_root}", curses.A_BOLD),
        (f"Application {ctx.installed_version} → {target}  [{app_marker}]", curses.color_pair(5) if app.get("available") else 0),
        (f"Ops tool    {ctx.ops_version} → {target_ops}  [{ops_marker}]", ops_attr),
        (f"Health      {health_label}", health_attr),
        (f"Backup      {backup_label}", backup_attr),
    ]


def _render_menu(
    stdscr,
    parts: list[str],
    items: list[MenuItem],
    ctx: MenuContext,
    selected: int,
    *,
    allow_back: bool,
    allow_quit: bool,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _safe_addstr(stdscr, 1, 2, "PacketSafari Operations", curses.color_pair(1) | curses.A_BOLD)
    _safe_addstr(stdscr, 2, 2, " › ".join(parts), curses.A_BOLD)
    _safe_addstr(stdscr, 3, 2, "─" * max(10, width - 4), curses.A_DIM)

    row = 4
    if len(parts) == 1:
        for line, attr in _dashboard_lines(ctx):
            _safe_addstr(stdscr, row, 3, line, attr)
            row += 1
        _safe_addstr(stdscr, row, 3, ctx.notice, curses.A_DIM)
        row += 1
        _safe_addstr(stdscr, row, 2, "─" * max(10, width - 4), curses.A_DIM)
        row += 1
    else:
        _safe_addstr(stdscr, row, 3, f"{ctx.profile} · {ctx.installed_version} · ops {ctx.ops_version}", curses.A_DIM)
        row += 1
        _safe_addstr(stdscr, row, 2, "─" * max(10, width - 4), curses.A_DIM)
        row += 1

    footer_row = max(row + 1, height - 3)
    available_rows = max(1, footer_row - row)
    item_height = 3
    visible_count = max(1, available_rows // item_height)
    start = min(max(0, selected - visible_count + 1), max(0, len(items) - visible_count))
    end = min(len(items), start + visible_count)

    for index in range(start, end):
        item = items[index]
        is_selected = index == selected
        if is_selected:
            attr = curses.color_pair(6) | curses.A_BOLD
        elif item.danger:
            attr = curses.color_pair(4) | curses.A_BOLD
        else:
            attr = curses.A_BOLD
        suffix = "  ›" if item.submenu_factory else ""
        _safe_addstr(stdscr, row, 2, f" {'>' if is_selected else ' '} {item.name}{suffix}", attr)
        row += 1
        description = textwrap.shorten(item.description, width=max(20, width - 8), placeholder="…")
        _safe_addstr(stdscr, row, 6, description, curses.color_pair(6) if is_selected else curses.A_DIM)
        row += 2

    if start > 0:
        _safe_addstr(stdscr, 4, width - 12, "↑ more", curses.A_DIM)
    if end < len(items):
        _safe_addstr(stdscr, footer_row - 1, width - 12, "↓ more", curses.A_DIM)

    footer = "[↑/↓] Move   [Enter] Open"
    if allow_back:
        footer += "   [Esc] Back"
    if allow_quit:
        footer += "   [q] Quit"
    _safe_addstr(stdscr, height - 2, 2, footer, curses.A_DIM)
    stdscr.refresh()


def _run_action_outside_curses(stdscr, action: Callable[[MenuContext], None], ctx: MenuContext) -> None:
    curses.def_prog_mode()
    curses.endwin()
    try:
        action(ctx)
    except Exception as exc:
        _print_error("Action Error", exc, ctx)
    finally:
        curses.reset_prog_mode()
        stdscr.refresh()


def _walk_menu(
    stdscr,
    parts: list[str],
    items: list[MenuItem],
    ctx: MenuContext,
    *,
    allow_back: bool,
    allow_quit: bool = False,
) -> bool:
    selected = 0
    while True:
        _render_menu(stdscr, parts, items, ctx, selected, allow_back=allow_back, allow_quit=allow_quit)
        key = stdscr.getch()
        if key in {curses.KEY_UP, ord("k")}:
            selected = (selected - 1) % len(items)
            continue
        if key in {curses.KEY_DOWN, ord("j")}:
            selected = (selected + 1) % len(items)
            continue
        if key in {27, curses.KEY_LEFT} and allow_back:
            return False
        if key in {ord("q"), ord("Q")} and allow_quit:
            return True
        if key not in {10, 13, curses.KEY_ENTER, curses.KEY_RIGHT}:
            continue
        item = items[selected]
        if item.submenu_factory:
            should_quit = _walk_menu(
                stdscr,
                parts + [item.name],
                item.submenu_factory(ctx),
                ctx,
                allow_back=True,
            )
            if should_quit:
                return True
            continue
        if item.action:
            _run_action_outside_curses(stdscr, item.action, ctx)


def run_menu(
    runtime_root: str | None = None,
    container_runtime_root: str = DEFAULT_CONTAINER_RUNTIME_ROOT,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> None:
    ctx = MenuContext(
        runtime_root=detect_runtime_root(runtime_root),
        container_runtime_root=container_runtime_root,
        api_base_url=api_base_url,
    )
    ctx.refresh_local()
    if not supports_onprem_host_actions(ctx.layout):
        ctx.notice = "Local development layout detected. Host mutations remain guarded by packetsafari-ops."
    else:
        _start_background_checks(ctx)

    def _main(stdscr) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(250)
        _init_colors()
        _walk_menu(stdscr, ["Operator cockpit"], _main_items(ctx), ctx, allow_back=False, allow_quit=True)

    curses.wrapper(_main)
