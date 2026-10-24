[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# These pinned artifacts are published by the official Lavalink and youtube-source projects.
# Update versions only after checking the upstream release notes and compatibility.
$LavalinkVersion = "4.2.2"
$LavalinkUrl = "https://github.com/lavalink-devs/Lavalink/releases/download/4.2.2/Lavalink.jar"
$LavalinkSha256 = "8cb801e591072c3689fafd71ccf571a95a4ead3cc35dfc045e157d763d89119a"
$YouTubePluginVersion = "1.18.2"
$YouTubePluginUrl = "https://maven.lavalink.dev/releases/dev/lavalink/youtube/youtube-plugin/1.18.2/youtube-plugin-1.18.2.jar"
$YouTubePluginSha256 = "fc1128b606b54fda124b78d261c3878198116028baf00f7bd014f44bab95d77d"

function Get-JavaMajorVersion {
    $java = Get-Command java -ErrorAction SilentlyContinue
    if ($null -eq $java) {
        throw "Java 17 or newer is required. Install a 64-bit JDK, reopen PowerShell, and run this script again."
    }

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Java reports its version on stderr; in Windows PowerShell that is an
        # ErrorRecord unless the native command is captured with Continue.
        $ErrorActionPreference = "Continue"
        $versionOutput = (& $java.Source -version 2>&1 | ForEach-Object { $_.ToString() }) -join "`n"
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($versionOutput -notmatch '(?i)(?:openjdk|java) version\s+"?(?<major>\d+)') {
        throw "Could not read the Java version. Confirm that 'java -version' works, then retry."
    }

    $major = [int]$Matches.major
    if ($major -lt 17) {
        throw "Java $major was found, but Lavalink 4 requires Java 17 or newer. Install Java 17 or 21 and retry."
    }
    return $major
}

function Test-ValidJar {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet("Lavalink", "YouTube")][string]$Kind
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        if ($Kind -eq "Lavalink") {
            $entry = $archive.GetEntry("META-INF/MANIFEST.MF")
            if ($null -eq $entry) { return $false }
            $reader = [System.IO.StreamReader]::new($entry.Open())
            try { $manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
            return $manifest -match "(?m)^Implementation-Version:\s*$LavalinkVersion\s*$"
        }

        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith("dev/lavalink/youtube/plugin/YoutubePluginLoader.class")) {
                return $true
            }
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $archive) { $archive.Dispose() }
    }
}

function Install-VerifiedJar {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Sha256,
        [Parameter(Mandatory = $true)][ValidateSet("Lavalink", "YouTube")][string]$Kind
    )

    if (Test-Path -LiteralPath $Destination) {
        $existingHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existingHash -eq $Sha256 -and (Test-ValidJar -Path $Destination -Kind $Kind)) {
            Write-Host "$Kind JAR is already installed and verified."
        }
        else {
            Write-Warning "$Destination already exists and does not match the pinned artifact. It was left unchanged; back it up and move it yourself before rerunning setup."
        }
        return
    }

    $temporaryPath = Join-Path ([System.IO.Path]::GetDirectoryName($Destination)) ("." + [guid]::NewGuid().ToString("N") + ".download")
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $temporaryPath -UseBasicParsing
        $downloadHash = (Get-FileHash -LiteralPath $temporaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($downloadHash -ne $Sha256) {
            throw "$Kind download failed its SHA-256 check. The temporary download will be removed."
        }
        if (-not (Test-ValidJar -Path $temporaryPath -Kind $Kind)) {
            throw "$Kind download is not a valid expected JAR. The temporary download will be removed."
        }

        # File.Move fails if another file appeared after the initial check; it never overwrites.
        [System.IO.File]::Move($temporaryPath, $Destination)
        if ($Kind -eq "Lavalink") { $installedVersion = $LavalinkVersion }
        else { $installedVersion = $YouTubePluginVersion }
        Write-Host "$Kind $installedVersion downloaded and verified."
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath
        }
    }
}

