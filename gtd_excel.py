"""Match a multi-document ERP workbook to completed scans and verified DTs."""
from collections import Counter, defaultdict
from copy import copy
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import asyncio
import io
import logging
import re
import zipfile

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from models import AppError
from upd_xml import allocate, decimal
from warehouse_store import WarehouseError
from warehouse_excel import MAX_FILE_SIZE, positive_integer, shipment_gtin_cell
from lxml import etree as E

REQUIRED={'Number','FromINN','GoodName','Amount','Declaration'}
KNOWN=REQUIRED | {'ToINN','IsReturn','Date','GoodId','Price','Country','NDS','CityFrom','FromAddress','CurrencyCode','UnitCode','CountryCode','EAN13','CustomTax','GoodsMilitaryAmount','GoodsItemTotal','CustomCode'}
YELLOW=PatternFill('solid',fgColor='FFF2CC')


def identifier(value):
    if value is None:return ''
    if isinstance(value,bool):raise ValueError('Некорректный идентификатор')
    if isinstance(value,(int,float)):
        if isinstance(value,float) and not value.is_integer():raise ValueError('Идентификатор должен быть целым')
        return str(int(value))
    return str(value).strip()


def date_key(value):
    if value is None:return ''
    if isinstance(value,(date,datetime)):return value.date().isoformat() if isinstance(value,datetime) else value.isoformat()
    for fmt in ('%Y-%m-%d','%d.%m.%Y'):
        try:return datetime.strptime(str(value).strip(),fmt).date().isoformat()
        except ValueError:pass
    raise ValueError('Дата должна иметь формат ДД.ММ.ГГГГ или ГГГГ-ММ-ДД')


def load_book(content):
    if not content or len(content)>MAX_FILE_SIZE:raise WarehouseError('Загрузите Excel .xlsx размером до 10 МБ')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist())>80*1024*1024:
                raise WarehouseError('Слишком большой распакованный Excel')
        return openpyxl.load_workbook(io.BytesIO(content),data_only=False,keep_links=True)
    except WarehouseError:raise
    except Exception as exc:raise WarehouseError('Не удалось прочитать файл .xlsx') from exc


def read_rows(book):
    rows=[]
    for sheet in book:
        columns={}
        header=None
        for row in sheet.iter_rows(min_row=1,max_row=min(20,sheet.max_row)):
            names={str(c.value).strip():c.column for c in row if c.value is not None}
            if REQUIRED<=names.keys():columns,header=names,row[0].row;break
        if header is None:continue
        if sheet.max_row>10001 or sheet.max_column>200:raise WarehouseError('Поддерживается до 10000 строк и 200 столбцов на листе')
        for n in range(header+1,sheet.max_row+1):
            if not any(sheet.cell(n,col).value is not None for col in columns.values()):continue
            data={key:sheet.cell(n,col).value for key,col in columns.items()}
            item=dict(sheet=sheet.title,row=n,columns=columns,data=data,status='unconfirmed',reason='',parts=None,
                      number=str(data['Number'] or '').strip(),pn=re.sub(r'\s+\([^()]*\)\s*$','',str(data['GoodName'] or '')).strip(),
                      before='' if data['Declaration'] is None else str(data['Declaration']),after='',changed=False)
            try:
                if any(sheet.cell(n,columns[key]).data_type=='f' for key in REQUIRED):
                    raise ValueError('Формулы в ключевых полях не поддерживаются')
                source_date=data.get('Date')
                try:
                    if isinstance(source_date,(int,float)) and not isinstance(source_date,bool):
                        source_date=openpyxl.utils.datetime.from_excel(source_date,book.epoch)
                    display_date=date_key(source_date)
                except (ValueError,OverflowError):
                    display_date=''  # Date is preserved in Excel, not used for matching.
                item.update(number=identifier(data['Number']),org=identifier(data['FromINN']),recipient=identifier(data.get('ToINN')),
                            date=display_date)
                item['quantity']=positive_integer(data['Amount'])
                item['good_id']=identifier(data.get('GoodId'))
                if not re.fullmatch(r'\d{10}|\d{12}',item['org']):raise ValueError('Некорректный FromINN')
                if not item['number'] or not item['pn']:raise ValueError('Не заполнены номер накладной или PN')
                if identifier(data.get('IsReturn',0)) not in ('','0'):raise ValueError('Возвраты не обрабатываются')
                if identifier(data.get('UnitCode',796))!='796':raise ValueError('Количество должно быть в штуках (UnitCode 796)')
            except (ValueError,WarehouseError) as exc:item['reason']=str(exc)
            rows.append(item)
    if not rows:raise WarehouseError('Не найдена таблица с Number, FromINN, GoodName, Amount и Declaration')
    return rows


