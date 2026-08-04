from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from matplotlib import rcParams
from matplotlib.figure import Figure

from iq_reader import IQRecording, read_iq_contiguous


rcParams["font.sans-serif"] = ["Microsoft YaHei", "Noto Sans SC", "Segoe UI", "SimHei", "Arial"]
rcParams["axes.unicode_minus"] = False


@dataclass(frozen=True)
class WindowFeature:
    start_sample: int
    start_time_s: float
    rms_dbfs: float
    peak_dbfs: float
    papr_db: float
    dc_dbc: float
    iq_power_ratio_db: float
    spectral_centroid_norm: float
    occupied_bandwidth_norm: float
    spectral_entropy: float
    spectral_peak_ratio_db: float
    outlier_score: float = 0.0


@dataclass(frozen=True)
class DetectedSegment:
    reason: str
    source_window_start_s: float
    extract_start_s: float
    duration_s: float
    rms_dbfs: float
    peak_dbfs: float
    papr_db: float
    outlier_score: float
    output_file: str


@dataclass
class DetectionResult:
    recording: IQRecording
    features: list[WindowFeature]
    segments: list[DetectedSegment]
    output_dir: Path
    figure: Figure
    saved_to_disk: bool


def _db10(value: float, floor: float = 1e-20) -> float:
    return 10.0 * math.log10(max(float(value), floor))


def _window_feature(recording: IQRecording, start: int, count: int, fft_points: int = 8192) -> WindowFeature:
    iq = read_iq_contiguous(recording, start, count)
    power = np.abs(iq) ** 2
    mean_power = float(np.mean(power))
    peak_power = float(np.max(power))
    i_power = float(np.mean(iq.real**2))
    q_power = float(np.mean(iq.imag**2))

    if iq.size > fft_points:
        fft_indices = np.linspace(0, iq.size - 1, fft_points, dtype=np.int64)
        fft_iq = iq[fft_indices]
    else:
        fft_iq = iq
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(fft_iq * np.hanning(fft_iq.size)))) ** 2
    spectrum = spectrum.astype(np.float64)
    spectrum /= max(float(np.sum(spectrum)), 1e-30)
    axis = np.linspace(-1.0, 1.0, spectrum.size, endpoint=False)
    centroid = float(np.sum(axis * spectrum))
    cumulative = np.cumsum(spectrum)
    low = int(np.searchsorted(cumulative, 0.005))
    high = min(int(np.searchsorted(cumulative, 0.995)), spectrum.size - 1)
    entropy = float(-np.sum(spectrum * np.log2(np.maximum(spectrum, 1e-30))) / np.log2(spectrum.size))

    return WindowFeature(
        start_sample=start,
        start_time_s=start / recording.sample_rate_hz,
        rms_dbfs=_db10(mean_power),
        peak_dbfs=_db10(peak_power),
        papr_db=_db10(peak_power / max(mean_power, 1e-30)),
        dc_dbc=_db10(abs(np.mean(iq)) ** 2 / max(mean_power, 1e-30)),
        iq_power_ratio_db=_db10(i_power / max(q_power, 1e-30)),
        spectral_centroid_norm=centroid,
        occupied_bandwidth_norm=float(axis[high] - axis[low]),
        spectral_entropy=entropy,
        spectral_peak_ratio_db=_db10(float(np.max(spectrum)) / max(float(np.median(spectrum)), 1e-30)),
    )


def _power_window_feature(recording: IQRecording, start: int, count: int) -> WindowFeature:
    """Calculate only the inexpensive metrics needed to locate the strongest window."""
    iq = read_iq_contiguous(recording, start, count)
    power = np.abs(iq) ** 2
    mean_power = float(np.mean(power))
    peak_power = float(np.max(power))
    i_power = float(np.mean(iq.real**2))
    q_power = float(np.mean(iq.imag**2))
    return WindowFeature(
        start_sample=start,
        start_time_s=start / recording.sample_rate_hz,
        rms_dbfs=_db10(mean_power),
        peak_dbfs=_db10(peak_power),
        papr_db=_db10(peak_power / max(mean_power, 1e-30)),
        dc_dbc=_db10(abs(np.mean(iq)) ** 2 / max(mean_power, 1e-30)),
        iq_power_ratio_db=_db10(i_power / max(q_power, 1e-30)),
        spectral_centroid_norm=0.0,
        occupied_bandwidth_norm=0.0,
        spectral_entropy=0.0,
        spectral_peak_ratio_db=0.0,
        outlier_score=0.0,
    )


