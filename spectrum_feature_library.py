from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from matplotlib.figure import Figure

from environment_spectrum import BAND_ORDER, SpectrumGroup, calculate_max_hold


ALL_BAND = "30M-6G (All)"
FEATURE_BANDS = (*BAND_ORDER, ALL_BAND)


@dataclass(frozen=True)
class FeatureRecord:
    id: int
    city: str
    point: str
    polarization: str
    band: str
    source_file_count: int
    frequency_count: int
    peak_frequency_mhz: float
    peak_dbuv_m: float
    mean_dbuv_m: float
    median_dbuv_m: float
    std_db: float
    p95_dbuv_m: float
    p99_dbuv_m: float
    dynamic_range_db: float
    centroid_mhz: float
    occupied_ratio: float
    strong_peak_count: int
    noise_floor_dbuv_m: float
    updated_at: str
    min_dbuv_m: float
    detection_threshold_dbuv_m: float
    effective_band_count: int
    effective_span_mhz: float
    effective_total_bandwidth_mhz: float
    energy_band_low_mhz: float
    energy_band_high_mhz: float
    energy_bandwidth_mhz: float
    top_peaks_json: str


@dataclass(frozen=True)
class SpectrumFeatureAnalysis:
    metrics: dict[str, float | int]
    peaks: tuple[dict[str, float | int], ...]
    effective_bands: tuple[dict[str, float], ...]


@dataclass(frozen=True)
class BuildResult:
    database_path: Path
    group_count: int
    profile_count: int
    failed_items: tuple[str, ...]


