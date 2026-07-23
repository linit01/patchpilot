# PatchPilot — Handoff (2026-07-23)

## TL;DR
Fixed a real update-detection accuracy bug and shipped it as **v1.7.5**, plus a
security bump as **v1.7.6** — both are on `main` and tagged/pushed; CI is
building images. **The one open item: verify host `10.0.1.101` (the k3s server)
actually reports its pending apt updates after PatchPilot self-updates to
v1.7.6.** Until the running deployment picks up the new image, that host still
shows the old (wrong) "up to date". See **Deferred** for the exact check.

## Deferred / known unfinished — DO THIS NEXT
**Verify 10.0.1.101 after self-update.** The v1.7.5 apt fix lives in the Ansible
playbook baked into the backend image, so the fleet only gets it once the
operator self-updates PatchPilot and the seed-ansible PVC is refreshed. Steps:

1. Confirm CI published the v1.7.6 images (`linit01/patchpilot:backend-1.7.6`,
   `frontend-1.7.6`) on Docker Hub.
2. Self-update PatchPilot **in-app** (PP's own update flow — **not**
   `kubectl rollout restart`; see memory `feedback_app_self_update`).
3. In PatchPilot, run an **update check** against `10.0.1.101`.
4. **Expected:** the host reports ~39 apt updates (was wrongly showing "up to
   date"). Cross-check against ground truth on the host (command below).
5. If it still shows 0/up-to-date, check the backend log for
   `[PARSER] WARNING` lines and inspect `/tmp/ansible_last_run.txt` in the
   backend pod for the `PACKAGE:` / `Show update status` lines for that host.

Confirmed root cause (already fixed): the v1.7.0 apt hold-filter used the awk
`NR==FNR` idiom, which drops the **entire** upgradable list when
`apt-mark showhold` is empty (no held packages). 10.0.1.101 has no holds → 39
became 0. Live-confirmed on the host: `apt list --upgradable` = 39, no holds,
PatchPilot's exact pipeline = 0.

## What works today (don't break these)
| Behavior | Notes |
|---|---|
| apt update detection ([ansible/check-os-updates.yml:101](ansible/check-os-updates.yml)) | Hold-filter now keys off line **shape** (hold lines are bare names; `apt list` lines contain a `name/origin` slash), NOT file position. **Do NOT revert to the `NR==FNR` idiom** — it silently drops all updates when there are no holds. Comment above the task warns about this. |
| Phased updates | Included via `-o APT::Get::Always-Include-Phased-Updates=true` on the `apt list` call. The old `export APT_GET_ALWAYS_INCLUDE_PHASED_UPDATES=1` env var was a no-op (apt doesn't read config keys as bare env vars) — don't reintroduce it. |
| Homebrew pin-filter (macOS) | Uses a shell `while read` loop that already handles an empty `brew list --pinned` — safe, left unchanged. |
| Backend parser reconciliation ([backend/ansible_runner.py:1153](backend/ansible_runner.py)) | `total_updates` = count of parsed `PACKAGE:` lines (ground truth), status-line count is discarded. A host reads "up to date" only when 0 packages parse. This is correct given correct playbook output — the bug was upstream in the playbook. |

## Repo conventions worth remembering
- **Release flow (solo dev, main is the only release branch):** commit on a
  branch → `git merge --ff-only` into `main` → grep-verify the change is in
  main's tree → `scripts/push_new_build.sh <version> "<msg>"`. Skipping the
  land-on-main step has shipped stale builds before (memory
  `feedback_release_workflow`).
- `push_new_build.sh` bumps VERSION + docker-compose + k8s tags, then
  commits/tags/pushes. Non-interactive runs require
  `PATCHPILOT_RELEASE_APPROVED=1` and a commit message as `$2`.
- The main working tree (`/Users/sanborn/github/patchpilot`) is **outside the
  agent sandbox's writable paths** — merges/releases there need the sandbox
  disabled. `gh` also needs the sandbox off (its config dir is read-denied).
- Ansible shell-block rules (memory `feedback_ansible_shell_block_quoting`): no
  em-dashes, no quote chars in `#` comments inside `shell: |`; run
  `ansible-playbook --syntax-check` before every push that touches one.

## Quick-reference commands
Ground-truth apt count on 10.0.1.101 (run as any user; system-wide):
```bash
sudo apt-get update -qq
```
```bash
apt list --upgradable 2>/dev/null | tail -n +2 | wc -l
```
Reproduce PatchPilot's exact (now-fixed) pipeline on the host:
```bash
awk 'index($0, "/") == 0 { if (length($0) > 0) held[$0]=1; next } { name=$1; sub(/\/.*/,"",name); if (name in held) next; print }' <(apt-mark showhold 2>/dev/null) <(apt list --upgradable -o APT::Get::Always-Include-Phased-Updates=true 2>/dev/null | tail -n +2) | wc -l
```
Syntax-check the playbook before a release (sandbox needs a writable tmp):
```bash
ANSIBLE_LOCAL_TMP="$TMPDIR/ansible-tmp" ANSIBLE_HOME="$TMPDIR/ansible-home" ansible-playbook --syntax-check ansible/check-os-updates.yml
```

## Site B — admin password recovery (runbook)
Site B is `patchpilot.apps.1445.lan` (k3s; namespace `patchpilot`, backend
deploy `patchpilot-backend` container `backend`, postgres deploy
`patchpilot-postgres` container `postgres`). The **full_admin username is
`sanborn`** (not `admin`).

Passwords are bcrypt-hashed (self-contained, no encryption key involved), so a
lost password can only be **reset**, never recovered — there is no forgot-password
flow, and in-app change-password needs the current password. Reset with the
supported `backend/setup_admin.py` (upserts by username, reactivates, clears
sessions). Caveat: it sets `role='admin'`, which **demotes `sanborn` from
`full_admin`** — the startup auto-promotion ([backend/app.py:933](backend/app.py))
only re-promotes the earliest-created user and only at restart, so restore the
role explicitly afterward.

Reset password (prompts twice, min 8 chars, keeps it out of shell history):
```bash
kubectl -n patchpilot exec -it deploy/patchpilot-backend -c backend -- python setup_admin.py --username sanborn
```
Restore the full_admin role:
```bash
kubectl -n patchpilot exec -it deploy/patchpilot-postgres -c postgres -- psql -U patchpilot -d patchpilot -c "UPDATE users SET role='full_admin' WHERE username='sanborn';"
```
Confirm the account / which DB the backend is on:
```bash
kubectl -n patchpilot exec -it deploy/patchpilot-postgres -c postgres -- psql -U patchpilot -d patchpilot -c "SELECT username, role, is_active, last_login FROM users;"
```

## RBAC role gotcha — Settings/sidebar gated on full_admin
The sidebar is gated entirely on `currentUser.role` (from `/api/auth/me`,
[frontend/app.js:466](frontend/app.js)). Full_admin-only nav items:
`nav-general` (**Settings/General**), `nav-users` (**Users**), `nav-advanced`
(**Advanced**). An `admin` sees only the write items (Hosts mgmt, SSH Keys,
Schedules); a `viewer` sees none. The role label under the username in the
sidebar shows the current role ("Full Admin" / "Admin" / "Viewer") — the
quickest way to spot a demotion.

**The gotcha:** `setup_admin.py` sets `role='admin'` on every run, silently
demoting a full_admin. The startup auto-promotion
([backend/app.py:933](backend/app.py)) only re-promotes the *earliest-created*
user, so if `sanborn` isn't that row it stays `admin` and loses
Settings/Users/Advanced. **Hit on Site A today** — `sanborn` was logged in but
couldn't see Settings. This is the same demotion behind the Site B recovery
caveat above.

Fix (per site, against that site's kubeconfig context), then log out/in or
hard-refresh so `/api/auth/me` re-reads the role live:
```bash
kubectl -n patchpilot exec -it deploy/patchpilot-postgres -c postgres -- psql -U patchpilot -d patchpilot -c "UPDATE users SET role='full_admin' WHERE username='sanborn';"
```

## Open questions for next session
- After self-update, does 10.0.1.101's count match the host's ground truth
  exactly, or is there a residual off-by-one / arch-allowlist gap
  ([backend/ansible_runner.py:982](backend/ansible_runner.py) restricts to
  `amd64|arm64|all|i386`)?
- Are there other Debian/Ubuntu hosts in the fleet that were also masked by the
  no-holds bug and should be re-checked?

## Memory pointers
- `feedback_release_workflow` — land branch work on main before shipping
- `feedback_app_self_update` — self-update via PP, not `kubectl rollout restart`
- `feedback_no_touching_user_k3s` — user drives all cluster-touching commands
- `feedback_ansible_shell_block_quoting` — shell-block quoting rules + syntax-check
- `feedback_versioning` — use `scripts/push_new_build.sh`

## Recently shipped (this session)
- **v1.7.6** (`e0900dd`) — bump `ansible-core` 2.19.6 → 2.19.11, clears
  CVE-2026-11332 (high). Dependabot alert #16 auto-closed as `fixed`
  (2026-07-23 16:23 UTC). Exposure was nil (we never run
  `ansible-galaxy role install`).
- **v1.7.5** (`f670b37`) — apt update-detection fix: hold-filter no longer drops
  all updates on hosts with no holds; phased updates now counted via the correct
  `-o` flag. CHANGELOG entries added for both.
- **Ops (no code change, DB-only fixes):** diagnosed the Site B admin login
  (forgot password → reset runbook above) and the Site A RBAC demotion
  (`sanborn` was `admin`, missing Settings → promote to `full_admin`). Both trace
  to the same `setup_admin.py` role-demotion gotcha.
- **Non-issue:** "scheduled task not running" on manual Run Now turned out to be
  Lens pointed at the wrong cluster (logs read on the wrong backend). Note: the
  "▶ Run" button is fire-and-forget — it flips the schedule to `running` and
  returns success immediately, so real outcome lives in the `[Schedule <id>]`
  backend logs / `patch_schedules.last_status`, not the button response.