def resolve_product(item, doc, catalog, aliases):
    """Resolve one ERP product to one GTIN, or an explicit unmarked PN."""
    gtin=catalog.get(item.get('good_id',''))
    pn=aliases.get(item['pn'],item['pn'])
    if gtin:
        if not any(line['gtin']==gtin for line in doc['lines']) and any(
                not line['gtin'] and line['pn'].strip()==pn for line in doc['lines']):
            return ('pn',pn),'Позиция без маркировки'
        return ('gtin',gtin),'GoodID → GTIN'
    gtins={line['gtin'] for line in doc['lines'] if line['pn'].strip()==pn and line['gtin']}
    if len(gtins)==1:return ('gtin',next(iter(gtins))),'PN → GTIN'
    if not gtins and any(not line['gtin'] and line['pn'].strip()==pn for line in doc['lines']):
        return ('pn',pn),'Позиция без маркировки'
    return None,''


def warehouse_composition(doc):
    result=Counter()
    for line in doc['lines']:
        key=('gtin',line['gtin']) if line['gtin'] else ('pn',line['pn'].strip())
        result[key]+=line['quantity']
    return result


def match_rows(store, rows, catalog):
    # BP numbers define invoice boundaries only. They are never compared with
    # warehouse numbers. Every invoice must match the WHOLE shipment, 1 to 1.
    invoices=defaultdict(list)
    for item in rows:
        key=(item.get('org'),item.get('recipient'),item['number'])
        invoices[key].append(item)
    by_org={};aliases={};snapshots={};matches={};groups=defaultdict(list)
    for key,items in invoices.items():
        org,recipient,number=key
        if any(item['reason'] for item in items):
            for item in items:
                if not item['reason']:item['reason']='В документе есть некорректные строки; полный состав не подтверждён'
            continue
        if not recipient:
            for item in items:item['reason']='Для сопоставления полного документа заполните ToINN'
            continue
        if org not in by_org:
            listed=store.list(admin=True,kind='shipment',org_inn=org)+store.list(admin=True,kind='shipment',org_inn=org,archived=True)
            by_org[org]=listed;aliases[org]=store.upd_aliases(org)
        candidates=[];same_parties=False;stale=False;unfinished=False
        for candidate in by_org[org]:
            if candidate['recipient_inn']!=recipient:continue
            same_parties=True
            document_id=candidate['id']
            if document_id not in snapshots:snapshots[document_id]=store.get(document_id)
            doc=snapshots[document_id]
            source=Counter();products=[]
            for item in items:
                product,method=resolve_product(item,doc,catalog,aliases[org])
                if product is None:break
                source[product]+=item['quantity'];products.append((product,method))
            else:
                if source!=warehouse_composition(doc):continue
                if doc['status']!='completed':
                    unfinished=True;continue
                if not doc['check_current'] or not (doc['result'] or {}).get('ok'):
                    stale=True;continue
                candidates.append((doc,products))
        if len(candidates)!=1:
            if len(candidates)>1:reason='Несколько складских отгрузок с теми же ИНН и полным составом; автоматический выбор невозможен'
            elif unfinished:reason='Полный состав совпал, но складская отгрузка ещё не завершена'
            elif stale:reason='Полный состав совпал, но у складской отгрузки нет актуальной успешной проверки'
            elif not same_parties:reason='Нет завершённой складской отгрузки с этими ИНН отправителя и получателя'
            else:reason='Не найдено совпадение полного состава GTIN / количество; проверьте GoodID → GTIN и все строки документа'
            for item in items:item['reason']=reason
            continue
        matches[key]=(items,*candidates[0])
    # Do not reuse one set of warehouse KIs for two distinct BP invoice numbers.
    claimed=defaultdict(list)
    for key,(_,doc,_) in matches.items():claimed[doc['id']].append(key)
    for key,(items,doc,products) in matches.items():
        if len(claimed[doc['id']])>1:
            for item in items:item['reason']='Одна складская отгрузка подходит нескольким номерам документов БП; требуется уточнение'
            continue
        for item,(product,method) in zip(items,products):
            item.update(warehouse_number=doc['number'],warehouse_date=doc['date'],match_source=method+'; полный состав документа',
                        _match_version=[doc['version'],doc['revision']])
            if product[0]=='pn':
                item['reason']='Складская отгрузка сопоставлена; позиция без маркировки не проверяется по КМ'
                continue
            gtin=product[1]
            item['target_gtins']=[gtin];item['target_pn']=gtin
            groups[(doc['id'],(gtin,))].append(item)
    return groups


