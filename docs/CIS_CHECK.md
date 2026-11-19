# Проверка КИ из SQL Server

`POST /api/check-cis` принимает от 1 до 2 000 КИ РЭП в одном XML-запросе размером до 512 КиБ. КИ состоит из 38 символов и передаётся без криптохвоста. Регистр серийного номера сохраняется. Запросы в ЧЗ делятся на пакеты до 1 000 КИ. Сервис возвращает один XML со всеми результатами.

Настройте `CIS_CHECK_KEY` и `CIS_CHECK_ORG_INN` в серверном `.env` или `EnvironmentFile` и перезапустите сервис. В `CIS_CHECK_KEY` укажите сам ключ. `CIS_CHECK_ORG_INN` должен совпадать с ИНН организации, для которой настроен токен ЧЗ. Для ключа используйте от 32 до 128 символов `A–Z`, `a–z`, `0–9`, `_`, `-`. Для получения ответа установите клиентский таймаут 150 секунд.

```http
POST /api/check-cis
Content-Type: application/xml; charset=utf-8
Authorization: Bearer <ключ сервиса>
```

```xml
<Cises>
  <Cis>01046406383452182112345678901234567890</Cis>
</Cises>
```

Пример ответа с условными данными

```xml
<CisCheckResult>
  <Row index="1">
    <Cis>01046406383452182112345678901234567890</Cis>
    <resultCode>OK</resultCode>
    <GTIN>04640638345218</GTIN>
    <quantityInPack>100</quantityInPack>
    <status>INTRODUCED</status>
    <packageType>UNIT</packageType>
    <productName>Артикул из карточки</productName>
    <product_group>radio</product_group>
    <PN>Артикул из карточки</PN>
  </Row>
</CisCheckResult>
```

| Поле | Источник и формат |
| --- | --- |
| `quantityInPack` | Количество единиц товара по конкретному КИ из `cises/info`, целое число |
| `declarationRegNumber`, `declarationDate` | Номер и дата ДТ из доступных сведений ЧЗ. Дата в формате `YYYY-MM-DD` |
| `status`, `statusEx` | Статус и особое состояние из ответа ЧЗ |
| `tnVedEaes`, `country`, `GTIN` | ТН ВЭД, код страны и GTIN. Храните как строки с ведущими нулями |
| `packageType` | Тип упаковки, начиная с наиболее подробного поля, которое вернул ЧЗ |
| `productName` | Модель или артикул из карточки по GTIN, если доступен. В остальных случаях используется наименование товара из ЧЗ |
| `brand`, `product_group` | Бренд и товарная группа ЧЗ |
| `PN` | Модель или артикул `model` из доступной карточки по GTIN |
| `producerInn`, `producerName`, `ownerInn`, `ownerName` | Участники оборота из ответа ЧЗ. ИНН храните как строки |

У каждого входного элемента свой `Row` с порядковым номером `index`, начиная с 1. Порядок и дубли сохраняются. Поля, которых нет в ответе ЧЗ, пропускаются. Если дополнительный запрос за ДТ дал сбой, в строке может прийти `declarationWarning`. При `INVALID_CIS` для сопоставления с входной строкой используйте `index`.

| `resultCode` при HTTP 200 | Значение |
| --- | --- |
| `OK` | Получены данные по КИ, в том числе `quantityInPack` и `status` |
| `PARTIAL` | КИ обработан, но ЧЗ не вернул корректное количество или статус. Подробности в `message` |
| `INVALID_CIS` | Ошибка формата КИ или контрольной цифры GTIN |
| `NOT_FOUND` | ЧЗ сообщил, что КИ не найден |
| `FORBIDDEN` | ЧЗ сообщил, что доступ к КИ ограничен |
| `UPSTREAM_ERROR` | Не удалось подтвердить данные по КИ в ответе ЧЗ |

При ошибке всего запроса возвращается `<Error code="…" message="…" />`. HTTP 401 означает ошибку ключа сервиса, 400 или 415 — ошибку формата, 413 — превышение размера тела. HTTP 502–504 означает, что сервис не смог получить результат для пакета или интеграция пока не настроена.

## Пример для SQL Server 2008

Подставьте URL и значение `Bearer <ключ>`. Пример использует `dbo.CisQueue(CIS, IsProcessed)` из исходной схемы, возвращает строки для записи в буферную таблицу и оставляет изменение очереди вызывающей процедуре. Для пробной отправки ограничьте выборку одной строкой. Для выполнения нужны уже настроенные права на вызов `sp_OA*` и доступ SQL-сервера к сервису.

