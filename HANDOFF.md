# PatchPilot — Handoff (2026-11-18)

## TL;DR
2026-11-18 shipped **v1.9.3 — committed, tagged, pushed (CI building images), NOT
yet deployed to Site A or Site B.** Deploy via PatchPilot's own in-app update,
Site A (patchpilot.apps.lan) first to confirm check behaviour, then Site B.
- **Fixes stale `UNREACHABLE` host status.** Symptom: hosts (e.g. rig1.lan) showed
  `unreachable` overnight but were actually up — a single-host Test Connection
  cleared it. Root cause: the whole-fleet Ansible check has a wall-clock timeout,
  and one slow host (`softwareupdate -l`, or `apt` under load) pushed the run past
  it; on timeout `run_check` discarded the **entire** run, so recovered hosts kept
  their stale status. The periodic re-check (`periodic_ansible_check`, every
  `refresh_interval`s, default 120s) does re-check unreachable hosts — it just
  never got usable output when the run timed out.
- **Three fixes (v1.9.3):** (1) bounded the slow per-host check tasks
  (`softwareupdate -l` via the detected timeout binary; `apt-get update`/`apt list`
  via `timeout`) plus an `ANSIBLE_TASK_TIMEOUT=240` backstop on the check run;
  (2) `run_check` now streams output and on timeout **saves results for hosts that
  finished** (new per-host `HOSTSTATUS … | check-complete` marker in
  `check-os-updates.yml`), leaving mid-scan hosts at their last known status
  instead of wiping the whole fleet; (3) new configurable `check_timeout_seconds`
  setting (default **600**, was a hardcoded 300; DB/env only, no Settings UI field
  yet). Files: `ansible/check-os-updates.yml`, `backend/ansible_runner.py`,
  `backend/app.py`.

---

2026-11-14 shipped v1.8.5 → v1.9.2, **all deployed to Site A and Site B**.
- **In-app updates now apply Kubernetes manifest changes** (v1.9.0), not just
  image tags: a `patchpilot-update` Job from the new image renders and applies
  that release's workload templates. Proven on both sites for 1.9.1 and 1.9.2.
- **The installer silently ignored its whole config file** on any machine
  without PyYAML (incl. this Mac) — fixed in v1.9.2 (`k8s/yaml_get.py`).
- **Per-site installer configs exist** (git-ignored):
  `k8s/install-config.site-a.yaml`, `k8s/install-config.site-b.yaml`, built
  from live state and verified by a write-blocked dry run + `kubectl diff`. The
  old default `k8s/install-config.yaml` (0.12.2-alpha, TLS off) was renamed
  `k8s/install-config.stale-2026-05-31.yaml`; a run without `--config` now stops.
- Backend limits default to 1 CPU / 1Gi (v1.9.2); busybox 1.38 everywhere.

## Deferred / known unfinished — DO THIS NEXT
1. **Postgres 15 → 18 is deliberately NOT done.** The updater refuses a major
   change. It needs, in order: (a) `postgresql-client-18` from the PGDG apt repo
   in the backend `Dockerfile` (Debian bookworm's client is 15, and `pg_dump` 15
   refuses a newer server, so in-app backups would break), shipped first since
   pg_dump 18 can dump a 15 server; (b) an upgrade path — backup → fresh 18 data
   dir → restore — in the installer and/or updater. PG18 images also moved the
   default data path to `/var/lib/postgresql/18/docker`; the template sets
   `PGDATA` explicitly. Quartermaster shows the finding; it was left
   dismissed/snoozed. PG15 support runs to ~Nov 2027.
2. **Site B's ingress names a ClusterIssuer that doesn't exist.** It asks for
   `lan-ca-issuer`; Site B's is `1445-lan-ca-issuer`. Its `Certificate` sits in
   `DoesNotExist` and Traefik serves the `*.apps.1445.lan` wildcard. The operator
   wants the cert setup unchanged, so `install-config.site-b.yaml` mirrors it and
   the installer now warns about it on every run. Fixing it is the operator's call.
