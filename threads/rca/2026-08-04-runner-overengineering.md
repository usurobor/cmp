# RCA — box-runner over-engineering (KISS/YAGNI violation)

**Date:** 2026-08-04 · **Class:** blameless retro (cnos.eng `rca`) · **Trigger:**
operator (Axiom) flagged a KISS/YAGNI violation in `docs/design/box-job-admission.md`.

## Capture (neutral)

Goal: a box runner so δ can run Slicer on cn-sigma without the 2026-08-01 failure
modes (global OOM killing the box, orphaned processes, GitHub-Actions-as-root as
the inbound channel, lost work across aborted runs).

Over ~5 design versions (v0.2→v0.5.1) and 4 implementation rounds, the artifact
grew from a "~150-line poller" (its own §10 estimate) to **1023 core code-lines**
plus a 700+-line harness, accreting: git-native **signed-object transport**,
**anti-rollback** (STATUS-TAMPER/RUNNER-REPLAY), a **privileged-boundary verb
API** splitting `cmp_admitd` from the poller, a **SQLite transactional
ledger/outbox**, **remote-ack verification**, **FIFO-in-privileged-state**, and
layered **compromised-poller** defenses.

Each Pi review was individually correct. Each δ patch faithfully complied. The
§10 cost guard (~300 lines → "re-review rather than push through") was blown at
632, then 868, then 1023, and flagged each time — but treated as advisory and
pushed past. The workload it defends: **one dispatcher (δ), one job type
(`slicer`), ~1 job/hour, one box, a private repo.**

## 5 whys (root cause)

1. **Why did it balloon?** Each round added hardening for a newly-considered
   adversary (forged git-ref → compromised poller → signing/ack oracle → …).
2. **Why a new adversary each round?** The **threat model was never stated or
   bounded up front.** Each capability was introduced ad hoc and treated as
   in-scope by default.
3. **Why in-scope by default?** **No one held the global cost/benefit.** The
   reviewer optimized *local correctness*; the implementer optimized *local
   compliance*. Both are locally rational; together they ratchet scope upward
   without bound.
4. **Why didn't the implementer (δ) hold it?** δ applied `process-economics`
   *per patch* but never re-asked "does this whole apparatus earn its cost for
   this workload?" Deference to a rigorous reviewer suppressed **frame-challenge**
   ("should we build this at all / this much?").
5. **Why wasn't the tripped cost-guard a stop?** §10's "re-review rather than
   push through" was read as advisory. The monotonic growth past an explicit
   budget *was* the alarm; it was flagged and overridden three times.

**Root cause:** an **unstated, unbounded threat model** combined with **no owner
of global cost/benefit**. Individually-correct local optimization by both the
reviewer and the implementer produced unbounded scope. The explicit cost guard
was the ignored alarm.

## Corrective actions

1. **State the threat model + explicit non-goals before hardening any design.**
   A "defect" against an out-of-scope adversary is not a defect. (v0.6.0 opens
   with: single δ dispatcher, box-local registry, private repo → compromised-
   poller and git-ref-forgery are **out of scope for v0**.)
2. **Match mechanism to cardinality.** Single-writer / single-tenant / low-rate /
   private ≠ Byzantine / multi-tenant. Byzantine machinery (signed objects,
   anti-rollback, privileged verb boundary, transactional outbox) is YAGNI here.
3. **The implementer owns global cost/benefit**, not just local compliance —
   obligated to challenge the frame, not only satisfy comments. δ should have
   raised KISS/YAGNI around v0.4, not waited for the operator.
4. **A tripped explicit cost guard is a STOP → simplify-the-whole-thing review**,
   never a flag to push past. §10 said exactly this; honor it as a hard gate.
5. **Don't re-solve at layer N what layer N+1 guarantees.** Slicer already has
   durable checkpoint/resume; the runner over-invested in exactly-once/
   crash-consistency that Slicer's resume already covers. At-most-once + a marker
   file is enough.
6. **Add a periodic "still the lightest thing that earns its cost?" checkpoint**
   to any multi-round review loop, owned by implementer/operator.

## Remediation (operator-signed-off)

Minimal **v0.6.0**: keep only the RCA-load-bearing five — (a) box **pulls** work;
(b) **box-local registry** (ref names a job, never a command); (c) non-root,
**cgroup + time-bounded, network-off `systemd-run`** unit; (d) **observable**
status/liveness ref; (e) **at-most-once** via a durable marker + Slicer's own
resume. Cut the rest as YAGNI. Target ~100 lines. The real security boundary is
**registry + sandbox**, not cryptography: a rogue queue entry can only request
`slicer`, which runs bounded and non-root.

## What worked (keep)

The git-ref dialogue protocol (cnos#698) and the cell-dispatch loop performed
well across many honest round-trips (including the corrected 17/17→PARTIAL
overclaim). The failure was **scope judgment, not tooling.** Tooling ≠ judgment.
