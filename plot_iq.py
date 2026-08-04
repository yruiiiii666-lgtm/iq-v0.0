from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal

from iq_reader import IQRecording, discover_recordings, get_recording, read_iq_contiguous, read_iq_window


ALL_PLOTS = ("time", "constellation", "spectrum", "spectrogram", "histogram", "summary")


def _save_current(path: Path, dpi: int = 150) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(path, dpi=dpi)
    plt.close()


def _db(values: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.abs(values), floor))


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


def _expand_y_for_label(y_values: np.ndarray) -> None:
    y_min = float(np.nanmin(y_values))
    y_max = float(np.nanmax(y_values))
    span = max(y_max - y_min, 1.0)
    plt.ylim(y_min - 0.05 * span, y_max + 0.22 * span)


def plot_time(recording: IQRecording, times: np.ndarray, iq: np.ndarray, out_dir: Path) -> Path:
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(times, iq.real, linewidth=0.7, label="I")
    axes[0].plot(times, iq.imag, linewidth=0.7, label="Q", alpha=0.85)
    axes[0].set_ylabel("Normalized amplitude")
    axes[0].legend(loc="upper right")
    axes[0].grid(True, alpha=0.25)

    amplitude = np.abs(iq)
    axes[1].plot(times, amplitude, linewidth=0.7, color="tab:green")
    axes[1].set_ylabel("|IQ|")
    axes[1].grid(True, alpha=0.25)

    phase = np.unwrap(np.angle(iq))
    axes[2].plot(times, phase, linewidth=0.7, color="tab:orange")
    axes[2].set_ylabel("Phase (rad)")
    axes[2].set_xlabel("Time (s)")
    axes[2].grid(True, alpha=0.25)

    fig.suptitle(f"{recording.stem} - Time Domain")
    path = out_dir / f"{recording.stem}_time.png"
    _save_current(path)
    return path


def plot_constellation(recording: IQRecording, iq: np.ndarray, out_dir: Path) -> Path:
    if iq.size > 80_000:
        iq = iq[np.linspace(0, iq.size - 1, 80_000, dtype=np.int64)]
    plt.figure(figsize=(7, 7))
    plt.scatter(iq.real, iq.imag, s=1, alpha=0.25)
    lim = max(float(np.max(np.abs(iq.real))), float(np.max(np.abs(iq.imag))), 1e-3)
    plt.xlim(-lim, lim)
    plt.ylim(-lim, lim)
    plt.gca().set_aspect("equal", adjustable="box")
    plt.xlabel("I")
    plt.ylabel("Q")
    plt.title(f"{recording.stem} - IQ Constellation")
    plt.grid(True, alpha=0.25)
    path = out_dir / f"{recording.stem}_constellation.png"
    _save_current(path)
    return path


