"""
PatchPilot — Update Checker
============================
Periodic check for new releases via the GitHub Releases API.
Exposes FastAPI endpoints for the frontend to query update status,
trigger manual checks, and apply updates.

Update channels:
  - "latest"  — user runs :latest image tags; restart pulls newest image
  - "pinned"  — user runs explicit version tags (e.g. :backend-0.9.8-alpha);
                 update patches the image tag in the deployment/compose file

Deployment-aware update execution:
  - Kubernetes: spawns a Job that runs kubectl set image + rollout restart
  - Docker:     runs docker compose pull + up -d via mounted socket

Authentication:
  - Public repos:  unauthenticated GitHub API (60 req/hour — plenty for 24h intervals)
  - Private repos:  set GITHUB_TOKEN env var (used in --developer workflows)
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from packaging.version import Version, InvalidVersion
from pydantic import BaseModel

from auth import require_full_admin, require_auth

logger = logging.getLogger("patchpilot.updates")

router = APIRouter(prefix="/api/updates", tags=["updates"])

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
GITHUB_OWNER = os.getenv("GITHUB_REPO_OWNER", "linit01")
GITHUB_REPO = os.getenv("GITHUB_REPO_NAME", "patchpilot")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"

# Read current app version (same logic as app.py)
def _read_version() -> str:
    env_ver = os.getenv("APP_VERSION")
    if env_ver:
        return env_ver
    for path in ("VERSION", "/app/VERSION", "../VERSION"):
        try:
            return open(path).read().strip()
        except FileNotFoundError:
            continue
    return "0.0.0-dev"

CURRENT_VERSION = _read_version()

# ─────────────────────────────────────────────────────────────────────────────
# Cached state
# ─────────────────────────────────────────────────────────────────────────────
_update_cache: dict = {
    "latest_version": None,
    "release_url": None,
    "release_notes": None,
    "published_at": None,
    "update_available": False,
    "last_checked": None,
    "check_error": None,
}
_update_lock = asyncio.Lock()

# In-progress update tracking
_update_status: dict = {
    "active": False,
    "step": None,
    "message": None,
    "error": None,
    "started_at": None,
    "completed_at": None,
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _github_headers() -> dict:
    """Build GitHub API request headers, with optional auth for private repos."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_version(tag: str) -> Optional[Version]:
    """Parse a version string, stripping leading 'v' if present."""
    tag = tag.strip().lstrip("v")
    try:
        return Version(tag)
    except InvalidVersion:
        return None


def _is_newer(latest_tag: str, current: str) -> bool:
    """Return True if latest_tag represents a newer version than current."""
    latest_v = _parse_version(latest_tag)
    current_v = _parse_version(current)
    if latest_v is None or current_v is None:
        # Fall back to string compare if parsing fails
        return latest_tag.lstrip("v") != current.lstrip("v")
    return latest_v > current_v


def _detect_install_mode() -> str:
    """Detect whether we're running in k3s or Docker.
    Mirrors the detection logic in uninstall_api.py."""
    env_mode = os.getenv("PATCHPILOT_INSTALL_MODE", "").lower()
    if env_mode in ("k3s", "k8s"):
        return "k3s"
    if env_mode == "docker":
        return "docker"
    # k3s kubeconfig or in-cluster service account
    if Path("/etc/rancher/k3s/k3s.yaml").exists():
        return "k3s"
    if Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        return "k3s"
    # Docker markers
    if Path("/.dockerenv").exists():
        return "docker"
    if Path("/var/run/docker.sock").exists():
        return "docker"
    return "docker"


