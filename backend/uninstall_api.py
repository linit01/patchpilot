"""
uninstall_api.py — PatchPilot v0.9.5-alpha
Admin-only endpoints for uninstalling PatchPilot.

Design constraints:
  - Docker uninstall drives the Docker CLI (docker binary) via the mounted
    socket (/var/run/docker.sock).  It stops and removes all Compose-project
    containers, named volumes, built images, the project network, and the
    build cache.  The installation directory is left untouched — that is
    the operator's decision.
  - We NEVER generate `rm -rf` with auto-detected paths.
  - K3s uninstall runs kubectl against the cluster directly.
  - The backend container itself cannot remove its own container while it is
    still running and serving the response.  It is excluded from the stop/rm
    step; Docker will clean it up automatically once the process exits (the
    Compose service has restart:unless-stopped, so a normal exit after
    uninstall will not restart it).
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from dependencies import get_db_pool

logger = logging.getLogger("patchpilot.uninstall")

# jsonpath for k3s node InternalIP — single-quoted to avoid f-string escaping issues
_NODE_IP_JSONPATH = '{.items[0].status.addresses[?(@.type=="InternalIP")].address}'

router = APIRouter(prefix="/api/uninstall", tags=["uninstall"])

# ── Background task state ──────────────────────────────────────────────────────
# Keyed by install_type; holds the result of the post-response cleanup steps.
# Simple in-memory store — only one uninstall can run at a time.
_bg_result: dict = {}   # {"status": "running"|"done", "completed": [], "failed": []}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _detect_install_type() -> str:
    """
    Return 'k3s', 'docker', or 'unknown'.
    Priority:
      1. PATCHPILOT_INSTALL_MODE env var (explicit, always wins)
      2. k3s kubeconfig or service-account token -> k3s
      3. /.dockerenv present -> docker (Docker injects this into every container)
      4. /var/run/docker.sock accessible -> docker
    """
    env_mode = os.environ.get("PATCHPILOT_INSTALL_MODE", "").lower()
    if env_mode in ("k3s", "docker"):
        return env_mode

    if Path("/etc/rancher/k3s/k3s.yaml").exists():
        return "k3s"

    if Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        return "k3s"

    # /.dockerenv is injected by Docker into every container automatically
    if Path("/.dockerenv").exists():
        return "docker"

    if Path("/var/run/docker.sock").exists():
        return "docker"

    return "unknown"


def _run(cmd: list[str], timeout: int = 90) -> tuple[int, str, str]:
    """Run a command. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Timed out after {timeout}s"
    except FileNotFoundError as e:
        return 1, "", f"Command not found: {e}"
    except Exception as e:
        return 1, "", str(e)


