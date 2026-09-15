# PatchPilot — Rename, Repackage & Launch Handoff

**Date:** 2026-08-23
**Repo:** `github.com/linit01/patchpilot`
**Current version:** v1.7.2 (154 releases, 285 commits)
**Status:** Shipped, commercially licensed, zero external users.

---

## 1. Why this work exists

PatchPilot was built for personal use and later ported to a paid product. It has never
been marketed. Research on 2026-08-23 surfaced three problems that must be fixed before
any launch attempt:

1. **Name collision.** `patch-pilot.app` (github.com/DazClimax/patchpilot) is another
   self-hosted patch manager called PatchPilot — same name, same category, same audience,
   GPL-3.0 and free. Our domain `getpatchpilot.app` is the classic second-arrival prefix.
2. **Wrong target market.** The homelab Linux patch-management niche is owned by PatchMon
   (~2,073 stars, AGPLv3 free self-host, funded UK company, cloud from $1/host/mo,
   Hostinger one-click deploys). Entering as the paid, proprietary, second-named option
   is not winnable.
3. **Correctness defects** in the shipping artifacts (see §3) that damage first-touch
   credibility.

**The repositioning:** PatchMon covers Linux, FreeBSD and Windows — *not macOS*. FleetDM
needs engineering effort and is usually paired with Munki for Mac patching. Jamf is
Apple-only and enterprise-priced. **A small mixed fleet containing Macs has no good
answer.** We already patch brew, softwareupdate, Mac App Store IDs, apt, dnf, winget and
PSWindowsUpdate from one dashboard. That is the gap.

**Scope discipline:** this is a one-weekend, low-stakes experiment. Goal is "does anyone
want this," not building a business. Do not gold-plate.

---

## 2. Phase 0 — Naming decision (blocks everything else)

Rename now. With zero users the migration cost is near zero; it rises permanently from here.

### Shortlist

The existing estate uses naval naming (quartermaster, quarterdeck, purser, helmsman,
signalman, lookout, bridge, vertrep, muster roll). These continue that convention.

| Name | Rationale | Notes |
|---|---|---|
| **Careen** | To careen is to heel a ship over for hull maintenance — literally scheduled upkeep. | Obscure, likely available, needs one line of explanation. |
| **Refit** | Naval refit = periodic overhaul. Legible to non-naval buyers. | Check collision with the old rEFIt Mac boot manager (dormant but memorable). |
| **Shipshape** | "In good order, properly maintained." Immediately understood by anyone. | Most legible option; most likely already taken. Check first. |
| **Holystone** | Sandstone block used to scrub decks — the ritual of routine upkeep. | Distinctive, very likely available. |
| **Watchbill** | The duty roster. Ties directly to the scheduling feature. | Good if we lead on maintenance windows. |
| **Capstan** | Mechanical hauling gear. | Weaker semantic tie to maintenance. |

Non-naval fallbacks if domains fail: **Patchwright**, **Fleetwright**, **Tidewatch**, **Evenkeel**.

### Names to avoid (verified collisions)
- **Bosun / Boatswain** — Bosun is Stack Exchange's monitoring/alerting tool. Same ops space.
- **Shipwright** — CNCF Shipwright, Kubernetes build strategies. Same ecosystem.

### Selection checklist
- [ ] Domain available (`.app` preferred to match current setup; `.com` if cheap)
- [ ] GitHub org/repo name free
- [ ] Docker Hub namespace free
- [ ] No existing project in patch management, k8s, or sysadmin tooling — search GitHub,
      PyPI, npm, and plain web before committing
- [ ] Not an active trademark in software/IT. If this ever earns real money, get a lawyer's
      read before registering anything.

---

## 3. Phase 1 — Correctness fixes (do regardless of rename)

These are all first-touch credibility issues found during review.

- [ ] **Payment provider mismatch.** Site checkout points at Freemius
      (`checkout.freemius.com/app/28811/...`). README documents LemonSqueezy end to end —
      activation flow, 7-day validation interval, 30-day offline grace, machine binding.
      Migration to Freemius is confirmed. **Rewrite the entire Licensing section of README.md**
      to match Freemius reality: activation steps, validation behaviour, grace period,
      deactivation/transfer. Audit `backend/license.py` for stale LemonSqueezy references.
- [ ] **Stale version badge.** README shields badge says `version-1.0.0`. VERSION file says
      1.7.2. Reads as abandoned. Either bump on release or switch to a dynamic badge.
- [ ] **`.cache_ggshield` is committed.** Add to `.gitignore` and `git rm --cached`. Minor,
      but the buyer persona is security-minded sysadmins who will notice.
- [ ] **Windows support is undersold.** Repo title and GitHub About both say "Linux & macOS"
      while the feature list includes winget and PSWindowsUpdate, and the site says
      "Linux, macOS & Windows." Fix repo description, README H1, and og/meta tags.
- [ ] **Site version drift.** Homepage says "v1.7"; latest release is v1.7.2.
- [ ] **Install script presentation.** `curl | bash` as the headline is normal but draws
      loud objections from this audience. Show the download-inspect-run variant alongside it.

