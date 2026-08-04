from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from matplotlib.figure import Figure
from scipy import signal

from iq_reader import IQRecording, read_iq_contiguous, read_iq_window


GUI_PLOTS = ("time", "constellation", "spectrum", "spectrogram", "histogram", "summary")


@dataclass
class AnalysisData:
    recording: IQRecording
    times: np.ndarray
    iq: np.ndarray
    stride: int
    spec_iq: np.ndarray | None
    start_sample: int
    selected_sample_count: int | None


def prepare_analysis_data(
    recording: IQRecording,
    start_s: float,
    duration_s: float | None,
    max_points: int,
    spectrogram_points: int,
    need_spectrogram: bool,
) -> AnalysisData:
    start_sample = int(max(start_s, 0.0) * recording.sample_rate_hz)
    sample_count = None if duration_s is None else int(max(duration_s, 0.0) * recording.sample_rate_hz)
    times, iq, stride = read_iq_window(recording, start_sample, sample_count, max_points=max_points)
    if iq.size == 0:
        raise ValueError(f"{recording.stem} 未选中任何样本")

    spec_iq = None
    if need_spectrogram:
        contiguous_count = min(spectrogram_points, sample_count or recording.total_samples - start_sample)
        spec_iq = read_iq_contiguous(recording, start_sample, int(contiguous_count))

    return AnalysisData(
        recording=recording,
        times=times,
        iq=iq,
        stride=stride,
        spec_iq=spec_iq,
        start_sample=start_sample,
        selected_sample_count=sample_count,
    )


def summary_text(data: AnalysisData) -> str:
    iq = data.iq
    amplitude = np.abs(iq)
    power = amplitude**2
    rec = data.recording
    selected_duration = (data.selected_sample_count / rec.sample_rate_hz) if data.selected_sample_count else rec.duration_s
    return "\n".join(
        [
            f"数据组：{rec.stem}",
            f"数据卷数：{len(rec.volumes)}",
            f"复数样本总数：{rec.total_samples:,}",
            f"采样率：{rec.sample_rate_hz:.6f} Hz",
            f"中心频率：{rec.center_frequency_mhz:.6f} MHz",
            f"频率范围：{rec.center_frequency_mhz - rec.sample_rate_hz / 2e6:.6f} MHz 至 {rec.center_frequency_mhz + rec.sample_rate_hz / 2e6:.6f} MHz",
            f"完整时长：{rec.duration_s:.6f} s",
            f"选中起点：{data.start_sample / rec.sample_rate_hz:.6f} s",
            f"选中时长：{selected_duration:.6f} s",
            f"参考电平：{rec.reference_level_dbm:.3f} dBm",
            f"显示/分析点数：{iq.size:,}",
            f"抽取步长：{data.stride}",
            f"I 均值/标准差：{float(np.mean(iq.real)):.8f} / {float(np.std(iq.real)):.8f}",
            f"Q 均值/标准差：{float(np.mean(iq.imag)):.8f} / {float(np.std(iq.imag)):.8f}",
            f"幅值 最小/平均/最大：{float(np.min(amplitude)):.8f} / {float(np.mean(amplitude)):.8f} / {float(np.max(amplitude)):.8f}",
            f"平均功率：{float(np.mean(power)):.8e}",
            f"峰值幅度：{20 * np.log10(max(float(np.max(amplitude)), 1e-12)):.3f} dBFS",
            f"RMS 幅度：{20 * np.log10(max(float(np.sqrt(np.mean(power))), 1e-12)):.3f} dBFS",
        ]
    )


def make_time_figure(data: AnalysisData) -> Figure:
    fig = Figure(figsize=(8.0, 5.2), constrained_layout=True)
    axes = fig.subplots(3, 1, sharex=True)
    times = data.times
    iq = data.iq
    axes[0].plot(times, iq.real, linewidth=0.8, label="I")
    axes[0].plot(times, iq.imag, linewidth=0.8, label="Q", alpha=0.85)
    axes[0].set_ylabel("幅度")
    axes[0].legend(loc="upper right")
    axes[0].grid(True, alpha=0.25)

    axes[1].plot(times, np.abs(iq), linewidth=0.8, color="tab:green")
    axes[1].set_ylabel("|IQ|")
    axes[1].grid(True, alpha=0.25)

    axes[2].plot(times, np.unwrap(np.angle(iq)), linewidth=0.8, color="tab:orange")
    axes[2].set_ylabel("相位 (rad)")
    axes[2].set_xlabel("时间 (s)")
    axes[2].grid(True, alpha=0.25)
    fig.suptitle(f"IQ 时域波形 | 中心频率 {data.recording.center_frequency_mhz:g} MHz")
    return fig


