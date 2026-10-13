# Production build command update — 2026-09-22

## Scope and status

Implemented the supplied document's environment/default and explicit build-mode
contract against the current refactored checkout, preserving existing changes.
Application identity remains `coinfly 1.0.0`, `com.devsolux.coinfly`, and
`com.devsolux.coinfly.worker`. No dependency pins, scientific rules, ledger formats,
account settings or financial permissions were changed.

This is **source implementation, not a completed production release**. The new
commands refuse the former development fallback. Independently reviewed publisher
metadata is absent; the existing product qualification providers and frozen
full-graph acceptance also remain incomplete. No signed/unsigned release archive,
accepted notarization, minimum-OS verdict or scientific qualification was produced.

```sh
./dev.sh build-unsigned --preflight
./dev.sh build-unsigned
./dev.sh build --preflight
./dev.sh build
```

The exact default fingerprint is `351A9115F928304981211FEB5866E94B6C760AD1` and
profile is `macos-notary`. `dev.sh` alone applies `${VAR:-default}`. Nonempty values
are forwarded without trimming or splitting. No identity/profile flags are needed.
Unsigned mode ignores those references for signing, including inherited defaults.
`dev.py` and `build_desktop.py` delegate to the shell once; the shell delegates to
`build_macos.py` with an explicit mode. A legacy direct builder invocation without
a mode delegates to the shell to resolve defaults, then returns the child status.

## Issue ledger

| Symptom / source | Reproduction and classification | Root cause / change | Regression / remaining prerequisite |
| --- | --- | --- | --- |
| `dev.sh` generated development descriptors for plain build; `stage_distribution` defaulted to accepting them | Confirmed by source and prior launcher tests; command-contract defect | Restored exact exports, explicit signed/unsigned selection and production-required metadata. Explicit development descriptor utility remains separate | Real Bash table covers unset, independently unset, empty, overrides, whitespace/Unicode, preflight, status propagation and unsupported flags. Reviewed publisher root/manifest still required |
| Build bypassed quality commands and synced running `.venv` | Confirmed orchestration; build isolation/quality defect | Both modes snapshot build inputs and sync a fresh release environment with `uv lock --check`, `uv sync --locked`; shared shell/Ruff/mypy/pytest-with-UI/dependency gates abort on failure | Every common-stage failure prevents publication. Existing source Ruff/mypy/runtime failures remain release blockers |
| `smoke_bundle` covered doctor and a disconnected UI only | Confirmed `verify_ui_startup.py` and frozen entrypoint; acceptance gap | Kept smoke scope explicit; product acceptance rejects missing native/telemetry/strategy/gateway checks and unknown doctor blockers. Account region/host fencing do not certify the product | Missing required/unknown checks fail. Existing hardcoded qualification placeholders have **not** been replaced with independent evidence providers; full frozen desktop/worker/IPC/slots/neural/Paper acceptance is still missing |
| No signed-bundle native attestation producer | Confirmed builder/spec versus `BundledKernelVerifier`; unresolved integration gap | Not fabricated as build metadata or a self-issued qualification record | Needs reviewed attestation policy, post-relocation/signing producer, final-byte binding, native/full-graph evidence; blocks production |
| Bundled verifier did not validate enclosing seal or compare running executable identity | Confirmed `kernel_registry.py`; unresolved trust-chain gap | No weaker unsigned verification capability introduced | Needs complete containing-app/running-worker/allowed-signer verification and legitimate unsigned integrity path. Unsigned sidecars/ad-hoc signatures cannot mint signed authority |
| Registry tried local-development candidates after bundled failure | Confirmed enumeration; capability downgrade defect (not evidence of a demonstrated Live bypass) | Non-development bundles now return only bundled candidates; missing, invalid or malformed production metadata/kernel cannot select local code | Missing/invalid bundle, malformed metadata and retained explicit local-development tests |
| Unbounded notary submission; checksum published after archive replacement; no final extraction | Confirmed builder; failure/publication defects | Bounded submission/tool waits; timeout recorded as Unknown with no automatic retry. Archive is extracted and rechecked by expected mode; archive/checksum/manifest/logs publish through one unique mode-directory rename | Rejection/malformed/timeout and stage/checksum/rename failures covered. Persistent submission-ID reconciliation/resume, final signed-distribution execution and richer SBOM/native notices still require work |
| Shared JIT/unsigned-memory entitlements | Confirmed existing plist; unresolved least-privilege qualification | Preserved permissions pending actual frozen numerical evidence; no claim that imports prove necessity | Requires per-process numerical tests and, where needed, NeuralHost isolation; no entitlement reduction or qualification claimed |
| Delegating wrappers could recurse during consolidation | Confirmed existing `dev.py -> build_desktop.py -> dev.sh` direction; integration risk | macOS wrappers terminate at `dev.sh -> build_macos.py --mode`; no return to dev.py | Each wrapper/mode/preflight combination delegates exactly once; non-macOS development packaging remains separate |
| Builder raised minimum OS to match dependencies | Confirmed `check_bundle`; deployment-contract defect | Fixed Info.plist/CMake target at 26.0; exact selected compiler/SDK checks; reject newer required binaries and wrong project-native SDK records | SDK 27/deployment 26 positive case, older third-party target, newer dependency rejection and toolchain mismatch fixtures; native libraries compiled/inspected on this host |

