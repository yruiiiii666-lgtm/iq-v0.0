from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np

if TYPE_CHECKING:
    from signal_reconstruction import ReconstructionResult


HARD_MAX_DIGITAL_PEAK_DBFS = 0.0
DEFAULT_DIGITAL_PEAK_DBFS = -1.0
DEFAULT_RF_SAFETY_LIMIT_DBM = 0.0
DEFAULT_MAX_SAMPLE_RATE_HZ = 100e6
PLAYBACK_COMMAND_PROFILE = "IQR_PLAYER_COMMANDS_V1"
VERIFIED_PROFILE_NAME = "verified_playback_profile.json"


@dataclass(frozen=True)
class IQRDeviceRecording:
    """One native IQR stream assembled from the e:/.ws1 and f:/.ws2 files."""

    stem: str
    waveform_path: str
    ws1_size_bytes: int | None
    ws2_size_bytes: int | None

    @property
    def is_complete(self) -> bool:
        return bool(
            self.ws1_size_bytes is not None
            and self.ws1_size_bytes > 0
            and self.ws2_size_bytes is not None
            and self.ws2_size_bytes > 0
        )

    @property
    def issue(self) -> str:
        missing: list[str] = []
        if not self.ws1_size_bytes:
            missing.append("e:/.ws1")
        if not self.ws2_size_bytes:
            missing.append("f:/.ws2")
        return "缺少 " + "、".join(missing) if missing else ""


def iqr_recording_key(value: str) -> str:
    text = value.strip().strip("'\"").replace("\\", "/")
    leaf = text.rsplit("/", 1)[-1]
    for suffix in (".ws1", ".ws2", ".wsm"):
        if leaf.casefold().endswith(suffix):
            leaf = leaf[: -len(suffix)]
            break
    return leaf.casefold()


def normalize_iqr_waveform_path(value: str) -> str:
    """Return the IQR player path, rooted on e: and without a file extension."""

    text = value.strip().strip("'\"").replace("\\", "/")
    if not text:
        raise ValueError("记录仪波形路径不能为空。")
    if len(text) >= 2 and text[1] == ":" and text[0].casefold() in ("e", "f"):
        relative = text[2:].lstrip("/")
    else:
        relative = text.rsplit("/", 1)[-1]
    for suffix in (".ws1", ".ws2", ".wsm"):
        if relative.casefold().endswith(suffix):
            relative = relative[: -len(suffix)]
            break
    if not relative:
        raise ValueError("记录仪波形名称不能为空。")
    return f"e:/{relative}"


def parse_iqr_catalog_response(response: str) -> tuple[tuple[str, str, int], ...]:
    """Parse MMEMory:CATalog? while preserving file names that contain commas."""

    try:
        fields = next(csv.reader([response.strip()], skipinitialspace=True))
    except (csv.Error, StopIteration) as exc:
        raise ValueError(f"IQR目录响应无法解析：{response!r}") from exc
    entries: list[tuple[str, str, int]] = []
    for field in fields[2:]:
        parts = [part.strip() for part in field.rsplit(",", 2)]
        if len(parts) != 3:
            continue
        name, file_type, size_text = parts
        try:
            size = int(float(size_text))
        except ValueError:
            continue
        entries.append((name, file_type.upper(), size))
    return tuple(entries)


def scpi_error_is_clear(response: str) -> bool:
    return response.strip().split(",", 1)[0].strip() in ("0", "+0")


def load_verified_playback_profile(path: Path | None = None) -> dict | None:
    profile_path = path or (
        Path(__file__).resolve().parent.parent / "test" / VERIFIED_PROFILE_NAME
    )
    if not profile_path.exists():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("compatible") is not True or payload.get("simulated") is True:
        return None
    if payload.get("command_profile") != PLAYBACK_COMMAND_PROFILE:
        return None
    devices = payload.get("devices", {})
    if not devices.get("smw", {}).get("visa_address"):
        return None
    if not devices.get("recorder", {}).get("visa_address"):
        return None
    payload["profile_path"] = str(profile_path)
    return payload


@dataclass(frozen=True)
class PlaybackSettings:
    route: str
    digital_peak_dbfs: float
    rf_level_dbm: float
    rf_safety_limit_dbm: float
    run_mode: str
    requested_duration_s: float
    external_10mhz_reference: bool = False
    iqr_display_mode: str = "IQ"
    maximum_sample_rate_hz: float = DEFAULT_MAX_SAMPLE_RATE_HZ


@dataclass(frozen=True)
class PlaybackPackage:
    directory: Path
    waveform_file: Path
    metadata_file: Path
    scaled_iq: np.ndarray
    applied_gain_db: float
    peak_dbfs: float
    rms_dbfs: float
    papr_db: float


def safe_waveform_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value).strip("._") or "playback"


def safe_instrument_waveform_name(value: str, max_length: int = 48) -> str:
    """Build a deterministic ASCII-only filename for an R&S instrument.

    Local package paths may retain Chinese names, but the SMBV100A remote file
    command is an ASCII SCPI header.  A short hash preserves uniqueness when
    transliteration removes some or all non-ASCII characters.
    """

    source = str(value).strip() or "playback"
    normalized = unicodedata.normalize("NFKD", source)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^0-9A-Za-z._-]+", "_", ascii_text).strip("._")
    contains_non_ascii = any(ord(character) > 127 for character in source)
    if contains_non_ascii:
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
        base_limit = max(1, max_length - len(digest) - 1)
        ascii_name = f"{(ascii_name or 'waveform')[:base_limit]}_{digest}"
    else:
        ascii_name = ascii_name or "playback"
    return ascii_name[:max_length].strip("._") or "playback"