def make_constellation_figure(data: AnalysisData) -> Figure:
    iq = data.iq
    if iq.size > 80_000:
        iq = iq[np.linspace(0, iq.size - 1, 80_000, dtype=np.int64)]
    fig = Figure(figsize=(6.2, 5.6), constrained_layout=True)
    ax = fig.subplots()
    ax.scatter(iq.real, iq.imag, s=1, alpha=0.25)
    lim = max(float(np.max(np.abs(iq.real))), float(np.max(np.abs(iq.imag))), 1e-3)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("I")
    ax.set_ylabel("Q")
    ax.set_title(f"IQ 星座图 | 中心频率 {data.recording.center_frequency_mhz:g} MHz")
    ax.grid(True, alpha=0.25)
    return fig


def _annotation_offset(x: float, y: float, x_values: np.ndarray, y_values: np.ndarray) -> tuple[int, int, str, str]:
    x_min = float(np.nanmin(x_values))
    x_max = float(np.nanmax(x_values))
    y_min = float(np.nanmin(y_values))
    y_max = float(np.nanmax(y_values))
    x_fraction = 0.5 if x_max == x_min else (x - x_min) / (x_max - x_min)
    y_fraction = 0.5 if y_max == y_min else (y - y_min) / (y_max - y_min)
    dx = -88 if x_fraction > 0.72 else 12
    dy = -54 if y_fraction > 0.68 else 14
    ha = "right" if dx < 0 else "left"
    va = "top" if dy < 0 else "bottom"
    return dx, dy, ha, va


def _expand_y_for_label(ax, y_values: np.ndarray) -> None:
    y_min = float(np.nanmin(y_values))
    y_max = float(np.nanmax(y_values))
    span = max(y_max - y_min, 1.0)
    ax.set_ylim(y_min - 0.05 * span, y_max + 0.22 * span)


