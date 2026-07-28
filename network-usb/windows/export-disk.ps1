<#
.SYNOPSIS
 Deelt (bind) een USB-schijf van deze Windows-machine via USB/IP, zodat de
 de server () hem kan importeren en read-only kan indexeren.
.DESCRIPTION
 Zonder parameters, of met -List: toont alle USB-apparaten met hun busid.
 Met -BusId <id>: bindt dat apparaat (usbipd bind), waarna het op de server
 verschijnt in het "Netwerk-USB"-paneel.
 Met -Unbind -BusId <id>: stopt het delen.

 'bind' vereist ADMINISTRATOR-rechten. 'list' niet.
.EXAMPLE
 .\export-disk.ps1 -List
 .\export-disk.ps1 -BusId 2-4
 .\export-disk.ps1 -Unbind -BusId 2-4
.NOTES
 Onderdeel van Archive Search Workbench - Netwerk-USB (USB/IP).
 Let op: zolang een schijf gedeeld (bound) is, is hij niet als gewone schijf
 in Windows beschikbaar. Voor read-only archiefschijven is dat prima.
#>
[CmdletBinding()]
param(
 [string]$BusId,
 [switch]$List,
 [switch]$Unbind
)

$ErrorActionPreference = 'Stop'

function Find-Usbipd {
 # PATH ververst niet altijd in de huidige sessie; check ook de vaste installlocatie.
 $cmd = Get-Command usbipd -ErrorAction SilentlyContinue
 if ($cmd) { return $cmd.Source }
 $fixed = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
 if (Test-Path $fixed) { return $fixed }
 return $null
}

function Test-Admin {
 $id = [Security.Principal.WindowsIdentity]::GetCurrent()
 (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
 [Security.Principal.WindowsBuiltInRole]::Administrator)
}

$USBIPD = Find-Usbipd
if (-not $USBIPD) {
 Write-Error "usbipd niet gevonden. Draai eerst install-usbipd.ps1 (als Administrator)."
 exit 1
}

if ($List -or (-not $BusId)) {
 Write-Host "Aangesloten USB-apparaten (busid = de kolom die je aan -BusId geeft):" -ForegroundColor Cyan
 & $USBIPD list
 Write-Host ""
 Write-Host "Deel een schijf met: .\export-disk.ps1 -BusId <busid>" -ForegroundColor Yellow
 exit 0
}

if (-not (Test-Admin)) {
 Write-Error "Binden/ontbinden vereist Administrator-rechten. Start PowerShell als Administrator."
 exit 1
}

if ($Unbind) {
 Write-Host "Stoppen met delen van busid $BusId..." -ForegroundColor Cyan
 & $USBIPD unbind --busid $BusId
 Write-Host "Klaar. De schijf is weer normaal in Windows beschikbaar." -ForegroundColor Green
 exit 0
}

Write-Host "Schijf met busid $BusId delen via USB/IP..." -ForegroundColor Cyan
& $USBIPD bind --busid $BusId
if ($LASTEXITCODE -ne 0) {
 Write-Error "Binden mislukt (usbipd exit $LASTEXITCODE). Controleer de busid via -List."
 exit 1
}
Write-Host ""
Write-Host "Gedeeld. Ga op de server naar het Netwerk-USB-paneel, kies deze machine" -ForegroundColor Green
Write-Host "en klik 'Koppel aan server'. Stoppen: .\export-disk.ps1 -Unbind -BusId $BusId" -ForegroundColor Green
