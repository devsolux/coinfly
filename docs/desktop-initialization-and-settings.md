# Desktop initialization and settings

Coinfly uses CustomTkinter 6.0, an independent worker process and SQLite. MaleCNS
dataset verification uses an asyncio loop and a background installer thread.
`PUBLISHER_TRUST_ROOT_MISSING` is a terminal prerequisite failure, not evidence
of a SQLite deadlock. The source distribution still has no publisher trust root;
neither a retry nor a UI preference can manufacture one. This applies to the
publisher-signed `coinfly` entry point; local development has a separate profile.

## Local development

The canonical launcher is `python dev.py` on macOS ARM64, Windows AMD64 and
Linux AMD64. Run `setup`, `prepare`, `kernel`, then `run`; Windows also provides
`dev.ps1`. Native Python must link SQLite >=3.51.3 and include Tk. The explicit
`kernel` command builds a local research artifact; normal startup does not compile.
Windows uses authenticated named pipes rather than Unix sockets. The Win32
implementation and Linux Secret Service setup backend require native desktop
qualification; Linux native execution signing is still missing.


`./dev.sh run` opens the explicitly labeled local paper desktop. Its worker uses
`~/.devsolux/coinfly/storage/development/<checkout-id>/` and a checkout-specific Unix socket, so it cannot
silently attach to the signed-release worker or its ledger. `./dev.sh worker`
starts that development worker in the foreground. `./dev.sh prepare` performs
the same data preparation without a window; it exits 0 only after verification.
Generated application data lives outside the checkout under `~/.devsolux/coinfly/`,
which `./dev.sh clean` preserves.

The first startup obtains the three original sources (1,109,008,094 bytes) from
the URLs in `sources.lock.json`, using the existing bounded HTTPS downloader.
Each source length and SHA-256 must match before parsing. The preserved import
and sensory-projection algorithm rebuilds all 166,700 retained neurons and
25,582,938 edges, including weak and self edges. Every array must match
`arrays.lock.json`; normalized transmitter values and neuron order must also
verify. Only then is staging atomically activated and the dataset marked READY.
Subsequent startups reverify the cache offline. Allow at least 3.2 GB free disk
space and several GB of RAM for preparation. Downloads resume after interruption;
Resume continues a paused operation without starting a second installer.

This uses a distinct `LOCAL_SOURCE_LOCKS` capability. It creates no signing key,
publisher manifest or native-kernel attestation. The worker rejects non-paper
contexts independently of UI controls. The development desktop injects the
same OS-keychain setup store and read-only Coinbase connection tester as the
signed desktop (`coinfly.cli.desktop_credential_services`), so credentials can
be saved and authentication tested from `./dev.sh run`; neither action reaches
the development worker or changes its paper-only admission. Data READY enables
the paper controls; autonomous trading additionally requires the configured
worker strategy, verified native kernel, causal feed and execution admission.
READY by itself does not certify a kernel or mean that the strategy is running.
`coinfly` still requires publisher-signed assets, and `./dev.sh doctor` still
reports the separate release-qualification gates with exit 2 where appropriate.

Use `.venv/bin/coinfly` for the signed-release startup. A modified active dataset
fails closed instead of being overwritten. Keep a damaged cache for diagnosis
and move its digest directory aside before rerunning preparation.

## Application data on Linux, macOS and Windows

`ApplicationPaths` resolves the native account home: the account database on Linux/macOS
and `SHGetFolderPathW(CSIDL_PROFILE)` on Windows. It ignores `HOME`, `USERPROFILE`,
`APPDATA`, and the current working directory. The root is
`/Users/<user>/.devsolux/coinfly` on macOS or
`C:\Users\<user>\.devsolux\coinfly` on Windows (including profiles on another drive).
Explicit home injection uses `Path.expanduser().resolve()`; `Path("~")` or
`resolve()` alone does not expand a tilde. Default storage still uses the native
account profile rather than an environment override.

