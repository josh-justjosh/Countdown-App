# Build standalone Windows x64 zip
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install -q -r requirements.txt -r requirements-build.txt
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }

python -m PyInstaller --noconfirm CountdownApp.spec

$OutDir = Join-Path $Root "dist\VT Vocal Countdown"
$Zip = Join-Path $Root "dist\VT-Vocal-Countdown-Windows-x64.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path $OutDir -DestinationPath $Zip
Write-Host "Built $Zip"
Get-Item $Zip | Format-List Name, Length
