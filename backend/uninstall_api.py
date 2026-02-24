"""
uninstall_api.py — PatchPilot v0.9.5-alpha
Admin-only endpoints for uninstalling PatchPilot.

Design constraints:
  - The backend runs INSIDE a container and has NO access to the Docker daemon
    (socket is not mounted by default) and NO knowledge of where the user
    installed the repo on the host filesystem.
  - Therefore Docker uninstall is ALWAYS manual — we generate the exact commands
    the operator needs to run on the host, clearly explained.
  - We NEVER generate `rm -rf` with auto-detected paths. The operator knows
    where they installed the software.
  - K3s uninstall CAN be partially automated because kubectl is available
    inside the k3s pod network.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from dependencies import get_db_pool

logger = logging.getLogger("patchpilot.uninstall")

router = APIRouter(prefix="/api/uninstall", tags=["uninstall"])


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
        automated = [
            "Revoke all active login sessions",
            "Note: container/volume teardown runs on the host (Docker CLI not available inside the container)",
        ]
        manual = [
            "# Run these commands on the HOST where PatchPilot is installed",
            "",
            "# 1. Go to your PatchPilot installation directory",
            "cd /path/to/patchpilot",
            "",
            "# 2. Stop all containers and remove named volumes",
            "docker compose down --volumes --remove-orphans",
            "",
            "# 3. Remove PatchPilot Docker images",
            "docker rmi $(docker images --filter 'reference=patchpilot*' -q) 2>/dev/null || true",
            "",
            "# 4. Remove the installation directory",
            "cd .. && rm -rf patchpilot",
            "",
            "# Optional: remove Docker itself",
            "# sudo apt-get remove --purge docker-ce docker-ce-cli containerd.io docker-compose-plugin",
            "# sudo rm -rf /var/lib/docker /etc/docker",
        ]
        desc = (
            "Docker Compose installation detected. "
            "Because the backend runs inside Docker, container teardown must be "
            "run directly on your host using the commands shown below."
        )
        can_auto = False

    elif install_type == "k3s":
        automated = [
            "Delete the PatchPilot Kubernetes namespace (removes all pods, services, PVCs)",
            "Delete the cert-manager ClusterIssuer resource",
            "Remove generated k8s manifests from k8s/.generated/",
        ]
        rc, node_ip, _ = _run([
            "kubectl", "get", "nodes",
            "-o", "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"
        ])
        ssh_host = node_ip if (rc == 0 and node_ip) else "<k3s-node-ip>"
        manual = [
            "# Remove hostPath data on the k3s node",
            f"ssh {ssh_host} 'sudo rm -rf /app-data/patchpilot-*'",
            "",
            "# Remove the installation directory (on your deploy machine)",
            "cd /path/to && rm -rf patchpilot",
            "",
            "# Optional: fully remove k3s from the node",
            "# ssh <node> '/usr/local/bin/k3s-uninstall.sh'",
        ]
        desc = (
            "Kubernetes / k3s installation detected. "
            "Namespace and cluster resources will be deleted automatically. "
            "hostPath data on the node requires manual SSH cleanup."
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
    user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    """
    Execute automated uninstall steps.
    Docker: revokes sessions, returns host commands for operator to run.
    K3s: deletes namespace and cluster resources via kubectl.
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
        # The backend has no Docker socket — cannot run docker commands from here.
        # What we CAN do: revoke all sessions so no stale logins remain.
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM sessions")
            completed.append("All active login sessions revoked")
        except Exception as e:
            # sessions table may not exist in all schema versions — non-fatal
            completed.append(f"Session revocation skipped: {e}")

        manual_cmds = [
            "# Run these commands on the HOST where PatchPilot is installed",
            "",
            "# 1. Go to your PatchPilot installation directory",
            "cd /path/to/patchpilot",
            "",
            "# 2. Stop all containers and remove named volumes",
            "docker compose down --volumes --remove-orphans",
            "",
            "# 3. Remove PatchPilot Docker images",
            "docker rmi $(docker images --filter 'reference=patchpilot*' -q) 2>/dev/null || true",
            "",
            "# 4. Remove the installation directory",
            "cd .. && rm -rf patchpilot",
            "",
            "# Optional: remove Docker itself",
            "# sudo apt-get remove --purge docker-ce docker-ce-cli containerd.io docker-compose-plugin",
            "# sudo rm -rf /var/lib/docker /etc/docker",
        ]

        return UninstallResult(
            success=True,
            install_type=install_type,
            steps_completed=completed,
            steps_failed=[],
            manual_commands=manual_cmds,
            message=(
                "Sessions cleared. Run the host commands below to remove "
                "all containers, volumes, images, and files."
            ),
        )

    # ── K3s ────────────────────────────────────────────────────────────────────
    elif install_type == "k3s":
        step = "Delete PatchPilot namespace (pods, services, PVCs, ConfigMaps)"
        rc, _, err = _run(
            ["kubectl", "delete", "namespace", "patchpilot", "--ignore-not-found=true"],
            timeout=120,
        )
        if rc == 0:
            completed.append(step)
        else:
            failed.append(f"{step}: {err}")

        step = "Delete ClusterIssuer (cert-manager)"
        rc, _, err = _run(
            ["kubectl", "delete", "clusterissuer", "letsencrypt-prod", "--ignore-not-found=true"]
        )
        if rc == 0:
            completed.append(step)
        else:
            completed.append(f"{step}: skipped (not present)")

        for gen_dir in [Path("/app/k8s/.generated"), Path("/k8s/.generated")]:
            if gen_dir.exists():
                shutil.rmtree(gen_dir, ignore_errors=True)
                completed.append(f"Removed generated manifests: {gen_dir}")
                break

        rc, node_ip, _ = _run([
            "kubectl", "get", "nodes",
            "-o", "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"
        ])
        ssh_host = node_ip if (rc == 0 and node_ip) else "<k3s-node-ip>"

        manual_cmds = [
            "# Remove hostPath data on the k3s node",
            f"ssh {ssh_host} 'sudo rm -rf /app-data/patchpilot-*'",
            "",
            "# Remove the installation directory (on your deploy machine)",
            "cd /path/to && rm -rf patchpilot",
            "",
            "# Optional: remove k3s entirely from the node",
            "# ssh <node> '/usr/local/bin/k3s-uninstall.sh'",
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
