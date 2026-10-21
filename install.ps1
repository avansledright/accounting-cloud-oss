# Accounting Cloud installer for Windows (Windows PowerShell 5.1 or PowerShell 7).
#
#   irm https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.ps1 | iex
#
# Runs the app with Docker Desktop: it downloads the release's compose file,
# writes a .env with a random SECRET_KEY and your time zone, pulls the images,
# starts them, adds Start menu and desktop shortcuts and opens the browser.
# Running it again updates to the latest release and keeps your .env and data.
#
# Options (set before running, e.g.  $env:WEB_PORT = "5060"):
#   ACCOUNTING_CLOUD_DIR      install folder         (default %USERPROFILE%\accounting-cloud)
#   ACCOUNTING_CLOUD_VERSION  release to install, e.g. 0.3.0 (default: latest)
#   WEB_PORT                  port on this computer  (default 5052, first install only)
#   NO_BROWSER=1              don't open the browser when done

function Install-AccountingCloud {
    $Repo = 'avansledright/accounting-cloud-oss'
    $Project = 'accountingcloud'   # Docker Compose project name: prefixes containers and data volumes

    function Say([string]$Text) { Write-Host $Text }
    function Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
    function Fail([string]$Text) { Write-Host ''; Write-Host "Error: $Text" -ForegroundColor Red }

    # Fetch a release file. ACCOUNTING_CLOUD_BASE_URL may be a local folder (testing).
    function Get-ReleaseFile([string]$Base, [string]$Name, [string]$Dest) {
        if ($Base -match '^(https?)://') {
            Invoke-WebRequest -UseBasicParsing -Uri "$Base/$Name" -OutFile $Dest -ErrorAction Stop
        } else {
            Copy-Item -LiteralPath (Join-Path ($Base -replace '^file://', '') $Name) -Destination $Dest -ErrorAction Stop
        }
    }

    function New-Secret {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $rng.Dispose()
        -join ($bytes | ForEach-Object { $_.ToString('x2') })
    }

    # Windows time zone -> IANA name for the container's TZ.
    function Get-IanaTimeZone {
        $id = [TimeZoneInfo]::Local.Id
        $iana = $null
        $convert = [TimeZoneInfo].GetMethod('TryConvertWindowsIdToIanaId', [type[]]@([string], [string].MakeByRefType()))
        if ($convert) {   # .NET 6+ (PowerShell 7)
            $out = $null
            if ([TimeZoneInfo]::TryConvertWindowsIdToIanaId($id, [ref]$out)) { $iana = $out }
        }
        if (-not $iana) {
            $map = @{
                'Eastern Standard Time' = 'America/New_York'; 'Central Standard Time' = 'America/Chicago'
                'Mountain Standard Time' = 'America/Denver'; 'US Mountain Standard Time' = 'America/Phoenix'
                'Pacific Standard Time' = 'America/Los_Angeles'; 'Alaskan Standard Time' = 'America/Anchorage'
                'Hawaiian Standard Time' = 'Pacific/Honolulu'; 'Atlantic Standard Time' = 'America/Halifax'
                'Newfoundland Standard Time' = 'America/St_Johns'; 'Canada Central Standard Time' = 'America/Regina'
                'US Eastern Standard Time' = 'America/Indiana/Indianapolis'
                'Central Standard Time (Mexico)' = 'America/Mexico_City'; 'E. South America Standard Time' = 'America/Sao_Paulo'
                'GMT Standard Time' = 'Europe/London'; 'W. Europe Standard Time' = 'Europe/Berlin'
                'Romance Standard Time' = 'Europe/Paris'; 'Central Europe Standard Time' = 'Europe/Budapest'
                'Central European Standard Time' = 'Europe/Warsaw'; 'E. Europe Standard Time' = 'Europe/Chisinau'
                'FLE Standard Time' = 'Europe/Kiev'; 'GTB Standard Time' = 'Europe/Bucharest'
                'India Standard Time' = 'Asia/Kolkata'; 'China Standard Time' = 'Asia/Shanghai'
                'Tokyo Standard Time' = 'Asia/Tokyo'; 'Singapore Standard Time' = 'Asia/Singapore'
                'AUS Eastern Standard Time' = 'Australia/Sydney'; 'New Zealand Standard Time' = 'Pacific/Auckland'
                'UTC' = 'UTC'
            }
            $iana = $map[$id]
        }
        if (-not $iana) {
            Say "Couldn't map the Windows time zone '$id'; using UTC. Set TZ in .env (e.g. TZ=America/New_York)."
            $iana = 'UTC'
        }
        $iana
    }

    # UTF-8 without a BOM and with LF line endings: Docker Compose reads it.
    function Write-PlainFile([string]$Path, [string]$Text) {
        [IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    }

    $dir = if ($env:ACCOUNTING_CLOUD_DIR) { $env:ACCOUNTING_CLOUD_DIR } else { Join-Path $HOME 'accounting-cloud' }
    $version = if ($env:ACCOUNTING_CLOUD_VERSION) { $env:ACCOUNTING_CLOUD_VERSION.TrimStart('v') } else { '' }
    if ($env:ACCOUNTING_CLOUD_BASE_URL) { $base = $env:ACCOUNTING_CLOUD_BASE_URL }
    elseif ($version) { $base = "https://github.com/$Repo/releases/download/v$version" }
    else { $base = "https://github.com/$Repo/releases/latest/download" }

    # Windows PowerShell 5.1 may default to TLS 1.0, which GitHub refuses.
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

    Say 'Accounting Cloud installer'

    Step 'Checking Docker'
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Say 'Docker Desktop is not installed. Install it, start it once, then run this installer again:'
        Say '  https://docs.docker.com/desktop/setup/install/windows-install/'
        return
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Say 'Docker Desktop is installed but not running. Start it from the Start menu, wait until'
        Say 'it says "Engine running", then run this installer again.'
        return
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose is missing. Update Docker Desktop and try again.'; return }
    Say 'OK'

    $envFile = Join-Path $dir '.env'
    # A first install must not silently attach to data volumes that already
    # exist under this project name: another copy, an old test.
    if (-not (Test-Path $envFile)) {
        $existing = @(docker volume ls -q --filter "label=com.docker.compose.project=$Project")
        if ($existing.Count -gt 0 -and $env:ACCOUNTING_CLOUD_REUSE_DATA -ne '1') {
            Say ''
            Say 'Docker already has Accounting Cloud data from another install:'
            $existing | ForEach-Object { Say "  $_" }
            Say 'Nothing was changed. To use that data, run  $env:ACCOUNTING_CLOUD_REUSE_DATA = "1"'
            Say "and then the installer again. To start fresh instead, remove those volumes with"
            Say "'docker volume rm' (this permanently deletes the data)."
            return
        }
    }

    Step "Downloading release files to $dir"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $tmp = Join-Path $dir '.docker-compose.yml.download'
    try { Get-ReleaseFile $base 'docker-compose.yml' $tmp }
    catch { Fail "could not download $base/docker-compose.yml ($($_.Exception.Message))"; return }
    Move-Item -Force $tmp (Join-Path $dir 'docker-compose.yml')
    Write-PlainFile (Join-Path $dir 'accounting-cloud-cli.ps1') $HelperScript
    Write-PlainFile (Join-Path $dir 'accounting-cloud.cmd') "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0accounting-cloud-cli.ps1`" %*`r`n"
    Say 'OK'

    Step 'Settings'
    if (Test-Path $envFile) {
        Say "Keeping existing $envFile"
    } else {
        $port = if ($env:WEB_PORT) { $env:WEB_PORT } else { '5052' }
        $tz = Get-IanaTimeZone
        Write-PlainFile $envFile @"
# Accounting Cloud settings. Full list: https://github.com/$Repo/blob/main/.env.example
# Restart after editing:  .\accounting-cloud start
COMPOSE_PROJECT_NAME=$Project
SECRET_KEY=$(New-Secret)
TZ=$tz
WEB_PORT=$port

"@
        Say "Created $envFile (time zone $tz, port $port)"
    }
    $port = '5052'
    foreach ($line in Get-Content $envFile) { if ($line -match '^WEB_PORT=(.+)$') { $port = $Matches[1].Trim() } }
    $url = "http://localhost:$port"

    Step 'Downloading and starting (the first time takes a minute or two)'
    Push-Location $dir
    try {
        if ($env:ACCOUNTING_CLOUD_SKIP_PULL -ne '1') {   # testing: use locally built images
            docker compose pull --quiet
            if ($LASTEXITCODE -ne 0) { Fail 'could not download the images (see above).'; return }
        }
        docker compose up -d --wait --remove-orphans
        if ($LASTEXITCODE -ne 0) { Fail "the app did not start. See the logs with: $dir\accounting-cloud logs"; return }
    } finally { Pop-Location }

    # Start menu and desktop shortcuts: start the app if needed, open the browser.
    try {
        $shell = New-Object -ComObject WScript.Shell
        $targets = @(
            (Join-Path ([Environment]::GetFolderPath('Programs')) 'Accounting Cloud.lnk'),
            (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Accounting Cloud.lnk')
        )
        foreach ($lnk in $targets) {
            $s = $shell.CreateShortcut($lnk)
            $s.TargetPath = 'powershell.exe'
            $s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\accounting-cloud-cli.ps1`" launch"
            $s.WorkingDirectory = $dir
            $s.IconLocation = "$env:SystemRoot\System32\imageres.dll,1"
            $s.Description = 'Accounting Cloud'
            $s.Save()
        }
    } catch { Say "(Couldn't create shortcuts: $($_.Exception.Message))" }

    Say ''
    Say "Accounting Cloud is running at $url"
    Say ''
    Say "Open it any time from the 'Accounting Cloud' shortcut on your desktop or Start menu."
    Say "Manage it with $dir\accounting-cloud"
    Say '  start | stop | status | logs | open | update | backup | uninstall'
    Say ''
    Say 'It starts again by itself whenever Docker Desktop starts. It has no login, so'
    Say 'it only listens on this computer; see docs/deployment.md before exposing it.'
    if ($env:NO_BROWSER -ne '1') { Start-Process $url }
}

# Written to accounting-cloud-cli.ps1 in the install folder, next to accounting-cloud.cmd,
# which runs it with -ExecutionPolicy Bypass (Windows blocks .ps1 files by default).
$HelperScript = @'
# Manage this Accounting Cloud install:  .\accounting-cloud <command>
param([string]$Command = '', [string]$Option = '')
Set-Location $PSScriptRoot
$port = '5052'
foreach ($line in Get-Content .env) { if ($line -match '^WEB_PORT=(.+)$') { $port = $Matches[1].Trim() } }
$url = "http://localhost:$port"
switch ($Command) {
    'start'  { docker compose up -d --wait; if ($LASTEXITCODE -eq 0) { "Running at $url" } }
    'stop'   { docker compose stop }
    'status' { docker compose ps }
    'logs'   { docker compose logs --tail 200 -f }
    'open'   { Start-Process $url }
    'launch' { docker compose up -d --wait *> $null; Start-Process $url }
    'update' {
        $env:ACCOUNTING_CLOUD_DIR = $PSScriptRoot; $env:NO_BROWSER = '1'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod 'https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.ps1' | Invoke-Expression
    }
    'backup' {
        # Copy the archive out of the container: piping binary output through
        # Windows PowerShell would corrupt it.
        $name = "backup-$(Get-Date -Format 'yyyy-MM-dd_HHmmss').archive.gz"
        docker compose exec -T db mongodump --archive=/tmp/backup.archive.gz --gzip
        if ($LASTEXITCODE -eq 0) {
            docker compose cp "db:/tmp/backup.archive.gz" ".\$name"
            docker compose exec -T db rm -f /tmp/backup.archive.gz
            "Saved $PSScriptRoot\$name"
        }
    }
    'uninstall' {
        'This stops Accounting Cloud and removes its containers.'
        if ($Option -eq '--delete-data') {
            docker compose down --volumes
            'Containers and data deleted.'
        } else {
            docker compose down
            'Containers removed. Your data is kept in Docker volumes;'
            'run  .\accounting-cloud uninstall --delete-data  to delete it too.'
        }
        foreach ($folder in 'Programs', 'Desktop') {
            $path = [Environment]::GetFolderPath($folder)
            if ($path) { Remove-Item -ErrorAction SilentlyContinue (Join-Path $path 'Accounting Cloud.lnk') }
        }
        "You can now delete this folder: $PSScriptRoot"
    }
    default  { 'Usage: .\accounting-cloud start|stop|status|logs|open|launch|update|backup|uninstall [--delete-data]' }
}
'@

Install-AccountingCloud
