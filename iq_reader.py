from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


HEADER_DEFAULT_BYTES = 8192


def _parse_braced_header(raw: bytes) -> dict[str, str]:
    text = raw.decode("latin1", errors="ignore")
    pairs = re.findall(r"\{([^:{}]+):([^{}]*)\}", text)
    return {key.strip(): value.strip() for key, value in pairs}


def parse_ws_header(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        raw = handle.read(HEADER_DEFAULT_BYTES)
    return _parse_braced_header(raw)


def _as_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _as_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class IQVolume:
    path: Path
    volume_index: int
    header: dict[str, str]
    header_bytes: int
    sample_count: int


@dataclass(frozen=True)
class IQRecording:
    stem: str
    volumes: tuple[IQVolume, ...]
    metadata_path: Path | None = None

    @property
    def sample_rate_hz(self) -> float:
        header = self.volumes[0].header
        return _as_float(header.get("CHANRATE0"), _as_float(header.get("CLOCK"), 1.0))

    @property
    def total_samples(self) -> int:
        return sum(volume.sample_count for volume in self.volumes)

    @property
    def duration_s(self) -> float:
        rate = self.sample_rate_hz
        return self.total_samples / rate if rate else 0.0

    @property
    def reference_level_dbm(self) -> float:
        return _as_float(self.volumes[0].header.get("CHANREFLVL0"), float("nan"))

    @property
    def center_frequency_mhz(self) -> float:
        matches = re.findall(r"(\d+(?:\.\d+)?)", self.stem)
        return float(matches[-1]) if matches else 0.0

    @property
    def summary(self) -> str:
        return (
            f"{self.stem}: {len(self.volumes)} volume(s), "
            f"{self.total_samples:,} complex samples, "
            f"{self.sample_rate_hz / 1e6:.6g} MS/s, "
            f"center {self.center_frequency_mhz:.6g} MHz, "
            f"{self.duration_s:.3f} s"
        )


def discover_recordings(data_dir: Path) -> list[IQRecording]:
    groups: dict[str, list[Path]] = {}
    for path in data_dir.glob("*.ws[0-9]*"):
        match = re.match(r"(.+)\.ws(\d+)$", path.name, flags=re.IGNORECASE)
        if not match:
            continue
        groups.setdefault(match.group(1), []).append(path)

    recordings: list[IQRecording] = []
    for stem, paths in sorted(groups.items()):
        volumes: list[IQVolume] = []
        for path in sorted(paths, key=lambda item: int(item.suffix[3:])):
            header = parse_ws_header(path)
            header_bytes = _as_int(header.get("SECTORSIZE"), HEADER_DEFAULT_BYTES)
            sample_count = _as_int(header.get("SAMPLES"))
            if not sample_count:
                payload_bytes = max(path.stat().st_size - header_bytes - 1, 0)
                sample_count = payload_bytes // 4
            volume_index = _as_int(header.get("VOLUME"), int(path.suffix[3:]))
            volumes.append(
                IQVolume(
                    path=path,
                    volume_index=volume_index,
                    header=header,
                    header_bytes=header_bytes,
                    sample_count=sample_count,
                )
            )
        metadata = data_dir / f"{stem}.wsm"
        recordings.append(
            IQRecording(stem=stem, volumes=tuple(volumes), metadata_path=metadata if metadata.exists() else None)
        )
    return recordings


def recording_from_paths(
    stem: str,
    metadata_path: Path,
    volume_paths: Iterable[Path],
) -> IQRecording:
    """Build one recording from explicitly associated metadata and volume files."""
    volume_paths = tuple(Path(path) for path in volume_paths)
    if not metadata_path.is_file():
        raise FileNotFoundError(f"未找到 IQ 元数据文件：{metadata_path}")
    if not volume_paths:
        raise FileNotFoundError("没有提供 IQ 数据卷文件")
    volumes: list[IQVolume] = []
    for fallback_index, path in enumerate(volume_paths, start=1):
        if not path.is_file():
            raise FileNotFoundError(f"未找到 IQ 数据文件：{path}")
        header = parse_ws_header(path)
        header_bytes = _as_int(header.get("SECTORSIZE"), HEADER_DEFAULT_BYTES)
        sample_count = _as_int(header.get("SAMPLES"))
        if not sample_count:
            payload_bytes = max(path.stat().st_size - header_bytes - 1, 0)
            sample_count = payload_bytes // 4
        volumes.append(
            IQVolume(
                path=path,
                volume_index=_as_int(header.get("VOLUME"), fallback_index),
                header=header,
                header_bytes=header_bytes,
                sample_count=sample_count,
            )
        )
    return IQRecording(stem=stem, volumes=tuple(volumes), metadata_path=metadata_path)


def get_recording(data_dir: Path, stem: str) -> IQRecording:
    for recording in discover_recordings(data_dir):
        if recording.stem.lower() == stem.lower():
            return recording
    available = ", ".join(item.stem for item in discover_recordings(data_dir)) or "(none)"
    raise FileNotFoundError(f"Recording {stem!r} not found. Available recordings: {available}")


def _volume_array(volume: IQVolume) -> np.memmap:
    return np.memmap(
        volume.path,
        dtype="<i2",
        mode="r",
        offset=volume.header_bytes,
        shape=(volume.sample_count, 2),
    )


def read_iq_by_indices(recording: IQRecording, indices: np.ndarray) -> np.ndarray:
    if indices.size == 0:
        return np.empty(0, dtype=np.complex64)

    indices = np.asarray(indices, dtype=np.int64)
    valid = (indices >= 0) & (indices < recording.total_samples)
    indices = indices[valid]
    output = np.empty(indices.size, dtype=np.complex64)

    base = 0
    cursor = 0
    for volume in recording.volumes:
        end = base + volume.sample_count
        mask = (indices >= base) & (indices < end)
        count = int(mask.sum())
        if count:
            local_indices = indices[mask] - base
            raw = _volume_array(volume)[local_indices]
            output[cursor : cursor + count] = raw[:, 0].astype(np.float32) / 32768.0
            output[cursor : cursor + count] += 1j * (raw[:, 1].astype(np.float32) / 32768.0)
            cursor += count
        base = end

    return output[:cursor]


def read_iq_window(
    recording: IQRecording,
    start_sample: int = 0,
    sample_count: int | None = None,
    max_points: int = 200_000,
) -> tuple[np.ndarray, np.ndarray, int]:
    total = recording.total_samples
    start_sample = max(0, min(int(start_sample), total))
    if sample_count is None:
        sample_count = total - start_sample
    sample_count = max(0, min(int(sample_count), total - start_sample))
    if sample_count == 0:
        return np.empty(0), np.empty(0, dtype=np.complex64), 1

    stride = max(1, math.ceil(sample_count / max_points))
    indices = start_sample + np.arange(0, sample_count, stride, dtype=np.int64)
    iq = read_iq_by_indices(recording, indices)
    times = indices[: iq.size] / recording.sample_rate_hz
    return times, iq, stride


def read_iq_contiguous(recording: IQRecording, start_sample: int, sample_count: int) -> np.ndarray:
    indices = np.arange(start_sample, start_sample + sample_count, dtype=np.int64)
    return read_iq_by_indices(recording, indices)


def recording_names(recordings: Iterable[IQRecording]) -> list[str]:
    return [recording.stem for recording in recordings]
