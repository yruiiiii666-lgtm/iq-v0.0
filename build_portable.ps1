$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildRoot = Join-Path $projectRoot ".portable_build"
$depsRoot = Join-Path $buildRoot "deps"
$workRoot = Join-Path $buildRoot "work"
$distRoot = Join-Path $projectRoot "portable_dist"
$portableRoot = Join-Path $distRoot "IQAnalyzer_Portable"

New-Item -ItemType Directory -Path $depsRoot -Force | Out-Null
python -m pip install --upgrade --target $depsRoot pyvisa-py

$env:IQ_ANALYZER_PORTABLE_DEPS = $depsRoot
python -m PyInstaller `
    --noconfirm `
    --clean `
    --workpath $workRoot `
    --distpath $distRoot `
    (Join-Path $projectRoot "iq_analyzer_portable.spec")

Copy-Item (Join-Path $projectRoot "PORTABLE_README.txt") $portableRoot -Force
Copy-Item (Join-Path $projectRoot "场景地点_IQ关联表_更新版.csv") $portableRoot -Force
New-Item -ItemType Directory -Path (Join-Path $portableRoot "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $portableRoot "output") -Force | Out-Null
Copy-Item (Join-Path $projectRoot "DATA_README.txt") (Join-Path $portableRoot "data") -Force
Copy-Item (Join-Path $projectRoot "OUTPUT_README.txt") (Join-Path $portableRoot "output") -Force

Write-Host ""
Write-Host "Portable build created:"
Write-Host $portableRoot
