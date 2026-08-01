# RCA: CMP Sub-2 embedding encode — a day lost to unmeasured jobs on a 2 GB box

**Date:** 2026-08-01
**Severity:** Medium (no data loss, no production impact; ~7h of engineering time and box time burned, zero result produced)
**Duration:** ~10:18 first contributing event → ~19:20 halt for RCA (~9h wall; ~7h of it directly on the embedding slice)
**Status:** DIAGNOSING — no remediation until signed off.

## Summary

The Sub-2 embedding-retrieval slice was launched five times over ~7 hours and
never produced a result. The proximate failures were different each time (a
blocked runner, a path PermissionError, a push race, then a memory blowup that
swapped the box), but they share one systemic root: **expensive jobs were
launched on a known 2 GB / 1 vCPU box without first measuring their resource
envelope on a representative sample, and without pre-flight observability — so
each mis-sized job failed slowly and invisibly instead of failing fast with a
number.** The memory blowup was the *third* memory-accumulation incident on this
box the same day (collector OOM, gen_candidate_pairs throttle, embed swap),
which is the signal that the cause is a missing process gate, not any single bug.

## Timeline (UTC)

| # | Time | Event |
|---|------|-------|
| 1 | 10:18 | `gen_candidate_pairs` run `30695440755` starts; on the 2 GB box it reclaim-throttles to ~22% CPU (715k reclaim events, ~1.2 GB held) and never makes progress. |
| 2 | 12:25 | Box watchdog goes blind: systemd unit has no `HOME`, so `gh` returns rc=4 and every remote check is silently skipped (`2>/dev/null` + `continue`). Blind until 17:45. |
| 3 | 12:26 | Embed run #1 (`30699683175`, sha dfd9ea5) created; single self-hosted runner is occupied by the stale gen job, so it sits `queued`. |
| 4 | 12:33 | Stale gen job cancelled; runner frees. |
| 5 | ~12:38–13:06 | Cloud escalates on the channel that the encode is stuck queued; box is unattended (its recorder does not wake it), asks sit unread. |
| 6 | 14:28 | Runner recovers; run #1 picks up and **fails in 2 s**: `os.makedirs('/var/lib/cmp/data/derived/embeddings')` → `PermissionError` (runner can read the box-owned corpus dir but cannot mkdir under it). Failure-marker push is **rejected non-fast-forward** (run pinned to old sha), so the branch never moves and cloud is not woken. |
| 7 | 16:42 | Run #3 (`30708717121`, sha a8df2bc) starts with EMB_DIR moved to the workspace and the push race fixed. Begins encoding. |
| 8 | 16:42–19:12 | Run #3 is **memory-bound and swapping**: RSS 1340 MB, 1028 MB in swap, 470k reclaim events, **37% CPU**. Cloud cannot see this — GitHub's API 404s in-progress logs — and reports "healthy, ~2h" from step-state alone. |
| 9 | 19:03 | Cloud adds a git-ref progress heartbeat (run #4, `30713926108`) to gain visibility; it queues behind run #3. |
| 10 | ~19:06 | Box (re-attached, watchdog fixed) reports from root: run #3 is swapping, not slow; clean-binary subset is **222,855** (not the ~150k modelled); encode is **3.2% done** (7,170 / 222,855 vectors) after 2h20m → **~70 h** projected. |
| 11 | ~19:11 | Cloud kills run #3 and #4; ships fix c49402b (ENC_BATCH 256→16, shorter text, reservoir-sample to 60k). |
| 12 | 19:12 | Run #5 (`30714258514`) starts; heartbeat confirms visibility (`[1] filtering clean-binary`). |
| 13 | ~19:20 | Operator halts all processes to run this RCA. Run #5 cancelled; all watchers stopped. |

## Five Whys

1. **Why did the embedding slice consume ~7 h and produce no result?**
   → Its final run ran out of memory and swapped the box (3.2% in 2h20m), after
   three earlier runs failed on a blocked runner, a path permission, and a push
   race.

2. **Why did it run out of memory?**
   → At `ENC_BATCH=256` with ~450-token texts, onnxruntime's attention tensor
   (batch × heads × seq²) is ~3.2 GB and the arena retains the peak; and the
   working set was mis-sized because the clean-binary subset was **222,855**, not
   the ~150k assumed. Both are numbers, not judgements — and neither was measured.

3. **Why was the memory footprint not known before launching?**
   → No capacity check preceded the run: the subset size, per-item peak RSS, and
   encode throughput were never measured on a small sample before committing the
   full job to the constrained box. The design asserted "streams, well under the
   cap" from an unvalidated mental model.

4. **Why was there no capacity check?**
   → There is no standing step that requires one. The job was authored and
   launched optimistically, the same way the collector (OOM ×2) and
   `gen_candidate_pairs` (throttle) were earlier the same day — three instances of
   the same class in one day.

5. **Why does the same class of failure keep recurring?** → **ROOT CAUSE:**
   There is no capacity-and-observability pre-flight gate for jobs on the shared
   2 GB / 1 vCPU box. Nothing forces a measured resource envelope (peak RSS vs
   cap, dataset size, projected wall-clock) on a representative sample before a
   full run, and nothing guarantees live progress is visible — so mis-sized jobs
   are admitted, then fail slowly and invisibly rather than being rejected up
   front with a number.

## Contributing causes (documented, not blamed)

- **Observability gap (both ends).** GitHub's API 404s in-progress logs; there
  was no job heartbeat; the box watchdog was blind 12:25–17:45 (no `HOME`); the
  box side was a *recorder*, not a *monitor* — it recorded state to a file that
  never woke anyone. Failures therefore took hours to surface.
- **Single runner, no capacity awareness.** One stale throttling job (step 1)
  blocked the entire queue (step 3) because nothing tracks whether the box is
  already saturated before admitting more.
- **Two latent code bugs compounded the diagnosis** (steps 6): a non-runner-
  writable output path, and a push that assumed fast-forward. Each individually
  minor; together they hid the real signal (the branch never moved).
- **`Restart=always` masked, then delayed.** The runner-down window (~12:25–14:28)
  looked like a hang because the auto-restart eventually recovered it — but only
  after a ~2 h queue timeout had already been consumed.

## Anti-pattern check (per skill)

- Not "human error" — the *system* admitted unmeasured jobs. The design-around is
  a gate, not "be more careful."
- Prefer automation — the preventive actions are a pre-flight probe and a
  heartbeat, not "add more review."
- Has concrete, owned action items (below).

## Actions (proposed — pending sign-off; NOT yet implemented)

```yaml
actions:
  - action: "Pre-flight capacity probe: before any full box job, encode/process a
             fixed small sample (e.g. 1–2k), measure peak RSS + throughput, and
             extrapolate to full N. Abort with a number if projected peak > cap
             or projected wall-clock > threshold."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Measure-N-first: any job whose memory scales with dataset size must
             emit the dataset count (e.g. 222,855) as an early, cheap stage before
             the expensive stage; memory model is derived from the measured N, not
             an assumed one."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Shard-and-checkpoint for large encodes/jobs (box rec #1): process in
             ~20k slices, flush each to disk, free between slices, resume from last
             complete shard. Peak RSS becomes a function of shard size, not corpus
             size; job survives cancellation."
    owner: sigma
    status: pending
    due: 2026-08-03
  - action: "Standing observability on every long box job: git-ref progress
             heartbeat (built, c49402b) as a required workflow element; box
             watchdog is now a real monitor with HOME fix (box, done)."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Record the box capacity envelope (2 GB RAM, 1 vCPU, ~7 GB disk, 2 GB
             swap on same spindle, no GPU) as a hard constraint doc in the repo,
             referenced by any job author; jobs that exceed it must trigger the
             cut-subset / shard / bigger-box decision UP FRONT."
    owner: sigma
    status: pending
    due: 2026-08-03
  - action: "Right-sizing decision is the operator's: a job that structurally needs
             more than the box (full 222,855 encode) surfaces the bigger-box vs
             cut-subset vs shard tradeoff before running, not after a failure."
    owner: operator
    status: pending
    due: 2026-08-03
```

## Sign-off

- [ ] Root cause accepted
- [ ] Actions accepted / amended
- [ ] Remediation freeze lifted

_No code changes, re-runs, or box jobs until the three boxes above are checked._
