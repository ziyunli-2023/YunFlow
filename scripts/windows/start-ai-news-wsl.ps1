param(
    [string]$DistroName = "Ubuntu",
    [string]$ServiceList = "ai-news ai-news-tunnel ai-news-tunnel-yunflow"
)

$wsl = Join-Path $env:SystemRoot "System32\wsl.exe"

if (-not (Test-Path $wsl)) {
    Write-Error "wsl.exe not found at $wsl"
    exit 1
}

$arguments = @(
    "-d", $DistroName,
    "-u", "root",
    "--",
    "bash", "-lc",
    "systemctl start $ServiceList && systemctl is-active $ServiceList"
)

$output = & $wsl @arguments 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Error ($output | Out-String)
    exit $exitCode
}

Write-Output ($output | Out-String)
