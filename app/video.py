"""Identify uploaded containers independently of browser MIME declarations."""

MIME_TYPES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo'}


def video_suffix(header):
    if b'ftyp' in header[:32]:
        return '.mp4'
    if header.startswith(b'\x1a\x45\xdf\xa3'):
        if b'webm' in header[:4096]:
            return '.webm'
        if b'matroska' in header[:4096]:
            return '.mkv'
    if header.startswith(b'RIFF') and header[8:12] == b'AVI ':
        return '.avi'
    raise ValueError('Поддерживаются MP4, MOV, WebM, MKV и AVI')
