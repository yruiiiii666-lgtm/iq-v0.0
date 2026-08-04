from __future__ import annotations

import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from iq_reader import IQRecording, read_iq_contiguous, recording_from_paths
from playback_device import (
    DEFAULT_DIGITAL_PEAK_DBFS,
    DEFAULT_RF_SAFETY_LIMIT_DBM,
    HARD_MAX_DIGITAL_PEAK_DBFS,
    IQRDeviceRecording,
    PlaybackPackage,
    PlaybackSettings,
    VisaPlaybackSession,
    iqr_recording_key,
    iq_level_metrics,
    load_verified_playback_profile,
    prepare_playback_package,
    scale_iq_to_peak,
    validate_playback_settings,
)
from signal_reconstruction import ReconstructionComponent, ReconstructionResult
from scene_catalog import SCENE_TYPES, SceneLocation, list_linked_iq_details, list_scene_locations


ROUTE_ARB = "SMBV100A 内部 ARB（重构波形）"
ROUTE_IQR = "IQR100 → SMBV100A 数字 IQ（记录仪波形）"
ROUTE_SIM = "离线仿真（不连接设备）"
ROUTES = (ROUTE_ARB, ROUTE_IQR, ROUTE_SIM)
PLAYBACK_RAW = "原始采集数据"
PLAYBACK_RECONSTRUCTED = "重构数据"
RAW_SCOPE_SINGLE = "单条IQ"
RAW_SCOPE_SCENE = "当前场景全部IQ（依次）"
RAW_PREVIEW_MAX_SAMPLES = 2_000_000
IQR_DISPLAY_IQ = "I/Q波形"
IQR_DISPLAY_FFT = "FFT频谱"


