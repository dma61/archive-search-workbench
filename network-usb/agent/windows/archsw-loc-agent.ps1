<#
.SYNOPSIS
    ArchSW-loc agent (Windows) - lokale agent voor de server Archive Search Workbench.
.DESCRIPTION
    Kleine achtergrond-service op de machine WAAR DE SCHIJF HANGT. Twee taken:
      1. LOKAAL LEZEN (transparant): is de schijf hier in Windows gemount, dan leest de
         agent het gevraagde bestand rechtstreeks van die schijf en geeft het aan de app.
         De schijf blijft gewoon in Windows staan (geen USB/IP-overname).
      2. DELEN via USB/IP (alleen als de server blok-toegang nodig heeft, bv. indexeren of
         een schijf die aan een andere machine hangt): usbipd bind/unbind.

    Endpoints (JSON, token via header X-Agent-Token; /health mag zonder):
      GET  /health                          -> status
      GET  /disks                           -> deelbare USB-apparaten + schijf-identiteit
      GET  /read-file?volume=&path=         -> stream een bestand van de lokaal gemounte schijf
      POST /bind   {busid}                  -> usbipd bind
      POST /unbind {busid}                  -> usbipd unbind

    Draai als SYSTEM (scheduled task). ASCII-only (PS 5.1 leest .ps1 zonder BOM als ANSI).
#>
[CmdletBinding()]
param(
    [int]$Port = 5060,
    [string]$TokenFile = "$env:ProgramData\archsw-loc-agent\token.txt"
)

$ErrorActionPreference = 'Stop'
$AgentVersion = '0.3.0'
$MaxReadBytes = 300MB

if (-not (Test-Path $TokenFile)) {
    Write-Error "Tokenbestand niet gevonden: $TokenFile. Draai install-agent.ps1 eerst."
    exit 1
}
$Token = (Get-Content -Raw $TokenFile).Trim()
if (-not $Token) { Write-Error "Leeg token in $TokenFile."; exit 1 }

function Find-Usbipd {
    $cmd = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fixed = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
    if (Test-Path $fixed) { return $fixed }
    return $null
}

function Get-BindableDevices {
    $usbipd = Find-Usbipd
    if (-not $usbipd) { return @() }
    $raw = & $usbipd list 2>&1
    $devices = @()
    $inConnected = $false
    foreach ($line in $raw) {
        $t = "$line".TrimEnd()
        if ($t -match '^Connected:') { $inConnected = $true; continue }
        if ($t -match '^Persisted:') { $inConnected = $false; continue }
        if (-not $inConnected) { continue }
        if ($t -match '^\s*BUSID') { continue }
        if ([string]::IsNullOrWhiteSpace($t)) { continue }
        $m = [regex]::Match($t, '^\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*?)\s{2,}(Not shared|Shared|Shared \(forced\)|Attached)\s*$')
        if ($m.Success) {
            $desc = $m.Groups[3].Value.Trim()
            $likely = ($desc -match 'Mass Storage|Disk|UAS|SCSI|Storage')
            $devices += [ordered]@{
                busid = $m.Groups[1].Value; vidpid = $m.Groups[2].Value
                device = $desc; state = $m.Groups[4].Value; likely_disk = [bool]$likely
            }
        }
    }
    return $devices
}

function Get-VolumeSerial([string]$letter) {
    # Volume-serienummer in het formaat dat de Linux-scan (blkid) ook geeft, zodat de
    # server op filesystem_uuid kan matchen. NTFS: 64-bit via fsutil; exFAT/FAT: 32-bit via vol.
    if (-not $letter) { return '' }
    $drive = ($letter + ':')
    try {
        $nt = & fsutil fsinfo ntfsinfo $drive 2>$null
        foreach ($line in $nt) {
            if ($line -match 'Serial Number\s*:\s*0x([0-9A-Fa-f]+)') { return $Matches[1].ToUpper() }
        }
    } catch {}
    try {
        $vl = & cmd /c "vol $drive" 2>$null
        foreach ($line in $vl) {
            if ($line -match '([0-9A-Fa-f]{4}-[0-9A-Fa-f]{4})') { return $Matches[1].ToUpper() }
        }
    } catch {}
    return ''
}

function Get-UsbDiskIdentity {
    $out = @()
    try {
        $disks = Get-Disk -ErrorAction SilentlyContinue | Where-Object { $_.BusType -eq 'USB' }
        foreach ($d in $disks) {
            $vols = @()
            try {
                foreach ($p in (Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)) {
                    $v = $null
                    try { $v = Get-Volume -Partition $p -ErrorAction SilentlyContinue } catch {}
                    if ($v) {
                        $vols += [ordered]@{
                            letter = "$($v.DriveLetter)"; label = "$($v.FileSystemLabel)"
                            fs = "$($v.FileSystem)"; sizeGB = [math]::Round(($v.Size)/1GB,1)
                            uuid = (Get-VolumeSerial "$($v.DriveLetter)")
                        }
                    }
                }
            } catch {}
            $out += [ordered]@{
                number = $d.Number; serial = "$($d.SerialNumber)".Trim()
                model = "$($d.FriendlyName)".Trim(); sizeGB = [math]::Round(($d.Size)/1GB,1)
                volumes = @($vols)
            }
        }
    } catch {}
    return $out
}

