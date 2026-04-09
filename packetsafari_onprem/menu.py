from __future__ import annotations

import curses
import json
import os
import textwrap
from argparse import Namespace
from dataclasses import dataclass
from typing import Callable

from .api import ApiError, LocalApiClient, detect_api_base_url
from .operations import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CONTAINER_RUNTIME_ROOT,
    detect_runtime_root,
    diagnostics_restart,
    rollback_release,
    runtime_layout,
    set_password,
    show_initial_admin_command,
    show_runtime_env,
    status,
    supports_onprem_host_actions,
    upgrade_release,
)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
RED = "\033[91m"


@dataclass(slots=True)
class MenuContext:
    runtime_root: str
    container_runtime_root: str
    api_base_url: str


@dataclass(slots=True)
class MenuItem:
    name: str
    description: str
    action: Callable[[MenuContext], None] | None = None
    submenu_factory: Callable[[MenuContext], list["MenuItem"]] | None = None


def _pause() -> None:
    input(f"\n{DIM}Press Enter to return...{RESET}")


def _clear() -> None:
    os.system("clear")


def _divider() -> str:
    return f"{DIM}{'─' * 76}{RESET}"


def _breadcrumb(parts: list[str]) -> str:
    return f"{CYAN}PacketSafari Ops{RESET} {DIM}>{RESET} " + f" {DIM}>{RESET} ".join(parts)


def _header(parts: list[str], ctx: MenuContext) -> None:
    layout = runtime_layout(ctx.runtime_root, ctx.container_runtime_root)
    print()
    print(_breadcrumb(parts))
    print(f"{DIM}Runtime root:{RESET} {BOLD}{ctx.runtime_root}{RESET} {DIM}[{layout.kind}]{RESET}")
    print(_divider())


def _print_json(title: str, payload: object, ctx: MenuContext, parts: list[str]) -> None:
    _clear()
    _header(parts + [title], ctx)
    print(json.dumps(payload, indent=2))
    _pause()


def _read_json_multiline() -> dict:
    print(f"{DIM}Paste JSON. End with a single line containing only '.'{RESET}")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)
    text = "\n".join(lines).strip() or "{}"
    return json.loads(text)


def _show_status(ctx: MenuContext) -> None:
    payload = status(runtime_layout(ctx.runtime_root, ctx.container_runtime_root))
    _print_json("Status", payload, ctx, ["Overview"])


def _show_runtime_env_action(ctx: MenuContext) -> None:
    layout = runtime_layout(ctx.runtime_root, ctx.container_runtime_root)
    _clear()
    _header(["Config", "Runtime Env"], ctx)
    text = show_runtime_env(layout)
    if text.strip():
        print(text)
    else:
        print(f"{YELLOW}No env file found at {layout.runtime_env_path}{RESET}")
    _pause()


def _show_onboarding_schema(ctx: MenuContext) -> None:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    try:
        payload = client.onboarding_schema().get("data") or {}
        _print_json("Schema", payload, ctx, ["Onboarding"])
    except ApiError as exc:
        _clear()
        _header(["Onboarding", "Schema"], ctx)
        print(f"{RED}Unable to reach the local PacketSafari onboarding API.{RESET}")
        print()
        print(f"{DIM}API base URL:{RESET} {ctx.api_base_url}")
        print(f"{DIM}Error:{RESET} {exc}")
        _pause()


def _validate_onboarding(ctx: MenuContext) -> None:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    _clear()
    _header(["Onboarding", "Validate Draft"], ctx)
    try:
        payload = client.onboarding_validate(_read_json_multiline())
        print(json.dumps(payload, indent=2))
    except (ApiError, json.JSONDecodeError) as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _save_onboarding(ctx: MenuContext) -> None:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    _clear()
    _header(["Onboarding", "Save Draft"], ctx)
    try:
        payload = client.onboarding_save_draft(_read_json_multiline())
        print(json.dumps(payload, indent=2))
    except (ApiError, json.JSONDecodeError) as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _finalize_onboarding(ctx: MenuContext) -> None:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    _clear()
    _header(["Onboarding", "Finalize"], ctx)
    try:
        payload = client.onboarding_finalize(_read_json_multiline())
        print(json.dumps(payload, indent=2))
    except (ApiError, json.JSONDecodeError) as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _show_initial_admin(ctx: MenuContext) -> None:
    _clear()
    _header(["IAM", "Initial Admin"], ctx)
    email = input("Initial admin email [admin@example.com]: ").strip() or "admin@example.com"
    print()
    print(f"{GREEN}{show_initial_admin_command(email=email)}{RESET}")
    _pause()


