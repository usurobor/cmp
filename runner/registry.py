"""registry.py — box-local, root-owned job registry (§3.3).

Binds exec provenance (`exec_sha256`, verified before every launch) and the
sandbox/resource ceiling. The request selects an immutable registered
implementation, never a mutable pathname. Changing what the box runs requires box
access, never a git push.

The doc shows the registry as YAML at /etc/cmp-jobs/registry.yaml. stdlib Python
has no YAML parser and the task is stdlib-only, so the fixture is the identical
structure serialized as JSON (registry.json). Root-owned semantics are simulated
by a fixture path. This is the sole format deviation and is documented here.
"""
import json, hashlib, os

REQUIRED = ("version", "exec", "exec_sha256", "workdir", "input_roots",
            "output_roots", "network", "user", "memory", "cpu", "timeout_cap")


class RegistryError(Exception):
    pass


def load(path):
    reg = json.load(open(path))
    if "jobs" not in reg or not isinstance(reg["jobs"], dict):
        raise RegistryError("registry missing 'jobs' map")
    for key, spec in reg["jobs"].items():
        for f in REQUIRED:
            if f not in spec:
                raise RegistryError("job %s missing field %s" % (key, f))
    return reg


def resolve(reg, job_key, job_version):
    """Return the spec for job_key iff registered and version matches. §3.2/§3.3."""
    spec = reg["jobs"].get(job_key)
    if spec is None:
        raise RegistryError("unknown job %r" % job_key)
    if str(spec["version"]) != str(job_version):
        raise RegistryError("job %s version mismatch: registry %s != request %s"
                            % (job_key, spec["version"], job_version))
    return spec


def verify_exec(spec):
    """Recompute sha256 of the registered exec and compare to exec_sha256.
    Verified before every launch (A2). Returns the digest or raises."""
    p = spec["exec"]
    if not os.path.isfile(p):
        raise RegistryError("exec not found: %s" % p)
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != spec["exec_sha256"]:
        raise RegistryError("exec_sha256 mismatch for %s: %s != %s" % (p, h, spec["exec_sha256"]))
    return h


def validate_params(spec, params):
    """Best-effort check of request params against the registry param declaration.
    (Workload projection/refusal stays inside Slicer per §4.3 — this only guards
    the declared shape.)"""
    decl = spec.get("params", {})
    if params and not isinstance(params, dict):
        raise RegistryError("params must be a mapping")
    for k, v in (params or {}).items():
        if k not in decl:
            raise RegistryError("unknown param %r" % k)
        rule = decl[k]
        if isinstance(rule, list):
            if v not in rule:
                raise RegistryError("param %s=%r not in %r" % (k, v, rule))
        elif isinstance(rule, dict) and rule.get("type") == "int":
            if not isinstance(v, int) or (("max" in rule) and v > rule["max"]):
                raise RegistryError("param %s=%r violates %r" % (k, v, rule))
    return True
