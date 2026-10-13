"""Production provenance failures never select local development kernels."""


import pytest

from coinfly.bootstrap.manifest import BootstrapError
from coinfly.research.assets import KERNEL_SOURCE_SHA256
from coinfly.research.kernel_registry import KernelRegistry, library_name


@pytest.mark.parametrize("bundled", [False, True])
def test_missing_or_invalid_production_kernel_does_not_fall_back(tmp_path, monkeypatch, bundled):
    import coinfly.research.kernel_registry as module

    bundle = tmp_path / "bundle"
    local = tmp_path / "kernels" / KERNEL_SOURCE_SHA256[:16] / library_name()
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local kernel")
    binary = bundle / "coinfly/research/neural/_native" / library_name()
    if bundled:
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"invalid production kernel")
    calls = []

    def verify(path, verifier):
        calls.append(path)
        if path == local:
            pytest.fail("Production attempted local fallback")
        raise BootstrapError("NATIVE_KERNEL_ATTESTATION_INVALID")

    monkeypatch.setattr(module, "verify_native_kernel", verify)
    registry = KernelRegistry(local.parent.parent, bundle_root=bundle)
    assert registry.verified() is None
    assert registry.capability is None
    assert calls == ([binary] if bundled else [])
    assert registry.describe()["ready"] is False


def test_malformed_package_metadata_cannot_select_local_kernel(tmp_path, monkeypatch):
    monkeypatch.setattr("coinfly.bootstrap.development_distribution.is_development_distribution",
                        lambda *args: (_ for _ in ()).throw(BootstrapError("INVALID_METADATA")))
    registry = KernelRegistry(tmp_path / "kernels", bundle_root=tmp_path / "bundle")
    assert registry.readiness() == (False, "INVALID_METADATA")
    assert registry.describe()["candidates"] == []


def test_unbundled_development_still_enumerates_local_kernel(tmp_path):
    local = tmp_path / KERNEL_SOURCE_SHA256[:16] / library_name()
    local.parent.mkdir()
    local.write_bytes(b"fixture")
    registry = KernelRegistry(tmp_path)
    assert [path for path, _ in registry.candidates()] == [local]
    assert registry.candidates()[0][1].capability == "LOCAL_DEVELOPMENT_KERNEL"
