from pathlib import Path
import tomllib

from packetsafari_onprem import __version__


def test_packaging_versions_match() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    release_version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert release_version == __version__
    assert project["project"]["version"] == __version__