3. **The v1.7.8 ClusterRole narrowing** (`clusterissuers` delete scoped to
   `letsencrypt-prod`) is on neither site. It lands on the next installer run with
   the site config. Full updates were enabled by applying only the Role doc.
4. **Docker installs still update image tags only** — the compose file is not
   refreshed from the release.
5. **The installer has no keep-larger guard for resources** (the updater does).
   A future hand-raise above the template would be reset by an installer re-run.
6. **`k8s/build-push.sh` has its own YAML reader** (developer build tool). Not
   checked for the nested-key fallback bug fixed in the installer.

### Carried over from 2026-10-09 — not revisited, status unknown (not done)
1. **Confirm a full check runs cleanly** on the current image at both sites
   (first ran on Python 3.12 / ansible-core 2.21 in v1.8.3). A wave of
   "unreachable" means look at `kubectl logs` for tracebacks first.
2. **Click-test the five rewired web controls.** v1.8.1 replaced five inline
   event handlers with delegated listeners. Escaping cannot break behavior, but
   delegation can, and none of it has been exercised in a browser:
   host-row checkbox → Details button → package-ID **Copy** in the host detail
   modal → package-ID `<code>` in the Packages table → **Mark Rebooted** on a
   reboot alert. A dead control means the delegation, not the escaping.
3. **Verify the incomplete-check alert** with the forced-row command below.
   Expect an amber "last check did not complete" warning while the host's status
   badge stays unchanged.
4. **A forced test row may still be live in the DB** on `johns-macmini.lan`:
   `check_fail_reason = 'forced test of the iOS check-failed card'`. It clears
   itself on the next successful check. If anyone reports a strange alert on that
   Mac, this is it — clear it with the command below.
5. **The 300s check timeout is still hardcoded** ([backend/ansible_runner.py:329](backend/ansible_runner.py:329), re-confirmed 2026-10-09)
   and is what triggered today's false-unreachable. `softwareupdate -l` on a Mac
   mid-update cycle can consume most of it alone. Consider raising it, making it
   configurable, or bounding the macOS system-update scan separately.
6. **Has anyone actually looked at the iOS Check Failed card?** It compiled four
   times (Debug and Release both clean) but was never seen rendering. Check it
   via TestFlight against the deployed backend: reason and "Failing since" will
   render; the amber remediation line needs the 1.8.1 backend.

