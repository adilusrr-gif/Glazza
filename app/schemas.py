import math
import ipaddress
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


class CameraLocation(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)

    @model_validator(mode='after')
    def paired_coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Укажите широту и долготу вместе')
        return self


class Camera(CameraLocation):
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
        if parsed.scheme not in ('rtsp', 'rtsps', 'http', 'https', 'rtmp', 'rtmps') or not parsed.hostname or parsed.fragment or any(c.isspace() or ord(c) < 32 or c in '{}#' for c in value):
            raise ValueError('Укажите RTSP, RTSPS, HTTP(S) или RTMP(S) адрес видеопотока без пробелов и фрагментов #')
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
            raise ValueError('Нужен адрес камеры в локальной сети или интернете')
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
