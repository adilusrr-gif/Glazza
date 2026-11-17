"""Independent boundary verification of live alert visibility at the history cap."""
from pathlib import Path
import json
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).parent))
from fastapi.testclient import TestClient
from app.main import create_app
from app.restaurant import TableAction
from restaurant_state import make_env, row, BASE


def run():
    with tempfile.TemporaryDirectory(prefix='glazza-independent-alert-review-') as directory:
        env=make_env(Path(directory),table_count=2)
        service=env['service'];old,cycle=env['tables'];clock=[BASE]
        def observe(occupancy,at):
            clock[0]=at
            service.observe('camera_default',{old['zone']:2,cycle['zone']:occupancy},available=True,now=at)
        def action(name,at):
            current=row(env,cycle['id'])
            service.action(cycle['id'],TableAction(action=name,visit_id=current['visit_id'],revision=current['revision']),'independent-review',now=at)
        observe(0,BASE);observe(0,BASE+5);service.tick(now=BASE+120)
        old_visit=row(env,old['id'])['visit_id']
        manager=env['staff']['default']['manager']
        old_alert=env['store'].rows("SELECT id FROM restaurant_alerts WHERE visit_id=? AND kind='escalation'",(old_visit,))[0]['id']
        # The app is separate from the producer service and uses the same temporary DB.
        app=create_app(env['settings'],background=False)
        checkpoints=[]
        with patch('app.restaurant._at',lambda now=None:clock[0] if now is None else float(now)),TestClient(app) as client:
            for i in range(203):
                start=BASE+200+i*250
                observe(2,start);observe(2,start+5)
                service.tick(now=start+120);action('greeted',start+121)
                for off in (130,140,150,160,170,180,190):observe(0,start+off)
                action('cleaned',start+190)
                if i+1 in [199,200,203]:
                    response=client.get('/api/restaurant/portal',headers={'Authorization':'Bearer '+manager['token']})
                    assert response.status_code==200
                    body=response.json()
                    returned=body['alerts'];live=[a for a in returned if a['ack_at'] is None and a['resolved_at'] is None]
                    database_live=env['store'].rows('SELECT id FROM restaurant_alerts WHERE recipient_id=? AND ack_at IS NULL AND resolved_at IS NULL',(manager['id'],))
                    assert [a['id'] for a in database_live]==[old_alert]
                    checkpoints.append({'newer_closed_escalations':i+1,'http_status':response.status_code,
                                        'database_live_tasks':len(database_live),'response_tasks':len(returned),
                                        'response_live_tasks':len(live),'old_alert_visible':any(a['id']==old_alert for a in returned),
                                        'old_table_overdue':next(t['overdue'] for t in body['tables'] if t['id']==old['id'])})
            assert checkpoints[0]['old_alert_visible'] is True
            assert checkpoints[1]['old_alert_visible'] is False
            assert checkpoints[2]['old_alert_visible'] is False
        return {'finding_id':'ROOT-ALERT-001','state':'verified','independent_method':'Fresh temporary DB;203 real service-generated closed manager escalations; HTTP portal checked at199,200,203 closed records',
                'source':'app/restaurant.py:569','checkpoints':checkpoints,
                'expected':'A live task remains visible at every checkpoint, independent of completed history count.',
                'boundary':'At199 newer closed alerts, the old live task is visible; at200 it disappears. The overdue table card remains visible.',
                'external_messages_sent':0}


if __name__=='__main__':
    result=run();output=ROOT/'docs/audits/restaurant-stability-2026-11-17/evidence/verification/state-review-alerts.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
