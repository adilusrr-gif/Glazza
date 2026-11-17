"""Assemble the evidence-backed restaurant audit without a quality-score claim.

This is a report assembler, not a substitute implementation of Project Sentinel's
unavailable scoring runtime. It validates local evidence and preserves raw reports.
"""
import copy
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
RUN=ROOT/'docs/audits/restaurant-stability-2026-11-17'


def read(name):return json.loads((RUN/name).read_text())
def write(name,value):(RUN/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def readable(text):
    text=text.replace('одинподход','один подход').replace('многосуточныйпилот','многосуточный пилот').replace('HTTPpool','HTTP pool')
    text=re.sub(r'(?<=\d)(?=[А-Яа-яЁё])|(?<=[А-Яа-яЁё])(?=\d)',' ',text)
    text=re.sub(r'(?<=[А-Яа-яЁё])(?=[A-Za-z])|(?<=[A-Za-z])(?=[А-Яа-яЁё])',' ',text)
    text=re.sub(r'(?<=\d)(?=[A-Z]{2,})|(?<=[A-Z])(?=\d)',' ',text)
    text=re.sub(r'(?<=\d)(?=[A-Za-z])|(?<=[a-oq-zA-OQ-Z])(?=\d)',' ',text)
    text=re.sub(r'(?<=[а-яё])(?=[А-ЯЁ]{2,})',' ',text)
    text=re.sub(r'([,;])(?=[^\s\d])',r'\1 ',text)
    text=re.sub(r';(?=\S)','; ',text)
    text=re.sub(r'\s*([×→])\s*',r' \1 ',text)
    return (text.replace('sqlite 3', 'sqlite3').replace('productionHTTP', 'production HTTP')
            .replace('NodeVM', 'Node VM').replace('DockerCI', 'Docker CI')
            .replace('бездубликатов', 'без дубликатов'))
def readable_markdown(text):
    parts=re.split(r'(`[^`]*`|\]\([^)]*\)|https?://\S+)',text)
    return ''.join(part if index%2 else readable(part) for index,part in enumerate(parts))
class ReadableHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=False);self.parts=[];self.protected=0
    def handle_starttag(self,tag,attrs):
        self.parts.append(self.get_starttag_text())
        if tag in ('pre','code','style','script'):self.protected+=1
    def handle_endtag(self,tag):
        self.parts.append('</'+tag+'>')
        if tag in ('pre','code','style','script'):self.protected-=1
    def handle_data(self,data):self.parts.append(data if self.protected else readable(data))
    def handle_entityref(self,name):self.parts.append('&'+name+';')
    def handle_charref(self,name):self.parts.append('&#'+name+';')
    def handle_decl(self,decl):self.parts.append('<!'+decl+'>')
def check_artifact(name):
    p=(RUN/name).resolve()
    assert p.is_relative_to(RUN.resolve()) and p.is_file(), f'Missing evidence: {name}'
    return sha(p)
def validate_location(loc):
    if not loc:return
    path=(ROOT/loc['file']).resolve()
    assert path.is_relative_to(ROOT) and path.is_file()
    lines=path.read_text().splitlines()
    start,end=loc['line'],loc.get('end_line',loc['line'])
    assert 1<=start<=end<=len(lines)
    for related in loc.get('related',[]):
        file,number=related.rsplit(':',1)
        assert 1<=int(number)<=len((ROOT/file).read_text().splitlines())
    return {'file':loc['file'],'line':start,'end_line':end,'source_sha256':sha(path),'exact_source':'\n'.join(lines[start-1:end])}


