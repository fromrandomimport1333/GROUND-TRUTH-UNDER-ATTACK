# Windows / PowerShell guide

Open the extracted project directory containing pyproject.toml in VS Code.
Commands use the venv executable directly; activation is optional. Python 3.14
is the requested target; execution here uses Python 3.12 on Linux, so native
Windows/3.14 behavior is not claimed as tested.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r .\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .\requirements-wegenernet.txt
.\.venv\Scripts\Activate.ps1
```

Keep the two pip install commands separate: the NetCDF extras are not hash-locked
and cannot be combined with --require-hashes. Activation is unnecessary if
PowerShell policy prevents it; continue with the explicit executable paths.

Store monthly half-hourly files in .\data\half-hourly. Do not mix HD hourly files.

```powershell
$env:WN_HALFHOURLY_FILE = (Resolve-Path '.\data\half-hourly\WN_L2_HHD_v8_UTM_TF1_UTC_2023-01.nc').Path
.\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }

.\.venv\Scripts\python.exe -m scripts.export_wegenernet_halfhourly `
  $env:WN_HALFHOURLY_FILE `
  --cells-file .\config\cells.txt `
  --start 2023-01-01T00:00:00Z --end 2023-02-01T00:00:00Z `
  --output .\outputs\half-hourly-january
if ($LASTEXITCODE -ne 0) { throw 'Export incomplete; inspect log and resume' }

.\.venv\Scripts\python.exe -m scripts.validate_wegenernet_export `
  .\outputs\half-hourly-january $env:WN_HALFHOURLY_FILE
if ($LASTEXITCODE -ne 0) { throw 'Validation failed' }
Get-Content .\outputs\half-hourly-january\SUCCESS.json
Get-Content .\outputs\half-hourly-january\2023-01\compact-schema.json
.\.venv\Scripts\python.exe -c "import pandas as pd; d=pd.read_csv('outputs/half-hourly-january/2023-01/compact.csv.gz'); print(d.shape); print(d.head()); print(d[['timestamp','cell_id']].duplicated().sum())"
```

For ALL 24 months once they are actually present, optionally including December
2022 in the same folder:

```powershell
$files = @(Get-ChildItem '.\data\half-hourly\WN_L2_HHD_v8_UTM_TF1_UTC_*.nc' | Sort-Object Name | ForEach-Object FullName)
if ($files.Count -eq 0) { throw 'No half-hourly source files found' }
.\.venv\Scripts\python.exe -m scripts.export_wegenernet_halfhourly @files `
  --cells-file .\config\cells.txt `
  --start 2023-01-01T00:00:00Z --end 2025-01-01T00:00:00Z `
  --warmup-hours 168 --output .\outputs\half-hourly-2023-2024
if ($LASTEXITCODE -ne 0) { throw 'Export incomplete; inspect log and resume' }
```

The command preflights coverage and fails on missing target intervals. Warm-up
at the beginning may be unavailable; it is reported. Monthly outputs are assigned
by source interval start, so January includes the label February 1 00:00.
The same command supports any consecutive monthly range by changing start/end.

Resume after interruption with exactly the same source contents, cells and dates:

```powershell
.\.venv\Scripts\python.exe -m scripts.export_wegenernet_halfhourly @files `
  --cells-file .\config\cells.txt `
  --start 2023-01-01T00:00:00Z --end 2025-01-01T00:00:00Z `
  --warmup-hours 168 --output .\outputs\half-hourly-2023-2024 --resume
```

Resume rereads earlier inputs to reconstruct history and cryptographic chain
state. It preserves completed partitions after checking their hashes, then
rebuilds incomplete ones. This can take substantial time. Ensure disk space for
the full evidence journals and temporary SQLite state, not just compact CSV.
Do not run two exporters concurrently on the same output directory.

Validate the completed supplied sequence (the validator's observation index
uses memory proportional to source records; full two-year validation here is
not measured):

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_wegenernet_export `
  .\outputs\half-hourly-2023-2024 @files
if ($LASTEXITCODE -ne 0) { throw 'Validation failed or resource limit reached' }
```

Do not call a resource-limited validation successful. Preserve actual exit status
and completed scope. Broker tests are separately opt-in and require Mosquitto:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --run-broker `
  --broker-executable 'C:\Program Files\mosquitto\mosquitto.exe'
```
