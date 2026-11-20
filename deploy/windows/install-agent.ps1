<#
.SYNOPSIS
  Start the ccmeter agent hidden at every Windows login (no administrator rights needed).

.DESCRIPTION
  Writes ~\.ccmeter\agent.json if it does not exist yet (needs -Server; ccmeter then asks for
  the server's token without echo, or reads it from $env:CCMETER_API_TOKEN), creates a small
  launcher in your Startup folder and starts the agent now. The agent keeps its state in
  ~\.ccmeter\state.json and its log in ~\.ccmeter\agent.log.

  Install ccmeter first:
    uv tool install "ccmeter @ git+https://github.com/ArzMat/ccmeter"

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-agent.ps1 -Server http://192.168.1.10:8787

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-agent.ps1 -Remove
#>
param(
  [string]$Server = "",
  [string]$HostLabel = "",
  [switch]$Remove
)
$ErrorActionPreference = "Stop"

$home_dir = Join-Path $env:USERPROFILE ".ccmeter"
$config = Join-Path $home_dir "agent.json"
$launcher = Join-Path $home_dir "run-agent.cmd"
$log = Join-Path $home_dir "agent.log"
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "ccmeter-agent.vbs"

function Stop-Agent {
  Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "ccmeter(\.exe)?""?\s+agent\s+run" } |
    ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }
}

if ($Remove) {
  Stop-Agent
  if (Test-Path $startup) { Remove-Item $startup -Force }
  Write-Host "ccmeter agent removed from startup (config and state kept in $home_dir)"
  exit 0
}

$ccmeter = (Get-Command ccmeter -ErrorAction SilentlyContinue).Source
if (-not $ccmeter) {
  throw 'ccmeter not found. Install it first: uv tool install "ccmeter @ git+https://github.com/ArzMat/ccmeter"'
}

if (-not (Test-Path $config)) {
  if (-not $Server) {
    throw "No $config yet: pass -Server (ccmeter then asks for the server's token)."
  }
  # The token is typed at the prompt or read from $env:CCMETER_API_TOKEN, never passed as an
  # argument: command lines are visible to every local user.
  $initArgs = @("agent", "init", "--server", $Server)
  if ($HostLabel) { $initArgs += @("--host-label", $HostLabel) }
  & $ccmeter @initArgs
  if ($LASTEXITCODE -ne 0) { throw "ccmeter agent init failed" }
} else {
  Write-Host "using the existing $config"
}

New-Item -ItemType Directory -Force $home_dir | Out-Null
@"
@echo off
"$ccmeter" agent run >> "$log" 2>&1
"@ | Set-Content -Path $launcher -Encoding ascii
('CreateObject("WScript.Shell").Run """' + $launcher + '""", 0, False') | Set-Content -Path $startup -Encoding ascii

Stop-Agent
Start-Process wscript.exe -ArgumentList "`"$startup`""
Write-Host "ccmeter agent started and registered at login ($startup)"
Write-Host "log: $log"
