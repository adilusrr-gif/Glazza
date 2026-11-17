"""Reproduce whether closed history can displace a live manager task.

All visits are generated through the real domain service and read through its
HTTP portal. Time is accelerated; there is no camera or external messaging.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.restaurant import TableAction


def run():
    with tempfile.TemporaryDirectory(prefix='glazza-alert-visibility-') as directory:
        root = Path(directory)
        settings = Settings(data=root/'data', frigate_config=root/'config.yml',
                            frigate_url='http://unused.invalid', analyzer_url='http://unused.invalid',
                            username='admin', password='synthetic-visibility-password',
                            worker_key='synthetic-visibility-worker-'*3, demo=False, secure_cookie=False)
        app = create_app(settings, background=False)
        store, service = app.state.store, app.state.engine.restaurant
        store.execute("UPDATE sites SET modules=? WHERE id='default'", (json.dumps(['person','restaurant']),))
        clock = [1_800_000_000.0]
        with patch('app.restaurant._at', lambda now=None: clock[0] if now is None else float(now)), TestClient(app) as client:
            login = client.post('/api/login', json={'username':settings.username,'password':settings.password})
            assert login.status_code == 200
            client.headers['X-CSRF-Token'] = login.json()['csrf']
            assert client.post('/api/cameras',json={'id':'dining','name':'Synthetic dining','site_id':'default','stream':'rtsp://127.0.0.1:9/test','zones':[]}).status_code == 201
            staff = {}
            for role in ['waiter','manager']:
                response = client.post('/api/restaurant/staff',json={'site_id':'default','name':role,'role':role})
                assert response.status_code == 201
                staff[role] = response.json()
            tables = []
            for name in ['Unresolved old visit','Repeated completed visits']:
                response = client.post('/api/restaurant/tables',json={
                    'site_id':'default','camera':'dining','name':name,
                    'points':[[.1,.1],[.8,.1],[.8,.8],[.1,.8]],
                    'waiter_id':staff['waiter']['id'],'vacancy_seconds':10})
                assert response.status_code == 201
                tables.append(response.json())
            old, cycling = tables
            def observe(cycling_count, delta=0):
                clock[0] += delta
                service.observe('dining',{old['zone']:1,cycling['zone']:cycling_count},available=True,now=clock[0])
                service.tick(now=clock[0])
            def current(identifier):
                return next(t for t in service.snapshot(now=clock[0])['tables'] if t['id']==identifier)
            def action(name):
                table = current(cycling['id'])
                service.action(cycling['id'], TableAction(action=name,visit_id=table['visit_id'],revision=table['revision']),actor='synthetic-admin',now=clock[0])

            observe(0);observe(0,5);observe(0,115)
            initial = service.snapshot(staff_id=staff['manager']['id'],now=clock[0])
            old_alert = next(a['id'] for a in initial['alerts'] if a['kind']=='escalation' and a['table_id']==old['id'])
            for _ in range(200):
                observe(1,1);observe(1,5);observe(1,115)
                action('greeted')
                observe(0,1);observe(0,10)
                action('cleaned')

            response=client.get('/api/restaurant/portal',headers={'Authorization':'Bearer '+staff['manager']['token']})
            assert response.status_code == 200
            portal=response.json()
            db_open=store.rows('SELECT id FROM restaurant_alerts WHERE recipient_id=? AND ack_at IS NULL AND resolved_at IS NULL',(staff['manager']['id'],))
            portal_open=[a for a in portal['alerts'] if a['ack_at'] is None and a['resolved_at'] is None]
            assert len(db_open)==1 and db_open[0]['id']==old_alert
            assert old_alert not in [a['id'] for a in portal['alerts']], 'Reproduction changed: open task is now visible'
            result={'environment':'Temporary SQLite + real FastAPI portal; synthetic service observations and virtual time',
                    'generated_visits':201,'closed_newer_manager_tasks':200,'http_status':response.status_code,
                    'observed':{'unresolved_manager_tasks_in_database':len(db_open),'returned_tasks':len(portal['alerts']),
                                'visible_unresolved_manager_tasks':len(portal_open),'old_task_present_in_response':False,
                                'old_table_overdue_still_visible':next(t['overdue'] for t in portal['tables'] if t['id']==old['id']),
                                'database_integrity':store.rows('PRAGMA integrity_check')[0]['integrity_check']},
                    'expected':'Open tasks remain visible regardless of newer completed history; history limits must not hide active obligations.',
                    'source':'app/restaurant.py:569'}
            acknowledged=client.post('/api/restaurant/alerts/'+old_alert+'/ack')
            acknowledged_again=client.post('/api/restaurant/alerts/'+old_alert+'/ack')
            deleted=client.delete('/api/restaurant/tables/'+cycling['id'])
            assert acknowledged.status_code==200 and acknowledged_again.status_code==200
            assert deleted.status_code==200
            result['additional_api_checks']={'admin_ack_status':acknowledged.status_code,
                'repeated_admin_ack_status':acknowledged_again.status_code,'delete_ready_table_status':deleted.status_code}
            return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    result=run();text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(text)
    print(text,end='')
