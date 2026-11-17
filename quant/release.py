"""Build and verify a source-install Windows release; never package runtime accounts or credentials."""

import hashlib
import json
import zipfile
from pathlib import Path


def main():
    root = Path.cwd().resolve()
    assert (root / "pyproject.toml").is_file() and root.name == "AIQuantTraderPro"
    required = [
        "README.md",
        "config.toml",
        "pyproject.toml",
        "app.py",
        "install.bat",
        "start.bat",
        "install.ps1",
        "start.ps1",
        "stop.ps1",
        "requirements-win-py314.lock",
        "docs/USER_MANUAL.md",
        "docs/API.md",
        "docs/RISK_AND_PAPER.md",
        "docs/ASSISTANT.md",
        "docs/STRATEGY.md",
        "docs/EXTENSIONS.md",
        "docs/PLAN.md",
        "reports/ACCEPTANCE.md",
        "reports/BACKTEST.md",
        "reports/backtest.json",
        "reports/tests-final.xml",
        "reports/install-tests.xml",
        "reports/soak.json",
        "reports/integration.json",
    ]
    assert all((root / p).is_file() for p in required), "required deliverable missing"
    for p in ("reports/soak.json", "reports/integration.json"):
        assert json.loads((root / p).read_text(encoding="utf-8"))["passed"], p
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(
            p.startswith(".venv")
            or p in ("__pycache__", "work", "build", "logs", ".pytest_cache", ".ruff_cache")
            or p.endswith(".egg-info")
            for p in relative.parts
        ):
            continue
        if path.name.startswith(".env") or path.name == "FILE_MANIFEST.json":
            continue
        if relative.parts[0] == "data" and relative.parts[:2] != ("data", "history"):
            continue
        if relative.parts[0] == "dist" and path.suffix != ".whl":
            continue
        if (
            path.suffix in (".py", ".toml", ".md", ".json", ".xml", ".txt", ".lock", ".ps1", ".bat", ".whl")
            or path.name == ".gitignore"
        ):
            files.append(path)
    manifest = {
        "name": "AI Quant Trader Pro",
        "version": "1.0.0",
        "edition": "Windows Python source installer",
        "files": {
            str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)
        },
    }
    manifest_path = root / "FILE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    archive = root.parent / "AIQuantTraderPro-Windows-v1.0.0.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files + [manifest_path]:
            z.write(path, "AIQuantTraderPro/" + path.relative_to(root).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        embedded = json.loads(z.read("AIQuantTraderPro/FILE_MANIFEST.json"))
        for name, digest in embedded["files"].items():
            assert hashlib.sha256(z.read("AIQuantTraderPro/" + name)).hexdigest() == digest, name
        assert not any("paper.sqlite" in n or ".venv/" in n or "/.env" in n for n in z.namelist())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    result = {
        "archive": str(archive),
        "bytes": archive.stat().st_size,
        "files": len(files) + 1,
        "sha256": digest,
        "zip_crc_and_all_file_hashes_verified": True,
        "excluded": "virtual environments, caches, logs, credentials, production market/paper/notification databases",
    }
    (root.parent / "AIQuantTraderPro-release-verification.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