The common pipeline does not promote a successful preflight, source unit suite,
mechanical signature, Apple notarization or disconnected window probe into these
missing qualifications. Missing frozen functionality is not an unsigned exemption.

## Observed host and native checks

Actual unsigned preflight (outside the restrictive sandbox for host metadata):

- Apple M4, native ARM64; macOS **26.6.2 / 25G83**.
- Xcode **27.0 / 27A266a** from `/Applications/Xcode.app/Contents/Developer`.
- Apple clang **21.0.0 / clang-2100.3.34.2**.
- SDK **27.0 / 26A425**, independently checked against its SystemVersion plist.
- Python **3.14.7**, linked SQLite **3.53.4**, native Tk binding **9.0**.
- Exit **1**, `Publisher metadata required`; Apple credential lookup and notary
  validation were not part of this unsigned invocation. It stopped before GUI
  preflight, dependency synchronization or release compilation.

The production native options were also exercised directly in the isolated
`build/devsh-validation/native-26` CMake directory, with empty vault team ID.
Both neural and vault dylibs built successfully. `vtool -show-build` reported
`platform MACOS`, `minos 26.0`, `sdk 27.0`; `lipo` reported `arm64`.
The unchanged vault code emitted a deprecated `kSecUseAuthenticationUIFail` warning.
No vault operation, real account read or trade was executed.

Native binary SHA-256 (local build evidence, **not** production attestations):

- Neural: `16e5d3b7f88ccaf17038441f65d629c8798abad66d5500e2d50261a70038231f`
- Vault: `7dde5c6f4ae1b14e86f387ec51682d8f727ed85d9e392f5acb6189b8b3e90501`

The scientific source pin remains
`e2d4d584f4633613277bb1bf6ea7ba20855c01a332ae2e9344ed7782604c8509`.
The retained graph contract remains 166,700 neurons / 25,582,938 edges. This change
did **not** run the graph, acquire graph acceptance evidence or alter its locks.
A macOS 26.6.2/M4 run does not establish minimum-runtime macOS 26.0 compatibility.

## Validation

Evidence logs are under ignored `build/devsh-validation/`; counts below belong to
this session, not the earlier refactor report.

- Focused launcher/build/profile/kernel-selection/identity suite: **179 passed**.
- Bash syntax and Ruff on changed implementation/test files: passed.
- Whole-checkout baseline Ruff: **23 findings**, predating these edits. Whole
  source mypy: **1 existing error**, `strategy/capital.py:48`, `no-any-return`.
  These files were not reformatted or altered to conceal the failures.
- First full pytest run in the sandbox: **1,137 passed, 8 failed, 22 skipped**,
  210 subtests passed. Eight IPC skips reflected sandbox socket restrictions;
  twelve UI skips reflected the default opt-in setting. The remaining skips were
  the below-minimum SQLite fixture and Windows-only native API test. This is not
  native acceptance.
- A full `--native-ui` rerun outside the sandbox was interrupted after stalling
  in the native UI portion; it did not complete and supplies no passing UI verdict.
  A focused rerun of the known runtime failures with native IPC access reported
  **21 passed, 7 failed**, 7 subtests passed. The database-lock diagnostic IPC test
  passed outside the sandbox. See `runtime-failures.txt` for the actual results.

Source failures include outdated `TargetWeights` construction in agent tests,
the packaged-schema doctor's changed error contract, database-startup expectations,
and a supervisor test referencing the removed `_recover_paper_context` hook.
They concern pre-existing refactor changes outside this build-input update.
No tests were skipped, weakened or disabled to make a production build pass.

## Remaining production work and runner requirements

The issue ledger's unresolved source integrations, full frozen acceptance on each
mode, minimum-runtime evidence, role-specific entitlements, native dependency/load
path audit, complete dependency/native SBOM/notices, and robust notary submission
reconciliation remain required. Existing live enrollment, derivatives and neural
isolation gaps were not redesigned by this signing-input change.

Use a trusted, logged-in **M4** release runner with the exact profile above and the
same `dev.sh` commands. Supply reviewed devsolux public publisher inputs in
`~/.devsolux/coinfly` (or pass `--dataset-distribution DIR`), independently of
Apple signing inputs. Unsigned jobs must receive no Apple credentials. A protected
signed job may receive only separately authorized signing references; untrusted PR
jobs must never receive them. A separate macOS **26.0 ARM64** target must exercise
each actual artifact, with evidence bound to its bytes. The current source CI
matrix is not such a release/target-acceptance job and was not reported as one.

There was no Developer ID signing, notarization, release upload, account credential read, live
arming, exchange order, cancellation, transfer, service installation, commit or push.

References checked for implementation guidance:
[Apple Xcode host requirements](https://developer.apple.com/xcode/system-requirements),
[PyInstaller macOS signing](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing),
[uv lock/sync semantics](https://docs.astral.sh/uv/concepts/projects/sync/).
These sources describe tooling; they provide no evidence that Coinfly passed it.