def _detect_update_channel() -> str:
    """
    Detect whether the user is running :latest tags or pinned version tags.
    For k8s, inspect the deployment image. For Docker, check compose file or
    the current APP_VERSION.
    """
    mode = _detect_install_mode()

    if mode == "k3s":
        try:
            kc = _kubectl()
            namespace = os.getenv("PP_NAMESPACE", "patchpilot")
            rc, out, _ = _run(kc + [
                "get", "deployment", "patchpilot-backend",
                "-n", namespace,
                "-o", "jsonpath={.spec.template.spec.containers[0].image}"
            ], timeout=10)
            if rc == 0 and out:
                tag = out.split(":")[-1] if ":" in out else ""
                if tag == "latest" or tag.endswith("-latest"):
                    return "latest"
                return "pinned"
        except Exception:
            pass

    # Docker path: check if the compose file uses :latest tags
    compose_paths = [
        "/install/docker-compose.yml",
        "/app/docker-compose.yml",
        "../docker-compose.yml",
        "docker-compose.yml",
    ]
    for cp in compose_paths:
        try:
            content = Path(cp).read_text()
            # Look for image lines with :latest or :<component>-latest
            if re.search(r"image:.*:.*latest", content):
                return "latest"
            return "pinned"
        except FileNotFoundError:
            continue

    return "pinned"


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
    """Resolve kubectl binary (mirrors uninstall_api._kubectl)."""
    env_bin = os.environ.get("KUBECTL_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return [env_bin]
    found = shutil.which("kubectl")
    if found:
        return [found]
    for candidate in (
        "/usr/local/bin/kubectl",
        "/usr/bin/kubectl",
        "/snap/bin/kubectl",
        "/opt/bin/kubectl",
    ):
        if Path(candidate).is_file():
            return [candidate]
    k3s_bin = shutil.which("k3s") or "/usr/local/bin/k3s"
    if Path(k3s_bin).is_file():
        return [k3s_bin, "kubectl"]
    raise RuntimeError("kubectl not found")


# ─────────────────────────────────────────────────────────────────────────────
# Core: GitHub Release Check
# ─────────────────────────────────────────────────────────────────────────────
async def check_for_updates() -> dict:
    """
    Query GitHub Releases API for the latest release.
    Updates the cached state and returns it.
    """
    async with _update_lock:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{GITHUB_API}/releases/latest",
                    headers=_github_headers(),
                )

            if resp.status_code == 404:
                # No releases yet
                _update_cache.update({
                    "latest_version": None,
                    "update_available": False,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "check_error": None,
                })
                return dict(_update_cache)

            if resp.status_code == 403:
                # Rate limited or forbidden
                error = "GitHub API rate limit exceeded or access denied"
                if not os.getenv("GITHUB_TOKEN"):
                    error += " (no GITHUB_TOKEN set — required for private repos)"
                _update_cache["check_error"] = error
                _update_cache["last_checked"] = datetime.now(timezone.utc).isoformat()
                logger.warning("GitHub API error: %s", error)
                return dict(_update_cache)

            resp.raise_for_status()
            data = resp.json()

            latest_tag = data.get("tag_name", "")
            update_available = _is_newer(latest_tag, CURRENT_VERSION)

            _update_cache.update({
                "latest_version": latest_tag.lstrip("v"),
                "release_url": data.get("html_url", ""),
                "release_notes": data.get("body", ""),
                "published_at": data.get("published_at", ""),
                "update_available": update_available,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "check_error": None,
            })

            if update_available:
                logger.info(
                    "Update available: %s → %s",
                    CURRENT_VERSION, latest_tag.lstrip("v"),
                )

        except httpx.HTTPStatusError as e:
            _update_cache["check_error"] = f"GitHub API HTTP {e.response.status_code}"
            _update_cache["last_checked"] = datetime.now(timezone.utc).isoformat()
            logger.warning("GitHub release check failed: %s", e)
        except Exception as e:
            _update_cache["check_error"] = str(e)
            _update_cache["last_checked"] = datetime.now(timezone.utc).isoformat()
            logger.warning("GitHub release check failed: %s", e)

    return dict(_update_cache)


# ─────────────────────────────────────────────────────────────────────────────
# Periodic Check Loop (called from app.py startup)
# ─────────────────────────────────────────────────────────────────────────────
async def periodic_update_check(get_setting_fn):
    """
    Background loop that checks for updates at the configured interval.
    get_setting_fn(key) should return the setting value from the DB.
    """
    # Wait a bit on startup before first check
    await asyncio.sleep(30)

    while True:
        try:
            enabled = await get_setting_fn("update_check_enabled")
            if enabled and enabled.lower() == "true":
                await check_for_updates()

            interval_str = await get_setting_fn("update_check_interval")
            interval = int(interval_str) if interval_str else 86400
            interval = max(interval, 3600)  # floor at 1 hour
        except Exception as e:
            logger.warning("Update check loop error: %s", e)
            interval = 86400

        await asyncio.sleep(interval)


