"""Behavioral containment contract for inactive candidate INT-2026-10-16-C."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ops" / "containment" / "int_c"
WORKER_PATH = PACKAGE / "worker.py"
UNIT_PATH = PACKAGE / "hermes-int-c-f10749396cea.service"


def _load_worker():
    spec = importlib.util.spec_from_file_location("int_c_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker = _load_worker()


def _policy(tmp_path: Path, *, connect_ips=None) -> Path:
    connect_ips = connect_ips or ["127.0.0.1", "::1"]
    payload = {
        "schema_version": 1,
        "job_id": worker.JOB_ID,
        "endpoints": [{
            "id": "fixture",
            "host": "fixture.invalid",
            "port": 443,
            "path": "/fixed",
            "connect_ips": connect_ips,
            "allowed_ipv4_cidrs": ["127.0.0.0/8"],
            "allowed_ipv6_cidrs": ["::1/128"],
        }],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _batch(root: Path, requests=None) -> Path:
    root.mkdir(exist_ok=True)
    path = root / "batch.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "job_id": worker.JOB_ID,
        "requests": requests or [{"endpoint": "fixture"}],
    }), encoding="utf-8")
    return path


def _run_worker(policy: Path, root: Path, batch: str = "batch.json", *extra: str, env=None):
    return subprocess.run(
        [sys.executable, "-I", str(WORKER_PATH), "--policy", str(policy),
         "--batch-root", str(root), "--batch", batch, *extra],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _unit_directives() -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for raw in UNIT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        directives.setdefault(key, []).append(value)
    return directives


def test_symlink_and_traversal_are_rejected(tmp_path):
    policy = _policy(tmp_path)
    batch_root = tmp_path / "input"
    _batch(batch_root)
    outside = tmp_path / "outside.json"
    outside.write_text((batch_root / "batch.json").read_text(), encoding="utf-8")
    (batch_root / "link.json").symlink_to(outside)

    traversal = _run_worker(policy, batch_root, "../outside.json", "--validate-only")
    symlink = _run_worker(policy, batch_root, "link.json", "--validate-only")

    assert traversal.returncode == 2
    assert symlink.returncode == 2
    assert "outside" not in traversal.stderr + symlink.stderr


def test_worker_does_not_write_or_delete_inputs(tmp_path):
    policy = _policy(tmp_path)
    batch_root = tmp_path / "input"
    batch = _batch(batch_root)
    before = {p.name: p.read_bytes() for p in (policy, batch)}

    result = _run_worker(policy, batch_root, "batch.json", "--validate-only")

    assert result.returncode == 0
    assert {p.name: p.read_bytes() for p in (policy, batch)} == before
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["batch.json", "policy.json"]


def test_operational_path_observes_no_subprocess(monkeypatch, tmp_path):
    policy = worker.load_policy(_policy(tmp_path))
    spawned = []

    def audit(event, _args):
        if event.startswith("subprocess") or event in {"os.system", "os.posix_spawn"}:
            spawned.append(event)

    sys.addaudithook(audit)
    results = worker.execute(policy, ("fixture",), lambda *_args: (200, b"ok"))

    assert results[0]["status"] == 200
    assert spawned == []


def test_ipv4_and_ipv6_are_explicitly_bounded(tmp_path):
    policy = worker.load_policy(_policy(tmp_path))
    seen = []

    def transport(_endpoint, address, _timeout, _max_bytes):
        seen.append(address)
        if address == "127.0.0.1":
            raise OSError("exercise IPv6 fallback")
        return 200, b"v6"

    assert worker.execute(policy, ("fixture",), transport)[0]["bytes"] == 2
    assert seen == ["127.0.0.1", "::1"]

    invalid = json.loads(_policy(tmp_path).read_text())
    invalid["endpoints"][0]["connect_ips"] = ["192.0.2.10"]
    invalid_path = tmp_path / "invalid-policy.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(worker.ContractError, match="outside"):
        worker.load_policy(invalid_path)


def test_allowed_destination_cannot_carry_arbitrary_exfiltration(tmp_path):
    policy_path = _policy(tmp_path)
    batch_root = tmp_path / "input"
    _batch(batch_root, [{"endpoint": "fixture", "body": "TOP-SECRET"}])

    rejected = _run_worker(policy_path, batch_root, "batch.json", "--validate-only")
    assert rejected.returncode == 2
    assert "TOP-SECRET" not in rejected.stdout + rejected.stderr

    policy = worker.load_policy(policy_path)
    captured = {}

    def transport(endpoint, _address, _timeout, _max_bytes):
        captured.update(host=endpoint.host, path=endpoint.path)
        return 200, b"sensitive-upstream-content"

    result = worker.execute(policy, ("fixture",), transport)
    encoded = json.dumps(result)
    assert captured == {"host": "fixture.invalid", "path": "/fixed"}
    assert "sensitive-upstream-content" not in encoded
    assert result[0]["sha256"]


def test_environment_is_not_an_input_or_output(tmp_path):
    policy = _policy(tmp_path)
    batch_root = tmp_path / "input"
    _batch(batch_root)
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "FAKE_API_SECRET": "do-not-leak"}

    result = _run_worker(policy, batch_root, "batch.json", "--validate-only", env=env)

    assert result.returncode == 0
    assert "do-not-leak" not in result.stdout + result.stderr
    directives = _unit_directives()
    assert directives["ExecStart"][0].startswith("/usr/bin/env -i ")
    assert "FAKE_API_SECRET" not in directives["ExecStart"][0]


def test_application_batch_memory_task_and_runtime_budgets(tmp_path):
    policy = worker.load_policy(_policy(tmp_path))
    too_many = tuple("fixture" for _ in range(worker.MAX_REQUESTS + 1))
    batch_root = tmp_path / "input"
    _batch(batch_root, [{"endpoint": item} for item in too_many])
    with pytest.raises(worker.ContractError, match="count"):
        worker.load_batch(batch_root, "batch.json")

    with pytest.raises(worker.ContractError, match="byte limit"):
        worker.execute(policy, ("fixture",), lambda *_args: (200, b"x" * (worker.MAX_RESPONSE_BYTES + 1)))

    with pytest.raises(worker.ContractError, match="runtime"):
        worker.execute(policy, ("fixture",), lambda *_args: (200, b"ok"), deadline=time.monotonic() - 1)

    directives = _unit_directives()
    assert directives["CPUQuota"] == ["25%"]
    assert directives["MemoryMax"] == ["512M"]
    assert directives["MemorySwapMax"] == ["0"]
    assert directives["TasksMax"] == ["2"]
    assert directives["RuntimeMaxSec"] == ["900"]


def test_packaged_startup_is_deliberately_invalid():
    directives = _unit_directives()
    assert "\n[Install]\n" not in UNIT_PATH.read_text(encoding="utf-8")
    assert directives["ConditionPathExists"] == ["/SECOND-AUTHORIZATION-REQUIRED/INT-2026-10-16-C"]
    assert any("@@" in value for value in directives["IPAddressAllow"])
    assert "/opt/hermes-int-c" in directives["ExecStart"][0]

    verify = subprocess.run(
        ["systemd-analyze", "verify", str(UNIT_PATH)], text=True, capture_output=True, check=False
    )
    assert "Invalid address" in verify.stderr

    packaged_policy = PACKAGE / "egress-policy.json"
    root = PACKAGE / "startup-fixture"
    startup = _run_worker(packaged_policy, root, "batch.json", "--validate-only")
    assert startup.returncode != 0


def test_package_manifest_binds_exact_payload_surface():
    manifest = PACKAGE / "PACKAGE.sha256"
    declared: dict[str, str] = {}
    for line in manifest.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        declared[name] = digest
    actual = {path.name for path in PACKAGE.iterdir() if path.is_file()} - {manifest.name}
    assert set(declared) == actual
    for name, digest in declared.items():
        assert hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest() == digest


def test_invalid_batch_exits_nonzero_without_payload_echo(tmp_path):
    policy = _policy(tmp_path)
    batch_root = tmp_path / "input"
    batch_root.mkdir()
    (batch_root / "batch.json").write_text('{"secret":"must-not-echo"}', encoding="utf-8")

    result = _run_worker(policy, batch_root)

    assert result.returncode == 2
    assert "must-not-echo" not in result.stdout + result.stderr
