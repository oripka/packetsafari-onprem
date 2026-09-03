from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from packetsafari_onprem.api import LocalApiClient, detect_api_base_url
    from packetsafari_onprem.menu import run_menu
    from packetsafari_onprem.operations import (
        DEFAULT_API_BASE_URL,
        DEFAULT_CONTAINER_RUNTIME_ROOT,
        DEFAULT_RUNTIME_ROOT,
        detect_runtime_root,
        diagnostics_logs,
        diagnostics_restart,
        doctor_deployment,
        format_healthcheck_report,
        healthcheck_deployment,
        configure_required_env,
        configure_upstream_proxy,
        operate_intelligence_egress,
        apply_update,
        check_for_update,
        configuration_overview,
        install_release,
        operate_security_content,
        rollback_release,
        runtime_layout,
        set_password,
        show_initial_admin_command,
        show_runtime_env,
        status,
        tune_runtime,
        upgrade_release,
    )
else:
    from .api import LocalApiClient, detect_api_base_url
    from .menu import run_menu
    from .operations import (
        DEFAULT_API_BASE_URL,
        DEFAULT_CONTAINER_RUNTIME_ROOT,
        DEFAULT_RUNTIME_ROOT,
        detect_runtime_root,
        diagnostics_logs,
        diagnostics_restart,
        doctor_deployment,
        format_healthcheck_report,
        healthcheck_deployment,
        configure_required_env,
        configure_upstream_proxy,
        operate_intelligence_egress,
        apply_update,
        check_for_update,
        configuration_overview,
        install_release,
        operate_security_content,
        rollback_release,
        runtime_layout,
        set_password,
        show_initial_admin_command,
        show_runtime_env,
        status,
        tune_runtime,
        upgrade_release,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="packetsafari-ops")
    parser.add_argument("--runtime-root")
    parser.add_argument("--container-runtime-root", default=DEFAULT_CONTAINER_RUNTIME_ROOT)
    subparsers = parser.add_subparsers(dest="command")

    def add_download_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--download-header",
            action="append",
            default=[],
            help="Additional HTTP download header, for example 'Authorization: Bearer ...'. May be repeated.",
        )
        command_parser.add_argument("--download-bearer-token", help="Bearer token used for HTTP/HTTPS release downloads.")
        command_parser.add_argument("--download-basic", help="Basic auth credentials as USER:PASS for HTTP/HTTPS release downloads.")
        command_parser.add_argument("--download-timeout", type=int, default=300, help="HTTP/HTTPS download timeout in seconds.")
        command_parser.add_argument(
            "--allow-insecure-download",
            action="store_true",
            help="Development only: allow HTTPS downloads with invalid certificates.",
        )

    def add_bundle_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--bundle-public-key",
            help="PacketSafari release public key for offline bundle signature verification.",
        )
        command_parser.add_argument(
            "--allow-unsigned-bundle",
            action="store_true",
            help="Development only: allow an unsigned offline bundle.",
        )

    def add_manifest_signature_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--manifest-signature",
            help="Detached manifest signature path or URL. Defaults to the manifest source plus '.sig'.",
        )

    install = subparsers.add_parser("install", help="Install PacketSafari on-prem into onboarding mode.")
    install_source = install.add_mutually_exclusive_group()
    install_source.add_argument("--manifest", help="Connected install release manifest path or URL.")
    install_source.add_argument("--bundle", help="Offline install bundle path or URL.")
    install.add_argument("--license", help="License token path or URL. Required with --manifest; optional with --bundle if bundled.")
    install.add_argument("--channel", default="stable", help="Release channel for default connected install discovery.")
    install.add_argument("--platform", default="linux-arm64", help="Release platform for default connected install discovery.")
    install.add_argument(
        "--manifest-url",
        help="Release manifest URL/path. If omitted, uses PACKETSAFARI_UPDATE_MANIFEST_URL or the default release channel.",
    )
    add_manifest_signature_arg(install)
    install.add_argument("--license-public-key", help="License public key path or URL. Defaults to the PacketSafari key bundled with the ops tool.")
    install.add_argument(
        "--allow-bundled-license-public-key",
        action="store_true",
        help="Development only: trust license-public.pem from the install bundle.",
    )
    add_bundle_args(install)
    add_download_args(install)
    install.add_argument("--non-interactive", action="store_true")
    install.add_argument("--audit-log-enabled")
    install.add_argument("--audit-log-persist")
    install.add_argument("--audit-retention-days")
    install.add_argument("--audit-forwarding-mode")
    install.add_argument("--audit-forwarder-type")
    install.add_argument(
        "--connectivity-policy",
        choices=["connected", "restricted", "airgapped"],
        default="connected",
        help="Initial runtime connectivity contract. Use airgapped before starting an offline-isolated deployment.",
    )
    install.add_argument("--size", choices=["auto", "small", "medium", "large", "none"], default="auto")

    status_parser = subparsers.add_parser("status", help="Show installer/runtime status.")
    status_parser.add_argument("--json", action="store_true")

    doctor = subparsers.add_parser("doctor", help="Run deployment readiness checks.")
    doctor.add_argument("--profile", choices=["onprem", "saas"], default="onprem")
    doctor.add_argument("--manifest", help="Release manifest to use for required env checks. Defaults to the active manifest.")
    doctor.add_argument("--api-base-url")

    healthcheck = subparsers.add_parser("healthcheck", help="Run guided health and storage hygiene checks.")
    healthcheck.add_argument("--profile", choices=["onprem", "saas"], help="Deployment profile. Defaults to the active installed profile.")
    healthcheck.add_argument("--manifest", help="Release manifest to use for required env checks. Defaults to the active manifest.")
    healthcheck.add_argument("--api-base-url")
    healthcheck.add_argument("--json", action="store_true")
    healthcheck.add_argument("--image-retention-keep", type=int, default=2, help="Recorded previous deployment image sets to keep.")
    healthcheck.add_argument("--prune-old-images", action="store_true", help="Remove unprotected PacketSafari images without prompting.")
    healthcheck.add_argument("--skip-image-retention-check", action="store_true")

    upgrade = subparsers.add_parser("upgrade", help="Apply a new release manifest or offline bundle.")
    source = upgrade.add_mutually_exclusive_group()
    source.add_argument("--manifest", help="Connected upgrade release manifest path or URL.")
    source.add_argument("--bundle", help="Air-gapped offline bundle path or URL (.tar.zst, or local split .part-* files).")
    add_bundle_args(upgrade)
    add_download_args(upgrade)
    upgrade.add_argument(
        "--profile",
        choices=["onprem", "saas"],
        default="onprem",
        help="Deployment profile. onprem verifies entitlement and takes an inline full backup by default; saas skips entitlement and requires a recent external backup proof by default.",
    )
    upgrade.add_argument(
        "--backup-mode",
        choices=["inline", "require-recent", "skip"],
        help="Backup policy. Defaults to inline for onprem and require-recent for saas.",
    )
    upgrade.add_argument("--channel", default="stable", help="Release channel for default connected upgrade discovery.")
    upgrade.add_argument("--platform", default="linux-arm64", help="Release platform for default connected upgrade discovery.")
    upgrade.add_argument(
        "--manifest-url",
        help="Release manifest URL/path. If omitted, uses PACKETSAFARI_UPDATE_MANIFEST_URL or the default release channel.",
    )
    add_manifest_signature_arg(upgrade)
    upgrade.add_argument(
        "--backup-proof",
        help="Path to the most recent external backup proof JSON/text file. Defaults to state/latest-backup.json in require-recent mode.",
    )
    upgrade.add_argument(
        "--max-backup-age-minutes",
        type=int,
        default=180,
        help="Maximum accepted age for --backup-proof in require-recent mode.",
    )
    upgrade.add_argument("--saas-operator-token", help="Internal SaaS deployment token. May also be read from the host secret file or environment.")
    upgrade.add_argument(
        "--allow-unbacked-upgrade",
        action="store_true",
        help="Allow --backup-mode skip. Intended only for container-only releases or disposable development hosts.",
    )
    upgrade.add_argument("--health-timeout", type=int, default=180)
    upgrade.add_argument("--skip-health-check", action="store_true")
    upgrade.add_argument(
        "--skip-image-pull",
        action="store_true",
        help="Do not pull images for manifest upgrades. Intended for local registry mirrors and disposable upgrade simulations where images are already present.",
    )
    upgrade.add_argument(
        "--simulate-failure-phase",
        choices=["preflight", "compose", "migration", "healthcheck", "promote"],
        help="Disposable test-host only: fail at a selected upgrade phase after optional DB/storage mutation. Requires PACKETSAFARI_ENABLE_UPGRADE_SIMULATION=true.",
    )

    rollback = subparsers.add_parser("rollback", help="Restore the latest runtime snapshot.")
    rollback.add_argument(
        "--profile",
        choices=["onprem", "saas"],
        default="onprem",
        help="Deployment profile. saas restores runtime metadata only unless the snapshot contains inline data backups.",
    )

    tune = subparsers.add_parser("tune", help="Generate host-sized runtime and compose settings.")
    tune.add_argument("--profile", choices=["auto", "small", "medium", "large", "none"], default="auto")
    tune.add_argument("--apply", action="store_true", help="Recreate the stack with the generated compose sizing override.")

    onboard = subparsers.add_parser("onboard", help="Operate on local onboarding APIs.")
    onboard.add_argument("--api-base-url")
    onboard.add_argument("action", choices=["schema", "save-draft", "validate", "finalize"])
    onboard.add_argument("--draft-json", default="{}")

    config = subparsers.add_parser("config", help="Inspect or update managed deployment config.")
    config.add_argument("action", choices=["overview", "show", "check-env", "prompt-env", "upstream-proxy"])
    config.add_argument("--manifest", help="Release manifest path or URL used to derive required env keys.")
    config.add_argument("--profile", choices=["onprem", "saas"], default="onprem")
    config.add_argument("--output", help="Env file to update for prompt-env. Defaults to the managed runtime env.")
    config.add_argument("--proxy-url", help="Corporate HTTP proxy URL to use for both HTTP_PROXY and HTTPS_PROXY.")
    config.add_argument("--http-proxy", help="Corporate HTTP proxy URL for HTTP_PROXY.")
    config.add_argument("--https-proxy", help="Corporate HTTP proxy URL for HTTPS_PROXY.")
    config.add_argument("--no-proxy", help="Comma-separated NO_PROXY value for egress-ironproxy.")
    config.add_argument("--clear", action="store_true", help="Remove HTTP_PROXY, HTTPS_PROXY, and NO_PROXY from ironproxy.env.")
    config.add_argument("--restart", action="store_true", help="Restart egress-ironproxy after writing ironproxy.env.")
    add_download_args(config)

    egress = subparsers.add_parser("egress", help="Manage purpose-scoped deployment egress approvals.")
    egress.add_argument(
        "action",
        choices=[
            "approve-intelligence-host",
            "remove-intelligence-host",
            "list-intelligence-hosts",
            "mode",
        ],
    )
    egress.add_argument(
        "egress_mode",
        nargs="?",
        choices=["allowlist", "unrestricted"],
        help="Proxy-routed HTTP(S) egress policy for `egress mode`; allowlist remains the default.",
    )
    egress.add_argument("--url", help="HTTPS feed URL or origin to approve or remove.")
    egress.add_argument("--approved-by", default="", help="Operator identity recorded with an approval.")
    egress.add_argument("--notes", default="", help="Optional approval rationale or change reference.")

    update = subparsers.add_parser("update", help="Check or apply the configured release channel update.")
    update.add_argument(
        "action",
        nargs="?",
        choices=["check", "apply"],
        default="apply",
        help="Update action. Defaults to apply so managed hosts can run `packetsafari-ops update`.",
    )
    update.add_argument(
        "--profile",
        choices=["onprem", "saas"],
        help="Deployment profile. Defaults to the active installed profile.",
    )
    update.add_argument("--channel", default="stable", help="Release channel for default update discovery.")
    update.add_argument("--platform", default="linux-arm64", help="Release platform for default update discovery.")
    update.add_argument(
        "--manifest-url",
        help="Release manifest URL/path. If omitted, uses PACKETSAFARI_UPDATE_MANIFEST_URL or the default release channel.",
    )
    add_manifest_signature_arg(update)
    update.add_argument(
        "--backup-mode",
        choices=["inline", "require-recent", "skip"],
        help="Backup policy for update apply. Defaults to inline for onprem and require-recent for saas.",
    )
    update.add_argument("--backup-proof", help="External backup proof for require-recent mode.")
    update.add_argument("--max-backup-age-minutes", type=int, default=180)
    update.add_argument("--saas-operator-token", help="Internal SaaS deployment token.")
    update.add_argument("--health-timeout", type=int, default=180)
    update.add_argument("--skip-health-check", action="store_true")
    update.add_argument("--skip-image-pull", action="store_true")
    update.add_argument("--image-retention-keep", type=int, default=2, help="Recorded previous deployment image sets to keep before offering image cleanup.")
    update.add_argument("--prune-old-images", action="store_true", help="After a successful update, remove unprotected PacketSafari images without prompting.")
    update.add_argument("--skip-image-retention-check", action="store_true")
    update.add_argument("--force", action="store_true", help="Apply even when the target version is not newer.")
    update.add_argument(
        "--json",
        action="store_true",
        help="Print the complete final update payload instead of the interactive terminal summary.",
    )
    update.add_argument(
        "--allow-unbacked-upgrade",
        action="store_true",
        help="Allow --backup-mode skip. Intended only for container-only releases or disposable development hosts.",
    )
    add_download_args(update)

    content = subparsers.add_parser("content", help="Verify and operate the signed data-only security-content channel.")
    content.add_argument("action", choices=["check", "apply", "import", "status", "rollback"])
    content.add_argument("--pack", help="Local path or authenticated URL to a signed security-content pack.")
    content.add_argument("--public-key", help="Release public key. Defaults to the installed PacketSafari release key.")
    content.add_argument("--allow-downgrade", action="store_true", help="Allow explicit activation of an older sequence.")
    add_download_args(content)

    iam = subparsers.add_parser("iam", help="Host-side IAM helpers.")
    iam.add_argument("action", choices=["show-initial-admin-command", "set-password"])
    iam.add_argument("--email", default="admin@example.com")
    iam.add_argument("--username")
    iam.add_argument("--password")

    diagnostics = subparsers.add_parser("diagnostics", help="Restart services or inspect logs.")
    diagnostics.add_argument("action", choices=["restart", "logs"])
    diagnostics.add_argument("--service")
    diagnostics.add_argument("--container", default="packetsafari-backend")
    diagnostics.add_argument("--since", default="15m")
    diagnostics.add_argument("--tail", type=int, default=200)

    tui = subparsers.add_parser("tui", help="Launch the simple operator menu.")
    tui.add_argument("--api-base-url")
    return parser