# ─────────────────────────────────────────────────────────────────────────────
# Update Execution — Kubernetes
# ─────────────────────────────────────────────────────────────────────────────
async def _apply_update_k8s(target_version: str):
    """
    Apply an update in a Kubernetes environment.

    For 'latest' channel:  rollout restart (forces image re-pull)
    For 'pinned' channel:  kubectl set image to new version tag, then rollout
    """
    global _update_status

    namespace = os.getenv("PP_NAMESPACE", "patchpilot")
    image_repo = os.getenv("PATCHPILOT_IMAGE_REPO", "linit01/patchpilot")
    channel = _detect_update_channel()
    kc = _kubectl()

    try:
        _update_status.update({
            "active": True,
            "step": "preparing",
            "message": f"Preparing {channel} update to v{target_version}...",
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        })

        if channel == "pinned":
            # Update image tags on both deployments
            _update_status["step"] = "updating_images"
            _update_status["message"] = "Updating deployment image tags..."

            for deploy, component in [
                ("patchpilot-backend", "backend"),
                ("patchpilot-frontend", "frontend"),
            ]:
                new_image = f"{image_repo}:{component}-{target_version}"
                # Find the container name in the deployment
                rc, out, err = _run(kc + [
                    "set", "image",
                    f"deployment/{deploy}",
                    f"{component}={new_image}",
                    "-n", namespace,
                ], timeout=30)
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to set image on {deploy}: {err}"
                    )
                logger.info("Updated %s → %s", deploy, new_image)

        # Rollout restart to pick up new images
        _update_status["step"] = "restarting"
        _update_status["message"] = "Restarting deployments..."

        for deploy in ["patchpilot-backend", "patchpilot-frontend"]:
            rc, _, err = _run(kc + [
                "rollout", "restart",
                f"deployment/{deploy}",
                "-n", namespace,
            ], timeout=30)
            if rc != 0:
                raise RuntimeError(f"Rollout restart failed for {deploy}: {err}")

        # Wait for rollout to complete
        _update_status["step"] = "waiting"
        _update_status["message"] = "Waiting for rollout to complete..."

        for deploy in ["patchpilot-backend", "patchpilot-frontend"]:
            rc, _, err = _run(kc + [
                "rollout", "status",
                f"deployment/{deploy}",
                "-n", namespace,
                "--timeout=120s",
            ], timeout=135)
            if rc != 0:
                logger.warning("Rollout status wait failed for %s: %s", deploy, err)
                # Don't fail hard — the restart was issued, pod may just be slow

        _update_status.update({
            "active": False,
            "step": "complete",
            "message": f"Update to v{target_version} complete. Page will reload.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        logger.error("K8s update failed: %s", e)
        _update_status.update({
            "active": False,
            "step": "failed",
            "message": str(e),
            "error": str(e),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Update Execution — Docker Compose
# ─────────────────────────────────────────────────────────────────────────────
async def _apply_update_docker(target_version: str):
    """
    Apply an update in a Docker Compose environment.

    For 'latest' channel:  pull new images + restart containers
    For 'pinned' channel:  rewrite image tags in compose file, pull + restart

    Strategy: use raw `docker pull` + `docker compose up -d` scoped to each
    service individually.  The frontend is restarted first (safe — no state),
    then the backend restarts itself (the container dies and compose's
    restart policy brings it back with the new image).
    """
    global _update_status

    channel = _detect_update_channel()
    image_repo = os.getenv("PATCHPILOT_IMAGE_REPO", "linit01/patchpilot")

    # Find the compose file
    compose_file = None
    for cp in ("/install/docker-compose.yml", "/app/docker-compose.yml",
               "../docker-compose.yml", "docker-compose.yml"):
        if Path(cp).is_file():
            compose_file = Path(cp)
            break

    # Verify docker is available
    docker_bin = shutil.which("docker")
    if not docker_bin:
        _update_status.update({
            "active": False, "step": "failed",
            "error": "docker not found",
            "message": "Cannot find docker binary. "
                       "Update manually: docker compose pull && docker compose up -d",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        return

    # Check if docker compose plugin is available
    compose_cmd = None
    rc, _, _ = _run(["docker", "compose", "version"], timeout=5)
    if rc == 0:
        compose_cmd = ["docker", "compose"]
    else:
        dc = shutil.which("docker-compose")
        if dc:
            compose_cmd = [dc]

    try:
        _update_status.update({
            "active": True,
            "step": "preparing",
            "message": f"Preparing {channel} update to v{target_version}...",
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        })

        backend_image = f"{image_repo}:backend-{target_version}"
        frontend_image = f"{image_repo}:frontend-{target_version}"

        if channel == "pinned" and compose_file:
            _update_status["step"] = "updating_tags"
            _update_status["message"] = "Updating image tags in docker-compose.yml..."

            content = compose_file.read_text()
            content = re.sub(
                rf"(image:\s*{re.escape(image_repo)}:backend-)\S+",
                rf"\g<1>{target_version}",
                content,
            )
            content = re.sub(
                rf"(image:\s*{re.escape(image_repo)}:frontend-)\S+",
                rf"\g<1>{target_version}",
                content,
            )
            compose_file.write_text(content)
            logger.info("Updated image tags in %s", compose_file)

        # ── Pull new images using raw docker pull ─────────────────────────
        _update_status["step"] = "pulling"
        _update_status["message"] = "Pulling new backend image..."

        rc, out, err = _run(["docker", "pull", backend_image], timeout=300)
        if rc != 0:
            raise RuntimeError(f"docker pull backend failed: {err}")

        _update_status["message"] = "Pulling new frontend image..."
        rc, out, err = _run(["docker", "pull", frontend_image], timeout=300)
        if rc != 0:
            raise RuntimeError(f"docker pull frontend failed: {err}")

        # ── Restart services via helper container ─────────────────────────
        # The backend can't restart itself.  Spawn a short-lived helper
        # container (same pattern as uninstall_api.py) that:
        #   1. Pulls new images (already done above, but the helper has
        #      access to the host Docker daemon via the socket)
        #   2. Stops the old frontend + backend containers
        #   3. Starts new ones via docker compose up -d
        # The helper runs detached so this process can return the status
        # before it gets killed.
        _update_status["step"] = "restarting"
        _update_status["message"] = "Launching updater to restart services..."

        compose_path = str(compose_file)
        project_dir = str(compose_file.parent)
        frontend_container = os.getenv("FRONTEND_CONTAINER_NAME", "patchpilot-frontend-1")
        backend_container = os.getenv("BACKEND_CONTAINER_NAME", "patchpilot-backend-1")

        # Resolve the HOST path of the compose file.
        # Inside the container, the compose file is at /install/docker-compose.yml
        # but the host path is whatever was mounted as .:/install.
        # We can get it via `docker inspect` on our own container.
        host_project_dir = None
        try:
            # Get our own container ID
            with open("/proc/self/cgroup") as f:
                for line in f:
                    if "docker" in line or "containerd" in line:
                        # Extract container ID from cgroup path
                        parts = line.strip().split("/")
                        for part in reversed(parts):
                            if len(part) >= 12 and all(c in "0123456789abcdef" for c in part[:12]):
                                our_container_id = part
                                break
                        break

            if our_container_id:
                rc, inspect_out, _ = _run(
                    ["docker", "inspect", our_container_id,
                     "--format", '{{range .Mounts}}{{if eq .Destination "/install"}}{{.Source}}{{end}}{{end}}'],
                    timeout=10,
                )
                if rc == 0 and inspect_out:
                    # Docker Desktop may prefix with /host_mnt
                    host_project_dir = inspect_out.replace("/host_mnt", "")
                    logger.info("Resolved host project dir: %s", host_project_dir)
        except Exception as e:
            logger.warning("Failed to resolve host project dir: %s", e)

        if not host_project_dir:
            # Fallback: check INSTALL_DIR env var
            host_project_dir = os.getenv("INSTALL_DIR", "")

        if not host_project_dir:
            raise RuntimeError(
                "Cannot determine host path for docker-compose.yml. "
                "Set INSTALL_DIR in your .env to the host path of your PatchPilot directory."
            )

        host_compose_path = f"{host_project_dir}/docker-compose.yml"

        # Build the updater script that runs inside the helper container
        updater_script = (
            f"echo '[updater] Waiting 3s for status response to flush...' && sleep 3"
            f" && echo '[updater] Stopping frontend...' && docker stop {frontend_container}"
            f" && docker rm {frontend_container}"
            f" && echo '[updater] Stopping backend...' && docker stop {backend_container}"
            f" && docker rm {backend_container}"
            f" && echo '[updater] Starting services with new images...'"
            f" && docker compose -f {host_compose_path} --project-directory {host_project_dir}"
            f"    up -d --no-deps backend frontend"
            f" && echo '[updater] Update complete!'"
        )

        rc, container_id, err = _run(
            ["docker", "run", "--rm", "-d",
             "-v", "/var/run/docker.sock:/var/run/docker.sock",
             "-v", f"{host_project_dir}:{host_project_dir}:ro",
             "--env-file", f"{project_dir}/.env",
             "docker:cli",
             "sh", "-c", updater_script],
            timeout=30,
        )

        if rc != 0:
            raise RuntimeError(f"Failed to launch updater container: {err}")

        logger.info("Updater container started: %s", container_id[:12] if container_id else "ok")

        _update_status.update({
            "active": False,
            "step": "complete",
            "message": f"Update to v{target_version} complete. Page will reload.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        logger.error("Docker update failed: %s", e)
        _update_status.update({
            "active": False,
            "step": "failed",
            "message": str(e),
            "error": str(e),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class UpdateStatusResponse(BaseModel):
    current_version: str
    latest_version: Optional[str]
    update_available: bool
    release_url: Optional[str]
    release_notes: Optional[str]
    published_at: Optional[str]
    last_checked: Optional[str]
    check_error: Optional[str]
    channel: str
    install_mode: str


class UpdateApplyResponse(BaseModel):
    message: str
    target_version: str


class UpdateProgressResponse(BaseModel):
    active: bool
    step: Optional[str]
    message: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


@router.get("/status")
async def get_update_status(user: dict = Depends(require_auth)) -> UpdateStatusResponse:
    """Return current update status (cached from last check)."""
    return UpdateStatusResponse(
        current_version=CURRENT_VERSION,
        latest_version=_update_cache.get("latest_version"),
        update_available=_update_cache.get("update_available", False),
        release_url=_update_cache.get("release_url"),
        release_notes=_update_cache.get("release_notes"),
        published_at=_update_cache.get("published_at"),
        last_checked=_update_cache.get("last_checked"),
        check_error=_update_cache.get("check_error"),
        channel=_detect_update_channel(),
        install_mode=_detect_install_mode(),
    )


@router.post("/check")
async def trigger_check(user: dict = Depends(require_full_admin)) -> UpdateStatusResponse:
    """Force an immediate update check (admin only)."""
    await check_for_updates()
    return await get_update_status(user)


@router.post("/apply")
async def apply_update(user: dict = Depends(require_full_admin)):
    """
    Trigger an update to the latest available version (admin only).
    Runs asynchronously — poll /api/updates/progress for status.
    """
    if _update_status.get("active"):
        raise HTTPException(409, detail="An update is already in progress")

    if not _update_cache.get("update_available"):
        raise HTTPException(400, detail="No update available")

    target = _update_cache["latest_version"]
    mode = _detect_install_mode()

    if mode == "k3s":
        asyncio.create_task(_apply_update_k8s(target))
    else:
        asyncio.create_task(_apply_update_docker(target))

    return UpdateApplyResponse(
        message=f"Update to v{target} started. Poll /api/updates/progress for status.",
        target_version=target,
    )


@router.get("/progress")
async def get_update_progress(user: dict = Depends(require_auth)) -> UpdateProgressResponse:
    """Poll update progress."""
    return UpdateProgressResponse(**_update_status)
