"""Independent reproduction of callback survival after malformed MQTT input.

Uses a separate ephemeral broker and an unmodified RestaurantFeed. No production
network, camera, or real notification provider is involved.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from amqtt.broker import Broker
import paho.mqtt.client as mqtt
from app.restaurant_feed import RestaurantFeed


class Recorder:
    latest=None
    health={}
    def configuration_tables(self):return [{'zone':'verify_seats','camera':'verify_camera'}]
    def observe(self,camera,counts,*,available,now):self.latest={'available':available,'counts':counts,'at':now}
    def set_health(self,value):self.health=value


async def wait_until(predicate,seconds=5):
    start=time.monotonic()
    while not predicate():
        if time.monotonic()-start>seconds:raise AssertionError('Independent verification setup timed out')
        await asyncio.sleep(.02)


async def run():
    broker=Broker({'listeners':{'default':{'type':'tcp','bind':'127.0.0.1:0'}},'sys_interval':0,
                   'plugins':{'amqtt.plugins.authentication.AnonymousAuthPlugin':{'allow_anonymous':True}}})
    await broker.start()
    port=broker._servers['default'].instance.sockets[0].getsockname()[1]
    publisher=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='independent-verifier')
    feed=RestaurantFeed(SimpleNamespace(mqtt_host='127.0.0.1',mqtt_port=port,mqtt_prefix='verify',demo=False))
    record=Recorder();feed.set_tables(record.configuration_tables())
    exceptions=[]
    old_hook=threading.excepthook
    def worker_alive():
        worker=feed.client._thread if feed.client else None
        return bool(worker and worker.is_alive())
    def capture(args):
        exceptions.append({'type':args.exc_type.__name__,'traceback':''.join(traceback.format_exception(args.exc_type,args.exc_value,args.exc_traceback))})
    threading.excepthook=capture
    def snapshot(count):
        return json.dumps({'verify_camera':{'config':{'enabled':True,'detect':True},'objects':[
            {'label':'person','current_zones':['verify_seats']} for _ in range(count)]}})
    try:
        publisher.connect_async('127.0.0.1',port);publisher.loop_start()
        await wait_until(publisher.is_connected)
        for topic,payload in [('available','online'),('verify_camera/enabled/state','ON'),('verify_camera/detect/state','ON'),('verify_camera/zone/verify_seats/state','ON')]:
            message=publisher.publish('verify/'+topic,payload,qos=1,retain=True)
            await asyncio.to_thread(message.wait_for_publish,2)
        feed.start()
        await wait_until(lambda:feed.connected and feed.online and feed.zone_enabled.get('verify_seats'))
        publisher.publish('verify/camera_activity',snapshot(1),qos=1)
        await wait_until(lambda:feed.counts.get('verify_seats')==1)
        feed.pump(record,lambda *args,**kwargs:{'signal':'online'})
        before={'worker_alive':worker_alive(),'observed':record.latest}
        bad='['*30000+'0'+']'*30000
        message=publisher.publish('verify/camera_activity',bad,qos=1)
        await asyncio.to_thread(message.wait_for_publish,2)
        await wait_until(lambda:bool(exceptions))
        await wait_until(lambda:not worker_alive())
        message=publisher.publish('verify/camera_activity',snapshot(2),qos=1)
        await asyncio.to_thread(message.wait_for_publish,2)
        await asyncio.sleep(.4)
        feed.start()  # Existing client prevents a restart of the dead loop.
        normal_time=time.time()
        feed.pump(record,lambda *args,**kwargs:{'signal':'online'},now=normal_time)
        after={'worker_alive':worker_alive(),'connected_flag':feed.connected,
               'count_after_valid_recovery_message':feed.counts.get('verify_seats'),'immediate_health':record.health}
        feed.pump(record,lambda *args,**kwargs:{'signal':'online'},now=normal_time+16)
        assert before['worker_alive'] and not after['worker_alive']
        assert after['count_after_valid_recovery_message']==1
        assert exceptions[0]['type']=='RecursionError'
        assert record.latest['available'] is False
        return {'environment':'Independent real loopback MQTT broker; synthetic payload; +16 seconds only for the final TTL check',
                'payload_bytes':len(bad),'before':before,'after':after,'after_virtual_ttl':record.latest,
                'exceptions':exceptions,'expected':'Malformed data is rejected without terminating the MQTT processing loop; a later valid snapshot is accepted.',
                'verification':'verified'}
    finally:
        threading.excepthook=old_hook
        await asyncio.to_thread(feed.close)
        publisher.disconnect();await asyncio.to_thread(publisher.loop_stop)
        await broker.shutdown()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    result=asyncio.run(run());data=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.write_text(data)
    print(data,end='')
