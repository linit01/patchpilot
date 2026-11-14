"""Tests for backend/manifest_updater.py — the in-app updater's manifest path.

Run from the repo root:  python -m pytest backend/tests
Needs PyYAML and pytest; the envsubst comparison is skipped where envsubst
(gettext) is not installed.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import manifest_updater as mu  # noqa: E402

TEMPLATES = ROOT / "k8s" / "templates"
INSTALLER = ROOT / "k8s" / "install-k3s.sh"

# Shaped like a real install's saved values; no real hostnames or addresses.
SAVED = {
    "PP_INSTALL_VALUES_SCHEMA": "1",
    "PP_NAMESPACE": "pp-test",
    "PP_VERSION": "1.0.0",
    "PP_BACKEND_IMAGE": "example/patchpilot:backend-1.0.0",
    "PP_FRONTEND_IMAGE": "example/patchpilot:frontend-1.0.0",
    "PP_DH_REPO": "example/patchpilot",
    "PP_IMAGE_TAG": "1.0.0",
    "PP_IMAGE_SUFFIX": "",
    "PP_IMAGE_PULL_POLICY": "IfNotPresent",
    "PP_PULL_SECRET_NAME": "pull-secret",
    "PP_DB_USER": "ppuser",
    "PP_DB_NAME": "ppdb",
    "PP_AUTO_REFRESH_INTERVAL": "300",
    "PP_DEFAULT_SSH_USER": "admin",
    "PP_DEFAULT_SSH_PORT": "22",
    "PP_BACKUP_STORAGE_TYPE": "nfs",
    "PP_NFS_SERVER": "192.0.2.10",
    "PP_NFS_SHARE": "/export/backups",
    "PP_BACKUP_RETAIN_COUNT": "5",
    "PP_MAX_BACKUP_SIZE_MB": "500",
    "PP_KUBECTL_BIN": "/usr/local/bin/kubectl",
    "PP_DATA_DIR": "/app-data",
    "PP_LICENSE_PROVIDER": "freemius",
    "PP_FREEMIUS_PRODUCT_ID": "12345",
}


def rendered_docs(values=SAVED):
    docs = []
    for name in mu.WORKLOAD_TEMPLATES:
        text = mu.render((TEMPLATES / name).read_text(), values)
        docs.extend(d for d in yaml.safe_load_all(text) if d)
    return docs


# ── render ───────────────────────────────────────────────────────────────────
def test_render_substitutes_both_forms_and_leaves_others():
    out = mu.render("a=$PP_X b=${PP_X} c=${PP_Y} d=$uri e=${NGINX_BACKEND_HOST}", {"PP_X": "v"})
    assert out == "a=v b=v c=${PP_Y} d=$uri e=${NGINX_BACKEND_HOST}"


def test_render_does_not_expand_shell_defaults_like_envsubst():
    assert mu.render("${PP_X:-dflt}", {"PP_X": "v"}) == "${PP_X:-dflt}"


@pytest.mark.skipif(not shutil.which("envsubst"), reason="envsubst not installed")
@pytest.mark.parametrize("name", mu.WORKLOAD_TEMPLATES)
def test_render_matches_installer_envsubst(name):
    """Same output as the installer's envsubst call for the workload templates."""
    shell_format = ":".join(f"${k}" for k in SAVED)
    text = (TEMPLATES / name).read_text()
    env = {**os.environ, **SAVED}
    expected = subprocess.run(["envsubst", shell_format], input=text, env=env,
                              capture_output=True, text=True, check=True).stdout
    assert mu.render(text, SAVED) == expected


def test_unresolved_lists_missing_settings():
    assert mu.unresolved("x: ${PP_NEW_THING} y: $PP_OTHER z: ${NGINX_BACKEND_HOST}") == [
        "PP_NEW_THING", "PP_OTHER"]


def test_workload_templates_render_completely_from_saved_keys():
    """A template that starts using a setting the installer doesn't save must
    fail here, not in a customer's update Job."""
    values = {k: SAVED.get(k, "x") for k in mu.SAVED_KEYS}
    for name in mu.WORKLOAD_TEMPLATES:
        text = mu.render((TEMPLATES / name).read_text(), values)
        assert mu.unresolved(text) == [], name


