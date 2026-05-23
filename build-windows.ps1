param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$nativeProject = Join-Path $root "native\Mlx90640Native\Mlx90640Native.vcxproj"
$nativeOut = Join-Path $root "src\InfraredCollector.Win\NativeBinaries"

New-Item -ItemType Directory -Force -Path $nativeOut | Out-Null

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

$dotnetCommand = Get-Command dotnet.exe -ErrorAction SilentlyContinue
if (-not $dotnetCommand) {
    throw @"
.NET SDK not found: dotnet.exe is not in PATH.

Install .NET 8 SDK x64, then reopen PowerShell:
  winget install Microsoft.DotNet.SDK.8

Or download the Windows x64 SDK installer from:
  https://dotnet.microsoft.com/download/dotnet/8.0

Verify with:
  dotnet --list-sdks
"@
}

$dotnetSdks = & $dotnetCommand.Source --list-sdks 2>$null
if ($LASTEXITCODE -ne 0 -or -not $dotnetSdks) {
    throw @"
.NET SDK not found: dotnet.exe was found at '$($dotnetCommand.Source)', but no SDKs are installed.

This usually means only the .NET Runtime is installed. Install .NET 8 SDK x64, then reopen PowerShell:
  winget install Microsoft.DotNet.SDK.8

Or download the Windows x64 SDK installer from:
  https://dotnet.microsoft.com/download/dotnet/8.0

Verify with:
  dotnet --list-sdks
"@
}

$dotnet8Sdk = $dotnetSdks | Where-Object { $_ -match '^8\.' } | Select-Object -First 1
if (-not $dotnet8Sdk) {
    throw @"
.NET 8 SDK not found.

Installed SDKs:
$($dotnetSdks -join "`n")

Install .NET 8 SDK x64, then reopen PowerShell:
  winget install Microsoft.DotNet.SDK.8

Or download the Windows x64 SDK installer from:
  https://dotnet.microsoft.com/download/dotnet/8.0
"@
}

Write-Host "Using dotnet: $($dotnetCommand.Source)"
Write-Host "Using .NET SDK: $dotnet8Sdk"
$dotnet = $dotnetCommand.Source

$msbuildCommand = Get-Command msbuild.exe -ErrorAction SilentlyContinue
$msbuild = $null
if ($msbuildCommand) {
    $msbuild = $msbuildCommand.Source
}
if (-not $msbuild) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $candidate = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
        if ($candidate -and (Test-Path $candidate)) {
            $msbuild = $candidate
        }
        if (-not $msbuild) {
            $install = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -property installationPath
            if ($install) {
                $candidate = Join-Path $install "MSBuild\Current\Bin\MSBuild.exe"
                if (Test-Path $candidate) { $msbuild = $candidate }
            }
        }
    }
}
if (-not $msbuild) {
    throw "MSBuild not found. Install Visual Studio Build Tools 2019 or 2022 with Desktop development with C++."
}

$platformToolset = "v143"
if ($msbuild -match "\\2019\\") {
    $platformToolset = "v142"
}

Write-Host "Using MSBuild: $msbuild"
Write-Host "Using PlatformToolset: $platformToolset"

Invoke-Checked $msbuild @(
    $nativeProject,
    "/p:Configuration=$Configuration",
    "/p:Platform=x64",
    "/p:PlatformToolset=$platformToolset",
    "/m"
)
Copy-Item (Join-Path $root "native\Mlx90640Native\x64\$Configuration\Mlx90640Native.dll") $nativeOut -Force

Invoke-Checked $dotnet @(
    "restore",
    (Join-Path $root "InfraredCollector.sln")
)
Invoke-Checked $dotnet @(
    "test",
    (Join-Path $root "tests\InfraredCollector.Tests\InfraredCollector.Tests.csproj"),
    "-c",
    $Configuration,
    "--no-restore"
)
Invoke-Checked $dotnet @(
    "publish",
    (Join-Path $root "src\InfraredCollector.Win\InfraredCollector.Win.csproj"),
    "-c",
    $Configuration,
    "-r",
    "win-x64",
    "--self-contained",
    "false"
)

Write-Host "Publish output:"
Write-Host (Join-Path $root "src\InfraredCollector.Win\bin\$Configuration\net8.0-windows\win-x64\publish")