def group_root(items):
    root=E.Element('Файл');table=E.SubElement(E.SubElement(root,'Документ'),'ТаблСчФакт')
    for n,item in enumerate(items):
        row=E.SubElement(table,'СведТов',НомСтр=str(n+1),НаимТов=item['pn'],ОКЕИ_Тов='796',КолТов=str(item['quantity']))
        # Preserve all commercial fields as part of the compatibility key. Qty,
        # DT and line total are the only fields that may change when rebuilding.
        values={k:(v.isoformat() if isinstance(v,(date,datetime)) else v) for k,v in item['data'].items()
                if k not in {'Amount','Declaration','GoodsItemTotal'}}
        import json
        E.SubElement(row,'ИнфПолФХЖ2').text=json.dumps(values,ensure_ascii=False,sort_keys=True,default=str)
    return root


def allocate_items(doc, items):
    # Reuse checked quantityInPack and normalized-KI handling from XML, but
    # include only this PN's scans in the reconciliation snapshot.
    pn=items[0]['target_pn']
    gtins=set(items[0]['target_gtins'])
    subset={**doc,'scans':[s for s in doc['scans'] if s['gtin'] in gtins],
            'lines':[{**line,'pn':pn} for line in doc['lines'] if line['gtin'] in gtins]}
    root=group_root(items);mapping={str(n):pn for n in range(len(items))}
    try:return allocate(root,subset,mapping),False
    except WarehouseError as exc:
        if exc.code!='upd_pack_boundary':raise
        return allocate(root,subset,mapping,regroup=True),True


def apply_declarations(book, items, allocation, rebuilt, declarations):
    scans=[s for group in allocation for s in group]
    if any(not declarations.get(s['cis']) for s in scans):
        for item in items:item['reason']='В ЧЗ не указан номер ГТД для части КМ; исходные строки сохранены'
        return
    # If original boundaries work, retain their order and commercial terms.
    for index,codes in enumerate(allocation):
        if not codes:continue
        indices=allocation.groups[index]
        sources=[items[i] for i in indices]
        grouped=defaultdict(list)
        for scan in codes:grouped[declarations[scan['cis']]].append(scan)
        totals=[sum(s['quantity'] for s in group) for group in grouped.values()]
        structural=len(sources)>1 or len(grouped)>1
        if structural:
            if any(any(key not in KNOWN and value is not None for key,value in item['data'].items())
                   or any(item['data'].get(key) not in (None,'',0,'0') for key in ('CustomTax','GoodsMilitaryAmount'))
                   for item in sources):
                for item in sources:item['reason']='Дополнительные поля или суммы требуют ручного разбиения строк'
                continue
            sheet=book[sources[0]['sheet']]
            if any(sheet.cell(item['row'],col).value is not None for item in sources
                   for col in range(1,sheet.max_column+1) if col not in item['columns'].values()):
                for item in sources:item['reason']='Заполнены столбцы без заголовка; нужно ручное разбиение'
                continue
            # Excel formulas/objects need row-reference rewriting. Retain these
            # groups rather than changing financial cells or object ranges.
            if (sheet.merged_cells.ranges or sheet.tables or sheet.auto_filter.ref or
                    sheet._charts or sheet._images or sheet.data_validations.count or len(sheet.conditional_formatting) or
                    any(c.data_type=='f' for tab in book for row in tab for c in row)):
                for item in sources:item['reason']='Нужно ручное разбиение: файл содержит формулы, объекты или связанные диапазоны'
                continue
        total_parts=None
        if structural and 'GoodsItemTotal' in sources[0]['columns']:
            try:
                total=sum(decimal(str(item['data']['GoodsItemTotal']),'GoodsItemTotal') for item in sources)
                precision=max(2,max(-decimal(str(item['data']['GoodsItemTotal']),'GoodsItemTotal').as_tuple().exponent for item in sources))
                quantum=Decimal(1).scaleb(-precision)
                cumulative,previous=0,Decimal(0)
                total_parts=[]
                for quantity in totals:
                    cumulative+=quantity
                    upto=(total*cumulative/sum(totals)).quantize(quantum,rounding=ROUND_HALF_UP)
                    total_parts.append(upto-previous);previous=upto
            except WarehouseError as exc:
                for item in sources:item['reason']=str(exc)
                continue
        parts=[]
        before_values={item['before'] for item in sources}
        for n,(dt,group) in enumerate(grouped.items()):
            if not isinstance(dt,str) or not dt.strip() or len(dt)>29:
                for item in sources:item['reason']='Некорректный номер ГТД в ответе ЧЗ'
                parts=[];break
            parts.append(dict(declaration=dt,quantity=totals[n],total=total_parts[n] if total_parts else None,
                              changed=before_values!={dt},rebuilt=structural))
        if not parts:continue
        sources[0]['parts']=parts
        for item in sources:
            item.update(status='changed' if any(p['changed'] or p['rebuilt'] for p in parts) else 'confirmed',
                        after='; '.join(grouped),changed=any(p['changed'] or p['rebuilt'] for p in parts),
                        reason='Строки пересобраны по ГТД и целым КМ' if structural else 'ГТД подтверждена ЧЗ')
        for item in sources[1:]:item['parts']=[]


