# Собирает Android-приложение и кладёт APK туда, откуда его раздаёт агент.
# Нужны JDK 21 и Android SDK. Путь без кириллицы: инструменты Android на ней спотыкаются.
param(
    [string]$Toolchain = "C:\android-toolchain"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $env:JAVA_HOME) { $env:JAVA_HOME = "$Toolchain\jdk" }
if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = "$Toolchain\sdk" }
# Кеши Gradle и ключ отладочной подписи — тоже в путь без кириллицы.
if (-not $env:GRADLE_USER_HOME) { $env:GRADLE_USER_HOME = "$Toolchain\gradle-home" }
if (-not $env:ANDROID_USER_HOME) { $env:ANDROID_USER_HOME = "$Toolchain\android-home" }

Push-Location "$root\android"
try {
    & .\gradlew.bat assembleRelease --console=plain
    if ($LASTEXITCODE -ne 0) { throw "Android build failed" }
} finally {
    Pop-Location
}

$out = "$root\phonenect\web\android"
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item "$root\android\app\build\outputs\apk\release\app-release.apk" "$out\phonenect.apk" -Force
Write-Output "APK: $out\phonenect.apk ($((Get-Item "$out\phonenect.apk").Length) bytes)"