## What works today (don't break these)
| Behavior | Notes |
|---|---|
| Check-failure reasons | Playbook emits `HOSTSTATUS: <host> \| unreachable \| <reason>` ([ansible/check-os-updates.yml:89](ansible/check-os-updates.yml)). The reason field is **optional in the parser** so an `/ansible` PVC still holding an older playbook keeps working — don't make it mandatory. |
| Status token stays `unreachable` | **Do NOT add a new status string** (e.g. `check-failed`). iOS `Host.status` is a strict Swift enum ([ios/PatchPilot/Models/Host.swift](ios/PatchPilot/Models/Host.swift)) — an unknown value fails JSON decode and breaks the host list with no compile-time warning. Web stats counters, status badges, the patch guard and the scheduled-patch retry skip all key off it too. The *reason* carries the distinction, not the status. |
| A host missing from Ansible output is **not** asserted down | `record_check_incomplete()` records only a reason. **Do not restore** `status='unreachable'`, `total_updates=0`, or the `delete_packages_for_host` / `delete_duplicate_apps_for_host` calls in that branch — that emptied a healthy host's update list, and inside a patch window the zeroed count made the host look "resolved" to the retry logic while the status made it skippable, so a slow check could silently cause a host to miss its window. |
| `last_checked` left stale for an unreported host | Deliberate — it's what keeps the staleness auto-trigger firing for a host that didn't report. |
| No inline event handlers carrying interpolated values | frontend/app.js passes values in `data-*` attributes read via `dataset`, dispatched by `initDelegatedHandlers()`. **HTML-escaping cannot secure `onclick="fn('${x}')"`**: an inline handler's attribute is HTML-decoded *before* it is compiled as JavaScript, so `&#39;` decodes back to `'` and still closes the string. Don't reintroduce the pattern. |
| All managed-host data escaped on render | Package names, versions and update types come verbatim from a host's package manager; `os_family`, `os_type` and `ip_address` from its Ansible facts. Every render path goes through `escapeHtml`, which **now also escapes `'`**. A new template interpolating host data must escape it. |
| Failure diagnostics use `print()`, not `logger.debug` | The root logger's only handler is the in-memory ring buffer behind `/api/backend-logs` ([backend/app.py:57](backend/app.py)) — there is no StreamHandler, so `logger.*` calls **never reach `kubectl logs` at any level**. Keep new diagnostics on `print()`. |
| Persistent SSH known_hosts (v1.7.8) | `/ansible/patchpilot_known_hosts` with `StrictHostKeyChecking=accept-new`. A re-imaged host needs its stale entry removed (`ssh-keygen -f <path> -R <host>`) or it will genuinely fail. |
| apt update detection (v1.7.5) | Hold-filter keys off line **shape**, not file position. **Do NOT revert to the `NR==FNR` idiom** — it silently drops all updates on hosts with no holds. |
| Phased updates | Via `-o APT::Get::Always-Include-Phased-Updates=true` on the `apt list` call. The `APT_GET_ALWAYS_INCLUDE_PHASED_UPDATES=1` env var was a no-op — don't reintroduce it. |
| Homebrew pin-filter (macOS) | Shell `while read` loop, already handles an empty `brew list --pinned`. |
| Broken-keg hint (code in v1.8.2, first shipped image v1.8.3) | `brew_broken_keg_hint()` in [backend/ansible_runner.py](backend/ansible_runner.py) matches `…/Cellar/<formula>/<ver> is not a directory` (Apple Silicon and Intel prefixes) and emits a `💡` line naming the formula and the `brew reinstall` fix. It is hooked into **all three** brew log paths: stdout loop, stderr loop, and the joined "Show Homebrew update results" debug line, which is where the real error surfaced. If you add a new brew output path, call it there too. This is a hint only: PP does not auto-repair the host. |
| `brew upgrade` stays one call | A single broken formula aborts the whole run, so nothing else on that host upgrades. That's why the hint exists. Don't assume a quiet brew task means "nothing to upgrade"; check the log for the 💡 line. |
| Backend base image is Python ≥3.12 (v1.8.3) | `Dockerfile` uses `python:3.12-slim-bookworm` because ansible-core ≥2.20 refuses older Python. Don't drop back to 3.11, and don't pin ansible-core below 2.20 to make a build pass: that reverts a security fix. |
| Backend parser reconciliation | `total_updates` = count of parsed `PACKAGE:` lines (ground truth); the status-line count is discarded. |
| In-app manifest updates (v1.9.0) | [backend/manifest_updater.py](backend/manifest_updater.py). The old backend launches Job `patchpilot-update` from the **new** image, which carries its release's `03-postgres`/`04-backend`/`05-frontend` templates at `/app/k8s-templates`. Renders them with ConfigMap `patchpilot-install-values`, refuses (changing nothing) on a Postgres major change, an unsaved `PP_*` setting, or any kind beyond Deployment/Service/ConfigMap; never lowers running CPU/memory; server-side dry run → apply → waits for rollouts. Installs without the ConfigMap or the wider Role fall back to the old image-only path, and Settings → Updates says so. |
| `SAVED_KEYS` ⇔ installer ⇔ templates | A new `${PP_X}` in a workload template must also be saved by `save_install_values()` in `install-k3s.sh` and listed in `SAVED_KEYS` — tests enforce both. **Existing installs' ConfigMaps won't have it**, so the updater will refuse that release with "re-run install-k3s.sh"; plan for that (default it in the updater, or ship it with an installer run). |
| Installer config reader (v1.9.2) | `k8s/yaml_get.py`, not an inline heredoc. Reads nested keys with or without PyYAML (tests compare it to PyYAML for every key the installer reads) and **exits non-zero on an unreadable config** so `set -e` stops the install. The old inline fallback returned the default for every nested key. Don't move it back inline. |
| `--dry-run` writes nothing (v1.9.2) | It used to create the namespace and pull secret. Recipe for a live install: dry-run with the site config, then `kubectl diff -f k8s/.generated/<file>`. `k8s/.generated/01-secrets.yaml` then holds the real password and key — delete it after. |
| Live-install invariants for a re-run | `postgres.storageClass` (A `app-data`, B `local-path`) must match: PVC classes are immutable, and a hostPath/static mode triggers `clean_node_data_dirs` (an `rm -rf` of the Postgres data dir) and `_reconcile_pv` (deletes the Postgres PV). Keep `certManager.createClusterIssuer: false` on both sites — `true` would write a ClusterIssuer named after `tls.clusterIssuer` (= the LAN CA issuer). |

