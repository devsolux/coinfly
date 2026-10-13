# PyInstaller build input; evaluated by the locked release toolchain.
import os
import plistlib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
native = Path(os.environ["COINFLY_NATIVE_BUILD"])
mode = os.environ["COINFLY_BUILD_MODE"]
if mode not in {"signed", "unsigned"}:
    raise ValueError("An explicit production distribution mode is required")
# ARM64 PyInstaller performs mechanical ad-hoc signing when identity is None.
# Inherited dev.sh defaults must never select Developer ID in unsigned mode.
identity = os.environ["MACOS_SIGN_IDENTITY"] if mode == "signed" else None
entitlements = str(root / "packaging/macos/entitlements.plist")
info = plistlib.loads((root / "packaging/macos/Info.plist").read_bytes())
datas = collect_data_files("coinfly") + collect_data_files("customtkinter")
publisher = Path(os.environ["COINFLY_STAGED_PUBLISHER"])
# Validated publisher metadata is part of the application resource
# envelope in Contents/Resources; never add it after BUNDLE/signing.
datas += [(str(publisher / name), "coinfly/bootstrap/publisher")
          for name in ("trust-root.json", "manifest.json")]
for package in ("coinfly", "customtkinter", "keyring", "numpy", "pandas", "pyarrow", "Pillow",
                "numba", "optuna", "polars"):
    datas += copy_metadata(package, recursive=True)

a = Analysis(
    [str(root / "packaging/desktop/entrypoint.py")],
    pathex=[str(root / "src"), str(root / "scripts")],
    binaries=[
        (str(native / "libcoinfly_vault.dylib"), "coinfly/security/_native"),
        (str(native / "libcoinfly_neural.dylib"), "coinfly/research/neural/_native"),
    ],
    datas=datas,
    # load_toolkit imports CustomTkinter dynamically. Its data/metadata alone
    # leave an empty namespace package that passes doctor but cannot open a UI.
    hiddenimports=(collect_submodules("coinfly") + collect_submodules("customtkinter")
                   + ["keyring.backends.macOS"]),
    # Keep sources available for neural-rule hashes and Numba's cache locator.
    module_collection_mode={"coinfly": "pyz+py"},
    excludes=["pytest", "mypy", "ruff", "build", "pip_audit"],
    noarchive=False,
)
pyz = PYZ(a.pure)
desktop = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="coinfly", console=False,
    target_arch="arm64", codesign_identity=identity, entitlements_file=entitlements,
)
worker = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="coinfly-worker", console=True,
    target_arch="arm64", codesign_identity=identity, entitlements_file=entitlements,
)
collection = COLLECT(desktop, worker, a.binaries, a.datas, name="coinfly")
app = BUNDLE(
    collection, name="coinfly.app", icon=str(root / "assets/icons/coinfly.icns"),
    bundle_identifier=info["CFBundleIdentifier"], version=info["CFBundleVersion"], info_plist=info,
)
