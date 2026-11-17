"""Independent acceptance sample of persisted deadline and process recovery."""
import json
from pathlib import Path
import tempfile

from restaurant_state import BASE, ROOT, RestaurantService, integrity, kill_at_boundary, make_env


def run():
    with tempfile.TemporaryDirectory(prefix='glazza-independent-pass-review-') as directory:
        directory=Path(directory);env=make_env(directory);start=BASE+123456
        first=kill_at_boundary(directory,'arrival_committed',start)
        original=env['store'].rows('SELECT * FROM restaurant_visits')[0]
        assert original['deadline_at']-original['arrived_at']==120
        restarted=RestaurantService(env['store'],env['settings'])
        restarted.tick(now=start+119.5)
        assert not env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation'")
        second=kill_at_boundary(directory,'deadline_committed',start+120)
        recovered=RestaurantService(env['store'],env['settings'])
        recovered.tick(now=start+121);recovered.tick(now=start+122)
        after=env['store'].rows('SELECT * FROM restaurant_visits')[0]
        alerts=env['store'].rows("SELECT id FROM restaurant_alerts WHERE kind='escalation'")
        assert after['id']==original['id'] and after['deadline_at']==original['deadline_at']
        assert after['escalated_at']==start+120 and len(alerts)==1
        integrity(env['store'])
        return {'state':'verified','environment':'Fresh independent temporary DB, real SIGKILL at committed boundaries, controlled timestamps',
                'sigkill_events':[first,second],'deadline_offset_seconds':after['deadline_at']-after['arrived_at'],
                'escalations_at_119_5_seconds':0,'escalations_after_120_seconds_and_restarts':len(alerts),
                'same_visit_id_after_restart':after['id']==original['id'],'integrity_check':'ok'}


if __name__=='__main__':
    report=run()
    output=ROOT/'docs/audits/restaurant-stability-2026-11-17/evidence/verification/state-review-passes.json'
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