---

## 4. Phase 2 — Rename migration

Do in one pass; do not half-migrate. Touch points:

- [ ] GitHub repo name + description + topics
- [ ] Domain + all site copy, meta tags, og:image, canonical URLs
- [ ] `install.sh`, `install.ps1`, and every documented curl URL
- [ ] Docker Hub / GHCR image names (`linit01/patchpilot` → new)
- [ ] Kubernetes manifests: namespace, deployment names, service names, ingress hosts
- [ ] `.env` variable prefix (`PATCHPILOT_ENCRYPTION_KEY` → new). Support both names for one
      release with a deprecation warning. Key loss is *not* the risk — backups embed the key,
      uninstall preserves the backup volume, and a fresh install can restore fully including
      license. The risk is a broken in-place upgrade forcing an unnecessary restore.
- [ ] **Preserve backup volume identity — this is the real hazard of the rename.** The
      restore-from-backup safety net assumes the backup volume is still where the app looks:
      - *Docker Compose:* volumes are namespaced by project name (defaults to directory name).
        Renaming the directory yields a new empty `<newname>_backups` volume; the old one
        persists on disk but is never mounted. Pin the project name explicitly
        (`COMPOSE_PROJECT_NAME` or `name:` in the compose file) or document the reattach step.
      - *Kubernetes:* PVCs are namespaced. Changing `namespace: patchpilot` binds a fresh PVC
        and orphans the NFS-backed original. Either keep the namespace or document migration.
- [ ] Restore path must accept backups written under the old env var name and old volume layout
- [ ] Freemius machine binding — confirm a restore isn't counted as a new activation, depending
      on how the machine fingerprint is derived
- [ ] Postgres database/user names (`patchpilot`) — migration path or fresh-install only
- [ ] Freemius product name and license email templates
- [ ] iOS app display name and bundle identifier
- [ ] README, QUICKSTART, KUBERNETES, USER_MANUAL, README_BACKUP_RESTORE, PROJECT_SUMMARY
- [ ] Keep old domain redirecting for a while; costs almost nothing

---

## 5. Phase 3 — Repositioning

### Homepage rewrite
Lead with the problem, not the feature list. Current page opens with features; it must open
with: *mixed fleets containing Macs have no good patch-management answer.*

Must appear above the fold:
- The mixed-fleet problem statement (Linux + macOS + Windows, one dashboard)
- The macOS gap — name that PatchMon and the alternatives don't cover it
- **The agentless answer to the objection.** Both competitors market *against* our
  architecture ("Without SSH. Without Exposure.", "outbound-only agents, no SSH or WinRM
  exposure"). Our counter: nothing to install on targets, no agent fleet to maintain, no
  agent to update, nothing to deploy on the office manager's MacBook. Make this case
  explicitly and early — do not let the competition frame it unanswered.
- The "why not just Ansible" answer: heterogeneous fleets, visibility, audit trail,
  scheduling. Our backend *is* Ansible; the product is everything around it.

### Business model changes (decide before launch)

- [ ] **Perpetual license instead of subscription.** Monthly creates an implied ongoing
      obligation. One-time price, higher than $49, updates for the current major version.
      Freemius supports one-time payments. This keeps a sale from becoming a relationship.
- [ ] **Community support, stated in writing on the sales page.** GitHub issues, best effort,
      no SLA. Optional paid support at an hourly rate set high enough that the call would be
      welcome. This single line is what protects retirement time.
- [ ] **Reconsider the license gate.** Backup/restore is currently paid-only, which makes the
      free tier the *unsafe* tier for a tool that changes production systems. Gating on host
      count instead (free ≤5 hosts, paid beyond) makes the trial a better demo and puts the
      paywall where budget actually exists. **This is real dev work — treat as optional for v1
      and decide on cost/benefit.**

---

## 6. Phase 4 — The launch test

One post. Then stop and watch.

- **Primary:** r/macsysadmin — Mac fleet patching is the underserved niche and we support it natively.
- **Secondary:** r/msp — different post, business framing, mixed fleets, RMM pricing pain.
- **Avoid r/selfhosted for now** — PatchMon comparison dominates and we lose it.
- Read each subreddit's self-promotion rules first. Post as a practitioner sharing a tool
  built for their own fleet, disclose the paid tier immediately and plainly.

### Success criteria
- ~10 installs = signal worth following, iterate on feedback
- ~0 installs after a fair post = clean stop, no further investment
- Either outcome is an acceptable end state

---

## 7. Non-goals

Explicitly out of scope. Do not let these creep in:

- Building an agent-based architecture to match competitors. Agentless is the differentiator.
- Compliance scanning, OpenSCAP/CIS, browser SSH, or feature-matching PatchMon.
- Productizing any other app in the estate. The other sixteen stay private.
- A portfolio site, content strategy, or consulting funnel.
- Any work that turns this into an obligation rather than an experiment.
