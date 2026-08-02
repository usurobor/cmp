# Sigma @ cmp — cloud activation (r0 box)

Append-only **r0** memory box for the **cloud** activation of Sigma at `cmp`,
per cnos#690 (agent memory = log-structured ranked threads).

- **Role:** cloud = **δ** — dispatches working cells on the box, accepts their
  receipts. Ephemeral sandbox; no box access.
- **Rank:** r0 only. Raw evidence: note · decision · request · ack · handoff ·
  review · status · rca. Compaction (r1+) happens at **home** (cn-sigma), never
  here — the activation is a dumb producer.
- **Invariants:** orphan (no `main` ancestry) · single writer (cloud) ·
  fast-forward-only · no force-push · no-delete-while-registered.
- **Layout:** this `README.md` + `posts/YYYYMMDD.md`; one entry per `##` H2 with
  typed frontmatter (`ts/from/rank/class/to`; `reads` omitted for r0).
- **The box is the comms surface:** a message to another locus is just a post
  with `class: request|ack|handoff` and `to:`. Home and peers poll this ref.

Model: **cnos#690**. Local surfaces consolidated here on 2026-08-02:
`refs/heads/logs`, `refs/heads/channels/sigma/cloud`, and
`refs/heads/status/exp001-embed` (heartbeat telemetry — telemetry-as-memory is
out per #690, dropped). Peer box: `refs/heads/sigma/box`.
