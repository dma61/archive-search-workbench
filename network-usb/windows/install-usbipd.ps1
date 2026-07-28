<#
.SYNOPSIS
 Installeert usbipd-win op deze Windows-machine (de "exporter").
.DESCRIPTION
 usbipd-win exporteert een USB-apparaat over het netwerk zodat de server ()
 het via 'usbip attach' als lokale schijf kan importeren.
 Probeert winget (id: dorssel.usbipd-win); valt terug op de MSI van GitHub.
 Vereist ADMINISTRATOR-rechten. Verifieert de installatie en meldt eerlijk succes/falen.
.NOTES
 Onderdeel van Archive Search Workbench - Netwerk-USB (USB/IP).
 Project: https://github.com/dorssel/usbipd-win (open source, Microsoft-aanbevolen voor WSL)
#>
[CmdletBinding()]
param(
 [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Test-Admin {
 $id = [Security.Principal.WindowsIdentity]::GetCurrent()
 (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
 [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Usbipd {
 # PATH ververst niet altijd in de huidige sessie; check ook de vaste installlocatie.
 $cmd = Get-Command usbipd -ErrorAction SilentlyContinue
 if ($cmd) { return $cmd.Source }
 $fixed = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
 if (Test-Path $fixed) { return $fixed }
 return $null
}

if (-not (Test-Admin)) {
 Write-Error "Dit script vereist Administrator-rechten. Start PowerShell als Administrator en draai opnieuw."
 exit 1
}

# Al geinstalleerd?
$existing = Find-Usbipd
if ($existing -and -not $Force) {
 Write-Host "usbipd is al aanwezig: $existing" -ForegroundColor Green
 & $existing --version
 Write-Host "Klaar. Gebruik export-disk.ps1 om een schijf te delen." -ForegroundColor Cyan
 exit 0
}

$installed = $false

# --- Poging 1: winget (juiste package-id) ---
$winget = Get-Command winget -ErrorAction SilentlyContinue
if ($winget) {
 Write-Host "usbipd-win installeren via winget (dorssel.usbipd-win)..." -ForegroundColor Cyan
 winget install --exact --id dorssel.usbipd-win --accept-source-agreements --accept-package-agreements
 if ($LASTEXITCODE -eq 0 -and (Find-Usbipd)) {
 $installed = $true
 } else {
 Write-Warning "winget-installatie lukte niet (exit $LASTEXITCODE). Ik val terug op de MSI van GitHub."
 }
} else {
 Write-Warning "winget niet gevonden - ik gebruik de MSI van GitHub."
}

# --- Poging 2: MSI van de nieuwste GitHub-release ---
if (-not $installed) {
 try {
 Write-Host "Nieuwste usbipd-win MSI opzoeken via GitHub API..." -ForegroundColor Cyan
 [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
 $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/dorssel/usbipd-win/releases/latest' `
 -Headers @{ 'User-Agent' = 'archive-workbench-setup' }
 $asset = $rel.assets | Where-Object { $_.name -like '*.msi' } | Select-Object -First 1
 if (-not $asset) { throw "Geen .msi-asset gevonden in de laatste release." }
 $msi = Join-Path $env:TEMP $asset.name
 $sizeMB = [math]::Round($asset.size / 1MB, 1)
 Write-Host "Downloaden: $($asset.name) ($sizeMB MB)..." -ForegroundColor Cyan
 Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msi
 Write-Host "Installeren (stil)..." -ForegroundColor Cyan
 $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait -PassThru
 if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
 throw "msiexec gaf exitcode $($p.ExitCode)."
 }
 if (Find-Usbipd) { $installed = $true }
 } catch {
 Write-Error "MSI-installatie mislukt: $($_.Exception.Message)"
 }
}

# --- Verificatie (geen silent success) ---
$path = Find-Usbipd
if ($installed -and $path) {
 Write-Host ""
 Write-Host "OK - usbipd-win geinstalleerd: $path" -ForegroundColor Green
 & $path --version
 Write-Host "usbipd-win opent firewall-poort 3240 (TCP) voor het lokale subnet." -ForegroundColor Green
 Write-Host "LET OP: open zo nodig een NIEUWE PowerShell zodat 'usbipd' in PATH staat." -ForegroundColor Yellow
 Write-Host "Volgende stap: .\export-disk.ps1 -List en .\export-disk.ps1 -BusId <id>" -ForegroundColor Cyan
 exit 0
} else {
 Write-Error "Installatie NIET gelukt. Installeer handmatig de nieuwste MSI van https://github.com/dorssel/usbipd-win/releases en draai daarna export-disk.ps1."
 exit 1
}
