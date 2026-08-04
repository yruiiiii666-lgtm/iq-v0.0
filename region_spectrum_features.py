from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from matplotlib import colormaps
from matplotlib.figure import Figure


@dataclass(frozen=True)
class RegionSignalFeature:
    rank: int
    typical_frequency_mhz: float
    occurrence_count: int
    location_count: int
    occurrence_probability: float
    mean_level_dbuv_m: float
    min_level_dbuv_m: float
    max_level_dbuv_m: float
    level_range_db: float
    mean_bandwidth_3db_mhz: float


@dataclass(frozen=True)
class RegionSpectrumResult:
    scene_type: str
    polarization: str
    band: str
    tolerance_mhz: float
    minimum_probability: float
    location_names: tuple[str, ...]
    signals: tuple[RegionSignalFeature, ...]
    figure: Figure


def analyze_region_spectrum(
    scene_type: str,
    polarization: str,
    band: str,
    location_peaks: dict[str, list[dict[str, float | int]]],
    tolerance_mhz: float,
    minimum_probability: float,
) -> RegionSpectrumResult:
    if tolerance_mhz <= 0:
        raise ValueError("频率匹配容差必须大于 0 MHz")
    if not 0 <= minimum_probability <= 1:
        raise ValueError("最低出现概率必须在 0 到 1 之间")
    location_names = tuple(sorted(location_peaks, key=str.casefold))
    if len(location_names) < 2:
        raise ValueError("区域融合至少需要两个具有频谱特征的采集地点")

    observations: list[dict[str, float | str]] = []
    for location, peaks in location_peaks.items():
        for peak in peaks:
            observations.append({
                "location": location,
                "frequency": float(peak.get("frequency_mhz", 0.0)),
                "level": float(peak.get("field_dbuv_m", 0.0)),
                "bandwidth": float(peak.get("bandwidth_3db_mhz", 0.0)),
            })
    observations.sort(key=lambda item: float(item["frequency"]))
    if not observations:
        raise ValueError("所选区域没有可用于融合的主要峰值")

    clusters: list[dict[str, dict[str, float | str]]] = []
    for observation in observations:
        frequency = float(observation["frequency"])
        candidates: list[tuple[float, int]] = []
        for index, cluster in enumerate(clusters):
            center = float(np.mean([float(item["frequency"]) for item in cluster.values()]))
            distance = abs(frequency - center)
            if distance <= tolerance_mhz:
                candidates.append((distance, index))
        if candidates:
            cluster = clusters[min(candidates)[1]]
            location = str(observation["location"])
            previous = cluster.get(location)
            if previous is None or float(observation["level"]) > float(previous["level"]):
                cluster[location] = observation
        else:
            clusters.append({str(observation["location"]): observation})

    features: list[RegionSignalFeature] = []
    for cluster in clusters:
        items = list(cluster.values())
        probability = len(items) / len(location_names)
        if probability < minimum_probability:
            continue
        frequencies = np.asarray([float(item["frequency"]) for item in items])
        levels = np.asarray([float(item["level"]) for item in items])
        bandwidths = np.asarray([float(item["bandwidth"]) for item in items])
        features.append(RegionSignalFeature(
            rank=0,
            typical_frequency_mhz=float(np.mean(frequencies)),
            occurrence_count=len(items),
            location_count=len(location_names),
            occurrence_probability=probability,
            mean_level_dbuv_m=float(np.mean(levels)),
            min_level_dbuv_m=float(np.min(levels)),
            max_level_dbuv_m=float(np.max(levels)),
            level_range_db=float(np.ptp(levels)),
            mean_bandwidth_3db_mhz=float(np.mean(bandwidths)),
        ))
    features.sort(key=lambda item: (-item.occurrence_probability, -item.mean_level_dbuv_m))
    features = [
        RegionSignalFeature(index, feature.typical_frequency_mhz, feature.occurrence_count, feature.location_count,
                            feature.occurrence_probability, feature.mean_level_dbuv_m, feature.min_level_dbuv_m,
                            feature.max_level_dbuv_m, feature.level_range_db, feature.mean_bandwidth_3db_mhz)
        for index, feature in enumerate(features, 1)
    ]

    figure = Figure(figsize=(11.0, 4.6), constrained_layout=True)
    scatter_ax, probability_ax = figure.subplots(1, 2)
    colors = colormaps["tab20"](np.linspace(0, 1, max(1, len(location_names))))
    for color, location in zip(colors, location_names):
        peaks = location_peaks[location]
        scatter_ax.scatter(
            [float(peak["frequency_mhz"]) for peak in peaks],
            [float(peak["field_dbuv_m"]) for peak in peaks],
            s=24, alpha=0.78, color=color, label=location,
        )
    scatter_ax.set(title="区域峰值频率-场强分布", xlabel="频率 (MHz)", ylabel="峰值场强 (dBμV/m)")
    scatter_ax.grid(True, alpha=0.2)
    scatter_ax.legend(loc="lower center", fontsize=6, ncol=2, framealpha=0.82)

    frequencies = [feature.typical_frequency_mhz for feature in features]
    probabilities = [feature.occurrence_probability * 100 for feature in features]
    probability_ax.bar(range(len(features)), probabilities, color="#2a9d8f")
    probability_ax.set(
        title="典型信号出现概率",
        xlabel="典型频率 (MHz)", ylabel="出现概率 (%)",
        xticks=range(len(features)), xticklabels=[f"{value:.1f}" for value in frequencies],
    )
    probability_ax.tick_params(axis="x", rotation=45, labelsize=7)
    probability_ax.set_ylim(0, 105)
    probability_ax.grid(True, axis="y", alpha=0.2)

    for axis in (scatter_ax, probability_ax):
        axis.title.set_fontsize(9)
        axis.xaxis.label.set_fontsize(8)
        axis.yaxis.label.set_fontsize(8)
        axis.tick_params(axis="both", labelsize=7)
    figure.suptitle(f"{scene_type} / {polarization} / {band} 区域典型电磁环境频谱特征", fontsize=10)
    return RegionSpectrumResult(
        scene_type, polarization, band, tolerance_mhz, minimum_probability,
        location_names, tuple(features), figure,
    )