def plot_spectrum(recording: IQRecording, iq: np.ndarray, out_dir: Path) -> Path:
    if iq.size < 16:
        raise ValueError("Not enough samples for spectrum plot")
    nperseg = min(65_536, max(256, 2 ** int(np.floor(np.log2(iq.size // 4 or iq.size)))))
    freq, psd = signal.welch(
        iq,
        fs=recording.sample_rate_hz,
        window="hann",
        nperseg=nperseg,
        return_onesided=False,
        scaling="density",
    )
    order = np.argsort(freq)
    actual_freq_mhz = recording.center_frequency_mhz + freq / 1e6
    psd_db = 10 * np.log10(np.maximum(psd, 1e-20))
    peak_index = int(np.argmax(psd))
    peak_freq_mhz = float(actual_freq_mhz[peak_index])
    peak_db = float(psd_db[peak_index])
    dx, dy, ha, va = _annotation_offset(peak_freq_mhz, peak_db, actual_freq_mhz, psd_db)
    plt.figure(figsize=(12, 5))
    plt.plot(actual_freq_mhz[order], psd_db[order], linewidth=0.8)
    plt.axvline(recording.center_frequency_mhz, color="tab:red", linestyle="--", linewidth=0.9, alpha=0.75, label="Center")
    plt.scatter([peak_freq_mhz], [peak_db], color="tab:orange", s=36, zorder=5, label="Peak")
    plt.annotate(
        f"Peak\n{peak_freq_mhz:.6g} MHz\n{peak_db:.2f} dB",
        xy=(peak_freq_mhz, peak_db),
        xytext=(dx, dy),
        textcoords="offset points",
        arrowprops={"arrowstyle": "->", "color": "tab:orange"},
        fontsize=9,
        ha=ha,
        va=va,
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "tab:orange", "alpha": 0.85},
    )
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("PSD (dB/Hz, relative)")
    plt.title(f"{recording.stem} - Power Spectral Density, center {recording.center_frequency_mhz:g} MHz")
    plt.legend(loc="best")
    plt.grid(True, alpha=0.25)
    plt.margins(x=0.03)
    _expand_y_for_label(psd_db)
    path = out_dir / f"{recording.stem}_spectrum.png"
    _save_current(path)
    return path


def plot_spectrogram(recording: IQRecording, iq: np.ndarray, out_dir: Path) -> Path:
    if iq.size < 256:
        raise ValueError("Not enough samples for spectrogram plot")
    nperseg = min(4096, max(256, 2 ** int(np.floor(np.log2(iq.size // 64 or iq.size)))))
    freq, time, spec = signal.spectrogram(
        iq,
        fs=recording.sample_rate_hz,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg // 2,
        return_onesided=False,
        scaling="density",
        mode="psd",
    )
    freq = np.fft.fftshift(freq)
    actual_freq_mhz = recording.center_frequency_mhz + freq / 1e6
    spec = np.fft.fftshift(spec, axes=0)
    spec_db = 10 * np.log10(np.maximum(spec, 1e-20))
    peak_freq_index, peak_time_index = np.unravel_index(int(np.argmax(spec)), spec.shape)
    peak_freq_mhz = float(actual_freq_mhz[peak_freq_index])
    peak_time_ms = float(time[peak_time_index] * 1e3)
    peak_db = float(spec_db[peak_freq_index, peak_time_index])
    dx, dy, ha, va = _annotation_offset(peak_time_ms, peak_freq_mhz, time * 1e3, actual_freq_mhz)
    plt.figure(figsize=(12, 6))
    plt.pcolormesh(time * 1e3, actual_freq_mhz, spec_db, shading="auto")
    plt.axhline(recording.center_frequency_mhz, color="white", linestyle="--", linewidth=0.9, alpha=0.75)
    plt.scatter([peak_time_ms], [peak_freq_mhz], color="tab:red", s=34, marker="x", linewidths=1.8, label="Peak")
    plt.annotate(
        f"Peak {peak_freq_mhz:.6g} MHz\n{peak_time_ms:.3f} ms, {peak_db:.2f} dB",
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
    plt.xlabel("Time in selected window (ms)")
    plt.ylabel("Frequency (MHz)")
    plt.title(f"{recording.stem} - Spectrogram, center {recording.center_frequency_mhz:g} MHz")
    plt.legend(loc="best")
    plt.colorbar(label="PSD (dB/Hz, relative)")
    path = out_dir / f"{recording.stem}_spectrogram.png"
    _save_current(path)
    return path


def plot_histogram(recording: IQRecording, iq: np.ndarray, out_dir: Path) -> Path:
    amplitude_db = _db(iq)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].hist(iq.real, bins=120, alpha=0.65, label="I")
    axes[0].hist(iq.imag, bins=120, alpha=0.65, label="Q")
    axes[0].set_title("I/Q Amplitude Distribution")
    axes[0].set_xlabel("Normalized amplitude")
    axes[0].set_ylabel("Count")
    axes[0].legend()
    axes[0].grid(True, alpha=0.25)

    axes[1].hist(amplitude_db, bins=120, color="tab:green")
    axes[1].set_title("|IQ| Distribution")
    axes[1].set_xlabel("Magnitude (dBFS)")
    axes[1].set_ylabel("Count")
    axes[1].grid(True, alpha=0.25)

    fig.suptitle(f"{recording.stem} - Histogram")
    path = out_dir / f"{recording.stem}_histogram.png"
    _save_current(path)
    return path


def write_summary(recording: IQRecording, iq: np.ndarray, stride: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    amplitude = np.abs(iq)
    power = amplitude**2
    path = out_dir / f"{recording.stem}_summary.txt"
    lines = [
        f"Recording: {recording.stem}",
        f"Volumes: {len(recording.volumes)}",
        f"Total complex samples: {recording.total_samples:,}",
        f"Sample rate: {recording.sample_rate_hz:.6f} Hz",
        f"Center frequency: {recording.center_frequency_mhz:.6f} MHz",
        f"Frequency span: {recording.center_frequency_mhz - recording.sample_rate_hz / 2e6:.6f} MHz to {recording.center_frequency_mhz + recording.sample_rate_hz / 2e6:.6f} MHz",
        f"Duration: {recording.duration_s:.6f} s",
        f"Reference level: {recording.reference_level_dbm:.3f} dBm",
        f"Analyzed points: {iq.size:,}",
        f"Decimation stride: {stride}",
        f"I mean/std: {float(np.mean(iq.real)):.8f} / {float(np.std(iq.real)):.8f}",
        f"Q mean/std: {float(np.mean(iq.imag)):.8f} / {float(np.std(iq.imag)):.8f}",
        f"Magnitude min/mean/max: {float(np.min(amplitude)):.8f} / {float(np.mean(amplitude)):.8f} / {float(np.max(amplitude)):.8f}",
        f"Power mean: {float(np.mean(power)):.8e}",
        f"Peak magnitude: {20 * np.log10(max(float(np.max(amplitude)), 1e-12)):.3f} dBFS",
        f"RMS magnitude: {20 * np.log10(max(float(np.sqrt(np.mean(power))), 1e-12)):.3f} dBFS",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def analyze_recording(
    recording: IQRecording,
    out_dir: Path,
    plots: tuple[str, ...] = ALL_PLOTS,
    start_s: float = 0.0,
    duration_s: float | None = None,
    max_points: int = 200_000,
    spectrogram_points: int = 1_048_576,
) -> list[Path]:
    start_sample = int(max(start_s, 0.0) * recording.sample_rate_hz)
    sample_count = None if duration_s is None else int(max(duration_s, 0.0) * recording.sample_rate_hz)
    times, iq, stride = read_iq_window(recording, start_sample, sample_count, max_points=max_points)
    if iq.size == 0:
        raise ValueError(f"No samples selected for {recording.stem}")

    out_dir = out_dir / recording.stem
    paths: list[Path] = []
    if "summary" in plots:
        paths.append(write_summary(recording, iq, stride, out_dir))
    if "time" in plots:
        paths.append(plot_time(recording, times, iq, out_dir))
    if "constellation" in plots:
        paths.append(plot_constellation(recording, iq, out_dir))
    if "spectrum" in plots:
        paths.append(plot_spectrum(recording, iq, out_dir))
    if "histogram" in plots:
        paths.append(plot_histogram(recording, iq, out_dir))
    if "spectrogram" in plots:
        contiguous_count = min(spectrogram_points, sample_count or recording.total_samples - start_sample)
        spec_iq = read_iq_contiguous(recording, start_sample, int(contiguous_count))
        paths.append(plot_spectrogram(recording, spec_iq, out_dir))
    return paths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Rohde & Schwarz IQR-WV IQ recordings.")
    parser.add_argument("--data-dir", type=Path, default=Path("../data"), help="Folder containing .ws1/.ws2/.wsm files.")
    parser.add_argument("--recording", default="all", help="Recording stem, for example miaofu869m, or 'all'.")
    parser.add_argument("--out-dir", type=Path, default=Path("output"), help="Output folder for plots and summaries.")
    parser.add_argument("--plots", nargs="+", default=["all"], choices=("all", *ALL_PLOTS), help="Plots to generate.")
    parser.add_argument("--start-sec", type=float, default=0.0, help="Analysis start time in seconds.")
    parser.add_argument("--duration-sec", type=float, default=None, help="Analysis duration in seconds. Default reads full file with decimation.")
    parser.add_argument("--max-points", type=int, default=200_000, help="Maximum decimated points for time/stat plots.")
    parser.add_argument("--spectrogram-points", type=int, default=1_048_576, help="Contiguous points used for spectrogram.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    plots = ALL_PLOTS if "all" in args.plots else tuple(args.plots)
    data_dir = args.data_dir.resolve()
    out_dir = args.out_dir.resolve()

    if args.recording.lower() == "all":
        recordings = discover_recordings(data_dir)
    else:
        recordings = [get_recording(data_dir, args.recording)]

    if not recordings:
        raise SystemExit(f"No recordings found in {data_dir}")

    print("Found recordings:")
    for recording in recordings:
        print("  " + recording.summary)

    for recording in recordings:
        print(f"\nAnalyzing {recording.stem} ...")
        paths = analyze_recording(
            recording=recording,
            out_dir=out_dir,
            plots=plots,
            start_s=args.start_sec,
            duration_s=args.duration_sec,
            max_points=args.max_points,
            spectrogram_points=args.spectrogram_points,
        )
        for path in paths:
            print(f"  wrote {path}")


if __name__ == "__main__":
    main()
