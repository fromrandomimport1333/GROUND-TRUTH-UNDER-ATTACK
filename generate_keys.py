"""Generate encrypted Ed25519 keys in a new directory; never overwrite keys."""

import argparse
import json
from pathlib import Path

from core.provenance.keys import generate_key_pair, key_password, safe_sensor_id
from core.secure_files import new_private_directory, write_new_private


def generate_sensor_bundle(output: Path, sensor_ids: list[str], password: bytes) -> list[dict]:
    if not sensor_ids or len(set(sensor_ids)) != len(sensor_ids):
        raise ValueError("unique sensor IDs required")
    for identifier in sensor_ids:
        safe_sensor_id(identifier)
    if len(password) < 16:
        raise ValueError("password too short")
    directory = new_private_directory(output)
    sensors = [generate_key_pair(directory, identifier, password) for identifier in sensor_ids]
    public = [{k: v for k, v in item.items() if k != "private_key"} for item in sensors]
    write_new_private(directory / "public_registry.json",
                      (json.dumps({"schema_version": "1.0", "sensors": public}, indent=2) + "\n").encode())
    return sensors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("keys/sensors"))
    parser.add_argument("--sensors", nargs="+", default=["sensor_01", "sensor_02", "sensor_03"])
    args = parser.parse_args()
    generate_sensor_bundle(args.output, args.sensors, key_password())
    print("KEYS GENERATED (encrypted private keys; directory must be retained locally)")


if __name__ == "__main__":
    main()
