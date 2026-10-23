"""Adapters to actual pretrained models. No heuristic 'crime' classifier."""
import hashlib
import json
import os
from pathlib import Path
import sys

LABELS = ['Normal', 'Abuse', 'Arrest', 'Arson', 'Assault', 'Burglary', 'Explosion', 'Fighting', 'RoadAccidents', 'Robbery', 'Shooting', 'Shoplifting', 'Stealing', 'Vandalism']
UPSTREAM_COMMIT = 'c41067f07d252efcda18008bea367886070c33b0'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_artifact(root, name):
    path = root / name
    manifest_path = root / (name + '.json')
    if not path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(name)
    manifest = json.loads(manifest_path.read_text())
    if sha256(path) != manifest.get('sha256'):
        raise ValueError('Model checksum mismatch')
    return path


class FaceModel:
    def __init__(self, root):
        import mediapipe as mp
        self.mp = mp
        path = verify_artifact(root, 'face_landmarker.task')
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(path)),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            num_faces=4, output_face_blendshapes=True,
        )
        self.model = mp.tasks.vision.FaceLandmarker.create_from_options(options)

    def analyze(self, frames):
        import cv2
        peaks, count = {}, 0
        # Frames are already sampled at one per 16 source frames. Cap face work to 120 images.
        stride = max(1, (len(frames) + 119) // 120)
        for frame in frames[::stride]:
            image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            result = self.model.detect(image)
            if result.face_blendshapes:
                count += 1
            for face in result.face_blendshapes:
                for coefficient in face:
                    name = coefficient.category_name
                    peaks[name] = max(peaks.get(name, 0), float(coefficient.score))
        return {'frames_with_faces': count, 'frames_analyzed': len(frames[::stride]), 'peak_blendshapes': peaks}


class VadClipModel:
    def __init__(self, root):
        import torch
        self.torch = torch
        self.device = os.getenv('GLAZZA_DEVICE', 'cpu')
        if self.device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('CUDA is not available')
        source = root / 'vadclip'
        manifest = json.loads((source / 'glazza-source.json').read_text())
        if manifest['upstream_commit'] != UPSTREAM_COMMIT:
            raise ValueError('Unexpected upstream commit')
        for relative, expected in manifest['files'].items():
            if sha256(source / relative) != expected:
                raise ValueError('Upstream source has changed')
        path = verify_artifact(root, 'vadclip_ucf.pth')
        # Only load state dict tensors, never arbitrary pickle objects.
        checkpoint = torch.load(path, map_location='cpu', weights_only=True)
        state = checkpoint.get('model_state_dict', checkpoint.get('state_dict', checkpoint))
        state = {key.removeprefix('module.'): value for key, value in state.items()}
        clip_weights = {key.removeprefix('clipmodel.'): value for key, value in state.items() if key.startswith('clipmodel.')}
        if not clip_weights:
            raise ValueError('Expected official UCF checkpoint including CLIP backbone')
        sys.path.insert(0, str(source / 'src'))
        from clip import clip
        from model import CLIPVAD
        from crop import image_crop
        self.crop = image_crop
        self.preprocess = clip._transform(224)
        # Reuse the frozen backbone inside the official checkpoint: no runtime download.
        original_loader = clip.load
        clip.load = lambda *args, **kwargs: (clip.build_model(dict(clip_weights)).to(self.device), self.preprocess)
        try:
            self.model = CLIPVAD(14, 512, 256, 512, 1, 2, 8, 10, 10, self.device)
        finally:
            clip.load = original_loader
        self.model.load_state_dict(state, strict=True)
        self.model.to(self.device).eval()

    def analyze(self, frames):
        from PIL import Image
        torch = self.torch
        all_scores = []
        with torch.inference_mode():
            # Preserve 10-crop augmentation and one feature per 16 source frames.
            # Each crop is inferred independently, then class probabilities are averaged.
            for start in range(0, len(frames), 256):
                segment = frames[start:start + 256]
                features = []
                for frame in segment:
                    crops = torch.stack([self.preprocess(Image.fromarray(self.crop(frame, n))) for n in range(10)]).to(self.device)
                    features.append(self.model.clipmodel.encode_image(crops).float())
                visual = torch.stack(features, dim=1)  # [10, T, 512]
                length = visual.shape[1]
                if length < 256:
                    visual = torch.nn.functional.pad(visual, (0, 0, 0, 256 - length))
                lengths = torch.full((10,), length, dtype=torch.int64, device=self.device)
                _, _, logits = self.model(visual, None, LABELS, lengths)
                all_scores.append(logits[:, :length].softmax(dim=-1).mean(dim=0).cpu())
        scores = torch.cat(all_scores, dim=0).numpy()
        return score_events(scores, float(os.getenv('GLAZZA_ACTION_THRESHOLD', '0.75')))


def score_events(scores, threshold=0.75):
    """Require two adjacent sampled frames above threshold; values are uncalibrated scores."""
    import numpy as np
    if not 0 < threshold <= 1:
        raise ValueError('Threshold must be in (0,1]')
    output = []
    for kind, labels in [('fight', ['Fighting', 'Assault']), ('theft', ['Shoplifting', 'Stealing', 'Robbery', 'Burglary'])]:
        indexes = [LABELS.index(label) for label in labels]
        values = scores[:, indexes].max(axis=1)
        sustained = np.minimum(values[:-1], values[1:])
        if len(sustained) and float(sustained.max()) >= threshold:
            index = int(np.argmax(sustained))
            label = labels[int(np.argmax(scores[index, indexes]))]
            output.append({'kind': kind, 'score': float(sustained.max()), 'label': label, 'sample_index': index, 'review_required': True})
    return output