@dataclass
class ComparisonResult:
    records: list[FeatureRecord]
    correlation: np.ndarray
    rms_difference_db: np.ndarray
    figure: Figure
    summary: str


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS spectrum_profiles (
            id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            point TEXT NOT NULL,
            polarization TEXT NOT NULL,
            band TEXT NOT NULL,
            source_file_count INTEGER NOT NULL,
            frequency_count INTEGER NOT NULL,
            peak_frequency_mhz REAL NOT NULL,
            peak_dbuv_m REAL NOT NULL,
            mean_dbuv_m REAL NOT NULL,
            median_dbuv_m REAL NOT NULL,
            std_db REAL NOT NULL,
            p95_dbuv_m REAL NOT NULL,
            p99_dbuv_m REAL NOT NULL,
            dynamic_range_db REAL NOT NULL,
            centroid_mhz REAL NOT NULL,
            occupied_ratio REAL NOT NULL,
            strong_peak_count INTEGER NOT NULL,
            noise_floor_dbuv_m REAL NOT NULL,
            top_peaks_json TEXT NOT NULL,
            frequencies_blob BLOB NOT NULL,
            values_blob BLOB NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(city, point, polarization, band)
        );
        CREATE INDEX IF NOT EXISTS idx_profiles_filter
        ON spectrum_profiles(city, polarization, band, point);
        """
    )
    migrations = {
        "min_dbuv_m": "REAL NOT NULL DEFAULT 0",
        "detection_threshold_dbuv_m": "REAL NOT NULL DEFAULT 0",
        "effective_band_count": "INTEGER NOT NULL DEFAULT 0",
        "effective_span_mhz": "REAL NOT NULL DEFAULT 0",
        "effective_total_bandwidth_mhz": "REAL NOT NULL DEFAULT 0",
        "energy_band_low_mhz": "REAL NOT NULL DEFAULT 0",
        "energy_band_high_mhz": "REAL NOT NULL DEFAULT 0",
        "energy_bandwidth_mhz": "REAL NOT NULL DEFAULT 0",
        "effective_bands_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    existing = {row[1] for row in connection.execute("PRAGMA table_info(spectrum_profiles)")}
    for name, declaration in migrations.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE spectrum_profiles ADD COLUMN {name} {declaration}")
    connection.commit()
    return connection


def _pack(values: np.ndarray) -> bytes:
    return zlib.compress(np.asarray(values, dtype="<f4").tobytes(), level=6)


def _unpack(blob: bytes, count: int) -> np.ndarray:
    values = np.frombuffer(zlib.decompress(blob), dtype="<f4", count=count)
    return values.astype(np.float64, copy=False)


def _strong_peaks(frequencies_hz: np.ndarray, values: np.ndarray, threshold: float) -> list[dict[str, float | int]]:
    if values.size < 3:
        indices = np.arange(values.size)
    else:
        indices = np.flatnonzero((values[1:-1] > values[:-2]) & (values[1:-1] >= values[2:])) + 1
    indices = indices[values[indices] >= threshold]
    if indices.size == 0:
        indices = np.asarray([int(np.argmax(values))])

    order = indices[np.argsort(values[indices])[::-1]]
    min_spacing_hz = max(100_000.0, float(np.ptp(frequencies_hz)) / 500.0)
    selected: list[int] = []
    for index in order:
        if all(abs(frequencies_hz[index] - frequencies_hz[other]) >= min_spacing_hz for other in selected):
            selected.append(int(index))
        if len(selected) == 10:
            break
    peaks: list[dict[str, float | int]] = []
    for rank, index in enumerate(selected, start=1):
        boundary = float(values[index] - 3.0)
        left = index
        while left > 0 and values[left - 1] >= boundary:
            left -= 1
        right = index
        while right + 1 < values.size and values[right + 1] >= boundary:
            right += 1

        def crossing(inner: int, outer: int) -> float:
            inner_value, outer_value = float(values[inner]), float(values[outer])
            if inner_value == outer_value:
                return float(frequencies_hz[inner])
            fraction = np.clip((boundary - inner_value) / (outer_value - inner_value), 0.0, 1.0)
            return float(frequencies_hz[inner] + fraction * (frequencies_hz[outer] - frequencies_hz[inner]))

        left_hz = crossing(left, left - 1) if left > 0 else float(frequencies_hz[left])
        right_hz = crossing(right, right + 1) if right + 1 < values.size else float(frequencies_hz[right])
        left_mhz = left_hz / 1e6
        right_mhz = right_hz / 1e6
        peaks.append({
            "rank": rank,
            "frequency_mhz": float(frequencies_hz[index] / 1e6),
            "field_dbuv_m": float(values[index]),
            "left_3db_mhz": left_mhz,
            "right_3db_mhz": right_mhz,
            "bandwidth_3db_mhz": max(0.0, right_mhz - left_mhz),
        })
    return peaks


def _effective_bands(
    frequencies_hz: np.ndarray, values: np.ndarray, threshold: float
) -> list[dict[str, float]]:
    mask = values >= threshold
    if mask.size >= 3:
        mask[1:-1] |= mask[:-2] & mask[2:]
    indices = np.flatnonzero(mask)
    if indices.size == 0:
        return []
    positive_steps = np.diff(frequencies_hz)
    positive_steps = positive_steps[positive_steps > 0]
    normal_step = float(np.median(positive_steps)) if positive_steps.size else 1.0
    splits = np.flatnonzero((np.diff(indices) > 1) | (np.diff(frequencies_hz[indices]) > normal_step * 5.0)) + 1
    bands: list[dict[str, float]] = []
    for run in np.split(indices, splits):
        if run.size < 2:
            continue
        start_mhz = float(frequencies_hz[run[0]] / 1e6)
        end_mhz = float(frequencies_hz[run[-1]] / 1e6)
        peak_index = int(run[np.argmax(values[run])])
        bands.append({
            "start_mhz": start_mhz,
            "end_mhz": end_mhz,
            "bandwidth_mhz": max(0.0, end_mhz - start_mhz),
            "peak_frequency_mhz": float(frequencies_hz[peak_index] / 1e6),
            "peak_dbuv_m": float(values[peak_index]),
        })
    return bands


def analyze_spectrum_features(frequencies_hz: np.ndarray, values: np.ndarray) -> SpectrumFeatureAnalysis:
    frequencies_hz = np.asarray(frequencies_hz, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(frequencies_hz) & np.isfinite(values)
    frequencies_hz, values = frequencies_hz[valid], values[valid]
    if values.size == 0:
        raise ValueError("频谱中没有可分析的有效数据")
    order = np.argsort(frequencies_hz)
    frequencies_hz, values = frequencies_hz[order], values[order]
    peak_index = int(np.argmax(values))
    noise_floor = float(np.percentile(values, 20))
    threshold = noise_floor + 6.0
    peaks = _strong_peaks(frequencies_hz, values, threshold)
    bands = _effective_bands(frequencies_hz, values, threshold)
    weights = np.power(10.0, (values - float(np.max(values))) / 10.0)
    cumulative = np.cumsum(weights) / max(float(np.sum(weights)), np.finfo(float).tiny)
    low_index = min(int(np.searchsorted(cumulative, 0.05)), values.size - 1)
    high_index = min(int(np.searchsorted(cumulative, 0.95)), values.size - 1)
    centroid_hz = float(np.sum(frequencies_hz * weights) / max(float(np.sum(weights)), np.finfo(float).tiny))
    effective_span = bands[-1]["end_mhz"] - bands[0]["start_mhz"] if bands else 0.0
    metrics: dict[str, float | int] = {
        "peak_frequency_mhz": float(frequencies_hz[peak_index] / 1e6),
        "peak_dbuv_m": float(values[peak_index]),
        "min_dbuv_m": float(np.min(values)),
        "mean_dbuv_m": float(np.mean(values)),
        "median_dbuv_m": float(np.median(values)),
        "std_db": float(np.std(values)),
        "p95_dbuv_m": float(np.percentile(values, 95)),
        "p99_dbuv_m": float(np.percentile(values, 99)),
        "dynamic_range_db": float(np.ptp(values)),
        "centroid_mhz": centroid_hz / 1e6,
        "occupied_ratio": float(np.mean(values >= threshold)),
        "strong_peak_count": len(peaks),
        "noise_floor_dbuv_m": noise_floor,
        "detection_threshold_dbuv_m": threshold,
        "effective_band_count": len(bands),
        "effective_span_mhz": max(0.0, effective_span),
        "effective_total_bandwidth_mhz": float(sum(band["bandwidth_mhz"] for band in bands)),
        "energy_band_low_mhz": float(frequencies_hz[low_index] / 1e6),
        "energy_band_high_mhz": float(frequencies_hz[high_index] / 1e6),
        "energy_bandwidth_mhz": float((frequencies_hz[high_index] - frequencies_hz[low_index]) / 1e6),
    }
    return SpectrumFeatureAnalysis(metrics, tuple(peaks), tuple(bands))


def _extract_features(frequencies_hz: np.ndarray, values: np.ndarray) -> dict[str, object]:
    analysis = analyze_spectrum_features(frequencies_hz, values)
    return {**analysis.metrics, "top_peaks": list(analysis.peaks), "effective_bands": list(analysis.effective_bands)}


def build_feature_library(
    groups: Iterable[SpectrumGroup],
    database_path: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> BuildResult:
    groups = list(groups)
    tasks = [(group, band) for group in groups for band in (*group.bands, ALL_BAND)]
    failures: list[str] = []
    completed = 0
    connection = _connect(database_path)
    try:
        for group, band in tasks:
            label = f"{group.city} / {group.point} / {group.polarization} / {band}"
            try:
                max_hold = calculate_max_hold(group, band)
                features = _extract_features(max_hold.frequencies_hz, max_hold.values_dbuv_m)
                now = datetime.now().isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO spectrum_profiles (
                        city, point, polarization, band, source_file_count, frequency_count,
                        peak_frequency_mhz, peak_dbuv_m, mean_dbuv_m, median_dbuv_m, std_db,
                        p95_dbuv_m, p99_dbuv_m, dynamic_range_db, centroid_mhz,
                        occupied_ratio, strong_peak_count, noise_floor_dbuv_m, top_peaks_json,
                        frequencies_blob, values_blob, updated_at, min_dbuv_m,
                        detection_threshold_dbuv_m, effective_band_count, effective_span_mhz,
                        effective_total_bandwidth_mhz, energy_band_low_mhz, energy_band_high_mhz,
                        energy_bandwidth_mhz, effective_bands_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(city, point, polarization, band) DO UPDATE SET
                        source_file_count=excluded.source_file_count,
                        frequency_count=excluded.frequency_count,
                        peak_frequency_mhz=excluded.peak_frequency_mhz,
                        peak_dbuv_m=excluded.peak_dbuv_m,
                        mean_dbuv_m=excluded.mean_dbuv_m,
                        median_dbuv_m=excluded.median_dbuv_m,
                        std_db=excluded.std_db,
                        p95_dbuv_m=excluded.p95_dbuv_m,
                        p99_dbuv_m=excluded.p99_dbuv_m,
                        dynamic_range_db=excluded.dynamic_range_db,
                        centroid_mhz=excluded.centroid_mhz,
                        occupied_ratio=excluded.occupied_ratio,
                        strong_peak_count=excluded.strong_peak_count,
                        noise_floor_dbuv_m=excluded.noise_floor_dbuv_m,
                        top_peaks_json=excluded.top_peaks_json,
                        frequencies_blob=excluded.frequencies_blob,
                        values_blob=excluded.values_blob,
                        min_dbuv_m=excluded.min_dbuv_m,
                        detection_threshold_dbuv_m=excluded.detection_threshold_dbuv_m,
                        effective_band_count=excluded.effective_band_count,
                        effective_span_mhz=excluded.effective_span_mhz,
                        effective_total_bandwidth_mhz=excluded.effective_total_bandwidth_mhz,
                        energy_band_low_mhz=excluded.energy_band_low_mhz,
                        energy_band_high_mhz=excluded.energy_band_high_mhz,
                        energy_bandwidth_mhz=excluded.energy_bandwidth_mhz,
                        effective_bands_json=excluded.effective_bands_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        group.city, group.point, group.polarization, band,
                        max_hold.source_file_count, int(max_hold.frequencies_hz.size),
                        features["peak_frequency_mhz"], features["peak_dbuv_m"],
                        features["mean_dbuv_m"], features["median_dbuv_m"], features["std_db"],
                        features["p95_dbuv_m"], features["p99_dbuv_m"], features["dynamic_range_db"],
                        features["centroid_mhz"], features["occupied_ratio"], features["strong_peak_count"],
                        features["noise_floor_dbuv_m"], json.dumps(features["top_peaks"], ensure_ascii=False),
                        _pack(max_hold.frequencies_hz), _pack(max_hold.values_dbuv_m), now,
                        features["min_dbuv_m"], features["detection_threshold_dbuv_m"],
                        features["effective_band_count"], features["effective_span_mhz"],
                        features["effective_total_bandwidth_mhz"], features["energy_band_low_mhz"],
                        features["energy_band_high_mhz"], features["energy_bandwidth_mhz"],
                        json.dumps(features["effective_bands"], ensure_ascii=False),
                    ),
                )
                connection.commit()
            except Exception as exc:
                failures.append(f"{label}: {exc}")
            completed += 1
            if progress is not None:
                progress(completed, len(tasks), label)
        profile_count = int(connection.execute("SELECT COUNT(*) FROM spectrum_profiles").fetchone()[0])
    finally:
        connection.close()
    return BuildResult(database_path, len(groups), profile_count, tuple(failures))


def list_feature_records(
    database_path: Path,
    city: str = "全部",
    polarization: str = "全部",
    band: str = ALL_BAND,
    keyword: str = "",
) -> list[FeatureRecord]:
    if not database_path.exists():
        return []
    clauses: list[str] = []
    parameters: list[object] = []
    if city and city != "全部":
        clauses.append("city = ?")
        parameters.append(city)
    if polarization and polarization != "全部":
        clauses.append("polarization = ?")
        parameters.append(polarization)
    if band and band != "全部":
        clauses.append("band = ?")
        parameters.append(band)
    if keyword.strip():
        clauses.append("(point LIKE ? OR city LIKE ?)")
        pattern = f"%{keyword.strip()}%"
        parameters.extend((pattern, pattern))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT id, city, point, polarization, band, source_file_count, frequency_count, "
            "peak_frequency_mhz, peak_dbuv_m, mean_dbuv_m, median_dbuv_m, std_db, "
            "p95_dbuv_m, p99_dbuv_m, dynamic_range_db, centroid_mhz, occupied_ratio, "
            "strong_peak_count, noise_floor_dbuv_m, updated_at, min_dbuv_m, "
            "detection_threshold_dbuv_m, effective_band_count, effective_span_mhz, "
            "effective_total_bandwidth_mhz, energy_band_low_mhz, energy_band_high_mhz, "
            "energy_bandwidth_mhz, top_peaks_json FROM spectrum_profiles" + where +
            " ORDER BY city, point, polarization, band",
            parameters,
        ).fetchall()
        return [FeatureRecord(*row) for row in rows]
    finally:
        connection.close()


def library_filter_values(database_path: Path) -> tuple[list[str], list[str], list[str]]:
    if not database_path.exists():
        return ["全部"], ["全部"], [ALL_BAND]
    connection = _connect(database_path)
    try:
        cities = [row[0] for row in connection.execute("SELECT DISTINCT city FROM spectrum_profiles ORDER BY city")]
        polarizations = [row[0] for row in connection.execute("SELECT DISTINCT polarization FROM spectrum_profiles ORDER BY polarization")]
        bands = [band for band in FEATURE_BANDS if connection.execute("SELECT 1 FROM spectrum_profiles WHERE band=? LIMIT 1", (band,)).fetchone()]
        return ["全部", *cities], ["全部", *polarizations], bands
    finally:
        connection.close()


def _load_spectrum(connection: sqlite3.Connection, record_id: int) -> tuple[np.ndarray, np.ndarray]:
    row = connection.execute(
        "SELECT frequency_count, frequencies_blob, values_blob FROM spectrum_profiles WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"数据库中不存在特征记录 {record_id}")
    return _unpack(row[1], row[0]), _unpack(row[2], row[0])


def load_feature_spectrum(database_path: Path, record_id: int) -> tuple[np.ndarray, np.ndarray]:
    """Load one compressed max-hold spectrum from the feature database."""
    connection = _connect(database_path)
    try:
        return _load_spectrum(connection, record_id)
    finally:
        connection.close()


def compare_feature_records(database_path: Path, records: list[FeatureRecord]) -> ComparisonResult:
    if len(records) < 2:
        raise ValueError("请至少选择两个测点进行对比")
    bands = {record.band for record in records}
    if len(bands) != 1:
        raise ValueError("只能对比同一频段的数据，请先使用频段筛选")

    connection = _connect(database_path)
    try:
        spectra = [_load_spectrum(connection, record.id) for record in records]
    finally:
        connection.close()

    lower = max(float(freq[0]) for freq, _ in spectra)
    upper = min(float(freq[-1]) for freq, _ in spectra)
    if upper <= lower:
        raise ValueError("所选频谱没有共同频率范围")
    grid = np.linspace(lower, upper, 1500)
    interpolated = np.vstack([np.interp(grid, freq, values) for freq, values in spectra])
    correlation = np.corrcoef(interpolated)
    difference = interpolated[:, None, :] - interpolated[None, :, :]
    rms = np.sqrt(np.mean(np.square(difference), axis=2))

    labels = [f"{record.point}-{record.polarization}" for record in records]
    figure = Figure(figsize=(11.0, 7.0), constrained_layout=True)
    axes = figure.subplots(2, 2)
    for (frequencies, values), label in zip(spectra, labels):
        axes[0, 0].plot(frequencies / 1e6, values, linewidth=0.9, alpha=0.85, label=label)
    axes[0, 0].set(title=f"{records[0].band} 最大值保持频谱对比", xlabel="频率 (MHz)", ylabel="电场强度 (dBμV/m)")
    axes[0, 0].grid(True, alpha=0.22)
    axes[0, 0].legend(fontsize=8, loc="best")

    positions = np.arange(len(records))
    axes[0, 1].bar(positions - 0.18, [record.peak_dbuv_m for record in records], width=0.36, label="峰值")
    axes[0, 1].bar(positions + 0.18, [record.p95_dbuv_m for record in records], width=0.36, label="P95")
    axes[0, 1].set(title="关键场强特征", ylabel="dBμV/m", xticks=positions, xticklabels=labels)
    axes[0, 1].tick_params(axis="x", rotation=25, labelsize=8)
    axes[0, 1].legend()
    axes[0, 1].grid(True, axis="y", alpha=0.22)

    image = axes[1, 0].imshow(correlation, vmin=-1, vmax=1, cmap="RdYlBu_r")
    axes[1, 0].set(title="频谱形状相关系数", xticks=positions, yticks=positions, xticklabels=labels, yticklabels=labels)
    axes[1, 0].tick_params(axis="x", rotation=25, labelsize=8)
    axes[1, 0].tick_params(axis="y", labelsize=8)
    for row in range(len(records)):
        for column in range(len(records)):
            axes[1, 0].text(column, row, f"{correlation[row, column]:.2f}", ha="center", va="center", fontsize=8)
    figure.colorbar(image, ax=axes[1, 0], shrink=0.78)

    axes[1, 1].bar(positions, [record.occupied_ratio * 100 for record in records], color="#2a9d8f")
    axes[1, 1].set(title="强信号频点占比（高于中位数 6 dB）", ylabel="占比 (%)", xticks=positions, xticklabels=labels)
    axes[1, 1].tick_params(axis="x", rotation=25, labelsize=8)
    axes[1, 1].grid(True, axis="y", alpha=0.22)

    pairs: list[tuple[float, float, str, str]] = []
    for row in range(len(records)):
        for column in range(row + 1, len(records)):
            pairs.append((float(correlation[row, column]), float(rms[row, column]), labels[row], labels[column]))
    most_similar = max(pairs, key=lambda item: item[0])
    most_different = min(pairs, key=lambda item: item[0])
    summary_lines = [
        f"对比频段：{records[0].band}",
        f"测点数量：{len(records)}",
        f"共同频率范围：{lower / 1e6:.3f} - {upper / 1e6:.3f} MHz",
        "",
        f"频谱形状最相似：{most_similar[2]} 与 {most_similar[3]}",
        f"相关系数：{most_similar[0]:.4f}，RMS 场强差：{most_similar[1]:.3f} dB",
        f"频谱形状差异最大：{most_different[2]} 与 {most_different[3]}",
        f"相关系数：{most_different[0]:.4f}，RMS 场强差：{most_different[1]:.3f} dB",
        "",
        "各测点特征：",
    ]
    for record in records:
        summary_lines.append(
            f"{record.city}/{record.point}/{record.polarization}：峰值 {record.peak_dbuv_m:.2f} dBμV/m "
            f"@ {record.peak_frequency_mhz:.3f} MHz，P95 {record.p95_dbuv_m:.2f} dBμV/m，"
            f"强信号占比 {record.occupied_ratio * 100:.2f}%"
        )
    return ComparisonResult(records, correlation, rms, figure, "\n".join(summary_lines))
