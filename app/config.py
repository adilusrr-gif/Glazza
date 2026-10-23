import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data: Path
    frigate_config: Path
    frigate_url: str
    analyzer_url: str
    worker_key: str
    username: str
    password: str
    demo: bool
    secure_cookie: bool
    poll_seconds: float = 5
    max_upload: int = 200 * 1024 * 1024
    retention_days: int = 30

    @classmethod
    def from_env(cls):
        return cls(
            data=Path(os.getenv('GLAZZA_DATA', './data')),
            frigate_config=Path(os.getenv('FRIGATE_CONFIG', './config/config.yml')),
            frigate_url=os.getenv('FRIGATE_URL', 'http://frigate:5000').rstrip('/'),
            analyzer_url=os.getenv('ANALYZER_URL', 'http://analyzer:8090').rstrip('/'),
            worker_key=os.getenv('GLAZZA_WORKER_KEY', ''),
            username=os.getenv('GLAZZA_ADMIN', 'admin'),
            password=os.getenv('GLAZZA_PASSWORD', ''),
            demo=os.getenv('GLAZZA_DEMO', 'false').lower() == 'true',
            secure_cookie=os.getenv('GLAZZA_SECURE_COOKIE', 'false').lower() == 'true',
            retention_days=int(os.getenv('GLAZZA_RETENTION_DAYS', '30')),
        )
