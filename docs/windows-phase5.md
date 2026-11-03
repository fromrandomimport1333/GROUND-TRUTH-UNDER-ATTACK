# Windows Phase 5

This is a cumulative project: Phases 1–5 are in one `CIS-Challenge` folder.
Use the folder containing this document's parent README and `pyproject.toml`.
Do not run commands in the older Phase 1 or Phase 4 copy. VS Code: File → Open
Folder → select this release's `CIS-Challenge` directory, then open its terminal.

Your existing Python environment can be reused; dependencies are unchanged.
The following absolute interpreter path matches your previously tested setup:

```powershell
Test-Path .\scripts\demo_attacks.py
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" -m scripts.demo_attacks
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" -m pytest -q tests
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" -m pytest -q tests --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

The Test-Path result must be True. Keep the explicit `tests` argument so pytest
loads the custom broker options. The demo needs no broker, existing keys or
provisioning. It creates temporary synthetic experiments and cleans them up.

For a fresh environment instead:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -m scripts.demo_attacks
.\.venv\Scripts\python.exe -m pytest -q tests --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

Never copy generated private keys/databases into a public submission. Existing
live deployments must retain their original matching signer/receiver state;
the Phase 5 experiments use fresh isolated state and do not migrate live state.
The Windows long-parameter-ID pytest fix is included. One existing symlink test
may be skipped on Windows due to privileges. Native Phase 5 results must be
reported from the actual run; Linux counts are not Windows verification.
