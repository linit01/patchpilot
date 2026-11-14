"""Tests for k8s/yaml_get.py — the installer's config reader.

The fallback parser (used when PyYAML is not installed) must give the same
answers as PyYAML for every key install-k3s.sh reads.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "k8s"))

import yaml_get as yg  # noqa: E402

EXAMPLE = ROOT / "k8s" / "install-config.yaml.example"
INSTALLER = (ROOT / "k8s" / "install-k3s.sh").read_text()
KEYS = sorted(set(re.findall(r"yaml_get (patchpilot\.[\w.]+)", INSTALLER)))

NESTED = """\
# leading comment
patchpilot:
  namespace: pp-test   # trailing comment
  image:
    strategy: registry
    pullPolicy: "IfNotPresent"
  network:
    hostname: 'pp.example.test'
    additionalHostnames: []
    tls:
      enabled: true
      clusterIssuer: issuer-a
      secretName: ''
  extra:
    hosts:
      - a.example.test
      - b.example.test
    inline: [x, y]
  certManager:
    createClusterIssuer: false
    email:
  storage:
    nfsShare: /export/with#hash
    backupsSize: 10Gi
"""


def test_installer_reads_some_keys():
    assert len(KEYS) > 20
    assert "patchpilot.network.tls.clusterIssuer" in KEYS


@pytest.mark.parametrize("text", [EXAMPLE.read_text(), NESTED], ids=["example", "nested"])
def test_fallback_matches_pyyaml(text):
    want, got = yaml.safe_load(text), yg.parse_fallback(text)
    for key in KEYS + ["patchpilot.extra.hosts", "patchpilot.extra.inline"]:
        assert yg.render(yg.get(got, key), "D") == yg.render(yg.get(want, key), "D"), key


def test_nested_values_and_defaults():
    d = yg.parse_fallback(NESTED)
    assert yg.render(yg.get(d, "patchpilot.network.hostname"), "D") == "pp.example.test"
    assert yg.render(yg.get(d, "patchpilot.network.tls.enabled"), "D") == "true"
    assert yg.render(yg.get(d, "patchpilot.certManager.createClusterIssuer"), "D") == "false"
    assert yg.render(yg.get(d, "patchpilot.certManager.email"), "D") == "D"
    assert yg.render(yg.get(d, "patchpilot.network.tls.secretName"), "D") == "D"
    assert yg.render(yg.get(d, "patchpilot.network.additionalHostnames"), "D") == ""
    assert yg.render(yg.get(d, "patchpilot.extra.hosts"), "D") == "a.example.test\nb.example.test"
    assert yg.render(yg.get(d, "patchpilot.storage.nfsShare"), "D") == "/export/with#hash"
    assert yg.render(yg.get(d, "patchpilot.missing.key"), "D") == "D"


def test_bad_indentation_is_an_error_not_defaults():
    with pytest.raises(yg.ConfigError):
        yg.parse_fallback("patchpilot:\n\tnamespace: x\n")
    with pytest.raises(yg.ConfigError):
        yg.parse_fallback("patchpilot:\n  this is not yaml\n")


def _run(*args):
    return subprocess.run([sys.executable, str(ROOT / "k8s" / "yaml_get.py"), *args],
                          capture_output=True, text=True, timeout=30)


def test_cli_reads_nested_key(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(NESTED)
    r = _run(str(cfg), "patchpilot.network.tls.clusterIssuer", "D")
    assert (r.returncode, r.stdout) == (0, "issuer-a\n")


def test_cli_fails_on_missing_file(tmp_path):
    r = _run(str(tmp_path / "nope.yaml"), "patchpilot.namespace", "D")
    assert r.returncode == 2 and r.stdout == ""


def test_cli_without_pyyaml_uses_fallback(tmp_path):
    """The bug this replaces: with no PyYAML every nested key came back as the default."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(NESTED)
    code = (
        "import sys, builtins\n"
        "real = builtins.__import__\n"
        "def no_yaml(name, *a, **k):\n"
        "    if name == 'yaml': raise ImportError\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = no_yaml\n"
        f"sys.argv = ['yaml_get.py', {str(cfg)!r}, 'patchpilot.network.hostname', 'D']\n"
        f"exec(open({str(ROOT / 'k8s' / 'yaml_get.py')!r}).read())\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert r.stdout == "pp.example.test\n", r.stderr
