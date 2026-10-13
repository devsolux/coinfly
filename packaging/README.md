# Native release assembly

These are reviewed-source build inputs; generated releases stay in ignored build outputs.
Application metadata is `coinfly`, `1.0.0`, `com.devsolux.coinfly`; the worker uses
the subordinate identifier `com.devsolux.coinfly.worker`. Windows manifest versions
require four components, so its assembly version is `1.0.0.0`.

The Python application identifier is defined in `src/coinfly/__init__.py` and
shared by runtime labels, IPC authentication and the desktop credential namespace.
Release identity checks keep `pyproject.toml`, both platform manifests and worker
service templates aligned with `com.devsolux.coinfly`. The `.worker` suffix identifies
the separate helper; credential-purpose suffixes keep vault items scoped.

The release toolchain must bundle Python 3.11+ with linked SQLite >=3.51.3,
the locked dependencies, native Tk and CustomTkinter 6.0.0, and the prebuilt vault
and neural libraries. Build the libraries in `native/CMakeLists.txt` on their
actual architecture. No runtime compiler is invoked by Coinfly.

## macOS ARM64 production commands

```sh
./dev.sh build-unsigned --preflight
./dev.sh build-unsigned
./dev.sh build --preflight
./dev.sh build
```

Both commands select the same production source, Release native configuration,
locked dependencies and quality policy. `build` adds Developer ID signing,
notarization and stapling. `build-unsigned` excludes those operations and does
not enumerate signing identities or validate a notary profile. PyInstaller's
mechanical ARM64 ad-hoc signatures are retained and identified as `ad-hoc`;
they provide no authenticated publisher identity or notarization.

`dev.sh` is the sole default resolver, including for the Python wrappers:

```sh
export MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-351A9115F928304981211FEB5866E94B6C760AD1}"

export MACOS_NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-macos-notary}"
```

Unset **or empty** values use those exact defaults. Nonempty values, including
spaces and non-ASCII characters, are forwarded unchanged. Plain `./dev.sh build`
requires no signing flags. Signed preflight validates the exact certificate,
private-key usability and existing profile before dependency sync/compilation;
it never chooses another identity, creates credentials or falls back to unsigned.
The defaults do not establish that these references are installed.

