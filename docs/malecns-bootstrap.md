# Neural preservation and authenticated dataset readiness

The four scientific JSON manifests and native kernel source under
`src/coinfly/research` retain their pinned hashes. MaleCNS v1.0 identifies the
upstream dataset; Coinfly 1.0.0 identifies the application. Model profile IDs and
study schema versions are separate scientific and persistence contracts.

The operational neural code retains the full graph, fixed sensory transforms,
DNp20/DNpe017 decoder, two identified dopamine compartments and centered temporal
plasticity rule. The scientific profile is `coinfly-dual-compartment-v1`.
It accepts explicit `NeuralPaths` dataset and `VerifiedNativeKernel`
capabilities. It does not read environment variables, discover another checkout,
compile native code at runtime, download code, or import pickle. Scientific
parameter changes require a separate qualified profile.

The target-weight mapping is separately named `fixed-decoder-spot-step-v1`.
BUY increases only the selected instrument's previous target by at most 0.10 of
strategy weight, using unallocated weight; SELL decreases only that instrument's
target by at most 0.10. HOLD and an exhausted permitted change emit no proposal.
An explicit sell-to-zero emits the complete zero target vector. The mapping does
not inspect prices, balances, P&L, margin, signer state or broker routes. It never
sells a different asset to make room. Perpetual neural allocation is unavailable
until its own mapping is declared and qualified. This is an engineered mapping,
with no demonstrated profitable learning.

`MemoryBrain.restore` first bounds ZIP/NPY expansion and validates exact array
layouts. Before copying any state into a native array it checks active-list and
flag agreement, queue occupancy and neuron indices, refractory/trace clocks,
finite bounded state, fixed synaptic weights and plastic efficacy bounds.
Provenance checks still bind graph identities, endpoints, original weights,
sensory geometry, compartment assignments, rule source and native build. Old
checkpoints are not automatically migrated through changed provenance.

Dataset readiness requires a deployment-pinned Ed25519 `TrustRoot` and a signed,
unexpired `DatasetManifest`. No real publisher key or release was supplied, so
normal bootstrap returns `PUBLISHER_TRUST_ROOT_MISSING`. Providing a trust root
without metadata yields `SIGNED_MANIFEST_MISSING`. Tests create independent
ephemeral test keys; they are never installed as a publisher root.

## Packaged publisher metadata

The application publisher is **devsolux**. macOS production builds read reviewed
`trust-root.json` and `manifest.json` from the native account's
`~/.devsolux/coinfly` data root by default. `--dataset-distribution DIR` selects
another build input directory. The publisher name is application metadata;
signature verification still requires an independently reviewed public key.
Dataset caches remain under this root's `datasets/malecns/` subdirectories.

Production builds reject missing or development metadata. Explicit development
packaging uses a distinct descriptor pair and the existing `LOCAL_SOURCE_LOCKS`
bootstrap with isolated SANDBOX/PAPER execution. It creates no publisher keys,
signatures, dataset readiness or live attestation. See
[desktop packaging](../packaging/README.md).

Release startup now loads `coinfly/bootstrap/publisher/trust-root.json` and
`manifest.json` from the application package. Frozen builds use the absolute
PyInstaller `sys._MEIPASS` root; source/wheel builds use the module's absolute
directory. There is no runtime search of the working directory, user storage,
environment or network for a trust root. OS code signing seals packaged resources;
an Apple/Windows signing certificate is not an Ed25519 dataset publisher key.

The public root JSON has `version` (positive integer), `keys` (objects containing
`key_id` and a 64-character lowercase `public_key_hex`), `threshold`,
`allowed_origins` (HTTPS origins), `expires_at` (timezone-qualified timestamp),
and optional `minimum_manifest_version` (default 1). Unknown and duplicate fields
are rejected. Both files have bounded sizes. The root must be independently
reviewed before inclusion in the signed application. No private keys belong in
either file. The second file is the signed envelope described below.

