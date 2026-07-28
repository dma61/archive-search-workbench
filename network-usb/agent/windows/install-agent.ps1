<#
.SYNOPSIS
    Installeert de ArchSW-loc agent (Windows) als achtergrond-service (scheduled task, SYSTEM).
.DESCRIPTION
    Lokale agent voor de server Archive Search Workbench. Draait bij de schijf en kan
    bestanden lokaal lezen (transparant) of een schijf via USB/IP delen.
    - Genereert eenmalig een token.
    - Kopieert de agent naar %ProgramData%\archsw-loc-agent\.
    - Registreert scheduled task 'ArchSW-loc-agent' die bij opstarten (en nu) als SYSTEM draait.
    - Opent firewall-poort (standaard 5060) voor het lokale subnet.
    - Meldt de agent aan bij de server-app (token + poort).
    Vereist ADMINISTRATOR. ASCII-only.
.PARAMETER Uninstall
    Verwijdert de scheduled task, firewall-regel en bestanden.
.EXAMPLE
    .\install-agent.ps1
    .\install-agent.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [int]$Port = 5060,
    [string]$ServerUrl = 'http://<server-ip>:5059',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ArchSW-loc-agent'
$InstallDir = Join-Path $env:ProgramData 'archsw-loc-agent'
$AgentDest = Join-Path $InstallDir 'archsw-loc-agent.ps1'
$TokenFile = Join-Path $InstallDir 'token.txt'
$FwName = "ArchSW-loc-agent $Port"
# Oude naam (voor nette migratie van eerdere installatie)
$OldTask = 'ArchiefAgent'
$OldDir = Join-Path $env:ProgramData 'archief-agent'
$OldFw = "Archief-agent $Port"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    Write-Error "Administrator-rechten vereist. Start PowerShell als Administrator."
    exit 1
}

function Stop-AgentProcesses {
    # Sluit lopende agent-processen af (oude en nieuwe naam) zodat poort 5060 vrijkomt.
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'archief-agent\.ps1|archsw-loc-agent\.ps1' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch {}
}

function Remove-Old {
    # Ruim een eventuele eerdere 'ArchiefAgent'-installatie op.
    try { Unregister-ScheduledTask -TaskName $OldTask -Confirm:$false -ErrorAction Stop } catch {}
    try { Remove-NetFirewallRule -DisplayName $OldFw -ErrorAction Stop } catch {}
    try { Remove-Item -Recurse -Force $OldDir -ErrorAction Stop } catch {}
    Stop-AgentProcesses
}

if ($Uninstall) {
    Write-Host "ArchSW-loc agent verwijderen..." -ForegroundColor Cyan
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}
    try { Remove-NetFirewallRule -DisplayName $FwName -ErrorAction Stop } catch {}
    try { Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop } catch {}
    Remove-Old
    Write-Host "Verwijderd." -ForegroundColor Green
    exit 0
}

Remove-Old  # migratie van oude naam

$usbipd = Get-Command usbipd -ErrorAction SilentlyContinue
if (-not $usbipd -and -not (Test-Path (Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'))) {
    Write-Warning "usbipd-win lijkt niet geinstalleerd. Draai install-usbipd.ps1 als je ook USB/IP-delen wilt (lokaal lezen werkt zonder)."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$srcAgent = Join-Path $PSScriptRoot 'archsw-loc-agent.ps1'
if (Test-Path $srcAgent) {
    Copy-Item -Force $srcAgent $AgentDest
} else {
    Write-Host "archsw-loc-agent.ps1 niet lokaal; downloaden van de server..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "$ServerUrl/netwerk-usb/dl/archsw-loc-agent.ps1" -OutFile $AgentDest
}

if (-not (Test-Path $TokenFile)) {
    $tok = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
    Set-Content -Path $TokenFile -Value $tok -Encoding ascii -NoNewline
    Write-Host "Nieuw token gegenereerd." -ForegroundColor Green
} else {
    Write-Host "Bestaand token hergebruikt." -ForegroundColor Yellow
}
$Token = (Get-Content -Raw $TokenFile).Trim()

$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$argLine = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$AgentDest`" -Port $Port"
$action = New-ScheduledTaskAction -Execute $psExe -Argument $argLine
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Scheduled task '$TaskName' geregistreerd en gestart." -ForegroundColor Green

try { Remove-NetFirewallRule -DisplayName $FwName -ErrorAction SilentlyContinue } catch {}
New-NetFirewallRule -DisplayName $FwName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -Profile Any -RemoteAddress LocalSubnet | Out-Null
Write-Host "Firewall-poort $Port geopend voor lokaal subnet." -ForegroundColor Green

Start-Sleep -Seconds 2
try {
    $h = Invoke-RestMethod -Uri "http://localhost:$Port/health" -TimeoutSec 5
    Write-Host "Agent draait: host=$($h.host) versie=$($h.agent_version)" -ForegroundColor Green
} catch {
    Write-Warning "Agent nog niet bereikbaar op localhost:$Port ($($_.Exception.Message))."
}

try {
    $reg = Invoke-RestMethod -Uri "$ServerUrl/api/remote/register-agent" -Method Post `
        -ContentType 'application/json' `
        -Body (@{ port = $Port; token = $Token } | ConvertTo-Json) -TimeoutSec 8
    if ($reg.success) { Write-Host "Aangemeld bij de app als host '$($reg.host)'." -ForegroundColor Green }
    else { Write-Warning "Aanmelden mislukt: $($reg.message)" }
} catch {
    Write-Warning "Kon niet aanmelden bij $ServerUrl ($($_.Exception.Message))."
    Write-Host "Token (voor handmatige registratie op de server):" -ForegroundColor Yellow
    Write-Host "  $Token"
}

Write-Host ""
Write-Host "Klaar. De app kan nu bestanden lokaal lezen en (indien nodig) schijven via USB/IP delen." -ForegroundColor Cyan
Write-Host "Verwijderen kan met:  .\install-agent.ps1 -Uninstall" -ForegroundColor Cyan
