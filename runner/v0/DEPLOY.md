# Slicer Box v0 — deploy runbook (cn-node-01)

**Audience:** Omega (operator/box-ops on Axiom's behalf). **Not** a CMP review
role — deployment and the on-box test only. Runner under test:
`73f5fc185594a926716d1a4fc4593ef7f3b68621`. Design of record:
`docs/design/box-job-admission.md` v0.6.0. Acceptance test: **issue #6**.

This installs the unprivileged poller + the one fixed Slicer entrypoint + a
single-entry root-owned registry, then runs **one** bounded sample and stops.
No production cutover, no self-hosted-runner unregister (separate operator
decision after #6 passes).

## 0. One value only δ cannot supply

- **`dataset`** — the real on-box path the fixed entrypoint samples. The example
  registry ships `/var/lib/cmp/data/corpus.jsonl`; set it to the actual dataset
  on cn-node-01. Everything else below is fixed.

## 1. Users (non-root)

```
useradd --system --no-create-home --shell /usr/sbin/nologin cmppoll   # runs the poller
useradd --system --no-create-home --shell /usr/sbin/nologin cmpjob    # runs the job
```
`cmpjob` holds no sudo, no keys, no git credential. `cmppoll` holds no root; it
starts/inspects only `cmpjob-*` units (grant via the narrow polkit rule in §4).

## 2. Code + state

```
install -d -o root -g root            /opt/cmp/bin /opt/cmp/runner
cp runner/v0/poller.py                /opt/cmp/runner/poller.py
cp runner/v0/slicer_run.py            /opt/cmp/bin/slicer-run    # exec in registry
chmod 0755 /opt/cmp/bin/slicer-run /opt/cmp/runner/poller.py

install -d -o cmppoll -g cmppoll      /var/lib/cmp/runner        # state_dir (markers, status, receipts)
git clone --mirror https://github.com/usurobor/cmp /opt/cmp/runner/mirror   # gitdir the poller fetches into
chown -R cmppoll:cmppoll /opt/cmp/runner/mirror
```

## 3. Registry + poller config (root-owned, NOT in any repo)

```
install -d -o root -g root /etc/cmp-jobs
cp runner/v0/registry.yaml     /etc/cmp-jobs/registry.yaml       # then EDIT `dataset:` (see §0)
cp runner/v0/poller.example.json /etc/cmp-jobs/poller.json
chown root:root /etc/cmp-jobs/registry.yaml /etc/cmp-jobs/poller.json
chmod 0644 /etc/cmp-jobs/registry.yaml /etc/cmp-jobs/poller.json
```
The registry is the security boundary: git names a job (`slicer`), never a
command. Changing what the box runs needs box access, not a git push.

## 4. systemd timer + polkit

```
cp runner/v0/cmp-poller.service /etc/systemd/system/cmp-poller.service
cp runner/v0/cmp-poller.timer   /etc/systemd/system/cmp-poller.timer
```
Narrow polkit rule so `cmppoll` may manage only `cmpjob-*` transient units
(`/etc/polkit-1/rules.d/50-cmp-poller.rules`):
```javascript
polkit.addRule(function(action, subject) {
  if (subject.user == "cmppoll" &&
      (action.id == "org.freedesktop.systemd1.manage-units") &&
      action.lookup("unit") && action.lookup("unit").indexOf("cmpjob-") == 0) {
    return polkit.Result.YES;
  }
});
```
```
systemctl daemon-reload
systemctl enable --now cmp-poller.timer
```

## 5. CI-inertness precondition (AC8)

Ensure `refs/heads/jobs/queue` and `refs/heads/jobs/status` exist (empty is
fine) and trigger no workflow. δ verifies 0 Actions runs on those refs as part
of #6.

## 6. Hand back to δ

Report to δ (`cn-sigma@cmp`, dialogue thread `cmp-runner-design-001`): the box
hostname, the chosen `dataset` path, `systemctl status cmp-poller.timer`, and
confirmation `jobs/queue`/`jobs/status` exist. δ then writes the single #6
request `{id, mode: sample, n: <small>}` to `jobs/queue` and collects the 8
return artifacts. **Stop after one sample.**
