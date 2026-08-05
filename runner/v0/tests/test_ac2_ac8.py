#!/usr/bin/env python3
"""Fixture-level tests for the two ACs provable without real systemd.

AC2 — malformed / unknown-mode / out-of-bounds / extra-field requests are
      rejected to status and create NO unit: a fake launcher is injected and
      asserted NEVER called; a valid request proves the launcher DOES fire
      (so the AC2 assertion is not vacuously green).

AC8 — the jobs/queue and jobs/status ref names trigger ZERO workflows: parse
      every .github/workflows/*.yml push-branch filter and assert none admit a
      jobs/* ref (nor a '**' wildcard that would).

Run: python3 runner/v0/tests/test_ac2_ac8.py
"""
import os, sys, re, json, tempfile, glob

HERE = os.path.dirname(os.path.abspath(__file__))
V0 = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(V0))
sys.path.insert(0, V0)
import poller  # noqa: E402

REG = poller.load_registry(os.path.join(V0, "registry.yaml"))


class Launcher:
    def __init__(self): self.calls = []
    def __call__(self, unit, spec): self.calls.append((unit, spec))


def _dispatch(requests, launch):
    d = tempfile.mkdtemp()
    st = poller.dispatch(requests, REG, d, launch,
                         unit_active=lambda: False, unit_show=lambda u: {}, now=1000)
    return st, d


def test_ac2_rejects_never_launch():
    bad = [
        {"id": "a", "mode": "sample", "n": int(REG["n_max"]) + 1},   # out of bounds
        {"id": "b", "mode": "turbo", "n": 10},                        # unknown mode
        {"id": "c", "job": "/bin/sh"},                                # extra/wrong field
        {"id": "bad id!", "mode": "sample", "n": 10},                 # illegal id
        ["not", "an", "object"],                                      # not a mapping
        {"id": "d", "mode": "sample", "n": 0},                        # n < 1
        {"id": "e", "mode": "sample", "n": True},                     # bool is not a valid n
    ]
    # each bad request is individually rejected by the pure admission boundary
    for req in bad:
        ok, reason = poller.validate(req, REG)
        assert not ok, "expected rejection for %r (got %s)" % (req, reason)
    # and end-to-end: the injected launcher is NEVER called; every emitted status is rejected
    lch = Launcher()
    statuses, d = _dispatch(bad, lch)
    assert lch.calls == [], "launcher must NEVER be called for rejected requests: %r" % lch.calls
    assert all(s["state"] == "rejected" for s in statuses.values()), \
        "every emitted status must be rejected: %r" % statuses
    assert os.listdir(os.path.join(d, "status")), "rejections must be written to a status snapshot"
    assert not os.path.exists(os.path.join(d, "launched")) or not os.listdir(os.path.join(d, "launched")), \
        "no launched/<id> marker may exist for a rejected request"
    print("AC2 reject path: PASS (%d bad requests, all rejected, 0 launches)" % len(bad))


def test_ac2_valid_launches_once():
    lch = Launcher()
    statuses, d = _dispatch([{"id": "ok1", "mode": "sample", "n": 10}], lch)
    assert len(lch.calls) == 1, "a valid request must launch exactly once: %r" % lch.calls
    unit, spec = lch.calls[0]
    assert unit == "cmpjob-ok1"
    assert spec["exec"] == REG["exec"] and spec["user"] == REG["user"], "spec comes from registry, not request"
    assert statuses["ok1"]["state"] == "launched"
    # re-posting the same id creates no second launch (O_EXCL marker) — AC6 seam
    st2 = poller.dispatch([{"id": "ok1", "mode": "sample", "n": 10}], REG, d, lch,
                          unit_active=lambda: False, unit_show=lambda u: {}, now=1001)
    assert len(lch.calls) == 1, "re-posting same id must NOT relaunch: %r" % lch.calls
    assert st2["ok1"]["reason"] == "marker"
    print("AC2 valid path: PASS (1 launch; duplicate id suppressed by marker)")


BRANCH_RE = re.compile(r"branches:\s*(\[[^\]]*\]|(?:\n\s*-\s*\S+)+)")


def _push_branch_tokens(text):
    # isolate the on: ... push: block heuristically, then pull branch tokens
    toks = []
    for m in BRANCH_RE.finditer(text):
        raw = m.group(1)
        toks += re.findall(r"[^\s,\[\]\-']+", raw)
    return [t for t in toks if t]


def test_ac8_zero_workflows_on_job_refs():
    wfs = glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))
    assert wfs, "expected workflow files to check"
    offenders = []
    for wf in wfs:
        text = open(wf).read()
        if "jobs/queue" in text or "jobs/status" in text:
            offenders.append((wf, "literal jobs/ ref present"))
        for tok in _push_branch_tokens(text):
            if tok.startswith("jobs/") or tok in ("**", "*"):
                offenders.append((wf, "push branch filter %r admits job refs" % tok))
    assert not offenders, "jobs/queue|jobs/status must trigger 0 workflows: %r" % offenders
    print("AC8 ref inertness: PASS (%d workflows, none trigger on jobs/*)" % len(wfs))


if __name__ == "__main__":
    fails = 0
    for t in (test_ac2_rejects_never_launch, test_ac2_valid_launches_once,
              test_ac8_zero_workflows_on_job_refs):
        try:
            t()
        except AssertionError as e:
            fails += 1; print("FAIL %s: %s" % (t.__name__, e))
    print("---\n%s" % ("ALL PASS" if not fails else "%d FAILURE(S)" % fails))
    sys.exit(1 if fails else 0)
