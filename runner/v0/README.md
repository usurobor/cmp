# Slicer Box runner — v0 (minimal)

Implements GitHub issue #5 against design of record `docs/design/box-job-admission.md`
**v0.6.0**. An unprivileged poller pulls a git queue ref, admits **one fixed**
`slicer` job against a box-local registry, launches it sandboxed via `systemd-run`,
and publishes an observable status ref. No inbound execution channel; git supplies
only `{id, mode, n}`, never a command.

## Files

| File | Role | Installed to |
|---|---|---|
| `poller.py` | the poller (single stdlib-only module) | `/opt/cmp/runner/v0/poller.py` |
| `slicer_run.py` | the one fixed Slicer entrypoint the runner launches | `/opt/cmp/bin/slicer-run` |
| `registry.yaml` | EXAMPLE single-entry registry (shape only) | `/etc/cmp-jobs/registry.yaml` (root-owned, **not in any repo**) |
| `poller.example.json` | EXAMPLE poller config | `/etc/cmp-jobs/poller.json` |
| `cmp-poller.service` / `.timer` | EXAMPLE systemd units (60 s) | `/etc/systemd/system/` |
| `tests/test_ac2_ac8.py` | fixture tests for AC2 + AC8 (no real systemd) | — |

## JSON shapes

**Request** — `requests/<id>.json` on `refs/heads/jobs/queue`, written by δ. Carries
nothing else; every path/user/limit is chosen by the box registry, never here:

```json
{ "id": "job-2026-08-05-01", "mode": "sample", "n": 20000 }
```

- `id`  — `^[A-Za-z0-9._-]{1,64}$` (used as the `cmpjob-<id>` unit + marker + status key)
- `mode` — must be one of the registry `modes` (`sample` | `full`)
- `n`   — `int`, `1 <= n <= registry.n_max`
- **any other key ⇒ rejected** (a `{"job": "/bin/sh"}` request never selects a command)

**Status** — force-pushed to `refs/heads/jobs/status` (mutable, single-writer, no history):

`runner.json` (heartbeat — a stale one means the box/poller is down):
```json
{ "observed_at": 1754434800, "health": "OK", "active": ["cmpjob-job-2026-08-05-01"] }
```
`health` is `OK` or `FETCH-FAIL`; `active` is the running units or `null`.

`status/<id>.json` (per-request, one of):
```json
{ "id": "...", "state": "rejected",  "reason": "n-out-of-bounds", "observed_at": 1754434800 }
{ "id": "...", "state": "launched",  "unit": "cmpjob-...",        "observed_at": 1754434800 }
{ "id": "...", "state": "queued",    "reason": "job-active",      "observed_at": 1754434800 }
{ "id": "...", "state": "running",   "unit": "cmpjob-...",        "observed_at": 1754434800 }
{ "id": "...", "state": "succeeded", "unit": "cmpjob-...", "result": "success", "exit": "0", "observed_at": 1754434800 }
{ "id": "...", "state": "failed",    "unit": "cmpjob-...", "result": "oom-kill", "exit": "137", "observed_at": 1754434800 }
```
Terminal state is derived from the **unit** (`systemctl show`), never self-declared by the job.

**Slicer receipt** — `receipt/<id>.json` (local; deterministic terminal artifact):
```json
{ "id": "...", "mode": "sample", "rows": 20000, "sha256": "…", "peak_rss_bytes": 13971456, "elapsed_s": 4.12 }
```
Plus an atomic `progress/<id>.json` (`{id, rows, target}`) written every 1000 rows.

## Install / run story (operator)

1. `useradd -r cmppoll` (poller) and `useradd -r cmpjob` (job run-user, no shell/keys/sudo).
2. Install `poller.py` → `/opt/cmp/runner/v0/`, `slicer_run.py` → `/opt/cmp/bin/slicer-run`.
3. Write `/etc/cmp-jobs/registry.yaml` (root:root 0644) from `registry.yaml`; set the real
   `dataset` path and limits. **This file is never committed to any repo.**
4. Write `/etc/cmp-jobs/poller.json` from `poller.example.json`; init the fetch mirror
   (`git init --bare /opt/cmp/runner/mirror` + set `origin`).
