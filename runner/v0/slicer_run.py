#!/usr/bin/env python3
"""slicer-run (v0) — the one fixed Slicer entrypoint the runner launches.

Bounded-sample executable, stdlib-only. Reads the first `n` rows of the
operator-designated on-box dataset (path comes from the registry via the params
file, NEVER from the git request), writes an ATOMIC local progress file as it
advances, and emits a deterministic terminal receipt {rows, sha256,
peak_rss_bytes, elapsed_s}. No model, no network, no full corpus.

    argv[1] = params.json  {id, mode, n, dataset, progress, receipt}
"""
import os, sys, json, time, hashlib, resource


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def run(p):
    n, t0, h, rows = int(p["n"]), time.time(), hashlib.sha256(), 0
    with open(p["dataset"], "rb") as f:
        for line in f:
            if rows >= n:
                break
            h.update(line); rows += 1
            if rows % 1000 == 0:
                atomic_write(p["progress"], {"id": p["id"], "rows": rows, "target": n})
    return {"id": p["id"], "mode": p["mode"], "rows": rows, "sha256": h.hexdigest(),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "elapsed_s": round(time.time() - t0, 3)}


if __name__ == "__main__":
    params = json.load(open(sys.argv[1]))
    receipt = run(params)
    atomic_write(params["progress"], {"id": params["id"], "rows": receipt["rows"], "target": int(params["n"])})
    atomic_write(params["receipt"], receipt)
    json.dump(receipt, sys.stdout); sys.stdout.write("\n")