def _format_update_result(payload: dict) -> str:
    summary = payload.get("updateSummary") if isinstance(payload.get("updateSummary"), dict) else {}
    previous_app = str(summary.get("previousApplicationVersion") or payload.get("currentVersion") or "unknown")
    installed_app = str(summary.get("installedApplicationVersion") or payload.get("version") or payload.get("targetVersion") or "unknown")
    previous_ops = str(summary.get("previousOpsVersion") or "unknown")
    installed_ops = str(summary.get("installedOpsVersion") or previous_ops)
    status_value = str(payload.get("status") or "ok")
    ops_updated = bool(summary.get("opsUpdated"))
    app_updated = previous_app != installed_app and status_value != "noop"
    if status_value == "noop" and ops_updated:
        title = "✓ OPS TOOL UPDATE SUCCEEDED — APPLICATION ALREADY CURRENT"
    elif status_value == "noop":
        title = "✓ PACKETSAFARI IS ALREADY CURRENT"
    else:
        title = "✓ PACKETSAFARI UPDATE SUCCEEDED"
    changed_services = summary.get("changedServices") if isinstance(summary.get("changedServices"), list) else []
    image_retention = payload.get("imageRetention") if isinstance(payload.get("imageRetention"), dict) else {}
    backup_retention = payload.get("backupRetention") if isinstance(payload.get("backupRetention"), dict) else {}
    candidate_count = int(image_retention.get("candidateCount") or 0)
    cleanup_status = str(image_retention.get("status") or "")
    if cleanup_status == "ok":
        cleanup = f"removed {len(image_retention.get('removedIds') or [])} old PacketSafari image(s)"
    elif candidate_count:
        cleanup = f"{candidate_count} removable PacketSafari image(s) kept"
    elif image_retention.get("skipped"):
        cleanup = "image-retention check skipped"
    else:
        cleanup = "no cleanup needed"
    backup_cleanup_status = str(backup_retention.get("status") or "not run")
    backup_removed = int(backup_retention.get("removedCount") or 0)
    backup_cleanup = (
        f"{backup_cleanup_status}; removed {backup_removed} old verified full backup(s)"
        if backup_removed
        else backup_cleanup_status
    )
    lines = [
        "",
        "=" * 76,
        title,
        "=" * 76,
        f"Application/backend  {previous_app} -> {installed_app}  [{'updated' if app_updated else 'current'}]",
        f"Ops tooling          {previous_ops} -> {installed_ops}  [{'updated' if ops_updated else 'current'}]",
        f"Profile              {payload.get('profile') or 'unknown'}",
        f"Changed services     {', '.join(str(item) for item in changed_services) if changed_services else 'none'}",
        f"Config reloads       {', '.join(str(item) for item in payload.get('configurationReloadedServices') or []) or 'none'}",
        f"Health/readiness     {summary.get('verification') or 'unknown'}",
        f"Rollback             {summary.get('rollback') or 'unchanged'}",
        f"Image cleanup        {cleanup}",
        f"Backup cleanup       {backup_cleanup}",
    ]
    if payload.get("snapshot"):
        lines.append(f"Snapshot             {payload['snapshot']}")
    warnings = [str(item) for item in summary.get("hostWarnings") or [] if str(item).strip()]
    if warnings:
        lines.append("Warnings             " + warnings[0])
        lines.extend(f"                     {warning}" for warning in warnings[1:])
    lines.extend(["=" * 76, ""])
    return "\n".join(lines)


