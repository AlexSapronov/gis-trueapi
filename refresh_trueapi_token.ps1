# refresh_trueapi_token.ps1
# Получение UUID + SIGNATURE для обновления токена True API (ГИС МТ / Честный ЗНАК).
#
# Назначение: True API challenge -> КриптоПро CSP (подпись УКЭП) -> UUID + SIGNATURE.
# Полученные UUID и SIGNATURE вставляются в служебную страницу /admin/token.
#
# Требования: Windows + КриптоПро CSP + действующая УКЭП.
#
# Важно для Windows PowerShell 5.1:
# файл должен быть сохранён как UTF-8 WITH BOM.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # Некритично.
}

function Write-WarnMessage([string]$Text) {
    Write-Host "Внимание: $Text" -ForegroundColor Yellow
}

function Find-CryptoProTool([string]$Name) {
    $candidates = @()

    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidates += $cmd.Source
    }

    $baseDirs = @(
        'C:\Program Files\Crypto Pro\CSP\',
        'C:\Program Files (x86)\Crypto Pro\CSP\'
    )

    foreach ($dir in $baseDirs) {
        $p = Join-Path $dir $Name
        if (Test-Path $p) {
            $candidates += $p
        }
    }

    return @($candidates | Where-Object { $_ } | Select-Object -Unique)
}

function Get-DisplayName($Cert) {
    $name = $Cert.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false
    )

    if ([string]::IsNullOrWhiteSpace($name)) {
        $name = $Cert.Subject
    }

    return $name
}

function Get-ContainerKind([string]$Fqcn) {
    if ($Fqcn -match '^\\\\\.\\(REGISTRY|HDIMAGE)\\') {
        return 'локальный контейнер'
    }

    return 'токен'
}

$csptest = @(Find-CryptoProTool 'csptest.exe') | Select-Object -First 1
$certmgr = @(Find-CryptoProTool 'certmgr.exe') | Select-Object -First 1

if (-not $csptest) {
    Write-Host "Ошибка: не найден csptest.exe." -ForegroundColor Red
    Write-Host "Проверьте установку КриптоПро CSP." -ForegroundColor Red
    exit 1
}

if (-not $certmgr) {
    Write-Host "Ошибка: не найден certmgr.exe." -ForegroundColor Red
    Write-Host "Проверьте установку КриптоПро CSP." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Ищем доступные сейчас контейнеры КриптоПро
# ---------------------------------------------------------------------------

$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $enumOutput = & $csptest -keyset -enum_cont -verifycontext -fqcn 2>&1
    $enumExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}

if ($enumExitCode -ne 0) {
    Write-Host "Ошибка: не удалось получить список контейнеров КриптоПро (exit code $enumExitCode)." -ForegroundColor Red
    exit 1
}

$containers = @()

foreach ($line in $enumOutput) {
    $s = ($line | Out-String).Trim()

    if ($s -match '^\\\\\.\\') {
        $containers += $s
    }
}

$containers = @($containers | Select-Object -Unique)