| Folder under the application root | Purpose |
| --- | --- |
| `storage/state.sqlite3` | Signed-release worker ledger |
| `ledger/` | Reserved ledger assets; no database migration or second ledger is created |
| `storage/development/<checkout-id>/state.sqlite3` | Separate local paper ledger per checkout |
| `datasets/malecns/source-locks/<lock-digest>/` | Verified local MaleCNS sources and derived graph, shared across checkouts |
| `datasets/malecns/signed/<release>/<manifest-digest>/` | Publisher-signed cache and persisted rollback metadata |
| `models/kernels/` | Location for independently attested native model binaries |
| `models/checkpoints/` | Location for explicitly saved model checkpoints |
| `runs/` | Default Optuna study database and research run outputs |
| `logs/`, `cache/` | Application logs and disposable application caches |
| `snapshots/` | Reserved application snapshots; existing model checkpoints stay under `models/checkpoints/` |
| `authority-locks/` | Signed-worker authority ownership locks |

Model loading/checkpoint APIs and user-selected report exports still take explicit
paths; this change does not auto-load binaries, create checkpoints or move explicit
output destinations. Development build/test/Python caches are not application data.
Short macOS Unix sockets stay in the private OS temporary directory because of the
Unix socket path limit. Windows IPC qualification is still a separate release gate;
portable storage support does not bypass it. Windows folders inherit native profile
permissions; POSIX directories require owner-only permissions. Managed symbolic
links and Windows reparse points/junctions are rejected.

Coinfly 1.0.0 uses this layout directly. It does not discover or import data from
other application roots or source checkouts. Fresh databases are initialized from
the final 1.0.0 schema; exactly matching current-format ledgers retain their
contents. Older or structurally modified ledgers fail without conversion. A fresh
checkout does not silently claim or upgrade an old development ledger.

## Startup and recovery

Desktop, development and worker entry points first prepare the complete private
application tree synchronously, before UI, database or worker threads start.
The desktop CLI and packaged worker entry point also defer importing UI/database
implementation modules until storage initialization succeeds.
Parent-first validation rejects managed links/junctions. Each directory uses
`mkdir(parents=True, exist_ok=True)` and a temporary write probe to detect ACL or
read-only mount failures even when a directory already exists. Creation is
idempotent, not a transaction over the entire tree: a failed attempt preserves
existing data and any directories already created, and a retry completes setup.
There is no fallback to the working directory or another user's storage.

Storage failures produce a redacted JSON `QualificationReport` on stderr, with
the same `checks`, `code`, `satisfied`, `detail`, `blocking_codes`, `status` and
`exit_code` contract as `coinfly doctor`. Permission denial, path collisions,
unsafe paths and other disk failures have distinct actionable explanations.
Doctor inspects the tree without creating it; startup additionally tests real
writes. Permission/setup failures exit 2. No database or UI is started after a
failed storage bootstrap. No existing folders are moved or schema migrated.

After storage is ready, the launcher opens the window before probing/launching the worker. DesktopClient
does that work on its observer thread. WorkerStartup opens the authenticated
local diagnostic endpoint before constructing WorkerSupervisor on a dedicated
thread. Startup snapshots need no database connection. Until construction and
recovery succeed, every financial command fails with `DATABASE_NOT_READY`.

Writer owns its SQLite connection on one thread. Schema creation runs in a
single transaction with short busy waits, retry notifications and a ten-second
deadline. SQLite BUSY/LOCKED results produce the visible message “Database locked
by another process - Retrying...” and a warning without private filesystem data.
A watchdog also bounds the visible startup state if a filesystem/constructor
call stalls; the same budget is forwarded to the Writer, so the lock/schema
deadline and the visible timeout expire together. On timeout, execution stays
unavailable and Settings remains usable. Retry cannot overlap an unfinished
constructor. Late results are disposed rather than silently activating the
worker. An interrupted constructor wait cancels the startup and sends the
writer thread its shutdown sentinel before joining, so a writer that became
ready during the interruption cannot be joined forever or keep the path owned.
Python threads are not forcibly terminated; an uninterruptible OS call may
outlive the visible timeout until the OS returns. The `authority-locks/` folder
is part of the prepared and inspected tree; a denied or occupied lock path is
reported as a storage diagnostic, not as a database failure.