def _change_password(ctx: MenuContext) -> None:
    _clear()
    _header(["IAM", "Change Admin Password"], ctx)
    username = input("Username: ").strip()
    password = input("New password: ").strip()
    try:
        payload = set_password(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
                username=username,
                password=password,
            )
        )
        print(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _upgrade(ctx: MenuContext) -> None:
    _clear()
    _header(["Updates & Rollback", "Upgrade"], ctx)
    layout = runtime_layout(ctx.runtime_root, ctx.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        print(f"{YELLOW}Upgrade is only supported for real on-prem runtime roots like /opt/packetsafari.{RESET}")
        _pause()
        return
    manifest = input("Manifest path: ").strip()
    try:
        payload = upgrade_release(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
                manifest=manifest,
            )
        )
        print(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _rollback(ctx: MenuContext) -> None:
    _clear()
    _header(["Updates & Rollback", "Rollback"], ctx)
    layout = runtime_layout(ctx.runtime_root, ctx.container_runtime_root)
    if not supports_onprem_host_actions(layout):
        print(f"{YELLOW}Rollback is only supported for real on-prem runtime roots like /opt/packetsafari.{RESET}")
        _pause()
        return
    try:
        payload = rollback_release(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
            )
        )
        print(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _restart_all(ctx: MenuContext) -> None:
    _clear()
    _header(["Diagnostics", "Restart Stack"], ctx)
    try:
        payload = diagnostics_restart(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
                service=None,
            )
        )
        print(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _restart_one(ctx: MenuContext) -> None:
    _clear()
    _header(["Diagnostics", "Restart One Service"], ctx)
    service = input("Service name: ").strip() or None
    try:
        payload = diagnostics_restart(
            Namespace(
                runtime_root=ctx.runtime_root,
                container_runtime_root=ctx.container_runtime_root,
                service=service,
            )
        )
        print(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"{RED}Error:{RESET} {exc}")
    _pause()


def _change_runtime_root(ctx: MenuContext) -> None:
    _clear()
    _header(["Runtime Root"], ctx)
    default = detect_runtime_root()
    entered = input(f"Runtime root [{default}]: ").strip()
    ctx.runtime_root = detect_runtime_root(entered or default)


def _overview_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Status", "Show the detected runtime root, env path, state, backups, and helper status.", action=_show_status),
        MenuItem("Runtime Env", "Print the current env file for the active runtime root.", action=_show_runtime_env_action),
    ]


def _onboarding_items(ctx: MenuContext) -> list[MenuItem]:
    status_text = _onboarding_status_description(ctx)
    return [
        MenuItem("Show Schema", f"Fetch the current onboarding schema, draft, license, and bootstrap status. {status_text}", action=_show_onboarding_schema),
        MenuItem("Validate Draft JSON", f"Paste a draft payload and validate it against the local onboarding API. {status_text}", action=_validate_onboarding),
        MenuItem("Save Draft JSON", f"Paste a draft payload and save it to the local onboarding state. {status_text}", action=_save_onboarding),
        MenuItem("Finalize Draft JSON", f"Paste a draft payload and finalize onboarding. {status_text}", action=_finalize_onboarding),
    ]


def _iam_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Show Initial Admin Command", "Print the backend-container command to create the first administrator.", action=_show_initial_admin),
        MenuItem("Change Admin Password", "Run the backend maintenance helper to set a new password.", action=_change_password),
    ]


def _update_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Upgrade From Manifest", "Apply a target on-prem release manifest.", action=_upgrade),
        MenuItem("Rollback Latest Snapshot", "Restore the most recent on-prem backup snapshot.", action=_rollback),
    ]


def _diagnostic_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Restart Full Stack", "Restart the entire detected stack.", action=_restart_all),
        MenuItem("Restart One Service", "Restart a single service by name.", action=_restart_one),
    ]


def _main_items(_ctx: MenuContext) -> list[MenuItem]:
    return [
        MenuItem("Overview", "See the detected runtime root, current env path, and deployment state.", submenu_factory=_overview_items),
        MenuItem("Onboarding", "Inspect, validate, save, or finalize onboarding through the local API.", submenu_factory=_onboarding_items),
        MenuItem("IAM", "Bootstrap the first admin or change an existing admin password.", submenu_factory=_iam_items),
        MenuItem("Updates & Rollback", "Run upgrade or rollback actions for real on-prem runtime roots.", submenu_factory=_update_items),
        MenuItem("Diagnostics", "Restart the full stack or a single service.", submenu_factory=_diagnostic_items),
        MenuItem("Change Runtime Root", "Switch between /opt/packetsafari and local packetsafari-data style roots.", action=_change_runtime_root),
    ]