The publisher is **devsolux**. Both modes use the native account's
`~/.devsolux/coinfly` data root as the default reviewed metadata directory.
Use `--dataset-distribution DIR` with `./dev.sh build`, `./dev.sh build-unsigned`
or `scripts/build_macos.py` to select an explicit directory. Only `trust-root.json`
and `manifest.json` are staged and validated; see
[publisher distribution](../docs/malecns-bootstrap.md#packaged-publisher-metadata).
Missing, expired, malformed or development inputs fail. Production commands no
longer initialize development descriptors. The explicit descriptor initializer
remains available to development tooling; Windows/Linux development packaging
and ordinary isolated Paper startup retain their separate behavior.

The verified production profile is Xcode **27.0**, Apple clang **21.0.0
(clang-2100.3.34.2)** and macOS SDK **27.0 / 26A425**. Set `DEVELOPER_DIR` to a
full `Xcode.app/Contents/Developer` to select an installation; the builder never
changes global xcode-select or substitutes standalone CLT/Homebrew LLVM. Xcode's
observed build number, SDK build and host OS build are recorded separately.
[Apple's host requirements](https://developer.apple.com/xcode/system-requirements)
currently require macOS 26.6 or later for Xcode 27. The shipped minimum and CMake
deployment target remain **26.0**, independently of SDK 27.0. Payload inspection
rejects binaries needing a newer runtime instead of raising the plist minimum;
compatible older third-party deployment records remain permitted.

Preflight checks native execution, linked SQLite >=3.51.3, selected tools, disk,
reviewed metadata and a usable Tk window; it does not run acceptance, sync
packages, compile or publish. Signed preflight additionally uses Apple signing
references; unsigned preflight does not. The release pipeline copies build
inputs (including current uncommitted changes) into a private workspace, records
a content digest, performs `uv lock --check` and exact `uv sync --locked` with
all desktop/research/training/dev/release dependencies into a fresh environment,
and runs shell, Ruff, strict mypy, pytest with native UI, and dependency audit.
Any failure stops the pipeline and retains diagnostics under `build/macos-MODE-*`.
It never updates the running development environment or silently repairs uv.lock.

The frozen version/doctor/UI probes remain **smoke tests**. The shared production
acceptance gate also requires the existing product qualification checks and
rejects unknown doctor blockers; only regional user enrollment and execution-host
fencing remain account setup. Current native, telemetry, strategy and gateway
qualification providers remain incomplete, so **this checkout cannot yet produce
a qualified production release**. The unsigned command does not waive these gaps
or grant native/live capability. A bundled kernel failure cannot fall back to a
local development kernel. The unresolved producer/verifier and frozen full-graph
acceptance work is listed in [the implementation ledger](../docs/devsh-production-builds.md).

On a qualified implementation the final archive is extracted into an unrelated
location and rechecked with the caller's expected mode. Signed archives also
require seal, ticket and Gatekeeper verification. Archive, checksum, manifest and
quality logs are published by one directory rename into a unique mode namespace:

```text
dist/1.0.0/macos-arm64/signed/<build-id>/coinfly-1.0.0-macos-arm64-signed.zip
dist/1.0.0/macos-arm64/unsigned/<build-id>/coinfly-1.0.0-macos-arm64-unsigned.zip
```

A failed build cannot replace another complete artifact set. Signed publication
requires Accepted notarization and validated stapling; the ZIP contains the
stapled app. Unsigned manifests identify excluded distribution-signing stages
as `NOT_APPLICABLE_UNSIGNED`. Build completion never installs services, publishes
a release externally, arms an account or proves financial qualification.
No signed or unsigned production artifact was generated by this change.

## Windows inputs

Windows AMD64 example build input (assembly/signing is separate):

```powershell
cmake -S native -B build/native -A x64
cmake --build build/native --config Release
```

Empty signer pins deliberately deny native vault access. The trusted build must
pin the verified signing identity, sign the main application and worker, qualify
their native vault/IPC behavior, then record the binary digests in the reviewed
release trust anchors. macOS additionally needs notarization and stapling.
The Windows overlapped named-pipe transport is a documented implementation gate;
its disabled task template must remain disabled until that adapter is qualified.

When assembling a Windows PyInstaller release, include the same two validated
metadata files with destination `coinfly/bootstrap/publisher` before Authenticode
signing, and run the resulting executable's `doctor` from an unrelated directory.
Require a satisfied `SIGNED_DATASET_DISTRIBUTION` check; checking exit 2 alone is
insufficient. This repository still has no qualified Windows release pipeline.
Source-contract CI exercises the loader and tamper cases using ephemeral test
keys; those keys must never be promoted to release inputs.

Install the LaunchAgent or Task Scheduler entry only as part of a qualified
per-user installer. Neither template contains credentials or financial overrides.
The source CLI does not register services automatically. Native GPU chart surfaces,
Retina/DPI behavior, suspend/restart behavior, and measured performance budgets still
require native validation; Tk widgets are not claimed to be GPU accelerated.


## Windows / Linux AMD64 development desktop bundles

On each actual target, use a compatible native Python/Tk and linked SQLite
>=3.51.3. Sync the locked desktop/research/training extras and `release` group,
then invoke the common builder from that environment:

```sh
uv sync --locked --extra desktop --extra research --extra training --group release
python dev.py build
```

`scripts/build_desktop.py` explicitly builds/verifies the local kernel, stages
exact development descriptors, and invokes `packaging/desktop/development.spec`.
The shared `packaging/desktop/entrypoint.py` serves desktop and worker (including
`.exe` names). The bundle is explicitly PAPER ONLY and cannot satisfy publisher,
native signing or live enrollment requirements. Generated kernel/build artifacts
are excluded from the source ZIP. Do not ship local development attestations as
production attestations.

The Windows/Linux packaging inputs were inspected, not executed in this session.
There is no claim of a signed installer, Linux execution-vault bridge, complete
native desktop QA or Windows/Linux production bundle attestation.
