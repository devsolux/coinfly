"""Application versions agree across Python and native package metadata."""

import plistlib
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import coinfly


def test_release_identity_matches_lockfile_and_native_packages():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "coinfly")
    assert coinfly.__version__ == project["project"]["version"] == package["version"] == "1.0.0"
    assert coinfly.__application_id__ == project["tool"]["coinfly"]["application-id"] == "com.devsolux.coinfly"
    assert coinfly.__publisher__ == project["tool"]["coinfly"]["publisher"] == "devsolux"
    assert project["project"]["authors"] == [{"name": coinfly.__publisher__}]
    metadata = plistlib.loads((root / "packaging/macos/Info.plist").read_bytes())
    assert metadata["CFBundleShortVersionString"] == metadata["CFBundleVersion"] == coinfly.__version__
    assert metadata["CFBundleIdentifier"] == coinfly.__application_id__
    assert metadata["LSBackgroundOnly"] is False
    windows = ET.parse(root / "packaging/windows/app.manifest")
    identity = windows.find("{urn:schemas-microsoft-com:asm.v1}assemblyIdentity")
    assert identity is not None
    assert identity.attrib["version"] == coinfly.__version__ + ".0"
    assert identity.attrib["name"] == coinfly.__application_id__
    worker_id = f"{coinfly.__application_id__}.worker"
    launch_agent = plistlib.loads((root / "packaging/macos/launchagent.plist").read_bytes())
    assert launch_agent["Label"] == worker_id
    task = ET.parse(root / "packaging/windows/worker_task.xml")
    task_namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert task.findtext("task:RegistrationInfo/task:URI", namespaces=task_namespace) == "\\" + worker_id
