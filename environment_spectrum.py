from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from matplotlib import rcParams
from matplotlib.figure import Figure


rcParams["font.sans-serif"] = ["Microsoft YaHei", "Noto Sans SC", "Segoe UI", "SimHei", "Arial"]
rcParams["axes.unicode_minus"] = False


BAND_ORDER = ("30M-200M", "200M-500M", "500M-1G", "1G-6G")


@dataclass(frozen=True)
class SpectrumGroup:
    city: str
    point: str
    polarization: str
    files_by_band: dict[str, tuple[Path, ...]]

    @property
    def bands(self) -> tuple[str, ...]:
        return tuple(band for band in BAND_ORDER if band in self.files_by_band)


@dataclass
class SpectrumResult:
    group: SpectrumGroup
    requested_band: str
    frequencies_hz: np.ndarray
    max_values_dbuv_m: np.ndarray
    winning_files: list[str]
    source_file_count: int
    output_csv: Path
    output_png: Path
    figure: Figure


@dataclass
class MaxHoldData:
    frequencies_hz: np.ndarray
    values_dbuv_m: np.ndarray
    winning_files: list[str]
    source_file_count: int


def _infer_band(path: Path) -> str | None:
    candidates = (path.parent.name, path.stem)
    for candidate in candidates:
        for band in BAND_ORDER:
            if candidate.upper().startswith(band.upper()):
                return band
    return None


def discover_spectrum_groups(root: Path) -> list[SpectrumGroup]:
    root = root.resolve()
    grouped: dict[tuple[str, str, str], dict[str, list[Path]]] = {}
    for path in root.rglob("*.CSV"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) < 4:
            continue
        city, point, polarization = relative.parts[:3]
        band = _infer_band(path)
        if band is None:
            continue
        grouped.setdefault((city, point, polarization), {}).setdefault(band, []).append(path)

    groups = []
    for (city, point, polarization), files_by_band in sorted(grouped.items()):
        groups.append(
            SpectrumGroup(
                city=city,
                point=point,
                polarization=polarization,
                files_by_band={band: tuple(sorted(paths)) for band, paths in files_by_band.items()},
            )
        )
    return groups


def read_max_hold_trace(path: Path) -> tuple[np.ndarray, np.ndarray]:
    frequencies: list[float] = []
    values: list[float] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2:
                continue
            try:
                frequency = float(row[0].strip())
                value = float(row[1].strip())
            except ValueError:
                continue
            if frequency > 0 and np.isfinite(value):
                frequencies.append(frequency)
                values.append(value)
    if not frequencies:
        raise ValueError(f"在 {path} 中未找到 T1 频谱样本")
    return np.asarray(frequencies, dtype=np.float64), np.asarray(values, dtype=np.float64)


def _safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .") or "unnamed"


def calculate_max_hold(group: SpectrumGroup, band: str) -> MaxHoldData:
    selected_bands = group.bands if band == "30M-6G (All)" else (band,)
    selected_files = [path for item in selected_bands for path in group.files_by_band.get(item, ())]
    if not selected_files:
        raise ValueError(f"未找到 {band} 对应的频谱 CSV 文件")

    return calculate_files_max_hold(selected_files)


def calculate_files_max_hold(files: list[Path]) -> MaxHoldData:
    if not files:
        raise ValueError("没有可用于最大值保持的频谱 CSV 文件")
    maxima: dict[float, tuple[float, str]] = {}
    for path in files:
        frequencies, values = read_max_hold_trace(path)
        for frequency, value in zip(frequencies, values):
            key = round(float(frequency), 3)
            previous = maxima.get(key)
            if previous is None or value > previous[0]:
                maxima[key] = (float(value), path.name)

    ordered = sorted(maxima.items())
    frequencies_hz = np.asarray([item[0] for item in ordered], dtype=np.float64)
    max_values = np.asarray([item[1][0] for item in ordered], dtype=np.float64)
    winners = [item[1][1] for item in ordered]

    return MaxHoldData(
        frequencies_hz=frequencies_hz,
        values_dbuv_m=max_values,
        winning_files=winners,
        source_file_count=len(files),
    )


