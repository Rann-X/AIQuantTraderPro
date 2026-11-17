"""Windows release integration: real start/stop scripts, local services, live OKX and read-lock contention."""

import json
import subprocess
import time
from pathlib import Path

import httpx

from quant.config import BAR_MS, load_settings
from quant.desktop import process_alive
from quant.models import Candle


def wait_until(check, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise TimeoutError("integration condition did not become true")


def main():
    settings = load_settings()
    runtime = Path("data/runtime.json")
    before = json.loads(runtime.read_text(encoding="utf-8")) if runtime.exists() else {}
    result = {"started_ms": int(time.time() * 1000), "previous_supervisor": before.get("supervisor_pid")}
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    subprocess.run(
        command + ["stop.ps1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
    )

    def stopped():
        try:
            return json.loads(runtime.read_text(encoding="utf-8")).get("stopped", False)
        except (OSError, ValueError):
            return False

    wait_until(stopped, 30)
    result["stop_script_verified"] = True
    # Windows background children can inherit captured pipe handles after PowerShell exits.
    subprocess.run(
        command + ["start.ps1", "-NoBrowser"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    with httpx.Client(timeout=5, trust_env=False) as client:

        def ready():
            try:
                return client.get("http://127.0.0.1:8765/health").status_code == 200
            except httpx.HTTPError:
                return False

        wait_until(ready, 120)
        health = client.get("http://127.0.0.1:8765/health").json()
        assert health["ready"]
        assert client.get("http://127.0.0.1:8501/_stcore/health").status_code == 200
        state = json.loads(runtime.read_text(encoding="utf-8"))
        assert all(x["running"] for x in state["services"].values())
        pid = state["supervisor_pid"]
        # Reproduce actual Windows read sharing that previously terminated the supervisor.
        with runtime.open("rb"):
            time.sleep(3)
            assert ready()
        time.sleep(2)
        assert json.loads(runtime.read_text(encoding="utf-8"))["supervisor_pid"] == pid
        result.update(
            start_script_verified=True,
            windows_status_read_lock_survived=True,
            supervisor_pid=pid,
            services=state["services"],
            health=health,
            candle_checks={},
            analysis_checks={},
        )
        for symbol in settings.symbols:
            for bar, step in BAR_MS.items():
                response = client.get(f"http://127.0.0.1:8765/candles/{symbol}/{bar}")
                response.raise_for_status()
                rows = [Candle(**r) for r in response.json()]
                assert len(rows) >= 100 and all(c.confirmed for c in rows)
                assert all(b.ts - a.ts == step for a, b in zip(rows, rows[1:]))
                result["candle_checks"][f"{symbol}:{bar}"] = {"count": len(rows), "last_ts": rows[-1].ts}
            analysis = client.get(f"http://127.0.0.1:8765/analysis/{symbol}").json()
            assert analysis["current_price"] is not None and analysis["reference_close"] is not None
            assert not analysis["risk_approved"]
            explanation = client.get(f"http://127.0.0.1:8765/explanation/{symbol}").json()
            assert explanation["engine"] == "grounded_rules"
            result["analysis_checks"][symbol] = {
                "status": analysis["status"],
                "current_price": analysis["current_price"],
                "reference_close": analysis["reference_close"],
            }
        paths = client.get("http://127.0.0.1:8765/openapi.json").json()["paths"]
        assert not any("post" in methods or "delete" in methods for methods in paths.values())
        result.update(read_only_api_verified=True, completed_ms=int(time.time() * 1000), passed=True)
        subprocess.run(
            command + ["stop.ps1"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        wait_until(lambda: not process_alive(pid), 30)
        assert all(not process_alive(v["pid"]) for v in state["services"].values())
        for port in (8501, 8765):
            try:
                client.get(f"http://127.0.0.1:{port}/health")
            except httpx.TransportError:
                pass
            else:
                raise AssertionError(f"port {port} still serving after owned process tree stop")
        result["owned_process_tree_stop_verified"] = True
        subprocess.run(
            command + ["start.ps1", "-NoBrowser"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        wait_until(ready, 120)
        result["final_runtime"] = json.loads(runtime.read_text(encoding="utf-8"))
        result["completed_ms"] = int(time.time() * 1000)
    Path("reports/integration.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in ("health", "candle_checks", "analysis_checks", "services")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
