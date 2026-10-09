from pathlib import Path

import pytest

from tests.e2e.core.windows_update import _machine


def _new_machine(tmp_path: Path) -> _machine.Machine:
    return _machine.Machine(
        root=tmp_path / "machine",
        profile_name="profile",
        profiles_root=tmp_path / "profiles",
        base_url="http://127.0.0.1:1",
    )


def _disable_windows_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_machine.Machine, "kill_owned", lambda self: None)
    monkeypatch.setattr(_machine, "_restore_hkcu_path", lambda saved: None)


def test_teardown_retries_read_only_tree_removal(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    machine = _new_machine(tmp_path)
    machine.root.mkdir(parents=True)
    machine.profile.mkdir(parents=True)
    _disable_windows_cleanup(monkeypatch)
    monkeypatch.delenv("HERMES_E2E_ARTIFACTS", raising=False)

    real_rmtree = _machine.shutil.rmtree
    attempts: dict[Path, int] = {}

    def flaky_rmtree_readonly(path: Path) -> None:
        path = Path(path)
        attempts[path] = attempts.get(path, 0) + 1
        if path == machine.root and attempts[path] == 1:
            raise OSError("transient lock")
        real_rmtree(path)

    monkeypatch.setattr(_machine, "rmtree_readonly", flaky_rmtree_readonly, raising=False)
    monkeypatch.setattr(_machine.time, "sleep", lambda seconds: None)

    machine.teardown()

    assert attempts[machine.root] == 2
    assert attempts[machine.profile] == 1
    assert not machine.root.exists()
    assert not machine.profile.exists()


def test_teardown_preserves_sources_when_artifact_capture_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    machine = _new_machine(tmp_path)
    _ = machine.logs
    machine.profile.mkdir(parents=True)
    artifacts = tmp_path / "artifacts"
    _disable_windows_cleanup(monkeypatch)
    monkeypatch.setenv("HERMES_E2E_ARTIFACTS", str(artifacts))

    def fail_copy(*args, **kwargs):
        raise OSError("artifact destination unavailable")

    monkeypatch.setattr(_machine.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="artifact destination unavailable"):
        machine.teardown()

    assert machine.root.exists()
    assert machine.profile.exists()


def test_teardown_accepts_already_missing_trees(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    machine = _new_machine(tmp_path)
    _disable_windows_cleanup(monkeypatch)
    monkeypatch.delenv("HERMES_E2E_ARTIFACTS", raising=False)

    machine.teardown()

    assert not machine.root.exists()
    assert not machine.profile.exists()