Dataset progress has its own IPC notification, independent from ledger-backed
audit publication. The progress bar animates only during active stages, stops on
terminal failure and shows a translated explanation. A missing worker also gets
a terminal diagnostic after ten seconds instead of an endless checking indicator;
that ten-second budget restarts after every successful subscription, so a brief
worker restart later in the session is a reconnect, not a terminal diagnostic.
Setup is dismissible. Sidebar navigation hides its window, the header setup
button reopens it, and “Continue to history” opens the existing ledger browser.
History and settings require no live certification; dataset activation and
financial actions retain their own checks. Backtests remain available through
the existing local research tools with verified assets; no new backtest engine
or live authorization is implied by browsing history.

## Credentials

The settings form accepts a masked Coinbase API key name and an ECDSA P-256 PEM
secret. Paste preserves PEM newlines, including escaped `\n`. Syntax validation
runs immediately; cryptographic validation and vault I/O run on a dedicated
single-flight thread. Controls clear submitted values and never put them in
configuration, SQLite, IPC, render events or error messages.

The launcher injects a narrow credential-storage interface into the UI; the UI
does not import the worker, signer or vault implementation. `keyring==25.7.0`
uses explicitly selected macOS Keychain / Windows Credential Manager backends,
and an explicit Linux Secret Service backend, with no plaintext,
environment-selected or null backend fallback. Key and secret
are stored together under `com.devsolux.coinfly.coinbase.setup.LIVE`, account
`credential-pair-v1`. Saving replaces that setup pair. Status checking is explicit
to avoid prompting for vault access merely by opening the application. Python
and Tk may retain transient string copies; universal memory erasure is not claimed.

The green “Securely stored” badge means local storage succeeded and was read
back. Explicit status checks also parse the stored PEM and require ECDSA P-256.
“Check stored credentials” inspects only the keychain, never the form: with no
saved pair it reports `CREDENTIALS_NOT_STORED` (amber), which is distinct from
the form's `CREDENTIALS_MISSING`. On macOS the first read of the item from a
new process can raise the system keychain prompt for the Python binary; the
badge shows “Waiting for OS keychain access…” until that prompt is answered,
and “Deny” reports `OS_KEYRING_LOCKED_OR_DENIED`.
Empty replacement fields retain this green result after a save or check; editing
a replacement shows its validation independently. It does not attest broker
permissions, account identity or live execution.
These setup credentials are separate from NativeKeystore's certified immutable
portfolio-bound signing references. Live account enrollment remains unavailable
until the existing signed-worker, regional and gateway requirements are met.
The readiness panel uses the same `code` / `satisfied` fields and release gates
as `coinfly doctor`; it does not read real account balances to infer readiness.

### Four distinct credential states

| State | Meaning | Where it is established |
| --- | --- | --- |
| Syntactically valid (`CREDENTIAL_FORMAT_VALID`) | Key name matches `organizations/…/apiKeys/…`; the secret has PEM shape | `credential_input.py`, immediately in the form |
| Securely stored (`SECURELY_STORED`) | Written to and read back from the OS keychain; parsed as an ECDSA P-256 key | `DesktopCredentialStore`, on the single-flight keyring thread |
| Coinbase authentication verified (`COINBASE_AUTH_VERIFIED`) | One explicit, user-initiated, read-only authenticated request succeeded | `CoinbaseConnectionTester`, injected by the launcher |
| Coinfly live execution qualified | Signed worker, native signing, regional evidence, gateway and account enrollment gates all satisfied | `AdapterFactory.availability`, `coinfly doctor`; never derived from the states above |

None of these implies the next. In particular, verified Coinbase authentication
does not unlock live execution, does not mark any release gate satisfied and is
never read by the worker: the desktop keeps the remote status only as a label.

### Test Coinbase connection

“Test Coinbase connection” is an explicit button, available in both the signed
and the local development desktop. It never runs because Settings was opened. When clicked, the credential worker reads the stored pair
from the OS keychain, classifies the key material (malformed PEM, non-EC key,
wrong curve or Ed25519 each get their own redacted code), mints one
request-bound ES256 JWT through the canonical signer in
`coinfly.security.coinbase_jwt` (`kid`/`sub` = key name, `iss` `cdp`, `nbf` now,
`exp` now + 120 s, random `nonce`, `uri` `GET api.coinbase.com/api/v3/brokerage/accounts`)
and sends a single bounded `GET /api/v3/brokerage/accounts?limit=1` over TLS
1.2+ with the pinned `certifi` bundle, no proxy, no redirects and no retry.
The registered `coinbase-advanced-spot` endpoint profile is the only host.

