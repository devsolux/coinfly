"""Verify source archive metadata without packaging operator files."""

import json
import runpy
import stat
import zipfile
from pathlib import Path


def test_source_archive_preserves_executable_launchers(tmp_path, capsys):
    root = tmp_path / "coinfly"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "build_release_archive.py"
    builder = scripts / source.name
    builder.write_bytes(source.read_bytes())
    (root / "pyproject.toml").write_text(
        '[project]\nname = "coinfly"\nversion = "1.0.0"\n'
        '[tool.coinfly]\napplication-id = "com.devsolux.coinfly"\n'
    )
    launcher = root / "dev.sh"
    launcher.write_text("#!/bin/bash\nexit 0\n")
    launcher.chmod(0o755)
    readme = root / "README.md"
    readme.write_text("Source archive test fixture\n")
    readme.chmod(0o644)
    schema = root / "src/coinfly/storage/schema.sql"
    schema.parent.mkdir(parents=True)
    schema.write_text("CREATE TABLE fixture (id INTEGER);\n")
    for name in ("data/source.feather", "storage/state.sqlite3", "datasets/graph.npz",
                 "models/checkpoint.npz", "runs/study.sqlite", "logs/session.log",
                 "cache/download.partial", "build/old.py", "dist/old.whl",
                 ".mypy_cache/version/cache.json", ".env", "coinbase-key.json", "private.pem"):
        private = root / name
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("PRIVATE FIXTURE — MUST NOT BE PACKAGED")

    runpy.run_path(str(builder), run_name="__main__")
    output = json.loads(capsys.readouterr().out)["file"]
    assert Path(output).parent == root / "dist"
    original = Path(output).read_bytes()
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "coinfly/dev.sh", "coinfly/README.md", "coinfly/pyproject.toml",
            "coinfly/scripts/build_release_archive.py", "coinfly/src/coinfly/storage/schema.sql",
        }
        entry = archive.getinfo("coinfly/dev.sh")
        assert entry.create_system == 3  # Unix permissions, even for archives built on Windows.
        assert stat.S_IMODE(entry.external_attr >> 16) == 0o755
        assert stat.S_IMODE(archive.getinfo("coinfly/README.md").external_attr >> 16) == 0o644
    runpy.run_path(str(builder), run_name="__main__")
    assert Path(output).read_bytes() == original
