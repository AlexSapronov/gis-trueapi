# refresh_trueapi_token.ps1
# Получение UUID + SIGNATURE для обновления токена True API (ГИС МТ / Честный ЗНАК).
#
# Назначение:  True API challenge -> КриптоПро CSP (подпись УКЭП) -> UUID + SIGNATURE.
# Полученные UUID и SIGNATURE вставляются в служебную страницу /admin/token
# backend-приложения.
#
# Требования: Windows + установленный КриптоПро CSP, действующая УКЭП в хранилище.
#
# Как работает выбор ЭЦП:
#   1. Ищет доступные СЕЙЧАС контейнеры закрытых ключей КриптоПро:
#        csptest.exe -keyset -enum_cont -verifycontext -fqcn
#   2. Для каждого контейнера получает SHA1-отпечаток сертификата:
#        certmgr.exe -list -container "<container>"
#   3. Сопоставляет отпечаток с сертификатом в Cert:\CurrentUser\My.
#   4. Показывает действующие сертификаты и просит выбрать нужный.
#   5. Подписывает challenge выбранной ЭЦП по THUMBPRINT (а не по ФИО).

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Русский вывод в консоль (UTF-8) для обычного Windows PowerShell 5.1
# ---------------------------------------------------------------------------
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # некритично — если консоль не дала сменить кодировку, продолжаем как есть
}

# ---------------------------------------------------------------------------
# Поиск инструментов КриптоПро
# ---------------------------------------------------------------------------
function Find-CryptoProTool([string]$Name) {
    $candidates = @()
    # 1) Get-Command как fallback
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidates += $cmd.Source
    }
    # 2) Известные пути установки
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
    return ($candidates | Select-Object -Unique) | Where-Object { $_ }
}

$csptest = Find-CryptoProTool 'csptest.exe' | Select-Object -First 1
$certmgr = Find-CryptoProTool 'certmgr.exe' | Select-Object -First 1

