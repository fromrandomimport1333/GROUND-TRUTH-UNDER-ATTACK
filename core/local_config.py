"""Trusted, local provisioning manifest. Never used as a remote data contract."""

import json
from pathlib import Path

from adapters.mqtt_transport import TLSSettings
from core.provenance.keys import safe_sensor_id


def load_manifest(directory: Path) -> dict:
    path = directory / "manifest.json"
    if path.stat().st_size > 65536:
        raise ValueError("manifest exceeds size limit")
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("schema_version") != "1.0":
        raise ValueError("unknown local manifest version")
    sensors = result.get("sensors", [])
    if not sensors or len(sensors) > 32 or len({s["sensor_id"] for s in sensors}) != len(sensors):
        raise ValueError("invalid sensor enrollment")
    for sensor in sensors:
        safe_sensor_id(sensor["sensor_id"])
    return result


def contained_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("manifest paths must stay inside provisioning directory")
    resolved = (root / path).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("manifest path escaped provisioning directory")
    return resolved


def connection_settings(directory: Path, manifest: dict, identity: str) -> TLSSettings:
    safe_sensor_id(identity)
    return TLSSettings(host=manifest["host"], port=manifest["port"],
        ca=contained_file(directory, "tls/ca.pem"),
        certificate=contained_file(directory, f"tls/{identity}.pem"),
        private_key=contained_file(directory, f"tls/{identity}.key.pem"))
