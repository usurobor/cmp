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
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RequestError("not UTF-8 JSON: %s" % e)
    if req.get("schema") != "cmp.job-request.v1":
        raise RequestError("bad schema: %r" % req.get("schema"))
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


def read_status(gitdir, ref, box_signers, pin):
    """Anti-rollback reader (§3.6 A16). pin={commit,event_id}. Verifies box sig,
    descendant of pinned commit, strictly increasing global event_id.
    Returns (events, new_pin) or raises StatusTamper."""
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return [], pin
    if pin.get("commit"):
        if not is_ancestor(gitdir, pin["commit"], tip):
            raise StatusTamper("STATUS-TAMPER: status ref does not descend from pinned commit")
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse",
                   "%s..%s" % (pin["commit"], tip)).stdout.decode().split()
    else:
        shas = git(gitdir, "rev-list", "--first-parent", "--reverse", tip).stdout.decode().split()
    events, last_eid = [], pin.get("event_id", 0)
    for sha in shas:
        verify_commit(gitdir, sha, box_signers)  # box-key or not trusted
        adds = added_paths(gitdir, sha)
        if len(adds) != 1 or not adds[0][1].startswith("events/"):
            raise StatusTamper("STATUS-TAMPER: malformed status commit")
        ev = json.loads(read_blob(gitdir, sha, adds[0][1]))
        eid = int(ev["event_id"])
        if eid <= last_eid:
            raise StatusTamper("STATUS-TAMPER: event_id %d regressed/reused (<= %d)" % (eid, last_eid))
        last_eid = eid
        events.append(ev)
    return events, {"commit": tip, "event_id": last_eid}


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
    """Anti-rollback (§4.5 A16). pin={seq,observed_at}. Lower/equal seq => RUNNER-REPLAY;
    regressed observed_at => stale (health downgraded, never 'healthy')."""
    tip = ref_sha(gitdir, ref)
    if tip is None:
        return None, pin
    verify_commit(gitdir, ref, box_signers)  # box-signed or reject
    snap = json.loads(read_blob(gitdir, tip, "runner.json"))
    seq = int(snap["seq"])
    if seq <= pin.get("seq", -1):
        raise RunnerReplay("RUNNER-REPLAY: seq %d <= pinned %d" % (seq, pin.get("seq", -1)))
    stale = snap.get("observed_at", 0) < pin.get("observed_at", 0)
    if stale:
        snap["health"] = "STALE"
    return snap, {"seq": seq, "observed_at": snap.get("observed_at", 0)}


# ---- CI-inert runtime check (A11 / AC10) ---------------------------------
def ci_inert_check(owner, repo, refs):
    """Query the GitHub Actions API for runs triggered by each operational ref.
    Truthful: if no token/network, report 'unavailable' — never a fake pass."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    out = {}
    for ref in refs:
        if not tok:
            out[ref] = {"status": "unavailable", "reason": "no_token"}
            continue
        url = "https://api.github.com/repos/%s/%s/actions/runs?branch=%s&per_page=1" % (
            owner, repo, urllib.request.quote(ref, safe=""))
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % tok,
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                n = json.loads(r.read()).get("total_count", None)
            out[ref] = {"status": "pass" if n == 0 else "fail", "total_count": n} if n is not None \
                else {"status": "unavailable", "reason": "no_count"}
        except (urllib.error.URLError, Exception) as e:  # noqa
            out[ref] = {"status": "unavailable", "reason": type(e).__name__}
    return out