5. Grant `cmppoll` a narrow polkit rule to start/inspect `cmpjob-*` units only.
6. Install `cmp-poller.service` + `cmp-poller.timer`; `systemctl enable --now cmp-poller.timer`.

Each tick: fetch `jobs/queue` → validate `{id,mode,n}` → (if admitted and no job active)
`O_EXCL launched/<id>` marker → `systemd-run cmpjob-<id>` (non-root, `MemoryMax`/
`MemorySwapMax=0`/`CPUQuota`/`RuntimeMaxSec`/`IPAddressDeny=any`) → force-update `jobs/status`.
On restart a still-running `cmpjob-<id>` is **adopted** via `systemctl show` (never killed
or relaunched); a re-posted `id` never launches twice (marker); Slicer's own resume
recomputes nothing.

## Tests

`python3 runner/v0/tests/test_ac2_ac8.py` — the only two ACs provable without real systemd:

- **AC2** — malformed / unknown-mode / out-of-bounds / extra-field requests are rejected to
  status with a fake launcher asserted **never** called; a valid request proves the launcher
  *does* fire (so the assertion is not vacuously green) and a duplicate `id` is suppressed.
- **AC8** — parses every `.github/workflows/*.yml` push-branch filter and asserts none admit a
  `jobs/*` ref (nor a `**`/`*` wildcard). `jobs/queue`/`jobs/status` trigger **zero** workflows.

AC1, AC3–AC7 need real systemd/cgroups and are proven at operator-authorized on-box staging
(sibling issue #6), **not** claimed here.

## Retained from the parked prototype (`runner/`) + why

Reused only where it makes v0 *smaller or safer*; the heavy machinery was dropped as YAGNI
per `threads/rca/2026-08-04-runner-overengineering.md`.

- **`systemd-run` argv shape** (from `cmp_launch.py:SystemdBackend.start`) — the exact
  `--collect --uid --property=MemoryMax/MemorySwapMax=0/CPUQuota/RuntimeMaxSec/IPAddressDeny=any`
  invocation. *Safer:* a vetted sandbox arg list rather than re-deriving it.
- **Atomic write pattern** (`_atomic_json`: tmp + `fsync` + `os.replace`) — for the status,
  progress, and receipt files. *Safer:* no torn reads by δ or on crash.
- **Terminal classification** (`classify_terminal`) — collapsed to the tiny `observe()`:
  success ⇔ `Result=success` ∧ `ExecMainStatus=0`, else failed. *Smaller:* the memcg/timeout
  reason codes were dropped (on-box AC-R2 concern, not fixture-level).
- **`O_EXCL` launch marker + single-active gate** (idea from `admissiond.py` FIFO/marker) —
  kept as one durable marker + one `list-units` check. *Smaller:* the SQLite ledger/outbox and
  the whole `cmp_admitd` verb boundary were removed; at-most-once borrows Slicer's resume (L2).
- **Queue read = added `requests/*.json` blobs at the tip** (from `refs.added_paths`/`read_blob`)
  — simplified to a flat `ls-tree`/`show`. *Smaller:* dropped signed-object verification
  (`verify_commit`, box signers, anti-rollback cursor) — out of scope for a single-δ private repo.

**Dropped entirely** (issue #5 "Out of scope"): signed-object transport, anti-rollback,
`cmp_admitd` verb API, SQLite ledger/outbox, remote-ack, FIFO-in-privileged-state,
compromised-poller hardening, generic multi-job registry, retries.

## Known gaps (honest)

- **Line budget:** poller is ~125 core lines vs the ~100 target (slicer +25). The overage is
  irreducible stdlib effect-shell (git plumbing to force-update the status ref, `systemd-run`
  arg construction, git queue reading), **not** added mechanism — the five load-bearing pieces
  only, no item from #5's out-of-scope list. The threat model did not grow.
- **`registry.yaml` parsed as a flat mapping** (a YAML subset) because the poller is
  stdlib-only (no `yaml` module). The single-entry registry needs nothing more; a nested
  multi-job registry is explicitly out of scope.
- **AC1, AC3–AC7 unproven here** — they require real systemd/cgroups and belong to the
  operator on-box staging run (sibling issue). This module does not claim them.
- **`publish`/`systemd_launch`/fetch are effect-only** and exercised at fixture level only
  through the injected-launcher seam; their real behavior is an on-box concern.
