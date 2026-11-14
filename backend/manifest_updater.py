"""
PatchPilot — release manifest updater (Kubernetes)
==================================================
Applies a release's own workload manifests during an in-app update, so changes
to the Kubernetes spec (sidecar/init images, probes, resources, env vars) reach
existing installs instead of only the backend/frontend image tags.

How it runs:
  - The running (old) backend launches a Job from the NEW backend image.
  - That image carries its release's templates at /app/k8s-templates.
  - The Job renders them with the values the installer saved in the
    `patchpilot-install-values` ConfigMap, checks them, and applies them.

Scope is deliberately narrow: only the workload templates (Postgres, frontend,
backend — Deployments, Services and the nginx ConfigMap) are applied here.
Namespace, RBAC, Secrets, PVs/PVCs, Ingress and ClusterIssuers stay with
install-k3s.sh: they hold credentials or storage, and a ServiceAccount cannot
grant itself new permissions anyway.

The pure functions (render, unresolved, image_refs, job_manifest,
postgres_major, check_documents) are used by update_checker and covered by
backend/tests/test_manifest_updater.py; main() is the Job's entry point.

Exit codes: 0 applied, 1 failed, 3 refused (nothing was applied).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml

VALUES_CONFIGMAP = "patchpilot-install-values"
VALUES_SCHEMA = "1"
TEMPLATES_DIR = Path(os.getenv("PP_TEMPLATES_DIR", "/app/k8s-templates"))

# Applied in this order: Postgres first (the backend's init container waits for
# it), the backend last (applying it replaces the pod that launched this Job).
WORKLOAD_TEMPLATES = ("03-postgres.yaml", "05-frontend.yaml", "04-backend.yaml")
ALLOWED_KINDS = {"Deployment", "Service", "ConfigMap"}
DEPLOYMENTS = ("patchpilot-postgres", "patchpilot-frontend", "patchpilot-backend")

# Every setting the workload templates reference. The installer writes these to
# the ConfigMap; a template that needs one not listed here makes render refuse.
SAVED_KEYS = (
    "PP_NAMESPACE", "PP_VERSION", "PP_BACKEND_IMAGE", "PP_FRONTEND_IMAGE",
    "PP_IMAGE_PULL_POLICY", "PP_PULL_SECRET_NAME",
    "PP_DB_USER", "PP_DB_NAME",
    "PP_AUTO_REFRESH_INTERVAL", "PP_DEFAULT_SSH_USER", "PP_DEFAULT_SSH_PORT",
    "PP_BACKUP_STORAGE_TYPE", "PP_NFS_SERVER", "PP_NFS_SHARE",
    "PP_BACKUP_RETAIN_COUNT", "PP_MAX_BACKUP_SIZE_MB",
    "PP_KUBECTL_BIN", "PP_DATA_DIR",
    "PP_LICENSE_PROVIDER", "PP_FREEMIUS_PRODUCT_ID",
    "PP_DH_REPO", "PP_IMAGE_TAG", "PP_IMAGE_SUFFIX",
)

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 3

JOB_NAME = "patchpilot-update"
JOB_DEADLINE = 900  # seconds; pull + apply + three rollouts

# envsubst semantics: `$NAME` and `${NAME}` only. `${NAME:-default}` is NOT
# expanded by envsubst, so it is not expanded here either.
_VAR_RE = re.compile(r"\$(?:\{(PP_[A-Z0-9_]+)\}|(PP_[A-Z0-9_]+))")
_LEFTOVER_RE = re.compile(r"\$\{?(PP_[A-Z0-9_]+)")


class Refused(Exception):
    """The update would be unsafe or cannot be rendered; nothing was applied."""


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions
# ─────────────────────────────────────────────────────────────────────────────
def render(text: str, values: dict) -> str:
    """Substitute `$PP_X` / `${PP_X}` for keys present in `values`, leaving
    everything else untouched — the same result as the installer's envsubst
    call with an explicit variable list."""
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return str(values[name]) if name in values else m.group(0)
    return _VAR_RE.sub(sub, text)


def unresolved(text: str) -> list[str]:
    """PP_* references still present after render — settings this release's
    templates need that the saved install values do not have."""
    return sorted(set(_LEFTOVER_RE.findall(text)))


def image_refs(saved: dict, version: str) -> tuple[str, str]:
    """Backend and frontend image references for `version`. An install that
    tracks the `latest` tag keeps its references (the restart re-pulls)."""
    if saved.get("PP_IMAGE_TAG") == "latest":
        return saved["PP_BACKEND_IMAGE"], saved["PP_FRONTEND_IMAGE"]
    repo = saved["PP_DH_REPO"].rstrip("/")
    suffix = saved.get("PP_IMAGE_SUFFIX", "")
    return f"{repo}:backend-{version}{suffix}", f"{repo}:frontend-{version}{suffix}"


def job_manifest(namespace: str, saved: dict, version: str) -> dict:
    """The Job that applies a release. It runs the NEW backend image, because
    that image carries the release's templates; the old backend only launches
    it and follows it."""
    backend_image, frontend_image = image_refs(saved, version)
    latest = saved.get("PP_IMAGE_TAG") == "latest"
    command = [
        "python", "/app/manifest_updater.py",
        "--namespace", namespace,
        "--version", version,
        "--backend-image", backend_image,
        "--frontend-image", frontend_image,
    ]
    if latest:
        command.append("--restart")
    pod_spec = {
        "serviceAccountName": "patchpilot-backend",
        "restartPolicy": "Never",
        "containers": [{
            "name": "updater",
            "image": backend_image,
            "imagePullPolicy": "Always" if latest else (saved.get("PP_IMAGE_PULL_POLICY") or "IfNotPresent"),
            "command": command,
            "env": [{"name": "PATCHPILOT_NAMESPACE", "value": namespace}],
            "resources": {
                "requests": {"memory": "64Mi", "cpu": "50m"},
                "limits": {"memory": "256Mi", "cpu": "500m"},
            },
        }],
    }
    if saved.get("PP_PULL_SECRET_NAME"):
        pod_spec["imagePullSecrets"] = [{"name": saved["PP_PULL_SECRET_NAME"]}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": JOB_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "patchpilot",
                "app.kubernetes.io/component": "updater",
                "app.kubernetes.io/version": version,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": JOB_DEADLINE,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/component": "updater"}},
                "spec": pod_spec,
            },
        },
    }


def postgres_major(image: Optional[str]) -> Optional[int]:
    """Major version from a postgres image reference: `postgres:15-alpine` → 15.
    None when the tag carries no leading number (e.g. `latest`)."""
    if not image:
        return None
    ref = image.split("@", 1)[0]
    tag = ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""
    m = re.match(r"(\d+)", tag)
    return int(m.group(1)) if m else None


_QUANTITY_RE = re.compile(r"^([0-9.]+)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")
_QUANTITY_SCALE = {
    None: 1, "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}


def parse_quantity(q) -> Optional[float]:
    """Kubernetes resource quantity → number ('500m' → 0.5, '1Gi' → 1073741824).
    None for forms this does not understand, which callers treat as 'leave it'."""
    m = _QUANTITY_RE.match(str(q).strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * _QUANTITY_SCALE[m.group(2)]
    except ValueError:
        return None


def keep_larger_resources(docs: list[dict], live_deployments: dict) -> list[str]:
    """Never lower a container's CPU/memory request or limit below what is
    running. Operators raise these by hand (or with a right-sizing tool) when
    the defaults are too small for their fleet; an update silently halving a
    memory limit is how a working install starts OOM-killing. A release can
    still RAISE a value. Only keys both sides define are compared. Mutates
    `docs`; returns one note per value kept."""
    notes = []
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        name = d["metadata"]["name"]
        live = live_deployments.get(name)
        if not live:
            continue
        live_containers = {
            c["name"]: c for c in live["spec"]["template"]["spec"].get("containers", [])
        }
        for c in d["spec"]["template"]["spec"].get("containers", []):
            lc = live_containers.get(c.get("name"))
            if not lc:
                continue
            for section in ("requests", "limits"):
                new = (c.get("resources") or {}).get(section) or {}
                old = (lc.get("resources") or {}).get(section) or {}
                for key in ("cpu", "memory"):
                    if key not in new or key not in old:
                        continue
                    n, o = parse_quantity(new[key]), parse_quantity(old[key])
                    if n is not None and o is not None and o > n:
                        notes.append(
                            f"{name}/{c['name']} {section}.{key}: keeping running "
                            f"{old[key]} (release default is {new[key]})"
                        )
                        new[key] = old[key]
    return notes


def container_image(docs: list[dict], deployment: str, container: str) -> Optional[str]:
    for d in docs:
        if d.get("kind") == "Deployment" and d.get("metadata", {}).get("name") == deployment:
            for c in d["spec"]["template"]["spec"].get("containers", []):
                if c.get("name") == container:
                    return c.get("image")
    return None


def check_documents(docs: list[dict], namespace: str, live_postgres_image: Optional[str]) -> None:
    """Refuse anything outside what an in-app update may change."""
    for d in docs:
        kind = d.get("kind")
        name = d.get("metadata", {}).get("name")
        if kind not in ALLOWED_KINDS:
            raise Refused(f"{kind} '{name}' is not applied by in-app updates — re-run install-k3s.sh")
        ns = d.get("metadata", {}).get("namespace")
        if ns != namespace:
            raise Refused(f"{kind} '{name}' targets namespace '{ns}', expected '{namespace}'")

    new_pg = container_image(docs, "patchpilot-postgres", "postgres")
    new_major, live_major = postgres_major(new_pg), postgres_major(live_postgres_image)
    if new_major is None or live_major is None:
        raise Refused(
            f"cannot compare Postgres versions (running {live_postgres_image!r}, "
            f"release wants {new_pg!r}) — not changing the database"
        )
    if new_major != live_major:
        raise Refused(
            f"this release moves Postgres from {live_major} to {new_major}, which needs a "
            f"data migration (back up, switch, restore) — not done by in-app updates"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cluster I/O (Job side)
# ─────────────────────────────────────────────────────────────────────────────
def _kubectl() -> str:
    found = shutil.which("kubectl") or "/usr/local/bin/kubectl"
    if not Path(found).is_file():
        raise RuntimeError("kubectl not found in the updater image")
    return found


def _run(args: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([_kubectl(), *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def load_saved_values(namespace: str) -> dict:
    r = _run(["get", "configmap", VALUES_CONFIGMAP, "-n", namespace, "-o", "json"], timeout=30)
    data = json.loads(r.stdout).get("data") or {}
    if data.get("PP_INSTALL_VALUES_SCHEMA") != VALUES_SCHEMA:
        raise Refused(
            f"saved install values use schema {data.get('PP_INSTALL_VALUES_SCHEMA')!r}, "
            f"this release expects {VALUES_SCHEMA!r} — re-run install-k3s.sh"
        )
    return data


def _log(msg: str) -> None:
    print(f"[manifest-update] {msg}", flush=True)


def apply_release(namespace: str, version: str, backend_image: str,
                  frontend_image: str, restart: bool, templates: Path) -> None:
    saved = load_saved_values(namespace)
    values = {k: saved.get(k, "") for k in SAVED_KEYS}
    values.update(
        PP_NAMESPACE=namespace,
        PP_VERSION=version,
        PP_BACKEND_IMAGE=backend_image,
        PP_FRONTEND_IMAGE=frontend_image,
    )

    rendered: list[tuple[str, list[dict]]] = []
    docs: list[dict] = []
    for name in WORKLOAD_TEMPLATES:
        text = render((templates / name).read_text(), values)
        missing = unresolved(text)
        if missing:
            raise Refused(
                f"{name} needs setting(s) the installer has not saved: {', '.join(missing)} "
                f"— re-run install-k3s.sh"
            )
        file_docs = [d for d in yaml.safe_load_all(text) if d]
        rendered.append((name, file_docs))
        docs.extend(file_docs)

    live = json.loads(_run(["get", "deployments", "-n", namespace, "-o", "json"],
                           timeout=30).stdout)
    live_deployments = {d["metadata"]["name"]: d for d in live.get("items", [])}
    live_pg = container_image(list(live_deployments.values()), "patchpilot-postgres", "postgres")
    check_documents(docs, namespace, live_pg)
    _log(f"checks passed (Postgres stays on major {postgres_major(live_pg)})")
    for note in keep_larger_resources(docs, live_deployments):
        _log(note)

    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for name, file_docs in rendered:
            p = Path(tmp) / name
            p.write_text(yaml.safe_dump_all(file_docs, sort_keys=False))
            files.append(p)

        for p in files:
            diff = _run(["diff", "-f", str(p)], timeout=60, check=False)
            if diff.returncode == 1:
                _log(f"changes in {p.name}:\n{diff.stdout.rstrip()}")
            elif diff.returncode == 0:
                _log(f"no changes in {p.name}")
            else:
                raise RuntimeError(f"kubectl diff {p.name} failed: {diff.stderr.strip()}")

        for p in files:
            _run(["apply", "--dry-run=server", "-f", str(p)], timeout=60)
        _log("server-side dry run passed")

        for p in files:
            out = _run(["apply", "-f", str(p)], timeout=60).stdout.strip()
            _log(f"applied {p.name}: {out.replace(chr(10), '; ')}")

    if restart:
        for dep in ("patchpilot-frontend", "patchpilot-backend"):
            _run(["rollout", "restart", f"deployment/{dep}", "-n", namespace], timeout=30)
        _log("restarted frontend and backend to re-pull the latest tag")

    for dep in DEPLOYMENTS:
        r = _run(["rollout", "status", f"deployment/{dep}", "-n", namespace,
                  "--timeout=300s"], timeout=320, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"{dep} did not finish rolling out: {r.stderr.strip()}")
    _log("all deployments rolled out")

    patch = {"data": {
        "PP_VERSION": version,
        "PP_BACKEND_IMAGE": backend_image,
        "PP_FRONTEND_IMAGE": frontend_image,
    }}
    _run(["patch", "configmap", VALUES_CONFIGMAP, "-n", namespace,
          "--type", "merge", "-p", json.dumps(patch)], timeout=30)
    _log(f"saved install values updated to v{version}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Apply this release's workload manifests")
    ap.add_argument("--version", required=True)
    ap.add_argument("--backend-image", required=True)
    ap.add_argument("--frontend-image", required=True)
    ap.add_argument("--namespace", default=os.getenv("PATCHPILOT_NAMESPACE", "patchpilot"))
    ap.add_argument("--restart", action="store_true",
                    help="rollout restart frontend/backend after apply (latest-tag installs)")
    ap.add_argument("--templates", type=Path, default=TEMPLATES_DIR)
    a = ap.parse_args(argv)

    try:
        apply_release(a.namespace, a.version, a.backend_image, a.frontend_image,
                      a.restart, a.templates)
    except Refused as e:
        print(f"REFUSED: {e}", flush=True)
        return EXIT_REFUSED
    except Exception as e:
        print(f"FAILED: {e}", flush=True)
        return EXIT_FAILED
    print(f"DONE: v{a.version} applied", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