def load_low_frequency_magnetic_spectrum(root: Path, city: str, point: str) -> MaxHoldData | None:
    """Load 9 kHz-30 MHz magnetic-loop measurements without mixing them into E-field profiles."""
    low_frequency_root = root.resolve() / city / point / "低频"
    if not low_frequency_root.is_dir():
        return None
    files = sorted(
        path for path in low_frequency_root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".csv"
    )
    return calculate_files_max_hold(files) if files else None


def aggregate_max_hold(group: SpectrumGroup, band: str, output_root: Path) -> SpectrumResult:
    max_hold = calculate_max_hold(group, band)
    frequencies_hz = max_hold.frequencies_hz
    max_values = max_hold.values_dbuv_m
    winners = max_hold.winning_files

    output_dir = output_root / "environment_spectrum" / _safe_name(group.city) / _safe_name(group.point) / _safe_name(group.polarization)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "30M-6G" if band == "30M-6G (All)" else band
    output_csv = output_dir / f"{_safe_name(group.point)}_{_safe_name(group.polarization)}_{suffix}_max_hold.csv"
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency_hz", "frequency_mhz", "max_hold_dbuv_m", "winning_source_file"])
        for frequency, value, winner in zip(frequencies_hz, max_values, winners):
            writer.writerow([f"{frequency:.3f}", f"{frequency / 1e6:.9f}", f"{value:.9f}", winner])

    figure = Figure(figsize=(10.0, 5.6), constrained_layout=True)
    ax = figure.subplots()
    ax.plot(frequencies_hz / 1e6, max_values, color="#1677b8", linewidth=1.0)
    peak_index = int(np.argmax(max_values))
    peak_frequency = frequencies_hz[peak_index] / 1e6
    peak_value = max_values[peak_index]
    ax.scatter([peak_frequency], [peak_value], color="#e85d04", s=28, zorder=4, label="全局峰值")
    ax.annotate(
        f"{peak_frequency:.3f} MHz\n{peak_value:.2f} dBμV/m",
        xy=(peak_frequency, peak_value),
        xytext=(10, -34),
        textcoords="offset points",
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#e85d04", "alpha": 0.95},
        arrowprops={"arrowstyle": "-", "color": "#e85d04"},
    )
    ax.set(
        title=f"{group.city} / {group.point} / {group.polarization} - {suffix} 最大值保持",
        xlabel="频率 (MHz)",
        ylabel="电场强度 (dBμV/m)",
    )
    ax.set_xlim(float(frequencies_hz[0] / 1e6), float(frequencies_hz[-1] / 1e6))
    if band == "30M-6G (All)":
        for boundary in (200, 500, 1000):
            ax.axvline(boundary, color="#94a3b8", linewidth=0.8, linestyle="--", alpha=0.65)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    output_png = output_dir / f"{_safe_name(group.point)}_{_safe_name(group.polarization)}_{suffix}_max_hold.png"
    figure.savefig(output_png)

    return SpectrumResult(
        group=group,
        requested_band=band,
        frequencies_hz=frequencies_hz,
        max_values_dbuv_m=max_values,
        winning_files=winners,
        source_file_count=max_hold.source_file_count,
        output_csv=output_csv,
        output_png=output_png,
        figure=figure,
    )


def spectrum_result_text(result: SpectrumResult) -> str:
    peak_index = int(np.argmax(result.max_values_dbuv_m))
    return "\n".join(
        [
            f"城市：{result.group.city}",
            f"测点：{result.group.point}",
            f"极化：{result.group.polarization}",
            f"频段：{result.requested_band}",
            f"源 CSV 文件数：{result.source_file_count}",
            f"合并后频点数：{result.frequencies_hz.size:,}",
            f"全局峰值频率：{result.frequencies_hz[peak_index] / 1e6:.6f} MHz",
            f"峰值场强：{result.max_values_dbuv_m[peak_index]:.3f} dBμV/m",
            f"峰值来源文件：{result.winning_files[peak_index]}",
            "",
            f"导出 CSV：{result.output_csv}",
            f"图像文件：{result.output_png}",
        ]
    )
