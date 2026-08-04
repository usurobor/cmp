"""refs.py — git-native signed-object transport for jobs/{queue,status,runner}.

box-job-admission v0.5.1 @ 8c6e24b, §3.1/§3.6/§4.5. stdlib + git + ssh-keygen only.

Trust is git-native signed objects verified by the box (§3.1): commits carry an
SSH signature (`gpgsig`), verified with `ssh-keygen -Y verify` against a box-local,
root-owned `allowed_signers`. We deliberately do NOT use `git verify-commit` /
`git commit -S` (they are not portable and this host intercepts the code-sign
helper) — we sign the commit payload ourselves and reconstruct+verify it ourselves,
which is exactly what §4.2 requires the launcher to do independently.
"""
import os, subprocess, tempfile, hashlib, json, re, urllib.request, urllib.error

# Frozen id grammar (§3.2): UTC basic-format timestamp, '-', slug <=32 chars.
ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9][a-z0-9-]{0,31}$")


class RequestError(Exception):
    pass


def request_digest(raw):
    """The sole content address: SHA-256 of the exact bytes of requests/<id>.json
    (§3.6). Hash the admitted bytes verbatim — no re-serialization."""
    return hashlib.sha256(raw).hexdigest()


def validate_request(raw, expect_id):
    """Parse+validate exact bytes against cmp.job-request.v1 and the id grammar.
    Returns the parsed request. Raises RequestError. (§3.2/§3.6)"""
    if not ID_RE.match(expect_id):
        raise RequestError("id grammar violation: %r" % expect_id)
    if len(raw) > 65536:
        raise RequestError("request too large: %d bytes" % len(raw))
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RequestError("not UTF-8 JSON: %s" % e)
    if not isinstance(req, dict):                       # a signed array/scalar is a bounded reject
        raise RequestError("request root must be a JSON object, got %s" % type(req).__name__)
    if req.get("schema") != "cmp.job-request.v1":
        raise RequestError("bad schema: %r" % req.get("schema"))
    if "params" in req and not isinstance(req["params"], dict):
        raise RequestError("params must be a mapping, got %s" % type(req["params"]).__name__)
    if req.get("id") != expect_id:
        raise RequestError("id mismatch: body %r != path %r" % (req.get("id"), expect_id))
    if not ID_RE.match(str(req.get("id", ""))):
        raise RequestError("id grammar violation in body")
    if not isinstance(req.get("job"), str) or not req["job"]:
        raise RequestError("missing job registry key")
    if "job_version" not in req:
        raise RequestError("missing job_version")
    return req

# Scrub git config so the host's forced commit.gpgsign/code-sign shim can't
# substitute a different key; we control signing explicitly.
GENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(gitdir, *args, input=None, check=True):
    r = subprocess.run(["git", "--git-dir", gitdir, *args], input=input,
                       capture_output=True, env=GENV)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args, r.stderr.decode(errors="replace")))
    return r


def _sign(privkey, payload):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "pl")
        open(p, "wb").write(payload)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", privkey, "-n", "git", p],
                       check=True, capture_output=True)
        return open(p + ".sig", "rb").read()


def make_commit(gitdir, tree, parents, ident, msg, privkey):
    """Build a commit object; if privkey, SSH-sign the payload and embed gpgsig."""
    hdr = "tree %s\n" % tree + "".join("parent %s\n" % p for p in parents)
    who = "%s\n%s\n" % ("author %s" % ident, "committer %s" % ident)
    body = msg if msg.endswith("\n") else msg + "\n"
    if privkey:
        payload = (hdr + who + "\n" + body).encode()
        sig = _sign(privkey, payload).decode()
        sl = sig.splitlines()
        gpg = "gpgsig " + sl[0] + "\n" + "".join(" " + l + "\n" for l in sl[1:])
        obj = (hdr + who + gpg + "\n" + body).encode()
    else:
        obj = (hdr + who + "\n" + body).encode()
    return git(gitdir, "hash-object", "-w", "-t", "commit", "--stdin", input=obj).stdout.decode().strip()


def commit_payload_and_sig(gitdir, sha):
    """Reconstruct the signed payload and armored signature from a commit object."""
    raw = git(gitdir, "cat-file", "commit", sha).stdout
    pl, sg, ins = [], [], False
    for ln in raw.split(b"\n"):
        if ln.startswith(b"gpgsig "):
            ins = True; sg.append(ln[7:]); continue
        if ins:
            if ln.startswith(b" "):
                sg.append(ln[1:]); continue
            ins = False
        pl.append(ln)
    return b"\n".join(pl), (b"\n".join(sg) + b"\n" if sg else b"")


