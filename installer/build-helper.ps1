$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    python -m PyInstaller --noconfirm --clean --onefile --name EbaydaHelper ebayda_helper.py

    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        throw "未找到 Inno Setup 6，请先安装后重试。"
    }
    & $iscc "$PSScriptRoot\EbaydaHelper.iss"
}
finally {
    Pop-Location
}
