# Coinfly 1.0.0 implementation status

This table describes the refactored source, not a claim of completed native or
live qualification. The [current-session evidence](evidence-matrix.md) distinguish implementation from
execution. The application remains **1.0.0** and uses a fresh-install format.

| Area | Implemented source | Remaining work / qualification |
| --- | --- | --- |
| Desktop targets | Portable `dev.py`/PowerShell launcher; macOS ARM64, Windows/Linux AMD64 selection; existing EN/DE desktop; expanded autonomous strategy controls | Native GUI runs and packaged smoke tests on every target; Linux desktop vault/session testing |
| IPC | Unix local transport; Windows named-pipe transport with explicit user ACL, peer verification and bounded overlapped I/O; one platform dispatcher | Win32 native round-trip/security/stress tests on Windows; signed production worker identity qualification |
| Full MaleCNS | Preserved graph locks, scientific manifests, decoder/plasticity and original C++ source; explicit Linux/Windows/macOS kernel build/verification | Full verified graph and target throughput/RSS/soak testing; production binary attestation |
| Strategy automation | Existing worker lifecycle plus configurable automatic universe/slot plan; baseline/frozen checkpoint choices; complete plan digest; bounded deferred-frame cleanup and fair scheduling | Separate NeuralHost process; model registry/promotion workflow; real full-graph desktop acceptance |
| Capital and actions | Strategy allocation boundary in sizing/admission/dispatch; explicit affected-instrument scope prevents unrelated rebalancing; exact reservations/fees | Full integrated Writer/WAL and end-to-end tests under the locked runtime; complete derivative funding/settlement effects on strategy NAV |
| Fresh schema | One root `storage/schema.sql`; final component DDL and digest/structure validation; current contracts reopen; legacy/unknown formats reject | End-to-end fresh startup/reopen on supported linked SQLite; no legacy conversion is supplied |
| Paper | Existing stateful exact ledger/fills/drain/research boundaries, integrated automatic slots and scoped planning | Full-graph replay/realtime paper demonstration and long-running recovery tests in the qualified runtime |
| Live spot composition | Positive trusted-service transport factory, supervisor dependency injection, context recovery, independent account/order/fill maintenance, scoped pagination and live mandate checks | Production enrollment/evidence provider and release artifact supply; actual account/route qualification; remaining blocking-send architecture |
| Cancellation and account truth | A cancel acknowledgement does not release reservations; reconciliation establishes terminal facts; incomplete account evidence invalidates readiness; cash subtotal labeled as cash | Real venue qualification, scope/placement coverage verification and worst-case reconciliation testing |
| Regional/derivative routing | Existing capability resolver and distinct product contracts; existing synthetic paper derivative model | Germany/Global entitlement evidence, route verification, live derivative gateway/engine and exact venue economics |
| Secrets | Explicit Keychain, Credential Manager and Linux Secret Service setup backends; no plaintext fallback | Linux native execution-vault bridge; genuine signing/enrollment qualification on all targets |
| Packaging and CI | Existing macOS signing builder; common desktop/worker entry point; Windows/Linux paper development spec; three-target CI triggers | Actual locked CI runs, signed Windows/Linux production installers and platform-specific native qualification |

`python dev.py run` is explicitly local paper development. A verified dataset,
local kernel, stored credential or successful read-only connection test does not
qualify live execution. The default Live path still requires real trusted
composition and independently verified release/account authority.

A separately authorized live qualification exercise has not been performed.
There is no claim that all work packages in the previous refactor plan are complete.
