"""Create a deterministic source archive.

This packages reviewed source and documentation; it never bundles operator data,
virtual environments, credentials, generated native binaries or release claims.
"""

import hashlib
import json
import tomllib
import zipfile
from pathlib import Path

EXCLUDED = {
    ".git", ".venv", ".agents", ".codex", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache",
}
ROOT_OUTPUTS = {"data", "storage", "datasets", "models", "runs", "logs", "cache", "htmlcov"}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".pyc", ".pyo", ".key", ".pem", ".npz", ".feather",
                    ".arrow", ".dylib", ".so", ".dll", ".partial", ".log"}
PRIVATE_NAMES = {".DS_Store", "coinbase-key.json", "cdp_api_key.json", ".coverage"}


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    if (metadata["project"]["name"], metadata["project"]["version"],
        metadata["tool"]["coinfly"]["application-id"]) != ("coinfly", "1.0.0", "com.devsolux.coinfly"):
        raise SystemExit("release identity mismatch")
    output = root / "dist" / "coinfly-1.0.0.zip"
    output.parent.mkdir(exist_ok=True)
    source_files = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if relative.parts[0] in ROOT_OUTPUTS or any(
                   part in EXCLUDED or part.startswith(".mypy_cache") or part.endswith(".egg-info")
                   for part in relative.parts):
                continue
            if path.is_symlink():
                raise SystemExit(f"symlinks are not release inputs: {relative}")
            if not path.is_file() or path.name in PRIVATE_NAMES or path.name.startswith((".env", ".coverage.")):
                continue
            if path.suffix in PRIVATE_SUFFIXES or path.name.endswith(("-wal", "-shm", ".zip")):
                continue
            data = path.read_bytes()
            name = "coinfly/" + relative.as_posix()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            # Keep launchers executable after extraction, with normalized modes. Windows
            # file modes carry no execute bit, so shell launchers are named explicitly.
            permissions = 0o755 if path.stat().st_mode & 0o111 or path.suffix == ".sh" else 0o644
            entry.external_attr = (0o100000 | permissions) << 16
            archive.writestr(entry, data)
            source_files += 1
    print(json.dumps({"file": str(output), "source_files": source_files, "bytes": output.stat().st_size,
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