def export_book(book, rows):
    by_sheet=defaultdict(list)
    for item in rows:by_sheet[item['sheet']].append(item)
    for name,items in by_sheet.items():
        sheet=book[name]
        # Capture source cells before moving rows; leave unrelated styles/types intact.
        replacements={item['row']:item for item in items if item['parts'] is not None}
        if not replacements:continue
        snapshots=[[copy(c) for c in row] for row in sheet.iter_rows()]
        dimensions={n:copy(dim) for n,dim in sheet.row_dimensions.items()}
        target=1
        for n,cells in enumerate(snapshots,1):
            item=replacements.get(n)
            parts=item['parts'] if item else [None]
            for part in parts:
                for original in cells:
                    cell=sheet.cell(target,original.column)
                    cell.value=original.value;cell.data_type=original.data_type;cell._style=copy(original._style)
                    cell.number_format=original.number_format
                    cell._hyperlink=copy(original.hyperlink)
                    if cell._hyperlink:cell._hyperlink.ref=cell.coordinate
                    cell.comment=copy(original.comment)
                if n in dimensions:
                    sheet.row_dimensions[target]=copy(dimensions[n]);sheet.row_dimensions[target].index=target
                if part:
                    columns=item['columns'];cell=sheet.cell(target,columns['Declaration'])
                    cell.value=part['declaration'];cell.data_type='s'
                    if part['rebuilt']:
                        sheet.cell(target,columns['Amount']).value=part['quantity']
                        if part['total'] is not None:sheet.cell(target,columns['GoodsItemTotal']).value=float(part['total'])
                    if part['changed'] or part['rebuilt']:
                        for col in range(1,sheet.max_column+1):sheet.cell(target,col).fill=YELLOW
                target+=1
        if target<=sheet.max_row:sheet.delete_rows(target,sheet.max_row-target+1)
    name='Проверка ГТД'
    while name in book.sheetnames:name+='_' 
    report=book.create_sheet(name)
    report.append(['Лист','Исходная строка','Накладная БП','PN','Исходная ГТД','ГТД из ЧЗ','Результат','Пояснение','Складская накладная','Дата складской накладной'])
    for item in rows:
        report.append([item['sheet'],item['row'],item['number'],item['pn'],item['before'],item['after'],
                       {'changed':'Изменена','confirmed':'Подтверждена','unconfirmed':'Не подтверждена'}[item['status']],item['reason'],item.get('warehouse_number',''),item.get('warehouse_date','')])
        report.row_dimensions[report.max_row].height=45
        for cell in report[report.max_row]:
            cell.alignment=Alignment(wrap_text=True,vertical='top')
            if isinstance(cell.value,str):cell.data_type='s'
            if item['changed']:cell.fill=YELLOW
    for cell in report[1]:cell.font=Font(bold=True)
    report.freeze_panes='A2';report.auto_filter.ref=report.dimensions
    for col,width in {'A':20,'B':18,'C':20,'D':44,'E':34,'F':42,'G':24,'H':75,'I':24,'J':24}.items():report.column_dimensions[col].width=width
    stream=io.BytesIO();book.save(stream)
    return stream.getvalue()


