# Получение UUID + SIGNATURE для обновления токена True API (ГИС МТ / Честный ЗНАК).
#
# Назначение:  True API challenge -> КриптоПро CSP (подпись УКЭП) -> UUID + SIGNATURE.
# Полученные UUID и SIGNATURE вставляются в служебную страницу /admin/token
# backend-приложения.
#
# Требования: Windows + установленный КриптоПро CSP, действующая УКЭП в хранилище.
#
# ВАЖНО: укажите ниже ПОДСУБЪЕКТ (имя владельца) вашего сертификата УКЭП —
# то же значение, что возвращает команда:
#     certutil -store -user My | findstr Subject
# либо через КриптоПро. Значение передаётся в `-my` к csptest.exe.

$certName = "ФАМИЛИЯ ИМЯ ОТЧЕСТВО ВЛАДЕЛЬЦА СЕРТИФИКАТА УКЭП"

$response = Invoke-RestMethod -Uri "https://markirovka.crpt.ru/api/v3/true-api/auth/key" -Method Get

$uuid = $response.uuid
$data = $response.data

if (-not $uuid -or -not $data) {
    throw "API did not return uuid or data"
}

$dataFile = Join-Path $env:TEMP "gis_data_hex.txt"
$sigFile = Join-Path $env:TEMP "gis_sig_b64.txt"

Remove-Item $dataFile -ErrorAction SilentlyContinue
Remove-Item $sigFile -ErrorAction SilentlyContinue

[System.IO.File]::WriteAllText(
    $dataFile,
    $data,
    [System.Text.Encoding]::ASCII
)

& "C:\Program Files\Crypto Pro\CSP\csptest.exe" `
    -sfsign `
    -sign `
    -in $dataFile `
    -out $sigFile `
    -my $certName `
    -base64 `
    -add

if ($LASTEXITCODE -ne 0) {
    throw "CryptoPro failed. Exit code: $LASTEXITCODE"
}

if (-not (Test-Path $sigFile)) {
    throw "Signature file was not created"
}

$sig = Get-Content $sigFile -Raw
$sig = $sig -replace '\s',''

Write-Host ""
Write-Host "========== UUID =========="
Write-Host $uuid
Write-Host "========== SIGNATURE =========="
Write-Host $sig
Write-Host "=============================="