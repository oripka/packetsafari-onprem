from pathlib import Path
import tomllib

from packetsafari_onprem import __version__


def test_tooling_versions_match_for_signed_self_update():
    root = Path(__file__).resolve().parents[1]
    source_version = (root / 'VERSION').read_text().strip()
    package_version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    assert source_version == package_version == __version__