async def process_excel_job(store, service, job_id):
    try:
        job=store.gtd_excel_get(job_id,files=True)
        book=await asyncio.to_thread(load_book,job['source'])
        rows=await asyncio.to_thread(read_rows,book)
        catalog=store.gtd_catalog()
        groups=await asyncio.to_thread(match_rows,store,rows,{item['good_id']:item['gtin'] for item in catalog['rows']})
        documents=defaultdict(list)
        for (document_id,pn),items in groups.items():documents[document_id].append(items)
        versions={'_catalog_revision':catalog['revision']};progress=0
        store.gtd_excel_progress(job_id,0,len(documents))
        for document_id,units in documents.items():
            doc=store.get(document_id)
            if any(item['_match_version']!=[doc['version'],doc['revision']] for items in units for item in items):
                raise WarehouseError('Отгрузка изменилась после сопоставления. Проверьте Excel повторно.',409)
            versions[document_id]=[doc['version'],doc['revision']]
            allocated=[]
            for items in units:
                try:allocated.append((items,*allocate_items(doc,items)))
                except WarehouseError as exc:
                    for item in items:item['reason']=exc.message
            cises=list(dict.fromkeys(s['cis'] for _,allocation,_ in allocated for group in allocation for s in group))
            try:
                if cises:
                    async with asyncio.timeout(180):
                        declarations=await service.lookup_gtd_batch(cises,doc['org_inn'],allow_missing=True)
                    for items,allocation,rebuilt in allocated:
                        await asyncio.to_thread(apply_declarations,book,items,allocation,rebuilt,declarations)
            except (AppError,TimeoutError) as exc:
                for items,_,_ in allocated:
                    for item in items:item['reason']=str(exc) or 'Истекло время получения ГТД'
            progress+=1;store.gtd_excel_progress(job_id,progress,len(documents))
        output=await asyncio.to_thread(export_book,book,rows)
        public=[{k:v for k,v in item.items() if k in {'sheet','row','number','pn','before','after','status','reason','changed','match_source','warehouse_number','warehouse_date'}} for item in rows]
        report=dict(rows=public,total=len(rows),changed=sum(i['status']=='changed' for i in rows),
                    confirmed=sum(i['status']=='confirmed' for i in rows),unconfirmed=sum(i['status']=='unconfirmed' for i in rows))
        store.gtd_excel_finish(job_id,output,report,versions)
    except asyncio.CancelledError:
        store.gtd_excel_fail(job_id,'Обработка прервана. Загрузите файл повторно.');raise
    except Exception as exc:
        if not isinstance(exc,WarehouseError):logging.getLogger(__name__).exception('GTD Excel job failed: %s',job_id)
        store.gtd_excel_fail(job_id,exc.message if isinstance(exc,WarehouseError) else 'Не удалось обработать Excel. Проверьте файл и повторите загрузку.')


def read_catalog(content):
    book=load_book(content)
    mapping={}
    try:
        for sheet in book:
            if sheet.max_row>10001 or sheet.max_column>200:raise WarehouseError('Справочник должен содержать до 10000 строк')
            columns={str(cell.value).strip().lower():cell.column for cell in sheet[1] if cell.value is not None}
            if not {'goodid','gtin'}<=columns.keys():continue
            for n in range(2,sheet.max_row+1):
                id_cell,gtin_cell=sheet.cell(n,columns['goodid']),sheet.cell(n,columns['gtin'])
                if id_cell.value is None and gtin_cell.value is None:continue
                try:
                    if id_cell.data_type=='f':raise ValueError('Формулы в GoodID не поддерживаются')
                    good_id=identifier(id_cell.value)
                    if not good_id or len(good_id)>100:raise ValueError('Некорректный GoodID')
                    gtin=shipment_gtin_cell(gtin_cell)
                    if not gtin:raise ValueError('Не заполнен GTIN')
                    if good_id in mapping and mapping[good_id]!=gtin:raise ValueError('Один GoodID связан с разными GTIN')
                    mapping[good_id]=gtin
                except ValueError as exc:raise WarehouseError(f'{sheet.title}, строка {n}: {exc}') from exc
        if not mapping:raise WarehouseError('Нужны заголовки GoodID и GTIN в первой строке и хотя бы одна связь')
        return mapping
    finally:book.close()
