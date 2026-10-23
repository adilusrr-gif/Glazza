#!/usr/bin/env python3
"""Consistent SQLite snapshot. Stop configuration changes during backup."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data')
    parser.add_argument('--destination', default='backups')
    args = parser.parse_args()
    source = Path(args.data) / 'glazza.db'
    if not source.is_file():
        parser.error('Database does not exist')
    destination = Path(args.destination) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(destination / 'glazza.db') as dst:
        src.backup(dst)
    (destination / 'glazza.db').chmod(0o600)
    for name in ['.env', 'config/config.yml']:
        path = Path(name)
        if path.is_file():
            target = destination / path.name
            shutil.copyfile(path, target)
            target.chmod(0o600)
    print(f'Backup created: {destination}. Treat as sensitive: camera and login configuration included.')


if __name__ == '__main__':
    main()
