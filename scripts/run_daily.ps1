# Entry point for Windows Task Scheduler.
# Makes sure the database container is up, then runs discover + work.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Docker Desktop may not be running yet after a reboot.
$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $dockerExe) { Start-Process $dockerExe }
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 5
        docker info *> $null
    } until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)
    if ($LASTEXITCODE -ne 0) { throw "Docker did not start within 3 minutes" }
}

docker compose up -d --wait
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

$python = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
& $python -m foxcomments daily
exit $LASTEXITCODE
