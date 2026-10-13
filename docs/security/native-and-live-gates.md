# Native credentials and live capability gates

Coinfly 1.0.0 includes native vault bridge source, scoped signing code, a read-only
Advanced Trade account adapter, and adversarial boundary tests. This source
delivery does not include a signed native release or live exchange certification.
`AdapterFactory.availability(config)` exposes the closed gates to the worker and UI.

| Gate | Required external evidence | Source delivery |
|---|---|---|
| `NATIVE_SIGNING_UNCERTIFIED` | macOS ARM64/Windows AMD64 signed worker/bridge identity, binary hashes, native vault qualification | Unsatisfied |
| `REGION_UNVERIFIED` | Independently authenticated broker-issued, account-bound service entity and product eligibility evidence | Unsatisfied |
| `LIVE_GATEWAY_UNCERTIFIED` | Active account gateway, protocol, units, margin, order/protection and reconciliation certification | Unsatisfied |
| `MODEL_NATIVE_ISOLATION_UNCERTIFIED` | Signed native fresh-spawn helper with restricted handles, filesystem, network and scoped IPC | Unsatisfied |
| `MODEL_PROVIDER_GATEWAY_UNCERTIFIED` | Native-purpose provider credential enrollment and certified provider egress | Unsatisfied |

These gates are not operator configuration. No checkbox, environment variable,
manually edited JSON, keystore alias, `INTX` permission string, or unsigned profile
document turns them green. The compiled trust-anchor collections are deliberately
empty. The source now contains a positive trusted-service Live spot mutation
composition path, but the production enrollment/evidence provider is not supplied.
No default source launch is a qualified live release. A live
adapter also needs the authority's durable old-context STOPPED/retirement proof
and fresh context-bound arm grant at construction and send.

## Native vault implementation