function Copy-TemplateIfMissing {
    param(
        [Parameter(Mandatory = $true)][string]$Template,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $Destination) {
        Write-Host "Existing $Label was preserved."
        return
    }
    if (-not (Test-Path -LiteralPath $Template)) {
        throw "Required template is missing: $Template"
    }
    Copy-Item -LiteralPath $Template -Destination $Destination
    Write-Host "Created $Label from its example. Review it before starting Miku."
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*?)\s*$')) {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

function Get-LavalinkPassword {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $insideLavalink = $false
    $insideLavalinkServer = $false
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^lavalink:\s*(?:#.*)?$') { $insideLavalink = $true; continue }
        if ($insideLavalink -and $line -match '^\S') {
            $insideLavalink = $false
            $insideLavalinkServer = $false
        }
        if ($insideLavalink -and $line -match '^\s{2,}server:\s*(?:#.*)?$') {
            $insideLavalinkServer = $true
            continue
        }
        if ($insideLavalinkServer -and $line -match '^\s{1,3}\S') {
            $insideLavalinkServer = $false
        }
        if ($insideLavalinkServer -and $line -match '^\s{4,}password:\s*(.*?)\s*$') {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

Write-Host "Checking Java..."
$javaMajor = Get-JavaMajorVersion
Write-Host "Java $javaMajor detected (Lavalink requires 17 or newer)."

$lavalinkDirectory = Join-Path $ProjectRoot "lavalink"
$pluginDirectory = Join-Path $lavalinkDirectory "plugins"
New-Item -ItemType Directory -Path $pluginDirectory -Force | Out-Null

Install-VerifiedJar -Uri $LavalinkUrl `
    -Destination (Join-Path $lavalinkDirectory "Lavalink.jar") `
    -Sha256 $LavalinkSha256 -Kind Lavalink

$pluginJar = Join-Path $pluginDirectory "youtube-plugin-$YouTubePluginVersion.jar"
$existingYouTubePlugins = @(Get-ChildItem -LiteralPath $pluginDirectory -Filter "youtube-plugin*.jar" -File -ErrorAction SilentlyContinue)
if ($existingYouTubePlugins.Count -gt 0 -and -not (Test-Path -LiteralPath $pluginJar)) {
    Write-Warning "An existing YouTube plugin JAR was found. It was left in place and no second plugin was downloaded. Verify its version before starting Lavalink."
}
else {
    Install-VerifiedJar -Uri $YouTubePluginUrl `
        -Destination $pluginJar -Sha256 $YouTubePluginSha256 -Kind YouTube
}

Copy-TemplateIfMissing -Template (Join-Path $ProjectRoot ".env.example") `
    -Destination (Join-Path $ProjectRoot ".env") -Label ".env"
Copy-TemplateIfMissing -Template (Join-Path $lavalinkDirectory "application.yml.example") `
    -Destination (Join-Path $lavalinkDirectory "application.yml") -Label "Lavalink configuration"

$botPassword = Get-DotEnvValue -Path (Join-Path $ProjectRoot ".env") -Name "LAVALINK_PASSWORD"
$serverPassword = Get-LavalinkPassword -Path (Join-Path $lavalinkDirectory "application.yml")
$placeholder = "CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD"
if ([string]::IsNullOrWhiteSpace($botPassword) -or [string]::IsNullOrWhiteSpace($serverPassword)) {
    Write-Warning "Set LAVALINK_PASSWORD in .env and lavalink.server.password in lavalink/application.yml. Values are not displayed."
}
elseif ($botPassword -ne $serverPassword) {
    Write-Warning "LAVALINK_PASSWORD differs from lavalink.server.password in application.yml. Make them match; values are not displayed."
}
elseif ($botPassword -eq $placeholder) {
    Write-Warning "Both password fields still contain the example placeholder. Replace both with the same private password before starting Lavalink."
}
else {
    Write-Host "Lavalink password values match (value hidden)."
}

Write-Host "Setup files are ready. Install Python dependencies and review both local configuration files before starting Lavalink and Miku."
