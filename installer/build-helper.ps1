$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
$repoRoot = Split-Path -Parent $PSScriptRoot

# Unicode escape for Chinese directory name to avoid .ps1 encoding issues on Windows PowerShell 5.1
# Original name: U+914D U+7F6E U+6587 U+4EF6
$configDirName = [string]::new([char[]](0x914D, 0x7F6E, 0x6587, 0x4EF6))

Push-Location $repoRoot
try {
    python -m PyInstaller --version | Out-Null

    # Windows CI Chinese path encoding is unreliable; copy to English name before packaging
    $configTmp = "_ebayda_config_tmp"
    if (Test-Path $configTmp) { Remove-Item $configTmp -Recurse -Force }
    Copy-Item -LiteralPath $configDirName -Destination $configTmp -Recurse

    python -m PyInstaller --noconfirm --clean --onefile --name EbaydaHelper --add-data "$configTmp;_ebayda_config" ebayda_helper.py

    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        throw "Inno Setup 6 not found, please install it first."
    }
    & $iscc "$PSScriptRoot\EbaydaHelper.iss"
}
finally {
    Pop-Location
}