class PlaybackModule(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        result_provider: Callable[[], ReconstructionResult | None],
        recording_provider: Callable[[], IQRecording | None],
        database_var: tk.StringVar,
        output_var: tk.StringVar,
        status_var: tk.StringVar,
        recording_context_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(parent, padding=8)
        self.result_provider = result_provider
        self.recording_provider = recording_provider
        self.recording_context_provider = recording_context_provider or (lambda: "")
        self.database_var = database_var
        self.output_var = output_var
        self.status_var = status_var
        self.current_result: ReconstructionResult | None = None
        self.raw_recording: IQRecording | None = None
        self.current_package: PlaybackPackage | None = None
        self.session: VisaPlaybackSession | None = None
        self.device_identities = ("未连接", "未连接")
        self.smw_identity_var = tk.StringVar(value="未连接")
        self.iqr_identity_var = tk.StringVar(value="未连接")
        self.device_summary_var = tk.StringVar(value="未连接")
        self.smw_status_detail_var = tk.StringVar(value="等待连接")
        self.iqr_status_detail_var = tk.StringVar(value="当前链路不使用 IQR100")
        self.device_details_window: tk.Toplevel | None = None
        self.popup_iqr_address_entry: ttk.Entry | None = None
        self.popup_iqr_path_entry: ttk.Entry | None = None
        self.connected = False
        self.device_configured = False
        self.rf_enabled = False
        self.playing = False
        self.hardware_playback_active = False
        self.sequence_transitioning = False
        self.start_transitioning = False
        self.playback_request_id = 0
        self.playback_cancel_event = threading.Event()
        self.play_after_id: str | None = None
        self.preview_clock_started_at: float | None = None
        self.preview_elapsed_origin_ms = 0.0
        self.display_elapsed_ms = 0.0
        self.preview_trend: list[tuple[float, float, float]] = []
        self.preview_trend_span_s = 10.0
        self.canvas_resize_after_id: str | None = None
        self.responsive_after_id: str | None = None
        self.compact_action_layout: bool | None = None
        self.compact_preview_layout: bool | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()

        self.playback_mode_var = tk.StringVar(value=PLAYBACK_RECONSTRUCTED)
        self.route_var = tk.StringVar(value=ROUTE_ARB)
        self.last_hardware_route = ROUTE_ARB
        self.simulation_var = tk.BooleanVar(value=True)
        self.smw_address_var = tk.StringVar(value="TCPIP0::192.168.48.102::inst0::INSTR")
        self.iqr_address_var = tk.StringVar(value="TCPIP0::192.168.48.103::inst0::INSTR")
        self.iqr_waveform_var = tk.StringVar(value="")
        self.peak_dbfs_var = tk.DoubleVar(value=DEFAULT_DIGITAL_PEAK_DBFS)
        self.peak_entry_var = tk.StringVar(value=f"{DEFAULT_DIGITAL_PEAK_DBFS:g}")
        self.rf_level_var = tk.StringVar(value="-30")
        self.rf_limit_var = tk.StringVar(value=f"{DEFAULT_RF_SAFETY_LIMIT_DBM:g}")
        self.run_mode_var = tk.StringVar(value="单次")
        self.loop_enabled_var = tk.BooleanVar(value=False)
        self.iqr_display_var = tk.StringVar(value=IQR_DISPLAY_IQ)
        self.external_reference_var = tk.BooleanVar(value=False)
        self.duration_var = tk.StringVar(value="1000")
        self.safety_confirm_var = tk.BooleanVar(value=False)
        self.source_text_var = tk.StringVar(value="尚未载入重构结果。")
        self.device_status_var = tk.StringVar(value="未连接")
        self.validation_var = tk.StringVar(value="请先载入当前重构信号。")
        self.window_ms_var = tk.StringVar(value="1")
        self.position_ms_var = tk.DoubleVar(value=0.0)
        self.play_status_var = tk.StringVar(value="波形预览未启动")
        self.raw_scene_var = tk.StringVar(value="工业区")
        self.raw_scope_var = tk.StringVar(value=RAW_SCOPE_SINGLE)
        self.raw_location_var = tk.StringVar()
        self.raw_iq_var = tk.StringVar()
        self.raw_selection_text_var = tk.StringVar(value="请选择场景、地点和IQ数据。")
        self.iqr_catalog_status_var = tk.StringVar(value="未连接IQR；连接后将自动核对e:/f:中的记录。")
        self.iqr_catalog: dict[str, IQRDeviceRecording] = {}
        self.iqr_catalog_loaded = False
        self.iqr_catalog_busy = False
        self.raw_location_map: dict[str, SceneLocation] = {}
        self.raw_recording_map: dict[str, IQRecording] = {}
        self.raw_sequence: list[tuple[IQRecording, str]] = []
        self.raw_sequence_index = -1
        self.output_source_var = tk.StringVar(value="未选择")
        self.output_center_var = tk.StringVar(value="--")
        self.output_sample_rate_var = tk.StringVar(value="--")
        self.output_bandwidth_var = tk.StringVar(value="--")
        self._syncing_source_tab = False

        self.verified_device_profile = load_verified_playback_profile()
        if self.verified_device_profile is not None:
            devices = self.verified_device_profile["devices"]
            validated = self.verified_device_profile.get("validated_settings", {})
            self.smw_address_var.set(devices["smw"]["visa_address"])
            self.iqr_address_var.set(devices["recorder"]["visa_address"])
            if "rf_power_dbm" in validated:
                self.rf_level_var.set(f"{float(validated['rf_power_dbm']):g}")
            if "external_reference" in validated:
                self.external_reference_var.set(bool(validated["external_reference"]))

        self._build_ui()
        self.device_status_var.trace_add("write", self._sync_device_status_display)
        self._sync_device_status_display()
        self._on_playback_mode_changed(announce=False)
        if self.verified_device_profile is not None:
            self.device_status_var.set(
                "已载入实物兼容性测试通过的设备地址；连接后仍保持RF关闭。"
            )
        self._on_simulation_toggled(announce=False)
        self.after(100, self._poll_messages)

    def _build_ui(self) -> None:
        self.top_shell = ttk.Frame(self, style="Panel.TFrame")
        self.top_shell.pack(fill=tk.X)
        self.top_scrollbar = ttk.Scrollbar(self.top_shell, orient=tk.VERTICAL)
        self.top_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.top_canvas = tk.Canvas(
            self.top_shell,
            bg="#ffffff",
            highlightthickness=0,
            height=360,
            yscrollcommand=self.top_scrollbar.set,
        )
        self.top_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.top_scrollbar.configure(command=self.top_canvas.yview)

        top = ttk.Frame(self.top_canvas, style="Panel.TFrame", padding=(4, 4, 4, 6))
        self.top_content = top
        self.top_window_id = self.top_canvas.create_window((0, 0), window=top, anchor="nw")
        top.columnconfigure(0, weight=5, minsize=360)
        top.columnconfigure(1, weight=3, minsize=240)
        top.columnconfigure(2, weight=4, minsize=320)
        self._build_source_area(top).grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._build_device_area(top).grid(row=0, column=1, sticky="nsew", padx=5)
        self._build_output_area(top).grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        top.bind("<Configure>", self._update_top_scrollregion, add="+")
        self.top_canvas.bind("<Configure>", self._on_top_canvas_configure, add="+")

        self.actions_frame = ttk.Frame(self, style="Panel.TFrame", padding=(8, 2, 8, 6))
        self.actions_frame.pack(fill=tk.X)
        self._build_action_bar(self.actions_frame)

        self.results_frame = ttk.Frame(self, style="Panel.TFrame", padding=(4, 0, 4, 4))
        self.results_frame.pack(fill=tk.BOTH, expand=True)
        self._build_results(self.results_frame)
        self._refresh_raw_scene_values()
        self.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self.after_idle(self._apply_responsive_layout)

    def _update_top_scrollregion(self, _event: tk.Event | None = None) -> None:
        bounds = self.top_canvas.bbox("all")
        if bounds is not None:
            self.top_canvas.configure(scrollregion=bounds)
        if self.top_content.winfo_reqheight() <= self.top_canvas.winfo_height():
            self.top_canvas.yview_moveto(0.0)

    def _on_top_canvas_configure(self, event: tk.Event) -> None:
        self.top_canvas.itemconfigure(self.top_window_id, width=max(1, event.width))
        self._update_top_scrollregion()

    def _schedule_responsive_layout(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self:
            return
        if self.responsive_after_id is not None:
            self.after_cancel(self.responsive_after_id)
        self.responsive_after_id = self.after(80, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self.responsive_after_id = None
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self._set_action_layout(compact=width < 1500)
        self.update_idletasks()

        action_height = max(1, self.actions_frame.winfo_reqheight())
        reserved_results = max(255, int(height * 0.42))
        available_for_top = max(120, height - action_height - reserved_results - 12)
        content_height = max(1, self.top_content.winfo_reqheight())
        target_height = min(content_height, available_for_top)
        if abs(int(float(self.top_canvas.cget("height"))) - target_height) > 2:
            self.top_canvas.configure(height=target_height)
        self._update_top_scrollregion()
        self._schedule_canvas_resize()

    def _set_action_layout(self, compact: bool) -> None:
        if self.compact_action_layout == compact:
            return
        self.compact_action_layout = compact
        self.validation_label.grid_forget()
        if compact:
            self.validation_label.configure(wraplength=max(500, self.winfo_width() - 32))
            self.validation_label.grid(
                row=1, column=0, columnspan=2, sticky="ew", padx=(0, 4), pady=(4, 0)
            )
        else:
            self.validation_label.configure(wraplength=560)
            self.validation_label.grid(row=0, column=1, sticky="ew", padx=(10, 0))

    def _build_source_area(self, parent: ttk.Frame) -> ttk.LabelFrame:
        source = ttk.LabelFrame(parent, text="回放信号来源", style="Card.TLabelframe", padding=8)
        self.source_notebook = ttk.Notebook(source)
        self.source_notebook.pack(fill=tk.BOTH, expand=True)
        self.raw_source_tab = ttk.Frame(self.source_notebook, padding=8)
        self.reconstructed_source_tab = ttk.Frame(self.source_notebook, padding=8)
        self.source_notebook.add(self.raw_source_tab, text="原始采集数据")
        self.source_notebook.add(self.reconstructed_source_tab, text="重构数据")
        self.source_notebook.select(self.reconstructed_source_tab)
        self.source_notebook.bind("<<NotebookTabChanged>>", self._on_source_tab_changed)

        scene_values = tuple(item for item in SCENE_TYPES if item != "未分类")
        scope_row = ttk.Frame(self.raw_source_tab, style="Panel.TFrame")
        scope_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(scope_row, text="回放范围", width=15).pack(side=tk.LEFT)
        ttk.Radiobutton(
            scope_row, text="单条IQ", value=RAW_SCOPE_SINGLE, variable=self.raw_scope_var,
            command=self._on_raw_scope_changed,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            scope_row, text="场景全部IQ", value=RAW_SCOPE_SCENE, variable=self.raw_scope_var,
            command=self._on_raw_scope_changed,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.raw_scene_combo = self._row(self.raw_source_tab, "场景类型", self.raw_scene_var, scene_values)
        self.raw_location_combo = self._row(self.raw_source_tab, "采集地点", self.raw_location_var, ())
        self.raw_iq_combo = self._row(self.raw_source_tab, "频段 / IQ数据", self.raw_iq_var, ())
        self.raw_scene_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_raw_locations())
        self.raw_location_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_raw_recordings())
        self.raw_iq_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_raw_selection_text())
        self.raw_load_button = ttk.Button(
            self.raw_source_tab,
            text="载入所选原始IQ",
            style="Accent.TButton",
            command=self.load_current_recording,
        )
        self.raw_load_button.pack(fill=tk.X, pady=(6, 4))
        ttk.Label(
            self.raw_source_tab,
            textvariable=self.raw_selection_text_var,
            foreground="#475569",
            justify=tk.LEFT,
            wraplength=410,
        ).pack(fill=tk.X, anchor="w")

        device_match_header = ttk.Frame(self.raw_source_tab, style="Panel.TFrame")
        device_match_header.pack(fill=tk.X, pady=(8, 2))
        ttk.Label(
            device_match_header,
            text="IQR设备记录核对",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        self.iqr_catalog_button = ttk.Button(
            device_match_header,
            text="刷新设备记录",
            command=self.refresh_iqr_catalog,
            state=tk.DISABLED,
        )
        self.iqr_catalog_button.pack(side=tk.RIGHT)
        self.iqr_catalog_label = ttk.Label(
            self.raw_source_tab,
            textvariable=self.iqr_catalog_status_var,
            foreground="#64748b",
            justify=tk.LEFT,
            wraplength=410,
        )
        self.iqr_catalog_label.pack(fill=tk.X, anchor="w")

        ttk.Label(
            self.reconstructed_source_tab,
            text="载入重构工作台当前生成的IQ波形。重构结果通过SMBV100A内部ARB发送。",
            foreground="#64748b",
            wraplength=410,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor="w")
        self.reconstructed_load_button = ttk.Button(
            self.reconstructed_source_tab,
            text="载入当前重构结果",
            style="Accent.TButton",
            command=self.load_current_result,
        )
        self.reconstructed_load_button.pack(fill=tk.X, pady=(8, 5))
        ttk.Label(
            self.reconstructed_source_tab,
            textvariable=self.source_text_var,
            foreground="#475569",
            justify=tk.LEFT,
            wraplength=410,
        ).pack(fill=tk.X, anchor="w")
        self.source_button = self.reconstructed_load_button
        return source

    def _build_device_area(self, parent: ttk.Frame) -> ttk.LabelFrame:
        device = ttk.LabelFrame(parent, text="设备连接状态", style="Card.TLabelframe", padding=10)
        self.device_area = device

        ttk.Label(device, text="当前链路", foreground="#64748b").pack(anchor="w")
        self.device_route_label = ttk.Label(
            device,
            textvariable=self.route_var,
            font=("Segoe UI", 10, "bold"),
            wraplength=280,
            justify=tk.LEFT,
        )
        self.device_route_label.pack(fill=tk.X, anchor="w", pady=(1, 7))

        ttk.Separator(device, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 7))
        ttk.Label(device, text="当前状态", foreground="#64748b").pack(anchor="w")
        self.device_summary_label = ttk.Label(
            device,
            textvariable=self.device_summary_var,
            foreground="#0f172a",
            wraplength=280,
            justify=tk.LEFT,
        )
        self.device_summary_label.pack(fill=tk.X, anchor="w", pady=(1, 8))

        self.smw_status_card = self._build_device_status_card(
            device,
            "SMBV100A",
            self.smw_identity_var,
            self.smw_status_detail_var,
        )
        self.smw_status_card.pack(fill=tk.X, pady=(0, 7))
        self.iqr_status_card = self._build_device_status_card(
            device,
            "IQR100",
            self.iqr_identity_var,
            self.iqr_status_detail_var,
        )
        self.iqr_status_card.pack(fill=tk.X, pady=(0, 8))

        ttk.Checkbutton(
            device,
            text="离线仿真（不连接设备）",
            variable=self.simulation_var,
            command=self._on_simulation_toggled,
        ).pack(anchor="w", pady=(0, 7))
        buttons = ttk.Frame(device, style="Panel.TFrame")
        buttons.pack(fill=tk.X)
        self.device_config_button = ttk.Button(
            buttons, text="配置并连接", style="Accent.TButton", command=self.show_device_details
        )
        self.device_config_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.disconnect_button = ttk.Button(buttons, text="断开", command=self.disconnect_devices)
        self.disconnect_button.pack(side=tk.LEFT)
        self.route_var.trace_add("write", lambda *_args: self._update_route_state())
        device.bind("<Configure>", self._resize_device_status_labels, add="+")
        return device

    def _build_device_status_card(
        self,
        parent: ttk.Frame,
        title: str,
        identity_var: tk.StringVar,
        detail_var: tk.StringVar,
    ) -> ttk.LabelFrame:
        card = ttk.LabelFrame(
            parent,
            text=title,
            style="Card.TLabelframe",
            padding=(8, 5, 8, 7),
        )
        identity_label = ttk.Label(
            card,
            textvariable=identity_var,
            foreground="#64748b",
            font=("Segoe UI", 9),
            wraplength=250,
            justify=tk.LEFT,
        )
        identity_label.pack(fill=tk.X, anchor="w")
        ttk.Separator(card, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(5, 5))
        detail_label = ttk.Label(
            card,
            textvariable=detail_var,
            foreground="#0f172a",
            font=("Segoe UI", 9),
            wraplength=250,
            justify=tk.LEFT,
        )
        detail_label.pack(fill=tk.X, anchor="w")
        if not hasattr(self, "device_status_wrap_labels"):
            self.device_status_wrap_labels: list[ttk.Label] = []
        self.device_status_wrap_labels.extend((identity_label, detail_label))
        return card

    def _resize_device_status_labels(self, event: tk.Event) -> None:
        if event.widget is not self.device_area:
            return
        outer_wrap = max(180, event.width - 28)
        inner_wrap = max(160, event.width - 50)
        self.device_route_label.configure(wraplength=outer_wrap)
        self.device_summary_label.configure(wraplength=outer_wrap)
        for label in self.device_status_wrap_labels:
            label.configure(wraplength=inner_wrap)

    @staticmethod
    def _format_device_status_detail(text: str) -> str:
        return "\n".join(part.strip() for part in text.split("｜") if part.strip())

    def _sync_device_status_display(self, *_args: object) -> None:
        text = self.device_status_var.get().strip() or "未连接"
        summary = text

        smw_marker = "SMBV100A:"
        iqr_marker = "｜IQR100:"
        if text.startswith(smw_marker) and iqr_marker in text:
            smw_text, iqr_text = text[len(smw_marker):].split(iqr_marker, 1)
            self.smw_status_detail_var.set(self._format_device_status_detail(smw_text))
            self.iqr_status_detail_var.set(self._format_device_status_detail(iqr_text))
            summary = "两台设备已配置完成，RF保持关闭。"
        elif text.startswith(("IQR100实际数据流已启动", "IQR100单次回放已完成")):
            parts = [part.strip() for part in text.split("｜") if part.strip()]
            summary = parts[0]
            if len(parts) > 1:
                self.iqr_status_detail_var.set("\n".join(parts[1:]))
        elif text.startswith("已连接"):
            self.smw_status_detail_var.set("连接正常，等待发送配置。")
            self.iqr_status_detail_var.set(
                "连接正常，等待发送配置。"
                if self.route_var.get() == ROUTE_IQR
                else "当前链路不使用 IQR100"
            )
        elif text.startswith("正在连接"):
            self.smw_status_detail_var.set("正在建立 VISA 会话并读取设备标识…")
            self.iqr_status_detail_var.set(
                "正在建立 VISA 会话并读取记录目录…"
                if self.route_var.get() == ROUTE_IQR
                else "当前链路不使用 IQR100"
            )
        elif text.startswith("正在发送波形并配置设备"):
            self.smw_status_detail_var.set("正在配置频率、电平和数字 IQ 输入…")
            self.iqr_status_detail_var.set(
                "正在选择记录并配置 Player…"
                if self.route_var.get() == ROUTE_IQR
                else "当前链路不使用 IQR100"
            )
        elif "离线仿真" in text:
            self.smw_status_detail_var.set("仿真模式，不建立硬件连接。")
            self.iqr_status_detail_var.set("仿真模式，不建立硬件连接。")
        elif "未连接" in text or text.startswith("设备已断开"):
            self.smw_status_detail_var.set("等待连接")
            self.iqr_status_detail_var.set(
                "等待连接" if self.route_var.get() == ROUTE_IQR else "当前链路不使用 IQR100"
            )
        elif "失败" in text or "错误" in text:
            if "IQR" in text:
                self.iqr_status_detail_var.set(self._format_device_status_detail(text))
            elif "SMBV" in text:
                self.smw_status_detail_var.set(self._format_device_status_detail(text))

        self.device_summary_var.set(summary)
        if hasattr(self, "device_summary_label"):
            if "失败" in text or "错误" in text:
                color = "#b91c1c"
            elif text.startswith("正在") or "等待" in text:
                color = "#1d4ed8"
            elif self.connected or "完成" in summary or "已启动" in summary:
                color = "#15803d"
            else:
                color = "#475569"
            self.device_summary_label.configure(foreground=color)

    def _build_output_area(self, parent: ttk.Frame) -> ttk.LabelFrame:
        output = ttk.LabelFrame(parent, text="输出参数设置", style="Card.TLabelframe", padding=8)
        self._readonly_row(output, "当前信号", self.output_source_var)
        self._readonly_row(output, "中心频率", self.output_center_var)
        self._readonly_row(output, "采样率", self.output_sample_rate_var)
        self._readonly_row(output, "输出带宽", self.output_bandwidth_var)
        row = ttk.Frame(output, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="数字IQ峰值", width=13).pack(side=tk.LEFT)
        ttk.Scale(
            row, from_=-30.0, to=HARD_MAX_DIGITAL_PEAK_DBFS, variable=self.peak_dbfs_var,
            command=self._on_peak_scale,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Entry(row, textvariable=self.peak_entry_var, width=7).pack(side=tk.LEFT)
        ttk.Label(row, text="dBFS").pack(side=tk.LEFT, padx=(3, 0))
        self.peak_entry_var.trace_add("write", lambda *_args: self._sync_peak_entry())
        self._row(output, "RF输出(dBm)", self.rf_level_var)
        self._row(output, "RF安全上限", self.rf_limit_var)
        self._row(output, "回放时长(ms)", self.duration_var)
        self._row(
            output,
            "IQR屏幕显示",
            self.iqr_display_var,
            (IQR_DISPLAY_IQ, IQR_DISPLAY_FFT),
        )
        ttk.Checkbutton(
            output,
            text="循环回放",
            variable=self.loop_enabled_var,
            command=self._on_loop_toggled,
        ).pack(anchor="w", pady=(3, 0))
        ttk.Checkbutton(
            output,
            text="使用外部10 MHz参考（仅已连接REF OUT→REF IN BNC线时勾选）",
            variable=self.external_reference_var,
        ).pack(anchor="w", pady=(3, 0))
        ttk.Checkbutton(
            output,
            text="已确认射频链路和负载安全",
            variable=self.safety_confirm_var,
            command=self._update_rf_button,
        ).pack(anchor="w", pady=(2, 0))
        return output

    @staticmethod
    def _readonly_row(parent: ttk.Frame, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=13).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=variable, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self.action_buttons = ttk.Frame(parent, style="Panel.TFrame")
        self.action_buttons.grid(row=0, column=0, sticky="w")
        self.prepare_button = ttk.Button(
            self.action_buttons,
            text="生成设备波形包",
            style="Accent.TButton",
            command=self.prepare_package,
        )
        self.prepare_button.pack(side=tk.LEFT, padx=(0, 5))
        self.send_button = ttk.Button(
            self.action_buttons,
            text="发送并配置设备",
            style="Teal.TButton",
            command=self.send_and_configure,
            state=tk.DISABLED,
        )
        self.send_button.pack(side=tk.LEFT, padx=5)
        self.start_button = ttk.Button(
            self.action_buttons,
            text="开始回放",
            style="Success.TButton",
            command=self.start_playback,
        )
        self.start_button.pack(side=tk.LEFT, padx=5)
        self.pause_button = ttk.Button(
            self.action_buttons,
            text="暂停",
            style="Warning.TButton",
            command=self.pause_playback,
        )
        self.pause_button.pack(side=tk.LEFT, padx=5)
        self.stop_button = ttk.Button(
            self.action_buttons,
            text="停止",
            style="Danger.TButton",
            command=self.stop_playback,
        )
        self.stop_button.pack(side=tk.LEFT, padx=5)
        self.rf_button = ttk.Button(
            self.action_buttons, text="RF输出：关闭", command=self.toggle_rf, state=tk.DISABLED
        )
        self.rf_button.pack(side=tk.LEFT, padx=5)
        self.validation_label = ttk.Label(
            parent, textvariable=self.validation_var, foreground="#64748b", justify=tk.LEFT,
            wraplength=560,
        )
        self.validation_label.grid(row=0, column=1, sticky="ew", padx=(10, 0))

    def _on_source_tab_changed(self, _event: tk.Event | None = None) -> None:
        if self._syncing_source_tab:
            return
        selected = self.source_notebook.select()
        requested = PLAYBACK_RAW if selected == str(self.raw_source_tab) else PLAYBACK_RECONSTRUCTED
        if self.playback_mode_var.get() != requested:
            self.playback_mode_var.set(requested)
            self._on_playback_mode_changed()

    def _is_raw_sequence_mode(self) -> bool:
        return self._is_raw_mode() and self.raw_scope_var.get() == RAW_SCOPE_SCENE

    def _on_raw_scope_changed(self) -> None:
        sequence_mode = self.raw_scope_var.get() == RAW_SCOPE_SCENE
        state = tk.DISABLED if sequence_mode else "readonly"
        self.raw_location_combo.configure(state=state)
        self.raw_iq_combo.configure(state=state)
        self.raw_load_button.configure(
            text="载入当前场景全部IQ" if sequence_mode else "载入所选原始IQ"
        )
        self.raw_sequence.clear()
        self.raw_sequence_index = -1
        if sequence_mode:
            count = 0
            for location in self.raw_location_map.values():
                count += len(list_linked_iq_details(
                    Path(self.database_var.get()), location.city, location.point
                ))
            self.raw_selection_text_var.set(
                f"{self.raw_scene_var.get()}共关联{count}条IQ；将按采集地点、中心频率依次回放。"
            )
        else:
            self._update_raw_selection_text()

    def _on_loop_toggled(self) -> None:
        self.run_mode_var.set("连续" if self.loop_enabled_var.get() else "单次")
        if self._is_raw_sequence_mode():
            meaning = (
                "每条由IQR100单次回放，最后一条结束后由软件回到第一条"
                if self.loop_enabled_var.get()
                else "每条由IQR100单次回放，最后一条结束后停止"
            )
        else:
            meaning = (
                "当前信号由IQR100以CONTinuous模式连续循环"
                if self.loop_enabled_var.get()
                else "当前信号由IQR100以SINGle模式回放一次"
            )
        if self.current_result is not None and not self.simulation_var.get():
            self.device_configured = False
            self.validation_var.set("循环设置已改变，请重新发送并配置设备。")
            self._update_action_states()
            self._update_rf_button()
        self.status_var.set(f"循环回放已{'开启' if self.loop_enabled_var.get() else '关闭'}：{meaning}。")

    def _on_run_mode_selected(self, _event: tk.Event | None = None) -> None:
        self.loop_enabled_var.set(self.run_mode_var.get() == "连续")
        self._on_loop_toggled()

    def _build_raw_scene_sequence(self) -> list[tuple[IQRecording, str]]:
        sequence: list[tuple[IQRecording, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for location_label, location in self.raw_location_map.items():
            links = list_linked_iq_details(
                Path(self.database_var.get()), location.city, location.point
            )
            for link in links:
                key = (location.city, location.point, link.recording_stem)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    recording = recording_from_paths(
                        link.recording_stem,
                        Path(link.wsm_file),
                        (Path(link.ws1_file), Path(link.ws2_file)),
                    )
                except Exception:
                    continue
                context = f"{location.scene_type}｜{location_label}"
                sequence.append((recording, context))
        sequence.sort(key=lambda item: (
            item[1].casefold(), item[0].center_frequency_mhz, item[0].stem.casefold()
        ))
        return sequence

    def _refresh_raw_scene_values(self) -> None:
        try:
            locations = list_scene_locations(Path(self.database_var.get()))
        except Exception as exc:
            self.raw_selection_text_var.set(f"场景数据库读取失败：{exc}")
            return
        available = {item.scene_type for item in locations if item.scene_type and item.scene_type != "未分类"}
        scene_values = tuple(item for item in SCENE_TYPES if item in available)
        self.raw_scene_combo.configure(values=scene_values)
        if scene_values and self.raw_scene_var.get() not in scene_values:
            self.raw_scene_var.set(scene_values[0])
        self._refresh_raw_locations()

    def refresh_raw_catalog(self) -> None:
        self._refresh_raw_scene_values()

    def _refresh_raw_locations(self) -> None:
        try:
            locations = list_scene_locations(
                Path(self.database_var.get()), scene_type=self.raw_scene_var.get()
            )
        except Exception as exc:
            self.raw_selection_text_var.set(f"地点读取失败：{exc}")
            return
        self.raw_location_map = {
            f"{item.city} / {item.point}": item for item in locations
        }
        values = tuple(self.raw_location_map)
        self.raw_location_combo.configure(values=values)
        self.raw_location_var.set(values[0] if values else "")
        self._refresh_raw_recordings()
        self._on_raw_scope_changed()

    def _refresh_raw_recordings(self) -> None:
        self.raw_recording_map.clear()
        location = self.raw_location_map.get(self.raw_location_var.get())
        if location is None:
            self.raw_iq_combo.configure(values=())
            self.raw_iq_var.set("")
            self.raw_selection_text_var.set("当前场景没有可用采集地点。")
            return
        for link in list_linked_iq_details(Path(self.database_var.get()), location.city, location.point):
            try:
                recording = recording_from_paths(
                    link.recording_stem,
                    Path(link.wsm_file),
                    (Path(link.ws1_file), Path(link.ws2_file)),
                )
            except Exception:
                continue
            label = (
                f"{recording.center_frequency_mhz:g} MHz｜{recording.stem}｜"
                f"{recording.duration_s:.3f} s"
            )
            self.raw_recording_map[label] = recording
        values = tuple(
            sorted(
                self.raw_recording_map,
                key=lambda label: self.raw_recording_map[label].center_frequency_mhz,
            )
        )
        self.raw_iq_combo.configure(values=values)
        self.raw_iq_var.set(values[0] if values else "")
        self._update_raw_selection_text()

    def _update_raw_selection_text(self) -> None:
        recording = self.raw_recording_map.get(self.raw_iq_var.get())
        if recording is None:
            self.raw_selection_text_var.set("当前地点没有可读取的IQ数据。")
            self._update_iqr_match_feedback(None, update_path=False)
            return
        self.raw_selection_text_var.set(
            f"{recording.stem}\n中心频率 {recording.center_frequency_mhz:.6f} MHz｜"
            f"采样率 {recording.sample_rate_hz / 1e6:.3f} MS/s｜时长 {recording.duration_s:.3f} s"
        )
        self._update_iqr_match_feedback(recording, update_path=False)

    def _set_iqr_catalog_status(self, text: str, color: str = "#64748b") -> None:
        self.iqr_catalog_status_var.set(text)
        if hasattr(self, "iqr_catalog_label"):
            self.iqr_catalog_label.configure(foreground=color)

    def _iqr_recording_for(self, recording: IQRecording | None) -> IQRDeviceRecording | None:
        if recording is None:
            return None
        return self.iqr_catalog.get(iqr_recording_key(recording.stem))

    def _update_iqr_match_feedback(
        self,
        recording: IQRecording | None = None,
        update_path: bool = True,
    ) -> None:
        selected = recording or self.raw_recording
        if selected is None:
            self._set_iqr_catalog_status("请先选择一条原始IQ记录。")
            return
        candidate_path = f"e:/{selected.stem}"
        if self.simulation_var.get() or self.route_var.get() == ROUTE_SIM:
            if update_path:
                self.iqr_waveform_var.set(candidate_path)
            self._set_iqr_catalog_status("离线仿真：使用本地磁盘数据，不读取IQR设备目录。")
            return
        if self.iqr_catalog_busy:
            self._set_iqr_catalog_status("正在读取IQR的e:/f:记录目录，请稍候...", "#1d4ed8")
            return
        if not self.connected or self.session is None:
            if update_path:
                self.iqr_waveform_var.set(candidate_path)
            self._set_iqr_catalog_status(
                f"尚未连接IQR；连接后将核对 {candidate_path}.ws1 / f:/{selected.stem}.ws2。"
            )
            return
        if not self.iqr_catalog_loaded:
            self._set_iqr_catalog_status("尚未取得IQR目录，请点击“刷新设备记录”。", "#b45309")
            return
        device_recording = self._iqr_recording_for(selected)
        if device_recording is None:
            if update_path:
                self.iqr_waveform_var.set(candidate_path)
            self._set_iqr_catalog_status(
                f"设备缺失：IQR的e:/f:根目录中未找到“{selected.stem}”，当前只能本地预览。",
                "#b91c1c",
            )
            return
        if update_path:
            self.iqr_waveform_var.set(device_recording.waveform_path)
        if device_recording.is_complete:
            self._set_iqr_catalog_status(
                f"设备已有，可回放：{device_recording.waveform_path}（e:/.ws1 与 f:/.ws2 完整）",
                "#15803d",
            )
        else:
            self._set_iqr_catalog_status(
                f"设备记录不完整：{device_recording.stem} {device_recording.issue}，不能实物回放。",
                "#b91c1c",
            )

    def _apply_iqr_catalog(self, entries: tuple[IQRDeviceRecording, ...]) -> None:
        self.iqr_catalog = {iqr_recording_key(item.stem): item for item in entries}
        self.iqr_catalog_loaded = True
        self.iqr_catalog_busy = False
        if hasattr(self, "iqr_catalog_button"):
            self.iqr_catalog_button.configure(
                state=tk.NORMAL if self.connected and not self.simulation_var.get() else tk.DISABLED
            )
        self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        self._update_action_states()

    def refresh_iqr_catalog(self) -> None:
        if self.simulation_var.get() or self.route_var.get() != ROUTE_IQR:
            self._set_iqr_catalog_status("离线仿真不读取IQR设备目录。")
            return
        if not self.connected or self.session is None or self.session.iqr is None:
            self._set_iqr_catalog_status("请先连接IQR100，再刷新设备记录。", "#b45309")
            return
        if self.iqr_catalog_busy:
            return
        self.iqr_catalog_busy = True
        self.iqr_catalog_loaded = False
        self.iqr_catalog_button.configure(state=tk.DISABLED)
        self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        threading.Thread(
            target=self._iqr_catalog_worker,
            args=(self.session,),
            daemon=True,
        ).start()

    def _iqr_catalog_worker(self, session: VisaPlaybackSession) -> None:
        try:
            self.messages.put(("iqr_catalog", session.list_iqr_recordings()))
        except Exception as exc:
            self.messages.put(("iqr_catalog_error", f"IQR设备目录读取失败：{exc}"))

    def _selected_raw_recording(self) -> IQRecording | None:
        return self.raw_recording_map.get(self.raw_iq_var.get())

    def _scrollable(self, parent: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(parent, bg="#ffffff", highlightthickness=0, width=390)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        controls = ttk.Frame(canvas, style="Panel.TFrame", padding=14)
        window_id = canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        def wheel(event: tk.Event) -> str | None:
            widget = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            current = widget
            while current is not None:
                if current in (canvas, controls):
                    delta = int(event.delta / 120) if event.delta else 0
                    if delta:
                        canvas.yview_scroll(-delta * 3, "units")
                        return "break"
                current = getattr(current, "master", None)
            return None

        self.bind_all("<MouseWheel>", wheel, add="+")
        return controls

    @staticmethod
    def _row(parent: ttk.Frame, label: str, variable: tk.Variable, values: tuple[str, ...] | None = None) -> ttk.Widget:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=15).pack(side=tk.LEFT)
        if values is None:
            widget: ttk.Widget = ttk.Entry(row, textvariable=variable)
        else:
            widget = ttk.Combobox(row, textvariable=variable, values=values, state="readonly")
        widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return widget

    def _build_controls(self, parent: ttk.Frame) -> None:
        mode = ttk.LabelFrame(parent, text="1. 回放模式", style="Card.TLabelframe", padding=10)
        mode.pack(fill=tk.X, pady=(0, 10))
        self._mode_option(
            mode,
            PLAYBACK_RAW,
            "直接调用当前场景选中的实测 IQ 数据，通过 IQW/IQR 链路回放。",
        )
        self._mode_option(
            mode,
            PLAYBACK_RECONSTRUCTED,
            "回放信号重构模块生成的数字 IQ 波形。",
        )

        source = ttk.LabelFrame(parent, text="2. 回放源", style="Card.TLabelframe", padding=10)
        source.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(source, textvariable=self.source_text_var, wraplength=330, justify=tk.LEFT).pack(anchor="w")
        self.source_button = ttk.Button(
            source,
            text="载入当前重构结果",
            style="Accent.TButton",
            command=self.load_selected_source,
        )
        self.source_button.pack(fill=tk.X, pady=(8, 3))

        route = ttk.LabelFrame(parent, text="3. 设备连接", style="Card.TLabelframe", padding=10)
        route.pack(fill=tk.X, pady=(0, 10))
        route_summary = ttk.Frame(route, style="Panel.TFrame")
        route_summary.pack(fill=tk.X)
        ttk.Label(route_summary, text="当前链路", foreground="#64748b", width=9).pack(side=tk.LEFT, anchor="n")
        ttk.Label(
            route_summary,
            textvariable=self.route_var,
            foreground="#0f172a",
            wraplength=250,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
        ttk.Label(
            route,
            textvariable=self.device_status_var,
            foreground="#475569",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor="w", pady=(6, 8))
        ttk.Checkbutton(
            route,
            text="离线仿真（不连接设备）",
            variable=self.simulation_var,
            command=self._on_simulation_toggled,
        ).pack(anchor="w", pady=(0, 7))
        device_buttons = ttk.Frame(route, style="Panel.TFrame")
        device_buttons.pack(fill=tk.X)
        self.device_config_button = ttk.Button(
            device_buttons,
            text="配置地址",
            style="Accent.TButton",
            command=self.show_device_details,
        )
        self.device_config_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.disconnect_button = ttk.Button(device_buttons, text="断开", command=self.disconnect_devices)
        self.disconnect_button.pack(side=tk.LEFT)
        self.route_var.trace_add("write", lambda *_args: self._update_route_state())

        level = ttk.LabelFrame(parent, text="4. 幅度与电平联锁", style="Card.TLabelframe", padding=10)
        level.pack(fill=tk.X, pady=(0, 10))
        row = ttk.Frame(level, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="数字IQ峰值", width=15).pack(side=tk.LEFT)
        ttk.Scale(
            row, from_=-30.0, to=HARD_MAX_DIGITAL_PEAK_DBFS, variable=self.peak_dbfs_var,
            command=self._on_peak_scale,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Entry(row, textvariable=self.peak_entry_var, width=7).pack(side=tk.LEFT)
        ttk.Label(row, text="dBFS").pack(side=tk.LEFT, padx=(3, 0))
        self.peak_entry_var.trace_add("write", lambda *_args: self._sync_peak_entry())
        self._row(level, "RF输出电平(dBm)", self.rf_level_var)
        self._row(level, "RF安全上限(dBm)", self.rf_limit_var)
        ttk.Label(
            level,
            text="数字IQ硬限值为0 dBFS，默认-1 dBFS留出余量。射频安全上限应按功放、耦合器和负载能力填写。",
            foreground="#64748b", wraplength=330, justify=tk.LEFT,
        ).pack(anchor="w", pady=(5, 0))

        playback = ttk.LabelFrame(parent, text="5. 回放参数", style="Card.TLabelframe", padding=10)
        playback.pack(fill=tk.X, pady=(0, 10))
        run_mode_combo = self._row(playback, "运行方式", self.run_mode_var, ("连续", "单次"))
        run_mode_combo.bind("<<ComboboxSelected>>", self._on_run_mode_selected)
        self._row(playback, "目标时长(ms)", self.duration_var)
        ttk.Checkbutton(
            playback,
            text="使用外部10 MHz参考（仅已连接REF OUT→REF IN BNC线时勾选）",
            variable=self.external_reference_var,
        ).pack(anchor="w", pady=(5, 2))
        ttk.Checkbutton(
            playback,
            text="已确认射频链路、衰减器、功放和负载安全",
            variable=self.safety_confirm_var,
            command=self._update_rf_button,
        ).pack(anchor="w", pady=(6, 3))

        actions = ttk.LabelFrame(parent, text="6. 准备与回放", style="Card.TLabelframe", padding=10)
        actions.pack(fill=tk.X, pady=(0, 10))
        self.prepare_button = ttk.Button(
            actions,
            text="校验并生成设备波形包",
            style="Accent.TButton",
            command=self.prepare_package,
        )
        self.prepare_button.pack(fill=tk.X, pady=3)
        self.send_button = ttk.Button(
            actions,
            text="发送波形并配置设备",
            style="Teal.TButton",
            command=self.send_and_configure,
            state=tk.DISABLED,
        )
        self.send_button.pack(fill=tk.X, pady=3)
        buttons = ttk.Frame(actions, style="Panel.TFrame")
        buttons.pack(fill=tk.X, pady=3)
        ttk.Button(
            buttons, text="开始", style="Success.TButton", command=self.start_playback
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            buttons, text="暂停", style="Warning.TButton", command=self.pause_playback
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(
            buttons, text="停止", style="Danger.TButton", command=self.stop_playback
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.rf_button = ttk.Button(actions, text="RF输出：关闭", command=self.toggle_rf, state=tk.DISABLED)
        self.rf_button.pack(fill=tk.X, pady=(5, 3))
        ttk.Label(actions, textvariable=self.validation_var, foreground="#64748b", wraplength=330, justify=tk.LEFT).pack(anchor="w", pady=(6, 0))
        self._update_route_state()

    def _mode_option(self, parent: ttk.Frame, value: str, description: str) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=3)
        ttk.Radiobutton(
            row,
            text=value,
            value=value,
            variable=self.playback_mode_var,
            command=self._on_playback_mode_changed,
        ).pack(anchor="w")
        ttk.Label(
            row,
            text=description,
            foreground="#64748b",
            wraplength=315,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=(23, 0), pady=(1, 0))

    def _build_results(self, parent: ttk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        preview = ttk.Frame(self.notebook)
        parameters = ttk.Frame(self.notebook, padding=12)
        logs = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(preview, text="回放输入波形")
        self.notebook.add(parameters, text="参数与校验")
        self.notebook.add(logs, text="设备日志")

        self.figure = Figure(figsize=(8, 3.2), constrained_layout=True)
        grid = self.figure.add_gridspec(1, 2, width_ratios=(1.0, 1.0))
        self.waveform_axis = self.figure.add_subplot(grid[0, 0])
        self.spectrum_axis = self.figure.add_subplot(grid[0, 1])
        self.canvas = FigureCanvasTkAgg(self.figure, master=preview)
        toolbar = NavigationToolbar2Tk(self.canvas, preview, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill=tk.X)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.configure(width=640, height=140)
        self.canvas_widget.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 4))
        control = ttk.Frame(preview, padding=(12, 0, 12, 8))
        control.pack(fill=tk.X)
        self.play_button = ttk.Button(control, text="开始波形预览", command=self.toggle_preview, state=tk.DISABLED)
        self.play_button.pack(side=tk.LEFT)
        ttk.Button(control, text="停止预览", command=lambda: self.stop_preview(True)).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(control, text="窗口").pack(side=tk.LEFT)
        ttk.Combobox(
            control, textvariable=self.window_ms_var, values=("0.1", "0.5", "1", "2", "5"),
            state="readonly", width=6,
        ).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(control, text="ms").pack(side=tk.LEFT, padx=(0, 8))
        self.seek = ttk.Scale(control, variable=self.position_ms_var, from_=0, to=1, command=self._on_seek)
        self.seek.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(control, textvariable=self.play_status_var, width=34).pack(side=tk.LEFT, padx=(8, 0))
        self.bind("<Configure>", self._schedule_canvas_resize, add="+")
        self.after_idle(self._resize_canvas_for_dpi)
        self.parameter_text = tk.Text(parameters, wrap=tk.WORD, relief=tk.FLAT, background="#ffffff")
        self.parameter_text.pack(fill=tk.BOTH, expand=True)
        self.parameter_text.configure(state=tk.DISABLED)
        self.log_text = tk.Text(logs, wrap=tk.WORD, relief=tk.FLAT, background="#0f172a", foreground="#e2e8f0")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self._draw_empty()

    def _schedule_canvas_resize(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self:
            return
        if self.canvas_resize_after_id is not None:
            self.after_cancel(self.canvas_resize_after_id)
        self.canvas_resize_after_id = self.after(80, self._resize_canvas_for_dpi)

    def _resize_canvas_for_dpi(self) -> None:
        self.canvas_resize_after_id = None
        self.update_idletasks()
        width = max(360, self.canvas_widget.winfo_width())
        height = max(110, self.canvas_widget.winfo_height())
        # Keep the Tk allocation fixed and adapt Matplotlib's physical inches to
        # the allocated pixel area. forward=False prevents the figure from
        # requesting a larger pane again on high-DPI monitors.
        self.figure.set_size_inches(width / self.figure.dpi, height / self.figure.dpi, forward=False)
        compact = height < 260
        if self.compact_preview_layout != compact:
            self.compact_preview_layout = compact
            if self.current_result is not None:
                self.draw_preview()
                return
        self.canvas.draw_idle()

    def _draw_empty(self) -> None:
        for axis, title in zip(
            (self.waveform_axis, self.spectrum_axis),
            ("设备回放输入时域波形", "设备回放输入频谱"),
        ):
            axis.clear()
            axis.set_title(title)
            axis.text(0.5, 0.5, "请先选择回放模式并载入数据", ha="center", va="center", transform=axis.transAxes, color="#64748b")
            axis.set_xticks([])
            axis.set_yticks([])
        self.canvas.draw_idle()

    def _is_raw_mode(self) -> bool:
        return self.playback_mode_var.get() == PLAYBACK_RAW

    def _source_label(self) -> str:
        return "原始采集信号" if self._is_raw_mode() else "重构信号"

    def _update_output_source_fields(self, result: ReconstructionResult | None) -> None:
        if result is None:
            self.output_source_var.set("未选择")
            self.output_center_var.set("--")
            self.output_sample_rate_var.set("--")
            self.output_bandwidth_var.set("--")
            return
        self.output_source_var.set(result.name)
        self.output_center_var.set(f"{result.center_frequency_mhz:.6f} MHz")
        self.output_sample_rate_var.set(f"{result.sample_rate_hz / 1e6:.3f} MS/s")
        self.output_bandwidth_var.set(f"{result.sample_rate_hz / 1e6:.3f} MHz")

    def _on_playback_mode_changed(self, announce: bool = True) -> None:
        self._cancel_playback_operation()
        if hasattr(self, "source_notebook"):
            target_tab = self.raw_source_tab if self._is_raw_mode() else self.reconstructed_source_tab
            if self.source_notebook.select() != str(target_tab):
                self._syncing_source_tab = True
                self.source_notebook.select(target_tab)
                self._syncing_source_tab = False
        self.stop_preview(False)
        self.preview_elapsed_origin_ms = 0.0
        self.display_elapsed_ms = 0.0
        self._reset_preview_trend()
        if self.rf_enabled:
            try:
                if self.session is not None and not self.simulation_var.get():
                    self.session.set_rf(False)
            except Exception as exc:
                self._log(f"切换回放模式时关闭 RF 失败：{exc}")
            self.rf_enabled = False
            self.rf_button.configure(text="RF输出：关闭")
        self.current_result = None
        self.raw_recording = None
        self.raw_sequence.clear()
        self.raw_sequence_index = -1
        self.hardware_playback_active = False
        self.sequence_transitioning = False
        self._update_output_source_fields(None)
        self.current_package = None
        self.device_configured = False
        self.position_ms_var.set(0.0)
        self.play_button.configure(state=tk.DISABLED)
        self.send_button.configure(state=tk.DISABLED)
        self._update_rf_button()
        if self._is_raw_mode():
            if self.route_var.get() not in (ROUTE_IQR, ROUTE_SIM):
                self.route_var.set(ROUTE_IQR)
            self.source_button = self.raw_load_button
            self.source_button.configure(text="载入当前场景选中的原始 IQ 数据")
            self.source_text_var.set("尚未载入原始采集数据。")
            self.validation_var.set("原始模式通过 IQW/IQR 直接调用已有采集波形，无需生成重构波形包。")
            self.prepare_button.configure(state=tk.DISABLED)
            if not self.raw_recording_map:
                self._refresh_raw_scene_values()
        else:
            if self.route_var.get() == ROUTE_IQR:
                self.route_var.set(ROUTE_ARB)
            self.source_button = self.reconstructed_load_button
            self.source_button.configure(text="载入当前重构结果")
            self.source_text_var.set("尚未载入重构结果。")
            self.validation_var.set("请先载入当前重构信号。")
            self.prepare_button.configure(state=tk.NORMAL)
        self.play_status_var.set("波形预览未启动")
        self._draw_empty()
        self._update_parameter_text()
        self._update_route_state()
        if self.simulation_var.get():
            self._on_simulation_toggled(announce=False)
        if announce:
            self.status_var.set(f"已切换为{self.playback_mode_var.get()}回放。")

    def load_selected_source(self) -> None:
        if self._is_raw_mode():
            self.load_current_recording()
        else:
            self.load_current_result()

    def load_current_recording(self) -> None:
        try:
            if self._is_raw_sequence_mode():
                self.raw_sequence = self._build_raw_scene_sequence()
                if not self.raw_sequence:
                    raise ValueError(f"{self.raw_scene_var.get()}没有可读取的IQ数据")
                self.raw_sequence_index = 0
                recording, scene_context = self.raw_sequence[0]
            else:
                self.raw_sequence.clear()
                self.raw_sequence_index = -1
                recording = self._selected_raw_recording()
                location = self.raw_location_map.get(self.raw_location_var.get())
                if recording is not None and location is not None:
                    scene_context = f"{location.scene_type}｜{location.city} / {location.point}"
                else:
                    recording = self.recording_provider()
                    scene_context = self.recording_context_provider().strip()
        except Exception as exc:
            messagebox.showerror("场景 IQ 读取失败", str(exc))
            return
        if recording is None:
            messagebox.showwarning(
                "没有可用原始IQ",
                "请在当前页面依次选择场景类型、采集地点和频段IQ数据。",
            )
            return
        self._load_raw_recording_source(recording, scene_context)

    def _load_raw_recording_source(self, recording: IQRecording, scene_context: str) -> None:
        try:
            sample_count = min(recording.total_samples, RAW_PREVIEW_MAX_SAMPLES)
            if sample_count < 32:
                raise ValueError("当前 IQ 记录过短，无法用于回放。")
            iq = read_iq_contiguous(recording, 0, sample_count).astype(np.complex64)
            if iq.size < 32 or not np.all(np.isfinite(iq)):
                raise ValueError("当前 IQ 记录为空、过短或包含无效值。")
        except Exception as exc:
            messagebox.showerror("原始数据载入失败", str(exc))
            return
        component = ReconstructionComponent(
            name=recording.stem,
            source_type="原始采集 IQ",
            modulation="原始采集波形",
            frequency_mhz=recording.center_frequency_mhz,
            offset_mhz=0.0,
            relative_level_db=0.0,
            bandwidth_mhz=recording.sample_rate_hz / 1e6,
            source_reference=str(recording.metadata_path or recording.volumes[0].path),
        )
        self.stop_preview(True)
        self.raw_recording = recording
        self.current_result = ReconstructionResult(
            name=recording.stem,
            mode="原始采集数据直接回放",
            iq=iq,
            sample_rate_hz=recording.sample_rate_hz,
            center_frequency_mhz=recording.center_frequency_mhz,
            components=(component,),
            metadata={
                "playback_mode": PLAYBACK_RAW,
                "source_scene": scene_context,
                "source_recording": recording.stem,
                "source_reference_level_dbm": recording.reference_level_dbm,
                "original_duration_s": recording.duration_s,
                "requested_playback_duration_s": recording.duration_s,
                "preview_sample_count": int(iq.size),
                "direct_iqr_playback": True,
            },
            figure=Figure(figsize=(1, 1)),
        )
        self.current_package = None
        self.device_configured = False
        self._update_output_source_fields(self.current_result)
        self.duration_var.set(f"{recording.duration_s * 1e3:g}")
        self._update_iqr_match_feedback(recording)
        self.source_text_var.set(
            f"场景：{scene_context or '当前场景'}\n"
            f"数据：{recording.stem}\n原始采集数据（直接回放）\n"
            f"中心频率 {recording.center_frequency_mhz:.6f} MHz｜"
            f"采样率 {recording.sample_rate_hz / 1e6:.6f} MS/s｜记录时长 {recording.duration_s:.3f} s"
        )
        if self._is_raw_sequence_mode() and self.raw_sequence:
            position = f"{self.raw_sequence_index + 1}/{len(self.raw_sequence)}"
            self.source_text_var.set(f"顺序队列 {position}\n{self.source_text_var.get()}")
            self.raw_selection_text_var.set(
                f"正在使用{self.raw_scene_var.get()}场景队列：{position}｜{recording.stem}"
            )
        self.seek.configure(to=max(recording.duration_s * 1e3, 0.001))
        self.position_ms_var.set(0.0)
        self.play_button.configure(state=tk.NORMAL)
        if self.simulation_var.get():
            self.validation_var.set("原始采集数据已载入；可直接准备仿真回放。")
        elif self._iqr_recording_for(recording) is not None and self._iqr_recording_for(recording).is_complete:
            self.validation_var.set("原始采集数据已载入，IQR设备中存在完整同名记录，可配置实物回放。")
        else:
            self.validation_var.set("原始采集数据已载入；连接IQR并核对设备记录后才能实物回放。")
        self._update_action_states()
        self.draw_preview()
        self._update_parameter_text()
        if self._is_raw_sequence_mode() and self.raw_sequence:
            self.status_var.set(
                f"已载入{self.raw_scene_var.get()}场景顺序队列："
                f"{self.raw_sequence_index + 1}/{len(self.raw_sequence)}｜{recording.stem}"
            )
        else:
            self.status_var.set(f"已载入当前场景原始 IQ：{scene_context or '当前场景'} / {recording.stem}")

    def load_current_result(self, result: ReconstructionResult | None = None) -> None:
        selected = result or self.result_provider()
        if selected is None:
            messagebox.showwarning("没有重构结果", "请先在“信号重构”模块生成重构信号。")
            return
        if self._is_raw_mode():
            self.playback_mode_var.set(PLAYBACK_RECONSTRUCTED)
            self._on_playback_mode_changed(announce=False)
        self.stop_preview(True)
        self.raw_recording = None
        self.current_result = selected
        self.current_package = None
        self.device_configured = False
        self._update_output_source_fields(selected)
        requested_ms = float(selected.metadata.get("requested_playback_duration_s", selected.duration_s)) * 1e3
        self.duration_var.set(f"{requested_ms:g}")
        source_reference = selected.metadata.get("source_reference_level_dbm")
        if source_reference is not None:
            try:
                reference_dbm = float(source_reference)
                safety_limit = float(self.rf_limit_var.get())
                if math.isfinite(reference_dbm) and reference_dbm <= safety_limit:
                    self.rf_level_var.set(f"{reference_dbm:g}")
            except (TypeError, ValueError):
                pass
        self.source_text_var.set(
            f"{selected.name}\n{selected.mode}\n中心频率 {selected.center_frequency_mhz:.6f} MHz｜"
            f"采样率 {selected.sample_rate_hz / 1e6:.6f} MS/s｜缓冲区 {selected.duration_s * 1e3:.3f} ms"
        )
        self.seek.configure(to=max(requested_ms, 0.001))
        self.position_ms_var.set(0.0)
        self.play_button.configure(state=tk.NORMAL)
        self.validation_var.set("已载入重构结果；请校验并生成设备波形包。")
        self._update_action_states()
        self.draw_preview()
        self._update_parameter_text()
        self.status_var.set(f"已将重构信号载入设备回放：{selected.name}")

    def accept_reconstruction(self, result: ReconstructionResult) -> None:
        self.load_current_result(result)

    def _settings(self) -> PlaybackSettings:
        try:
            peak = float(self.peak_entry_var.get())
            rf_level = float(self.rf_level_var.get())
            rf_limit = float(self.rf_limit_var.get())
            duration_s = float(self.duration_var.get()) * 1e-3
        except ValueError as exc:
            raise ValueError("幅度、电平和时长必须填写数值。") from exc
        return PlaybackSettings(
            route=self.route_var.get(),
            digital_peak_dbfs=peak,
            rf_level_dbm=rf_level,
            rf_safety_limit_dbm=rf_limit,
            run_mode="连续" if self.loop_enabled_var.get() else "单次",
            requested_duration_s=duration_s,
            external_10mhz_reference=self.external_reference_var.get(),
            iqr_display_mode=(
                "FFT" if self.iqr_display_var.get() == IQR_DISPLAY_FFT else "IQ"
            ),
        )

    def _on_peak_scale(self, value: str) -> None:
        self.peak_entry_var.set(f"{float(value):.1f}")
        if self.current_result is not None:
            self.draw_preview()

    def _sync_peak_entry(self) -> None:
        try:
            value = min(HARD_MAX_DIGITAL_PEAK_DBFS, max(-30.0, float(self.peak_entry_var.get())))
        except ValueError:
            return
        if abs(self.peak_dbfs_var.get() - value) > 0.05:
            self.peak_dbfs_var.set(value)

    def _update_route_state(self) -> None:
        if self.route_var.get() != ROUTE_SIM:
            self.last_hardware_route = self.route_var.get()
        use_iqr = self.route_var.get() == ROUTE_IQR
        popup_state = tk.NORMAL if use_iqr else tk.DISABLED
        if self.popup_iqr_address_entry is not None and self.popup_iqr_address_entry.winfo_exists():
            self.popup_iqr_address_entry.configure(state=popup_state)
        if self.popup_iqr_path_entry is not None and self.popup_iqr_path_entry.winfo_exists():
            self.popup_iqr_path_entry.configure(state=popup_state)
        if use_iqr:
            if self._is_raw_mode():
                self.validation_var.set("原始模式：连接IQR后将自动核对e:/f:中的同名采集记录。")
            elif self.current_result is None:
                self.validation_var.set(
                    "记录仪链路需要选择设备中已有且可播放的采集记录；具体格式取决于IQW/IQR型号和选件。"
                )
        if hasattr(self, "iqr_catalog_button"):
            self.iqr_catalog_button.configure(
                state=(
                    tk.NORMAL
                    if use_iqr and self.connected and not self.simulation_var.get() and not self.iqr_catalog_busy
                    else tk.DISABLED
                )
            )
        if self._is_raw_mode():
            self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        self._sync_device_status_display()
        self._update_action_states()

    def _on_simulation_toggled(self, announce: bool = True) -> None:
        self._cancel_playback_operation()
        simulation = self.simulation_var.get()
        if simulation:
            if self.route_var.get() != ROUTE_SIM:
                self.last_hardware_route = self.route_var.get()
            if self.session is not None:
                self.session.close()
                self.session = None
            self.route_var.set(ROUTE_SIM)
            self.connected = True
            self.iqr_catalog.clear()
            self.iqr_catalog_loaded = False
            self.iqr_catalog_busy = False
            self.device_configured = False
            self.rf_enabled = False
            self.device_identities = ("离线仿真", "不使用")
            self.smw_identity_var.set(self.device_identities[0])
            self.iqr_identity_var.set(self.device_identities[1])
            self.device_status_var.set("离线仿真已启用，无需连接设备。")
            self.device_config_button.configure(state=tk.DISABLED)
            self.disconnect_button.configure(state=tk.DISABLED)
            self.iqr_catalog_button.configure(state=tk.DISABLED)
            self.send_button.configure(text="准备仿真回放")
            self.rf_button.configure(text="RF输出：仿真模式", state=tk.DISABLED)
        else:
            if self.route_var.get() == ROUTE_SIM:
                hardware_route = ROUTE_IQR if self._is_raw_mode() else self.last_hardware_route
                if hardware_route not in (ROUTE_ARB, ROUTE_IQR):
                    hardware_route = ROUTE_ARB
                self.route_var.set(hardware_route)
            self.connected = False
            self.iqr_catalog.clear()
            self.iqr_catalog_loaded = False
            self.iqr_catalog_busy = False
            self.device_configured = False
            self.rf_enabled = False
            self.device_identities = ("未连接", "未连接")
            self.smw_identity_var.set("未连接")
            self.iqr_identity_var.set("未连接")
            self.device_status_var.set("未连接，请配置实物设备。")
            self.device_config_button.configure(state=tk.NORMAL)
            self.disconnect_button.configure(state=tk.NORMAL)
            self.iqr_catalog_button.configure(state=tk.DISABLED)
            self.send_button.configure(text="发送波形并配置设备")
            self.rf_button.configure(text="RF输出：关闭")
            if self.current_result is None:
                self.validation_var.set("实物设备模式：请先载入回放源并配置设备。")
            elif self._is_raw_mode():
                self.validation_var.set("请确认记录仪波形路径，然后连接并配置设备。")
            elif self.current_package is None:
                self.validation_var.set("请校验并生成设备波形包，然后连接设备。")
        self._update_action_states()
        if self._is_raw_mode():
            self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        self._update_rf_button()
        if announce:
            state_text = "离线仿真" if simulation else "实物设备"
            self.status_var.set(f"已切换为{state_text}回放。")

    def _update_action_states(self) -> None:
        if not hasattr(self, "send_button"):
            return
        source_ready = self.current_result is not None
        package_required = self.route_var.get() == ROUTE_ARB
        hardware_ready = self.simulation_var.get() or self.route_var.get() == ROUTE_SIM or self.connected
        device_record_ready = True
        if self._is_raw_mode() and self.route_var.get() == ROUTE_IQR and not self.simulation_var.get():
            device_record = self._iqr_recording_for(self.raw_recording)
            device_record_ready = bool(
                self.iqr_catalog_loaded and device_record is not None and device_record.is_complete
            )
        send_ready = (
            source_ready
            and hardware_ready
            and device_record_ready
            and (not package_required or self.current_package is not None)
        )
        self.send_button.configure(state=tk.NORMAL if send_ready else tk.DISABLED)
        if hasattr(self, "prepare_button"):
            prepare_ready = source_ready and not self._is_raw_mode() and not self.simulation_var.get()
            self.prepare_button.configure(state=tk.NORMAL if prepare_ready else tk.DISABLED)
        if self.simulation_var.get():
            self.validation_var.set(
                "离线仿真无需连接设备或生成设备波形包。"
                + ("点击“准备仿真回放”后即可开始。" if source_ready else "请先载入回放源。")
            )

    def prepare_package(self) -> None:
        if self._is_raw_mode():
            messagebox.showinfo("无需生成波形包", "原始采集模式由 IQW/IQR 直接调用已有波形，无需生成重构波形包。")
            return
        result = self.current_result
        if result is None:
            messagebox.showwarning("没有回放源", "请先载入重构结果。")
            return
        try:
            settings = self._settings()
            errors = validate_playback_settings(result, settings)
            if errors:
                raise ValueError("\n".join(errors))
        except ValueError as exc:
            messagebox.showerror("回放参数无效", str(exc))
            return
        self.validation_var.set("正在量化并生成R&S设备波形包...")
        threading.Thread(target=self._prepare_worker, args=(result, settings), daemon=True).start()

    def _prepare_worker(self, result: ReconstructionResult, settings: PlaybackSettings) -> None:
        try:
            package = prepare_playback_package(result, settings, Path(self.output_var.get()))
            self.messages.put(("package", package))
        except Exception as exc:
            self.messages.put(("error", f"生成设备波形包失败：{exc}"))

    def connect_devices(self) -> None:
        self._cancel_playback_operation()
        if self.simulation_var.get() or self.route_var.get() == ROUTE_SIM:
            self.simulation_var.set(True)
            self._on_simulation_toggled(announce=False)
            return
        if self.session is not None:
            self.session.close()
            self.session = None
        self.connected = False
        self.iqr_catalog.clear()
        self.iqr_catalog_loaded = False
        self.iqr_catalog_busy = self.route_var.get() == ROUTE_IQR
        self.device_configured = False
        self.rf_enabled = False
        self.rf_button.configure(text="RF输出：关闭")
        self._update_rf_button()
        self.device_identities = ("正在识别...", "正在识别..." if self.route_var.get() == ROUTE_IQR else "未连接")
        self.smw_identity_var.set(self.device_identities[0])
        self.iqr_identity_var.set(self.device_identities[1])
        self.device_status_var.set("正在连接设备并读取IQR记录目录...")
        self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        session: VisaPlaybackSession | None = None
        try:
            session = VisaPlaybackSession(logger=lambda line: self.messages.put(("log", line)))
            iqr_address = self.iqr_address_var.get() if self.route_var.get() == ROUTE_IQR else ""
            identities = session.connect(self.smw_address_var.get(), iqr_address)
            catalog: tuple[IQRDeviceRecording, ...] = ()
            catalog_error = ""
            if iqr_address:
                try:
                    catalog = session.list_iqr_recordings()
                except Exception as exc:
                    catalog_error = f"IQR设备目录读取失败：{exc}"
            self.messages.put(("connected", (session, identities, catalog, catalog_error)))
        except Exception as exc:
            if session is not None:
                session.close()
            self.messages.put(("connect_error", f"设备连接失败：{exc}"))

    def disconnect_devices(self) -> None:
        self.stop_playback(notify=False)
        release_warnings: tuple[str, ...] = ()
        if self.session is not None:
            release_warnings = self.session.close(return_to_local=True)
            self.session = None
        self.connected = False
        self.iqr_catalog.clear()
        self.iqr_catalog_loaded = False
        self.iqr_catalog_busy = False
        self.device_configured = False
        self.rf_enabled = False
        self.device_identities = ("未连接", "未连接")
        self.smw_identity_var.set("未连接")
        self.iqr_identity_var.set("未连接")
        if release_warnings:
            self.device_status_var.set("设备已断开，但部分设备未能确认返回本地控制。")
        else:
            self.device_status_var.set("设备已断开，并已返回本地面板控制。")
        self.iqr_catalog_button.configure(state=tk.DISABLED)
        self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
        self.rf_button.configure(text="RF输出：关闭")
        self._update_rf_button()
        if release_warnings:
            for warning in release_warnings:
                self._log(warning)
            self._log("设备连接已断开；请查看日志并在仪器面板确认REMOTE标识已消失。")
        else:
            self._log("设备连接已断开，IQR100和SMBV100A已退出远程控制模式。")

    def show_device_details(self) -> None:
        if self.simulation_var.get():
            return
        if self.device_details_window is not None and self.device_details_window.winfo_exists():
            self.device_details_window.lift()
            self.device_details_window.focus_force()
            return
        dialog = tk.Toplevel(self)
        self.device_details_window = dialog
        dialog.title("设备连接配置")
        dialog.geometry("800x450")
        dialog.minsize(620, 400)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", self._close_device_details)

        header = ttk.Frame(dialog, padding=(16, 10, 16, 7))
        header.pack(fill=tk.X)
        header_row = ttk.Frame(header)
        header_row.pack(fill=tk.X)
        ttk.Label(header_row, text="设备连接配置", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT)
        ttk.Button(
            header_row,
            text="连接并识别",
            style="Accent.TButton",
            command=self._connect_from_dialog,
        ).pack(side=tk.RIGHT)
        ttk.Label(
            header,
            textvariable=self.playback_mode_var,
            foreground="#64748b",
        ).pack(anchor="w", pady=(3, 0))

        form = ttk.LabelFrame(dialog, text="连接参数", style="Card.TLabelframe", padding=10)
        form.pack(fill=tk.X, padx=16, pady=(0, 8))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="设备链路", width=16).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        allowed_routes = (ROUTE_IQR,) if self._is_raw_mode() else (ROUTE_ARB, ROUTE_IQR)
        popup_route_combo = ttk.Combobox(
            form,
            textvariable=self.route_var,
            values=allowed_routes,
            state="readonly",
        )
        popup_route_combo.grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(form, text="SMBV100A VISA 地址", width=16).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Entry(form, textvariable=self.smw_address_var).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(form, text="IQW/IQR VISA 地址", width=16).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=6)
        self.popup_iqr_address_entry = ttk.Entry(form, textvariable=self.iqr_address_var)
        self.popup_iqr_address_entry.grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(form, text="记录仪波形路径", width=16).grid(row=3, column=0, sticky="w", padx=(0, 10), pady=6)
        self.popup_iqr_path_entry = ttk.Entry(form, textvariable=self.iqr_waveform_var)
        self.popup_iqr_path_entry.grid(row=3, column=1, sticky="ew", pady=6)

        status = ttk.LabelFrame(dialog, text="连接与识别状态", style="Card.TLabelframe", padding=10)
        status.pack(fill=tk.X, padx=16, pady=(0, 8))
        status.columnconfigure(1, weight=1)
        ttk.Label(status, text="连接状态", foreground="#64748b", width=12).grid(row=0, column=0, sticky="nw", pady=4)
        ttk.Label(
            status,
            textvariable=self.device_status_var,
            foreground="#0f172a",
            wraplength=580,
            justify=tk.LEFT,
        ).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(status, text="SMBV100A", foreground="#64748b", width=12).grid(row=1, column=0, sticky="nw", pady=4)
        ttk.Entry(status, textvariable=self.smw_identity_var, state="readonly").grid(
            row=1, column=1, sticky="ew", pady=4
        )
        ttk.Label(status, text="IQW/IQR", foreground="#64748b", width=12).grid(row=2, column=0, sticky="nw", pady=4)
        ttk.Entry(status, textvariable=self.iqr_identity_var, state="readonly").grid(
            row=2, column=1, sticky="ew", pady=4
        )

        buttons = ttk.Frame(dialog, padding=(16, 0, 16, 10))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="断开连接", command=self.disconnect_devices).pack(side=tk.LEFT)
        ttk.Button(buttons, text="复制连接信息", command=self._copy_device_details).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="关闭", command=self._close_device_details).pack(side=tk.RIGHT)
        self._update_route_state()
        dialog.bind("<Return>", lambda _event: self._connect_from_dialog())

    def _connect_from_dialog(self) -> None:
        self._connect_hardware(parent=self.device_details_window)

    def _connect_hardware(self, parent: tk.Misc | None = None) -> None:
        if self.simulation_var.get():
            return
        if not self.smw_address_var.get().strip():
            messagebox.showwarning("缺少设备地址", "请填写 SMBV100A VISA 地址。", parent=parent)
            return
        if self.route_var.get() == ROUTE_IQR and not self.iqr_address_var.get().strip():
            messagebox.showwarning("缺少设备地址", "记录仪链路需要填写 IQW/IQR VISA 地址。", parent=parent)
            return
        self.connect_devices()

    def _copy_device_details(self) -> None:
        details = (
            f"回放模式：{self.playback_mode_var.get()}\n"
            f"设备链路：{self.route_var.get()}\n"
            f"连接状态：{self.device_status_var.get()}\n"
            f"SMBV100A VISA：{self.smw_address_var.get()}\n"
            f"SMBV100A IDN：{self.device_identities[0]}\n"
            f"IQW/IQR VISA：{self.iqr_address_var.get()}\n"
            f"IQW/IQR IDN：{self.device_identities[1]}\n"
            f"记录仪波形路径：{self.iqr_waveform_var.get() or '未填写'}\n"
            f"IQR Player屏幕显示：{self.iqr_display_var.get()}"
        )
        self.clipboard_clear()
        self.clipboard_append(details)
        self.update_idletasks()
        self.status_var.set("完整设备连接信息已复制。")

    def _close_device_details(self) -> None:
        self.popup_iqr_address_entry = None
        self.popup_iqr_path_entry = None
        if self.device_details_window is not None and self.device_details_window.winfo_exists():
            self.device_details_window.destroy()
        self.device_details_window = None

    def send_and_configure(self) -> None:
        if self.current_result is None:
            messagebox.showwarning("没有回放源", "请先载入原始采集数据或重构结果。")
            return
        if self.route_var.get() == ROUTE_ARB and self.current_package is None:
            messagebox.showwarning("波形未准备", "SMBV100A 内部 ARB 回放前请先生成设备波形包。")
            return
        if not self.connected:
            messagebox.showwarning("设备未连接", "请先连接设备或启用设备仿真模式。")
            return
        try:
            settings = self._settings()
            errors = validate_playback_settings(self.current_result, settings)
            if errors:
                raise ValueError("\n".join(errors))
            if self.route_var.get() == ROUTE_IQR and self._is_raw_mode() and not self.simulation_var.get():
                if not self.iqr_catalog_loaded:
                    raise ValueError("尚未读取IQR设备目录，请先刷新设备记录。")
                queue_items = (
                    self.raw_sequence
                    if self._is_raw_sequence_mode() and self.raw_sequence
                    else [(self.raw_recording, "")]
                )
                unavailable = [
                    recording.stem
                    for recording, _context in queue_items
                    if recording is not None
                    and (
                        self._iqr_recording_for(recording) is None
                        or not self._iqr_recording_for(recording).is_complete
                    )
                ]
                if unavailable:
                    preview = "、".join(unavailable[:5])
                    extra = f"等{len(unavailable)}条" if len(unavailable) > 5 else ""
                    raise ValueError(
                        f"IQR中缺少完整的同名记录：{preview}{extra}。"
                        "请确认e:/.ws1与f:/.ws2均存在，或改为单条回放。"
                    )
                current_device_recording = self._iqr_recording_for(self.raw_recording)
                if current_device_recording is None:
                    raise ValueError("当前原始IQ在IQR中不可用。")
                self.iqr_waveform_var.set(current_device_recording.waveform_path)
            elif self.route_var.get() == ROUTE_IQR and not self.iqr_waveform_var.get().strip():
                raise ValueError("IQW/IQR数字IQ链路需要填写记录仪中的已有波形路径。")
        except ValueError as exc:
            messagebox.showerror("设备参数无效", str(exc))
            return
        self.device_status_var.set("正在发送波形并配置设备；RF保持关闭...")
        threading.Thread(
            target=self._send_worker,
            args=(settings, self._is_raw_sequence_mode()),
            daemon=True,
        ).start()

    def _send_worker(self, settings: PlaybackSettings, sequence_mode: bool = False) -> None:
        try:
            if self.simulation_var.get() or settings.route == ROUTE_SIM:
                self.messages.put(("configured", "仿真配置完成，RF保持关闭。"))
                return
            if self.session is None or self.current_result is None:
                raise RuntimeError("设备会话或回放源不可用。")
            if settings.route == ROUTE_ARB:
                if self.current_package is None:
                    raise RuntimeError("设备波形包不可用。")
                status = self.session.upload_and_configure_smw(
                    self.current_package,
                    self.current_result.center_frequency_mhz,
                    settings.rf_level_dbm,
                )
            else:
                status = self.session.configure_external_digital_iq(
                    self.current_result.sample_rate_hz,
                    self.current_result.center_frequency_mhz,
                    settings.rf_level_dbm,
                )
                iqr_status = self.session.load_iqr_recording(
                    self.iqr_waveform_var.get().strip(),
                    settings.run_mode == "连续" and not sequence_mode,
                    settings.external_10mhz_reference,
                    settings.iqr_display_mode,
                )
                status = f"SMBV100A: {status}｜IQR100: {iqr_status}"
            self.messages.put(("configured", status))
        except Exception as exc:
            self.messages.put(("error", f"设备配置失败：{exc}"))

    def start_playback(self) -> None:
        if not self.device_configured:
            messagebox.showwarning("尚未配置", "请先发送波形并配置设备。")
            return
        if self.playing:
            self.status_var.set("设备当前正在回放。")
            return
        if self.start_transitioning or self.sequence_transitioning:
            self.status_var.set("IQR100正在准备回放，请等待设备样本计数开始增长。")
            return
        use_hardware_iqr = (
            self.session is not None
            and not self.simulation_var.get()
            and self.route_var.get() == ROUTE_IQR
        )
        display_mode = "FFT" if self.iqr_display_var.get() == IQR_DISPLAY_FFT else "IQ"
        if use_hardware_iqr:
            assert self.session is not None
            resume_paused_cycle = self.session.iqr_paused
            if not resume_paused_cycle:
                self.preview_elapsed_origin_ms = 0.0
                self.display_elapsed_ms = 0.0
                self.position_ms_var.set(0.0)
                self._reset_preview_trend()
                self.draw_preview()
            try:
                expected_duration_s = max(0.001, float(self.duration_var.get()) * 1e-3)
            except ValueError:
                expected_duration_s = max(0.001, self.current_result.duration_s)
            watch_single_completion = (
                not resume_paused_cycle
                and not self.loop_enabled_var.get()
                and not self._is_raw_sequence_mode()
            )
            request_id, cancel_event = self._begin_playback_operation()
            self.start_transitioning = True
            self.playing = False
            self.hardware_playback_active = False
            self.preview_clock_started_at = None
            if self.play_after_id is not None:
                self.after_cancel(self.play_after_id)
                self.play_after_id = None
            self.device_status_var.set(
                "已发送开始请求，IQR100正在装载数据；等待Player进入Running且样本计数开始增长。"
            )
            self.play_status_var.set("等待IQR100实际数据流启动，软件波形暂不计时")
            self._log(
                "IQR100正在准备回放；设备进入Running且硬件样本计数增长前，"
                "软件波形和计时保持静止。"
            )
            threading.Thread(
                target=self._start_iqr_worker,
                args=(
                    self.session,
                    display_mode,
                    self.loop_enabled_var.get() and not self._is_raw_sequence_mode(),
                    watch_single_completion,
                    expected_duration_s,
                    30.0,
                    request_id,
                    cancel_event,
                ),
                daemon=True,
            ).start()
            return
        try:
            if self.session is not None and not self.simulation_var.get():
                self.session.start(
                    use_iqr=False,
                    iqr_display_mode=display_mode,
                )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.playing = True
        self.hardware_playback_active = True
        self._start_preview_clock()
        self._log("回放已开始；RF输出仍由独立安全按钮控制。")
        if not self.play_after_id:
            self._advance_preview()

    def _begin_playback_operation(self) -> tuple[int, threading.Event]:
        self.playback_cancel_event.set()
        self.playback_request_id += 1
        self.playback_cancel_event = threading.Event()
        return self.playback_request_id, self.playback_cancel_event

    def _cancel_playback_operation(self) -> None:
        self.playback_cancel_event.set()
        self.playback_request_id += 1
        self.start_transitioning = False

    def _start_iqr_worker(
        self,
        session: VisaPlaybackSession,
        display_mode: str,
        expected_continuous: bool,
        watch_single_completion: bool,
        expected_duration_s: float,
        completion_timeout_s: float,
        request_id: int,
        cancel_event: threading.Event,
    ) -> None:
        try:
            if cancel_event.is_set():
                return
            # Read settings before the trigger.  Some IQR firmware does not
            # answer VISA queries while a native recording is being replayed;
            # querying immediately after EXECute can therefore time out even
            # though the hardware completes the replay successfully.
            run_mode = session.query_iqr_player_run_mode()
            expected_prefix = "CONT" if expected_continuous else "SING"
            if expected_prefix not in run_mode.upper():
                expected_text = "CONTinuous" if expected_continuous else "SINGle"
                raise RuntimeError(
                    f"循环设置未与IQR100同步：软件要求{expected_text}，设备回读{run_mode}。"
                )
            session.start(use_iqr=True, iqr_display_mode=display_mode)
            if cancel_event.is_set():
                session.stop(use_iqr=True)
                return
            self.messages.put(
                (
                    "playback_triggered",
                    (request_id, run_mode, expected_duration_s),
                )
            )
            if watch_single_completion:
                # Do not poll IQR during the known replay interval.  On the
                # affected firmware a STATe?/SAMPles? query occupies the VISA
                # session until its 10 s timeout.  Wait locally, then confirm
                # Ready after the recorder has returned to its command loop.
                settling_s = min(2.0, max(0.75, expected_duration_s * 0.05))
                if cancel_event.wait(expected_duration_s + settling_s):
                    return
                try:
                    completed_state, final_samples = session.wait_iqr_player_complete(
                        timeout_s=completion_timeout_s,
                        cancel_check=cancel_event.is_set,
                    )
                except InterruptedError:
                    return
                except Exception as exc:
                    error_text = str(exc)
                    is_visa_timeout = (
                        "VI_ERROR_TMO" in error_text
                        or "-1073807339" in error_text
                        or "timeout expired" in error_text.casefold()
                    )
                    if is_visa_timeout:
                        self.messages.put(
                            (
                                "log",
                                "IQR100已到达单次回放预计结束时间；设备状态查询仍超时，"
                                "按已发送的单次任务结束处理。",
                            )
                        )
                        self.messages.put(
                            (
                                "playback_completed",
                                (request_id, "预计时长已结束（状态回读超时）", None),
                            )
                        )
                    else:
                        self.messages.put(
                            (
                                "playback_monitor_error",
                                (request_id, f"IQR100回放完成状态监测失败：{exc}"),
                            )
                        )
                else:
                    self.messages.put(
                        (
                            "playback_completed",
                            (request_id, completed_state, final_samples),
                        )
                    )
        except InterruptedError:
            try:
                session.stop(use_iqr=True)
            except Exception:
                pass
        except Exception as exc:
            try:
                session.stop(use_iqr=True)
            except Exception:
                pass
            self.messages.put(("playback_start_error", (request_id, f"IQR100启动失败：{exc}")))

    def pause_playback(self) -> None:
        if self.start_transitioning:
            self._cancel_playback_operation()
            self.sequence_transitioning = False
            try:
                if self.session is not None and not self.simulation_var.get():
                    self.session.stop(use_iqr=self.route_var.get() == ROUTE_IQR)
            except Exception as exc:
                messagebox.showerror("取消启动失败", str(exc))
            self.device_status_var.set("IQR100启动等待已取消；设备保持已配置状态。")
            self.play_status_var.set("启动已取消，波形未开始计时")
            self._log("IQR100启动等待已取消。")
            return
        self._freeze_preview_clock()
        self.playing = False
        self.hardware_playback_active = False
        if self.play_after_id is not None:
            self.after_cancel(self.play_after_id)
            self.play_after_id = None
        try:
            if self.session is not None and not self.simulation_var.get():
                self.session.pause(use_iqr=self.route_var.get() == ROUTE_IQR)
        except Exception as exc:
            messagebox.showerror("暂停失败", str(exc))
        self._log("回放已暂停。")

    def stop_playback(self, notify: bool = True) -> None:
        was_preparing = self.start_transitioning or self.sequence_transitioning
        self._cancel_playback_operation()
        self.playing = False
        self.hardware_playback_active = False
        self.sequence_transitioning = False
        self.stop_preview(True)
        try:
            if self.session is not None and not self.simulation_var.get():
                self.session.stop(use_iqr=self.route_var.get() == ROUTE_IQR)
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))
            return
        stopped_text = "准备已取消，波形未播放" if was_preparing else "回放已停止"
        self.play_status_var.set(stopped_text)
        self.status_var.set(stopped_text + "。")
        self._log("IQR100准备已取消，回放已停止。" if was_preparing else "回放已停止。")
        if notify:
            if was_preparing:
                detail = "已取消设备回放准备，设备不会开始本次回放。"
            elif self.simulation_var.get():
                detail = "仿真回放已停止。"
            else:
                detail = "已向设备发送停止命令，当前回放已停止。"
            if self.rf_enabled:
                detail += "\n\n注意：RF输出仍处于开启状态，请根据需要单独关闭。"
                messagebox.showwarning("回放已停止", detail)
            else:
                messagebox.showinfo("回放已停止", detail)

    def _update_rf_button(self) -> None:
        enabled = (
            self.connected
            and self.device_configured
            and self.safety_confirm_var.get()
            and not self.simulation_var.get()
        )
        self.rf_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def toggle_rf(self) -> None:
        if not self.safety_confirm_var.get():
            messagebox.showwarning("安全联锁未确认", "请确认射频链路、衰减器、功放和负载安全。")
            return
        requested = not self.rf_enabled
        if requested:
            if not messagebox.askyesno(
                "确认打开RF输出",
                f"将以{self.rf_level_var.get()} dBm打开SMBV100A射频输出。\n"
                "请再次确认后级链路和负载安全。",
            ):
                return
        try:
            if self.session is not None and not self.simulation_var.get():
                self.session.set_rf(requested)
        except Exception as exc:
            messagebox.showerror("RF控制失败", str(exc))
            return
        self.rf_enabled = requested
        self.rf_button.configure(text=f"RF输出：{'开启' if requested else '关闭'}")
        self._log(f"RF输出切换为{'开启' if requested else '关闭'}。")

    def toggle_preview(self) -> None:
        if self.current_result is None:
            return
        if self.playing:
            self.pause_playback()
            self.play_button.configure(text="继续预览")
            return
        self.playing = True
        self.hardware_playback_active = False
        self._start_preview_clock()
        self.play_button.configure(text="暂停预览")
        self._advance_preview()

    def stop_preview(self, reset: bool = True) -> None:
        self.playing = False
        self.preview_clock_started_at = None
        if self.play_after_id is not None:
            self.after_cancel(self.play_after_id)
            self.play_after_id = None
        if hasattr(self, "play_button"):
            self.play_button.configure(text="开始波形预览")
        if reset:
            self.preview_elapsed_origin_ms = 0.0
            self.display_elapsed_ms = 0.0
            self.position_ms_var.set(0.0)
            self._reset_preview_trend()
            if self.current_result is not None:
                self.draw_preview()

    def _start_preview_clock(self) -> None:
        self.preview_elapsed_origin_ms = self.display_elapsed_ms
        self.preview_clock_started_at = time.perf_counter()

    def _freeze_preview_clock(self) -> None:
        if self.preview_clock_started_at is None:
            return
        elapsed_wall_ms = (time.perf_counter() - self.preview_clock_started_at) * 1e3
        self.display_elapsed_ms = self.preview_elapsed_origin_ms + elapsed_wall_ms
        self.preview_elapsed_origin_ms = self.display_elapsed_ms
        self.preview_clock_started_at = None

    def _on_seek(self, value: str) -> None:
        try:
            selected_ms = max(0.0, float(value))
        except ValueError:
            return
        self.display_elapsed_ms = selected_ms
        self._reset_preview_trend()
        if self.playing:
            self._start_preview_clock()
        self.draw_preview()

    def _advance_preview(self) -> None:
        if not self.playing or self.current_result is None:
            self.play_after_id = None
            return
        try:
            requested_ms = max(0.001, float(self.duration_var.get()))
        except ValueError:
            requested_ms = max(0.001, self.current_result.duration_s * 1e3)
        if self.preview_clock_started_at is None:
            self._start_preview_clock()
        elapsed_wall_ms = (time.perf_counter() - self.preview_clock_started_at) * 1e3
        elapsed_ms = self.preview_elapsed_origin_ms + elapsed_wall_ms
        if elapsed_ms >= requested_ms and self._is_raw_sequence_mode():
            self.display_elapsed_ms = requested_ms
            self.position_ms_var.set(requested_ms)
            self.draw_preview()
            self.play_after_id = None
            self._advance_raw_sequence()
            return
        if elapsed_ms >= requested_ms and not self.loop_enabled_var.get():
            self.display_elapsed_ms = requested_ms
            self.position_ms_var.set(requested_ms)
            self.draw_preview()
            self.playing = False
            self.preview_clock_started_at = None
            self.play_after_id = None
            self.play_button.configure(text="重新预览")
            return
        self.display_elapsed_ms = elapsed_ms
        self.position_ms_var.set(elapsed_ms % requested_ms)
        self.draw_preview()
        self.play_after_id = self.after(100, self._advance_preview)

    def _advance_raw_sequence(self) -> None:
        if self.sequence_transitioning or not self.raw_sequence:
            return
        next_index = self.raw_sequence_index + 1
        if next_index >= len(self.raw_sequence):
            if self.loop_enabled_var.get():
                next_index = 0
            else:
                if (
                    self.hardware_playback_active and self.rf_enabled and self.session is not None
                    and not self.simulation_var.get()
                ):
                    try:
                        self.session.set_rf(False)
                    except Exception as exc:
                        self._log(f"场景顺序结束时关闭RF失败：{exc}")
                self.rf_enabled = False
                self.rf_button.configure(text="RF输出：关闭")
                self.playing = False
                self.hardware_playback_active = False
                self.preview_clock_started_at = None
                self.play_button.configure(text="重新预览")
                message = f"{self.raw_scene_var.get()}场景全部{len(self.raw_sequence)}条IQ已依次回放完成。"
                self.play_status_var.set(message)
                self.status_var.set(message)
                self._log(message)
                return

        hardware_active = self.hardware_playback_active
        self.sequence_transitioning = True
        self.raw_sequence_index = next_index
        recording, context = self.raw_sequence[next_index]
        self._load_raw_recording_source(recording, context)
        queue_position = f"{next_index + 1}/{len(self.raw_sequence)}"

        if not hardware_active:
            self.sequence_transitioning = False
            self.playing = True
            self.hardware_playback_active = False
            self._start_preview_clock()
            self.play_button.configure(text="暂停预览")
            self._log(f"预览切换到场景队列 {queue_position}：{recording.stem}")
            self._advance_preview()
            return

        if self.simulation_var.get() or self.route_var.get() == ROUTE_SIM:
            self.sequence_transitioning = False
            self.device_configured = True
            self.playing = True
            self.hardware_playback_active = True
            self._start_preview_clock()
            self._log(f"仿真回放切换到场景队列 {queue_position}：{recording.stem}")
            self._advance_preview()
            return

        if self.session is None or self.current_result is None:
            self.sequence_transitioning = False
            self.hardware_playback_active = False
            self.device_status_var.set("场景顺序切换失败：设备会话不可用。")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            self.sequence_transitioning = False
            self.hardware_playback_active = False
            self.device_status_var.set(f"场景顺序切换失败：{exc}")
            return
        restore_rf = self.rf_enabled and self.safety_confirm_var.get()
        self.rf_enabled = False
        self.rf_button.configure(text="RF输出：关闭")
        request_id, cancel_event = self._begin_playback_operation()
        self.start_transitioning = True
        self.play_status_var.set("正在装载下一条IQ，软件波形暂不计时")
        self.device_status_var.set(
            f"正在切换场景队列 {queue_position}：{recording.stem}；"
            "等待IQR100进入Running且样本计数开始增长..."
        )
        threading.Thread(
            target=self._sequence_transition_worker,
            args=(
                self.session,
                self.current_result,
                self.iqr_waveform_var.get().strip(),
                settings,
                restore_rf,
                request_id,
                cancel_event,
            ),
            daemon=True,
        ).start()

    def _sequence_transition_worker(
        self,
        session: VisaPlaybackSession,
        result: ReconstructionResult,
        waveform_path: str,
        settings: PlaybackSettings,
        restore_rf: bool,
        request_id: int,
        cancel_event: threading.Event,
    ) -> None:
        try:
            if cancel_event.is_set():
                return
            session.stop(use_iqr=True)
            smw_status = session.configure_external_digital_iq(
                result.sample_rate_hz,
                result.center_frequency_mhz,
                settings.rf_level_dbm,
            )
            if cancel_event.is_set():
                return
            iqr_status = session.load_iqr_recording(
                waveform_path,
                False,
                settings.external_10mhz_reference,
                settings.iqr_display_mode,
            )
            if cancel_event.is_set():
                return
            session.start(use_iqr=True, iqr_display_mode=settings.iqr_display_mode)
            if cancel_event.is_set():
                session.stop(use_iqr=True)
                return
            if restore_rf:
                session.set_rf(True)
            self.messages.put(
                (
                    "sequence_started",
                    (
                        request_id,
                        smw_status,
                        f"{iqr_status}｜LAN触发命令已发送｜"
                        "设备执行期间暂停IQR状态轮询，避免固件VISA超时",
                        restore_rf,
                    ),
                )
            )
        except InterruptedError:
            try:
                session.stop(use_iqr=True)
            except Exception:
                pass
        except Exception as exc:
            try:
                session.stop(use_iqr=True)
            except Exception:
                pass
            self.messages.put(
                ("sequence_error", (request_id, f"场景顺序切换失败：{exc}"))
            )

    def _window_data(self) -> tuple[np.ndarray, float, float]:
        result = self.current_result
        assert result is not None
        try:
            target_peak = min(HARD_MAX_DIGITAL_PEAK_DBFS, float(self.peak_entry_var.get()))
        except ValueError:
            target_peak = DEFAULT_DIGITAL_PEAK_DBFS
        window_ms = max(0.01, float(self.window_ms_var.get()))
        position_ms = self.position_ms_var.get()
        count = max(32, int(round(window_ms * 1e-3 * result.sample_rate_hz)))
        preview_peak = float(np.max(np.abs(result.iq))) if result.iq.size else 0.0
        desired_peak = 10.0 ** (target_peak / 20.0)
        gain = desired_peak / max(preview_peak, 1e-12)

        if self._is_raw_mode() and self.raw_recording is not None:
            recording = self.raw_recording
            buffer_ms = recording.duration_s * 1e3
            buffer_position_ms = position_ms % max(buffer_ms, 1e-12)
            start = int(round(buffer_position_ms * 1e-3 * recording.sample_rate_hz)) % recording.total_samples
            first_count = min(count, recording.total_samples - start)
            pieces = [read_iq_contiguous(recording, start, first_count)]
            remaining = count - first_count
            if remaining > 0:
                pieces.append(read_iq_contiguous(recording, 0, remaining))
            source = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        else:
            buffer_ms = result.duration_s * 1e3
            buffer_position_ms = position_ms % max(buffer_ms, 1e-12)
            start = int(round(buffer_position_ms * 1e-3 * result.sample_rate_hz)) % result.iq.size
            indices = (start + np.arange(count, dtype=np.int64)) % result.iq.size
            source = result.iq[indices]
        return (source * gain).astype(np.complex64), window_ms, buffer_position_ms

    def draw_preview(self) -> None:
        result = self.current_result
        if result is None:
            return
        try:
            source, window_ms, buffer_position_ms = self._window_data()
        except Exception:
            return
        stride = max(1, math.ceil(source.size / 5000))
        source_plot = source[::stride]
        elapsed_ms = self.display_elapsed_ms
        window_time_ms = np.arange(source_plot.size) * stride / result.sample_rate_hz * 1e3
        elapsed_text = self._format_elapsed_time(elapsed_ms)

        source_label = self._source_label()
        compact = (
            self.compact_preview_layout
            if self.compact_preview_layout is not None
            else self.canvas_widget.winfo_height() < 260
        )
        self.waveform_axis.clear()
        self.waveform_axis.plot(
            window_time_ms, source_plot.real, color="#2563eb", linewidth=0.75, label="I"
        )
        self.waveform_axis.plot(
            window_time_ms, source_plot.imag, color="#dc2626", linewidth=0.75, alpha=0.82, label="Q"
        )
        waveform_title = "输入时域波形" if compact else f"{source_label}时域波形｜已回放 {elapsed_text}"
        self.waveform_axis.set(
            title=waveform_title,
            xlabel="时间 (ms)" if compact else "当前窗口时间 (ms)",
            ylabel="" if compact else "归一化幅度",
            xlim=(0.0, window_ms),
        )
        self.waveform_axis.ticklabel_format(axis="x", style="plain", useOffset=False)
        self.waveform_axis.set_title(waveform_title, fontsize=9 if compact else 10, pad=2 if compact else 6)
        self.waveform_axis.xaxis.label.set_fontsize(8 if compact else 10)
        self.waveform_axis.xaxis.labelpad = 1 if compact else 4
        self.waveform_axis.grid(alpha=0.2)
        self.waveform_axis.legend(
            loc="upper right",
            ncol=2,
            fontsize=7 if compact else 9,
            handlelength=1.4 if compact else 2.0,
            borderpad=0.25 if compact else 0.4,
            labelspacing=0.2 if compact else 0.5,
        )

        fft_points = min(source.size, 131_072)
        window = np.hanning(fft_points)
        source_spectrum = np.fft.fftshift(np.fft.fft(source[:fft_points] * window))
        source_db = 20.0 * np.log10(np.maximum(np.abs(source_spectrum), 1e-12))
        source_db -= float(np.max(source_db))
        frequency_mhz = result.center_frequency_mhz + np.fft.fftshift(
            np.fft.fftfreq(fft_points, d=1.0 / result.sample_rate_hz)
        ) / 1e6
        self.spectrum_axis.clear()
        self.spectrum_axis.plot(
            frequency_mhz,
            source_db,
            color="#2563eb",
            linewidth=0.8,
            label=f"{source_label}设备输入",
        )
        self.spectrum_axis.set(
            title="输入频谱" if compact else "设备回放输入频谱",
            xlabel="频率 (MHz)",
            ylabel="" if compact else "相对幅度 (dB)",
            ylim=(-100, 3),
        )
        self.spectrum_axis.set_title(
            "输入频谱" if compact else "设备回放输入频谱",
            fontsize=9 if compact else 10,
            pad=2 if compact else 6,
        )
        self.spectrum_axis.xaxis.label.set_fontsize(8 if compact else 10)
        self.spectrum_axis.xaxis.labelpad = 1 if compact else 4
        self.spectrum_axis.grid(alpha=0.2)
        if not compact:
            self.spectrum_axis.legend(loc="upper right")
            self.figure.suptitle(
                f"{self.playback_mode_var.get()}｜{result.name}｜"
                f"{result.center_frequency_mhz:.6f} MHz｜{result.sample_rate_hz / 1e6:.3f} MS/s",
                fontsize=11,
            ).set_visible(True)
            self.figure.set_constrained_layout_pads(w_pad=0.02, h_pad=0.02, wspace=0.04, hspace=0.02)
        else:
            if self.figure._suptitle is not None:
                self.figure._suptitle.set_visible(False)
            self.figure.set_constrained_layout_pads(w_pad=0.005, h_pad=0.005, wspace=0.025, hspace=0.005)
        for axis in (self.waveform_axis, self.spectrum_axis):
            axis.tick_params(labelsize=7 if compact else 9, pad=1 if compact else 3)
        self.canvas.draw_idle()
        self.play_status_var.set(
            f"已回放 {elapsed_text}｜当前数据位置 {buffer_position_ms:.3f} ms｜细节窗口 {window_ms:g} ms"
        )

    def _reset_preview_trend(self) -> None:
        self.preview_trend.clear()

    def _record_preview_trend(self, source: np.ndarray) -> None:
        magnitude = np.abs(source)
        peak = float(np.max(magnitude))
        rms = float(np.sqrt(np.mean(np.square(magnitude))))
        point = (self.display_elapsed_ms / 1e3, peak, rms)
        if self.preview_trend and abs(self.preview_trend[-1][0] - point[0]) < 1e-9:
            self.preview_trend[-1] = point
        else:
            self.preview_trend.append(point)
        cutoff_s = max(0.0, point[0] - self.preview_trend_span_s * 2.0)
        first_visible = next(
            (index for index, item in enumerate(self.preview_trend) if item[0] >= cutoff_s),
            len(self.preview_trend) - 1,
        )
        if first_visible:
            del self.preview_trend[:first_visible]

    @staticmethod
    def _format_elapsed_time(elapsed_ms: float) -> str:
        if elapsed_ms >= 3_600_000.0:
            return f"{elapsed_ms / 3_600_000.0:.2f} h"
        if elapsed_ms >= 60_000.0:
            return f"{elapsed_ms / 60_000.0:.2f} min"
        if elapsed_ms >= 1_000.0:
            return f"{elapsed_ms / 1_000.0:.3f} s"
        return f"{elapsed_ms:.3f} ms"

    def _update_parameter_text(self) -> None:
        result = self.current_result
        if result is None:
            text = f"尚未载入{self.playback_mode_var.get()}。"
        else:
            try:
                settings = self._settings()
                scaled, gain_db = scale_iq_to_peak(result.iq, settings.digital_peak_dbfs)
                peak_dbfs, rms_dbfs, papr_db = iq_level_metrics(scaled)
                errors = validate_playback_settings(result, settings)
            except ValueError as exc:
                text = str(exc)
            else:
                original_duration = result.metadata.get("original_duration_s")
                original_note = (
                    f"原始记录时长：{float(original_duration):.6f} s\n"
                    if original_duration is not None
                    else ""
                )
                text = (
                    f"回放模式：{self.playback_mode_var.get()}\n"
                    f"回放源：{result.name}\n"
                    f"信号方式：{result.mode}\n"
                    f"中心频率：{result.center_frequency_mhz:.6f} MHz\n"
                    f"采样率：{result.sample_rate_hz / 1e6:.6f} MS/s\n"
                    f"{original_note}"
                    f"预览/设备波形缓冲区：{result.duration_s * 1e3:.3f} ms / {result.iq.size:,}复采样点\n"
                    f"目标回放时长：{settings.requested_duration_s * 1e3:.3f} ms\n\n"
                    f"目标数字峰值：{peak_dbfs:.3f} dBFS（硬限值0 dBFS）\n"
                    f"数字RMS：{rms_dbfs:.3f} dBFS\n"
                    f"PAPR：{papr_db:.3f} dB\n"
                    f"相对源 IQ 增益：{gain_db:+.3f} dB\n"
                    f"SMBV100A射频输出设定：{settings.rf_level_dbm:.3f} dBm\n"
                    f"软件RF安全上限：{settings.rf_safety_limit_dbm:.3f} dBm\n\n"
                    f"校验结果：{'通过' if not errors else '；'.join(errors)}\n\n"
                    "说明：最终时域图由 I/Q 正交合成得到，使用低显示中频便于观察波形；"
                    "仪器实际在设定中心频率上变频。当前图表仅表示送入设备的数字 I/Q，"
                    "不代表 RF 端实测输出。"
                )
        self.parameter_text.configure(state=tk.NORMAL)
        self.parameter_text.delete("1.0", tk.END)
        self.parameter_text.insert("1.0", text)
        self.parameter_text.configure(state=tk.DISABLED)

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{stamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "package":
                self.current_package = payload  # type: ignore[assignment]
                package = self.current_package
                self.validation_var.set(
                    f"校验通过：{package.waveform_file.name}｜峰值{package.peak_dbfs:.3f} dBFS｜"
                    f"RMS {package.rms_dbfs:.3f} dBFS｜PAPR {package.papr_db:.3f} dB"
                )
                self._update_action_states()
                self._log(f"设备波形包已生成：{package.directory}")
                self._update_parameter_text()
                self.draw_preview()
            elif kind == "connected":
                self.session, identities, catalog, catalog_error = payload  # type: ignore[misc]
                self.connected = True
                self.device_identities = identities
                self.smw_identity_var.set(identities[0])
                self.iqr_identity_var.set(identities[1])
                count = 2 if identities[1] != "未连接" else 1
                self.iqr_catalog_busy = False
                if identities[1] != "未连接" and not catalog_error:
                    self._apply_iqr_catalog(tuple(catalog))
                    complete = sum(item.is_complete for item in catalog)
                    incomplete = len(catalog) - complete
                    self.device_status_var.set(
                        f"已连接 {count} 台设备；IQR发现{complete}条完整记录"
                        + (f"、{incomplete}条不完整记录。" if incomplete else "。")
                    )
                elif catalog_error:
                    self.iqr_catalog_loaded = False
                    self.device_status_var.set(f"已连接 {count} 台设备；{catalog_error}")
                    self._set_iqr_catalog_status(str(catalog_error), "#b91c1c")
                    self.iqr_catalog_button.configure(state=tk.NORMAL)
                else:
                    self.device_status_var.set(f"已连接 {count} 台设备。")
                self._update_action_states()
                self._update_rf_button()
            elif kind == "iqr_catalog":
                catalog = tuple(payload)  # type: ignore[arg-type]
                self._apply_iqr_catalog(catalog)
                complete = sum(item.is_complete for item in catalog)
                incomplete = len(catalog) - complete
                self.device_status_var.set(
                    f"IQR目录已刷新：{complete}条完整记录"
                    + (f"、{incomplete}条不完整记录。" if incomplete else "。")
                )
                self._log(self.device_status_var.get())
            elif kind == "iqr_catalog_error":
                self.iqr_catalog_busy = False
                self.iqr_catalog_loaded = False
                self.iqr_catalog.clear()
                self.iqr_catalog_button.configure(
                    state=tk.NORMAL if self.connected and not self.simulation_var.get() else tk.DISABLED
                )
                self.device_status_var.set(str(payload))
                self._set_iqr_catalog_status(str(payload), "#b91c1c")
                self._update_action_states()
                self._log(str(payload))
            elif kind == "configured":
                self.device_configured = True
                self.device_status_var.set(str(payload))
                self._log(f"设备配置完成：{payload}")
                self._update_rf_button()
            elif kind == "playback_triggered":
                request_id, run_mode, expected_duration_s = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.playing = True
                self.hardware_playback_active = True
                self.device_status_var.set(
                    f"IQR100已接受LAN回放触发｜运行方式：{run_mode}｜"
                    f"预计单次时长：{expected_duration_s:.3f} s｜"
                    "设备执行期间暂停状态查询，完成后自动更新"
                )
                self.play_status_var.set("IQR100正在执行回放，软件波形开始同步计时")
                self.play_button.configure(text="暂停预览")
                self._start_preview_clock()
                self._log(
                    f"IQR100 LAN触发命令已发送；运行方式{run_mode}；"
                    "为兼容设备固件，回放期间不发送状态查询。"
                )
                if not self.play_after_id:
                    self._advance_preview()
            elif kind == "playback_started":
                request_id, player_state, run_mode, replayed_samples, link_status = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.playing = True
                self.hardware_playback_active = True
                self.device_status_var.set(
                    f"IQR100实际数据流已启动｜Player：{player_state}｜"
                    f"样本计数：{replayed_samples:g} Sa｜运行方式：{run_mode}｜{link_status}"
                )
                self.play_status_var.set("IQR100样本计数已增长，软件波形开始同步计时")
                self.play_button.configure(text="暂停预览")
                self._start_preview_clock()
                self._log(
                    f"IQR100已进入{player_state}且样本计数为{replayed_samples:g} Sa，"
                    f"软件波形现在开始播放；运行方式{run_mode}；{link_status}。"
                )
                if not self.play_after_id:
                    self._advance_preview()
            elif kind == "playback_start_error":
                request_id, error_text = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.playing = False
                self.hardware_playback_active = False
                self.preview_clock_started_at = None
                self.device_status_var.set(str(error_text))
                self.play_status_var.set("设备实际数据流未启动，软件波形未播放")
                self._log(str(error_text))
                messagebox.showerror("IQR100启动失败", str(error_text))
            elif kind == "playback_completed":
                request_id, player_state, final_samples = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.playing = False
                self.hardware_playback_active = False
                self.preview_clock_started_at = None
                if self.play_after_id is not None:
                    self.after_cancel(self.play_after_id)
                    self.play_after_id = None
                try:
                    completed_ms = max(0.001, float(self.duration_var.get()))
                except ValueError:
                    completed_ms = (
                        max(0.001, self.current_result.duration_s * 1e3)
                        if self.current_result
                        else 0.001
                    )
                self.display_elapsed_ms = completed_ms
                self.preview_elapsed_origin_ms = completed_ms
                self.position_ms_var.set(completed_ms)
                if self.current_result is not None:
                    self.draw_preview()
                self.play_button.configure(text="重新预览")
                sample_text = (
                    f"{float(final_samples):g} Sa"
                    if final_samples is not None
                    else "设备未回读"
                )
                self.device_status_var.set(
                    f"IQR100单次回放已完成｜Player：{player_state}｜"
                    f"最终样本计数：{sample_text}｜可直接再次点击开始回放"
                )
                self.play_status_var.set("IQR100单次回放已完成，可直接重新回放")
                self.status_var.set("设备单次回放已完成。")
                self._log(
                    f"IQR100单次回放已完成：Player={player_state}，"
                    f"最终样本计数={sample_text}；设备保持已配置状态。"
                )
            elif kind == "playback_monitor_error":
                request_id, error_text = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self._log(str(error_text))
                self.device_status_var.set(str(error_text))
            elif kind == "sequence_started":
                request_id, smw_status, iqr_status, restored_rf = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.sequence_transitioning = False
                self.device_configured = True
                self.playing = True
                self.hardware_playback_active = True
                self.rf_enabled = bool(restored_rf)
                self.rf_button.configure(text=f"RF输出：{'开启' if restored_rf else '关闭'}")
                self.device_status_var.set(f"SMBV100A: {smw_status}｜IQR100: {iqr_status}")
                self._start_preview_clock()
                self._log(
                    f"场景顺序已切换到 {self.raw_sequence_index + 1}/{len(self.raw_sequence)}："
                    f"{self.raw_recording.stem if self.raw_recording is not None else '未知IQ'}"
                )
                self._advance_preview()
            elif kind == "sequence_error":
                request_id, error_text = payload  # type: ignore[misc]
                if request_id != self.playback_request_id:
                    continue
                self.start_transitioning = False
                self.sequence_transitioning = False
                self.playing = False
                self.hardware_playback_active = False
                self.device_configured = False
                self.rf_enabled = False
                self.rf_button.configure(text="RF输出：关闭")
                self.device_status_var.set(str(error_text))
                self.play_status_var.set("下一条IQ实际数据流未启动，软件波形未播放")
                self._log(str(error_text))
                messagebox.showerror("场景顺序回放失败", str(error_text))
            elif kind == "log":
                self._log(str(payload))
            elif kind == "connect_error":
                self.connected = False
                self.iqr_catalog.clear()
                self.iqr_catalog_loaded = False
                self.iqr_catalog_busy = False
                self.device_identities = ("未连接", "未连接")
                self.smw_identity_var.set("未连接")
                self.iqr_identity_var.set("未连接")
                self.device_status_var.set(str(payload))
                self.iqr_catalog_button.configure(state=tk.DISABLED)
                self._update_iqr_match_feedback(self.raw_recording or self._selected_raw_recording())
                self._update_action_states()
                self._update_rf_button()
                self._log(str(payload))
                messagebox.showerror("回放操作失败", str(payload))
            elif kind == "error":
                self.device_status_var.set(str(payload))
                self._log(str(payload))
                messagebox.showerror("回放操作失败", str(payload))
        self.after(100, self._poll_messages)

    def rescale_figure(self, dpi: float) -> None:
        width = max(1, self.canvas_widget.winfo_width())
        height = max(1, self.canvas_widget.winfo_height())
        self.figure.set_dpi(dpi)
        self.figure.set_size_inches(width / dpi, height / dpi, forward=False)
        self.canvas.draw_idle()

    def shutdown(self) -> None:
        self._cancel_playback_operation()
        self.stop_preview(False)
        self._close_device_details()
        if self.session is not None:
            self.session.close()
            self.session = None
