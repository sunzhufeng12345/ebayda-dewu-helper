$ErrorActionPreference = "Stop"
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    python -m PyInstaller --version | Out-Null

    # Windows CI 上中文路径编码不可靠，先复制为英文名再打包
    $configTmp = "_ebayda_config_tmp"
    if (Test-Path $configTmp) { Remove-Item $configTmp -Recurse -Force }
    Copy-Item "配置文件" $configTmp -Recurse

    python -m PyInstaller --noconfirm --clean --onefile --name EbaydaHelper --add-data "$configTmp;_ebayda_config" ebayda_helper.py

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
