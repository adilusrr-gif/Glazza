"""YuNet + SFace, local embeddings; no emotions, intent or demographic inference."""
import base64
import io
import json
from pathlib import Path
import threading

from fastapi import Header, HTTPException, Request
from .models import sha256


class IdentityModel:
    def __init__(self, root):
        import cv2
        self.cv = cv2
        artifacts=json.loads(Path(__file__).with_name('identity_artifacts.json').read_text())
        for name, item in artifacts['files'].items():
            path=root/'identity'/name
            if not path.is_file(): raise FileNotFoundError(name)
            if sha256(path)!=item['sha256']: raise ValueError('Identity artifact checksum mismatch')
        self.detector=cv2.FaceDetectorYN.create(str(root/'identity/face_detection_yunet_2023mar.onnx'),'',(640,640),.9,.3,5000)
        self.recognizer=cv2.FaceRecognizerSF.create(str(root/'identity/face_recognition_sface_2021dec.onnx'),'')
        self.lock=threading.Lock()

    def encode(self, raw):
        import numpy as np
        from PIL import Image
        # Inspect dimensions before OpenCV decoding to bound decompression memory.
        with Image.open(io.BytesIO(raw), formats=('JPEG','PNG')) as image:
            if image.format not in ('JPEG','PNG') or image.width*image.height>8_000_000:
                raise ValueError('JPEG/PNG up to 8 MP required')
            image.verify()
        frame=self.cv.imdecode(np.frombuffer(raw,dtype=np.uint8),self.cv.IMREAD_COLOR)
        if frame is None: raise ValueError('Invalid image')
        h,w=frame.shape[:2]
        scale=min(1,960/max(h,w))
        frame=self.cv.resize(frame,(int(w*scale),int(h*scale)))
        with self.lock:
            self.detector.setInputSize((frame.shape[1],frame.shape[0]))
            _,faces=self.detector.detect(frame)
            if faces is None or len(faces)!=1: raise ValueError('Exactly one face required')
            face=faces[0]
            if min(face[2],face[3])<60: raise ValueError('Face too small')
            aligned=self.recognizer.alignCrop(frame,face)
            vector=self.recognizer.feature(aligned).flatten()
            vector=vector/np.linalg.norm(vector)
        ok,jpeg=self.cv.imencode('.jpg',frame,[self.cv.IMWRITE_JPEG_QUALITY,85])
        if not ok: raise ValueError('Cannot encode image')
        return {'model':'opencv-sface-2021dec','embedding':vector.tolist(),'jpeg':base64.b64encode(jpeg).decode()}


def register(app,models,authorize):
    @app.post('/identity/encode')
    async def encode(request: Request,x_worker_key: str|None=Header(default=None)):
        import asyncio
        authorize(x_worker_key)
        if 'identity' not in models: raise HTTPException(503,'Identity model not loaded')
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>2*1024*1024: raise HTTPException(413)
        try: return await asyncio.to_thread(models['identity'].encode,bytes(raw))
        except Exception: raise HTTPException(422,'Need one clear face in JPEG/PNG <=2MB')