def validate_playback_settings(result: ReconstructionResult, settings: PlaybackSettings) -> tuple[str, ...]:
    errors: list[str] = []
    if not np.isfinite(settings.digital_peak_dbfs):
        errors.append("数字IQ峰值必须是有限数值。")
    elif settings.digital_peak_dbfs > HARD_MAX_DIGITAL_PEAK_DBFS + 1e-9:
        errors.append("数字IQ峰值不得超过0 dBFS，否则会发生数字削顶。")
    elif settings.digital_peak_dbfs < -80.0:
        errors.append("数字IQ峰值不应低于-80 dBFS。")
    if not np.isfinite(settings.rf_level_dbm):
        errors.append("射频输出电平必须是有限数值。")
    elif settings.rf_level_dbm > settings.rf_safety_limit_dbm + 1e-9:
        errors.append(
            f"射频输出电平{settings.rf_level_dbm:g} dBm超过软件安全上限"
            f"{settings.rf_safety_limit_dbm:g} dBm。"
        )
    if settings.requested_duration_s <= 0:
        errors.append("目标回放时长必须大于0。")
    if result.sample_rate_hz <= 0 or result.sample_rate_hz > settings.maximum_sample_rate_hz:
        errors.append(
            f"采样率{result.sample_rate_hz / 1e6:g} MS/s超过当前回放配置允许的"
            f"{settings.maximum_sample_rate_hz / 1e6:g} MS/s。"
        )
    if result.iq.size < 32 or not np.all(np.isfinite(result.iq)):
        errors.append("重构IQ为空、过短或包含无效数值。")
    return tuple(errors)


def scale_iq_to_peak(iq: np.ndarray, target_peak_dbfs: float) -> tuple[np.ndarray, float]:
    if target_peak_dbfs > HARD_MAX_DIGITAL_PEAK_DBFS + 1e-9:
        raise ValueError("数字IQ峰值不得超过0 dBFS。")
    source = np.asarray(iq, dtype=np.complex64)
    current_peak = float(np.max(np.abs(source)))
    if current_peak <= 0 or not math.isfinite(current_peak):
        raise ValueError("IQ波形没有有效幅度。")
    target_peak = 10.0 ** (target_peak_dbfs / 20.0)
    gain = target_peak / current_peak
    scaled = (source * gain).astype(np.complex64)
    applied_gain_db = 20.0 * math.log10(max(gain, 1e-20))
    return scaled, applied_gain_db


def iq_level_metrics(iq: np.ndarray) -> tuple[float, float, float]:
    magnitude = np.abs(np.asarray(iq))
    peak = float(np.max(magnitude))
    rms = float(np.sqrt(np.mean(np.square(magnitude, dtype=np.float64))))
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-20))
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-20))
    return peak_dbfs, rms_dbfs, peak_dbfs - rms_dbfs


def write_smu_wv(path: Path, iq: np.ndarray, sample_rate_hz: float) -> Path:
    """Write the R&S SMU-WV format documented in application note 1MA299."""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.asarray(iq, dtype=np.complex64)
    peak = float(np.max(np.abs(samples)))
    if peak > 1.0 + 1e-6:
        raise ValueError("SMU-WV样本峰值超过满量程。")
    peak_dbfs, rms_dbfs, _papr = iq_level_metrics(samples)
    # R&S LEVEL OFFS stores positive backoff values relative to full scale.
    rms_backoff = -rms_dbfs
    peak_backoff = -peak_dbfs
    quantized_i = np.clip(np.rint(samples.real * 32767.0), -32768, 32767).astype("<i2")
    quantized_q = np.clip(np.rint(samples.imag * 32767.0), -32768, 32767).astype("<i2")
    interleaved = np.empty(samples.size * 2, dtype="<i2")
    interleaved[0::2] = quantized_i
    interleaved[1::2] = quantized_q
    date_text = datetime.now().strftime("%Y-%m-%d;%H:%M:%S")
    header = (
        "{TYPE: SMU-WV,0}"
        "{COMMENT: Generated by IQ Data Analyzer}"
        f"{{DATE: {date_text}}}"
        f"{{LEVEL OFFS: {rms_backoff:.9g}, {peak_backoff:.9g}}}"
        f"{{CLOCK: {sample_rate_hz:.12g}}}"
        f"{{SAMPLES: {samples.size}}}"
        f"{{WAVEFORM-{4 * samples.size + 1}:#"
    ).encode("ascii")
    with path.open("wb") as stream:
        stream.write(header)
        stream.write(interleaved.tobytes(order="C"))
        stream.write(b"}")
    return path