def _format_update_failure(exc: Exception) -> str:
    def sanitize_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ").,;":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port else hostname
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", "")) + suffix

    reason = re.sub(r"https?://[^\s]+", sanitize_url, str(exc))
    return "\n".join(
        [
            "",
            "=" * 76,
            "✗ PACKETSAFARI UPDATE FAILED",
            "=" * 76,
            f"Reason: {reason}",
            "",
            "The command exited without reporting successful release promotion.",
            "Inspect `packetsafari-ops status` and run `packetsafari-ops healthcheck` before retrying.",
            "=" * 76,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.runtime_root = detect_runtime_root(getattr(args, "runtime_root", None))
    if hasattr(args, "api_base_url"):
        args.api_base_url = detect_api_base_url(getattr(args, "api_base_url", None))

    if args.command is None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.print_help()
            return 2
        run_menu(
            runtime_root=args.runtime_root,
            container_runtime_root=args.container_runtime_root,
            api_base_url=detect_api_base_url(None),
        )
        return 0

    if args.command == "install":
        print(json.dumps(install_release(args), indent=2))
        return 0
    if args.command == "status":
        payload = status(runtime_layout(args.runtime_root, args.container_runtime_root))
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Installer version: {payload['installerVersion']}")
            print(f"Runtime root: {payload['runtimeRoot']}")
            sizing_status = payload.get("sizingStatus") or {}
            if sizing_status.get("stale"):
                print(f"WARNING: {sizing_status.get('message')}", file=sys.stderr)
                for warning in sizing_status.get("warnings") or []:
                    print(f"WARNING: {warning}", file=sys.stderr)
            state = payload.get("state") or {}
            if state:
                print(json.dumps(state, indent=2))
            else:
                print("No deployment state found.")
        return 0
    if args.command == "doctor":
        print(json.dumps(doctor_deployment(args), indent=2))
        return 0
    if args.command == "healthcheck":
        payload = healthcheck_deployment(args)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(format_healthcheck_report(payload))
        return 0 if payload.get("ok") else 1
    if args.command == "upgrade":
        print(json.dumps(upgrade_release(args), indent=2))
        return 0
    if args.command == "update":
        if args.action == "check":
            print(json.dumps(check_for_update(args), indent=2))
        else:
            human_output = bool(sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty() and not args.json)
            setattr(args, "human_output", human_output)
            try:
                payload = apply_update(args)
            except Exception as exc:
                if human_output:
                    print(_format_update_failure(exc), file=sys.stderr)
                    return 1
                raise
            if human_output:
                print(_format_update_result(payload))
            else:
                print(json.dumps(payload, indent=2))
        return 0
    if args.command == "content":
        print(json.dumps(operate_security_content(args), indent=2))
        return 0
    if args.command == "rollback":
        print(json.dumps(rollback_release(args), indent=2))
        return 0
    if args.command == "tune":
        print(json.dumps(tune_runtime(args), indent=2))
        return 0
    if args.command == "onboard":
        client = LocalApiClient(args.api_base_url)
        values = json.loads(args.draft_json or "{}")
        if args.action == "schema":
            payload = client.onboarding_schema()
        elif args.action == "save-draft":
            payload = client.onboarding_save_draft(values)
        elif args.action == "validate":
            payload = client.onboarding_validate(values)
        else:
            payload = client.onboarding_finalize(values)
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "config":
        if args.action == "overview":
            print(json.dumps(configuration_overview(runtime_layout(args.runtime_root, args.container_runtime_root)), indent=2))
        elif args.action == "show":
            print(show_runtime_env(runtime_layout(args.runtime_root, args.container_runtime_root)))
        elif args.action == "upstream-proxy":
            print(json.dumps(configure_upstream_proxy(args), indent=2))
        else:
            print(json.dumps(configure_required_env(args), indent=2))
        return 0
    if args.command == "egress":
        if args.action == "mode" and not args.egress_mode:
            parser.error("egress mode requires allowlist or unrestricted")
        if args.action not in {"list-intelligence-hosts", "mode"} and not str(args.url or "").strip():
            parser.error(f"egress {args.action} requires --url")
        print(json.dumps(operate_intelligence_egress(args), indent=2))
        return 0
    if args.command == "iam":
        if args.action == "show-initial-admin-command":
            print(show_initial_admin_command(email=args.email))
            return 0
        if not args.username or not args.password:
            parser.error("iam set-password requires --username and --password")
        print(json.dumps(set_password(args), indent=2))
        return 0
    if args.command == "diagnostics":
        if args.action == "restart":
            print(json.dumps(diagnostics_restart(args), indent=2))
            return 0
        return diagnostics_logs(args)
    if args.command == "tui":
        run_menu(runtime_root=args.runtime_root, container_runtime_root=args.container_runtime_root, api_base_url=args.api_base_url)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
