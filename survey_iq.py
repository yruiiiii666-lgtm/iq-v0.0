from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal

from iq_reader import get_recording, read_iq_contiguous


def db10(value: float, floor: float = 1e-20) -> float:
    return 10.0 * math.log10(max(value, floor))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a lightweight survey of a large IQR-WV recording.")
    parser.add_argument("stem")
    parser.add_argument("--data-dir", type=Path, default=Path("../data"))
    parser.add_argument("--out-dir", type=Path, default=Path("survey_output"))
    parser.add_argument("--interval-ms", type=float, default=100.0, help="Spacing between snapshots.")
    parser.add_argument("--window-ms", type=float, default=5.0, help="Contiguous data read per snapshot.")
    parser.add_argument("--nfft", type=int, default=8192)
    args = parser.parse_args()

    recording = get_recording(args.data_dir.resolve(), args.stem)
    rate = recording.sample_rate_hz
    interval = max(1, round(args.interval_ms * 1e-3 * rate))
    window = max(args.nfft, round(args.window_ms * 1e-3 * rate))
    starts = np.arange(0, max(recording.total_samples - window + 1, 1), interval, dtype=np.int64)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, float | int]] = []
    psd_sum: np.ndarray | None = None
    frequencies: np.ndarray | None = None
    total_read = 0

    for start in starts:
        iq = read_iq_contiguous(recording, int(start), min(window, recording.total_samples - int(start)))
        power = np.abs(iq) ** 2
        mean_power = float(np.mean(power))
        peak_power = float(np.max(power))
        dc_power = float(abs(np.mean(iq)) ** 2)
        clipping = float(np.mean((np.abs(iq.real) >= 32760 / 32768) | (np.abs(iq.imag) >= 32760 / 32768)))
        frequencies, psd = signal.welch(
            iq,
            fs=rate,
            window="hann",
            nperseg=args.nfft,
            noverlap=args.nfft // 2,
            return_onesided=False,
            scaling="density",
        )
        frequencies = np.fft.fftshift(frequencies)
        psd = np.fft.fftshift(psd)
        psd_sum = psd.astype(np.float64) if psd_sum is None else psd_sum + psd
        rows.append(
            {
                "start_time_s": float(start / rate),
                "samples": int(iq.size),
                "rms_dbfs": db10(mean_power),
                "peak_dbfs": db10(peak_power),
                "papr_db": db10(peak_power / mean_power),
                "dc_dbc": db10(dc_power / mean_power),
                "clipping_percent": clipping * 100.0,
                "i_mean": float(np.mean(iq.real)),
                "q_mean": float(np.mean(iq.imag)),
            }
        )
        total_read += iq.size

    assert psd_sum is not None and frequencies is not None
    mean_psd = psd_sum / len(rows)
    spectral_power = mean_psd / np.sum(mean_psd)
    cumulative = np.cumsum(spectral_power)
    low = int(np.searchsorted(cumulative, 0.005))
    high = int(np.searchsorted(cumulative, 0.995))
    peak_index = int(np.argmax(mean_psd))
    entropy = float(-np.sum(spectral_power * np.log2(np.maximum(spectral_power, 1e-30))) / np.log2(len(spectral_power)))

    csv_path = args.out_dir / f"{recording.stem}_time_features.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    rms_values = np.array([row["rms_dbfs"] for row in rows], dtype=float)
    summary = {
        "recording": recording.stem,
        "center_frequency_mhz": recording.center_frequency_mhz,
        "sample_rate_hz": rate,
        "duration_s": recording.duration_s,
        "total_samples": recording.total_samples,
        "survey_snapshots": len(rows),
        "survey_window_ms": args.window_ms,
        "survey_interval_ms": args.interval_ms,
        "survey_fraction_percent": 100.0 * total_read / recording.total_samples,
        "rms_dbfs_mean": float(np.mean(rms_values)),
        "rms_dbfs_p05": float(np.percentile(rms_values, 5)),
        "rms_dbfs_p50": float(np.percentile(rms_values, 50)),
        "rms_dbfs_p95": float(np.percentile(rms_values, 95)),
        "peak_dbfs_max": float(max(row["peak_dbfs"] for row in rows)),
        "papr_db_max": float(max(row["papr_db"] for row in rows)),
        "clipping_percent_max": float(max(row["clipping_percent"] for row in rows)),
        "dc_dbc_median": float(np.median([row["dc_dbc"] for row in rows])),
        "peak_frequency_offset_hz": float(frequencies[peak_index]),
        "peak_frequency_mhz": float(recording.center_frequency_mhz + frequencies[peak_index] / 1e6),
        "occupied_bandwidth_99_hz": float(frequencies[high] - frequencies[low]),
        "spectral_entropy_normalized": entropy,
    }
    json_path = args.out_dir / f"{recording.stem}_summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True)
    axes[0].plot([row["start_time_s"] for row in rows], rms_values, linewidth=1)
    axes[0].set(xlabel="Time (s)", ylabel="RMS (dBFS)", title=f"{recording.stem}: time survey")
    axes[0].grid(alpha=0.25)
    axes[1].plot(recording.center_frequency_mhz + frequencies / 1e6, 10 * np.log10(np.maximum(mean_psd, 1e-30)))
    axes[1].set(xlabel="Frequency (MHz)", ylabel="PSD (dB/Hz)", title="Mean Welch PSD")
    axes[1].grid(alpha=0.25)
    figure_path = args.out_dir / f"{recording.stem}_survey.png"
    fig.savefig(figure_path, dpi=160)
    plt.close(fig)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {csv_path}, {json_path}, and {figure_path}")


if __name__ == "__main__":
    main()
