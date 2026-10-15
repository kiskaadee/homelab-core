---
type: decision
status: accepted
project: homelab-core
date: 2026-10-15
tags: [architecture, appctl, validation, pydantic, security, api]
---

# ADR 0003: Centralized Runtime Validation & Serialization via Pydantic v2

## Context and Problem Statement

As Homelab Core transitions from decoupled CLI scripts into an authoritative operational control plane (`homelab-appctl.service`, Milestone P4), the system must process untrusted inputs across two distinct ingestion boundaries:
1. Declarative workload manifests (`app.yaml`) authored in `~/Sites` by independent workloads or external repository webhooks.
2. Command-oriented API requests sent across dual transports (host Unix Domain Socket `/run/homelab/appctl.sock` and network HTTP listener).

Previously, manifest parsing relied on Python standard library dataclasses combined with manual regex checking and dictionary traversals in `scripts/core_manifest.py`. While functional for basic validation, manual ad-hoc validation created subtle security and maintainability risks:
- Input parsing logic was duplicated across `core_manifest.py`, `appctl_engine.py`, and `gitops_dispatcher.py`.
- Validation errors varied in structure and could inadvertently reflect unvalidated raw payloads (potentially leaking secrets or host paths).
- Future daemon API endpoints would require writing independent request/response serialization, type-checking, and error-handling routines for every operation.

The central architectural question is: **How should the control plane enforce strict, fail-closed type safety and consistent serialization across workload manifests and daemon API endpoints without duplicating validation logic or leaking secrets?**

## Decision Drivers

* **Fail-Closed Type Safety**: Unrecognized fields, unexpected types, or invalid operations must be rejected immediately at the transport boundary before reaching the operational router.
* **Unified Contract Enforcement**: Workload manifests (`app.yaml`) and control plane API payloads must share a common, declarative schema definition rather than separate parser implementations.
* **Prevention of Information Disclosure**: Error payloads returned to clients or logged to audit streams must describe schema violations without echoing raw input values, environment variables, or credentials.
* **Dual-Transport Parity**: The same validated contract must govern both local Unix Domain Socket calls (CLI) and remote HTTP calls (GitOps dispatcher, Web UI).
* **Maintainability & Performance**: Minimizing bespoke parsing code while maintaining fast validation overhead suitable for daemon and CLI operations.

## Considered Options

* **Option 1: Standard Library Dataclasses with Custom Manual Validators**: Continue relying solely on Python `dataclasses` and manual validation functions (`isinstance`, regexes).
* **Option 2: JSON Schema (`jsonschema`) with Manual Object Deserialization**: Use JSON Schema definitions to validate raw dicts, then manually map validated dicts to dataclasses.
* **Option 3: Pydantic v2 with Strict Mode & Fail-Closed Configuration (Adopted)**: Introduce Pydantic v2 as the authoritative validation and serialization engine across manifests and API models.

## Decision Outcome

Chosen option: **Option 3: Pydantic v2 with Strict Mode & Fail-Closed Configuration**, because:

> *"We opted for centralizing runtime validation and serialization across workload manifest and daemon API around Pydantic models to enforce fail-closed type safety, strict schema-based contract boundaries and consistent serialization and error handling without leaking secrets."*

### Summary of Architectural Commitments

1. **Single Schema & Validation Layer**:
   All domain entities—including workload manifest configurations (`SourceConfig`, `DeploymentConfig`, `HomepageConfig`, `Manifest`) and control plane API command payloads (requests, responses, errors)—are defined exclusively as Pydantic v2 models inheriting from `pydantic.BaseModel`.
2. **Fail-Closed Schema Boundaries (`extra="forbid"`)**:
   Every model enforces `ConfigDict(extra="forbid")`. Any payload containing unexpected, undocumented, or extraneous fields is rejected at the transport boundary with zero execution.
3. **Strict Typing Policy (`strict=True`)**:
   To prevent unexpected coercion bugs (e.g. string `"false"` coercing to boolean `True`, or floats coercing to ints), API operations, target identifiers, flags (`recreate`, `remove_orphans`, `visible`, `auth`), and tokens enforce strict types. Explicit field validators handle string trimming/normalization where intentional.