def _add_outlier_scores(features: list[WindowFeature]) -> list[WindowFeature]:
    matrix = np.array(
        [
            [
                item.rms_dbfs,
                item.peak_dbfs,
                item.papr_db,
                item.dc_dbc,
                item.iq_power_ratio_db,
                item.spectral_centroid_norm,
                item.occupied_bandwidth_norm,
                item.spectral_entropy,
                item.spectral_peak_ratio_db,
            ]
            for item in features
        ],
        dtype=np.float64,
    )
    median = np.median(matrix, axis=0)
    mad = np.median(np.abs(matrix - median), axis=0)
    scale = np.maximum(1.4826 * mad, 1e-9)
    robust_z = np.clip((matrix - median) / scale, -20.0, 20.0)
    scores = np.sqrt(np.mean(robust_z**2, axis=1))
    return [WindowFeature(**{**asdict(item), "outlier_score": float(score)}) for item, score in zip(features, scores)]


def _choose_distinct(features: list[WindowFeature], extract_samples: int) -> list[tuple[str, WindowFeature]]:
    rankings = (
        ("strongest_power", sorted(features, key=lambda item: item.rms_dbfs, reverse=True)),
        ("maximum_amplitude", sorted(features, key=lambda item: item.peak_dbfs, reverse=True)),
        ("most_unusual", sorted(features, key=lambda item: item.outlier_score, reverse=True)),
    )
    selected: list[tuple[str, WindowFeature]] = []
    for reason, ranking in rankings:
        choice = next(
            (
                candidate
                for candidate in ranking
                if all(abs(candidate.start_sample - existing.start_sample) >= extract_samples for _, existing in selected)
            ),
            ranking[0],
        )
        selected.append((reason, choice))
    return selected