def make_spectrum_figure(data: AnalysisData) -> Figure:
    iq = data.iq
    if iq.size < 16:
        raise ValueError("用于绘制频谱的样本数不足")
    nperseg = min(65_536, max(256, 2 ** int(np.floor(np.log2(iq.size // 4 or iq.size)))))
    freq, psd = signal.welch(
        iq,
        fs=data.recording.sample_rate_hz,
        window="hann",
        nperseg=nperseg,
        return_onesided=False,
        scaling="density",
    )
    order = np.argsort(freq)
    actual_freq_mhz = data.recording.center_frequency_mhz + freq / 1e6
    psd_db = 10 * np.log10(np.maximum(psd, 1e-20))
    peak_index = int(np.argmax(psd))
    peak_freq_mhz = float(actual_freq_mhz[peak_index])
    peak_db = float(psd_db[peak_index])
    dx, dy, ha, va = _annotation_offset(peak_freq_mhz, peak_db, actual_freq_mhz, psd_db)
    fig = Figure(figsize=(8.0, 4.8), constrained_layout=True)
    ax = fig.subplots()
    ax.plot(actual_freq_mhz[order], psd_db[order], linewidth=0.9)
    ax.axvline(data.recording.center_frequency_mhz, color="tab:red", linestyle="--", linewidth=0.9, alpha=0.75, label="中心频率")
    ax.scatter([peak_freq_mhz], [peak_db], color="tab:orange", s=36, zorder=5, label="峰值")
    ax.annotate(
        f"峰值\n{peak_freq_mhz:.6g} MHz\n{peak_db:.2f} dB",
        xy=(peak_freq_mhz, peak_db),
        xytext=(dx, dy),
        textcoords="offset points",
        arrowprops={"arrowstyle": "->", "color": "tab:orange"},
        fontsize=9,
        ha=ha,
        va=va,
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "tab:orange", "alpha": 0.85},
    )
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("PSD (dB/Hz，相对值)")
    ax.set_title(f"功率谱密度 | 中心频率 {data.recording.center_frequency_mhz:g} MHz")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.25)
    ax.margins(x=0.03)
    _expand_y_for_label(ax, psd_db)
    return fig


def make_spectrogram_figure(data: AnalysisData) -> Figure:
    iq = data.spec_iq
    if iq is None or iq.size < 256:
        raise ValueError("用于绘制时频图的样本数不足")
    nperseg = min(4096, max(256, 2 ** int(np.floor(np.log2(iq.size // 64 or iq.size)))))
    freq, time, spec = signal.spectrogram(
        iq,
        fs=data.recording.sample_rate_hz,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg // 2,
        return_onesided=False,
        scaling="density",
        mode="psd",
    )
    freq = np.fft.fftshift(freq)
    actual_freq_mhz = data.recording.center_frequency_mhz + freq / 1e6
    spec = np.fft.fftshift(spec, axes=0)
    spec_db = 10 * np.log10(np.maximum(spec, 1e-20))
    peak_freq_index, peak_time_index = np.unravel_index(int(np.argmax(spec)), spec.shape)
    peak_freq_mhz = float(actual_freq_mhz[peak_freq_index])
    peak_time_ms = float(time[peak_time_index] * 1e3)
    peak_db = float(spec_db[peak_freq_index, peak_time_index])
    dx, dy, ha, va = _annotation_offset(peak_time_ms, peak_freq_mhz, time * 1e3, actual_freq_mhz)
    fig = Figure(figsize=(8.0, 5.0), constrained_layout=True)
    ax = fig.subplots()
    mesh = ax.pcolormesh(time * 1e3, actual_freq_mhz, spec_db, shading="auto")
    ax.axhline(data.recording.center_frequency_mhz, color="white", linestyle="--", linewidth=0.9, alpha=0.75)
    ax.scatter([peak_time_ms], [peak_freq_mhz], color="tab:red", s=34, marker="x", linewidths=1.8, label="峰值")
    ax.annotate(
        f"峰值 {peak_freq_mhz:.6g} MHz\n{peak_time_ms:.3f} ms，{peak_db:.2f} dB",
        xy=(peak_time_ms, peak_freq_mhz),
        xytext=(dx, dy),
        textcoords="offset points",
        color="white",
        fontsize=9,
        ha=ha,
        va=va,
        arrowprops={"arrowstyle": "->", "color": "white"},
        bbox={"boxstyle": "round,pad=0.25", "fc": "black", "ec": "white", "alpha": 0.45},
    )
    ax.set_xlabel("选中窗口内时间 (ms)")
    ax.set_ylabel("频率 (MHz)")
    ax.set_title(f"时频图 | 中心频率 {data.recording.center_frequency_mhz:g} MHz")
    ax.legend(loc="best")
    fig.colorbar(mesh, ax=ax, label="PSD (dB/Hz，相对值)")
    return fig


def make_histogram_figure(data: AnalysisData) -> Figure:
    iq = data.iq
    amplitude_db = 20 * np.log10(np.maximum(np.abs(iq), 1e-12))
    fig = Figure(figsize=(8.0, 4.8), constrained_layout=True)
    axes = fig.subplots(1, 2)
    axes[0].hist(iq.real, bins=120, alpha=0.65, label="I")
    axes[0].hist(iq.imag, bins=120, alpha=0.65, label="Q")
    axes[0].set_title("I/Q 分布")
    axes[0].set_xlabel("归一化幅度")
    axes[0].set_ylabel("数量")
    axes[0].legend()
    axes[0].grid(True, alpha=0.25)

    axes[1].hist(amplitude_db, bins=120, color="tab:green")
    axes[1].set_title("|IQ| 分布")
    axes[1].set_xlabel("幅值 (dBFS)")
    axes[1].set_ylabel("数量")
    axes[1].grid(True, alpha=0.25)
    fig.suptitle(f"IQ 幅度统计 | 中心频率 {data.recording.center_frequency_mhz:g} MHz")
    return fig


FIGURE_BUILDERS = {
    "time": make_time_figure,
    "constellation": make_constellation_figure,
    "spectrum": make_spectrum_figure,
    "spectrogram": make_spectrogram_figure,
    "histogram": make_histogram_figure,
}
