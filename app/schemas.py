import math
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class Zone(BaseModel):
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{1,39}$')
    points: list[tuple[float, float]] = Field(min_length=3, max_length=20)
    restricted: bool = True

    @field_validator('points')
    @classmethod
    def valid_points(cls, points):
        if any(not math.isfinite(n) or not 0 <= n <= 1 for p in points for n in p):
            raise ValueError('Координаты зоны должны быть от 0 до 1')
        area = abs(sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))) / 2
        if area < 0.0001:
            raise ValueError('Зона должна иметь ненулевую площадь')
        return points


class Camera(BaseModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{1,39}$')
    name: str = Field(min_length=1, max_length=80)
    stream: str = Field(min_length=1, max_length=2048)
    zones: list[Zone] = Field(default_factory=list, max_length=12)
    analyze: bool = False

    @field_validator('stream')
    @classmethod
    def valid_stream(cls, value):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError('Некорректный адрес камеры') from error
        if parsed.scheme not in ('rtsp', 'rtsps') or not parsed.hostname or any(c.isspace() or c in '{}\x00' for c in value):
            raise ValueError('Нужен RTSP/RTSPS адрес без пробелов и фигурных скобок')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('Некорректный порт')
        return value

    @model_validator(mode='after')
    def unique_zones(self):
        names = [z.name for z in self.zones]
        if len(names) != len(set(names)) or self.id in names:
            raise ValueError('Имена зон должны быть уникальны и отличаться от имени камеры')
        return self


class Review(BaseModel):
    status: Literal['new', 'confirmed', 'dismissed']
    note: str = Field(default='', max_length=2000)


class ZoneUpdate(BaseModel):
    zones: list[Zone] = Field(max_length=12)
