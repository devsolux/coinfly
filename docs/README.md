# Coinfly 1.0.0 documentation

## Development and operation

- [Current-session validation evidence](evidence-matrix.md)
- [Desktop setup, credentials and application storage](desktop-initialization-and-settings.md)
- [Implemented capabilities and remaining gates](implementation-status.md)
- [Verification and package builds](validation.md)
- [Native release assembly](../packaging/README.md)
- [Production build commands and current validation](devsh-production-builds.md)
- [Context switching, recovery and late fills](runbooks/context-switch-and-late-fill.md)
- [Regional attestation and incomplete balances](runbooks/regional-attestation-and-balance-gaps.md)
- [Initialization root cause and recovery](runbooks/initialization-root-cause.md)
- [Paper startup without credentials](runbooks/paper-startup.md)
- [Live arming workflow and external prerequisites](runbooks/live-arming.md)
- [Worker-owned autonomous lifecycle](autonomous-lifecycle.md)

## Venues

- [Coinbase route verification (2026-09-21)](venues/coinbase-route-verification.md)
- [Capability matrix](venues/capability-matrix.md)

## Scientific model

- [Full-graph provenance and dataset bootstrap](malecns-bootstrap.md)
- [Experimental training and evaluation](malecns-training.md)
- [Reinforcement, credit capacity and study objectives](malecns-capacity-fitness.md)
- [KC→MBON plasticity kernel](malecns-rstdp-kernel.md)

## Architecture and boundaries

- [Safety invariants and supporting tests](requirements/invariants.md)
- [Exclusive execution context](adr/0005-exclusive-engine-context.md)
- [Environment and region separation](adr/0006-environment-and-region-realms.md)
- [Balance coverage and valuation](adr/0007-balance-coverage-and-valuation.md)
- [Native credentials and live gates](security/native-and-live-gates.md)

Application version 1.0.0, upstream MaleCNS v1.0, neural profile identifiers and
persistence schema versions identify different contracts. They are not interchangeable.
