# PatchPilot — Future Work / Follow-ups

Internal notes on improvements that are deferred but worth revisiting. Add new
entries at the top with a date and owner. Remove entries when shipped.

---

## 2026-04-26 — Auto-recover Fernet key on reinstall (R2 / R3)

**Context:** As of v0.17.x the Docker Compose uninstall now preserves
`{project}_backups` (parity with K3s `reclaimPolicy: Retain`). On the next
install, Docker Compose silently reuses the existing `backups` volume.

**The gap:** `install.sh` generates a brand-new `PATCHPILOT_ENCRYPTION_KEY`
(Fernet key) for the new `.env`. Old `.tgz` archives in the retained volume were
encrypted with the *previous* install's key, so they show up in the dashboard
but cannot be restored unless the user manually copies the old key into the new
`.env`. The current behavior matches K3s today (R1) — documented limitation.

**Options previously considered:**

- **R2 — interactive prompt at install time.** `install.sh` peeks into the
  existing volume (e.g. `docker run --rm -v {project}_backups:/data busybox …`),
  detects archives, and prompts: "Old backups found. Reuse old encryption key
  (recommended) / generate fresh?"

- **R3 — automatic import via recovery `.env`.** Pair with writing a copy of
  `.env` into `/backups/recovery/.env-<timestamp>` after install (the unshipped
  "B-bk1" idea). On startup the backend checks for the recovery file; if its
  Fernet key differs from the on-disk one *and* the on-disk one is freshly
  generated (no prior backups created with it), copy the recovery key into
  `.env` and restart. Net effect: download → uninstall → reinstall →
  archives restorable, no user action.

**Why deferred:** R3 is the cleanest UX but couples three things (uninstall
volume retention, install-time `.env` recovery write, backend startup key
swap). R2 is interactive, which conflicts with the `--no-interactive` /
web-installer paths. R1 (do nothing) is what shipped.

**Suggested next step:** Implement R3 once we have telemetry or a user report
indicating reinstall-with-old-backups is a real path people hit. Until then,
the manual workaround is documented for users who need it.