def prepare_playback_package(
    result: ReconstructionResult,
    settings: PlaybackSettings,
    output_root: Path,
) -> PlaybackPackage:
    errors = validate_playback_settings(result, settings)
    if errors:
        raise ValueError("\n".join(errors))
    scaled, applied_gain_db = scale_iq_to_peak(result.iq, settings.digital_peak_dbfs)
    peak_dbfs, rms_dbfs, papr_db = iq_level_metrics(scaled)
    safe_name = safe_waveform_name(result.name)
    directory = output_root / "playback_packages" / safe_name
    waveform_file = write_smu_wv(directory / f"{safe_name}.wv", scaled, result.sample_rate_hz)
    metadata_file = directory / f"{safe_name}_playback.json"
    payload = {
        "name": result.name,
        "reconstruction_mode": result.mode,
        "center_frequency_mhz": result.center_frequency_mhz,
        "sample_rate_hz": result.sample_rate_hz,
        "buffer_duration_s": result.duration_s,
        "requested_duration_s": settings.requested_duration_s,
        "waveform_format": "R&S SMU-WV, int16 little-endian interleaved I/Q",
        "settings": asdict(settings),
        "digital_level": {
            "hard_max_peak_dbfs": HARD_MAX_DIGITAL_PEAK_DBFS,
            "peak_dbfs": peak_dbfs,
            "rms_dbfs": rms_dbfs,
            "papr_db": papr_db,
            "applied_gain_db": applied_gain_db,
        },
        "device_note": (
            "The .wv file can be loaded by a compatible R&S SMW/SMBV ARB. "
            "IQR import of reconstructed RAW data requires the IQR-K101 option."
        ),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    metadata_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return PlaybackPackage(
        directory, waveform_file, metadata_file, scaled, applied_gain_db,
        peak_dbfs, rms_dbfs, papr_db,
    )


def ieee_block(payload: bytes) -> bytes:
    length = str(len(payload)).encode("ascii")
    return b"#" + str(len(length)).encode("ascii") + length + payload


class VisaPlaybackSession:
    """Small pyvisa wrapper. RF is always forced off when a session is configured."""

    def __init__(self, logger: Callable[[str], None] | None = None) -> None:
        self.logger = logger or (lambda _message: None)
        self.resource_manager = None
        self.smw = None
        self.iqr = None
        self._iqr_paused = False
        self._iqr_display_mode = "IQ"
        self._iqr_lock = threading.RLock()
        self._smw_lock = threading.RLock()

    @property
    def iqr_paused(self) -> bool:
        return self._iqr_paused

    def _open(self, address: str):
        import pyvisa

        if self.resource_manager is None:
            try:
                self.resource_manager = pyvisa.ResourceManager()
            except Exception as vendor_error:
                try:
                    self.resource_manager = pyvisa.ResourceManager("@py")
                except Exception:
                    raise RuntimeError(
                        "没有找到可用的 VISA 后端。请检查便携包是否完整，"
                        "或安装仪器厂商提供的 VISA 驱动。"
                    ) from vendor_error
        resource = self.resource_manager.open_resource(address)
        resource.timeout = 10_000
        resource.chunk_size = 1024 * 1024
        resource.write_termination = "\n"
        resource.read_termination = "\n"
        return resource

    def connect(self, smw_address: str, iqr_address: str = "") -> tuple[str, str]:
        self.close()
        self.smw = self._open(smw_address)
        smw_id = str(self.smw.query("*IDN?")).strip()
        iqr_id = "未连接"
        if iqr_address.strip():
            self.iqr = self._open(iqr_address.strip())
            iqr_id = str(self.iqr.query("*IDN?")).strip()
        self.logger(f"SMBV100A连接成功：{smw_id}")
        if self.iqr is not None:
            self.logger(f"IQW/IQR连接成功：{iqr_id}")
        return smw_id, iqr_id

    def upload_and_configure_smw(
        self,
        package: PlaybackPackage,
        center_frequency_mhz: float,
        rf_level_dbm: float,
    ) -> str:
        if self.smw is None:
            raise RuntimeError("SMBV100A尚未连接。")
        instrument_name = safe_instrument_waveform_name(package.waveform_file.stem) + ".wv"
        remote_path = f"/var/user/{instrument_name}"
        payload = package.waveform_file.read_bytes()
        command = f"MMEMory:DATA '{remote_path}',".encode("ascii") + ieee_block(payload) + b"\n"
        self.smw.write_raw(command)
        self.smw.query("*OPC?")
        self.smw.write("OUTPut1:STATe OFF")
        self.smw.write(f"SOURce1:FREQuency:CW {center_frequency_mhz * 1e6:.12g}")
        self.smw.write(f"SOURce1:POWer:POWer {rf_level_dbm:.9g}")
        self.smw.write(f"SOURce1:BB:ARBitrary:WAVeform:SELect '{remote_path}'")
        self.smw.write("SOURce1:BB:ARBitrary:STATe ON")
        error = str(self.smw.query("SYSTem:ERRor?")).strip()
        self.logger(f"SMBV100A波形已上传并配置，RF保持关闭：{remote_path}")
        self.logger(f"SMBV100A状态：{error}")
        return error

    def configure_external_digital_iq(
        self,
        sample_rate_hz: float,
        center_frequency_mhz: float,
        rf_level_dbm: float,
    ) -> str:
        if self.smw is None:
            raise RuntimeError("SMBV100A尚未连接。")
        self.smw.write("OUTPut1:STATe OFF")
        self.smw.write(f"SOURce1:FREQuency:CW {center_frequency_mhz * 1e6:.12g}")
        self.smw.write(f"SOURce1:POWer:POWer {rf_level_dbm:.9g}")
        self.smw.write("SOURce1:BBIN:MODE DIGital")
        # Seed the expected value, then let the DIG I/Q input clock determine
        # the actual rate. USER mode requires a separate shared reference and
        # can leave an IQR-to-SMBV stream stalled when that reference is absent.
        self.smw.write("SOURce1:BBIN:SRATe:SOURce USER")
        self.smw.write(f"SOURce1:BBIN:SRATe {sample_rate_hz:.12g}")
        self.smw.write("SOURce1:BBIN:SRATe:SOURce DIN")
        self.smw.write("SOURce1:BBIN:STATe ON")
        self.smw.query("*OPC?")
        rate_source = str(self.smw.query("SOURce1:BBIN:SRATe:SOURce?")).strip()
        bbin_state = str(self.smw.query("SOURce1:BBIN:STATe?")).strip()
        try:
            connected_device = str(self.smw.query("SOURce1:BBIN:CDEVice?")).strip()
        except Exception as exc:
            connected_device = f"未查询到（{exc}）"
        error = str(self.smw.query("SYSTem:ERRor?")).strip()
        if not scpi_error_is_clear(error):
            raise RuntimeError(f"SMBV100A数字IQ输入配置失败：{error}")
        if "DIN" not in rate_source.upper():
            raise RuntimeError(
                f"SMBV100A采样率来源配置失败：请求DIN（Digital I/Q In），设备回读{rate_source}。"
            )
        if bbin_state.upper() not in ("1", "ON"):
            raise RuntimeError(f"SMBV100A数字基带输入未开启，设备回读{bbin_state}。")
        self.logger(
            "SMBV100A数字基带输入已配置：采样率来源DIN（Digital I/Q In），"
            f"连接设备{connected_device or 'None'}，RF保持关闭。"
        )
        return f"{error}｜BBIN {bbin_state}｜时钟DIN｜连接设备：{connected_device or 'None'}"

    def verify_smw_digital_iq_stream(self, timeout_s: float = 2.0) -> str:
        if self.smw is None:
            raise RuntimeError("SMBV100A尚未连接。")
        rate_source = str(self.smw.query("SOURce1:BBIN:SRATe:SOURce?")).strip()
        bbin_state = str(self.smw.query("SOURce1:BBIN:STATe?")).strip()
        deadline = time.monotonic() + timeout_s
        fifo_status = ""
        while True:
            fifo_status = str(
                self.smw.query("SOURce1:BBIN:SRATe:FIFO:STATus?")
            ).strip()
            if fifo_status.upper() == "OK" or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        try:
            connected_device = str(self.smw.query("SOURce1:BBIN:CDEVice?")).strip()
        except Exception:
            connected_device = "未查询到"
        if "DIN" not in rate_source.upper():
            raise RuntimeError(f"SMBV100A数字IQ采样率来源不是DIN：{rate_source}")
        if bbin_state.upper() not in ("1", "ON"):
            raise RuntimeError(f"SMBV100A数字基带输入未开启：{bbin_state}")
        if fifo_status.upper() != "OK":
            meaning = "数据供给不足/链路无数据" if "URUN" in fifo_status.upper() else "输入数据率过高"
            raise RuntimeError(
                f"SMBV100A数字IQ FIFO状态为{fifo_status}（{meaning}）。"
                "请检查26针数字IQ线、SMBV100A-K18选件和参考时钟连接。"
            )
        return (
            f"BBIN {bbin_state}｜时钟{rate_source}｜FIFO {fifo_status}｜"
            f"连接设备：{connected_device or 'None'}"
        )

    def list_iqr_recordings(self) -> tuple[IQRDeviceRecording, ...]:
        """Read flat native IQR records from e: (.ws1) and f: (.ws2)."""

        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        try:
            drives = str(self.iqr.query("MMEMory:DRIVes?")).strip()
            self.logger(f"IQR可用磁盘：{drives}")
        except Exception as exc:
            self.logger(f"IQR磁盘列表查询失败，继续直接读取e:/f:：{exc}")

        e_entries = parse_iqr_catalog_response(
            str(self.iqr.query("MMEMory:CATalog? 'e:'")).strip()
        )
        f_entries = parse_iqr_catalog_response(
            str(self.iqr.query("MMEMory:CATalog? 'f:'")).strip()
        )
        ws1: dict[str, tuple[str, int]] = {}
        ws2: dict[str, tuple[str, int]] = {}
        for name, file_type, size in e_entries:
            if file_type != "DIR" and name.casefold().endswith(".ws1"):
                stem = name[:-4]
                ws1[iqr_recording_key(stem)] = (stem, size)
        for name, file_type, size in f_entries:
            if file_type != "DIR" and name.casefold().endswith(".ws2"):
                stem = name[:-4]
                ws2[iqr_recording_key(stem)] = (stem, size)

        recordings: list[IQRDeviceRecording] = []
        for key in sorted(set(ws1) | set(ws2)):
            stem = ws1[key][0] if key in ws1 else ws2[key][0]
            recordings.append(
                IQRDeviceRecording(
                    stem=stem,
                    waveform_path=f"e:/{stem}",
                    ws1_size_bytes=ws1[key][1] if key in ws1 else None,
                    ws2_size_bytes=ws2[key][1] if key in ws2 else None,
                )
            )
        complete_count = sum(item.is_complete for item in recordings)
        self.logger(
            f"IQR目录读取完成：{complete_count}条完整记录，"
            f"{len(recordings) - complete_count}条不完整记录。"
        )
        return tuple(recordings)

    def _wait_iqr_player_ready(self, timeout_s: float = 30.0) -> str:
        assert self.iqr is not None
        deadline = time.monotonic() + timeout_s
        state = ""
        while time.monotonic() < deadline:
            state = self.query_iqr_player_state()
            if "please wait" not in state.casefold():
                return state
            time.sleep(0.2)
        raise TimeoutError(f"IQR加载记录超时，最后状态：{state or '无响应'}")

    def _wait_iqr_player_armed_for_lan(
        self,
        timeout_s: float = 15.0,
        stable_s: float = 0.6,
    ) -> str:
        """Wait until the LAN-trigger state remains stable before EXECute.

        Firmware 04.10.x can expose the LAN wait text shortly before its command
        loop is ready to consume an EXECute event.  Requiring a stable interval
        prevents an automatic scene transition from triggering much faster than
        a human-operated first replay.
        """

        assert self.iqr is not None
        deadline = time.monotonic() + timeout_s
        state = ""
        previous_state = ""
        lan_wait_started_at: float | None = None
        while time.monotonic() < deadline:
            state = self.query_iqr_player_state()
            if state != previous_state:
                self.logger(f"IQR Player重新武装状态：{state or '空响应'}")
                previous_state = state
            normalized = " ".join(state.casefold().split())
            if normalized.startswith("waiting for lan remote trigger"):
                if lan_wait_started_at is None:
                    lan_wait_started_at = time.monotonic()
                if time.monotonic() - lan_wait_started_at >= max(0.0, stable_s):
                    return state
            else:
                lan_wait_started_at = None
            if "error" in normalized or "failed" in normalized:
                raise RuntimeError(f"IQR Player重新武装失败，设备状态：{state}")
            time.sleep(0.1)
        raise TimeoutError(
            "等待IQR进入LAN远程触发就绪状态超时，"
            f"最后状态：{state or '无响应'}"
        )

    def _wait_iqr_player_disarmed(self, timeout_s: float = 15.0) -> str:
        """Wait until STOP/ARM OFF has returned the player to Ready."""

        assert self.iqr is not None
        deadline = time.monotonic() + timeout_s
        state = ""
        previous_state = ""
        while time.monotonic() < deadline:
            state = self.query_iqr_player_state()
            if state != previous_state:
                self.logger(f"IQR Player解除武装状态：{state or '空响应'}")
                previous_state = state
            normalized = " ".join(state.casefold().split())
            if normalized == "ready":
                return state
            if "error" in normalized or "failed" in normalized:
                raise RuntimeError(f"IQR Player解除武装失败，设备状态：{state}")
            time.sleep(0.1)
        raise TimeoutError(
            f"等待IQR停止并解除武装超时，最后状态：{state or '无响应'}"
        )

    def reset_iqr_player_for_recording_switch(self, timeout_s: float = 15.0) -> str:
        """Clear the previous one-shot trigger latch before loading another file.

        On IQR firmware 04.10.x, STOP can leave the LAN trigger system armed.  A
        following ``ARM ON`` is then not a new arm edge and the next EXECute
        event can be ignored even though the front panel says it is waiting for
        a LAN command.  Force OFF and wait for Ready before selecting the next
        waveform so every queue item starts from a clean trigger state.
        """

        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        with self._iqr_lock:
            self.iqr.write("TRIGger:PLAYer:STOP")
            self.iqr.write("TRIGger:PLAYer:ARM OFF")
            state = self._wait_iqr_player_disarmed(timeout_s=timeout_s)
            self._iqr_paused = False
        self.logger(f"IQR Player已停止并解除武装，可切换记录：{state}。")
        return state

    @staticmethod
    def _parse_iqr_player_state(response: object) -> str:
        """Return the readable state from an IQR SCPI string response.

        IQR firmware returns player states as SCPI strings, for example
        ``"Running"`` and ``"Ready"``.  PyVISA deliberately keeps those
        surrounding quotes, so comparing its raw response with ``Running``
        makes the UI miss both the start and completion transitions.
        """

        state = str(response).strip()
        if len(state) >= 2 and state[0] == state[-1] and state[0] in ('"', "'"):
            state = state[1:-1].strip()
        return state

    def query_iqr_player_state(self) -> str:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        with self._iqr_lock:
            response = self.iqr.query("TRIGger:PLAYer:STATe?")
        return self._parse_iqr_player_state(response)

    def query_iqr_player_run_mode(self) -> str:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        with self._iqr_lock:
            response = str(self.iqr.query("TRIGger:PLAYer:MODE?")).strip()
        upper = response.upper()
        if "CONT" in upper:
            return "CONTinuous"
        if "SING" in upper:
            return "SINGle"
        return response

    def set_iqr_player_run_mode(self, continuous: bool) -> str:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        requested = "CONTinuous" if continuous else "SINGle"
        with self._iqr_lock:
            self.iqr.write(f"TRIGger:PLAYer:MODE {requested}")
            active = self.query_iqr_player_run_mode()
        expected_prefix = "CONT" if continuous else "SING"
        if expected_prefix not in active.upper():
            raise RuntimeError(
                f"IQR循环模式校验失败：请求{requested}，设备回读{active or '空响应'}。"
            )
        self.logger(f"IQR Player运行方式已回读确认：{active}。")
        return active

    def wait_iqr_player_running(
        self,
        timeout_s: float = 120.0,
        poll_interval_s: float = 0.25,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """Wait until the IQR reports that samples are actually being replayed."""

        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        deadline = time.monotonic() + timeout_s
        state = ""
        previous_state = ""
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("IQR回放启动等待已取消。")
            state = self.query_iqr_player_state()
            if state != previous_state:
                self.logger(f"IQR Player启动状态：{state or '空响应'}")
                previous_state = state
            normalized = " ".join(state.casefold().split())
            if normalized == "running" or normalized.startswith("running "):
                return state
            if "error" in normalized or "failed" in normalized:
                raise RuntimeError(f"IQR Player启动失败，设备状态：{state}")
            time.sleep(max(0.05, poll_interval_s))
        error_text = ""
        try:
            error_text = str(self.iqr.query("SYSTem:ERRor?")).strip()
        except Exception:
            pass
        detail = f"；设备错误：{error_text}" if error_text and not scpi_error_is_clear(error_text) else ""
        raise TimeoutError(
            f"等待IQR开始回放超时（{timeout_s:g} s），最后状态：{state or '无响应'}{detail}"
        )

    def query_iqr_replayed_samples(self) -> float:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        with self._iqr_lock:
            response = str(self.iqr.query("OUTPut:IQ:SAMPles?")).strip()
        try:
            return float(response)
        except ValueError as exc:
            raise RuntimeError(f"IQR回放样本计数无法解析：{response!r}") from exc

    def wait_iqr_player_complete(
        self,
        timeout_s: float,
        poll_interval_s: float = 0.25,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[str, float]:
        """Wait for a started single replay cycle to return to an idle trigger state."""

        deadline = time.monotonic() + max(1.0, timeout_s)
        state = ""
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("IQR单次回放完成监测已取消。")
            state = self.query_iqr_player_state()
            normalized = " ".join(state.casefold().split())
            if (
                normalized == "ready"
                or normalized.startswith("waiting for lan remote trigger")
                or normalized.startswith("press \"play\\rec\" button to start")
                or normalized.startswith("waiting for trigger signal")
                or normalized.startswith("waiting for time to elapse")
            ):
                samples = self.query_iqr_replayed_samples()
                self.logger(f"IQR Player单次回放完成：{state}｜{samples:g} Sa。")
                return state, samples
            if "error" in normalized or "failed" in normalized:
                raise RuntimeError(f"IQR Player单次回放异常结束：{state}")
            time.sleep(max(0.05, poll_interval_s))
        raise TimeoutError(f"等待IQR单次回放完成超时，最后状态：{state or '无响应'}")

    def wait_iqr_stream_active(
        self,
        running_timeout_s: float = 120.0,
        sample_timeout_s: float = 8.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[str, float]:
        """Require both Running state and an advancing hardware sample counter."""

        state = self.wait_iqr_player_running(
            timeout_s=running_timeout_s,
            poll_interval_s=0.2,
            cancel_check=cancel_check,
        )
        deadline = time.monotonic() + sample_timeout_s
        initial_samples = self.query_iqr_replayed_samples()
        samples = initial_samples
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("IQR数据流启动等待已取消。")
            samples = self.query_iqr_replayed_samples()
            if samples > initial_samples:
                self.logger(
                    f"IQR硬件样本计数已增长：{initial_samples:g} → {samples:g} Sa。"
                )
                return state, samples
            time.sleep(0.1)

        assert self.iqr is not None
        diagnostics: list[str] = []
        for label, command in (
            ("数字IQ目标", "OUTPut1:SYSTem:INSTrument:DESTination:STATus?"),
            ("采样时钟", "INPut:CLOCk:SOURce?"),
            ("10MHz参考", "SYSTem:REFerence:FREQuency:SOURce?"),
        ):
            try:
                diagnostics.append(f"{label}={str(self.iqr.query(command)).strip()}")
            except Exception as exc:
                diagnostics.append(f"{label}=查询失败({exc})")
        raise RuntimeError(
            "IQR100虽显示Running，但OUTPut:IQ:SAMPles?样本计数持续未增长"
            f"（{initial_samples:g} Sa），实际数据流没有启动。"
            + "｜"
            + "｜".join(diagnostics)
            + "。请检查IQR DIGITAL IQ OUT到SMBV BASEBAND DIGITAL IN的26针线；"
            "若选择外部10 MHz参考，还必须连接SMBV REF OUT到IQR REF IN，否则请取消该选项。"
        )

    def set_iqr_player_display_mode(self, display_mode: str = "IQ") -> str:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        normalized = display_mode.strip().upper()
        if normalized not in ("IQ", "FFT"):
            raise ValueError("IQR Player显示模式必须是IQ或FFT。")
        with self._iqr_lock:
            self.iqr.write(f"MEASure:SPECtrum:PLAYer:MODE {normalized}")
            self._iqr_display_mode = normalized
        self.logger(f"IQR Player屏幕显示已切换为{'I/Q波形' if normalized == 'IQ' else 'FFT频谱'}。")
        return normalized

    def load_iqr_recording(
        self,
        waveform_path: str,
        continuous: bool,
        external_10mhz_reference: bool = False,
        display_mode: str = "IQ",
    ) -> str:
        if self.iqr is None:
            raise RuntimeError("IQW/IQR记录仪尚未连接。")
        normalized_path = normalize_iqr_waveform_path(waveform_path)
        escaped = normalized_path.replace("'", "''")
        # A scene queue loads a different native recording after every SINGle
        # cycle.  STOP alone does not reliably clear the 04.10.x trigger latch;
        # finish a real OFF -> ON arm cycle around every waveform selection.
        self.reset_iqr_player_for_recording_switch()
        with self._iqr_lock:
            self.iqr.write("INSTrument:SELect:MODE PLAYer")
            self.iqr.write("INSTrument:SELect:TYPE STReam")
            active_display_mode = self.set_iqr_player_display_mode(display_mode)
            self.iqr.write(
                "SYSTem:REFerence:FREQuency:SOURce "
                + ("EXTernal" if external_10mhz_reference else "INTernal")
            )
            self.iqr.write("INPut:CLOCk:SOURce INTernal")
            self.iqr.write(f"OUTPut:PLAYer:WAVeform:SELect '{escaped}'")
            load_state = self._wait_iqr_player_ready()
            try:
                destination = str(
                    self.iqr.query("OUTPut:SYSTem:INSTrument:DESTination:IDENtification?")
                ).strip()
            except Exception as exc:
                destination = f"未查询到（{exc}）"
            destination_status = str(
                self.iqr.query("OUTPut1:SYSTem:INSTrument:DESTination:STATus?")
            ).strip()
            if destination_status.upper() not in ("1", "ON"):
                raise RuntimeError(
                    "IQR100未检测到数字IQ输出目标。请检查IQR DIGITAL IQ OUT到"
                    f"SMBV BASEBAND DIGITAL IN的26针连接线；设备回读{destination_status}。"
                )
            self.iqr.write("TRIGger:PLAYer:SYNC SALone")
            active_run_mode = self.set_iqr_player_run_mode(continuous)
            self.iqr.write("TRIGger:PLAYer:SOURce LAN")
            self.iqr.write("TRIGger:PLAYer:ARM ON")
            state = self._wait_iqr_player_armed_for_lan()
            error = str(self.iqr.query("SYSTem:ERRor?")).strip()
            if not scpi_error_is_clear(error):
                raise RuntimeError(f"IQR记录加载失败：{error}")
            self._iqr_paused = False
        self.logger(
            f"IQR记录已装载：{normalized_path}｜加载状态：{load_state}｜"
            f"目标设备：{destination}（连接{destination_status}）｜采样时钟：Internal｜"
            f"10 MHz参考：{'External' if external_10mhz_reference else 'Internal'}"
        )
        display_text = "I/Q波形" if active_display_mode == "IQ" else "FFT频谱"
        return (
            f"{state}｜{normalized_path}｜Player显示：{display_text}｜"
            f"运行方式：{active_run_mode}（已回读）｜目标连接：{destination_status}"
        )

    def start(self, use_iqr: bool = False, iqr_display_mode: str = "IQ") -> None:
        if use_iqr:
            if self.iqr is None:
                raise RuntimeError("IQW/IQR记录仪尚未连接。")
            with self._iqr_lock:
                normalized_display_mode = iqr_display_mode.strip().upper()
                if normalized_display_mode != self._iqr_display_mode:
                    self.set_iqr_player_display_mode(normalized_display_mode)
                if self._iqr_paused:
                    self.iqr.write("TRIGger:PLAYer:STARt")
                else:
                    # Loading a recording already leaves the IQR armed and
                    # waiting for the LAN trigger.  Do not disarm that first
                    # cycle: doing so can make the following EXECute event get
                    # lost.  After a completed SINGle cycle the state is Ready;
                    # only that branch needs ARM ON followed by an explicit
                    # wait for the LAN-trigger state.
                    current_state = self.query_iqr_player_state()
                    normalized = " ".join(current_state.casefold().split())
                    if normalized.startswith("waiting for lan remote trigger"):
                        armed_state = current_state
                    else:
                        self.iqr.write("TRIGger:PLAYer:SOURce LAN")
                        self.iqr.write("TRIGger:PLAYer:ARM ON")
                        armed_state = self._wait_iqr_player_armed_for_lan()
                    self.logger(f"IQR Player已等待LAN触发：{armed_state}。")
                    self.iqr.write("TRIGger:PLAYer:EXECute")
                self._iqr_paused = False
        self.logger("回放已启动；RF是否打开由独立安全联锁控制。")

    def start_iqr_with_stream_confirmation(
        self,
        iqr_display_mode: str = "IQ",
        stream_timeout_s: float = 3.0,
        retry_delay_s: float = 0.8,
    ) -> tuple[str, bool]:
        """Trigger IQR and require actual digital-IQ data at the SMBV input.

        IQR state queries can block while native data is replaying, so the
        positive start acknowledgement comes from the SMBV digital-input FIFO.
        If no stream arrives and IQR still answers that it is waiting for LAN,
        the first event was lost; resend EXECute once after a settling delay.
        """

        self.start(use_iqr=True, iqr_display_mode=iqr_display_mode)
        try:
            return self.verify_smw_digital_iq_stream(timeout_s=stream_timeout_s), False
        except Exception as first_error:
            state = self.query_iqr_player_state()
            normalized = " ".join(state.casefold().split())
            if not normalized.startswith("waiting for lan remote trigger"):
                raise RuntimeError(
                    f"IQR触发后未确认到数字IQ数据流；Player状态：{state or '无响应'}；"
                    f"SMBV100A：{first_error}"
                ) from first_error

            self.logger(
                "IQR仍在等待LAN触发，首次EXECute未生效；"
                f"等待{max(0.0, retry_delay_s):g} s后重发一次。"
            )
            if retry_delay_s > 0:
                time.sleep(retry_delay_s)
            self.start(use_iqr=True, iqr_display_mode=iqr_display_mode)
            try:
                link_status = self.verify_smw_digital_iq_stream(
                    timeout_s=stream_timeout_s
                )
            except Exception as retry_error:
                retry_state = self.query_iqr_player_state()
                error_text = ""
                try:
                    assert self.iqr is not None
                    error_text = str(self.iqr.query("SYSTem:ERRor?")).strip()
                except Exception:
                    pass
                detail = f"；IQR错误：{error_text}" if error_text else ""
                raise RuntimeError(
                    "IQR LAN触发重试后仍没有实际数字IQ数据流；"
                    f"Player状态：{retry_state or '无响应'}；SMBV100A：{retry_error}{detail}"
                ) from retry_error
            self.logger("IQR LAN触发重试成功，SMBV100A数字IQ FIFO已收到数据。")
            return link_status, True

    def pause(self, use_iqr: bool = False) -> None:
        if use_iqr and self.iqr is not None:
            with self._iqr_lock:
                self.iqr.write("TRIGger:PLAYer:PAUSe")
                self._iqr_paused = True

    def stop(self, use_iqr: bool = False) -> None:
        if use_iqr and self.iqr is not None:
            with self._iqr_lock:
                self.iqr.write("TRIGger:PLAYer:STOP")
                self._iqr_paused = False

    def query_rf_enabled(self) -> bool:
        if self.smw is None:
            raise RuntimeError("SMBV100A尚未连接。")
        with self._smw_lock:
            response = str(self.smw.query("OUTPut1:STATe?")).strip().strip('"').upper()
        if response in ("1", "ON"):
            return True
        if response in ("0", "OFF"):
            return False
        raise RuntimeError(f"SMBV100A RF状态无法解析：{response!r}")

    def set_rf(self, enabled: bool) -> bool:
        if self.smw is None:
            raise RuntimeError("SMBV100A尚未连接。")
        with self._smw_lock:
            self.smw.write(f"OUTPut1:STATe {'ON' if enabled else 'OFF'}")
            actual = self.query_rf_enabled()
        if actual != enabled:
            raise RuntimeError(
                f"SMBV100A RF切换未生效：请求{'ON' if enabled else 'OFF'}，"
                f"设备回读{'ON' if actual else 'OFF'}。"
            )
        self.logger(f"SMBV100A RF输出已回读确认：{'ON' if actual else 'OFF'}")
        return actual

    def _return_resource_to_local(self, resource, label: str) -> str | None:
        visa_error: Exception | None = None
        try:
            from pyvisa.constants import RENLineOperation

            resource.control_ren(RENLineOperation.deassert_gtl)
            self.logger(f"{label}已通过VISA Go To Local返回面板控制。")
            return None
        except Exception as exc:
            visa_error = exc
        try:
            resource.write("&GTL")
            self.logger(f"{label}已通过&GTL返回面板控制。")
            return None
        except Exception as fallback_error:
            message = (
                f"{label}退出远程控制失败：VISA GTL={visa_error}；"
                f"&GTL={fallback_error}"
            )
            self.logger(message)
            return message

    def close(self, return_to_local: bool = True) -> tuple[str, ...]:
        warnings: list[str] = []
        resources = ((self.iqr, "IQR100"), (self.smw, "SMBV100A"))
        for resource, label in resources:
            if resource is not None:
                try:
                    if resource is self.iqr:
                        resource.write("TRIGger:PLAYer:STOP")
                    else:
                        resource.write("OUTPut1:STATe OFF")
                except Exception as exc:
                    warning = f"{label}断开前安全停止失败：{exc}"
                    warnings.append(warning)
                    self.logger(warning)
                if return_to_local:
                    local_warning = self._return_resource_to_local(resource, label)
                    if local_warning:
                        warnings.append(local_warning)
                try:
                    resource.close()
                except Exception as exc:
                    warning = f"{label} VISA资源关闭失败：{exc}"
                    warnings.append(warning)
                    self.logger(warning)
        self.iqr = None
        self.smw = None
        self._iqr_paused = False
        self._iqr_display_mode = "IQ"
        if self.resource_manager is not None:
            try:
                self.resource_manager.close()
            except Exception:
                pass
            self.resource_manager = None
        return tuple(warnings)