def detect_representative_segments(
    recording: IQRecording,
    output_root: Path,
    window_ms: float = 2.0,
    interval_ms: float = 10.0,
    extract_ms: float = 20.0,
    max_windows: int = 3000,
    progress: Callable[[float, str], None] | None = None,
    save_output: bool = False,
) -> DetectionResult:
    def report(percent: float, stage: str) -> None:
        if progress is not None:
            progress(max(0.0, min(float(percent), 100.0)), stage)

    report(0, "正在准备扫描窗口")
    rate = recording.sample_rate_hz
    window_samples = max(8192, round(window_ms * 1e-3 * rate))
    interval_samples = max(window_samples, round(interval_ms * 1e-3 * rate))
    extract_samples = max(window_samples, round(extract_ms * 1e-3 * rate))
    last_start = max(0, recording.total_samples - window_samples)
    starts = np.arange(0, last_start + 1, interval_samples, dtype=np.int64)
    if starts.size > max_windows:
        starts = np.unique(np.linspace(0, last_start, max_windows, dtype=np.int64))

    features: list[WindowFeature] = []
    total_windows = max(int(starts.size), 1)
    update_interval = max(1, total_windows // 200)
    for index, start in enumerate(starts, start=1):
        features.append(_power_window_feature(recording, int(start), window_samples))
        if index == total_windows or index % update_interval == 0:
            report(90.0 * index / total_windows, f"正在扫描窗口功率：{index:,}/{total_windows:,}")

    report(91, "正在定位平均功率最强片段")
    choices = [("strongest_power", max(features, key=lambda item: item.rms_dbfs))]

    output_dir = output_root / "detected_segments" / recording.stem
    if save_output:
        output_dir.mkdir(parents=True, exist_ok=True)
    segments: list[DetectedSegment] = []
    strongest_iq = np.empty(0, dtype=np.complex64)
    half = extract_samples // 2
    for choice_index, (reason, feature) in enumerate(choices, start=1):
        center = feature.start_sample + window_samples // 2
        extract_start = max(0, min(center - half, recording.total_samples - extract_samples))
        count = min(extract_samples, recording.total_samples - extract_start)
        iq = read_iq_contiguous(recording, extract_start, count)
        strongest_iq = iq
        output_file = output_dir / f"{recording.stem}_{reason}_{extract_start / rate:.6f}s.npy"
        if save_output:
            np.save(output_file, iq, allow_pickle=False)
        segments.append(
            DetectedSegment(
                reason=reason,
                source_window_start_s=feature.start_time_s,
                extract_start_s=extract_start / rate,
                duration_s=count / rate,
                rms_dbfs=feature.rms_dbfs,
                peak_dbfs=feature.peak_dbfs,
                papr_db=feature.papr_db,
                outlier_score=feature.outlier_score,
                output_file=str(output_file.resolve()) if save_output else "",
            )
        )
        report(94, "正在提取平均功率最强片段")

    report(95, "正在整理检测结果" if not save_output else "正在写入特征与摘要文件")
    if save_output:
        feature_path = output_dir / f"{recording.stem}_window_features.csv"
        feature_fields = (
            "start_sample", "start_time_s", "rms_dbfs", "peak_dbfs",
            "papr_db", "dc_dbc", "iq_power_ratio_db",
        )
        with feature_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=feature_fields)
            writer.writeheader()
            writer.writerows({field: getattr(item, field) for field in feature_fields} for item in features)
        summary_path = output_dir / f"{recording.stem}_strongest_power.json"
        summary_path.write_text(json.dumps([asdict(item) for item in segments], ensure_ascii=False, indent=2), encoding="utf-8")

    report(97, "正在生成检测结果图")
    figure = Figure(figsize=(9.2, 6.4), constrained_layout=True)
    ax_power, ax_segment, ax_spectrum = figure.subplots(3, 1)
    times = np.array([item.start_time_s for item in features])
    ax_power.plot(times, [item.rms_dbfs for item in features], linewidth=0.9, color="#2563eb")
    ax_power.set_ylabel("RMS (dBFS)")
    ax_power.set_xlabel("记录时间 (s)")
    ax_power.set_title(f"{recording.stem}：平均功率最强片段")
    ax_power.grid(alpha=0.25)
    segment = segments[0]
    ax_power.axvspan(
        segment.extract_start_s,
        segment.extract_start_s + segment.duration_s,
        color="#dc2626",
        alpha=0.18,
        label="提取片段",
    )
    ax_power.axvline(segment.source_window_start_s, color="#dc2626", linewidth=1.2, label="最强功率窗口")
    ax_power.legend(loc="best")

    block = max(1, math.ceil(strongest_iq.size / 5000))
    usable = strongest_iq.size // block * block
    if usable:
        block_power = np.mean(np.abs(strongest_iq[:usable].reshape(-1, block)) ** 2, axis=1)
        local_time_ms = (np.arange(block_power.size) * block + block / 2.0) / rate * 1e3
        ax_segment.plot(local_time_ms, 10.0 * np.log10(np.maximum(block_power, 1e-20)), color="#0f766e", linewidth=0.8)
    ax_segment.set(xlabel="片段内时间 (ms)", ylabel="功率 (dBFS)", title="最强片段时域功率包络")
    ax_segment.grid(alpha=0.25)

    fft_points = min(131072, 2 ** int(math.floor(math.log2(max(strongest_iq.size, 2)))))
    fft_iq = strongest_iq[:fft_points]
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(fft_iq * np.hanning(fft_points)))) ** 2
    spectrum_db = 10.0 * np.log10(np.maximum(spectrum, 1e-20))
    spectrum_db -= float(np.max(spectrum_db))
    frequency_mhz = recording.center_frequency_mhz + np.fft.fftshift(np.fft.fftfreq(fft_points, 1.0 / rate)) / 1e6
    ax_spectrum.plot(frequency_mhz, spectrum_db, color="#7c3aed", linewidth=0.8)
    ax_spectrum.set(xlabel="频率 (MHz)", ylabel="相对功率 (dB)", title="最强片段频谱")
    ax_spectrum.grid(alpha=0.25)
    if save_output:
        figure.savefig(output_dir / f"{recording.stem}_detection.png")
    report(100, "自动检测完成")
    return DetectionResult(recording, features, segments, output_dir, figure, save_output)


def result_text(result: DetectionResult) -> str:
    segment = result.segments[0]
    lines = [
        f"数据组：{result.recording.stem}",
        f"扫描窗口数：{len(result.features):,}",
        "检测结论：以下片段为全记录中扫描窗口平均功率最强的位置",
        f"文件输出：{'已保存到 ' + str(result.output_dir) if result.saved_to_disk else '未启用，结果仅显示在界面'}",
        "",
        "平均功率最强片段",
        f"   提取起始时间：{segment.extract_start_s:.6f} s",
        f"   提取持续时间：{segment.duration_s * 1e3:.3f} ms",
        f"   最强功率窗口位置：{segment.source_window_start_s:.6f} s",
        f"   窗口RMS功率：{segment.rms_dbfs:.3f} dBFS",
        f"   窗口峰值：{segment.peak_dbfs:.3f} dBFS",
        f"   峰均比：{segment.papr_db:.3f} dB",
        *( [f"   文件：{segment.output_file}"] if segment.output_file else [] ),
    ]
    return "\n".join(lines)
