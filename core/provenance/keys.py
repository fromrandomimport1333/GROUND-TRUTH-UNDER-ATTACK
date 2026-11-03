"""Encrypted sensor keys. TLS identities are provisioned separately."""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.secure_files import read_private, write_new_private


def safe_sensor_id(sensor_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", sensor_id):
        raise ValueError("sensor ID must be a portable filename identifier, maximum 48 characters")
    return sensor_id


def key_password() -> bytes:
    value = os.environ.get("CIS_KEY_PASSWORD")
    if value is None:
        value = getpass.getpass("Sensor key password (at least 16 characters): ")
    if len(value) < 16:
        raise ValueError("key password must contain at least 16 characters")
    return value.encode("utf-8")


def key_id(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes_raw()
    return "ed25519-" + hashlib.sha256(raw).hexdigest()[:32]


def generate_key_pair(directory: Path, sensor_id: str, password: bytes) -> dict:
    safe_sensor_id(sensor_id)
    if len(password) < 16:
        raise ValueError("password too short")
    key = Ed25519PrivateKey.generate()
    private_name = f"{sensor_id}.ed25519.pem"
    public_name = f"{sensor_id}.public.pem"
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(password))
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    write_new_private(directory / private_name, encrypted)
    write_new_private(directory / public_name, public)
    return {"sensor_id": sensor_id, "key_id": key_id(key), "stream_id": "stream_01",
            "private_key": private_name, "public_key": public_name}


def load_private_key(path: Path, password: bytes) -> Ed25519PrivateKey:
    value = serialization.load_pem_private_key(read_private(path), password=password)
    if not isinstance(value, Ed25519PrivateKey):
        raise ValueError("expected an Ed25519 sensor key")
    return value
