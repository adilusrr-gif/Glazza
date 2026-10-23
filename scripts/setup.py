#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description='Initialize Glazza without overwriting existing configuration')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--uid', type=int, help='Container UID override for rootless/user-namespace hosts')
    parser.add_argument('--gid', type=int, help='Container GID override for rootless/user-namespace hosts')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = root / '.env'
    if env.exists():
        parser.error('.env already exists. Preserve credentials; edit it explicitly to change settings.')
    uid = os.getuid() if hasattr(os, 'getuid') else 10001
    gid = os.getgid() if hasattr(os, 'getgid') else 10001
    if uid == 0 and args.uid is None:
        uid = gid = 10001
    uid = args.uid if args.uid is not None else uid
    gid = args.gid if args.gid is not None else gid
    data_dir = 'data-demo' if args.demo else 'data'
    for name in (data_dir, 'config', 'media', 'models'):
        path = root / name
        path.mkdir(exist_ok=True, mode=0o700 if name != 'models' else 0o755)
        if hasattr(os, 'getuid') and os.getuid() == 0:
            try:
                os.chown(path, uid, gid)
            except OSError:
                parser.error('Cannot assign container UID/GID. Run as a regular user or supply mapped --uid and --gid.')
    config = root / 'config/config.yml'
    if not config.exists():
        config.write_text('mqtt:\n  enabled: false\ncameras: {}\n')
        config.chmod(0o600)
        if hasattr(os, 'getuid') and os.getuid() == 0:
            os.chown(config, uid, gid)
    password, worker_key = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    env.write_text(f'GLAZZA_ADMIN=admin\nGLAZZA_PASSWORD={password}\nGLAZZA_WORKER_KEY={worker_key}\nGLAZZA_DEMO={str(args.demo).lower()}\nGLAZZA_DATA_DIR=./{data_dir}\nGLAZZA_UID={uid}\nGLAZZA_GID={gid}\nGLAZZA_BIND=127.0.0.1\nGLAZZA_SECURE_COOKIE=false\nGLAZZA_RETENTION_DAYS=30\n')
    env.chmod(0o600)
    credentials = root / 'credentials.txt'
    credentials.write_text(f'Glazza: http://localhost:8080\nLogin: admin\nPassword: {password}\n')
    credentials.chmod(0o600)
    print('Initialized. Login credentials saved in credentials.txt (not tracked by Git).')
    print('Run: docker compose up -d --build' if args.demo else 'Run: docker compose --profile nvr up -d --build')


if __name__ == '__main__':
    main()