def verify_commit(gitdir, sha, signers_file):
    """Independently verify an SSH-signed commit against a root-owned allowed_signers.
    Returns (principal, fingerprint) or raises ValueError (unsigned/wrong-key)."""
    payload, sig = commit_payload_and_sig(gitdir, sha)
    if not sig:
        raise ValueError("unsigned")
    with tempfile.TemporaryDirectory() as d:
        sp = os.path.join(d, "sig")
        open(sp, "wb").write(sig)
        fp = subprocess.run(["ssh-keygen", "-Y", "find-principals", "-f", signers_file,
                             "-s", sp], input=payload, capture_output=True)
        if fp.returncode != 0 or not fp.stdout.strip():
            raise ValueError("wrong-key")
        principal = fp.stdout.decode().split()[0]
        vr = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers_file,
                             "-I", principal, "-n", "git", "-s", sp],
                            input=payload, capture_output=True)
        if vr.returncode != 0:
            raise ValueError("wrong-key")
        msg = vr.stderr.decode() + vr.stdout.decode()
        fpr = ""
        if "SHA256:" in msg:
            fpr = "SHA256:" + msg.split("SHA256:")[1].split()[0]
    return principal, fpr


# ---- tree helpers --------------------------------------------------------
def _blob(gitdir, data):
    return git(gitdir, "hash-object", "-w", "--stdin", input=data).stdout.decode().strip()


def build_tree(gitdir, base_tree, path, data):
    with tempfile.TemporaryDirectory() as d:
        idx = os.path.join(d, "index")
        e = {**GENV, "GIT_INDEX_FILE": idx}
        if base_tree:
            subprocess.run(["git", "--git-dir", gitdir, "read-tree", base_tree], env=e, check=True, capture_output=True)
        b = _blob(gitdir, data)
        subprocess.run(["git", "--git-dir", gitdir, "update-index", "--add",
                        "--cacheinfo", "100644,%s,%s" % (b, path)], env=e, check=True, capture_output=True)
        return subprocess.run(["git", "--git-dir", gitdir, "write-tree"], env=e,
                              check=True, capture_output=True).stdout.decode().strip()


def ref_sha(gitdir, ref):
    r = git(gitdir, "rev-parse", "--verify", "-q", ref, check=False)
    return r.stdout.decode().strip() if r.returncode == 0 else None


def parents(gitdir, sha):
    out = git(gitdir, "rev-list", "--parents", "-n", "1", sha).stdout.decode().split()
    return out[1:]