def _onboarding_status_description(ctx: MenuContext) -> str:
    client = LocalApiClient(detect_api_base_url(ctx.api_base_url)).with_detected_base_url()
    ctx.api_base_url = client.base_url
    summary = client.health_summary()
    if not summary.get("reachable"):
        return f"API unreachable at {summary.get('baseUrl')}. Start the local stack first."
    if not summary.get("onPremises"):
        return f"Backend reachable at {summary.get('baseUrl')}, but on-prem mode is disabled."
    if not summary.get("onboardingMode"):
        return f"Backend reachable at {summary.get('baseUrl')}, but onboarding mode is disabled."
    return f"Onboarding API ready at {summary.get('baseUrl')}."


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    available = max(0, width - x - 1)
    stdscr.addnstr(y, x, text, available, attr)


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


def _render_curses_menu(stdscr, parts: list[str], items: list[MenuItem], ctx: MenuContext, selected: int, *, allow_back: bool, allow_quit: bool) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    layout = runtime_layout(ctx.runtime_root, ctx.container_runtime_root)

    _safe_addstr(stdscr, 1, 2, "PacketSafari Ops", curses.color_pair(1) | curses.A_BOLD)
    _safe_addstr(stdscr, 1, 19, " > ".join(parts), curses.A_BOLD)
    _safe_addstr(stdscr, 2, 2, f"Runtime root: {ctx.runtime_root} [{layout.kind}]", curses.A_DIM)
    _safe_addstr(stdscr, 3, 2, f"API: {ctx.api_base_url}", curses.A_DIM)
    _safe_addstr(stdscr, 4, 2, "─" * max(10, width - 4), curses.A_DIM)

    row = 6
    for index, item in enumerate(items):
        attr = curses.A_BOLD
        prefix = "  "
        if index == selected:
            attr = curses.color_pair(6) | curses.A_BOLD
            prefix = "❯ "
        suffix = "  →" if item.submenu_factory else ""
        _safe_addstr(stdscr, row, 2, f"{prefix}{item.name}{suffix}", attr)
        row += 1
        for line in textwrap.wrap(item.description, max(20, width - 8)) or [""]:
            desc_attr = curses.A_DIM if index != selected else curses.color_pair(6)
            _safe_addstr(stdscr, row, 5, line, desc_attr)
            row += 1
        row += 1

    footer = []
    if allow_back:
        footer.append(("Esc", "Back", curses.color_pair(3)))
    if allow_quit:
        footer.append(("q", "Quit", curses.color_pair(4)))
    footer.append(("↑/↓", "Move", curses.color_pair(2)))
    footer.append(("Enter", "Open", curses.color_pair(5)))

    footer_text = "   ".join(f"[{key}] {label}" for key, label, _ in footer)
    _safe_addstr(stdscr, height - 2, 2, footer_text, curses.A_DIM)
    stdscr.refresh()


def _run_action_outside_curses(stdscr, action: Callable[[MenuContext], None], ctx: MenuContext) -> None:
    curses.def_prog_mode()
    curses.endwin()
    try:
        try:
            action(ctx)
        except Exception as exc:
            _clear()
            _header(["Action Error"], ctx)
            print(f"{RED}Action failed.{RESET}")
            print()
            print(str(exc))
            _pause()
    finally:
        curses.reset_prog_mode()
        stdscr.refresh()


def _walk_menu(stdscr, parts: list[str], items: list[MenuItem], ctx: MenuContext, *, allow_back: bool, allow_quit: bool = False) -> bool:
    selected = 0
    while True:
        _render_curses_menu(stdscr, parts, items, ctx, selected, allow_back=allow_back, allow_quit=allow_quit)
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
            should_quit = _walk_menu(stdscr, parts + [item.name], item.submenu_factory(ctx), ctx, allow_back=True)
            if should_quit:
                return True
            continue
        if item.action:
            _run_action_outside_curses(stdscr, item.action, ctx)


def run_menu(runtime_root: str | None = None, container_runtime_root: str = DEFAULT_CONTAINER_RUNTIME_ROOT, api_base_url: str = DEFAULT_API_BASE_URL) -> None:
    ctx = MenuContext(
        runtime_root=detect_runtime_root(runtime_root),
        container_runtime_root=container_runtime_root,
        api_base_url=api_base_url,
    )

    def _main(stdscr) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        _init_colors()
        _walk_menu(stdscr, ["Main Menu"], _main_items(ctx), ctx, allow_back=False, allow_quit=True)

    curses.wrapper(_main)
