# Windows PowerShell: Phase 3

This cumulative archive contains Phases 1–3. Extract it into
`E:\CIS_Challenge_Phase1`, merging the inner `CIS-Challenge` source folder.
Keep the parent `.venv` and all existing private local bundles. No dependency
changes were made. Open the inner folder in VS Code and select
`E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe`.

## 1. Verify source and tests

```powershell
cd "E:\CIS_Challenge_Phase1\CIS-Challenge"
..\.venv\Scripts\python.exe -m pytest -q
```

Default tests skip real broker integration and one POSIX-only test on Windows.
Then run the complete broker-enabled suite:

```powershell
..\.venv\Scripts\python.exe -m pytest -q --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

Tests use temporary bundles and an ephemeral local port. They do not replace
production credentials or alter the installed Windows service. Report actual
results; Linux results in this release do not certify this Windows run.

## 2. Provision a fresh Phase 3 demonstration

A receiver starting at genesis cannot accept a previously advanced Phase 2
signer without its missing history. For the demo use a NEW named bundle on a
separate loopback port. Preserve your previous bundle unchanged.

```powershell
..\.venv\Scripts\python.exe -m scripts.setup_local --output local-phase3 --port 8884
```

Enter a password of at least 16 characters. Retain it for the simulator.
Provision only once; if `local-phase3` already exists, reuse it and its state.
Never delete an existing directory to reset the security state.

Initialize the receiver once, BEFORE the first sensor publication:

```powershell
..\.venv\Scripts\python.exe -m scripts.manage_registry --state local-phase3/receiver.sqlite3 init --bundle local-phase3/sensors
```

This imports only the enrolled public keys and streams. Existing receiver state
is never overwritten. Receiver startup does not need the sensor password.

## 3. Run three terminals from the project folder

Terminal A — broker:

```powershell
& "C:\Program Files\mosquitto\mosquitto.exe" -c ".\local-phase3\mosquitto.conf"
```

Terminal B — provenance receiver:

```powershell
..\.venv\Scripts\python.exe -m scripts.receive_mqtt --local local-phase3 --count 9 --timeout 120
```

Wait for `VERIFIER_READY`. Terminal C — three sensor rounds:

```powershell
..\.venv\Scripts\python.exe sensor.py --local local-phase3 --count 3
```

Enter the original sensor password. Terminal B emits JSON verdicts with
`status: "valid"` for the nine contiguous readings. The stdout output is the
example Layer 4 sink, not a completed L4 decision engine.

To repeat, restart B first and then C, retaining BOTH SQLite databases. Sequences
continue. Never run the simulator while the receiver is stopped if you expect a
complete clean-session demo; missing messages correctly result in `gap`.

## Administration examples

Revocation (replace placeholders with known enrolled identifiers):

```powershell
..\.venv\Scripts\python.exe -m scripts.manage_registry --state local-phase3/receiver.sqlite3 revoke --sensor sensor_01 --key-id "ENROLLED_KEY_ID"
```

Rotation requires a separately provisioned replacement public key and matching
signer stream. `enroll --sensor sensor_01 --public-key PATH --stream NEW_STREAM
--retire-key OLD_KEY_ID` atomically authorizes the replacement and revokes the old
key. It does not generate signer credentials. Do not run placeholder commands.

Read `phase3-security.md` before changing time tolerances or recovering a stream.
For the implemented processing stage, continue with windows-phase4.md.
