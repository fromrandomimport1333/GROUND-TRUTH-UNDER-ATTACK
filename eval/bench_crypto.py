"""Ed25519 sign/verify throughput and per-record bytes for the L1 hash chain.

Wire format per record:  seq (8 B, big-endian u64) | prev_hash (32 B, SHA-256 of
the previous wire record) | payload (sensor reading) | signature (64 B, Ed25519
over seq|prev_hash|payload). The security overhead added per record is
signature + seq + prev_hash = 104 B; the payload is the sensor data itself.

CLI: uv run python -m eval.bench_crypto [--n 10000] [--run NAME]
     -> results/<run>/crypto.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import struct
import time
from typing import Any

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from eval.overhead import host_info, run_dir

SEQ_BYTES = 8
PREV_HASH_BYTES = 32
SIG_BYTES = 64
ADDED_BYTES_PER_RECORD = SEQ_BYTES + PREV_HASH_BYTES + SIG_BYTES  # 104
GENESIS_PREV_HASH = b"\x00" * PREV_HASH_BYTES
TAMPER_SAMPLE = 100


def _payload(temperature: float, epoch_s: float) -> bytes:
    return struct.pack(">dd", temperature, epoch_s)


def _sign_chain(key: Ed25519PrivateKey, payloads: list[bytes]) -> list[bytes]:
    wires: list[bytes] = []
    prev_hash = GENESIS_PREV_HASH
    for seq, payload in enumerate(payloads):
        msg = seq.to_bytes(SEQ_BYTES, "big") + prev_hash + payload
        wire = msg + key.sign(msg)
        wires.append(wire)
        prev_hash = hashlib.sha256(wire).digest()
    return wires


def _verify_record(
    pub: Ed25519PublicKey, wire: bytes, prev_wire: bytes | None
) -> bool:
    msg, sig = wire[:-SIG_BYTES], wire[-SIG_BYTES:]
    expected_prev = (
        GENESIS_PREV_HASH if prev_wire is None else hashlib.sha256(prev_wire).digest()
    )
    if msg[SEQ_BYTES : SEQ_BYTES + PREV_HASH_BYTES] != expected_prev:
        return False
    try:
        pub.verify(sig, msg)
    except InvalidSignature:
        return False
    return True


def crypto_overhead(n: int) -> dict[str, Any]:
    """Sign and verify `n` hash-chained sensor records with Ed25519.

    `sign_per_s` includes the chain hashing and the signature. `verify_per_s`
    includes the link check and the signature check. Tamper detection is
    checked on a sample after the timed loops so it does not affect timing.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    rng = np.random.default_rng(0)
    temps = rng.normal(15.0, 5.0, size=n)
    t0 = 1_700_000_000.0
    payloads = [_payload(float(temps[i]), t0 + 300.0 * i) for i in range(n)]
    payload_bytes = len(payloads[0])

    key = Ed25519PrivateKey.generate()
    pub = key.public_key()

    t = time.perf_counter()
    wires = _sign_chain(key, payloads)
    sign_s = max(time.perf_counter() - t, 1e-9)

    t = time.perf_counter()
    failures = 0
    prev: bytes | None = None
    for wire in wires:
        if not _verify_record(pub, wire, prev):
            failures += 1
        prev = wire
    verify_s = max(time.perf_counter() - t, 1e-9)

    sample = np.linspace(0, n - 1, num=min(n, TAMPER_SAMPLE), dtype=int)
    tampered_rejected = 0
    for i in sample:
        i = int(i)
        bad = bytearray(wires[i])
        bad[SEQ_BYTES + PREV_HASH_BYTES] ^= 0x01  # flip one payload bit
        prev_wire = wires[i - 1] if i > 0 else None
        if not _verify_record(pub, bytes(bad), prev_wire):
            tampered_rejected += 1

    wire_bytes = len(wires[0])
    assert wire_bytes == payload_bytes + ADDED_BYTES_PER_RECORD

    return {
        "algorithm": "Ed25519",
        "n": n,
        "sign_per_s": n / sign_s,
        "verify_per_s": n / verify_s,
        "bytes_per_record": ADDED_BYTES_PER_RECORD,
        "payload_bytes_per_record": payload_bytes,
        "wire_bytes_per_record": wire_bytes,
        "verify_failures": failures,
        "tampered_checked": len(sample),
        "tampered_rejected": tampered_rejected,
        "cryptography_version": importlib.metadata.version("cryptography"),
        "host": host_info(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=10_000, help="records to sign and verify")
    parser.add_argument(
        "--run", default=None, help="results/<run>/ folder (default: UTC timestamp)"
    )
    args = parser.parse_args(argv)

    result = crypto_overhead(args.n)
    path = run_dir(args.run) / "crypto.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
