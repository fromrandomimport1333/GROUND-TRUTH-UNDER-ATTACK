"""Read-only dependency checks. Does not start a broker or contact a network."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGES = {
    "pydantic": "pydantic", "cryptography": "cryptography", "pytest": "pytest",
    "paho-mqtt": "paho.mqtt.client", "cbor2": "cbor2", "numpy": "numpy", "pandas": "pandas",
}


def inspect_environment() -> dict:
    packages = {}
    for distribution, module in PACKAGES.items():
        try:
            version = importlib.metadata.version(distribution)
            importlib.import_module(module)
            packages[distribution] = {"version": version, "import_ok": True}
        except Exception as exc:
            packages[distribution] = {"import_ok": False, "error_type": type(exc).__name__}
    broker = {"installed": False, "target_version": "2.1.2", "target_matches": False}
    executable = shutil.which("mosquitto")
    if executable:
        broker["installed"] = True
        try:
            result = subprocess.run([executable, "-h"], capture_output=True, text=True,
                                    timeout=5, check=False)
            match = re.search(r"mosquitto version (\d+\.\d+\.\d+)", result.stdout + result.stderr)
            broker["version"] = match.group(1) if match else "unrecognized"
            broker["target_matches"] = broker["version"] == "2.1.2"
        except (OSError, subprocess.TimeoutExpired) as exc:
            broker["error_type"] = type(exc).__name__
    runtime_ok = sys.version_info[:2] == (3, 14)
    dependencies_ok = all(p["import_ok"] for p in packages.values())
    return {
        "python": platform.python_version(), "python_target": "3.14", "python_target_matches": runtime_ok,
        "system": platform.system(), "machine": platform.machine(),
        "packages": packages, "mosquitto": broker,
        "git_available": shutil.which("git") is not None,
        "vscode_cli_available": shutil.which("code") is not None,
        "contracts_environment_ready": runtime_ok and dependencies_ok,
        "full_plan_environment_ready": runtime_ok and dependencies_ok and broker["target_matches"],
        "broker_tls_tested": False,
        "note": "No hostnames, user names, credentials or absolute local paths are recorded.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contracts-only", action="store_true",
                        help="Do not require the broker for the exit status")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inspect_environment()
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    key = "contracts_environment_ready" if args.contracts_only else "full_plan_environment_ready"
    return 0 if report[key] else 1


if __name__ == "__main__":
    raise SystemExit(main())