def main():
    manifest=read('manifest.json');inventory=read('inventory.json')
    reports=[json.loads(p.read_text()) for p in sorted((RUN/'inbox').glob('*.json'))]
    assert {'root-stability-review','restaurant-transport-resilience'} & {d['agent'] for d in reports}
    surfaces={s['id']:s for s in inventory['surfaces']}
    for report in reports:
        for s in report.get('discovered_surfaces',[]):
            if s['id'] not in surfaces:surfaces[s['id']]=s
    inventory.update(surfaces=list(surfaces.values()),surface_count=len(surfaces),
                     denominator_note='29 initial source-derived surfaces +1 discovered staff-scope service boundary; domains remain explicit. Tested means at least one evidenced variant, not exhaustive contract coverage.')
    write('inventory.json',inventory)
    observations=defaultdict(list)
    for report in reports:
        for item in report.get('coverage',[]):
            assert item['surface_id'] in surfaces,item['surface_id']
            assert item['domain'] in surfaces[item['surface_id']]['domains'],(item['surface_id'],item['domain'])
            if item['status'] in ('passed','failed'):assert item['evidence_refs']
            for ref in item.get('evidence_refs',[]):check_artifact(ref)
            observations[(item['surface_id'],item['domain'])].append({'agent':report['agent'],**item})
    ranking={'failed':4,'blocked':3,'passed':2,'not_applicable':1,'untested':0}
    coverage=[]
    for surface in surfaces.values():
        for domain in surface['domains']:
            entries=observations.get((surface['id'],domain),[])
            status=max((x['status'] for x in entries),key=ranking.get,default='untested')
            coverage.append({'surface_id':surface['id'],'domain':domain,'status':status,'observations':entries})
    pairs=Counter(x['status'] for x in coverage)
    per_surface={s:max((x['status'] for x in coverage if x['surface_id']==s),key=ranking.get) for s in surfaces}
    surface_counts=Counter(per_surface.values())
    counts={'surfaces_discovered':len(surfaces),'surfaces_tested':surface_counts['passed']+surface_counts['failed'],
            'surfaces_passed':surface_counts['passed'],'surfaces_failed':surface_counts['failed'],
            'surfaces_blocked':surface_counts['blocked'],'surfaces_untested':surface_counts['untested'],
            'surface_domain_pairs':len(coverage),'pairs_tested':pairs['passed']+pairs['failed'],
            'pairs_passed':pairs['passed'],'pairs_failed':pairs['failed'],'pairs_blocked':pairs['blocked'],'pairs_untested':pairs['untested'],
            'api_endpoints_exercised':len({x['surface_id'] for x in coverage if x['domain']=='api' and x['status'] in ('passed','failed')}),
            'api_endpoints_discovered':len([s for s in surfaces if s.startswith('api:')])}
    domain_counts={}
    for domain in sorted({x['domain'] for x in coverage}):
        dc=Counter(x['status'] for x in coverage if x['domain']==domain)
        domain_counts[domain]={'discovered':sum(dc.values()),'tested':dc['passed']+dc['failed'],**dict(dc)}
    raw=[]
    for report in reports:
        for f in report.get('findings',[]):
            f=copy.deepcopy(f);f['source_agent']=report['agent'];raw.append(f)
    write('raw-findings.json',{'schema_version':'1.0','findings':raw})
    by_key=defaultdict(list)
    for f in raw:by_key[f['root_cause_key']].append(f)
    assert all(len(group)==1 for group in by_key.values()),'Shared-cause review required; do not merge automatically here'
    write('finding-resolution.json',{'schema_version':'1.0','method':'Manual causal review, no cross-agent duplicates; UI GET/body/POST symptoms already grouped under one unbounded-request cause. Queue priority and retry_after require separate remedies.',
         'raw_count':len(raw),'canonical_count':len(raw),'duplicates_collapsed':0,'review_required':[],
         'decisions':[{'finding_id':f['client_id'],'source_ids':[f['client_id']],'root_cause_key':f['root_cause_key'],'decision':'keep_separate','reason':'Distinct control and regression test.'} for f in raw]})
    verification=read('evidence/verification/state-review.json')
    verification['verifier']='Independent specialists and root; original observations preserved'
    verification['verifications'].append({'finding_id':'REST-MQTT-001','state':'verified','method':'reproduced',
      'evidence':['evidence/verification/root-mqtt.json'],'notes':'Separate broker/topic and60001-byte depth30000 payload; dead Paho thread, connected flag true, valid next snapshot ignored. Final TTL check uses+16virtual seconds.'})
    verified={x['finding_id']:x for x in verification['verifications']}
    findings=[]
    domain_map={'REST-MQTT-001':['functional','performance'],'REST-TG-002':['performance'],'REST-TG-003':['performance'],
                'ROOT-ALERT-001':['api','functional'],'ROOT-UI-001':['functional'],'ROOT-LOOP-001':['performance','architecture','code_quality']}
    for f in raw:
        f=copy.deepcopy(f);f['id']=f['client_id'];f['domains']=domain_map[f['id']]
        f['violation_example']=validate_location(f.get('location'))
        f['evidence_hashes']={e['artifact']:check_artifact(e['artifact']) for e in f.get('evidence',[]) if e.get('artifact')}
        f['evidence_grade']='A' if f['id'] in verified else 'B'
        f['verification']=verified.get(f['id'],{'finding_id':f['id'],'state':'not_attempted','notes':'Reproducible primary runtime evidence exists; no additional independent runtime replay.'})
        f['claim_integrity']={'observation':'independently_verified' if f['id'] in verified else 'source_and_artifact',
                              'recommendation':'grounded','fix_applied':False,'fix_examples':'none'}
        if f['id'] not in verified:verification['verifications'].append(f['verification'])
        findings.append(f)
    for v in verification['verifications']:
        for ref in v.get('evidence',[]):
            assert ref.startswith('evidence/verification/');check_artifact(ref)
    findings.sort(key=lambda f:(0 if f['severity']=='high' else 1,f['id']))
    write('verification.json',verification)
    load=read('evidence/load-results.json');connections=read('evidence/load-connections.json')
    acceptance=read('evidence/load-acceptance.json');transport=read('inbox/transport.json')['metrics']
    loop=read('evidence/loop-stall.json')['observed']
    metrics={'baseline_tests_passed':211,'baseline_duration_s':23.08,'load_total_requests':acceptance['measured_product_http_requests'],
             'load_unexpected_responses':acceptance['unexpected_responses'],'soak_duration_s':load['duration_s'],'soak_requests':load['soak_requests'],
             'tables':60,'simulated_cameras':3,'virtual_clients':24,'soak_groups':load['soak_groups'],
             'independent_pools':{k:connections[k] for k in ['clients','connection_pools','duration_s','requests','latency']},
             'burst':load['burst'],'deadline_lag_s':load['deadline_lag_s'],'soak_logic':load['logical'],
             'rss_soak_start_bytes':load['resources'][0]['rss_bytes'],'rss_soak_last_sample_bytes':load['resources'][-1]['rss_bytes'],
             'after_burst_resources':load['final_resource_after_benchmarks'],'state_real_sigkills':28,'state_virtual_hours':72,
             'state_virtual_visits':3000,'concurrent_action_rounds':25,'concurrent_actions_per_round':12,'transport':transport,'loop_stall':loop}
    limitations=[
      '10 минут реальной нагрузки и72 часа ускоренного логического времени — разные проверки. Многосуточная непрерывная эксплуатация не подтверждена.',
      'Реальная физическая камера, обработка видео, Frigate/Mosquitto контейнеры, нейросетевой инференс и целевой сервер ресторана не участвовали. Эти измерения не определяют конфигурацию сервера для видеонаблюдения.',
      'MQTT использует настоящий локальный AMQTT брокер и производственный Paho клиент, но публикации камеры синтетические. Telegram использует HTTP mock; настоящие сообщения не отправлялись.',
      'Основной soak:24виртуальных клиента с общимHTTPpool. Отдельная проверка:24независимыхpool/120с. Общий вычислительный хост и параллельные тесты ограничивают переносимость latency.',
      'Проверены реальные SIGKILL процессов и восстановлениеSQLite; отключение питания, заполнение/повреждение диска, сетевое хранилище не проверялись.',
      'Новый faulttest интерфейса выполнен в NodeVM. Успешный Chromium/Compose/DockerCI от14ноября относится к идентичному исходному коммиту и отмечен как историческое свидетельство, не как новый браузерный soak.',
      'Покрытие endpoint означает выполненные перечисленные варианты; полная матрица всех ролей, неверных payload и браузеров не заявляется. Непроверенные пары поверхность×домен сохранены в inventory/report.json.',
      'Исправлений продуктового кода нет. Рекомендации не применены и не проверены как исправления.',
      'Project Sentinel CLI/официальный расчёт баллов отсутствует в установленном пакете. Артефакты собраны по evidence-contract; канонический числовой балл не рассчитывался.',
      'Это аудит стабильности ресторанного модуля, а не новая полная security/CVE-проверка или аудит всех моделей Glazza.'
    ]
    verdict={'overall':'INCOMPLETE','scope':'Production acceptance cannot be concluded: physical environment absent and role/domain variants remain untested.',
             'laboratory':'CONDITIONAL GO','laboratory_scope':'Normal synthetic workload and durable state passed;1high and5medium confirmed defects remain.',
             'deployment_recommendation':'До исправлений не рекомендован автономный круглосуточный запуск. Контролируемый технический пилот возможен с явными ограничениями.',
             'canonical_quality_score':None}
    report={'schema_version':'1.0','project':'Glazza','target_commit':manifest['target_commit'],
            'generated_at':datetime.now(timezone.utc).isoformat(),'verdict':verdict,'coverage_counts':counts,'domain_counts':domain_counts,
            'findings':findings,'coverage':coverage,'metrics':metrics,'limitations':limitations,'source_reports':[d['agent'] for d in reports]}
    write('report.json',report)
    write('knowledge-snapshot.json',{'as_of':'2026-11-17','scope':'Exact tested code/runtime and one provider retry contract; not a dependency-advisory refresh.',
          'runtime':read('evidence/environment.json'),'references':[{'url':'https://core.telegram.org/bots/api#responseparameters',
          'retrieved_on':'2026-11-17','claim':'retry_after задаёт число секунд ожидания до повторения запроса после ограничения частоты.',
          'relevance':'REST-TG-002','artifact':'evidence/transport-source-telegram.json'}],
          'not_performed':['Current vulnerability-advisory audit','Production model validation','Price or cost forecasts'],
          'runtime_limitation':manifest['runtime_limitation']})
    write('quality-graph.json',{'schema_version':'1.0','nodes':[
      {'id':s['id'],'file':s.get('file'),'domains':s['domains']} for s in surfaces.values()],
      'relationships':[
        {'from':'integration:restaurant:mqtt','to':'integration:restaurant:camera','type':'supplies_current_observations'},
        {'from':'integration:restaurant:camera','to':'flow:restaurant:seating','type':'advances_durable_state'},
        {'from':'job:restaurant:tick','to':'flow:restaurant:deadline','type':'evaluates_deadline'},
        {'from':'flow:restaurant:deadline','to':'integration:restaurant:telegram','type':'queues_optional_external_delivery'},
        {'from':'architecture:restaurant:sqlite','to':'job:restaurant:tick','type':'shared_writer_lock_boundary'},
        {'from':'api:GET:/api/restaurant/portal','to':'ui:restaurant:staff','type':'polling_response'},
        {'from':'business:restaurant:handoff','to':'flow:restaurant:departure','type':'manual_cleaning_confirmation_required'}]})
    # Existing211 cases remain raw; supplemental failures are distinct confirmed causes.
    junit_root=ET.parse(RUN/'evidence/baseline-junit.xml').getroot()
    if junit_root.tag!='testsuites':
        container=ET.Element('testsuites');container.append(junit_root);junit_root=container
    suite=ET.SubElement(junit_root,'testsuite',name='restaurant_stability_findings',tests=str(len(findings)),failures=str(len(findings)),errors='0')
    for f in findings:
        case=ET.SubElement(suite,'testcase',classname='stability.confirmed_findings',name=f['id'])
        failure=ET.SubElement(case,'failure',type=f['severity'],message=f['title']);failure.text=f['impact']+'\n'+f['recommendation']
    ET.indent(junit_root);ET.ElementTree(junit_root).write(RUN/'junit.xml',encoding='utf-8',xml_declaration=True)
    sarif={'version':'2.1.0','$schema':'https://json.schemastore.org/sarif-2.1.0.json','runs':[{
      'tool':{'driver':{'name':'Glazza restaurant stability audit','rules':[{'id':f['id'],'shortDescription':{'text':f['title']},'help':{'text':f['recommendation']}} for f in findings]}},
      'results':[{'ruleId':f['id'],'level':'error' if f['severity']=='high' else 'warning','message':{'text':f['impact']},
       'locations':[{'physicalLocation':{'artifactLocation':{'uri':f['location']['file']},'region':{'startLine':f['location']['line'],'endLine':f['location'].get('end_line',f['location']['line'])}}}],
       'properties':{'evidenceGrade':f['evidence_grade'],'rootCause':f['root_cause_key'],'status':'confirmed','fixed':False}} for f in findings]}]}
    write('report.sarif',sarif)
    # Human reports share the same evidence-derived values and source links.
    commit=manifest['target_commit'];sourcebase=f'https://github.com/adilusrr-gif/Glazza/blob/{commit}/'
    rows=[
      ('Базовые тесты','211 passed;23,08с;1upstream deprecation warning','evidence/baseline-pytest.log'),
      ('Непрерывная нагрузка',f"{load['duration_s']:.3f}с;60столов;24виртуальных клиента;{load['soak_requests']}запросов;0ошибок",'evidence/load-results.json'),
      ('Все нагрузочные HTTP-запросы',f"{acceptance['measured_product_http_requests']};всеHTTP200;логические инварианты проверены отдельно",'evidence/load-acceptance.json'),
      ('Обычный персональный экран',f"p95={load['soak_groups']['staff_read']['latency']['p95_ms']:.3f}мс;общийHTTPpool",'evidence/load-results.json'),
      ('24независимых соединения',f"{connections['duration_s']:.3f}с;{connections['requests']}запросов;p95={connections['latency']['p95_ms']:.3f}мс",'evidence/load-connections.json'),
      ('Burst50 /100',f"p95={load['burst'][0]['latency']['p95_ms']/1000:.3f} /{load['burst'][1]['latency']['p95_ms']/1000:.3f}с;0HTTPошибок;скорость требует ограничения нагрузки",'evidence/load-results.json'),
      ('Двухминутный дедлайн',f"60просроченных визитов;максимальное сохранённое опоздание{load['deadline_lag_s']['max']:.3f}с",'evidence/load-soak-runtime.json'),
      ('Визиты под нагрузкой','180визитов;420адресных задач;0дубликатов;SQLite integrity=ok','evidence/load-soak-runtime.json'),
      ('Память API',f"{metrics['rss_soak_start_bytes']/1e6:.3f}→{metrics['rss_soak_last_sample_bytes']/1e6:.3f}MB во времяsoak;послеburst132,448MB/41поток",'evidence/load-results.json'),
      ('Аварийное завершение','28реальныхSIGKILL;28integrity_check=ok;дедлайны и визиты сохранены','evidence/state-results.json'),
      ('Ускоренная история','3000завершённых визитов за72виртуальных часа;это не72часа непрерывной работы','evidence/state-results.json'),
      ('Параллельные действия','25раундов×12операций;одинподход,устаревшие ревизии отклонены','evidence/state-results.json'),
      ('Перезапуски MQTT',f"8реальных перезапусков;восстановление{transport['broker_recovery_min_seconds']:.3f}–{transport['broker_recovery_max_seconds']:.3f}с",'evidence/transport-mqtt.json'),
      ('Медленная внешняя доставка',f"Менеджер ждёт{transport['manager_delivery_lag_seconds']:.3f}с после in-appэскалации;сам tick опоздал лишь{transport['deadline_lag_seconds']:.5f}с",'evidence/transport-delivery.json')]
    intro=f"# Glazza: проверка стабильности ресторана\n\nДата:17 ноября2026. Проверенный коммит:`{commit}`. Продуктовый код не изменён.\n\n**Результат: рабочая реализация есть; автономный запуск24/7 пока не рекомендован.** Нормальная лабораторная нагрузка и восстановление данных прошли; выявлены6подтверждённых причин сбоев:1HIGH и5MEDIUM.\n\n**Статус промышленной приёмки: INCOMPLETE.** Нет реального объекта/видеопотока и полного покрытия всех вариантов доступа. **Лабораторный статус: CONDITIONAL GO** с обязательным исправлением подтверждённых дефектов перед автономной эксплуатацией. Числовой балл качества не рассчитывался.\n\n"
    # Insert normal spaces between adjacent numeric tokens and prose in authored summaries.
    intro=intro.replace('Дата:17','Дата: 17').replace('ноября2026','ноября 2026').replace('запуск24/7','запуск 24/7').replace('выявлены6','выявлены 6 ').replace(':1HIGH и5MEDIUM',': 1 HIGH и 5 MEDIUM').replace('коммит:`','коммит: `')
    md=intro+'## Что измерено\n\n| Проверка | Результат | Доказательство |\n|---|---|---|\n'
    for label,value,ref in rows:md+=f'| {label} | {value} | [{Path(ref).name}]({ref}) |\n'
    md+='\np95 — значение, быстрее которого завершились95% запросов в указанном сценарии. Ни100HTTP200, ни211зелёных тестов не отменяют обнаруженные аварийные сбои. Подход официанта и уборка по-прежнему подтверждаются сотрудником; распознавание этих действий по видео не заявляется.\n\n'
    md+=f"## Покрытие\n\nИнвентаризировано **{counts['surfaces_discovered']} поверхностей** ресторанного модуля. Выполнен хотя бы один проверяемый вариант для **{counts['surfaces_tested']}/{counts['surfaces_discovered']}**: {counts['surfaces_passed']} без найденного сбоя, {counts['surfaces_failed']} с подтверждённым сбоем; {counts['surfaces_blocked']} заблокирована отсутствием физической камеры. Все **{counts['api_endpoints_exercised']}/{counts['api_endpoints_discovered']} HTTP-endpoint** получили реальные тестовые вызовы, но не все комбинации ролей/ошибок входа.\n\nПо более строгому знаменателю «поверхность × домен»: **{counts['pairs_tested']}/{counts['surface_domain_pairs']}** выполнено, {counts['pairs_failed']} failed, {counts['pairs_blocked']} blocked, {counts['pairs_untested']} untested. Полная карта и сохранённые противоположные наблюдения находятся в [report.json](report.json), [inventory.json](inventory.json) и [API-map](evidence/verification/baseline-api-map.json).\n\n"
    md+='Базовый набор покрывает 11 из 12 endpoint. Дополнительный успешный вызов административного подтверждения уведомления и повторное подтверждение проверены в [alert-visibility.json](evidence/alert-visibility.json), раздел `additional_api_checks`.\n\n'
    md+='## Подтверждённые дефекты\n\n'
    for f in findings:
        loc=f['location'];url=sourcebase+loc['file']+'#L'+str(loc['line'])
        md+=f"### {f['id']} · {f['severity'].upper()} · {f['title']}\n\n**Где:** [{loc['file']}:{loc['line']}]({url}). **Доказательство:** уровень{f['evidence_grade']}; {f['claim_integrity']['observation']}.\n\n{f['impact']}\n\n**Причина:** {f['cause']}\n\n"
        for e in f['evidence']:
            if e.get('artifact'):md+=f"- [{e['artifact']}]({e['artifact']}): {e.get('observed','')}\n"
        md+=f"\n**Исправление:** {f['recommendation']}\n\n**Регрессия:** {f['regression_test']}\n\n"
    md+='## Ограничение нагрузки\n\nПри100одновременных полных административных снимках p95 достиг8,361с, максимум8,751с. Это не измерение100телефонов в обычном режиме. Причина не установлена профилированием; нельзя объявлять достаточным ресурс сервера только по отсутствиюHTTP500. В персональном интерфейсе порог устаревания равен8с (`app/static/staff.js:47`); пересечение такого порога — риск, но персональный экран одновременно с даннымburst не измерялся.\n\n'
    md+='## Порядок исправления\n\n1. Защитить обработчикMQTT от завершения потока на некорректном входе и проверять восстановление валидным следующим пакетом.\n2. Разделить срочную первую отправку менеджеру и повторы старых ошибок; уважать `retry_after` провайдера.\n3. Ограничить запросы интерфейса по времени, включая чтение тела; после неизвестного результатаPOST перечитывать состояние.\n4. Выдавать открытые задачи независимо от закрытой истории.\n5. Убрать синхронное ожиданиеSQLite из event loop; затем повторить нагрузку и аварийные сценарии.\n6. На целевом сервере провести24–72часа реального пилота с камерой и рабочим каналом уведомлений; измерять пропущенные посадки, ложные вызовы и полную задержку до телефона. Это план проверки, не уже полученный результат.\n\n'
    md+='## Границы вывода\n\n'+'\n'.join('- '+x for x in limitations)+'\n\n'
    md+='## Воспроизведение и машинные форматы\n\nКоманды и зависимости: [tests/stability/README.md](../../../..//tests/stability/README.md). Полные исходные результаты — в `evidence/`, исходные заключения — в `inbox/`. [SARIF](report.sarif), [JUnit](junit.xml), [JSON](report.json), [независимая проверка](verification.json), [решения по причинам](finding-resolution.json). ВJUnit сохранены211базовыхPASS и добавлены6отдельныхFAIL подтверждённых причин; это не повторный запуск217обычныхunit-тестов.\n'
    md=md.replace('../../../..//tests/stability/README.md','../../../tests/stability/README.md')
    (RUN/'report.md').write_text(readable_markdown(md))
    executive=intro+f"## Ключевые числа\n\n- {acceptance['measured_product_http_requests']} измеренныхHTTP-запросов:0неожиданных ответов.\n- Основнойsoak:{load['duration_s']:.3f}с,60столов,24виртуальных пользователя.\n- 28SIGKILL без нарушенияSQLite и потери дедлайна.\n- 3000визитов/72виртуальных часа.\n- 100одновременных полных панелей:p95{load['burst'][1]['latency']['p95_ms']/1000:.3f}с.\n- Внешнее сообщение менеджеру в аварийном сценарии задержалось на{transport['manager_delivery_lag_seconds']:.3f}с.\n\n## Что исправлять\n\n"+'\n'.join(f"- {f['severity'].upper()} · {f['id']}: {f['title']}" for f in findings)+f"\n\n## Покрытие\n\n{counts['surfaces_tested']}/{counts['surfaces_discovered']} поверхностей имеют выполненные варианты; {counts['pairs_tested']}/{counts['surface_domain_pairs']} пар поверхность×домен проверены. Это не процент точности камеры.\n\n## Ограничения\n\n10минут не заменяют многосуточныйпилот. Видео/инференс/рабочийTelegram исключены. Подход и уборка подтверждаются вручную. Исправления не внесены. [Полный отчёт](report.md).\n"
    executive=executive.replace('запросов:0', 'запросов: 0').replace('soak:', 'soak: ').replace('панелей:p95', 'панелей: p95 = ')
    executive=executive.replace('с,60', 'с, 60').replace('столов,24', 'столов, 24')
    (RUN/'executive-summary.md').write_text(readable_markdown(executive))
    esc=html.escape
    def table(headers,table_rows):
        return '<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>' for row in table_rows)+'</tbody></table></div>'
    parts=['<h1>Glazza · стабильность ресторана</h1>',f'<p class="meta">17 ноября2026 · commit <code>{commit}</code> · продуктовый код не изменён</p>',
           '<div class="verdict"><strong>К автономному запуску24/7 пока не готово.</strong><p>INCOMPLETE для промышленной приёмки. Лабораторный статус:CONDITIONAL GO. Базовая нагрузка и восстановление данных прошли; остаются1HIGH и5MEDIUM.</p></div>',
           '<div class="cards">'+''.join(f'<div><strong>{esc(v)}</strong><span>{esc(k)}</span></div>' for k,v in [('Реальная нагрузка','601,577с'),('HTTP-запросы','10 398'),('Подтверждённые причины','6'),('Аварийные завершения','28')])+'</div>',
           '<h2>Измерения</h2>',table(['Проверка','Результат','Источник'],[[esc(a),esc(b),f'<a href="{esc(c)}">{esc(Path(c).name)}</a>'] for a,b,c in rows]),
           '<h2>Покрытие</h2>',f"<p>{counts['surfaces_tested']}/{counts['surfaces_discovered']} поверхностей имеют выполненный вариант; {counts['surfaces_failed']} со сбоями;{counts['surfaces_blocked']} blocked. По доменам: {counts['pairs_tested']}/{counts['surface_domain_pairs']} пар проверены. Все12API-endpoint получили вызовы; это не полное покрытие контрактов и всех ролей.</p>",
           table(['Домен','Обнаружено','Проверено','Passed','Failed','Blocked','Untested'],[[esc(k)]+[str(v.get(key,0)) for key in ['discovered','tested','passed','failed','blocked','untested']] for k,v in domain_counts.items()]),
           '<p>Базовый набор покрывает 11 из 12 endpoint; административное подтверждение уведомления дополнительно проверено в <a href="evidence/alert-visibility.json">alert-visibility.json</a>, раздел <code>additional_api_checks</code>.</p>',
           '<h2>Подтверждённые дефекты</h2>']
    for f in findings:
        loc=f['location'];url=sourcebase+loc['file']+'#L'+str(loc['line'])
        parts+=['<article class="finding">',f'<p class="severity {f["severity"]}">{esc(f["severity"].upper())} · {esc(f["id"])} · evidence {f["evidence_grade"]}</p>',
                '<h3>'+esc(f['title'])+'</h3>','<p>'+esc(f['impact'])+'</p>',
                f'<p><strong>Источник:</strong> <a href="{esc(url)}">{esc(loc["file"])}:{loc["line"]}</a></p>',
                '<p><strong>Причина:</strong> '+esc(f['cause'])+'</p>',
                '<ul>'+''.join(f'<li><a href="{esc(e["artifact"])}">{esc(e["artifact"])}</a> · {esc(str(e.get("observed","")))}</li>' for e in f['evidence'] if e.get('artifact'))+'</ul>',
                '<p><strong>Исправление:</strong> '+esc(f['recommendation'])+'</p>',
                '<p><strong>Проверка исправления:</strong> '+esc(f['regression_test'])+'</p>',
                '<details><summary>Точный исходник</summary><pre>'+esc(f['violation_example']['exact_source'])+'</pre></details>','</article>']
    parts+=['<h2>Границы результата</h2>','<ul>'+''.join('<li>'+esc(x)+'</li>' for x in limitations)+'</ul>',
            '<p>Предельная нагрузка:100одновременных полных административных снимков дали p95=8,361с. Это предупреждение о запасе по нагрузке, а не заявленный приемлемыйSLA. Необходимо исправить найденные сбои и затем пройти24–72часа реального пилота.</p>',
            '<h2>Файлы</h2><p><a href="report.md">Полный Markdown</a> · <a href="report.json">JSON</a> · <a href="report.sarif">SARIF</a> · <a href="junit.xml">JUnit</a> · <a href="verification.json">Независимая проверка</a> · <a href="inventory.json">Инвентарь</a></p>']
    style='body{font:16px/1.6 system-ui,sans-serif;color:#17212b;background:#f2f5f6;margin:0}main{max-width:1160px;margin:auto;padding:40px 24px}h1{font-size:34px;line-height:1.2}h2{margin-top:36px}h3{font-size:21px}.meta{color:#52616d}a{color:#145f91;overflow-wrap:anywhere}.verdict{background:#fff2da;border-left:5px solid #bf7914;padding:20px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.cards div,.finding{background:white;border:1px solid #dbe3e7;padding:20px;border-radius:10px}.cards strong{display:block;font-size:28px}.cards span{font-size:14px;color:#52616d}.table-wrap{overflow-x:auto;background:white;border:1px solid #dbe3e7;border-radius:8px}table{border-collapse:collapse;width:100%}th,td{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #dbe3e7}th{background:#e7edf0}.finding{margin:20px 0}.severity{font-weight:700;font-size:13px;color:#8e4d03}.severity.high{color:#a72a2a}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f4f6;padding:16px;font-size:13px}li{margin:8px 0}details summary{cursor:pointer}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:24px 14px}h1{font-size:28px}}'
    document='<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Glazza — проверка стабильности ресторана</title><style>'+style+'</style></head><body><main>'+''.join(parts)+'</main></body></html>'
    formatter=ReadableHTML();formatter.feed(document);formatter.close();document=''.join(formatter.parts)
    (RUN/'report.html').write_text(document)
    parser=HTMLParser();parser.feed(document);parser.close()
    for name in ['executive-summary.md','report.md','report.html','report.json','report.sarif','junit.xml','inventory.json','quality-graph.json','knowledge-snapshot.json','raw-findings.json','finding-resolution.json','verification.json']:assert (RUN/name).is_file()
    evidence_hashes={str(p.relative_to(RUN)):sha(p) for p in sorted((RUN/'evidence').rglob('*')) if p.is_file()}
    write('evidence-index.json',{'target_commit':commit,'algorithm':'SHA-256','files':evidence_hashes})
    print(json.dumps({'findings':len(findings),'severity':dict(Counter(f['severity'] for f in findings)),'coverage':counts,'evidence_files':len(evidence_hashes),'verdict':verdict},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
