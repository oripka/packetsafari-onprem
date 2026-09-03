from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_local_release.py"
SPEC = importlib.util.spec_from_file_location("build_local_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_local_release_builds_content_pack_unless_overridden(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    builder = app_root / "scripts" / "build_security_content_pack.py"
    builder.parent.mkdir(parents=True)
    builder.touch()
    build_spec = tmp_path / "content-pack-build.json"
    signing_key = tmp_path / "release-private.pem"
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(command: list[str], *, cwd=None, env=None) -> None:
        calls.append((command, cwd))
        Path(command[command.index("--output") + 1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[command.index("--output") + 1]).touch()

    monkeypatch.setattr(MODULE, "DEFAULT_DATA_ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "run", fake_run)

    generated = MODULE.prepare_security_content_pack(
        app_root,
        pack_override=None,
        build_spec=str(build_spec),
        signing_key=str(signing_key),
    )

    assert generated == (tmp_path / "security-content" / "release" / "security-content-pack.tar.gz")
    assert calls == [
        (
            [
                MODULE.sys.executable,
                str(builder),
                "--spec",
                str(build_spec),
                "--signing-key",
                str(signing_key),
                "--output",
                str(generated),
            ],
            app_root,
        )
    ]

    override = tmp_path / "reviewed-pack.tar.gz"
    override.touch()
    assert MODULE.prepare_security_content_pack(
        app_root,
        pack_override=str(override),
        build_spec=str(build_spec),
        signing_key=str(signing_key),
    ) == override
    assert len(calls) == 1