```sql
DECLARE @Url nvarchar(500) = N'http://HOST:8000/api/check-cis';
DECLARE @AuthToken nvarchar(500) = N'Bearer <ключ>';
DECLARE @Object int, @Hr int, @Status int, @RequestedCount int;
DECLARE @RequestBody nvarchar(max), @ResponseBody nvarchar(max), @RequestXml xml, @Result xml;
DECLARE @ResponseTable TABLE (ResponseText nvarchar(max));

SET @RequestXml = (
    SELECT TOP (2000) LTRIM(RTRIM(CIS)) AS [Cis]
    FROM dbo.CisQueue
    WHERE IsProcessed = 0 AND CIS IS NOT NULL
    ORDER BY CIS COLLATE Latin1_General_BIN2
    FOR XML PATH(''), ROOT('Cises'), TYPE
);

SET @RequestedCount = @RequestXml.value('count(/Cises/Cis)', 'int');
IF @RequestedCount = 0 RETURN;
SET @RequestBody = CONVERT(nvarchar(max), @RequestXml);

EXEC @Hr = sp_OACreate 'MSXML2.ServerXMLHTTP.6.0', @Object OUTPUT;
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAMethod @Object, 'setTimeouts', NULL, 5000, 5000, 15000, 150000;
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAMethod @Object, 'open', NULL, 'POST', @Url, 'false';
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAMethod @Object, 'setRequestHeader', NULL, 'Content-Type', 'application/xml; charset=utf-8';
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAMethod @Object, 'setRequestHeader', NULL, 'Authorization', @AuthToken;
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAMethod @Object, 'send', NULL, @RequestBody;
IF @Hr <> 0 GOTO HttpError;
EXEC @Hr = sp_OAGetProperty @Object, 'status', @Status OUTPUT;
IF @Hr <> 0 GOTO HttpError;
INSERT INTO @ResponseTable (ResponseText)
    EXEC @Hr = sp_OAGetProperty @Object, 'responseText';
IF @Hr <> 0 GOTO HttpError;
EXEC sp_OADestroy @Object;

SELECT @ResponseBody = ResponseText FROM @ResponseTable;
IF @Status IS NULL OR @Status <> 200
BEGIN
    SELECT @Status AS HttpStatus, @ResponseBody AS ErrorBody;
    RETURN;
END;

SET @Result = CONVERT(xml, @ResponseBody);
IF @Result IS NULL OR @Result.value('count(/CisCheckResult/Row)', 'int') <> @RequestedCount
BEGIN
    RAISERROR(N'Количество строк в ответе проверки не совпадает с количеством отправленных КИ.', 16, 1);
    RETURN;
END;

SELECT
    R.value('(@index)[1]', 'int') AS RequestIndex,
    R.value('(Cis/text())[1]', 'nvarchar(38)') AS CIS,
    R.value('(quantityInPack/text())[1]', 'int') AS quantityInPack,
    R.value('(declarationRegNumber/text())[1]', 'nvarchar(50)') AS declarationRegNumber,
    R.value('(declarationDate/text())[1]', 'date') AS declarationDate,
    R.value('(status/text())[1]', 'nvarchar(60)') AS status,
    R.value('(tnVedEaes/text())[1]', 'nvarchar(10)') AS tnVedEaes,
    R.value('(country/text())[1]', 'nvarchar(3)') AS country,
    R.value('(packageType/text())[1]', 'nvarchar(60)') AS packageType,
    R.value('(productName/text())[1]', 'nvarchar(max)') AS productName,
    R.value('(brand/text())[1]', 'nvarchar(max)') AS brand,
    R.value('(product_group/text())[1]', 'nvarchar(80)') AS product_group,
    R.value('(producerInn/text())[1]', 'nvarchar(12)') AS producerInn,
    R.value('(producerName/text())[1]', 'nvarchar(max)') AS producerName,
    R.value('(ownerInn/text())[1]', 'nvarchar(12)') AS ownerInn,
    R.value('(ownerName/text())[1]', 'nvarchar(max)') AS ownerName,
    R.value('(statusEx/text())[1]', 'nvarchar(80)') AS statusEx,
    R.value('(GTIN/text())[1]', 'nvarchar(14)') AS GTIN,
    R.value('(PN/text())[1]', 'nvarchar(max)') AS PN,
    R.value('(resultCode/text())[1]', 'nvarchar(30)') AS resultCode,
    R.value('(message/text())[1]', 'nvarchar(max)') AS Message,
    R.value('(declarationWarning/text())[1]', 'nvarchar(max)') AS declarationWarning
FROM @Result.nodes('/CisCheckResult/Row') AS Rows(R)
ORDER BY RequestIndex;
RETURN;

HttpError:
IF @Object IS NOT NULL EXEC sp_OADestroy @Object;
RAISERROR(N'Ошибка MSXML при вызове проверки КИ. HRESULT: %d.', 16, 1, @Hr);
```