## Decision required / strategic crossroads
- **Accept or revisit the "preserve last known status" trade.** A host the run
  never reported on now keeps its previous status, so a genuinely dead host can
  read "up-to-date" with only an amber warning beside it. The justification: a
  truly unreachable host normally *does* appear in Ansible output with its own
  `HOSTSTATUS` marker, because `ignore_unreachable` keeps the play running — so
  the missing-entirely case is dominated by timeouts and aborts, not real
  outages. Raised with the operator 2026-09-15, not yet accepted or rejected.
- **Should PatchPilot exclude Xcode / Command Line Tools updates on macOS hosts
  by default?** Installing one re-arms the Xcode licence prompt, which breaks
  `/usr/bin/python3` and therefore every subsequent check on that host (see
  memory `project-macos-xcode-license`). `mas` already excludes Xcode by default
  via `MAS_EXCLUDED_IDS`; this would be the same idea for `softwareupdate`.

## Repo conventions worth remembering
- **Release flow (solo dev, main is the only release branch):** land work on
  `main` → grep-verify the change is in main's tree →
  `scripts/push_new_build.sh <version> "<msg>"` (memory
  `feedback_release_workflow`).
- **`push_new_build.sh` runs `git add -A`.** Commit the real change first,
  then `git stash push -- <path>` any unrelated dirty file (usually the iOS
  `UserInterfaceState.xcuserstate`) before running the script, and
  `git stash pop` afterwards. Otherwise the stray file lands in the release
  commit. Used this way for v1.8.2 and v1.8.3.
- `push_new_build.sh` bumps VERSION + docker-compose + k8s tags, then
  commits/tags/pushes. Non-interactive runs need `PATCHPILOT_RELEASE_APPROVED=1`
  and a commit message as `$2`; without it, it updates the files, prints the git
  commands and exits 2 without pushing — which is the useful way to bump
  versions without releasing.