The release builder requires both files, validates its staged copy, embeds it
before signing, and verifies the frozen loader through `doctor`. Runtime repeats
verification on the installer thread before starting the existing resumable
HTTPS stream. Missing resources produce stable diagnostics; invalid metadata
never starts a download. `SIGNED_DATASET_DISTRIBUTION` in doctor means the
packaged metadata verifies; only installer READY establishes data readiness.
Changing metadata requires a reviewed new package. Rollback/version floors and
expiry still apply; copying old metadata is not a recovery mechanism.

## Verification and activation

The signed envelope has exactly `signed` and `signatures` fields. Signatures cover
canonical UTF-8 JSON of `signed` with sorted keys, compact separators, ASCII
escaping and no NaN/Infinity. Distinct pinned keys must meet the root threshold.
The signed manifest contains schema version 1, a monotonic release version,
`malecns-v1.0`, the preserved model profile, issued/expiry timestamps and exactly
five assets: the three original Feather sources, `graph.npz`, and
`normalized/neurons.feather`. Every asset carries its path, HTTPS URL, exact byte
length and SHA-256. Source lengths and digests must additionally equal the
original committed locks. Signed metadata cannot replace those source hashes.

`DatasetInstaller` runs under an owner-restricted per-user root at
`~/.devsolux/coinfly/datasets/malecns/signed/`.
Application composition can inject the native OS home path;
tests inject isolated temporary homes. The installer serializes processes with
an OS file lock, persists the highest authenticated root/manifest version, and
rejects rollback and same-version metadata equivocation. The root itself is
provisioned through a trusted release mechanism; this component does not invent
an online root-rotation service. Restoring an entire older user directory also
restores local rollback memory, so deployment root version floors and expiry
remain necessary during host recovery.

Downloads use an explicit CA bundle, empty proxy configuration, bounded reads,
identity encoding, validated resume ranges and no redirects. Only the five raw
data paths are accepted; there is no archive extraction or executable download.
Temporary files stay in a digest-specific staging directory. All raw file
lengths/hashes, original graph array digests, exact graph shapes/types, normalized
transmitter digest and exact integer neuron order must verify before an atomic
directory rename activates `<release>/<manifest_digest>/`. Missing, changed,
partial, symlinked, hardlinked or unsafe data fails closed. A previously activated
cache can be used offline only after its signed metadata and all data are
reverified under a current provisioned trust root.

`BootstrapManager.check()`, `ensure()` and `install()` perform installer construction,
disk and network work on a background thread. Startup calls `ensure()` to verify
the cache and automatically install missing assets. `check()` remains an offline
cache check; invalid cached data, missing trust roots and missing signed metadata
do not trigger an automatic replacement or a weaker verification profile.
The source installer can also initialize the complete application layout itself
when used with its default paths, without relying on a prior desktop launch.

Both installers share a three-attempt transfer routine with cancellable backoff.
Truncated streams and transient network failures resume bounded partial files;
filesystem failures surface as storage diagnostics instead of network retries.
Downloads still require exact byte lengths and SHA-256 before activation.
Progress includes state, asset, completed/total bytes, throughput and retries;
storage failures also include a readable detail and a doctor-format stderr report.
Progress is immutable and coalesced through one pending
thread-to-loop callback and one latest-value queue per subscriber. Pause, cancel
and resource-pressure controls are cooperative. These operations own no ledger,
execution engine, fill consumer or guardian. A completed transfer remains
unready until verification and activation finish. Dataset READY attests only the
data; native neural execution also requires an independently attested prebuilt
binary on the qualified macOS ARM64 or Windows AMD64 host.

The automated checks cover cryptographic tampering, root/manifest failure,
rollback/equivocation, exact source locks, forbidden paths/origins, resumable
download behavior, bounds, failed activation, cached hash rechecks, asynchronous
cancellation, original neural rules and native checkpoint index validation.
Small installer unit fixtures explicitly bypass only the large-data fixture
construction step; the production manifest verifier rejects their artificial
source hashes. The local development bootstrap prepares and verifies all 166,700 retained
neurons and 25,582,938 edges using source and derived-array locks. It issues a
distinct `LOCAL_SOURCE_LOCKS` capability and enables isolated paper controls.
It does not issue publisher or native-kernel certification. See
[development setup](desktop-initialization-and-settings.md#local-development).
Synthetic unit fixtures do not establish full-graph execution performance or
profitable learning.