`native/macos/keychain_bridge.mm` uses `SecItemAdd`, `SecItemCopyMatching` and
`SecItemDelete` in the Security Framework data-protection Keychain. Items are
non-synchronizing, device-local, and require unlocked access. Every operation
checks the signed worker identifier `com.devsolux.coinfly.worker` and the release
team identity. `native/windows/credential_bridge.cpp` uses `CredWriteW`,
`CredReadW`, and `CredDeleteW` for generic current-user credentials with
`CRED_PERSIST_LOCAL_MACHINE`. It verifies Authenticode and a release-pinned signer
certificate before every operation. Local-machine persistence here means the
same user on the same computer; it does not grant every local user access.
([Apple Keychain protection](https://support.apple.com/guide/security/keychain-data-protection-secb0694df1a/web),
[Microsoft CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw))

The Python factory accepts no secret filename or binary path. It locates a
packaged bridge, checks a release-pinned hash, then requires the native identity
check. Empty signing pins deny use. The native execution-vault bridge remains
unimplemented on Linux; this does not exclude Linux desktop paper operation or
its separately scoped Secret Service setup storage. All native execution bridges
require target-host release qualification. CMake is a
release-build tool only; application startup never runs a compiler.
Frozen bridge lookup starts at absolute `sys._MEIPASS`; resolved binaries must
remain inside that bundle root. Internal PyInstaller relocation links are
supported, external links are rejected. This corrects resource lookup without
turning an OS signature into worker/vault qualification.

Each native item embeds the SHA-256 digest of its immutable opaque binding,
environment, authority, verified-profile ID, portfolio, purpose and version.
Enrolling a new version creates a new item; it cannot overwrite another binding.
Stage/verify/activate receipts contain opaque references only. A failed staged
verification cannot sign. Staged records must be reconciled with their durable
nonsensitive enrollment receipts before activation after a crash.

Native bytes are borrowed inside the worker through an exact realm/purpose check.
Mutable buffers are wiped on success and exceptions. Python strings, library
copies and operating-system internals can retain transient copies; this source
does not claim universal locked RAM, Secure Enclave import, TPM nonexportability,
or erasure of every transient copy. Real native-host tests and crash-report
inspection remain release requirements.

## Signing, permissions and routing

The signer accepts ES256/P-256 material only, emits a request-specific JWT, and
binds its one-use local authorization to context, method, endpoint, query, body
digest, credential/profile versions and drain permission. Native access and the
worker's authority/permission gates run again at the actual HTTP owner before
send. `SigningGateway.send` holds the authority transition guard and signer lock
through synchronous request initiation; integrations must not defer the callback
to another thread. The issued registry retains the original digest/time/token
hash, and retirement invalidates pending authorizations. Coinbase's documented JWT
contract uses a two-minute lifetime and requires a different JWT for each unique
request. ([Coinbase API key authentication](https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication))

One canonical builder, `coinfly.security.coinbase_jwt.build_request_jwt`, mints
every Coinbase token in the repository: the certified `SigningGateway` path and
the desktop's explicit read-only connection test both call it, so there is a
single ES256 implementation with one set of claim tests (`kid`/`sub`, `iss`
`cdp`, `nbf`/`exp` = 120 s, hexadecimal `nonce`, `uri` = `METHOD host/path`,
raw 64-byte `r||s` signature). `load_es256_private_key` is the single PEM parser;
it classifies malformed PEM, non-EC, wrong-curve and Ed25519 material without
echoing it, and the certified path still collapses every failure to
`ES256_KEY_INVALID`. The desktop connection test uses the unbound keychain
setup pair, never NativeKeystore, never the worker, and its verified result
satisfies none of the gates in the table above.

`key_permissions` is parsed as `can_view`, `can_trade`, `can_transfer`,
`portfolio_uuid` and `portfolio_type`. Execution needs View+Trade, no Transfer and
the approved singleton portfolio. Observation needs View only and the approved
portfolio. The response does not establish jurisdiction or the service entity.
([Coinbase key permissions](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions))

`RegionAttestor` supplies an integration protocol without fabricating an account
eligibility endpoint. Its default denies live. The GERMANY application policy is
spot only; verified Coinbase Germany GmbH bindings also deny perpetuals under
GLOBAL. The current public EEA agreement names service-specific Luxembourg and
Ireland entities and applies to Germany from 23 October 2025; that public text
does not attest a particular account. ([Coinbase EEA agreement](https://www.coinbase.com/legal/eea/user_agreement_irlux))

Registered endpoints are immutable. The HTTP reader is GET only, uses the pinned
`certifi` CA bundle, constructs fresh connections, rejects redirects and arbitrary
query parameters, and has no SDK, proxy, netrc, cookie/session or automatic retry
path. No production perpetual hostname is fabricated: the published migration
guide still describes cutover as coming soon, so the active account gateway must
be independently certified. ([Coinbase derivatives overview](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/derivatives/overview))

## Read-only account and model boundaries

`AdvancedTradeReadOnlyAccounts` implements `AccountsReader.read_page`. It follows
documented cursors through the collector, rejects stalled/malformed pages and
unexpected portfolios, and preserves finite exact Decimal amounts and holds.
Only its enrolled wallet/fiat scope is reported. CDP account queries are limited
to their permissioned portfolio; finishing pagination is not proof that no other
portfolio or vault exists. ([Coinbase List Accounts](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts))

The static sandbox reader uses no credentials and cannot establish realistic
execution or margin behavior. Its endpoint is
`https://api-sandbox.coinbase.com/api/v3/brokerage`. Stateful execution belongs to
the separate paper broker. ([Coinbase sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox))

Models receive exactly `get_cached_features`, `get_cached_order_book`, and
`submit_target_weights`. Tool payloads reject extra context, environment, region,
equity, signer and margin fields. The runtime supplies the expected context epoch
to the trusted proposal sink. Proposal receipts say `PROPOSED`; they do not claim
that risk approved or executed a trade. No AgentKit wallet provider or provider
SDK is created by default. Native model isolation remains unavailable until its
OS sandbox is certified; a Python subprocess is not treated as a security sandbox.

The test suite uses synthetic identities, generated ephemeral test keys and
in-process fake transports. It performs no live trades or real credential reads.
Primary documentation was reviewed on 18 September 2026 and the API key
authentication, List Accounts and key permissions pages again on 21 September
2026; public API facts do not replace deployment qualification or account
authorization.

## Production distribution modes

`./dev.sh build` resolves its Developer ID and notary profile from the exact
`dev.sh` environment defaults; `./dev.sh build-unsigned` ignores those references.
Both require reviewed publisher metadata and the same product acceptance gates.
Mechanical ARM64 ad-hoc signatures confer no Developer ID/team, notarization or
financial authority. Unsigned production currently has no qualified bundled
kernel verification path; it must fail the relevant acceptance gate rather than
mint signed authority or use a local development kernel. Missing/invalid bundled
production kernels no longer fall back to `models/kernels` development candidates.
The signed provenance producer and complete enclosing-app/running-process
verification also remain release work. See [the issue ledger](../devsh-production-builds.md).
