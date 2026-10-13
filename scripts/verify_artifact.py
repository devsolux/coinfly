"""Verify built wheel identity and safety-critical non-Python package data."""

import email
import zipfile
from pathlib import Path


def main() -> None:
    wheels = sorted(Path("dist").glob("coinfly-1.0.0-*.whl"))
    if not wheels:
        raise SystemExit("coinfly 1.0.0 wheel missing")
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = archive.namelist()
        for path in ("coinfly/py.typed", "coinfly/storage/schema.sql",
                     "coinfly/research/manifests/sources.lock.json", "coinfly/research/neural/kernel.cpp"):
            if path not in names:
                raise SystemExit(f"required wheel asset missing: {path}")
        metadata = email.message_from_bytes(archive.read("coinfly-1.0.0.dist-info/METADATA"))
        if metadata["Name"] != "coinfly" or metadata["Version"] != "1.0.0":
            raise SystemExit("wheel identity mismatch")
        if any(Path(name).parts[0] not in {"coinfly", "coinfly-1.0.0.dist-info"}
               or any(part.startswith(".env") for part in Path(name).parts) for name in names):
            raise SystemExit("unexpected source or ambient configuration in runtime wheel")
    print("coinfly 1.0.0 wheel metadata and required data verified")


if __name__ == "__main__":
    main()