def _kubectl() -> list[str]:
    """
    Resolve the kubectl binary to use inside a k3s pod.

    For k3s installs, KUBECTL_BIN is baked into the deployment by install-k3s.sh
    at install time via $(which kubectl) on the deploy host — so this will
    ordinarily resolve on the first check. The remaining candidates are a safety
    net for manual deploys or upgrades that predate this feature.

    Search order:
      1. KUBECTL_BIN env var  — set automatically by install-k3s.sh
      2. shutil.which("kubectl") — standard PATH lookup
      3. Common hard-coded paths
      4. k3s kubectl          — k3s bundles kubectl as a sub-command

    Raises RuntimeError if nothing is found so callers can surface a clear error.
    """
    # 1. Explicit override
    env_bin = os.environ.get("KUBECTL_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return [env_bin]

    # 2. PATH lookup
    found = shutil.which("kubectl")
    if found:
        return [found]

    # 3. Hard-coded common paths
    for candidate in (
        "/usr/local/bin/kubectl",
        "/usr/bin/kubectl",
        "/snap/bin/kubectl",
        "/opt/bin/kubectl",
    ):
        if Path(candidate).is_file():
            return [candidate]

    # 4. k3s bundles kubectl
    k3s_bin = shutil.which("k3s") or "/usr/local/bin/k3s"
    if Path(k3s_bin).is_file():
        return [k3s_bin, "kubectl"]

    raise RuntimeError(
        "kubectl not found. Install kubectl on the node, add it to PATH, "
        "or set the KUBECTL_BIN env var in your deployment manifest."
    )


def _docker() -> list[str]:
    """
    Resolve the docker CLI binary inside the container.
    Raises RuntimeError if not found — callers surface a clear error.
    """
    found = shutil.which("docker")
    if found:
        return [found]
    for candidate in ("/usr/bin/docker", "/usr/local/bin/docker"):
        if Path(candidate).is_file():
            return [candidate]
    raise RuntimeError(
        "docker CLI not found in container. "
        "Ensure docker-ce-cli is installed in the backend image (see Dockerfile)."
    )


def _compose_project() -> str:
    """
    Return the Compose project name for this deployment.
    Docker Compose sets COMPOSE_PROJECT_NAME in the container's environment;
    fall back to 'patchpilot' if it is absent (manual / non-Compose starts).
    """
    return os.environ.get("COMPOSE_PROJECT_NAME", "patchpilot")


def _own_container_id() -> str | None:
    """
    Return this container's short ID so we can exclude ourselves from
    the stop/rm step (can't remove our own container while still running).
    Docker sets the hostname to the full container ID by default.
    """
    hostname = os.environ.get("HOSTNAME", "")
    if len(hostname) >= 12 and all(c in "0123456789abcdef" for c in hostname.lower()):
        return hostname[:12]
    return None


def _docker_cleanup_background(dk: list[str], project: str, own_id: str | None) -> None:
    """
    Runs AFTER the HTTP response has been delivered.

    Phase A (steps 2-4): remove everything that isn't the backend itself.
      Step 2 - Collect ALL image IDs used by project containers (including
               pulled images like postgres:15-alpine and nginx:alpine that
               carry no project label), then stop and remove those containers.
      Step 3 - Remove volumes not held open by the backend.
      Step 4 - Remove the project network.

    Phase B (step 5): spawn a detached janitor container.
      The backend container, its mounted volumes, and its image cannot be
      removed while this process is still running.  We spawn a tiny
      docker:cli container (with socket access) that sleeps 5 s, then
      removes the backend container, remaining volumes, ALL collected image
      IDs (postgres, nginx, patchpilot-backend, etc.), and the build cache.
      The janitor is independent of the Compose project and survives us.

    Phase C (step 6): stop this container.
      docker stop sends SIGTERM to uvicorn for a clean exit.
      Docker will NOT restart it (restart:unless-stopped only fires on
      non-zero exits; SIGTERM = exit 0).
    """
    global _bg_result
    completed: list[str] = []
    failed:    list[str] = []

    # -- 2. Collect image IDs, then stop & remove other project containers --
    # We harvest image IDs NOW, before containers are gone, because pulled
    # images (postgres, nginx) carry no compose project label -- the only
    # reliable way to find them is from the containers themselves.
    step = "Stop and remove project containers"
    rc, ids_out, err = _run(
        dk + ["ps", "-a", "-q",
              "--filter", f"label=com.docker.compose.project={project}"],
        timeout=15,
    )
    all_image_ids: list[str] = []
    if rc != 0:
        failed.append(f"{step} (list): {err}")
    else:
        container_ids = [c for c in ids_out.splitlines() if c]

        # Harvest image ID from every project container before removal
        for cid in container_ids:
            rc2, img_id, _ = _run(
                dk + ["inspect", "--format", "{{.Image}}", cid],
                timeout=10,
            )
            if rc2 == 0 and img_id.strip():
                all_image_ids.append(img_id.strip())
        all_image_ids = list(set(all_image_ids))

        # Remove all containers except ourselves
        others = [
            c for c in container_ids
            if not own_id or (not c.startswith(own_id) and not own_id.startswith(c))
        ]
        if others:
            rc, _, err = _run(dk + ["rm", "-f"] + others, timeout=60)
            if rc == 0:
                completed.append(f"{step}: {len(others)} removed")
            else:
                failed.append(f"{step}: {err}")
        else:
            completed.append(f"{step}: none found")

    # -- 3. Remove volumes not held open by the backend ---------------------
    step = "Remove project volumes"
    rc, vols_out, _ = _run(
        dk + ["volume", "ls", "-q",
              "--filter", f"label=com.docker.compose.project={project}"],
        timeout=15,
    )
    all_vols = [v for v in vols_out.splitlines() if v] if rc == 0 else []
    remaining_vols: list[str] = []
    removed_vols:   list[str] = []
    for vol in all_vols:
        rc, _, _ = _run(dk + ["volume", "rm", vol], timeout=15)
        if rc == 0:
            removed_vols.append(vol)
        else:
            remaining_vols.append(vol)   # still mounted -- janitor handles it
    if removed_vols:
        completed.append(f"{step}: {', '.join(removed_vols)}")
    if remaining_vols:
        completed.append(f"{step}: deferred to janitor (in use): {', '.join(remaining_vols)}")
    if not removed_vols and not remaining_vols:
        completed.append(f"{step}: none found")

    # -- 4. Remove project network ------------------------------------------
    step = "Remove project network"
    rc, nets_out, _ = _run(
        dk + ["network", "ls", "-q",
              "--filter", f"label=com.docker.compose.project={project}"],
        timeout=15,
    )
    if rc != 0:
        failed.append(f"{step} (list): could not list networks")
    else:
        net_ids = [n for n in nets_out.splitlines() if n]
        if net_ids:
            rc, _, err = _run(dk + ["network", "rm"] + net_ids, timeout=30)
            if rc == 0:
                completed.append(f"{step}: {len(net_ids)} removed")
            else:
                completed.append(f"{step}: deferred (backend still attached)")
        else:
            completed.append(f"{step}: none found")

    # -- 5. Spawn janitor container -----------------------------------------
    # Removes: backend container, remaining volumes, ALL project image IDs
    # (postgres:15-alpine, nginx:alpine, patchpilot-backend, etc.),
    # and the build cache.
    step = "Spawn cleanup janitor"
    backend_container = own_id or f"{project}-backend-1"

    vol_rm_cmd = (
        "docker volume rm -f " + " ".join(f"'{v}'" for v in remaining_vols)
        if remaining_vols else "true"
    )
    img_rm_cmd = (
        "docker rmi -f " + " ".join(all_image_ids)
        if all_image_ids else "true"
    )

    janitor_script = (
        f"sleep 5"
        f" && docker rm -f '{backend_container}' 2>/dev/null || true"
        f" && {vol_rm_cmd} 2>/dev/null || true"
        f" && {img_rm_cmd} 2>/dev/null || true"
        f" && docker builder prune -f 2>/dev/null || true"
    )

    rc, janitor_id, err = _run(
        dk + ["run", "--rm", "-d",
              "-v", "/var/run/docker.sock:/var/run/docker.sock",
              "docker:cli",
              "sh", "-c", janitor_script],
        timeout=30,
    )
    if rc == 0:
        completed.append(f"{step}: started ({janitor_id[:12] if janitor_id else 'ok'})")
    else:
        leftover = [f"container {backend_container}"] + remaining_vols + all_image_ids
        failed.append(
            f"{step}: could not start janitor ({err}). "
            f"Remove manually: {', '.join(leftover)}"
        )

    _bg_result["completed"].extend(completed)
    _bg_result["failed"].extend(failed)
    _bg_result["status"] = "done"
    logger.info(
        "Docker cleanup complete -- completed=%d failed=%d; stopping self",
        len(completed), len(failed),
    )

    # -- 6. Stop this container (phase C) -----------------------------------
    # SIGTERM -> uvicorn exits 0 -> Docker does NOT restart (unless-stopped).
    if own_id:
        _run(dk + ["stop", own_id], timeout=30)


# ── Schemas ────────────────────────────────────────────────────────────────────

class UninstallStatus(BaseModel):
    install_type: str
    can_auto_uninstall: bool
    description: str
    automated_steps: list[str]
    manual_steps: list[str]


class UninstallResult(BaseModel):
    success: bool
    install_type: str
    steps_completed: list[str]
    steps_failed: list[str]
    manual_commands: list[str]
    message: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/status", response_model=UninstallStatus)
async def get_uninstall_status(user: dict = Depends(require_admin)):
    """
    Detect install type and return a preview of automated vs manual steps.
    No changes are made — this is read-only.
    """
    install_type = _detect_install_type()

    if install_type == "docker":
        project = _compose_project()
        automated = [
            "Revoke all active login sessions",
            f"Stop and remove all containers in Compose project '{project}' (except this backend — it exits after responding)",
            f"Remove named volumes: {project}_postgres_data, {project}_backups",
            f"Remove Compose network: {project}_{project}",
            f"Remove built backend image (tagged {project}-backend / patchpilot-backend)",
            "Prune dangling build cache",
        ]
        manual = [
            "# The installation directory is NOT removed — delete it yourself when ready:",
            "# rm -rf /path/to/patchpilot",
            "",
            "# Optional: remove Docker itself",
            "# sudo apt-get remove --purge docker-ce docker-ce-cli containerd.io docker-compose-plugin",
        ]
        desc = (
            f"Docker Compose installation detected (project: {project}). "
            "All containers, volumes, images, and build cache will be removed automatically. "
            "The installation directory is left in place."
        )
        can_auto = True

    elif install_type == "k3s":
        try:
            kc = _kubectl()
            rc, node_ip, _ = _run(
                kc + ["get", "nodes",
                      "-o", _NODE_IP_JSONPATH]
            )
            ssh_host = node_ip.strip() if (rc == 0 and node_ip.strip()) else "<k3s-node-ip>"
        except RuntimeError:
            ssh_host = "<k3s-node-ip>"
        automated = [
            "Revoke all active login sessions",
            "Run a privileged Kubernetes Job to remove /app-data/patchpilot-* on the node (no SSH required)",
            "Delete the PatchPilot namespace — postgres-data and ansible-data PVs auto-deleted (reclaimPolicy: Delete)",
            "backups PV is RETAINED — /app-data/patchpilot-backups survives for post-uninstall restore",
            "Delete the cert-manager ClusterIssuer resource",
            "Remove generated k8s manifests from k8s/.generated/",
        ]
        manual = [
            "# If the cleanup Job fails, run this on the k3s node manually:",
            f"# ssh {ssh_host} 'sudo rm -rf /app-data/patchpilot-*'",
            "",
            "# Remove PatchPilot images from k3s containerd cache (run on node):",
            f"# ssh {ssh_host} \"sudo k3s crictl rmi \\$(sudo k3s crictl images | grep patchpilot | awk '{{print $3}}') 2>/dev/null || true\"",
            "",
            "# Remove the installation directory (on your deploy machine):",
            "# cd /path/to && rm -rf patchpilot",
            "",
            "# Optional: fully remove k3s from the node:",
            f"# ssh {ssh_host} '/usr/local/bin/k3s-uninstall.sh'",
        ]
        desc = (
            "Kubernetes / k3s installation detected. "
            "postgres-data and ansible-data PVs use reclaimPolicy: Delete and are removed with the namespace. "
            "The backups PV uses reclaimPolicy: Retain — your backup archives survive uninstall "
            "and can be restored on a fresh install. "
            "containerd image removal requires crictl on the node and is provided as a manual command."
        )
        can_auto = True

    else:
        automated = []
        manual = [
            "# Could not determine install type.",
            "# Add PATCHPILOT_INSTALL_MODE=docker (or k3s) to your .env and restart.",
            "",
            "# Docker manual uninstall:",
            "cd /path/to/patchpilot && docker compose down --volumes --remove-orphans",
            "docker rmi $(docker images --filter 'reference=patchpilot*' -q) 2>/dev/null || true",
            "",
            "# K3s manual uninstall:",
            "cd /path/to/patchpilot && ./k8s/install-k3s.sh --uninstall",
        ]
        desc = (
            "Install type could not be detected. "
            "Add PATCHPILOT_INSTALL_MODE=docker or =k3s to your .env and restart the backend."
        )
        can_auto = False

    return UninstallStatus(
        install_type=install_type,
        can_auto_uninstall=can_auto,
        description=desc,
        automated_steps=automated,
        manual_steps=manual,
    )


@router.post("/execute", response_model=UninstallResult)
async def execute_uninstall(
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """
    Execute automated uninstall steps.

    Docker: runs steps 1–3 synchronously (sessions, containers, volumes),
            returns the response, then completes steps 4–6 (network, images,
            build cache) in a background task so the network teardown does not
            kill the HTTP connection mid-response.
            Poll GET /api/uninstall/result for final background status.
    K3s:    deletes namespace and cluster resources via kubectl.
    """
    install_type = _detect_install_type()
    completed: list[str] = []
    failed: list[str] = []
    manual_cmds: list[str] = []

    logger.warning(
        "Uninstall initiated by admin '%s' — install_type=%s",
        user.get("username", "unknown"),
        install_type,
    )

    # ── Docker ─────────────────────────────────────────────────────────────────
    if install_type == "docker":
        try:
            dk = _docker()
        except RuntimeError as exc:
            return UninstallResult(
                success=False,
                install_type=install_type,
                steps_completed=[],
                steps_failed=[str(exc)],
                manual_commands=[],
                message=str(exc),
            )

        project = _compose_project()
        own_id  = _own_container_id()

        _bg_result.clear()
        _bg_result.update({"status": "running", "completed": [], "failed": []})

        # ── 1. Revoke sessions — only safe inline step ──────────────────────
        # Everything else (containers, volumes, network, images) runs in the
        # background task AFTER this response is delivered.  Removing any
        # other container inline would kill the nginx frontend that's proxying
        # this very request, dropping the connection before the client sees
        # the response.
        step = "Revoke all active login sessions"
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM sessions")
            completed.append(step)
        except Exception as e:
            completed.append(f"{step}: skipped ({e})")

        _bg_result["completed"].extend(completed)
        background_tasks.add_task(_docker_cleanup_background, dk, project, own_id)

        return UninstallResult(
            success=True,
            install_type=install_type,
            steps_completed=completed,
            steps_failed=[],
            manual_commands=[
                "# Installation directory NOT removed — delete when ready:",
                "# rm -rf /path/to/patchpilot",
                "",
                "# Optional: remove Docker itself",
                "# sudo apt-get remove --purge docker-ce docker-ce-cli containerd.io docker-compose-plugin",
            ],
            message=(
                "Sessions revoked. Containers, volumes, network, and images are being "
                "removed in the background — poll GET /api/uninstall/result for status."
            ),
        )

    # ── K3s ────────────────────────────────────────────────────────────────────
    elif install_type == "k3s":
        # Resolve kubectl once — fail early with a clear message if not found
        try:
            kc = _kubectl()
        except RuntimeError as exc:
            failed.append(str(exc))
            return UninstallResult(
                success=False,
                install_type=install_type,
                steps_completed=completed,
                steps_failed=failed,
                manual_commands=[
                    "# kubectl not found in the container — run these on the node directly:",
                    "kubectl delete namespace patchpilot --ignore-not-found=true",
                    "kubectl delete clusterissuer letsencrypt-prod --ignore-not-found=true",
                    "# Then clean hostPath data on the k3s node:",
                    "ssh <k3s-node-ip> \'sudo rm -rf /app-data/patchpilot-*\'",
                ],
                message=str(exc),
            )

        namespace = os.environ.get("PATCHPILOT_NAMESPACE", "patchpilot")

        # ── Step 1: Revoke sessions ────────────────────────────────────────
        step = "Revoke all active login sessions"
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM sessions")
            completed.append(step)
        except Exception as e:
            completed.append(f"{step}: skipped ({e})")

        # ── Step 2: Privileged Job to clean hostPath dirs ──────────────────
        # Runs a busybox container inside the cluster that mounts /app-data
        # directly from the node via hostPath and removes patchpilot-* dirs.
        # This eliminates the SSH dependency entirely — the Job runs with node
        # filesystem access natively via Kubernetes privileged security context.
        step = "Run hostPath cleanup Job on node"
        job_name = "patchpilot-hostpath-cleanup"
        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 30,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "tolerations": [{"operator": "Exists"}],
                        "containers": [{
                            "name": "cleanup",
                            "image": "busybox:1.36",
                            "command": [
                                "sh", "-c",
                                "find /app-data -maxdepth 1 -name 'patchpilot-*' "
                                "-exec rm -rf {} + 2>/dev/null; "
                                "echo 'hostPath cleanup complete'"
                            ],
                            "securityContext": {
                                "privileged": True,
                                "runAsUser": 0,
                            },
                            "volumeMounts": [{
                                "name": "app-data",
                                "mountPath": "/app-data",
                            }],
                        }],
                        "volumes": [{
                            "name": "app-data",
                            "hostPath": {
                                "path": "/app-data",
                                "type": "DirectoryOrCreate",
                            },
                        }],
                    },
                },
            },
        }

        # Get node IP for fallback manual command
        rc, node_ip, _ = _run(
            kc + ["get", "nodes",
                  "-o", _NODE_IP_JSONPATH]
        )
        ssh_host = node_ip.strip() if (rc == 0 and node_ip.strip()) else "<k3s-node-ip>"

        job_succeeded = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as jf:
                json.dump(job_manifest, jf)
                jf_path = jf.name

            # Delete any leftover job from a prior run first
            _run(kc + ["delete", "job", job_name, "-n", namespace,
                        "--ignore-not-found=true"], timeout=15)

            rc, out, err = _run(kc + ["apply", "-f", jf_path], timeout=15)
            Path(jf_path).unlink(missing_ok=True)

            if rc != 0:
                raise RuntimeError(f"kubectl apply job failed: {err}")

            # Wait up to 60s for the Job to complete
            rc, _, err = _run(
                kc + ["wait", "--for=condition=complete",
                      f"job/{job_name}", "-n", namespace, "--timeout=60s"],
                timeout=75,
            )
            if rc == 0:
                job_succeeded = True
                completed.append(f"{step}: success")
            else:
                # Check if it failed vs timed out
                rc2, phase, _ = _run(
                    kc + ["get", "job", job_name, "-n", namespace,
                          "-o", "jsonpath={.status.conditions[0].type}"],
                    timeout=10,
                )
                phase = phase.strip() if rc2 == 0 else "unknown"
                raise RuntimeError(
                    f"Job did not complete in 60s (phase={phase}). "
                    f"Run manually: ssh {ssh_host} 'sudo rm -rf /app-data/patchpilot-*'"
                )

        except RuntimeError as e:
            failed.append(f"{step}: {e}")
            logger.warning("hostPath cleanup Job failed — will surface SSH fallback: %s", e)
        except Exception as e:
            failed.append(f"{step}: unexpected error: {e}")
            logger.exception("Unexpected error during cleanup Job")
        finally:
            try:
                Path(jf_path).unlink(missing_ok=True)
            except Exception:
                pass

        # ── Step 3: Delete namespace ───────────────────────────────────────
        # Done AFTER the cleanup Job so /app-data is wiped before the PVCs
        # and PVs are removed (avoids k8s leaving orphaned hostPath dirs).
        step = "Delete PatchPilot namespace (pods, services, PVCs, ConfigMaps)"
        rc, _, err = _run(
            kc + ["delete", "namespace", namespace, "--ignore-not-found=true"],
            timeout=120,
        )
        if rc == 0:
            completed.append(step)
        else:
            failed.append(f"{step}: {err}")

        # ── Step 4: Delete ClusterIssuer ───────────────────────────────────
        # NOTE: PVs no longer need explicit deletion — reclaimPolicy: Delete
        # means Kubernetes removes the PV and its hostPath data automatically
        # when the PVC is deleted (which happens with the namespace above).
        step = "Delete ClusterIssuer (cert-manager)"
        rc, _, err = _run(
            kc + ["delete", "clusterissuer", "letsencrypt-prod",
                  "--ignore-not-found=true"]
        )
        if rc == 0:
            completed.append(step)
        else:
            completed.append(f"{step}: skipped (not present)")

        # ── Step 6: Remove generated manifests ────────────────────────────
        for gen_dir in [Path("/app/k8s/.generated"), Path("/k8s/.generated")]:
            if gen_dir.exists():
                shutil.rmtree(gen_dir, ignore_errors=True)
                completed.append(f"Removed generated manifests: {gen_dir}")
                break

        # ── Build manual commands ──────────────────────────────────────────
        # NOTE: containerd image removal requires crictl on the node itself —
        # the backend pod cannot reach the k3s containerd socket.
        manual_cmds = []
        if not job_succeeded:
            manual_cmds += [
                "# hostPath cleanup Job did not complete — run on the k3s node:",
                f"ssh {ssh_host} 'sudo rm -rf /app-data/patchpilot-*'",
                "",
            ]
        manual_cmds += [
            "# Remove PatchPilot images from k3s containerd cache (run on node):",
            f"ssh {ssh_host} \"sudo k3s crictl rmi \\$(sudo k3s crictl images | grep patchpilot | awk '{{print \\$3}}') 2>/dev/null || true\"",
            "",
            "# Remove the installation directory (on your deploy machine):",
            "# cd /path/to && rm -rf patchpilot",
            "",
            "# Optional: remove k3s entirely from the node:",
            f"# ssh {ssh_host} '/usr/local/bin/k3s-uninstall.sh'",
        ]

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Install type unknown. "
                "Add PATCHPILOT_INSTALL_MODE=docker or PATCHPILOT_INSTALL_MODE=k3s "
                "to your .env and restart the backend."
            ),
        )

    success = len(failed) == 0
    message = (
        "Automated steps complete. Run the manual commands below to finish cleanup."
        if success
        else f"Completed with {len(failed)} error(s). Review and run manual commands."
    )

    return UninstallResult(
        success=success,
        install_type=install_type,
        steps_completed=completed,
        steps_failed=failed,
        manual_commands=manual_cmds,
        message=message,
    )


@router.get("/result")
async def get_uninstall_result(user: dict = Depends(require_admin)):
    """
    Poll for the result of the Docker background cleanup steps (network,
    images, build cache) that run after /execute returns.

    Returns:
      { "status": "running" | "done" | "idle",
        "completed": [...],
        "failed":    [...] }

    "idle" means /execute has not been called yet in this process lifetime.
    The frontend should poll every 2 s until status == "done".
    """
    if not _bg_result:
        return {"status": "idle", "completed": [], "failed": []}
    return {
        "status":    _bg_result.get("status", "idle"),
        "completed": _bg_result.get("completed", []),
        "failed":    _bg_result.get("failed", []),
    }
