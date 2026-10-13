# Locked-toolchain native Windows/Linux PAPER ONLY bundle. Not a live release.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
publisher = Path(os.environ["COINFLY_STAGED_PUBLISHER"])
kernel = Path(os.environ["COINFLY_DEVELOPMENT_KERNEL"])
name = "coinfly_neural.dll" if sys.platform == "win32" else "libcoinfly_neural.so"
datas = collect_data_files("coinfly") + collect_data_files("customtkinter")
datas += [(str(publisher / filename), "coinfly/bootstrap/publisher")
          for filename in ("trust-root.json", "manifest.json")]
datas += [(str(kernel / (name + ".attestation.json")), "coinfly/research/neural/_native")]
for package in ("coinfly", "customtkinter", "keyring", "numpy", "pandas", "pyarrow", "Pillow",
                "numba", "optuna", "polars"):
    datas += copy_metadata(package, recursive=True)
backend = "keyring.backends.Windows" if sys.platform == "win32" else "keyring.backends.SecretService"
a = Analysis([str(root / "packaging/desktop/entrypoint.py")],
    pathex=[str(root / "src"), str(root / "scripts")],
    binaries=[(str(kernel / name), "coinfly/research/neural/_native")], datas=datas,
    hiddenimports=collect_submodules("coinfly") + collect_submodules("customtkinter") + [backend],
    module_collection_mode={"coinfly": "pyz+py"},
    excludes=["pytest", "mypy", "ruff", "build", "pip_audit"], noarchive=False)
pyz = PYZ(a.pure)
desktop = EXE(pyz, a.scripts, [], exclude_binaries=True, name="coinfly", console=False)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="coinfly-worker", console=True)
collection = COLLECT(desktop, worker, a.binaries, a.datas, name="coinfly-1.0.0-development")
