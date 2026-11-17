"""Windows process supervisor; owns and stops only children it created."""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path


def commands():
    return {
        "market": [sys.executable, "-m", "quant.cli", "collect"],
        "api": [sys.executable, "-m", "quant.cli", "api"],
        "paper": [sys.executable, "-m", "quant.paper_cli", "watch"],
        "assistant": [sys.executable, "-m", "quant.assistant_cli", "watch"],
        "terminal": [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.address=127.0.0.1",
            "--server.port=8501",
            "--server.headless=true",
        ],
    }


def publish_status(path, status):
    """Windows readers may briefly deny FILE_SHARE_DELETE; status contention is nonfatal."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except PermissionError:
        return False


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def run(*, open_browser=True):
    import msvcrt

    root = Path.cwd().resolve()
    if not (root / "config.toml").is_file() or not (root / "app.py").is_file():
        raise RuntimeError(
            "Run the terminal from the unpacked project directory containing config.toml and app.py"
        )
    os.chdir(root)
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    lock = open("data/desktop.lock", "a+b")
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise RuntimeError("AI Quant terminal already running") from None
    children, logs, attempts, restart_at = {}, {}, {}, {}
    stop = Path("data/desktop.stop")
    stop.unlink(missing_ok=True)
    try:
        for port in (8501, 8765):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(
                        f"localhost port {port} occupied; stop that service or use standalone scripts"
                    )
        for name, command in commands().items():
            logs[name] = open(f"logs/desktop-{name}.log", "ab")
            children[name] = subprocess.Popen(
                command,
                cwd=root,
                stdout=logs[name],
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            attempts[name], restart_at[name] = 0, 0.0
        began, opened = time.monotonic(), False
        while not stop.exists():
            status = {"supervisor_pid": os.getpid(), "time_ms": int(time.time() * 1000), "services": {}}
            for name, process in children.items():
                code = process.poll()
                status["services"][name] = {
                    "pid": process.pid,
                    "running": code is None,
                    "exit_code": code,
                    "restarts": attempts[name],
                }
                if code is not None and attempts[name] < 3 and time.monotonic() >= restart_at[name]:
                    attempts[name] += 1
                    restart_at[name] = time.monotonic() + min(30, 2 ** attempts[name])
                    children[name] = subprocess.Popen(
                        commands()[name],
                        cwd=root,
                        stdout=logs[name],
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            publish_status("data/runtime.json", status)
            if open_browser and not opened and time.monotonic() - began >= 5:
                webbrowser.open("http://127.0.0.1:8501")
                opened = True
            time.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:
                # Windows venv python.exe can be a launcher with a second interpreter child.
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
        for process in children.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for file in logs.values():
            file.close()
        publish_status("data/runtime.json", {"stopped": True, "time_ms": int(time.time() * 1000)})
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop"])
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.command == "stop":
        root = Path.cwd().resolve()
        (root / "data").mkdir(exist_ok=True)
        (root / "data/desktop.stop").write_text("stop requested", encoding="utf-8")
        status_path = root / "data/runtime.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        if not process_alive(status.get("supervisor_pid")):
            publish_status(
                status_path,
                {"stopped": True, "already_not_running": True, "time_ms": int(time.time() * 1000)},
            )
    else:
        run(open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
