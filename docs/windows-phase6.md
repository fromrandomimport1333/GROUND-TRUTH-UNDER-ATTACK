# Windows Phase 6

The cumulative archive contains one complete `CIS-Challenge` project for Phases
1–6. Extract `CIS_Challenge_Phase6.zip` to `E:\CIS_Challenge_Phase6`. Open
`E:\CIS_Challenge_Phase6\CIS-Challenge` in VS Code. Dependencies are unchanged.
Your existing virtual environment and installed Mosquitto can be reused.

```powershell
cd "E:\CIS_Challenge_Phase6\CIS-Challenge"
Test-Path .\benchmark.py
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" -m pytest -q tests --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

The Test-Path command should return True. Keep the explicit `tests` argument.
One existing symlink test may be skipped due to Windows privileges.

Measure offline stages and a real local TLS/plaintext transport comparison:

```powershell
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" benchmark.py --iterations 50 --series 3 --broker-executable "C:\Program Files\mosquitto\mosquitto.exe" --output reports\my-windows-benchmark.json
```

Run the three-sensor stability test for two minutes:

```powershell
& "E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe" stability.py --broker-executable "C:\Program Files\mosquitto\mosquitto.exe" --duration 120 --output reports\my-windows-stability.json
```

Both commands create private temporary experiment keys and an owned foreground
broker on ephemeral loopback ports, then clean them up. They do not modify your
existing private bundle or require manually launching a broker. No password
prompt is needed for these isolated experiments. Output files use exclusive
creation; choose a new filename for a later run rather than overwriting evidence.
The report appears when the run completes. Stability should take about two minutes
plus setup/cleanup; unusually slow storage can increase runtime.

Review `all_planned_faults_exercised`, `sender_receiver_heads_match`, pending counts,
actual elapsed time and status counts. Do not interpret `gap` and `replay` in the
fault scenario counts as unexpected failures. An unexpected outcome raises an
error. Timing values will vary by machine; there are no invented pass thresholds.

Fresh installation, if the earlier environment is unavailable:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
```

Then use `.\.venv\Scripts\python.exe` in the commands above. Choose this interpreter
in VS Code. The full attack demo remains available as `-m scripts.demo_attacks`.