def test_installer_saves_exactly_the_keys_the_updater_reads():
    script = INSTALLER.read_text()
    body = script[script.index("save_install_values() {"):]
    body = body[:body.index("\n}\n")]
    saved = set(re.findall(r"--from-literal=(PP_[A-Z0-9_]+)=", body))
    assert saved - {"PP_INSTALL_VALUES_SCHEMA"} == set(mu.SAVED_KEYS)
    assert f"PP_INSTALL_VALUES_SCHEMA={mu.VALUES_SCHEMA} " in body


def test_installer_calls_save_install_values():
    script = INSTALLER.read_text()
    apply_fn = script[script.index("apply_manifests() {"):]
    assert "\n  save_install_values\n" in apply_fn[:apply_fn.index("\n}\n")]


# ── image references and the Job ─────────────────────────────────────────────
def test_image_refs_pinned():
    assert mu.image_refs(SAVED, "1.9.0") == (
        "example/patchpilot:backend-1.9.0", "example/patchpilot:frontend-1.9.0")


def test_image_refs_developer_arch_suffix():
    saved = {**SAVED, "PP_IMAGE_SUFFIX": "-arm64"}
    assert mu.image_refs(saved, "1.9.0")[0] == "example/patchpilot:backend-1.9.0-arm64"


def test_image_refs_latest_keeps_references():
    saved = {**SAVED, "PP_IMAGE_TAG": "latest",
             "PP_BACKEND_IMAGE": "example/patchpilot:backend-latest",
             "PP_FRONTEND_IMAGE": "example/patchpilot:frontend-latest"}
    assert mu.image_refs(saved, "1.9.0") == (
        "example/patchpilot:backend-latest", "example/patchpilot:frontend-latest")


def test_job_runs_new_image_with_backend_identity():
    job = mu.job_manifest("pp-test", SAVED, "1.9.0")
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert job["metadata"] == {
        "name": mu.JOB_NAME, "namespace": "pp-test",
        "labels": {"app.kubernetes.io/name": "patchpilot",
                   "app.kubernetes.io/component": "updater",
                   "app.kubernetes.io/version": "1.9.0"}}
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == mu.JOB_DEADLINE
    assert pod["serviceAccountName"] == "patchpilot-backend"
    assert pod["imagePullSecrets"] == [{"name": "pull-secret"}]
    assert c["image"] == "example/patchpilot:backend-1.9.0"
    assert c["imagePullPolicy"] == "IfNotPresent"
    assert c["command"][:2] == ["python", "/app/manifest_updater.py"]
    assert "--restart" not in c["command"]
    assert c["command"][c["command"].index("--frontend-image") + 1] == \
        "example/patchpilot:frontend-1.9.0"


def test_job_for_latest_channel_repulls_and_restarts():
    saved = {**SAVED, "PP_IMAGE_TAG": "latest",
             "PP_BACKEND_IMAGE": "example/patchpilot:backend-latest",
             "PP_FRONTEND_IMAGE": "example/patchpilot:frontend-latest"}
    c = mu.job_manifest("pp-test", saved, "1.9.0")["spec"]["template"]["spec"]["containers"][0]
    assert c["imagePullPolicy"] == "Always"
    assert "--restart" in c["command"]


def test_dockerfile_ships_the_workload_templates():
    dockerfile = (ROOT / "Dockerfile").read_text()
    for name in mu.WORKLOAD_TEMPLATES:
        assert f"k8s/templates/{name}" in dockerfile
    assert str(mu.TEMPLATES_DIR) + "/" in dockerfile


# ── postgres guard and document checks ───────────────────────────────────────
@pytest.mark.parametrize("image,major", [
    ("postgres:15-alpine", 15),
    ("postgres:18", 18),
    ("docker.io/library/postgres:16.4-bookworm", 16),
    ("registry.example:5000/postgres:15", 15),
    ("postgres:latest", None),
    ("postgres", None),
    ("postgres@sha256:" + "a" * 64, None),
    ("", None),
    (None, None),
])
def test_postgres_major(image, major):
    assert mu.postgres_major(image) == major


def test_current_templates_pass_against_same_major():
    docs = rendered_docs()
    live = mu.container_image(docs, "patchpilot-postgres", "postgres")
    mu.check_documents(docs, "pp-test", live)


