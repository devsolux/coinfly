# macOS production builds: status and open work

Commands, defaults and the publication layout are described in
[native release assembly](../packaging/README.md). This page records what the
production pipeline already enforces and what still blocks a qualified release.

```sh
./dev.sh build-unsigned --preflight
./dev.sh build-unsigned
./dev.sh build --preflight
./dev.sh build
```

`dev.sh` is the only place that resolves the signing defaults
(`MACOS_SIGN_IDENTITY`, `MACOS_NOTARY_PROFILE`). `dev.py` and
`scripts/build_desktop.py` delegate to `dev.sh` once, and `dev.sh` calls
`scripts/build_macos.py` with an explicit `--mode`. Unsigned mode never looks up
Apple credentials.

**Status:** source implementation only. The checkout cannot yet produce a
qualified production release, and no signed or unsigned production archive has
been published.

## Enforced by the pipeline

Each item is covered by `tests/test_production_build_modes.py`,
`tests/test_macos_release.py`, `tests/test_macos_build_profile.py` or
`tests/research/test_production_kernel_selection.py`.

- Production builds require reviewed publisher metadata (`trust-root.json`,
  `manifest.json`) and never generate development descriptors. Without them the
  build stops with `Publisher metadata required`.
- Both modes snapshot the build inputs, run `uv lock --check` and
  `uv sync --locked` into a fresh release environment, and run the shell, Ruff,
  strict mypy, pytest (with native UI) and dependency-audit gates. Any failure
  stops publication.
- Product acceptance rejects missing native, telemetry, strategy or gateway
  checks and unknown doctor blockers. Smoke probes (version, doctor, disconnected
  UI) are not treated as acceptance.
- A non-development bundle selects only bundled kernel candidates. A missing or
  invalid bundled kernel never falls back to a local development kernel.
- The deployment target is fixed at macOS **26.0** (Info.plist and CMake),
  independent of SDK 27.0. Payload inspection rejects binaries that need a newer
  runtime and checks the exact compiler/SDK profile.
- Notary submission and tool waits are bounded. A timeout is recorded as Unknown
  and never retried automatically.
- The final archive is extracted and rechecked for the expected mode. Archive,
  checksum, manifest and logs publish through one directory rename, so a failed
  build cannot replace a complete artifact set.

## Open prerequisites

| Gap | What is needed |
| --- | --- |
| Publisher metadata | Independently reviewed devsolux `trust-root.json` and `manifest.json` in `~/.devsolux/coinfly` or `--dataset-distribution DIR` |
| Qualification providers | The native, telemetry, strategy and gateway providers are still placeholders; they need independent evidence providers |
| Bundled kernel attestation | A reviewed post-signing attestation producer bound to the final bytes, plus containing-app, running-worker and allowed-signer verification in `kernel_registry.py` |
| Entitlements | The shared JIT/unsigned-memory entitlements are kept until per-process numerical tests (and, where needed, a separate NeuralHost) justify reducing them |
| Notarization recovery | Persistent submission-ID reconciliation and resume after an Unknown outcome |
| Frozen acceptance | Full frozen desktop, worker, IPC, slot, neural and paper acceptance for each mode, plus a run on a macOS 26.0 ARM64 target bound to the artifact bytes |
| Supply chain | Native dependency/load-path audit and a complete dependency/native SBOM with notices |

Live enrollment, live derivatives and neural-process isolation are separate gaps;
see [implementation status](implementation-status.md).

## Toolchain profile

| Item | Required value |
| --- | --- |
| Host | Native Apple Silicon (ARM64), logged-in desktop session |
| Xcode | 27.0, selected through `DEVELOPER_DIR` (never global `xcode-select`) |
| Compiler | Apple clang 21.0.0 (clang-2100.3.34.2) |
| SDK | macOS 27.0 (26A425) |
| Deployment target | macOS 26.0 |
| Python | ≥3.11 linked to SQLite ≥3.51.3, with native Tk |

The preserved graph contract is 166,700 neurons and 25,582,938 edges; the pinned
kernel source SHA-256 is
`e2d4d584f4633613277bb1bf6ea7ba20855c01a332ae2e9344ed7782604c8509`
(`KERNEL_SOURCE_SHA256` in `src/coinfly/research/assets.py`).

## Release runner requirements

- Use a trusted, logged-in Apple Silicon runner with the exact profile above and
  the same `dev.sh` commands.
- Supply publisher metadata independently of Apple signing inputs.
- Unsigned jobs receive no Apple credentials. Only a protected signed job may
  receive signing references; untrusted pull-request jobs never do.
- The source CI workflow (`.github/workflows/ci.yml`) validates source contracts
  only. It is not a release or target-acceptance job.

## Release workflows

Both run only on manual dispatch or a published release and never hold broker
credentials.

- `.github/workflows/release.yml` (GitHub-hosted) checks `uv.lock`, builds the
  source archive, sdist and wheel, verifies them, and signs each file and
  `SHA256SUMS-python` with Sigstore keyless signing. A published release gets the
  files and their `.sigstore.json` bundles.
- `.github/workflows/desktop.yml` is opt-in: set the repository variable
  `COINFLY_NATIVE_RUNNERS=true` once self-hosted runners with the labels in
  `.github/actionlint.yaml` exist. It runs `./dev.sh build-unsigned` and, in the
  protected `coinfly-production-signing` environment, `./dev.sh build` on macOS
  ARM64. `COINFLY_DATASET_DISTRIBUTION`, `MACOS_SIGN_IDENTITY` and
  `MACOS_NOTARY_PROFILE` are repository variables; empty values use the `dev.sh`
  defaults. Windows/Linux jobs build the paper-only development bundles as
  short-lived artifacts that are never signed or published. Until the acceptance
  gaps above are closed the macOS jobs stop before publication.

Verify a downloaded asset against its bundle:

```sh
cosign verify-blob --bundle FILE.sigstore.json \
  --certificate-identity-regexp '^https://github.com/devsolux/coinfly/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com FILE
```

Tooling references: [Apple Xcode requirements](https://developer.apple.com/xcode/system-requirements),
[PyInstaller macOS signing](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing),
[uv lock and sync](https://docs.astral.sh/uv/concepts/projects/sync/).
