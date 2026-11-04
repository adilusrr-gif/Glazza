"""Opt-in real YuNet/SFace enrollment, matching and human decision through ASGI APIs.

Uses public Google MediaPipe portrait solely as an integration fixture, never commits it.
Not an independent accuracy benchmark or spoof-resistance assessment.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request

import cv2
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from analyzer.identity import IdentityModel, register
from app.main import create_app
from app.config import Settings
from app.admission import process

ROOT=Path(__file__).resolve().parents[1]


def main():
    with urllib.request.urlopen('https://storage.googleapis.com/mediapipe-assets/portrait.jpg',timeout=30) as response:
        photo=response.read()
    model=IdentityModel(ROOT/'models')
    encoded=model.encode(photo)
    assert len(encoded['embedding'])==128
    try:
        model.encode(b'not an image')
        raise AssertionError('Invalid image accepted')
    except (ValueError,OSError):pass
    key='identity-smoke-worker-key-'*2
    worker=FastAPI()
    def authorize(value):
        if value!=key:raise HTTPException(401)
    register(worker,{'identity':model},authorize)
    with tempfile.TemporaryDirectory(prefix='glazza-identity-') as directory:
        root=Path(directory)
        settings=Settings(data=root/'data',frigate_config=root/'config.yml',frigate_url='http://nvr',analyzer_url='http://worker',worker_key=key,username='admin',password='identity-smoke-password',demo=False,secure_cookie=False)
        app=create_app(settings,background=False)
        worker_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=worker),base_url='http://worker')
        async def transport(request):
            if request.url.host=='nvr':
                assert request.url.params.get('crop')=='1'
                return httpx.Response(200,content=photo)
            return await worker_client.request(request.method,request.url.path,content=request.content,headers=request.headers)
        app.state.engine.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with TestClient(app) as client:
            login=client.post('/api/login',json={'username':'admin','password':settings.password}).json()
            client.headers['X-CSRF-Token']=login['csrf']
            person=client.post('/api/admission/people',json={'name':'Public integration fixture','category':'staff','valid_from':time.time()-60,'valid_until':time.time()+3600,'basis':'Public fixture; temporary isolated test only'}).json()['id']
            enrollment=client.post(f'/api/admission/people/{person}/photo',content=photo)
            assert enrollment.status_code==200,enrollment.text
            assert client.get(f'/api/admission/people/{person}/photo').headers['content-type']=='image/jpeg'
            camera={'id':'entrance','name':'Fixture entrance','stream':'rtsp://127.0.0.1/test','zones':[{'name':'door','points':[[0,0],[1,0],[1,1]],'restricted':False}]}
            assert client.post('/api/cameras',json=camera).status_code==201
            assert client.put('/api/admission/entrances',json={'camera':'entrance','zone':'door'}).status_code==200
            app.state.engine.ingest([{'id':'face-smoke','camera':'entrance','label':'person','start_time':time.time(),'zones':['door']}])
            asyncio.run(process(app.state.engine))
            item=client.get('/api/admission').json()['entries'][0]
            assert item['candidate_id']==person and item['decision'] is None,item
            assert item['reason']=='candidate_requires_review' and item['score']>.99,item
            r=client.post('/api/admission/entries/'+item['id']+'/decision',json={'decision':'allowed','person_id':person,'note':'Fixture verified by human test'})
            assert r.status_code==200,r.text
            assert client.get('/api/admission').json()['pending']==0
            assert client.delete(f'/api/admission/people/{person}').status_code==200
            assert client.get(f'/api/admission/people/{person}/photo').status_code==404
            print(json.dumps({'result':'PASS','model':encoded['model'],'embedding_dimensions':128,'self_match':item['score'],'automatic_admission':False,'enrollment_camera_event_review_delete':'PASS'},indent=2))
        asyncio.run(worker_client.aclose())


if __name__=='__main__':main()
