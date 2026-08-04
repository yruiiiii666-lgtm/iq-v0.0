from __future__ import annotations

import argparse
import csv
from pathlib import Path

from iq_reader import get_recording, read_iq_contiguous


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an IQR-WV recording segment to CSV.")
    parser.add_argument("stem", help="Recording stem, for example miaofu1895g")
    parser.add_argument("--data-dir", type=Path, default=Path("../data"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start-seconds", type=float, default=0.0)
    parser.add_argument("--duration-seconds", type=float, default=0.005)
    parser.add_argument("--chunk-samples", type=int, default=100_000)
    args = parser.parse_args()

    recording = get_recording(args.data_dir.resolve(), args.stem)
    rate = recording.sample_rate_hz
    start = max(0, round(args.start_seconds * rate))
    requested = max(0, round(args.duration_seconds * rate))
    count = min(requested, recording.total_samples - start)
    if count <= 0:
        raise SystemExit("The requested interval is outside the recording.")

    output = args.output or Path(f"{recording.stem}_{args.start_seconds:g}s_{args.duration_seconds:g}s.csv")
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_index", "time_s", "i_raw", "q_raw", "i_normalized", "q_normalized"])
        for offset in range(0, count, args.chunk_samples):
            chunk_count = min(args.chunk_samples, count - offset)
            iq = read_iq_contiguous(recording, start + offset, chunk_count)
            for local_index, value in enumerate(iq):
                sample_index = start + offset + local_index
                i_norm = float(value.real)
                q_norm = float(value.imag)
                writer.writerow(
                    [
                        sample_index,
                        f"{sample_index / rate:.9f}",
                        round(i_norm * 32768),
                        round(q_norm * 32768),
                        f"{i_norm:.9f}",
                        f"{q_norm:.9f}",
                    ]
                )

    print(f"Exported {count:,} samples ({count / rate:.9f} s) to {output.resolve()}")


if __name__ == "__main__":
    main()
