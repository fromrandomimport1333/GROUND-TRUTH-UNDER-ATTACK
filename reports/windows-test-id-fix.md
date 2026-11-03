> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Windows test-name compatibility fix

User-provided Windows logs on 2026-10-20 show four setup/teardown errors affecting
two oversized-input parameter cases. Pytest included the entire 65,537-byte
payload in each node ID and wrote it to PYTEST_CURRENT_TEST. Windows rejected
that environment variable because it exceeded its 32,767-character limit.
The uploaded fragments do not include a final pytest summary, so no Windows
pass count is claimed.

Fix: tests/conftest.py supplies bounded IDs for byte parameters, using the argument
name, byte count and first twelve SHA256 hex characters. Both original oversized
payloads, all assertions and all 318 cases are retained. No runtime behavior,
security policy, dependency version or signed contract changed.

Actual post-fix checks on Linux CPython 3.14.8:
- Collection: 318 cases; maximum node ID length 189 characters.
- Both oversized cases collect with payload_65537bytes_1abe08ebecf1 identifiers.
- Full real-broker regression: 318 passed in 8.21s, exit code 0.
- Captured output: windows-test-id-fix-results.txt.

The Windows user also supplied a successful Phase 4 demo output: 48 valid
readings (16 per sensor), all normalized to Celsius, with available temporal,
spatial and synthetic-reference groups. This confirms that demo on Windows;
it does not certify the full Windows test suite or live TLS/broker workflow.
Rerun pytest on Windows to validate the test-name fix natively. No successful
native rerun has yet been reported. Earlier reports remain historical evidence.