if ($containers.Count -eq 0) {
    Write-Host "Не найдено доступных контейнеров закрытых ключей КриптоПро."
    Write-Host "Проверьте, что токен подключён."
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Контейнер -> SHA1 thumbprint -> сертификат CurrentUser\My
# ---------------------------------------------------------------------------

$now = Get-Date
$choices = @()

foreach ($container in $containers) {
    $previousErrorActionPreference = $ErrorActionPreference

    try {
        # На Windows PowerShell 5.1 stderr native-программы при
        # ErrorActionPreference=Stop может прервать скрипт раньше проверки
        # $LASTEXITCODE. Поэтому для одного вызова certmgr временно Continue.
        $ErrorActionPreference = 'Continue'
        $listOutput = & $certmgr -list -container $container 2>&1
        $certmgrExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($certmgrExitCode -ne 0) {
        Write-WarnMessage "не удалось прочитать контейнер (exit code $certmgrExitCode): $container"
        continue
    }

    $thumbs = @()

    foreach ($line in $listOutput) {
        $s = ($line | Out-String)

        # Подтверждённый формат CryptoPro certmgr 5.0.13800:
        # SHA1 Thumbprint     : ddf20ee29cc17a62d0aecc4f119d3f2a9736bc94
        # Также поддерживается "SHA1 : ..." и отпечаток с пробелами.
        if ($s -match '(?i)\bSHA1(?:\s+Thumbprint)?\b\s*:\s*(.+)$') {
            $t = $Matches[1] -replace '[^0-9A-Fa-f]', ''

            if ($t.Length -eq 40) {
                $thumbs += $t.ToUpperInvariant()
            }
        }
    }

    $thumbs = @($thumbs | Select-Object -Unique)

    if ($thumbs.Count -eq 0) {
        Write-WarnMessage "контейнер найден, но отпечаток SHA1 не получен: $container"
        continue
    }

    foreach ($thumb in $thumbs) {
        $cert = Get-ChildItem -Path 'Cert:\CurrentUser\My' |
            Where-Object { $_.Thumbprint -eq $thumb } |
            Select-Object -First 1

        if (-not $cert) {
            Write-WarnMessage "контейнер найден, но сертификат не установлен в личном хранилище: $container"
            continue
        }

        if (-not $cert.HasPrivateKey) {
            Write-WarnMessage "сертификат не связан с закрытым ключом, пропущен: $(Get-DisplayName $cert)"
            continue
        }

        if ($cert.NotBefore -gt $now) {
            Write-WarnMessage "сертификат ещё не действует, пропущен: $(Get-DisplayName $cert)"
            continue
        }

        if ($cert.NotAfter -lt $now) {
            Write-WarnMessage "срок действия сертификата истёк, пропущен: $(Get-DisplayName $cert)"
            continue
        }

        $choices += [PSCustomObject]@{
            Container  = $container
            Kind       = Get-ContainerKind $container
            Cert       = $cert
            Thumbprint = $thumb
            Name       = Get-DisplayName $cert
            NotAfter   = $cert.NotAfter
        }
    }
}

$choices = @($choices | Sort-Object Thumbprint, Container -Unique)

if ($choices.Count -eq 0) {
    Write-Host ""
    Write-Host "Не найдено доступных действующих сертификатов УКЭП." -ForegroundColor Red
    Write-Host "Проверьте, что токен подключён и сертификат установлен в личное хранилище." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Выбор сертификата
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Доступные сертификаты УКЭП:"
Write-Host ""

$i = 1

foreach ($c in $choices) {
    $lastSeg = $c.Container.Split('\') |
        Where-Object { $_ } |
        Select-Object -Last 1

    if ($lastSeg) {
        $mediaLabel = "[$($c.Kind)] $lastSeg"
    }
    else {
        $mediaLabel = "[$($c.Kind)]"
    }

    Write-Host "[$i] $($c.Name)"
    Write-Host "    Действителен до: $($c.NotAfter.ToString('dd.MM.yyyy'))"
    Write-Host "    Носитель: $mediaLabel"
    Write-Host "    Отпечаток: $($c.Thumbprint)"
    Write-Host ""

    $i++
}

$max = $choices.Count
$selected = $null

while ($null -eq $selected) {
    $answer = Read-Host "Выберите сертификат [1-$max]"

    if ([string]::IsNullOrWhiteSpace($answer)) {
        Write-Host "Пустой ввод. Введите число от 1 до $max." -ForegroundColor Yellow
        continue
    }

    $num = 0

    if (-not [int]::TryParse($answer, [ref]$num)) {
        Write-Host "Некорректный ввод. Введите число от 1 до $max." -ForegroundColor Yellow
        continue
    }

    if ($num -lt 1 -or $num -gt $max) {
        Write-Host "Число вне диапазона. Введите число от 1 до $max." -ForegroundColor Yellow
        continue
    }

    $selected = $choices[$num - 1]
}

Write-Host ""
Write-Host "Выбран сертификат: $($selected.Name)"
Write-Host ""

# ---------------------------------------------------------------------------
# 4. Получаем challenge True API
# ---------------------------------------------------------------------------

try {
    $response = Invoke-RestMethod `
        -Uri "https://markirovka.crpt.ru/api/v3/true-api/auth/key" `
        -Method Get
}
catch {
    Write-Host "Ошибка при получении challenge True API: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$uuid = $response.uuid
$data = $response.data

if (-not $uuid -or -not $data) {
    Write-Host "Ошибка: True API не вернул uuid или data." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 5. Подписываем challenge
# ---------------------------------------------------------------------------

$guid = [Guid]::NewGuid().ToString('N')
$dataFile = Join-Path $env:TEMP "gis_trueapi_$guid.data"
$sigFile = Join-Path $env:TEMP "gis_trueapi_$guid.sig"

try {
    [System.IO.File]::WriteAllText(
        $dataFile,
        $data,
        [System.Text.Encoding]::ASCII
    )

    $previousErrorActionPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = 'Continue'

        & $csptest `
            -sfsign `
            -sign `
            -in $dataFile `
            -out $sigFile `
            -my $selected.Thumbprint `
            -base64 `
            -add

        $signExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($signExitCode -ne 0) {
        Write-Host "Ошибка подписи КриптоПро. Exit code: $signExitCode" -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path $sigFile)) {
        Write-Host "Ошибка: файл подписи не был создан." -ForegroundColor Red
        exit 1
    }

    $sig = Get-Content $sigFile -Raw
    $sig = $sig -replace '\s', ''

    Write-Host ""
    Write-Host "========== UUID =========="
    Write-Host $uuid
    Write-Host "========== SIGNATURE =========="
    Write-Host $sig
    Write-Host "=============================="
}
finally {
    Remove-Item $dataFile -ErrorAction SilentlyContinue
    Remove-Item $sigFile -ErrorAction SilentlyContinue
}
