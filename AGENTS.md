# 🤖 Antigravity Workspace Guidelines: Homelab Core & Infrastructure

This repository is the central control plane for the `roadtotech.me` homelab cluster. Follow these operational rules across all tasks.

---

## 🏛️ Architectural Invariants

1. **Decentralized Manifests (`app.yaml`)**:
   - Never hardcode application environment variables, domains, or port maps inside `Core/scripts/appctl` or `Core/scripts/appctl_engine.py`.
   - Every application in `~/Sites` is self-describing via its own `app.yaml`.
2. **Read-Only Socket Proxy**:
   - `socket-proxy` must remain restricted to read-only endpoints (`POST=0`, `DELETE=0`).
3. **Zero Hardcoded Secrets**:
   - Never commit raw `.env` files, password strings, database files (`*.db`, `*.sqlite3`), or SSL certificates (`acme.json`, `*.key`).
   - All runtime secrets are managed declaratively in `~/Config/hosts/desktop/secrets.yaml` via SOPS and rendered to `/run/secrets/rendered/`.
4. **License Invariant**:
   - Preserve and maintain `The Unlicense` (public domain) across all Core and Sites repositories.

---

## 📚 Brain Vault & Continuous Learning

* **Knowledge Capture**: Whenever architectural designs, threat analyses, debugging insights, or `/learn` commands are executed, ensure a clear, structured markdown note is persisted to [`/home/kiskaadee/Brain/homelab/`](/home/kiskaadee/Brain/homelab) or [`/home/kiskaadee/Brain/learning/`](/home/kiskaadee/Brain/learning) and indexed in its corresponding `README.md`.
* **Obsidian & Docs Viewer Compatibility**: All documentation must be clean GitHub-flavored markdown compatible with Obsidian and the live docs viewer at `https://docs.roadtotech.me`.