function Find-DriveByLabel([string]$volume) {
    if (-not $volume) { return $null }
    $v = Get-Volume -ErrorAction SilentlyContinue |
         Where-Object { $_.FileSystemLabel -eq $volume -and $_.DriveLetter } |
         Select-Object -First 1
    if ($v) { return "$($v.DriveLetter)" }
    return $null
}

$ContentTypes = @{
    '.pdf'='application/pdf'; '.txt'='text/plain; charset=utf-8'; '.md'='text/plain; charset=utf-8'
    '.csv'='text/csv'; '.json'='application/json'; '.xml'='application/xml'; '.html'='text/html'
    '.jpg'='image/jpeg'; '.jpeg'='image/jpeg'; '.png'='image/png'; '.gif'='image/gif'
    '.docx'='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    '.xlsx'='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    '.doc'='application/msword'; '.xls'='application/vnd.ms-excel'
}

function Invoke-Usbipd([string]$verb, [string]$busid) {
    $usbipd = Find-Usbipd
    if (-not $usbipd) { return @{ ok=$false; message='usbipd niet gevonden' } }
    if ($busid -notmatch '^\d+-\d+$') { return @{ ok=$false; message="ongeldige busid: $busid" } }
    $res = & $usbipd $verb --busid $busid 2>&1
    if ($LASTEXITCODE -eq 0) { return @{ ok=$true; message="$verb $busid ok"; output="$res" } }
    return @{ ok=$false; message="usbipd $verb mislukt (exit $LASTEXITCODE): $res" }
}

function Write-Json($ctx, $obj, [int]$status = 200) {
    $json = $obj | ConvertTo-Json -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $ctx.Response.StatusCode = $status
    $ctx.Response.ContentType = 'application/json; charset=utf-8'
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

function Write-FileBytes($ctx, [string]$fullPath) {
    $item = Get-Item -LiteralPath $fullPath
    if ($item.Length -gt $MaxReadBytes) {
        Write-Json $ctx @{ ok=$false; error="bestand te groot ($([math]::Round($item.Length/1MB)) MB)" } 413
        return
    }
    $bytes = [IO.File]::ReadAllBytes($fullPath)
    $ext = [IO.Path]::GetExtension($fullPath).ToLower()
    $ct = $ContentTypes[$ext]; if (-not $ct) { $ct = 'application/octet-stream' }
    $ctx.Response.StatusCode = 200
    $ctx.Response.ContentType = $ct
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

$listener = New-Object Net.HttpListener
$listener.Prefixes.Add("http://+:$Port/")
try { $listener.Start() } catch {
    Write-Error "Kan niet luisteren op poort $Port (admin/urlacl nodig?): $($_.Exception.Message)"; exit 1
}
Write-Host "ArchSW-loc agent v$AgentVersion luistert op poort $Port" -ForegroundColor Green

while ($listener.IsListening) {
    $ctx = $null
    try {
        $ctx = $listener.GetContext()
        $req = $ctx.Request
        $path = $req.Url.AbsolutePath.TrimEnd('/'); if ($path -eq '') { $path = '/' }

        if ($path -ne '/health' -and $req.Headers['X-Agent-Token'] -ne $Token) {
            Write-Json $ctx @{ ok=$false; error='ongeldig of ontbrekend token' } 401; continue
        }

        $body = @{}
        if ($req.HasEntityBody) {
            $reader = New-Object IO.StreamReader($req.InputStream, $req.ContentEncoding)
            $raw = $reader.ReadToEnd(); $reader.Close()
            if ($raw) { try { $body = $raw | ConvertFrom-Json } catch { $body = @{} } }
        }

        switch ("$($req.HttpMethod) $path") {
            'GET /health' {
                Write-Json $ctx @{ ok=$true; host=$env:COMPUTERNAME; os='windows'; agent_version=$AgentVersion }
            }
            'GET /disks' {
                Write-Json $ctx @{ ok=$true; host=$env:COMPUTERNAME
                    bindable=@(Get-BindableDevices); usbdisks=@(Get-UsbDiskIdentity) }
            }
            'GET /read-file' {
                $volume = $req.QueryString['volume']
                $rel = $req.QueryString['path']
                if (-not $rel) { Write-Json $ctx @{ ok=$false; error='path vereist' } 400; continue }
                $drive = Find-DriveByLabel $volume
                if (-not $drive) {
                    Write-Json $ctx @{ ok=$false; reason='not_here'; error="schijf '$volume' niet in Windows gemount" } 404; continue
                }
                $base = "$drive`:\"
                $relWin = ($rel -replace '/', '\').TrimStart('\')
                $full = [IO.Path]::GetFullPath((Join-Path $base $relWin))
                if (-not $full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
                    Write-Json $ctx @{ ok=$false; error='ongeldig pad' } 403; continue
                }
                if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
                    Write-Json $ctx @{ ok=$false; reason='not_found'; error='bestand niet gevonden op de schijf' } 404; continue
                }
                Write-FileBytes $ctx $full
            }
            'POST /bind'   { Write-Json $ctx (Invoke-Usbipd 'bind'   "$($body.busid)") }
            'POST /unbind' { Write-Json $ctx (Invoke-Usbipd 'unbind' "$($body.busid)") }
            default        { Write-Json $ctx @{ ok=$false; error="onbekend: $path" } 404 }
        }
    } catch {
        try { if ($ctx) { Write-Json $ctx @{ ok=$false; error="$($_.Exception.Message)" } 500 } } catch {}
    }
}
