# Windows / PowerShell reproduction

Extract CIS-Team-updated.zip. Open the inner CIS-Team folder in VS Code and use
its PowerShell terminal. Python 3.14 is the project target; this delivery was
executed on Linux/Python 3.12, not natively on Windows. Existing dependencies and
Windows-specific code are retained. No Linux command is required below.

## Environment and inputs

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r .\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .\requirements-wegenernet.txt
```

Optional activation (explicit interpreter paths below work without activation):

```powershell
.\.venv\Scripts\Activate.ps1
```

Create a data folder and copy your actual NetCDF into it. It is not duplicated
inside the source-code ZIP. The supplied 30-cell list is already config/cells.txt.

```powershell
New-Item -ItemType Directory -Force .\data
Copy-Item "$HOME\Downloads\WN_L2_HD_v8_UTM_TF1_UTC_2024-01.nc" .\data\
$nc = (Resolve-Path .\data\WN_L2_HD_v8_UTM_TF1_UTC_2024-01.nc).Path
```

Adjust only the Copy-Item input path if the download is elsewhere. Do not rename
variables or change the actual cell IDs. Both UTF-16 and UTF-8 cell files work.

## Tests

```powershell
$env:WN_JANUARY_FILE = $nc
.\.venv\Scripts\python.exe -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
```

Without WN_JANUARY_FILE, the one actual-NetCDF preflight test explicitly skips.
Default broker tests skip. If Mosquitto is installed, run those tests explicitly:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

The supplied test logs identify which environment-dependent tests were skipped.
No Mosquitto service is required to process the NetCDF offline.

## Generate January

The output folder must not already exist. Use a new name for each run.

```powershell
.\.venv\Scripts\python.exe -m scripts.export_wegenernet $nc `
  --cells-file .\config\cells.txt `
  --start 2024-01-01T00:00:00Z `
  --end 2024-02-01T00:00:00Z `
  --warmup-hours 168 `
  --output .\output-january-30
if ($LASTEXITCODE -ne 0) { throw "January export failed; do not consume partial output" }
```

This performs real experimental signing and verification of central/neighbor
readings. It can take time; progress prints every 24 source hours. Memory is
bounded; temporary SQLite databases and final audit files need disk space.
There is no December download, synthetic fill or skipped January hour. Missing
December is recorded as 168 missing warm-up hours, with early features null.

## Inspect and independently validate

```powershell
Get-Content .\output-january-30\SUCCESS.json
Get-Content .\output-january-30\manifest.json
Get-ChildItem .\output-january-30 | Select-Object Name, Length
.\.venv\Scripts\python.exe -m scripts.validate_wegenernet_export .\output-january-30 $nc
if ($LASTEXITCODE -ne 0) { throw "Independent source validation failed" }
.\.venv\Scripts\python.exe -c "import pandas as pd; d=pd.read_csv('output-january-30/compact.csv.gz'); print(d.shape); print(d[['timestamp','cell_id','T_zAvgTerr_HM__latest','T_zAvgTerr_HM__std_6h']].head())"
.\.venv\Scripts\python.exe -c "import gzip; from core.contracts import FeatureWindow; f=gzip.open('output-january-30/windows.jsonl.gz','rt',encoding='utf-8'); w=FeatureWindow.model_validate_json(next(f)); print(w.window_id, w.start, len(w.readings))"
```

CSV missing numeric fields become NaN in pandas. Raw JSON uses null. Never fill
missing values with zero without an explicit agreed ML policy.

To obtain plain JSONL for an existing reader:

```powershell
.\.venv\Scripts\python.exe -c "import gzip,shutil; a=gzip.open('output-january-30/windows.jsonl.gz','rb'); b=open('output-january-30/windows.jsonl','wb'); shutil.copyfileobj(a,b); a.close(); b.close()"
```

## Later: additional real files and warm-up

After obtaining December and the next months, place them in data. The command
checks actual timestamps and fails if any target source interval is missing.
Do not run the following expecting it to invent unavailable months.

Example January–February, requiring February and optionally December warm-up:

```powershell
$files = @(Get-ChildItem .\data\WN_L2_HD_v8_UTM_TF1_UTC_*.nc | Select-Object -ExpandProperty FullName)
.\.venv\Scripts\python.exe -m scripts.export_wegenernet @files `
  --cells-file .\config\cells.txt `
  --start 2024-01-01T00:00:00Z --end 2024-03-01T00:00:00Z `
  --warmup-hours 168 --output .\output-jan-feb-30
```

For the full leap year, only after all twelve files exist:

```powershell
.\.venv\Scripts\python.exe -m scripts.export_wegenernet @files `
  --cells-file .\config\cells.txt `
  --start 2024-01-01T00:00:00Z --end 2025-01-01T00:00:00Z `
  --warmup-hours 168 --output .\output-2024-30
```

Expected future target is 8,784 hours × 30 = 263,520 cell-hours. This has NOT been
validated against real full-year data. Seven preceding days are read when
supplied, but not exported as target ML rows. Sources are sorted by decoded time.
Use the independent validator with the same file list after generation.
