"""Offline VD-MIL adapter. Upstream code/weights are installed separately with attribution.

Roger Figueroa Quintero's VD-MIL is CC BY-NC 4.0; see THIRD_PARTY.md.
This adapter preserves the author's preprocessing, without per-video min/max rescaling.
"""
import json
import math
import os
from pathlib import Path
import sys

from .models import sha256


def fight_intervals(scores, threshold=0.8, consecutive=3):
    """Three overlapping 1s windows (0.5s stride) must agree; timestamps are clip-relative."""
    if not 0 < threshold <= 1 or consecutive < 2:
        raise ValueError('Invalid fight gate')
    if any(not math.isfinite(float(s)) or not 0 <= s <= 1 for s in scores):
        raise ValueError('Invalid violence scores')
    detections, start = [], None
    for i in range(len(scores) + 1):
        if i < len(scores) and scores[i] >= threshold:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= consecutive:
                detections.append({'kind': 'fight', 'label': 'Possible violence / fight',
                                   'score': float(min(scores[start:i])),
                                   'start_seconds': start * 0.5, 'end_seconds': (i - 1) * 0.5 + 1,
                                   'model': 'VD-MIL / MoViNet-A0', 'review_required': True})
            start = None
    return detections


class FightModel:
    def __init__(self, root):
        import torch
        from torch import nn
        self.torch = torch
        self.device = os.getenv('GLAZZA_DEVICE', 'cpu')
        self.threshold = float(os.getenv('GLAZZA_FIGHT_THRESHOLD', '0.8'))
        self.consecutive = int(os.getenv('GLAZZA_FIGHT_WINDOWS', '3'))
        if not 2 <= self.consecutive <= 8:
            raise ValueError('GLAZZA_FIGHT_WINDOWS must be 2..8')
        fight_intervals([], self.threshold, self.consecutive)
        torch.set_num_threads(max(1, min(16, int(os.getenv('GLAZZA_TORCH_THREADS', '2')))))
        if self.device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('CUDA is not available')
        source = root / 'vdmil'
        artifacts = json.loads(Path(__file__).with_name('fight_artifacts.json').read_text())
        # Compare all source AND weight bytes to release-pinned hashes, not operator manifests.
        for relative, artifact in artifacts['files'].items():
            path = source / relative
            if not path.is_file():
                raise FileNotFoundError(relative)
            if sha256(path) != artifact['sha256']:
                raise ValueError('VD-MIL artifact checksum mismatch: ' + relative)
        sys.path.insert(0, str(source))
        from movinets import MoViNet
        from movinets.config import _C
        from movinet_classifier import Net
        # pretrained=False prevents downloads; explicitly reproduce the pretrained architecture.
        self.backbone = MoViNet(_C.MODEL.MoViNetA0.clone(), causal=True, pretrained=False,
                               num_classes=600, conv_type='2plus1d', tf_like=True)
        state = torch.load(source / 'backbone.pt', map_location='cpu', weights_only=True)
        self.backbone.load_state_dict(state, strict=True)
        self.backbone.classifier[3] = nn.Identity()
        self.backbone.to(self.device).eval()
        # Legacy upstream saves an nn.Module, not a state dict. Restrict unpickling to the
        # exact audited network/layer classes; never fall back to weights_only=False.
        with torch.serialization.safe_globals([Net, set, nn.Sequential, nn.Linear, nn.ReLU, nn.Sigmoid]):
            legacy = torch.load(source / 'classifier.pt', map_location='cpu', weights_only=True)
        self.classifier = Net()
        self.classifier.load_state_dict(legacy.state_dict(), strict=True)
        self.classifier.to(self.device).eval()

    def analyze(self, frames):
        import cv2
        import numpy as np
        torch = self.torch
        scores = []
        # Frames arrive at 8fps, BGR, 172x172. Reset causal state for each independent window.
        with torch.inference_mode():
            for start in range(0, len(frames) - 7, 4):
                rgb = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames[start:start + 8]])
                x = torch.from_numpy(rgb).permute(3, 0, 1, 2).unsqueeze(0).to(self.device, dtype=torch.float32) / 255
                self.backbone.clean_activation_buffers()
                features = torch.nn.functional.normalize(self.backbone(x), p=2, dim=1)
                scores.append(float(self.classifier(features).item()))
        self.backbone.clean_activation_buffers()
        return {'model': 'VD-MIL / MoViNet-A0', 'threshold': self.threshold,
                'sample_fps': 8, 'window_seconds': 1, 'stride_seconds': 0.5,
                'required_windows': self.consecutive, 'windows_analyzed': len(scores),
                'peak_score': max(scores, default=0), 'scores': scores,
                'detections': fight_intervals(scores, self.threshold, self.consecutive)}
