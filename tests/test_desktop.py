from unittest.mock import patch

from quant.desktop import commands, process_alive, publish_status


def test_status_read_lock_does_not_stop_services(tmp_path):
    path = tmp_path / "runtime.json"
    with patch("quant.desktop.os.replace", side_effect=PermissionError("Windows read lock")):
        assert not publish_status(path, {"test": True})
    assert publish_status(path, {"test": True})
    assert '"test": true' in path.read_text()


def test_supervised_commands_only_start_public_and_virtual_services():
    values = commands()
    assert set(values) == {"market", "api", "paper", "assistant", "terminal"}
    assert values["market"][-1] == "collect"
    assert values["paper"][-1] == "watch"
    assert "--server.address=127.0.0.1" in values["terminal"]


def test_native_process_probe_checks_actual_state():
    import os

    assert process_alive(os.getpid())
    assert not process_alive(0)