The response body is parsed only to check that an `accounts` list came back and
is then discarded; balances, account identifiers, the key and the token never
reach logs, SQLite, IPC, render events or the UI. The verdict is one of:
`COINBASE_AUTH_VERIFIED`, `COINBASE_AUTH_REJECTED` (401),
`COINBASE_CLOCK_SKEW_SUSPECTED` (401 with a `Date` header more than 30 s from the
local clock), `COINBASE_AUTH_FORBIDDEN` (403), `COINBASE_RATE_LIMITED` (429),
`COINBASE_SERVER_ERROR` (5xx), `COINBASE_REQUEST_REJECTED`, `HTTP_REDIRECT_FORBIDDEN`,
`COINBASE_RESPONSE_MALFORMED`, `NETWORK_DNS_FAILURE`, `NETWORK_TLS_FAILURE`,
`NETWORK_TIMEOUT`, `NETWORK_UNREACHABLE`, `CONNECTION_TEST_CANCELLED`, the local
credential codes (`CREDENTIALS_NOT_STORED`, `CREDENTIALS_MISSING`, `COINBASE_KEY_FORMAT_INVALID`,
`COINBASE_SECRET_FORMAT_INVALID`, `COINBASE_KEY_NOT_EC`,
`COINBASE_KEY_CURVE_UNSUPPORTED`, `COINBASE_KEY_ED25519_UNSUPPORTED`,
`OS_KEYRING_UNAVAILABLE`, `OS_KEYRING_LOCKED_OR_DENIED`) or
`COINBASE_AUTH_UNKNOWN_FAILURE`. A success means only that Coinbase accepted this
credential for that read operation at that time; it does not establish trading
permission, portfolio scope or eligibility. No order is previewed, placed,
cancelled or simulated. Coinbase's environment-variable advice does not apply:
the OS keychain remains the only credential source.

Format and backend references: [Coinbase authentication](https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication)
(reviewed 21 September 2026: ES256/P-256 only, Ed25519 unsupported, 120-second
tokens, a new JWT per request), [List Accounts](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts)
and [keyring documentation](https://keyring.readthedocs.io/en/stable/).

## Environment handover

The mutually exclusive environment selector submits the existing revision-bound
preview/confirmation workflow. Effective environment changes only after the
worker has fenced admissions, drained the source, reconciled unknown orders,
retired source handles, reset transient state and initialized a fresh unarmed
target. Real source exits cannot be relabeled as paper trades during a switch.
Before first activation the effective-context status says “Not initialized”;
it does not claim that the default selection is an effective context. Connected
workers permit diagnostic previews while assets are missing. Confirmation stays
disabled until READY; the worker independently enforces this at apply and arm.
Live selection still explains the missing certified enrollment. Transition phase
or failure details replace generic enrollment text while a handover is pending.

Network adapters register resources with `coordinator.transports` using their
immutable ContextKey. BoundHttpsReader accepts `context=` and `transports=` and
checks the endpoint and signer binding. Retirement revokes further sends,
shuts down active sockets, retires signer handles and discards late responses.
ContextCoordinator requires transport retirement before resetting/initializing
the replacement. A failed close blocks the handover and preserves the admission
fence. Explicit retries may construct fresh target handles only after previous
target handles closed; retired objects themselves cannot be reused.

The paper engine owns no exchange sockets. Live mutation requires a separately
qualified adapter and the existing certification gates.

## Verification and recovery

Regression tests use real SQLite file locks and isolated local IPC, generated
ephemeral keys, a memory vault fixture and synthetic HTTP connections. Native
widget tests cover masking, validation, status and Settings access after failure.
No real keychain writes, external exchange requests or trades are used in tests.

Remove unused setup credentials through the OS credential manager. Never delete
the ledger to clear a lock. Close the competing process and retry initialization.
Only current versioned paper snapshots and exactly matching checkpoint provenance
are accepted. Older formats are rejected; no implicit conversions or cash
reconstruction occur during startup.