def is_ancestor(gitdir, a, b):
    return git(gitdir, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def added_paths(gitdir, sha):
    """name-status of a commit vs its first parent (or empty tree for a root)."""
    ps = parents(gitdir, sha)
    if len(ps) == 0:
        out = git(gitdir, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", sha).stdout
    else:
        out = git(gitdir, "diff-tree", "--no-commit-id", "--name-status", "-r", sha).stdout
    res = []
    for ln in out.decode().splitlines():
        f = ln.split("\t")
        res.append((f[0], f[1]))
    return res


def read_blob(gitdir, sha, path):
    return git(gitdir, "cat-file", "-p", "%s:%s" % (sha, path)).stdout


# ---- queue reader (§3.6 cursor law) --------------------------------------
class QueueError(Exception):
    def __init__(self, kind, detail, sha=None):
        super().__init__("%s: %s" % (kind, detail))
        self.kind, self.detail, self.sha = kind, detail, sha


def read_queue(gitdir, ref, cursor, signers_file):
    """First-parent linear traversal cursor..tip. Yields dicts per new request.
    Raises QueueError(QUEUE-TAMPER/QUEUE-NONLINEAR/QUEUE-MALFORMED/QUEUE-UNSIGNED)."""
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return []
    if cursor:
        if not is_ancestor(gitdir, cursor, tip):
            raise QueueError("QUEUE-TAMPER", "tip does not descend from last_accepted_commit", tip)
        rng = "%s..%s" % (cursor, tip)
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse", rng).stdout.decode().split()
    else:
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse", tip).stdout.decode().split()
    out = []
    for sha in shas:
        if len(parents(gitdir, sha)) > 1:
            raise QueueError("QUEUE-NONLINEAR", "merge commit in queue history", sha)
        adds = added_paths(gitdir, sha)
        if len(adds) != 1 or adds[0][0] != "A" or not adds[0][1].startswith("requests/") \
                or not adds[0][1].endswith(".json"):
            raise QueueError("QUEUE-MALFORMED", "commit must add exactly one requests/<id>.json", sha)
        rid = adds[0][1][len("requests/"):-len(".json")]
        principal, fpr = verify_commit(gitdir, sha, signers_file)  # raises ValueError if unsigned/wrong
        data = read_blob(gitdir, sha, adds[0][1])
        out.append({"sha": sha, "id": rid, "path": adds[0][1], "bytes": data,
                    "principal": principal, "fingerprint": fpr})
    return out


# ---- status ref (append-only, box-signed, §3.6) --------------------------
def status_append(gitdir, ref, event, boxkey):
    tip = ref_sha(gitdir, ref)
    base_tree = git(gitdir, "rev-parse", tip + "^{tree}").stdout.decode().strip() if tip else None
    eid = event["event_id"]
    data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
    tree = build_tree(gitdir, base_tree, "events/%s.json" % eid, data)
    ident = "cmp-box <box@cmp> %d +0000" % 1785000000
    sha = make_commit(gitdir, tree, [tip] if tip else [], ident, "status %s" % eid, boxkey)
    git(gitdir, "update-ref", ref, sha)
    return sha


class StatusTamper(Exception):
    pass


STATUS_SCHEMAS = {"cmp.job-status.v1", "cmp.queue-event.v1"}
_TERMINAL_STATES = {"succeeded", "failed", "rejected", "timeout", "indeterminate"}
JOB_STATES = {"queued", "admitted", "running", "succeeded", "failed", "rejected", "timeout", "indeterminate"}
QUEUE_KINDS = {"QUEUE-MALFORMED", "QUEUE-NONLINEAR", "QUEUE-UNSIGNED", "QUEUE-TAMPER", "LEDGER-INCONSISTENT"}
EID6 = re.compile(r"^[0-9]{6}$")


def _validate_event(ev, eid_name):
    """Full type/enum/grammar validation of one status event; any violation is a
    bounded STATUS-TAMPER (Pi re-audit #6), never an uncaught exception or an
    accepted unknown state."""
    if not isinstance(ev, dict):
        raise StatusTamper("STATUS-TAMPER: event is not a JSON object")
    if not EID6.match(eid_name):
        raise StatusTamper("STATUS-TAMPER: event-id grammar %r" % eid_name)
    if str(ev.get("event_id")) != eid_name:
        raise StatusTamper("STATUS-TAMPER: filename %s != payload event_id %s" % (eid_name, ev.get("event_id")))
    if not EID6.match(str(ev.get("event_id", ""))):
        raise StatusTamper("STATUS-TAMPER: payload event_id grammar")
    sch = ev.get("schema")
    if sch not in STATUS_SCHEMAS:
        raise StatusTamper("STATUS-TAMPER: unknown schema %r" % sch)
    if not isinstance(ev.get("observed_at"), int):
        raise StatusTamper("STATUS-TAMPER: observed_at must be int")
    if sch == "cmp.job-status.v1":
        if not isinstance(ev.get("job_id"), str) or not ev["job_id"]:
            raise StatusTamper("STATUS-TAMPER: job_id must be a non-empty string")
        if not isinstance(ev.get("seq"), int) or ev["seq"] < 1:
            raise StatusTamper("STATUS-TAMPER: seq must be a positive int")
        if ev.get("state") not in JOB_STATES:
            raise StatusTamper("STATUS-TAMPER: unknown job state %r" % ev.get("state"))
        if ev.get("reason") is not None and not isinstance(ev.get("reason"), str):
            raise StatusTamper("STATUS-TAMPER: reason must be null or string")
        if ev["state"] in ("rejected", "failed", "timeout", "indeterminate") and not ev.get("reason"):
            raise StatusTamper("STATUS-TAMPER: state %s requires a reason" % ev["state"])
        for f in ("request_digest", "queue_commit"):
            if f in ev and ev[f] is not None and not isinstance(ev[f], str):
                raise StatusTamper("STATUS-TAMPER: %s must be null or string" % f)
    else:  # cmp.queue-event.v1
        if ev.get("kind") not in QUEUE_KINDS:
            raise StatusTamper("STATUS-TAMPER: unknown queue-event kind %r" % ev.get("kind"))
        if ev.get("job_id") is not None or ev.get("request_digest") is not None:
            raise StatusTamper("STATUS-TAMPER: queue-event must have null job_id/request_digest")


def read_status(gitdir, ref, box_signers, pin):
    """Anti-rollback reader enforcing the FULL frozen wire contract (§3.6 A16, Pi #7):
    box signature; descendant of the pinned commit; each commit single-parent and
    append-only; exactly `A events/<event_id>.json`; filename==payload event_id;
    known schema; strictly increasing global event_id; per-job monotonic seq;
    terminal finality. pin={commit,event_id}. Raises StatusTamper."""
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return [], pin
    if pin.get("commit"):
        if pin["commit"] == tip:
            return [], pin                      # unchanged reread
        if not is_ancestor(gitdir, pin["commit"], tip):
            raise StatusTamper("STATUS-TAMPER: status ref does not descend from pinned commit")
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse",
                   "%s..%s" % (pin["commit"], tip)).stdout.decode().split()
    else:
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse", tip).stdout.decode().split()
    events, last_eid = [], pin.get("event_id", 0)
    seen_seq, terminated = pin.get("jobseq", {}), set(pin.get("terminated", []))
    seen_seq = dict(seen_seq)
    for sha in shas:
        if len(parents(gitdir, sha)) > 1:
            raise StatusTamper("STATUS-TAMPER: non-linear status commit %s" % sha)
        verify_commit(gitdir, sha, box_signers)  # box-key or not trusted (AC12)
        adds = added_paths(gitdir, sha)
        if len(adds) != 1 or adds[0][0] != "A" or not adds[0][1].startswith("events/") \
                or not adds[0][1].endswith(".json"):
            raise StatusTamper("STATUS-TAMPER: commit must add exactly one events/<id>.json")
        eid_name = adds[0][1][len("events/"):-len(".json")]
        try:
            ev = json.loads(read_blob(gitdir, sha, adds[0][1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise StatusTamper("STATUS-TAMPER: unparseable event bytes: %s" % e)
        _validate_event(ev, eid_name)
        eid = int(ev["event_id"])
        if eid <= last_eid:
            raise StatusTamper("STATUS-TAMPER: event_id %d regressed/reused (<= %d)" % (eid, last_eid))
        last_eid = eid
        if ev["schema"] == "cmp.job-status.v1":
            jid = ev["job_id"]
            if jid in terminated:
                raise StatusTamper("STATUS-TAMPER: event after terminal for job %s" % jid)
            s = ev["seq"]
            if s <= seen_seq.get(jid, 0):
                raise StatusTamper("STATUS-TAMPER: per-job seq %d regressed for %s" % (s, jid))
            seen_seq[jid] = s
            if ev["state"] in _TERMINAL_STATES:
                terminated.add(jid)
        events.append(ev)
    return events, {"commit": tip, "event_id": last_eid,
                    "jobseq": seen_seq, "terminated": sorted(terminated)}


# ---- remote transport (§4.4 / Pi #5) -------------------------------------
def fetch_queue(gitdir, remote, qref):
    """Fetch jobs/queue from origin into the local bare mirror. A remote that simply
    has no queue ref yet is 'empty', not a failure; anything else (unreachable remote,
    transport error) raises and the caller maps it to FETCH-FAIL health."""
    r = git(gitdir, "fetch", remote, "+%s:%s" % (qref, qref), check=False)
    if r.returncode != 0:
        if b"couldn't find remote ref" in r.stderr or b"couldn't find remote ref" in r.stdout:
            return
        raise RuntimeError("FETCH-FAIL: %s" % r.stderr.decode()[:200])


def push_ref(gitdir, remote, ref, force=False):
    """Append-only push (status) / force push (runner). Raises on failure."""
    spec = ("+" if force else "") + "%s:%s" % (ref, ref)
    r = git(gitdir, "push", remote, spec, check=False)
    if r.returncode != 0:
        raise RuntimeError("PUSH-FAIL: %s" % r.stderr.decode()[:200])


class OutboxConflict(Exception):
    pass


def status_event_bytes(gitdir, ref, eid6):
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return None
    r = git(gitdir, "cat-file", "-p", "%s:events/%s.json" % (tip, eid6), check=False)
    return r.stdout if r.returncode == 0 else None


def publish_event(gitdir, ref, eid, payload, boxkey):
    """Idempotently append one box-signed event. Safely repeatable from the same
    event_id+bytes (Pi #6): if events/<eid>.json already exists it must match these
    exact bytes (else it is an outbox/status CONFLICT), and republish is a no-op."""
    eid6 = "%06d" % int(eid)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    existing = status_event_bytes(gitdir, ref, eid6)
    if existing is not None:
        if existing != data:
            raise OutboxConflict("event %s already published with different bytes" % eid6)
        return ref_sha(gitdir, ref)
    tip = ref_sha(gitdir, ref)
    base = git(gitdir, "rev-parse", tip + "^{tree}").stdout.decode().strip() if tip else None
    tree = build_tree(gitdir, base, "events/%s.json" % eid6, data)
    ident = "cmp-box <box@cmp> %d +0000" % 1785000000
    sha = make_commit(gitdir, tree, [tip] if tip else [], ident, "status %s" % eid6, boxkey)
    git(gitdir, "update-ref", ref, sha)
    return sha


def remote_has_event(origin, ref, eid6):
    """Confirm origin's ref tip actually carries events/<eid6>.json (Pi #5: ack only
    after a verified push)."""
    tip = ref_sha(origin, ref)
    if tip is None:
        return False
    return git(origin, "cat-file", "-e", "%s:events/%s.json" % (tip, eid6), check=False).returncode == 0


# ---- runner ref (mutable snapshot, box-signed, §4.5) ---------------------
def runner_write(gitdir, ref, snap, boxkey):
    data = (json.dumps(snap, sort_keys=True, separators=(",", ":")) + "\n").encode()
    tree = build_tree(gitdir, None, "runner.json", data)  # one-entry tree, no history
    ident = "cmp-box <box@cmp> %d +0000" % 1785000000
    sha = make_commit(gitdir, tree, [], ident, "runner seq %s" % snap["seq"], boxkey)
    git(gitdir, "update-ref", ref, sha)  # force-update in place (single-writer)
    return sha


class RunnerReplay(Exception):
    pass


def read_runner(gitdir, ref, box_signers, pin):
    """Anti-rollback (§4.5 A16, Pi #7). pin={seq,observed_at,commit}. Resolve the
    ACTUAL tip: an unchanged reread (same commit object) is NOT a replay; a DIFFERENT
    object with seq <= pinned seq IS `RUNNER-REPLAY`; a regressed observed_at is stale
    (health downgraded, never 'healthy')."""
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return None, pin
    verify_commit(gitdir, ref, box_signers)  # box-signed or reject
    snap = json.loads(read_blob(gitdir, tip, "runner.json"))
    seq = int(snap["seq"])
    if tip == pin.get("commit"):
        return snap, pin                      # unchanged reread of the same object
    if seq <= pin.get("seq", -1):
        raise RunnerReplay("RUNNER-REPLAY: different object seq %d <= pinned %d" % (seq, pin.get("seq", -1)))
    if snap.get("observed_at", 0) < pin.get("observed_at", 0):
        snap["health"] = "STALE"
    return snap, {"seq": seq, "observed_at": snap.get("observed_at", 0), "commit": tip}


# ---- CI-inert runtime check (A11 / AC10) ---------------------------------
def ci_inert_check(owner, repo, refs):
    """Query the GitHub Actions API for runs triggered by each operational ref.
    Truthful: if no token/network, report 'unavailable' — never a fake pass."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    out = {}
    for ref in refs:
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if not tok:
            out[ref] = {"status": "unavailable", "reason": "no_token", "branch": branch}
            continue
        # The Actions API `branch` filter takes the BRANCH NAME, not refs/heads/... .
        url = "https://api.github.com/repos/%s/%s/actions/runs?branch=%s&per_page=100" % (
            owner, repo, urllib.request.quote(branch, safe=""))
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % tok,
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = json.loads(r.read())
            n = body.get("total_count")
            if n is None:
                out[ref] = {"status": "unavailable", "reason": "no_count", "branch": branch}
                continue
            # independently confirm each returned run's head_branch actually matches
            mismatched = [run.get("id") for run in body.get("workflow_runs", [])
                          if run.get("head_branch") != branch]
            triggered = [run.get("id") for run in body.get("workflow_runs", [])
                         if run.get("head_branch") == branch]
            out[ref] = {"status": "pass" if not triggered else "fail", "total_count": n,
                        "branch": branch, "confirmed_on_branch": len(triggered),
                        "api_returned_off_branch": len(mismatched)}
        except (urllib.error.URLError, Exception) as e:  # noqa
            out[ref] = {"status": "unavailable", "reason": type(e).__name__, "branch": branch}
    return out
