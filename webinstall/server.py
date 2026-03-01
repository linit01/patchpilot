"""
PatchPilot v0.9.6-alpha — Web Install Server
Collects configuration via browser wizard, writes config files,
then streams ./install.sh output back to the browser via SSE.
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────────
WEBINSTALL_DIR  = Path(__file__).parent
REPO_ROOT       = Path(os.environ.get("PATCHPILOT_ROOT", WEBINSTALL_DIR.parent))
STATIC_DIR      = WEBINSTALL_DIR / "static"
K8S_DIR         = REPO_ROOT / "k8s"
CONFIG_FILE     = K8S_DIR / "install-config.yaml"
INSTALL_SCRIPT  = REPO_ROOT / "install.sh"
BUILD_SCRIPT    = K8S_DIR / "build-push.sh"
RESUME_FILE     = Path("/tmp/patchpilot-install-resume")
DEVELOPER_MODE  = os.environ.get("PATCHPILOT_DEVELOPER", "false").lower() == "true"

app = FastAPI(title="PatchPilot Web Installer")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


# ── Server info (developer mode flag, version) ─────────────────────────────────
@app.get("/api/info")
async def info():
    """Returns server capabilities so the UI can show/hide developer features."""
    return {
        "developer": DEVELOPER_MODE,
        "version":   "0.9.6-alpha",
        "repo_root": str(REPO_ROOT),
    }


# ── Cluster info ───────────────────────────────────────────────────────────────
@app.get("/api/cluster-info")
async def cluster_info():
    contexts, storage_classes = [], []
    try:
        r = subprocess.run(["kubectl","config","get-contexts","-o","name"],
                           capture_output=True, text=True, timeout=5)
        contexts = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    try:
        r = subprocess.run(["kubectl","get","sc","--no-headers",
                            "-o","custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts:
                storage_classes.append({"name": parts[0],
                                        "provisioner": parts[1] if len(parts) > 1 else ""})
    except Exception:
        pass
    return {"contexts": contexts, "storageClasses": storage_classes}


@app.get("/api/validate-cluster")
async def validate_cluster():
    try:
        r = subprocess.run(["kubectl","cluster-info"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            ctx = subprocess.run(["kubectl","config","current-context"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            return {"ok": True, "context": ctx}
        return {"ok": False, "error": r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Config models ──────────────────────────────────────────────────────────────
class DockerConfig(BaseModel):
    mode: str = "docker"

class K3sConfig(BaseModel):
    mode: str = "k3s"
    namespace: str = "patchpilot"
    dh_repo: str = "linit01/patchpilot"
    dh_username: str
    dh_token: str
    image_tag: str = "0.9.6-alpha"
    pull_policy: str = "Always"
    hostname: str
    additional_hostnames: str = ""
    tls_enabled: bool = True
    https_redirect: bool = True
    security_headers: bool = True
    ingress_class: str = "traefik"
    cluster_issuer: str = "letsencrypt-prod"
    tls_secret_name: str = ""
    create_cluster_issuer: bool = True
    le_email: str = ""
    challenge_type: str = "dns01-cloudflare"
    cf_email: str = ""
    cf_api_token_secret: str = "cloudflare-api-token-secret"
    db_user: str = "patchpilot"
    db_password: str = ""
    db_name: str = "patchpilot"
    postgres_storage_class: str = "local-data"
    postgres_storage_size: str = "5Gi"
    encryption_key: str = ""
    auto_refresh_interval: int = 60
    default_ssh_user: str = "root"
    default_ssh_port: int = 22
    backup_retain_count: int = 3
    max_backup_size_mb: int = 500
    backup_storage_type: str = "local"
    app_storage_class: str = ""
    backups_storage_size: str = "10Gi"
    ansible_storage_size: str = "1Gi"
    nfs_server: str = ""
    nfs_share: str = ""
    ansible_playbook_path: str = ""
    ansible_inventory_path: str = ""


# ── Write config ───────────────────────────────────────────────────────────────
@app.post("/api/configure/k3s")
async def configure_k3s(cfg: K3sConfig):
    try:
        _write_k3s_config(cfg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "config_file": str(CONFIG_FILE)}

@app.post("/api/configure/docker")
async def configure_docker(cfg: DockerConfig):
    if not (REPO_ROOT / ".env.example").exists():
        raise HTTPException(status_code=500, detail=".env.example not found")
    return {"status": "ok"}


# ── Resume after manual cleanup pause ─────────────────────────────────────────
@app.post("/api/resume")
async def resume():
    """
    Called by the browser when the user confirms they've run the cleanup command.
    Creates the resume file that unblocks the waiting install script.
    """
    try:
        RESUME_FILE.touch()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}


# ── Build & Push stream (developer mode only) ──────────────────────────────────
@app.get("/api/build-stream")
async def build_stream(
    repo: str = "linit01/patchpilot",
    tag: str = "0.9.6-alpha",
    platform: str = "",
    no_cache: bool = False,
    push: bool = True,
):
    """
    SSE — runs k8s/build-push.sh with the given parameters.
    Only meaningful when DEVELOPER_MODE is True, but not gated server-side
    so the script can be invoked manually too.
    """
    if not BUILD_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"build-push.sh not found at {BUILD_SCRIPT}")

    cmd = [str(BUILD_SCRIPT), "--tag", tag]
    if platform:
        cmd += ["--platform", platform]
    if no_cache:
        cmd.append("--no-cache")
    if not push:
        cmd.append("--no-push")

    async def event_generator():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env={**os.environ,
                     "TERM": "xterm-256color",
                     "DH_REPO_OVERRIDE": repo},
            )
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                yield f"data: {json.dumps(line)}\n\n"
            await proc.wait()
            yield f"data: {json.dumps('__EXIT__' + str(proc.returncode))}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps('__ERROR__' + str(exc))}\n\n"
            yield f"data: {json.dumps('__EXIT__1')}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Install stream ─────────────────────────────────────────────────────────────
@app.get("/api/install-stream")
async def install_stream(mode: str = "k3s"):
    """
    SSE — spawns install.sh --<mode> --no-interactive and streams output.
    Special markers emitted by the shell script:
      __PAUSE_CLEANUP__ <cmd>   → pauses UI, shows cleanup card
      __NOTE_CLEANUP__ <cmd>    → informational note after uninstall
      __EXIT__<code>            → install finished
    """
    if mode not in ("docker", "k3s"):
        raise HTTPException(status_code=400, detail="mode must be docker or k3s")

    # Clean up any stale resume file from a previous run
    RESUME_FILE.unlink(missing_ok=True)

    cmd = [str(INSTALL_SCRIPT), f"--{mode}", "--no-interactive"]

    async def event_generator():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env={**os.environ, "TERM": "xterm-256color"},
            )
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()

                # Intercept structured pause marker
                if line.startswith("__PAUSE_CLEANUP__"):
                    cmd_str = line[len("__PAUSE_CLEANUP__"):].strip()
                    yield f"data: {json.dumps('__PAUSE_CLEANUP__' + cmd_str)}\n\n"
                    continue

                # Intercept post-uninstall cleanup note
                if line.startswith("__NOTE_CLEANUP__"):
                    cmd_str = line[len("__NOTE_CLEANUP__"):].strip()
                    yield f"data: {json.dumps('__NOTE_CLEANUP__' + cmd_str)}\n\n"
                    continue

                yield f"data: {json.dumps(line)}\n\n"

            await proc.wait()
            yield f"data: {json.dumps('__EXIT__' + str(proc.returncode))}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps('__ERROR__' + str(exc))}\n\n"
            yield f"data: {json.dumps('__EXIT__1')}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Uninstall stream ───────────────────────────────────────────────────────────
@app.get("/api/uninstall-stream")
async def uninstall_stream():
    """
    SSE — runs install-k3s.sh --uninstall --no-interactive.
    Requires install-config.yaml to exist (namespace is read from it).
    """
    if not CONFIG_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="No install-config.yaml found. Cannot determine namespace to uninstall."
        )

    k3s_script = REPO_ROOT / "k8s" / "install-k3s.sh"
    cmd = [str(k3s_script), "--uninstall", "--no-interactive"]

    async def event_generator():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                env={**os.environ, "TERM": "xterm-256color"},
            )
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line.startswith("__NOTE_CLEANUP__"):
                    cmd_str = line[len("__NOTE_CLEANUP__"):].strip()
                    yield f"data: {json.dumps('__NOTE_CLEANUP__' + cmd_str)}\n\n"
                    continue
                yield f"data: {json.dumps(line)}\n\n"
            await proc.wait()
            yield f"data: {json.dumps('__EXIT__' + str(proc.returncode))}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps('__ERROR__' + str(exc))}\n\n"
            yield f"data: {json.dumps('__EXIT__1')}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Config writer ──────────────────────────────────────────────────────────────
def _write_k3s_config(cfg: K3sConfig):
    app_sc = cfg.app_storage_class or (
        "nfs-backups" if cfg.backup_storage_type == "nfs" else cfg.postgres_storage_class
    )
    additional = [h.strip() for h in cfg.additional_hostnames.replace(",", " ").split() if h.strip()]
    data = {
        "patchpilot": {
            "version": "0.9.6-alpha",
            "namespace": cfg.namespace,
            "image": {
                "strategy": "registry",
                "dockerHubRepo": cfg.dh_repo,
                "tag": cfg.image_tag,
                "pullPolicy": cfg.pull_policy,
            },
            "dockerHub": {"username": cfg.dh_username, "token": cfg.dh_token},
            "network": {
                "hostname": cfg.hostname,
                "additionalHostnames": additional,
                "tls": {
                    "enabled": cfg.tls_enabled,
                    "clusterIssuer": cfg.cluster_issuer,
                    "secretName": cfg.tls_secret_name,
                },
                "httpsRedirect": cfg.https_redirect,
                "securityHeaders": cfg.security_headers,
                "ingressClass": cfg.ingress_class,
            },
            "certManager": {
                "createClusterIssuer": cfg.create_cluster_issuer,
                "email": cfg.le_email,
                "challengeType": cfg.challenge_type,
                "cloudflare": {
                    "email": cfg.cf_email,
                    "apiTokenSecretName": cfg.cf_api_token_secret,
                },
            },
            "postgres": {
                "user": cfg.db_user,
                "password": cfg.db_password,
                "database": cfg.db_name,
                "storageSize": cfg.postgres_storage_size,
                "storageClass": cfg.postgres_storage_class,
            },
            "app": {
                "encryptionKey": cfg.encryption_key,
                "autoRefreshInterval": cfg.auto_refresh_interval,
                "defaultSshUser": cfg.default_ssh_user,
                "defaultSshPort": cfg.default_ssh_port,
                "backupRetainCount": cfg.backup_retain_count,
                "maxBackupSizeMb": cfg.max_backup_size_mb,
            },
            "storage": {
                "type": cfg.backup_storage_type,
                "storageClass": app_sc,
                "backupsSize": cfg.backups_storage_size,
                "ansibleSize": cfg.ansible_storage_size,
                "nfsServer": cfg.nfs_server,
                "nfsShare": cfg.nfs_share,
            },
            "ansible": {
                "playbookPath": cfg.ansible_playbook_path,
                "inventoryPath": cfg.ansible_inventory_path,
            },
        }
    }
    K8S_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
