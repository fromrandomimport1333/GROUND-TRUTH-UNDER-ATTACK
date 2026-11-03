> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 1 verification results

Verification was performed on Linux x86_64 with CPython 3.14.8. Exact installed
package versions and import results are recorded in `environment.json`.

| Check actually executed | Result |
| --- | --- |
| Universal dependency lock resolution | Successful; includes Windows-only dependency markers |
| Hash-checked installation into Python 3.14 environment | Successful |
| Full automated test suite | 114 passed, no failures or skips |
| Integration subset | 6 passed, 108 deselected |
| `python -m pip check` | Exit 0; no broken requirements |
| Optional editable package install | Successful with `--no-deps --no-build-isolation` |
| Contract-only environment checker | Exit 0 |
| Full-plan environment checker | Exit 1 because Mosquitto is missing |
| JSON schema export | Successful for SignedReading, VerifiedReading and FeatureWindow |
| Git private-key and environment exclusions | Confirmed with `git check-ignore` |

`phase1-test-results.txt` contains command outputs and exit statuses. Tests are
synthetic internal-contract tests, not a dataset evaluation. The full
suite consists of 108 unit/regression checks and 6 integration checks. Integration
checks cover serialization, dependency primitives and contract consumer stubs.
They do not run a real detector, decision engine, broker or application pipeline.

The first full run passed 101 tests. After adding explicit security-boundary
regression cases, the final suite passed 114. The Ed25519 test checks the
installed cryptography primitive and detects modified test bytes; it does not
claim that the future application signer/verifier is implemented.

## Broker installation limitation

Mosquitto 2.1.2 source was downloaded from its official distribution and a
local build was attempted with `make -j2 WITH_DOCS=no WITH_WEBSOCKETS=no
WITH_EDITLINE=no WITH_SQLITE=no`. It failed with exit code 2 because
`cjson/cJSON.h` is not installed in this environment. No broker was installed,
started or exposed. No TLS connection was tested. Source and partial build
outputs are excluded from the deliverable.

Use the official Mosquitto 2.1.2 installer/package for the deployment OS and
run `mosquitto -h` followed by the full environment checker. Missing broker
installation means development step 1 remains partially complete. VS Code CLI
availability here does not verify the user's local editor installation.

## Remaining verification boundaries

Windows/macOS execution, MQTT v5/TLS communication, application signatures,
hash-chain/replay state, semantic unit normalization, time alignment, feature
calculations, all real L3/L4 integrations, dataset integration, attack/evasion
performance and benchmarks remain untested because their implementations or
inputs belong to later phases. Phase 2 requires separate approval.

A second isolated Python 3.14 environment installed the hash-locked requirements
and ran the complete source suite successfully: 114 passed. This checks clean
dependency installation on the same Linux host, not a different operating system.
