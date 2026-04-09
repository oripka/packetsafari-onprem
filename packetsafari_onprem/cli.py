from __future__ import annotations

import argparse
import json
import sys
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
        install_release,
        rollback_release,
        runtime_layout,
        set_password,
        show_initial_admin_command,
        show_runtime_env,
        status,
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
        install_release,
        rollback_release,
        runtime_layout,
        set_password,
        show_initial_admin_command,
        show_runtime_env,
        status,
        upgrade_release,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="packetsafari-ops")
    parser.add_argument("--runtime-root")
    parser.add_argument("--container-runtime-root", default=DEFAULT_CONTAINER_RUNTIME_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="Install PacketSafari on-prem into onboarding mode.")
    install.add_argument("--license", required=True)
    install.add_argument("--manifest")
    install.add_argument("--non-interactive", action="store_true")
    install.add_argument("--audit-log-enabled")
    install.add_argument("--audit-log-persist")
    install.add_argument("--audit-retention-days")
    install.add_argument("--audit-forwarding-mode")
    install.add_argument("--audit-forwarder-type")

    status_parser = subparsers.add_parser("status", help="Show installer/runtime status.")
    status_parser.add_argument("--json", action="store_true")

    upgrade = subparsers.add_parser("upgrade", help="Apply a new release manifest.")
    upgrade.add_argument("--manifest", required=True)

    subparsers.add_parser("rollback", help="Restore the latest runtime snapshot.")

    onboard = subparsers.add_parser("onboard", help="Operate on local onboarding APIs.")
    onboard.add_argument("--api-base-url")
    onboard.add_argument("action", choices=["schema", "save-draft", "validate", "finalize"])
    onboard.add_argument("--draft-json", default="{}")

    config = subparsers.add_parser("config", help="Inspect managed deployment config.")
    config.add_argument("action", choices=["show"])

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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.runtime_root = detect_runtime_root(getattr(args, "runtime_root", None))
    if hasattr(args, "api_base_url"):
        args.api_base_url = detect_api_base_url(getattr(args, "api_base_url", None))

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
            state = payload.get("state") or {}
            if state:
                print(json.dumps(state, indent=2))
            else:
                print("No deployment state found.")
        return 0
    if args.command == "upgrade":
        print(json.dumps(upgrade_release(args), indent=2))
        return 0
    if args.command == "rollback":
        print(json.dumps(rollback_release(args), indent=2))
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
        print(show_runtime_env(runtime_layout(args.runtime_root, args.container_runtime_root)))
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