def test_refuses_postgres_major_change():
    with pytest.raises(mu.Refused, match="Postgres from 14 to 15"):
        mu.check_documents(rendered_docs(), "pp-test", "postgres:14-alpine")


def test_refuses_unknown_postgres_version():
    with pytest.raises(mu.Refused, match="cannot compare"):
        mu.check_documents(rendered_docs(), "pp-test", "postgres:latest")


def test_refuses_kinds_outside_the_workloads():
    docs = rendered_docs() + [{"kind": "Secret", "metadata": {"name": "s", "namespace": "pp-test"}}]
    with pytest.raises(mu.Refused, match="Secret 's'"):
        mu.check_documents(docs, "pp-test", "postgres:15-alpine")


def test_refuses_other_namespace():
    with pytest.raises(mu.Refused, match="namespace"):
        mu.check_documents(rendered_docs(), "elsewhere", "postgres:15-alpine")


# ── resources: never lower what is running ───────────────────────────────────
@pytest.mark.parametrize("q,val", [
    ("500m", 0.5), ("1", 1.0), ("0.5", 0.5), ("2", 2.0),
    ("512Mi", 512 * 2**20), ("1Gi", 2**30), ("1G", 1e9), ("128974848", 128974848.0),
    ("1e3", None), ("lots", None), ("", None),
])
def test_parse_quantity(q, val):
    assert mu.parse_quantity(q) == val


def _live(resources_by_container):
    return {"patchpilot-backend": {"spec": {"template": {"spec": {"containers": [
        {"name": n, "resources": r} for n, r in resources_by_container.items()]}}}}}


def _backend(docs):
    d = next(d for d in docs if d.get("kind") == "Deployment"
             and d["metadata"]["name"] == "patchpilot-backend")
    return next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "backend")


def test_keeps_a_raised_running_limit():
    """Limits raised by hand above the release default are kept."""
    docs = rendered_docs()
    notes = mu.keep_larger_resources(docs, _live({"backend": {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "2", "memory": "2Gi"}}}))
    res = _backend(docs)["resources"]
    assert res["limits"] == {"memory": "2Gi", "cpu": "2"}
    assert res["requests"] == {"memory": "256Mi", "cpu": "100m"}
    assert len(notes) == 2 and all("keeping running" in n for n in notes)


def test_equal_running_limits_need_no_note():
    """The Site A shape since the default became 1 CPU / 1Gi."""
    docs = rendered_docs()
    notes = mu.keep_larger_resources(docs, _live({"backend": {
        "limits": {"cpu": "1", "memory": "1024Mi"}}}))
    assert _backend(docs)["resources"]["limits"] == {"memory": "1Gi", "cpu": "1"}
    assert notes == []


def test_release_can_raise_a_value():
    """The Site B shape: running the old 500m/512Mi default, release raises it."""
    docs = rendered_docs()
    notes = mu.keep_larger_resources(docs, _live({"backend": {
        "limits": {"cpu": "500m", "memory": "512Mi"}}}))
    assert _backend(docs)["resources"]["limits"] == {"memory": "1Gi", "cpu": "1"}
    assert notes == []


def test_unknown_quantities_and_missing_keys_are_left_alone():
    docs = rendered_docs()
    notes = mu.keep_larger_resources(docs, _live({
        "backend": {"limits": {"memory": "weird"}},
        "not-in-release": {"limits": {"memory": "64Gi"}}}))
    assert _backend(docs)["resources"]["limits"]["memory"] == "1Gi"
    assert notes == []


def test_written_yaml_round_trips_the_rendered_documents():
    """apply_release writes yaml.safe_dump_all of the parsed docs; the nginx
    template's `$uri`/`${NGINX_BACKEND_HOST}` text must survive unchanged."""
    docs = rendered_docs()
    again = [d for d in yaml.safe_load_all(yaml.safe_dump_all(docs, sort_keys=False)) if d]
    assert again == docs
    cm = next(d for d in again if d["kind"] == "ConfigMap")
    blob = "\n".join(cm["data"].values())
    assert "${NGINX_BACKEND_HOST}" in blob and "$uri" in blob


def test_rendered_workloads_are_only_allowed_kinds_in_namespace():
    for d in rendered_docs():
        assert d["kind"] in mu.ALLOWED_KINDS
        assert d["metadata"]["namespace"] == "pp-test"