if (-not $csptest) {
    Write-Host "Ошибка: не найден csptest.exe." -ForegroundColor Red
    Write-Host "Проверьте установку КриптоПро CSP (C:\Program Files\Crypto Pro\CSP\)." -ForegroundColor Red
    exit 1
}
if (-not $certmgr) {
    Write-Host "Ошибка: не найден certmgr.exe." -ForegroundColor Red
    Write-Host "Проверьте установку КриптоПро CSP (C:\Program Files\Crypto Pro\CSP\)." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
function Write-WarnMessage([string]$Text) {
    Write-Host "Внимание: $Text" -ForegroundColor Yellow
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
    # REGISTRY / HDIMAGE => локальный (software) контейнер; остальное — внешний носитель/токен.
    if ($Fqcn -match '\\\\.\\(REGISTRY|HDIMAGE)\\') {
        return 'локальный контейнер'
    }
    return 'токен'
}

# ---------------------------------------------------------------------------
# 1. Перечисляем доступные сейчас контейнеры закрытых ключей
# ---------------------------------------------------------------------------
$enumOutput = & $csptest -keyset -enum_cont -verifycontext -fqcn 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Ошибка: не удалось получить список контейнеров КриптоПро (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

$containers = @()
foreach ($line in $enumOutput) {
    $s = ($line | Out-String).Trim()
    if ($s -match '^\\\\\.\\') {
        $containers += $s
    }
}
$containers = $containers | Select-Object -Unique

if (-not $containers -or $containers.Count -eq 0) {
    Write-Host "Не найдено доступных контейнеров закрытых ключей КриптоПро."
    Write-Host "Проверьте, что токен подключён."
    exit 1
}

# ---------------------------------------------------------------------------
# 2-4. Сопоставляем контейнер -> SHA1 отпечаток -> сертификат в CurrentUser\My
# ---------------------------------------------------------------------------
$now = Get-Date
$choices = @()  # элементы: @{ Container; Kind; Cert; Thumbprint; Name; NotAfter; DisplayText }

foreach ($container in $containers) {
    $listOutput = & $certmgr -list -container $container 2>&1
    # Ошибка одного контейнера не роняет весь поиск.
    if ($LASTEXITCODE -ne 0) {
        Write-WarnMessage "не удалось прочитать контейнер (exit code $LASTEXITCODE): $container"
        continue
    }

    $thumbs = @()
    foreach ($line in $listOutput) {
        $s = ($line | Out-String)
        # Ищем строку с SHA1 и hex-значением отпечатка.
        if ($s -match 'SHA1\s*[:\s]+([0-9A-Fa-f]{40})') {
            $t = $Matches[1] -replace '\s',''
            $thumbs += $t.ToUpperInvariant()
        }
    }
    $thumbs = $thumbs | Select-Object -Unique

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

        # Фильтрация: действующий сейчас, имеет закрытый ключ.
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

        $name = Get-DisplayName $cert
        $kind = Get-ContainerKind $container
        $shortThumb = $thumb.Substring(0, 8) + '...' + $thumb.Substring($thumb.Length - 8)

        $choices += [PSCustomObject]@{
            Container    = $container
            Kind         = $kind
            Cert         = $cert
            Thumbprint   = $thumb
            Name         = $name
            NotAfter     = $cert.NotAfter
            DisplayText  = "$name"
        }
    }
}

if (-not $choices -or $choices.Count -eq 0) {
    Write-Host ""
    Write-Host "Не найдено доступных действующих сертификатов УКЭП." -ForegroundColor Red
    Write-Host "Проверьте, что токен подключён и сертификат установлен в личное хранилище." -ForegroundColor Red
    exit 1
}

# Дедупликация по thumbprint + container.
$choices = $choices | Sort-Object Thumbprint, Container -Unique

# ---------------------------------------------------------------------------
# Показываем список и запрашиваем выбор
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Доступные сертификаты УКЭП:"
Write-Host ""
$i = 1
foreach ($c in $choices) {
    $containerShort = $c.Container
    # Для читаемости обрезаем длинный FQCN до последнего сегмента (имени контейнера)
    $lastSeg = $containerShort.Split('\') | Where-Object { $_ } | Select-Object -Last 1
    $mediaLabel = "[$($c.Kind)]" + $(if ($lastSeg) { " $lastSeg" } else { "" })

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
Write-Host "Выбран сертификат: $($selected.Name) (отпечаток $($selected.Thumbprint))"
Write-Host ""

# ---------------------------------------------------------------------------
# 5. Получаем challenge True API
# ---------------------------------------------------------------------------
try {
    $response = Invoke-RestMethod -Uri "https://markirovka.crpt.ru/api/v3/true-api/auth/key" -Method Get
} catch {
    Write-Host "Ошибка при получении challenge True API: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$uuid = $response.uuid
$data = $response.data

if (-not $uuid -or -not $data) {
    Write-Host "Ошибка: API True API не вернул uuid или data." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 6. Подписываем challenge по THUMBPRINT
# ---------------------------------------------------------------------------
$guid = [Guid]::NewGuid().ToString('N')
$dataFile = Join-Path $env:TEMP "gis_trueapi_$guid.data"
$sigFile  = Join-Path $env:TEMP "gis_trueapi_$guid.sig"

try {
    [System.IO.File]::WriteAllText(
        $dataFile,
        $data,
        [System.Text.Encoding]::ASCII
    )

    & $csptest `
        -sfsign `
        -sign `
        -in $dataFile `
        -out $sigFile `
        -my $selected.Thumbprint `
        -base64 `
        -add

    if ($LASTEXITCODE -ne 0) {
        Write-Host "Ошибка подписи КриптоПро. Exit code: $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path $sigFile)) {
        Write-Host "Ошибка: файл подписи не был создан." -ForegroundColor Red
        exit 1
    }

    $sig = Get-Content $sigFile -Raw
    $sig = $sig -replace '\s',''

    Write-Host ""
    Write-Host "========== UUID =========="
    Write-Host $uuid
    Write-Host "========== SIGNATURE =========="
    Write-Host $sig
    Write-Host "=============================="
}
finally {
    Remove-Item $dataFile -ErrorAction SilentlyContinue
    Remove-Item $sigFile  -ErrorAction SilentlyContinue
}