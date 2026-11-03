# Windows Phase 2 setup

This is a cumulative source release. Extract it into the same outer folder as
Phase 1, replacing/merging the inner `CIS-Challenge` source files. The archive
contains no `.venv`, private keys or signer state. Your existing parent `.venv`
is retained. Never delete an existing `local/` bundle during later updates.

## Verify the updated source

Use PowerShell at the inner project directory. For your existing setup:

```powershell
cd "E:\CIS_Challenge_Phase1\CIS-Challenge"
Test-Path .\sensor.py
Test-Path .\scripts\setup_local.py
```

Both checks must return True. Then run:

```powershell
..\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m scripts.demo_signing
```

The default suite explicitly skips optional real-broker tests. One POSIX symlink
test is also skipped on Windows because creating symlinks may require privileges.
These skipped tests must not be reported as passed network/Windows checks.

## Provision once in the final project location

```powershell
..\.venv\Scripts\python.exe -m scripts.setup_local
```

Enter a password of at least 16 characters and retain it securely. Input is not
echoed. Reuse it for `sensor.py`. The script creates a private `local/` directory
with three encrypted Ed25519 keys, a public registry description, separate TLS
identities, broker configuration/ACLs and an initialized signer database.

Setup uses `whoami` and `icacls` to restrict newly created directories/files to
your Windows account. Permission-setting errors stop provisioning. Administrator
privileges and global PowerShell policy changes are not required. No passwords
are stored in configuration. `CIS_KEY_PASSWORD` is an optional automation input;
interactive input is preferable to leaving secrets in process environments.
Never paste passwords or private keys into chat or Git.

Existing bundles are never overwritten. Do not move `local/`: generated broker
paths refer to its original location. Certificates are local development
identities with bounded validity, not a public production PKI.

## Run the live demo in three terminals

Each terminal must be at the inner project directory.

Terminal 1 starts the broker:

```powershell
& "C:\Program Files\mosquitto\mosquitto.exe" -c ".\local\mosquitto.conf"
```

Leave it running. Terminal 2 starts reception:

```powershell
..\.venv\Scripts\python.exe -m scripts.observe_mqtt --count 9
```

Wait for `OBSERVER_READY`. Terminal 3 starts the sensors:

```powershell
..\.venv\Scripts\python.exe sensor.py --count 3
```

Enter the key password. Each of three sensors sends three readings, with a
default interval of two seconds. The sender prints connection/publication events;
the observer prints nine `RECEIVED_CBOR` records and exits. This describes the
expected behavior; native Windows Phase 2 execution is still pending your run.

The diagnostic observer intentionally reports `provenance_verified: false`.
It decodes CBOR but does not implement Layer 1b verification or replay controls.

To demonstrate restart continuity, start the observer with `--count 3`, then run
`sensor.py --count 1`. Sequences continue instead of resetting. Stop the broker
with Ctrl+C when finished. The listener is loopback-only on port 8883.

If 8883 is occupied, stop the conflicting local demo or provision a new bundle
with `--output local-other --port 8884`. Both Python programs then need
`--local local-other`, and the broker needs that bundle's config. Do not work
around a TLS failure by starting a plaintext or anonymous broker.

## Run actual broker integration tests

```powershell
..\.venv\Scripts\python.exe -m pytest -q --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

These tests choose a temporary loopback port and generate temporary identities.
They start/terminate their own broker without altering your existing service.
An explicitly requested missing broker is a test failure. Send the summary or
first error so Windows Phase 2 verification can be recorded accurately.

In VS Code, select the existing parent `.venv\Scripts\python.exe` using
**Python: Select Interpreter** if the extension defaults to a missing inner venv.