4. **Information Disclosure Prevention**:
   Transport adapters intercept `pydantic.ValidationError` and map it into structured `ERR_VALIDATION_FAILED` (HTTP 400) responses. Error messages report violating field names and failure rules, but strictly sanitize and redact raw input values to prevent leaking secrets, credentials, or filesystem paths.
5. **Dual-Transport Parity**:
   The identical Pydantic request and response models govern both Unix domain socket messages and HTTP API endpoints. Neither transport performs custom validation outside the shared Pydantic models.
6. **Host Runtime Alignment**:
   Pydantic is packaged into the NixOS system environment (`nixos/configuration.nix`), systemd unit paths (`nixos/modules/homeserver.nix`), Nix flake development shells, and repository test dependencies (`pyproject.toml`) constrained to major version range `>=2.0,<3.0`.

## Pros and Cons of the Options

### Option 1: Standard Library Dataclasses with Custom Manual Validators
* Good, because it requires zero external dependencies and uses Python standard library modules.
* Bad, because validation code must be manually implemented for every field, nested structure, and API payload.
* Bad, because it is error-prone and historically led to parser duplication across `core_manifest.py`, `appctl_engine.py`, and `gitops_dispatcher.py`.
* Bad, because serialization and deserialization (e.g. JSON to model and back) require bespoke helper methods.

### Option 2: JSON Schema (`jsonschema`) with Manual Object Deserialization
* Good, because JSON Schema is a standardized language-agnostic specification.
* Bad, because it requires maintaining two separate artifacts for every entity: a JSON Schema document and a corresponding Python dataclass or class.
* Bad, because mapping validated JSON Schema dicts to Python typed objects adds runtime boilerplate and cognitive overhead.
* Bad, because error messages from `jsonschema` can be difficult to sanitize and format into uniform domain error structures.

### Option 3: Pydantic v2 with Strict Mode & Fail-Closed Configuration (Adopted)
* Good, because it provides declarative, unified model definitions that handle validation, type-checking, and JSON serialization in a single class.
* Good, because Pydantic v2's Rust core (`pydantic-core`) delivers high-performance validation with minimal latency impact on the daemon.
* Good, because built-in `ValidationError` metadata makes structured error mapping straightforward and consistent.
* Good, because `model_dump(mode="json")` and `model_validate_json()` provide deterministic, standardized serialization across dual transports.
* Bad, because it introduces a third-party dependency into the Core platform that must be maintained across NixOS packages, development shells, and CI pipelines (mitigated by locking `>=2.0,<3.0` in `pyproject.toml` and Nix flake checks).

## Consequences

### Positive Consequences
* Eliminates fragmented parsing code: all callers (`appctl_engine.py`, `gitops_dispatcher.py`, `homelab-appctl.service`) validate against the same authoritative schemas in `core_manifest.py`.
* Pre-P4 security posture is substantially hardened: unvalidated fields and type confusion attacks are blocked at ingestion.
* Clean separation of concerns: transport adapters focus on wire protocol handling while Pydantic guarantees valid domain objects for the operational router.
* Client error payloads are structured, actionable, and safe from secret leakage.

### Negative Consequences & Accepted Trade-offs
* Core depends on `pydantic` in host NixOS environment; all host Python environments executing `appctl` or `homelab-gitops` must have `python3Packages.pydantic` available.
* Schema evolution requires strict versioning: since `extra="forbid"` is enforced, extending manifests or API payloads with new fields requires coordinated model updates.

## More Information

* **Governing Readiness Plan**: [`docs/plans/p4-readiness-plan.md`](../plans/p4-readiness-plan.md) (Section 2.B)
* **Governing Roadmap**: [`ROADMAP.md`](../../ROADMAP.md) (Milestone P4)
* **Platform Security Invariants**: [`AGENTS.md`](../../AGENTS.md) (Invariants #1, #2, #3)
