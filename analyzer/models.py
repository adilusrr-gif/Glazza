"""Adapters to actual pretrained models. No heuristic 'crime' classifier."""
import hashlib
import json
import os
from pathlib import Path
import sys

LABELS = ['Normal', 'Abuse', 'Arrest', 'Arson', 'Assault', 'Burglary', 'Explosion', 'Fighting', 'RoadAccidents', 'Robbery', 'Shooting', 'Shoplifting', 'Stealing', 'Vandalism']
VADCLIP_NAME = 'VadCLIP UCF-Crime'
RETAIL_LABELS = ('Shoplifting', 'Stealing')
RETAIL_SAMPLE_SECONDS = 16 / 30
MAX_SAMPLE_GAP_SECONDS = 2.0
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
    if not manifest_path.is_file():
        raise FileNotFoundError(name)
    manifest = json.loads(manifest_path.read_text())
    if name == 'vadclip_ucf.pth' and manifest.get('layout') == 'sha256':
        checksum = manifest.get('sha256', '')
        if len(checksum) != 64 or any(ch not in '0123456789abcdef' for ch in checksum):
            raise ValueError('Invalid model checksum')
        path = root / 'vadclip-weights' / (checksum + '.pth')
    if not path.is_file():
        raise FileNotFoundError(name)
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
        self.action_threshold = float(os.getenv('GLAZZA_ACTION_THRESHOLD', '0.75'))
        self.theft_threshold = float(os.getenv('GLAZZA_THEFT_THRESHOLD') or self.action_threshold)
        self.theft_samples = int(os.getenv('GLAZZA_THEFT_SAMPLES', '2'))
        if not (0 < self.action_threshold <= 1 and 0 < self.theft_threshold <= 1 and 2 <= self.theft_samples <= 8):
            raise ValueError('Invalid VadCLIP gate configuration')
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
        def load_backbone(*args, **kwargs):
            backbone = clip.build_model(dict(clip_weights)).to(self.device)
            if self.device == 'cpu':
                backbone.float()
            return backbone, self.preprocess

        clip.load = load_backbone
        try:
            self.model = CLIPVAD(14, 512, 256, 512, 1, 2, 8, 10, 10, self.device)
        finally:
            clip.load = original_loader
        self.model.load_state_dict(state, strict=True)
        self.model.to(self.device).eval()

    def analyze(self, frames, sample_times, duration):
        from PIL import Image
        torch = self.torch
        all_scores = []
        with torch.inference_mode():
            # Preserve upstream 10-crop augmentation for each sampled source frame.
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
        return score_events(scores, self.action_threshold, theft_threshold=self.theft_threshold,
                            theft_samples=self.theft_samples, sample_times=sample_times, duration=duration)


def score_events(scores, threshold=0.75, *, theft_threshold=None, theft_samples=2, sample_times=None, duration=None):
    """Return model-supported review intervals. Scores are uncalibrated class scores."""
    import numpy as np
    scores = np.asarray(scores)
    theft_threshold = threshold if theft_threshold is None else theft_threshold
    if not (0 < threshold <= 1 and 0 < theft_threshold <= 1 and 2 <= theft_samples <= 8):
        raise ValueError('Invalid action gate')
    if (scores.ndim != 2 or scores.shape[1] != len(LABELS) or not np.isfinite(scores).all()
            or (scores < 0).any() or (scores > 1).any()):
        raise ValueError('Invalid VadCLIP class scores')
    if sample_times is not None:
        sample_times = np.asarray(sample_times)
        if (sample_times.shape != (len(scores),) or not np.isfinite(sample_times).all()
                or (np.diff(sample_times) <= 0).any() or duration is None or not np.isfinite(duration)
                or (sample_times < 0).any() or (sample_times >= duration).any()):
            raise ValueError('Invalid sample timestamps')

    def detection(kind, label, start, stop, score):
        item = {'kind': kind, 'label': label, 'score': float(score), 'sample_index': int(start),
                'sample_count': int(stop - start), 'model': VADCLIP_NAME, 'review_required': True}
        if sample_times is not None:
            end = float(sample_times[stop]) if stop < len(scores) else duration
            # Do not extend the highlighted evidence across a recording interruption.
            if end - sample_times[stop - 1] > MAX_SAMPLE_GAP_SECONDS:
                end = float(sample_times[stop - 1]) + RETAIL_SAMPLE_SECONDS
            item.update(start_seconds=float(sample_times[start]), end_seconds=end)
        return item

    output = []
    fight_labels = ['Fighting', 'Assault']
    fight_indexes = [LABELS.index(label) for label in fight_labels]
    values = scores[:, fight_indexes].max(axis=1)
    sustained = np.minimum(values[:-1], values[1:])
    if len(sustained) and sustained.max() >= threshold:
        start = int(np.argmax(sustained))
        label = fight_labels[int(np.argmax(scores[start, fight_indexes]))]
        output.append(detection('fight', label, start, start + 2, sustained[start]))

    theft = []
    for label in RETAIL_LABELS:
        column = LABELS.index(label)
        values = scores[:, column]
        # The same retail class must win outright on consecutive observed frames.
        competitors = np.delete(scores, column, axis=1).max(axis=1)
        matches = np.flatnonzero((values >= theft_threshold) & (values > competitors))
        breaks = np.diff(matches) != 1
        if sample_times is not None:
            breaks |= np.diff(sample_times[matches]) > MAX_SAMPLE_GAP_SECONDS
        for run in np.split(matches, np.flatnonzero(breaks) + 1):
            if len(run) >= theft_samples:
                windows = np.lib.stride_tricks.sliding_window_view(values[run], theft_samples)
                score = windows.min(axis=1).max()
                theft.append((int(run[0]), int(run[-1] + 1), label, score))

    # A label change within contiguous corroborated evidence is still one
    # review episode. Establish support within each class before merging.
    episodes = []
    for start, stop, label, score in sorted(theft):
        if (episodes and start == episodes[-1][1] and
                (sample_times is None or sample_times[start] - sample_times[start - 1] <= MAX_SAMPLE_GAP_SECONDS)):
            first, _, previous_label, previous_score = episodes.pop()
            start = first
            if previous_score >= score:
                label, score = previous_label, previous_score
        episodes.append((start, stop, label, score))
    return output + [detection('theft', label, start, stop, score) for start, stop, label, score in episodes]