- **`push_new_build.sh` does NOT touch the iOS project.** Bump
  `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in
  `ios/PatchPilot.xcodeproj/project.pbxproj` by hand (currently 1.8.1 / build 2).
  TestFlight rejects a duplicate version+build pair.
- **Tag AFTER any rebase, never before.** v1.8.0 was tagged, then rebased onto
  `origin/main` to pick up a Dependabot merge — which orphaned the tag onto a
  discarded commit. Correct sequence:
  `git fetch origin && git rebase origin/main && git tag <v> && git push && git push origin <v>`.
- **Agent sandbox facts (corrected 2026-09-15):** the repo working tree *is*
  writable by the agent. `~/.config/gh` is read-denied, so `git push` and `gh`
  fail for the agent and must be run by the operator — which also means the
  repo-visibility pre-push check can't be performed by the agent. Writes to
  `~/.claude/**` (including `memory/MEMORY.md`) must use the file tools, not Bash.
  `xcrun simctl` cannot reach CoreSimulatorService from the sandbox.
- Ansible syntax-check needs `ANSIBLE_LOCAL_TEMP` (not `ANSIBLE_LOCAL_TMP`).
- Ansible shell-block rules (memory `feedback_ansible_shell_block_quoting`): no
  em-dashes, no quote chars in `#` comments inside `shell: |`; run
  `--syntax-check` before every push that touches one.
- `/tmp/ansible_last_run.txt` in the backend pod holds the last **completed**
  check's full Ansible output — it is written only after `communicate()` returns,
  so after a timeout it is a *stale snapshot of the previous successful run*. A
  healthy-looking dump there alongside a red host is the tell.

## Quick-reference commands
Re-run the installer on a site (always with its config and kubeconfig):
```bash
KUBECONFIG=~/.kube/k3s.config ./k8s/install-k3s.sh --config k8s/install-config.site-a.yaml
```
```bash
KUBECONFIG=~/.kube/k3s-1445.config ./k8s/install-k3s.sh --config k8s/install-config.site-b.yaml
```
See what the last in-app update did (Job log is kept for an hour):
```bash
kubectl logs -n patchpilot job/patchpilot-update
```
Run the backend tests (needs a venv with `pyyaml` and `pytest`):
```bash
python -m pytest backend/tests -q
```
Force an incomplete-check reason to test the alert and the iOS card:
```bash
kubectl exec -n patchpilot deploy/patchpilot-postgres -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE hosts SET check_fail_reason = '"'"'forced test'"'"', check_fail_at = NOW() WHERE hostname = '"'"'johns-macmini.lan'"'"';"'
```
Clear it:
```bash
kubectl exec -n patchpilot deploy/patchpilot-postgres -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE hosts SET check_fail_reason = NULL, check_fail_at = NULL WHERE hostname = '"'"'johns-macmini.lan'"'"';"'
```
See why a check failed (v1.8.0+ prints the reason and Ansible's own fatal lines):
```bash
kubectl logs -n patchpilot deploy/patchpilot-backend --tail=300 | grep -E "reason=|\[ANSIBLE\]|\[PARSER\]|\[CHECK\]"
```
Read the last completed check's full Ansible output:
```bash
kubectl exec -n patchpilot deploy/patchpilot-backend -- grep -i "<hostname>" /tmp/ansible_last_run.txt
```
Remove a stale SSH host key after re-imaging a host:
```bash
kubectl exec -n patchpilot deploy/patchpilot-backend -- ssh-keygen -f /ansible/patchpilot_known_hosts -R <hostname>
```
Repair a broken Homebrew keg on a macOS host (the 💡 hint names the formula):
```bash
brew reinstall <formula>
```
If reinstall fails with the same error:
```bash
brew uninstall --force --ignore-dependencies <formula> && brew install <formula>
```
Check the interpreter Ansible actually uses on a macOS host (not the Homebrew
`python3` on an interactive PATH):
```bash
ssh -o BatchMode=yes <user>@<host> '/usr/bin/python3 -V'
```
Syntax-check the playbook before a release:
```bash
ANSIBLE_LOCAL_TEMP="$TMPDIR/ansible-tmp" ANSIBLE_HOME="$TMPDIR/ansible-home" ansible-playbook --syntax-check ansible/check-os-updates.yml
```
Ground-truth apt count on a Debian host:
```bash
apt list --upgradable 2>/dev/null | tail -n +2 | wc -l
```
Add an internal CA to an iOS simulator so the app can reach a `.lan` host:
```bash
xcrun simctl keychain booted add-root-cert ~/Documents/Beacon-DEV-network-info/beacon-dev-lan-root-ca.crt
```

## Site B — admin password recovery (runbook)
Site B is `patchpilot.apps.1445.lan` (k3s; namespace `patchpilot`, backend
deploy `patchpilot-backend` container `backend`, postgres deploy
`patchpilot-postgres` container `postgres`). The **full_admin username is
`sanborn`** (not `admin`).

Passwords are bcrypt-hashed (self-contained, no encryption key involved), so a
lost password can only be **reset**, never recovered — there is no
forgot-password flow, and in-app change-password needs the current password.
Reset with the supported `backend/setup_admin.py` (upserts by username,
reactivates, clears sessions). Caveat: it sets `role='admin'`, which **demotes
`sanborn` from `full_admin`** — the startup auto-promotion
([backend/app.py:935](backend/app.py)) only re-promotes the earliest-created
user and only at restart, so restore the role explicitly afterward.

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
The sidebar is gated entirely on `currentUser.role` (from `/api/auth/me`).
Full_admin-only nav items: `nav-general` (**Settings/General**), `nav-users`
(**Users**), `nav-advanced` (**Advanced**). An `admin` sees only the write items
(Hosts mgmt, SSH Keys, Schedules); a `viewer` sees none. The role label under the
username in the sidebar shows the current role — the quickest way to spot a
demotion.

**The gotcha:** `setup_admin.py` sets `role='admin'` on every run, silently
demoting a full_admin. The startup auto-promotion only re-promotes the
*earliest-created* user, so if `sanborn` isn't that row it stays `admin` and
loses Settings/Users/Advanced. Fix with the role-restore command above, then log
out/in or hard-refresh so `/api/auth/me` re-reads the role.

## Open questions for next session
- **Only `frontend/app.js` was audited for the XSS pattern.** `setup.html` and
  any other frontend JS were not looked at. Same audit is worth running there:
  find `innerHTML` sinks, check whether interpolated values originate from a
  managed host, and confirm `escapeHtml` is applied.
- `10.0.1.101` now reports `up-to-date / 0`. That's consistent with the v1.7.5
  fix plus the deliberate NVIDIA `apt-mark hold` freeze (memory
  `project_k3s_nvidia_holds`), but it was **not** explicitly re-verified against
  the host's ground-truth count — the July handoff item was never closed out.
- **Xcode / CoreSimulator skew on johns-macmini.** `xcrun simctl list devices`
  from the operator's shell shows `iPhone 17 Pro (Booted)` while the agent's
  simulator tooling reports it `Shutdown`; build logs show `DVTCoreDeviceCore`
  failing with a symbol mismatch against
  `/Library/Developer/PrivateFrameworks/CoreDevice.framework`. The device archive
  to TestFlight succeeded, so this is simulator-discovery only. Suspected fix:
  `sudo xcodebuild -runFirstLaunch`.
- Should the iOS app consume `/api/alerts`? It currently doesn't — the Check
  Failed card reads `check_fail_reason` off the host object instead, so
  reboot-required and duplicate-app alerts are web-only.

## Memory pointers
- `project-macos-xcode-license` — **an unaccepted Xcode licence breaks
  `/usr/bin/python3`, so Ansible's `setup` fails over a healthy SSH connection
  and PP reports the host unreachable.** Check this first on any macOS host that
  is "unreachable" while its connection test passes.
- `project-false-unreachable-timeout` — an "unreachable" that clears itself is a
  timed-out check run, not a host problem.
- `feedback_release_workflow` — land branch work on main before shipping
- `feedback_app_self_update` — self-update via PP, not `kubectl rollout restart`
- `feedback_no_touching_user_k3s` — operator drives all cluster-touching commands
- `feedback_ansible_shell_block_quoting` — shell-block rules + syntax-check
- `feedback_versioning` — use `scripts/push_new_build.sh`
- `feedback_dashboard_locked_patterns` — 5-card stats row and sidebar nav are
  locked in; don't propose restructuring them

## Recently shipped
### 2026-11-14
- **v1.8.5** (operator): busybox 1.36 → 1.38 — but only in `deployment.yaml` and
  the installer's uninstall Job; `templates/04-backend.yaml` and
  `backend/uninstall_api.py` were missed and fixed in v1.9.0. Site A's
  `wait-for-postgres` was moved to 1.38 by hand with `kubectl set image`, since
  the image-only updater never touched it.
- **v1.9.0** (`56eac41` + release `fcaf9b4`): manifest-applying in-app updates;
  `patchpilot-install-values` saved by the installer; Role gains
  deployments/services/configmaps create+patch and `pods/log` get. Also fixed:
  literal `${PP_LICENSE_PROVIDER:-freemius}` env values (envsubst ignores `:-`),
  updater ignoring a custom namespace (read `PP_NAMESPACE`, never set), update
  progress screen giving up after 3 min.
- **Ops — full updates enabled from live state, not an installer run** (the
  installer config was stale): each site's `patchpilot-install-values` was built
  from its live Deployments and only the Role doc of `00b-rbac.yaml` applied.
- **v1.9.1** (`b471e96`): update-scope line moved into Version Status. First real
  manifest update: Site B got busybox 1.38 + real license env; Postgres untouched.
- **v1.9.2** (`f71105d`): installer config reader, email prompts only when
  creating a ClusterIssuer (+ warning when the named one is missing), `--dry-run`
  writes nothing, backend limits 1 CPU / 1Gi (Site B rose on update; Site A was
  already there by hand since 2026-10-01).
- **Postgres restart counts** (23 on A, 6 on B) are k3s service restarts —
  last exit `Unknown/255`, same minute as every other pod; nodes never went NotReady.

### 2026-10-09
- **Backend base image → `python:3.12-slim-bookworm`.** The v1.8.2 CI image
  build failed at `pip install -r requirements.txt`: Dependabot/Aikido's
  ansible-core 2.21.1 bump (`e9f52e6`) needs Python ≥3.12, and v1.8.2 was the
  first tag built since that bump. **No v1.8.2 images exist.** The fix shipped
  as **v1.8.3** (`302334a` release, fix in `6bb623e`): CI passed, deployed to
  Site A then Site B.
  **Don't pin ansible-core back below 2.20** to "fix" a build: that reverts a
  security update.
- **v1.8.2** (`46df68a` release, fix in `47ba615`): broken-Homebrew-keg hint in
  the patch log (see "What works today"). Confirmed there are no hardcoded
  `pipx` references anywhere in the repo; the failure was purely host-side.
- **Ops:** johns-mbp.lan had a stale pipx keg (`Error:
  /opt/homebrew/Cellar/pipx/1.17.12 is not a directory`) that aborted every brew
  upgrade. The operator ran `brew reinstall pipx` and patching then succeeded.

### Between sessions (2026-09-15 → 2026-10-09, not by the agent)
- `857ddea` "updated limits": k8s/deployment.yaml resource bump, memory 512Mi → 1024Mi and
  cpu 500m → 1.
- `e9f52e6` ansible-core 2.19.11 → 2.21.1 (Dependabot security fix, #17).
- `b38edd6` accepted Xcode's recommended iOS project settings.

### 2026-09-15
- **v1.8.1** (tag → `98f18be`) — **Security:** stored XSS in the dashboard.
  `frontend/app.js` interpolated managed-host data into `innerHTML` unescaped
  across 23 sites, and five of those were inline event handlers where escaping is
  not a fix. A compromised managed host, or a hostile package name reaching one,
  could have executed script in the operator's authenticated session — which can
  start fleet-wide patch runs. v1.8.0 had widened this by putting remote
  `setup`-failure text into the alert message. `escapeHtml` also didn't escape
  `'`. **Fixed:** a check that never reported on a host no longer writes
  `status='unreachable'`, `total_updates=0` or deletes its package rows.
  **Added:** `check_incomplete` warning alert, `check_fail_hint` on the hosts
  endpoints, and the iOS Check Failed card.
- **v1.8.0** (`2d6f74c`) — a failed check now reports *why*. The playbook had
  collapsed "SSH failed" and "fact gathering failed" into one status, and
  `ignore_unreachable` + `ignore_errors` each convert their task into
  `ok`+`ignored`, so the PLAY RECAP read `unreachable=0 failed=0` either way and
  no signal survived. Reason is now persisted per host (`check_fail_reason`,
  `check_fail_at`, auto-migrated) and surfaced in alerts with remediation for
  known macOS causes. **Deployed and running.**
- **iOS 1.8.1 build 2** — on TestFlight, active. Debug and Release both compile
  clean; version strings verified in the built product.
- **Ops:** the original "macmini unreachable" was an unaccepted Xcode licence
  after an Xcode update — accepted on the host, which fixed it. The Beacon-dev
  LAN root CA was added to the iOS simulator's trust store so the app can reach
  `patchpilot.apps.lan` (the app has no custom `URLSessionDelegate`, and
  `NSAllowsArbitraryLoads` does **not** bypass certificate validation).
- **`PATCHPILOT-HANDOFF.md` is now tracked** (committed in `98f18be`). It carries
  product positioning and marketing notes; it had been deliberately kept out of
  the v1.8.1 release commit while repo visibility was unverified, and the agent
  was never able to verify whether `linit01/patchpilot` is public or private.
