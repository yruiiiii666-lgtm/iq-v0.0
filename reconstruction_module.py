from __future__ import annotations

import csv
import math
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from scene_catalog import SCENE_TYPES, IQLocationLink, list_linked_iq_details, list_scene_locations
from signal_reconstruction import (
    LocationSpectrumIQSummary,
    ReconstructionResult,
    SceneIQRepresentative,
    SpectrumIQCorrespondence,
    TypicalSignal,
    TypicalSignalResult,
    analyze_typical_signals,
    aggregate_spectrum_iq_correspondence,
    build_spectrum_iq_correspondence,
    reconstruct_complex_scene,
    reconstruct_hybrid_scene,
    reconstruct_measured_signal,
    reconstruct_multi_system,
    reconstruct_single_modulated,
    reconstruction_explanation,
    reconstruction_summary,
    save_reconstruction,
    export_scene_iq_representative_cache,
    load_scene_iq_representative_cache,
    REPRESENTATIVE_DEFAULTS,
    save_scene_iq_representative_cache,
    select_scene_iq_representatives,
)
from spectrum_feature_library import ALL_BAND, FEATURE_BANDS
from iq_reader import recording_from_paths
from runtime_paths import application_dir


RECONSTRUCTION_MODES = (
    "实际采集典型场景信号重构",
    "单频点调制信号重构",
    "多无线电系统合成信号重构",
    "复杂场景多信号组合重构",
)

FIELD_LEVELS = ("6", "10", "12", "15", "20", "30", "60", "100", "120", "140")

SCENE_FIELD_DEFAULTS = {
    "大型停车场": "6",
    "工业区": "10", "购物广场": "10", "地铁站": "10", "基站": "10",
    "广播电视塔": "12",
    "居民区": "15", "高密度居民区": "15", "机场区域": "15", "医院外部": "15",
    "闹市区": "20", "发电站": "20", "智能网联示范区": "20",
    "短波电台": "120",
}

SINGLE_SIGNAL_PRESETS = {
    "中短波广播（AM）": ("AM", "1", "2", "1000", "0.8", "10000"),
    "调频广播（FM）": ("FM", "100", "5", "1000", "5", "10000"),
    "RKE/PEPS（ASK）": ("ASK", "433.92", "2", "1000", "0.9", "2000"),
    "RKE/PEPS（FSK）": ("FSK", "433.92", "2", "1000", "1", "2000"),
    "数字电视（16QAM）": ("16QAM", "666", "10", "1000", "0.5", "5000000"),
    "移动通信（QPSK）": ("QPSK", "1850", "40", "1000", "0.5", "1000000"),
    "卫星导航（BPSK）": ("BPSK", "1575.42", "10", "1000", "0.5", "1023000"),
    "蓝牙（FSK）": ("FSK", "2441", "10", "1000", "0.5", "1000000"),
    "WLAN（QPSK）": ("QPSK", "2437", "40", "1000", "0.5", "10000000"),
}

MULTI_SYSTEM_PRESETS = {
    "RKE/PEPS组合": "RKE-ASK,ASK,-4,0,2000\nPEPS-FSK,FSK,4,-3,2000",
    "广播组合": "调频广播1,FM,-8,0,1000\n调频广播2,FM,0,-3,1000\n数字音频,QPSK,8,-6,1000000",
    "移动通信组合": "移动通信1,QPSK,-10,0,1000000\n移动通信2,16QAM,0,-3,2000000\n移动通信3,QPSK,10,-6,1000000",
    "车载无线综合": "RKE,ASK,-12,0,2000\nPEPS,FSK,-6,-3,2000\n卫星导航,BPSK,0,-6,1023000\n蓝牙,FSK,7,-6,1000000\nWLAN,QPSK,13,-9,5000000",
}

COMPLEX_SCENE_TEMPLATES = {
    "实采背景+窄带干扰": "窄带连续波,CW,0,-6,1000\n脉冲干扰,脉冲,8,-9,1000",
    "实采背景+RKE/PEPS": "RKE,ASK,-6,-3,2000\nPEPS,FSK,6,-6,2000",
    "实采背景+移动通信": "邻道QPSK,QPSK,-10,-6,1000000\n邻道16QAM,16QAM,10,-9,2000000",
    "实采背景+多系统": "窄带FM,FM,-12,-6,1000\nRKE,ASK,-6,-3,2000\n移动通信,QPSK,6,-9,1000000\n脉冲干扰,脉冲,12,-12,1000",
}


class ReconstructionModule(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        database_var: tk.StringVar,
        output_var: tk.StringVar,
        status_var: tk.StringVar,
        iq_root_var: tk.StringVar | None = None,
        on_result_ready: Callable[[ReconstructionResult], None] | None = None,
    ) -> None:
        super().__init__(parent, padding=8)
        self.database_var = database_var
        self.output_var = output_var
        self.status_var = status_var
        self.iq_root_var = iq_root_var
        self.on_result_ready = on_result_ready
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.current_typical_result: TypicalSignalResult | None = None
        self.current_correspondence: tuple[LocationSpectrumIQSummary, ...] = ()
        self.current_representatives: tuple[SceneIQRepresentative, ...] = ()
        self.representative_by_item: dict[str, SceneIQRepresentative] = {}
        self.complex_base_representative: SceneIQRepresentative | None = None
        self.typical_by_item: dict[str, TypicalSignal] = {}
        self.current_result: ReconstructionResult | None = None
        self.current_links: dict[str, IQLocationLink] = {}
        self.preview_canvas: FigureCanvasTkAgg | None = None
        self.preview_toolbar: NavigationToolbar2Tk | None = None
        self.comparison_canvas: FigureCanvasTkAgg | None = None
        self.comparison_toolbar: NavigationToolbar2Tk | None = None
        self.comparison_figure: Figure | None = None
        self.preview_play_after_id: str | None = None
        self.preview_playing = False
        self.preview_position_ms = tk.DoubleVar(value=0.0)
        self.preview_window_ms = tk.StringVar(value="1")
        self.preview_play_status = tk.StringVar(value="生成后可按1 ms窗口播放查看I/Q变化（仅为可视化预览）。")
        self.comparison_status_var = tk.StringVar(value="生成实测或复杂场景重构后显示原始采集与重构结果。")

        self.scene_var = tk.StringVar(value="工业区")
        self.correspondence_scene_var = tk.StringVar(value="全部")
        self.correspondence_guard_var = tk.StringVar(value="1")
        self.representative_scene_var = tk.StringVar(value="工业区")
        self.polarization_var = tk.StringVar(value="垂直极化")
        self.band_var = tk.StringVar(value=ALL_BAND)
        self.tolerance_var = tk.StringVar(value="2")
        self.minimum_probability_var = tk.StringVar(value="0.3")
        self.mode_var = tk.StringVar(value=RECONSTRUCTION_MODES[0])
        self.mode_explanation_var = tk.StringVar(value=reconstruction_explanation(self.mode_var.get()))
        self.name_var = tk.StringVar(value="场景重构信号")
        self.center_var = tk.StringVar(value="95")
        self.sample_rate_var = tk.StringVar(value="40")
        self.duration_var = tk.StringVar(value="1000")
        self.single_duration_var = tk.StringVar(value="100")
        self.multi_duration_var = tk.StringVar(value="100")
        self.complex_duration_var = tk.StringVar(value="1000")
        self.seed_var = tk.StringVar(value="2026")

        self.location_var = tk.StringVar()
        self.recording_var = tk.StringVar()
        self.measured_start_var = tk.StringVar(value="0")
        self.measured_source_duration_var = tk.StringVar(value="100")
        self.measured_crossfade_var = tk.StringVar(value="2")
        self.measured_field_var = tk.StringVar(value="10")
        self.measured_selection_text = tk.StringVar(value="尚未选择场景代表IQ，请返回“信号选择”页面选择。")
        self.location_combo: ttk.Widget | None = None
        self.recording_combo: ttk.Widget | None = None

        self.modulation_var = tk.StringVar(value="FM")
        self.single_preset_var = tk.StringVar(value="调频广播（FM）")
        self.single_field_var = tk.StringVar(value="30")
        self.offset_var = tk.StringVar(value="0")
        self.level_var = tk.StringVar(value="0")
        self.modulation_frequency_var = tk.StringVar(value="1000")
        self.modulation_index_var = tk.StringVar(value="2")
        self.symbol_rate_var = tk.StringVar(value="10000")
        self.measured_first_var = tk.BooleanVar(value=True)
        self.multi_preset_var = tk.StringVar(value="车载无线综合")
        self.multi_field_var = tk.StringVar(value="30")
        self.complex_template_var = tk.StringVar(value="实采背景+多系统")
        self.complex_field_var = tk.StringVar(value="30")
        self.complex_base_text = tk.StringVar(value="尚未选择实采典型场景IQ")

        self.progress_text = tk.StringVar(value="等待操作")
        self._build_ui()
        self.refresh_catalog()
        self._load_saved_correspondence()
        self.after_idle(self._load_selected_scene_cache)
        self.after(100, self._poll_messages)

    def _build_ui(self) -> None:
        self.workflow_notebook = ttk.Notebook(self)
        self.workflow_notebook.pack(fill=tk.BOTH, expand=True)
        self.selection_page = ttk.Frame(self.workflow_notebook)
        self.workspace_page = ttk.Frame(self.workflow_notebook)
        self.workflow_notebook.add(self.selection_page, text="信号选择")
        self.workflow_notebook.add(self.workspace_page, text="重构工作台")

        selection_main = ttk.PanedWindow(self.selection_page, orient=tk.HORIZONTAL)
        selection_main.pack(fill=tk.BOTH, expand=True)
        selection_left = ttk.Frame(selection_main, style="Panel.TFrame", width=390)
        selection_left.pack_propagate(False)
        selection_right = ttk.Frame(selection_main, style="Panel.TFrame", padding=10)
        selection_main.add(selection_left, weight=0)
        selection_main.add(selection_right, weight=1)
        self._build_selection_controls(self._scrollable_controls(selection_left))
        self._build_selection_results(selection_right)
        self.selection_control_notebook.bind(
            "<<NotebookTabChanged>>", self._sync_selection_result_tab
        )
        self.selection_control_notebook.select(0)
        self.notebook.select(self.representative_tab)

        workspace_main = ttk.PanedWindow(self.workspace_page, orient=tk.HORIZONTAL)
        workspace_main.pack(fill=tk.BOTH, expand=True)
        workspace_left = ttk.Frame(workspace_main, style="Panel.TFrame", width=420)
        workspace_left.pack_propagate(False)
        workspace_right = ttk.Frame(workspace_main, style="Panel.TFrame", padding=10)
        workspace_main.add(workspace_left, weight=0)
        workspace_main.add(workspace_right, weight=1)
        self._build_reconstruction_controls(self._scrollable_controls(workspace_left))
        self._build_reconstruction_results(workspace_right)

    def _scrollable_controls(self, parent: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(parent, bg="#ffffff", highlightthickness=0, width=380)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        controls = ttk.Frame(canvas, style="Panel.TFrame", padding=14)
        window_id = canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        def on_wheel(event: tk.Event) -> str | None:
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

        self.bind_all("<MouseWheel>", on_wheel, add="+")
        return controls

    @staticmethod
    def _row(parent: ttk.Frame, label: str, variable: tk.StringVar, values: tuple[str, ...] | None = None) -> ttk.Widget:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
        if values is None:
            widget: ttk.Widget = ttk.Entry(row, textvariable=variable)
        else:
            widget = ttk.Combobox(row, textvariable=variable, values=values, state="readonly")
        widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return widget

    def _build_selection_controls(self, parent: ttk.Frame) -> None:
        self.selection_control_notebook = ttk.Notebook(parent)
        self.selection_control_notebook.pack(fill=tk.X, pady=(0, 10))
        representative_controls = ttk.Frame(self.selection_control_notebook, style="Panel.TFrame", padding=8)
        correspondence_controls = ttk.Frame(self.selection_control_notebook, style="Panel.TFrame", padding=8)
        screening_controls = ttk.Frame(self.selection_control_notebook, style="Panel.TFrame", padding=8)
        self.selection_control_notebook.add(representative_controls, text="代表频段/最强片段")
        self.selection_control_notebook.add(correspondence_controls, text="频谱-IQ关联")
        self.selection_control_notebook.add(screening_controls, text="典型频谱信号")

        correspondence = ttk.LabelFrame(correspondence_controls, text="频谱与IQ频点对应", style="Card.TLabelframe", padding=10)
        correspondence.pack(fill=tk.X, pady=(0, 10))
        scene_values = ("全部",) + tuple(item for item in SCENE_TYPES if item not in {"全部", "未分类"})
        self._row(correspondence, "场景类型", self.correspondence_scene_var, scene_values)
        self._row(correspondence, "带宽保护量(MHz)", self.correspondence_guard_var)
        self.correspondence_button = ttk.Button(
            correspondence,
            text="生成全地点频谱-IQ对应表",
            style="Accent.TButton",
            command=self.run_correspondence,
        )
        self.correspondence_button.pack(fill=tk.X, pady=(7, 3))
        self.correspondence_export_button = ttk.Button(
            correspondence,
            text="导出频谱-IQ对应表",
            command=self.export_correspondence,
            state=tk.DISABLED,
        )
        self.correspondence_export_button.pack(fill=tk.X, pady=3)
        ttk.Label(
            correspondence,
            text="每个采集地点只占一行；多个频谱峰值和IQ频点在单元格内按逗号分隔。生成后自动保存CSV。",
            foreground="#64748b",
            wraplength=325,
        ).pack(anchor="w", pady=(5, 0))

        representative = ttk.LabelFrame(
            representative_controls, text="代表频段与最强实测片段", style="Card.TLabelframe", padding=10
        )
        representative.pack(fill=tk.X, pady=(0, 10))
        representative_scenes = tuple(item for item in SCENE_TYPES if item not in {"全部", "未分类"})
        self.representative_scene_combo = self._row(
            representative, "场景类型", self.representative_scene_var, representative_scenes
        )
        self.representative_scene_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._load_selected_scene_cache()
        )
        self.representative_button = ttk.Button(
            representative,
            text="加载场景结果（无缓存则分析）",
            style="Accent.TButton",
            command=self.run_representative_selection,
        )
        self.representative_button.pack(fill=tk.X, pady=(7, 3))
        ttk.Button(
            representative,
            text="重新分析并更新缓存",
            command=lambda: self.run_representative_selection(force_refresh=True),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            representative, text="将选中片段用于实测重构", command=self.use_selected_representative
        ).pack(fill=tk.X, pady=3)
        self.representative_export_button = None
        ttk.Label(
            representative,
            text=(
                "固定规则：10 ms检测窗、1 s真实IQ片段、至少2个地点证据、最多5个频段。"
                "首次分析后保存到数据库并自动更新Excel，之后直接读取。"
            ),
            foreground="#64748b",
            wraplength=325,
        ).pack(anchor="w", pady=(5, 0))

        screening = ttk.LabelFrame(screening_controls, text="典型信号筛选", style="Card.TLabelframe", padding=10)
        screening.pack(fill=tk.X, pady=(0, 10))
        self._row(screening, "场景类型", self.scene_var, tuple(item for item in SCENE_TYPES if item not in {"全部", "未分类"}))
        self._row(screening, "极化方式", self.polarization_var, ("垂直极化", "水平极化"))
        self._row(screening, "频段", self.band_var, tuple(reversed(FEATURE_BANDS)))
        self._row(screening, "频率容差(MHz)", self.tolerance_var)
        self._row(screening, "最低出现概率", self.minimum_probability_var)
        self.screen_button = ttk.Button(screening, text="生成跨场景典型信号", style="Accent.TButton", command=self.run_typical_screening)
        self.screen_button.pack(fill=tk.X, pady=(7, 3))
        ttk.Button(screening, text="选择当前带宽内推荐信号", command=self.select_recommended_signals).pack(fill=tk.X, pady=3)
        ttk.Button(screening, text="导出典型信号与IQ候选", command=self.export_typical_signals).pack(fill=tk.X, pady=3)

        navigation = ttk.LabelFrame(parent, text="进入重构", style="Card.TLabelframe", padding=10)
        navigation.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(
            navigation, text="打开重构工作台", style="Accent.TButton", command=self.show_reconstruction_workspace
        ).pack(fill=tk.X)
        self.selection_progress = ttk.Progressbar(navigation, mode="indeterminate")
        self.selection_progress.pack(fill=tk.X, pady=(8, 3))
        ttk.Label(navigation, textvariable=self.progress_text, style="Subtle.TLabel", wraplength=325).pack(anchor="w")

    def _build_reconstruction_controls(self, parent: ttk.Frame) -> None:
        ttk.Button(parent, text="返回信号选择", command=self.show_signal_selection).pack(fill=tk.X, pady=(0, 10))
        common = ttk.LabelFrame(parent, text="重构任务", style="Card.TLabelframe", padding=10)
        common.pack(fill=tk.X, pady=(0, 10))
        self._row(common, "重构方式", self.mode_var, RECONSTRUCTION_MODES)
        self._row(common, "输出名称", self.name_var)
        ttk.Label(
            common,
            textvariable=self.mode_explanation_var,
            foreground="#475569",
            wraplength=325,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(7, 0))
        self.mode_var.trace_add("write", lambda *_args: self._on_reconstruction_mode_changed())

        self.mode_host = ttk.Frame(parent, style="Panel.TFrame")
        self.mode_host.pack(fill=tk.X)
        self.mode_frames: dict[str, ttk.LabelFrame] = {}
        self._build_measured_controls()
        self._build_single_controls()
        self._build_multi_controls()
        self._build_complex_controls()
        self._show_mode_controls()

        run_frame = ttk.LabelFrame(parent, text="生成与导出", style="Card.TLabelframe", padding=10)
        run_frame.pack(fill=tk.X, pady=(10, 0))
        self.generate_button = ttk.Button(run_frame, text="生成并预览重构信号", style="Accent.TButton", command=self.run_reconstruction)
        self.generate_button.pack(fill=tk.X, pady=3)
        self.export_button = ttk.Button(run_frame, text="导出重构波形和参数", command=self.export_reconstruction, state=tk.DISABLED)
        self.export_button.pack(fill=tk.X, pady=3)
        self.progress = ttk.Progressbar(run_frame, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(8, 3))
        ttk.Label(run_frame, textvariable=self.progress_text, style="Subtle.TLabel", wraplength=325).pack(anchor="w")
        ttk.Label(
            run_frame,
            text="导出格式为complex64 NPY、小端float32交织IQ、JSON参数及预览图；仪器专用格式需根据最终信号源补充。",
            foreground="#64748b",
            wraplength=325,
        ).pack(anchor="w", pady=(8, 0))

    def _new_mode_frame(self, mode: str, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(self.mode_host, text=title, style="Card.TLabelframe", padding=10)
        self.mode_frames[mode] = frame
        return frame

    def _build_measured_controls(self) -> None:
        mode = RECONSTRUCTION_MODES[0]
        frame = self._new_mode_frame(mode, "已选代表频段、最强实测片段与回放参数")
        ttk.Label(
            frame,
            textvariable=self.measured_selection_text,
            foreground="#334155",
            wraplength=325,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(0, 8))
        ttk.Separator(frame).pack(fill=tk.X, pady=(0, 7))
        self._row(frame, "片段起点(s)", self.measured_start_var)
        self._row(frame, "片段长度(ms)", self.measured_source_duration_var)
        self._row(frame, "目标回放时长(ms)", self.duration_var)
        self._row(frame, "循环平滑(ms)", self.measured_crossfade_var)
        self._row(frame, "目标场强(V/m)", self.measured_field_var, FIELD_LEVELS)
        ttk.Label(
            frame,
            text=(
                "片段起点和长度来自整条IQ扫描。目标时长大于入选片段时自动进行交叉渐变循环；"
                "循环发生在完整入选片段结束后。"
            ),
            foreground="#64748b",
            wraplength=325,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(7, 0))

    def _build_single_controls(self) -> None:
        mode = RECONSTRUCTION_MODES[1]
        frame = self._new_mode_frame(mode, "单频点调制参数")
        self._row(frame, "中心频率(MHz)", self.center_var)
        self._row(frame, "采样率(MS/s)", self.sample_rate_var)
        self._row(frame, "信号时长(ms)", self.single_duration_var)
        self._row(frame, "随机种子", self.seed_var)
        self._row(frame, "标准系统模板", self.single_preset_var, tuple(SINGLE_SIGNAL_PRESETS))
        ttk.Button(frame, text="载入模板参数", command=self.apply_single_preset).pack(fill=tk.X, pady=(2, 5))
        self._row(frame, "调制类型", self.modulation_var, ("CW", "AM", "FM", "ASK", "FSK", "BPSK", "QPSK", "16QAM", "脉冲", "噪声"))
        self._row(frame, "相对频偏(MHz)", self.offset_var)
        self._row(frame, "相对电平(dB)", self.level_var)
        self._row(frame, "调制频率(Hz)", self.modulation_frequency_var)
        self._row(frame, "调制指数/占空比", self.modulation_index_var)
        self._row(frame, "符号率(Bd)", self.symbol_rate_var)
        self._row(frame, "目标场强(V/m)", self.single_field_var, ("30", "60", "100", "140"))

    def _build_multi_controls(self) -> None:
        mode = RECONSTRUCTION_MODES[2]
        frame = self._new_mode_frame(mode, "多无线电系统定义")
        self._row(frame, "中心频率(MHz)", self.center_var)
        self._row(frame, "采样率(MS/s)", self.sample_rate_var)
        self._row(frame, "信号时长(ms)", self.multi_duration_var)
        self._row(frame, "随机种子", self.seed_var)
        self._row(frame, "组合模板", self.multi_preset_var, tuple(MULTI_SYSTEM_PRESETS))
        ttk.Button(frame, text="载入组合模板", command=self.apply_multi_preset).pack(fill=tk.X, pady=(2, 5))
        self._row(frame, "目标总场强(V/m)", self.multi_field_var, ("30", "60", "100", "140"))
        ttk.Label(frame, text="每行：名称,调制,频偏MHz,相对电平dB,调制频率或符号率", wraplength=325).pack(anchor="w")
        self.component_text = tk.Text(frame, height=8, wrap="none", font=("Consolas", 9))
        self.component_text.pack(fill=tk.X, pady=(6, 0))
        self.component_text.insert(
            "1.0",
            MULTI_SYSTEM_PRESETS[self.multi_preset_var.get()],
        )

    def _build_complex_controls(self) -> None:
        mode = RECONSTRUCTION_MODES[3]
        frame = self._new_mode_frame(mode, "复杂场景组合")
        ttk.Label(frame, textvariable=self.complex_base_text, foreground="#334155", wraplength=325).pack(anchor="w")
        ttk.Button(
            frame, text="使用代表频段最强片段作为背景", command=self.use_selected_representative_for_complex
        ).pack(fill=tk.X, pady=(5, 7))
        self._row(frame, "中心频率(MHz)", self.center_var)
        self._row(frame, "采样率(MS/s)", self.sample_rate_var)
        self._row(frame, "信号时长(ms)", self.complex_duration_var)
        self._row(frame, "随机种子", self.seed_var)
        self._row(frame, "组合模板", self.complex_template_var, tuple(COMPLEX_SCENE_TEMPLATES))
        ttk.Button(frame, text="载入复杂场景模板", command=self.apply_complex_template).pack(fill=tk.X, pady=(2, 5))
        self._row(frame, "目标总场强(V/m)", self.complex_field_var, ("30", "60", "100", "120", "140"))
        ttk.Label(frame, text="叠加信号：名称,调制,频偏MHz,相对电平dB,调制频率或符号率", wraplength=325).pack(anchor="w")
        self.complex_component_text = tk.Text(frame, height=7, wrap="none", font=("Consolas", 9))
        self.complex_component_text.pack(fill=tk.X, pady=(6, 0))
        self.complex_component_text.insert("1.0", COMPLEX_SCENE_TEMPLATES[self.complex_template_var.get()])
        ttk.Label(
            frame,
            text="当前生成一个回放通道；叠加频偏必须位于采样带宽内，跨频段场景应分别生成后由多路信号源同步合路。",
            foreground="#64748b",
            wraplength=325,
        ).pack(anchor="w", pady=(6, 0))

    def _show_mode_controls(self) -> None:
        for frame in self.mode_frames.values():
            frame.pack_forget()
        frame = self.mode_frames.get(self.mode_var.get())
        if frame is not None:
            frame.pack(fill=tk.X)

    def _on_reconstruction_mode_changed(self) -> None:
        self.mode_explanation_var.set(reconstruction_explanation(self.mode_var.get()))
        self._show_mode_controls()

    def show_signal_selection(self) -> None:
        self.workflow_notebook.select(self.selection_page)

    def show_reconstruction_workspace(self) -> None:
        self._show_mode_controls()
        self.workflow_notebook.select(self.workspace_page)

    def _sync_selection_result_tab(self, _event: tk.Event | None = None) -> None:
        """Keep the control category and its result table on the same subject."""
        if not hasattr(self, "notebook"):
            return
        result_tabs = (
            self.representative_tab,
            self.correspondence_tab,
            self.typical_tab,
        )
        index = self.selection_control_notebook.index(self.selection_control_notebook.select())
        self.notebook.select(result_tabs[index])

    def apply_single_preset(self) -> None:
        preset = SINGLE_SIGNAL_PRESETS.get(self.single_preset_var.get())
        if preset is None:
            return
        modulation, center, sample_rate, modulation_frequency, modulation_index, symbol_rate = preset
        self.modulation_var.set(modulation)
        self.center_var.set(center)
        self.sample_rate_var.set(sample_rate)
        self.modulation_frequency_var.set(modulation_frequency)
        self.modulation_index_var.set(modulation_index)
        self.symbol_rate_var.set(symbol_rate)
        self.offset_var.set("0")
        self.level_var.set("0")
        self.name_var.set(self.single_preset_var.get().replace("（", "_").replace("）", ""))

    def apply_multi_preset(self) -> None:
        definition = MULTI_SYSTEM_PRESETS.get(self.multi_preset_var.get())
        if definition is None:
            return
        self.component_text.delete("1.0", tk.END)
        self.component_text.insert("1.0", definition)
        self.name_var.set(self.multi_preset_var.get())

    def apply_complex_template(self) -> None:
        definition = COMPLEX_SCENE_TEMPLATES.get(self.complex_template_var.get())
        if definition is None:
            return
        self.complex_component_text.delete("1.0", tk.END)
        self.complex_component_text.insert("1.0", definition)
        self.name_var.set(self.complex_template_var.get())

    def use_selected_representative_for_complex(self) -> None:
        row = self._selected_representative()
        if row is None:
            messagebox.showinfo("尚未选择", "请先在“代表频段/最强片段”表中选择一行。")
            return
        self.complex_base_representative = row
        self.complex_base_text.set(f"实采背景：{row.scene_type}｜{row.point}｜{row.recording_stem}")
        self.center_var.set(f"{row.center_frequency_mhz:g}")
        self.sample_rate_var.set(f"{row.sample_rate_hz / 1e6:g}")
        self.measured_start_var.set(f"{row.selected_start_s:.6f}")
        self.measured_source_duration_var.set(f"{row.selected_duration_s * 1e3:.6f}")
        self.complex_duration_var.set("1000")
        self.name_var.set(f"{row.scene_type}_{row.representative_frequency_mhz:g}MHz_复杂场景")

    def _build_selection_results(self, parent: ttk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.correspondence_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.correspondence_tab, text="频谱-IQ对应表")
        columns = (
            "serial", "city", "point", "scene", "spectrum_frequencies", "levels", "bandwidths",
            "iq_centers", "recordings", "file_statuses", "summary",
        )
        self.correspondence_tree = ttk.Treeview(
            self.correspondence_tab,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Peak.Treeview",
        )
        headings = {
            "serial": "序号", "city": "城市", "point": "采集地点", "scene": "场景类型",
            "spectrum_frequencies": "频谱峰值频率(MHz)", "levels": "峰值功率(dBμV/m)",
            "bandwidths": "BW_3dB(MHz)", "iq_centers": "IQ中心频率(MHz)",
            "recordings": "IQ数据组名", "file_statuses": "IQ文件状态", "summary": "汇总说明",
        }
        widths = {
            "serial": 55, "city": 90, "point": 180, "scene": 100,
            "spectrum_frequencies": 360, "levels": 300, "bandwidths": 300, "iq_centers": 300,
            "recordings": 500, "file_statuses": 220, "summary": 220,
        }
        for column in columns:
            self.correspondence_tree.heading(column, text=headings[column])
            self.correspondence_tree.column(column, width=widths[column], anchor="center", stretch=False)
        correspondence_ybar = ttk.Scrollbar(
            self.correspondence_tab, orient=tk.VERTICAL, command=self.correspondence_tree.yview
        )
        correspondence_xbar = ttk.Scrollbar(
            self.correspondence_tab, orient=tk.HORIZONTAL, command=self.correspondence_tree.xview
        )
        self.correspondence_tree.configure(
            yscrollcommand=correspondence_ybar.set, xscrollcommand=correspondence_xbar.set
        )
        self.correspondence_tree.grid(row=0, column=0, sticky="nsew")
        correspondence_ybar.grid(row=0, column=1, sticky="ns")
        correspondence_xbar.grid(row=1, column=0, sticky="ew")
        self.correspondence_details = tk.Text(
            self.correspondence_tab,
            height=7,
            wrap="word",
            bg="#ffffff",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
        )
        self.correspondence_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.correspondence_details.insert(
            "1.0",
            "点击左侧“生成全地点频谱-IQ对应表”。一个地点只显示一行，多个频率按逗号排列。",
        )
        self.correspondence_details.configure(state=tk.DISABLED)
        self.correspondence_tab.columnconfigure(0, weight=1)
        self.correspondence_tab.rowconfigure(0, weight=1)

        self.representative_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.representative_tab, text="代表频段/最强片段")
        representative_columns = (
            "rank", "frequency_group", "typical_frequency", "coverage", "score", "spectrum_support",
            "center", "city", "point", "recording", "power", "relative", "candidates",
            "start", "end", "duration", "recording_duration", "window", "scan_windows", "sample_rate",
        )
        self.representative_tree = ttk.Treeview(
            self.representative_tab,
            columns=representative_columns,
            show="headings",
            selectmode="browse",
            style="Peak.Treeview",
        )
        representative_headings = {
            "rank": "排序", "frequency_group": "IQ中心频段(MHz)",
            "typical_frequency": "频谱参考频率(MHz)", "coverage": "地点覆盖",
            "score": "代表得分", "spectrum_support": "频谱支持",
            "center": "选中IQ中心(MHz)",
            "city": "城市", "point": "采集地点", "recording": "IQ数据组",
            "power": "估算功率(dBm)", "relative": "相对功率(dBFS)", "candidates": "候选数量",
            "start": "片段起点(s)", "end": "片段终点(s)", "duration": "实际回放片段(s)",
            "recording_duration": "原记录时长(s)", "window": "检测窗口(ms)",
            "scan_windows": "扫描窗口数", "sample_rate": "采样率(MS/s)",
        }
        representative_widths = {
            "rank": 55, "frequency_group": 145, "typical_frequency": 150, "coverage": 95,
            "score": 90, "spectrum_support": 95, "center": 130, "city": 90, "point": 180,
            "recording": 310, "power": 115, "relative": 115, "candidates": 80,
            "start": 105, "end": 105, "duration": 105, "recording_duration": 110,
            "window": 105, "scan_windows": 100, "sample_rate": 105,
        }
        for column in representative_columns:
            self.representative_tree.heading(column, text=representative_headings[column])
            self.representative_tree.column(column, width=representative_widths[column], anchor="center", stretch=False)
        representative_ybar = ttk.Scrollbar(
            self.representative_tab, orient=tk.VERTICAL, command=self.representative_tree.yview
        )
        representative_xbar = ttk.Scrollbar(
            self.representative_tab, orient=tk.HORIZONTAL, command=self.representative_tree.xview
        )
        self.representative_tree.configure(
            yscrollcommand=representative_ybar.set, xscrollcommand=representative_xbar.set
        )
        self.representative_tree.grid(row=0, column=0, sticky="nsew")
        representative_ybar.grid(row=0, column=1, sticky="ns")
        representative_xbar.grid(row=1, column=0, sticky="ew")
        self.representative_details = tk.Text(
            self.representative_tab, height=5, wrap="word", bg="#ffffff",
            relief="solid", borderwidth=1, padx=10, pady=8,
        )
        self.representative_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.representative_details.insert(
            "1.0", "选择场景后，点击左侧“分析代表频段并扫描最强片段”。"
        )
        self.representative_details.configure(state=tk.DISABLED)
        representative_actions = ttk.Frame(self.representative_tab)
        representative_actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(
            representative_actions,
            text="使用选中最强片段进入实测重构",
            style="Accent.TButton",
            command=self.use_selected_representative,
        ).pack(side=tk.LEFT)
        ttk.Label(
            representative_actions,
            text="也可以双击表格中的代表频段。",
            style="Subtle.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0))
        self.representative_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_representative_details())
        self.representative_tree.bind("<Double-1>", lambda _event: self.use_selected_representative())
        self.representative_tab.columnconfigure(0, weight=1)
        self.representative_tab.rowconfigure(0, weight=1)

        typical_tab = ttk.Frame(self.notebook)
        self.typical_tab = typical_tab
        self.notebook.add(typical_tab, text="典型信号筛选")
        columns = ("rank", "category", "frequency", "scene_p", "global_p", "specificity", "contrast", "bandwidth", "iq", "score")
        self.typical_tree = ttk.Treeview(typical_tab, columns=columns, show="headings", selectmode="extended", style="Peak.Treeview")
        headings = {
            "rank": "排序", "category": "类别", "frequency": "典型频率(MHz)",
            "scene_p": "场景概率", "global_p": "全局概率", "specificity": "特异性",
            "contrast": "场强增量(dB)", "bandwidth": "平均BW_3dB(MHz)",
            "iq": "IQ候选", "score": "综合得分",
        }
        widths = {"rank": 55, "category": 90, "frequency": 125, "scene_p": 90, "global_p": 90,
                  "specificity": 85, "contrast": 110, "bandwidth": 140, "iq": 75, "score": 90}
        for column in columns:
            self.typical_tree.heading(column, text=headings[column])
            self.typical_tree.column(column, width=widths[column], anchor="center", stretch=True)
        ybar = ttk.Scrollbar(typical_tab, orient=tk.VERTICAL, command=self.typical_tree.yview)
        xbar = ttk.Scrollbar(typical_tab, orient=tk.HORIZONTAL, command=self.typical_tree.xview)
        self.typical_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.typical_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.typical_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_typical_details())
        self.typical_details = tk.Text(typical_tab, height=9, wrap="word", bg="#ffffff", relief="solid", borderwidth=1, padx=10, pady=8)
        self.typical_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.typical_details.insert("1.0", "点击“生成跨场景典型信号”，结果将区分公共背景、场景增强、场景特有和场景常见信号。")
        self.typical_details.configure(state=tk.DISABLED)
        typical_tab.columnconfigure(0, weight=1)
        typical_tab.rowconfigure(0, weight=1)

    def _build_reconstruction_results(self, parent: ttk.Frame) -> None:
        self.reconstruction_notebook = ttk.Notebook(parent)
        self.reconstruction_notebook.pack(fill=tk.BOTH, expand=True)

        summary_tab = ttk.Frame(self.reconstruction_notebook)
        self.reconstruction_notebook.add(summary_tab, text="重构摘要")
        self.summary_text = tk.Text(summary_tab, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11))
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        self.summary_text.insert("1.0", "请配置参数并生成重构信号。")
        self.summary_text.configure(state=tk.DISABLED)

        self.preview_tab = ttk.Frame(self.reconstruction_notebook)
        self.reconstruction_notebook.add(self.preview_tab, text="重构预览")
        preview_controls = ttk.Frame(self.preview_tab, padding=(10, 8, 10, 4))
        preview_controls.pack(fill=tk.X)
        self.preview_play_button = ttk.Button(
            preview_controls, text="播放I/Q窗口", command=self._toggle_preview_playback, state=tk.DISABLED
        )
        self.preview_play_button.pack(side=tk.LEFT)
        ttk.Button(preview_controls, text="停止", command=self._stop_preview_playback).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(preview_controls, text="窗口(ms)").pack(side=tk.LEFT)
        preview_window_combo = ttk.Combobox(
            preview_controls,
            textvariable=self.preview_window_ms,
            values=("0.1", "0.5", "1", "2", "5"),
            state="readonly",
            width=6,
        )
        preview_window_combo.pack(side=tk.LEFT, padx=(5, 10))
        preview_window_combo.bind("<<ComboboxSelected>>", lambda _event: self._draw_preview_iq_window())
        ttk.Label(preview_controls, textvariable=self.preview_play_status, style="Subtle.TLabel").pack(side=tk.LEFT)
        self.preview_seek = ttk.Scale(
            self.preview_tab,
            from_=0.0,
            to=1.0,
            variable=self.preview_position_ms,
            command=lambda _value: self._draw_preview_iq_window() if not self.preview_playing else None,
        )
        self.preview_seek.pack(fill=tk.X, padx=12, pady=(0, 3))
        self.preview_host = ttk.Frame(self.preview_tab)
        self.preview_host.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self.preview_host, text="生成后显示I/Q波形、相对频谱和时频图。", style="Subtle.TLabel").pack(pady=30)

        self.comparison_tab = ttk.Frame(self.reconstruction_notebook)
        self.reconstruction_notebook.add(self.comparison_tab, text="原始/重构对比")
        comparison_controls = ttk.Frame(self.comparison_tab, padding=(10, 8, 10, 4))
        comparison_controls.pack(fill=tk.X)
        comparison_control_row = ttk.Frame(comparison_controls)
        comparison_control_row.pack(fill=tk.X)
        ttk.Label(comparison_control_row, text="对比窗口(ms)").pack(side=tk.LEFT)
        self.comparison_window_combo = ttk.Combobox(
            comparison_control_row,
            textvariable=self.preview_window_ms,
            values=("0.1", "0.5", "1", "2", "5"),
            state=tk.DISABLED,
            width=6,
        )
        self.comparison_window_combo.pack(side=tk.LEFT, padx=(5, 12))
        self.comparison_window_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._draw_preview_iq_window()
        )
        ttk.Label(
            comparison_controls,
            textvariable=self.comparison_status_var,
            style="Subtle.TLabel",
            wraplength=500,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(5, 0))
        self.comparison_seek = ttk.Scale(
            self.comparison_tab,
            from_=0.0,
            to=1.0,
            variable=self.preview_position_ms,
            command=lambda _value: self._draw_preview_iq_window() if not self.preview_playing else None,
            state=tk.DISABLED,
        )
        self.comparison_seek.pack(fill=tk.X, padx=12, pady=(0, 3))
        self.comparison_host = ttk.Frame(self.comparison_tab)
        self.comparison_host.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            self.comparison_host,
            text="实测重构会显示原始采集片段；纯参数合成模式没有对应的原始采集信号。",
            style="Subtle.TLabel",
        ).pack(pady=30)

    def refresh_catalog(self) -> None:
        database = Path(self.database_var.get())
        locations = list_scene_locations(database) if database.exists() else []
        labels = tuple(f"{item.city}/{item.point}" for item in locations)
        if self.location_combo is not None:
            self.location_combo.configure(values=labels)
        if labels and self.location_var.get() not in labels:
            # The measured reconstruction is intentionally left unselected until
            # a representative IQ is chosen on the preceding workflow page.
            self.current_links.clear()
            self.recording_var.set("")
        elif self.location_var.get():
            self._refresh_location_iq()

    def _refresh_location_iq(self) -> None:
        label = self.location_var.get()
        self.current_links.clear()
        if "/" not in label:
            if self.recording_combo is not None:
                self.recording_combo.configure(values=())
            self.recording_var.set("")
            return
        city, point = label.split("/", 1)
        links = list_linked_iq_details(Path(self.database_var.get()), city, point)
        for link in links:
            self.current_links[link.recording_stem] = link
        values = tuple(self.current_links)
        if self.recording_combo is not None:
            self.recording_combo.configure(values=values)
        if self.recording_var.get() not in self.current_links:
            self.recording_var.set(values[0] if values else "")
        self._sync_measured_center()

    def _sync_measured_center(self) -> None:
        stem = self.recording_var.get()
        matches = re.findall(r"(\d+(?:\.\d+)?)", stem)
        if matches:
            self.center_var.set(matches[-1])

    def _busy(self, active: bool, text: str) -> None:
        state = tk.DISABLED if active else tk.NORMAL
        self.screen_button.configure(state=state)
        self.correspondence_button.configure(state=state)
        self.representative_button.configure(state=state)
        self.generate_button.configure(state=state)
        if active:
            self.progress.start(12)
            self.selection_progress.start(12)
        else:
            self.progress.stop()
            self.selection_progress.stop()
        self.progress_text.set(text)
        self.status_var.set(text)

    def run_correspondence(self) -> None:
        try:
            guard = float(self.correspondence_guard_var.get())
            if guard < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("参数无效", "带宽保护量必须是大于或等于0的数字。")
            return
        arguments = (
            Path(self.database_var.get()),
            self.polarization_var.get(),
            self.band_var.get(),
            self.correspondence_scene_var.get(),
            guard,
            Path(self.iq_root_var.get()) if self.iq_root_var is not None and self.iq_root_var.get().strip() else None,
        )
        self._busy(True, "正在建立各地点频谱峰值与IQ实测频点的对应关系...")
        threading.Thread(target=self._correspondence_worker, args=arguments, daemon=True).start()

    def run_representative_selection(self, force_refresh: bool = False) -> None:
        arguments = (
            Path(self.database_var.get()), self.representative_scene_var.get(), force_refresh,
            Path(self.output_var.get()) / "场景代表IQ筛选结果.xlsx",
        )
        self._busy(
            True,
            (
                f"正在重新分析{self.representative_scene_var.get()}并更新缓存..."
                if force_refresh else f"正在读取{self.representative_scene_var.get()}场景缓存..."
            ),
        )
        threading.Thread(target=self._representative_worker, args=arguments, daemon=True).start()

    def _representative_worker(
        self,
        database_path: Path,
        scene_type: str,
        force_refresh: bool,
        workbook_path: Path,
    ) -> None:
        def report(current: int, total: int, stem: str) -> None:
            self.messages.put(("representative_progress", (current, total, stem)))

        try:
            if not force_refresh:
                cached = load_scene_iq_representative_cache(database_path, scene_type)
                if cached is not None:
                    rows, updated_at = cached
                    exported = export_scene_iq_representative_cache(database_path, workbook_path)
                    self.messages.put(("representative_done", {
                        "rows": rows, "cached": True, "updated_at": updated_at, "workbook": exported,
                    }))
                    return
            rows = select_scene_iq_representatives(
                database_path,
                scene_type,
                float(REPRESENTATIVE_DEFAULTS["frequency_tolerance_mhz"]),
                float(REPRESENTATIVE_DEFAULTS["sample_window_ms"]),
                str(REPRESENTATIVE_DEFAULTS["polarization"]),
                str(REPRESENTATIVE_DEFAULTS["band"]),
                float(REPRESENTATIVE_DEFAULTS["playback_segment_ms"]),
                int(REPRESENTATIVE_DEFAULTS["minimum_location_count"]),
                int(REPRESENTATIVE_DEFAULTS["maximum_bands"]),
                progress=report,
            )
            updated_at = save_scene_iq_representative_cache(
                database_path, scene_type, rows, REPRESENTATIVE_DEFAULTS
            )
            exported = export_scene_iq_representative_cache(database_path, workbook_path)
            self.messages.put(("representative_done", {
                "rows": rows, "cached": False, "updated_at": updated_at, "workbook": exported,
            }))
        except Exception as exc:
            self.messages.put(("error", f"代表频段与最强片段分析失败：{exc}"))

    def _load_selected_scene_cache(self) -> None:
        try:
            cached = load_scene_iq_representative_cache(
                Path(self.database_var.get()), self.representative_scene_var.get()
            )
        except Exception:
            cached = None
        if cached is None:
            self.current_representatives = ()
            self.representative_by_item.clear()
            self.representative_tree.delete(*self.representative_tree.get_children())
            self._set_text(
                self.representative_details,
                f"{self.representative_scene_var.get()}尚无缓存。首次点击加载时会完整分析并保存，之后直接读取。",
            )
            return
        rows, updated_at = cached
        self._show_representatives(rows, cached=True, updated_at=updated_at, workbook=None)

    def _show_representatives(
        self,
        rows: tuple[SceneIQRepresentative, ...],
        cached: bool = False,
        updated_at: str = "",
        workbook: Path | None = None,
    ) -> None:
        self.current_representatives = rows
        self.representative_by_item.clear()
        self.representative_tree.delete(*self.representative_tree.get_children())
        for row in rows:
            frequency_group = (
                f"{row.group_low_frequency_mhz:g}"
                if row.group_low_frequency_mhz == row.group_high_frequency_mhz
                else f"{row.group_low_frequency_mhz:g}～{row.group_high_frequency_mhz:g}"
            )
            item = self.representative_tree.insert("", tk.END, values=(
                row.rank, frequency_group, f"{row.representative_frequency_mhz:.6f}",
                f"{row.location_count}/{row.scene_location_count}", f"{row.representative_score:.3f}",
                f"{row.spectrum_support_count}/{row.spectrum_location_count}",
                f"{row.center_frequency_mhz:g}", row.city, row.point, row.recording_stem,
                f"{row.estimated_power_dbm:.3f}", f"{row.relative_power_dbfs:.3f}", row.candidate_count,
                f"{row.selected_start_s:.6f}", f"{row.selected_end_s:.6f}",
                f"{row.selected_duration_s:.3f}", f"{row.recording_duration_s:.6f}",
                f"{row.sample_window_ms:g}", row.scanned_window_count, f"{row.sample_rate_hz / 1e6:g}",
            ))
            self.representative_by_item[item] = row
        if self.representative_export_button is not None:
            self.representative_export_button.configure(state=tk.NORMAL if rows else tk.DISABLED)
        source_text = "数据库缓存" if cached else "本次重新分析"
        workbook_text = f"｜Excel：{workbook}" if workbook is not None else ""
        self._set_text(
            self.representative_details,
            f"{self.representative_scene_var.get()}共得到{len(rows)}个代表频段｜来源：{source_text}｜"
            f"更新时间：{updated_at or '未知'}{workbook_text}\n"
            "已按地点证据门槛筛选并限制最大数量；检测窗只定位最强时刻，"
            "表中的实际回放片段是围绕该时刻截取的连续原始IQ。",
        )
        self.notebook.select(self.representative_tab)
        self._busy(False, f"{self.representative_scene_var.get()}已从{source_text}加载：{len(rows)}个频段。")

    def _selected_representative(self) -> SceneIQRepresentative | None:
        selection = self.representative_tree.selection()
        return self.representative_by_item.get(selection[0]) if selection else None

    def _show_representative_details(self) -> None:
        row = self._selected_representative()
        if row is None:
            return
        spectrum_level = (
            f"{row.spectrum_median_level_dbuv_m:.3f} dBμV/m"
            if row.spectrum_median_level_dbuv_m is not None else "无可用场强"
        )
        spectrum_bandwidth = (
            f"{row.spectrum_median_bandwidth_mhz:.6f} MHz"
            if row.spectrum_median_bandwidth_mhz is not None else "无可用带宽"
        )
        text = (
            f"场景：{row.scene_type}\n"
            f"IQ中心频段：{row.group_low_frequency_mhz:g}～{row.group_high_frequency_mhz:g} MHz｜"
            f"频谱参考频率：{row.representative_frequency_mhz:.6f} MHz\n"
            f"IQ地点覆盖：{row.location_count}/{row.scene_location_count}｜候选IQ：{row.candidate_count}｜"
            f"代表得分：{row.representative_score:.3f}\n"
            f"频谱地点支持：{row.spectrum_support_count}/{row.spectrum_location_count}｜"
            f"中位场强：{spectrum_level}｜中位带宽：{spectrum_bandwidth}\n"
            f"最强稳定事件：{row.city}/{row.point}｜{row.recording_stem}\n"
            f"中心频率：{row.center_frequency_mhz:g} MHz｜估算功率：{row.estimated_power_dbm:.3f} dBm｜"
            f"相对功率：{row.relative_power_dbfs:.3f} dBFS\n"
            f"检测事件：{row.detected_event_duration_s * 1e3:.3f} ms｜"
            f"实际回放片段：{row.selected_start_s:.6f}～{row.selected_end_s:.6f} s｜"
            f"长度：{row.selected_duration_s:.3f} s｜原记录：{row.recording_duration_s:.6f} s\n"
            f"检测窗口：{row.sample_window_ms:g} ms｜扫描窗口：{row.scanned_window_count}｜"
            f"疑似削顶窗口：{row.rejected_clipped_windows}\n"
            f"选择说明：{row.selection_note}\n"
            f"文件：{row.ws1_file}"
        )
        self._set_text(self.representative_details, text)

    def use_selected_representative(self) -> None:
        row = self._selected_representative()
        if row is None:
            messagebox.showinfo("尚未选择", "请先在“代表频段/最强片段”表中选择一行。")
            return
        location_label = f"{row.city}/{row.point}"
        self.location_var.set(location_label)
        self._refresh_location_iq()
        if row.recording_stem not in self.current_links:
            messagebox.showerror("关联数据不可用", "所选IQ已不在当前地点的数据库关联中，请重新筛选。")
            return
        self.recording_var.set(row.recording_stem)
        self.center_var.set(f"{row.center_frequency_mhz:g}")
        self.sample_rate_var.set(f"{row.sample_rate_hz / 1e6:g}")
        self.measured_start_var.set(f"{row.selected_start_s:.6f}")
        source_duration_ms = row.selected_duration_s * 1e3
        self.measured_source_duration_var.set(f"{source_duration_ms:.6f}")
        self.duration_var.set(f"{max(1000.0, source_duration_ms):g}")
        self.measured_crossfade_var.set("2")
        self.measured_field_var.set(SCENE_FIELD_DEFAULTS.get(row.scene_type, "10"))
        self.name_var.set(
            f"{row.scene_type}_{row.representative_frequency_mhz:g}MHz_最强实测片段重构"
        )
        self.complex_base_representative = row
        self.complex_base_text.set(f"实采背景：{row.scene_type}｜{row.point}｜{row.recording_stem}")
        self.measured_selection_text.set(
            f"场景：{row.scene_type}\n"
            f"地点：{row.city}/{row.point}\n"
            f"代表频段：{row.group_low_frequency_mhz:g}～{row.group_high_frequency_mhz:g} MHz\n"
            f"实际回放IQ：{row.recording_stem}\n"
            f"检测事件：{row.detected_event_duration_s * 1e3:.3f} ms\n"
            f"实际片段：{row.selected_start_s:.6f}～{row.selected_end_s:.6f} s "
            f"({row.selected_duration_s:.3f} s)\n"
            f"中心频率：{row.center_frequency_mhz:g} MHz    "
            f"采样率：{row.sample_rate_hz / 1e6:g} MS/s"
        )
        self.mode_var.set(RECONSTRUCTION_MODES[0])
        self.progress_text.set(
            f"已将{row.recording_stem}最强时刻附近的{row.selected_duration_s:.3f} s真实IQ带入重构；"
            "目标播放更长时自动循环。"
        )
        self.show_reconstruction_workspace()

    def export_representatives(self) -> None:
        rows = self.current_representatives
        if not rows:
            messagebox.showwarning("没有筛选结果", "请先分析代表频段与最强IQ片段。")
            return
        selected = filedialog.asksaveasfilename(
            title="导出代表频段与最强IQ片段表",
            initialdir=self.output_var.get(),
            initialfile=f"{self.representative_scene_var.get()}_代表频段与最强IQ片段.csv",
            defaultextension=".csv",
            filetypes=(("CSV表格", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        with Path(selected).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow((
                "排序", "场景类型", "频段中心(MHz)", "频段下限(MHz)", "频段上限(MHz)",
                "代表频率(MHz)", "代表得分", "IQ地点数", "场景地点数", "频谱支持地点数",
                "有效频谱地点数", "频谱中位场强(dBμV/m)", "频谱中位带宽(MHz)",
                "候选IQ数量", "城市", "采集地点", "选中IQ数据组", "IQ中心频率(MHz)",
                "估算功率(dBm)", "相对功率(dBFS)", "仪器参考电平(dBm)",
                "片段起点(s)", "片段终点(s)", "片段长度(s)", "检测事件长度(s)",
                "原记录时长(s)", "功率窗口(ms)", "扫描窗口数", "疑似削顶窗口数", "选择说明",
                "采样率(Hz)", "WSM文件", "WS1文件", "WS2文件",
            ))
            for row in rows:
                writer.writerow((
                    row.rank, row.scene_type, row.group_center_frequency_mhz,
                    row.group_low_frequency_mhz, row.group_high_frequency_mhz,
                    row.representative_frequency_mhz, row.representative_score,
                    row.location_count, row.scene_location_count, row.spectrum_support_count,
                    row.spectrum_location_count, row.spectrum_median_level_dbuv_m,
                    row.spectrum_median_bandwidth_mhz, row.candidate_count,
                    row.city, row.point, row.recording_stem, row.center_frequency_mhz,
                    row.estimated_power_dbm, row.relative_power_dbfs, row.reference_level_dbm,
                    row.selected_start_s, row.selected_end_s, row.selected_duration_s,
                    row.detected_event_duration_s, row.recording_duration_s, row.sample_window_ms,
                    row.scanned_window_count, row.rejected_clipped_windows, row.selection_note,
                    row.sample_rate_hz,
                    row.wsm_file, row.ws1_file, row.ws2_file,
                ))
        self.progress_text.set(f"代表频段与最强IQ片段表已导出：{selected}")
        self.status_var.set(f"代表频段与最强IQ片段表已导出：{selected}")

    def _correspondence_worker(self, *arguments) -> None:
        try:
            rows = build_spectrum_iq_correspondence(*arguments)
            self.messages.put(("correspondence_done", rows))
        except Exception as exc:
            self.messages.put(("error", f"频谱-IQ对应表生成失败：{exc}"))

    def _show_correspondence(self, rows: tuple[SpectrumIQCorrespondence, ...]) -> None:
        summaries = aggregate_spectrum_iq_correspondence(rows)
        self.current_correspondence = summaries
        self.correspondence_tree.delete(*self.correspondence_tree.get_children())
        for row in summaries:
            self.correspondence_tree.insert("", tk.END, values=(
                row.serial, row.city, row.point, row.scene_type,
                row.spectrum_peak_frequencies_mhz, row.spectrum_peak_levels_dbuv_m,
                row.spectrum_bandwidths_3db_mhz, row.iq_center_frequencies_mhz,
                row.iq_recording_stems, row.iq_file_statuses, row.correspondence_summary,
            ))
        association_path = self._association_csv()
        self._update_association_csv(association_path, summaries)
        with_spectrum = sum(bool(row.spectrum_peak_frequencies_mhz) for row in summaries)
        with_iq = sum(bool(row.iq_center_frequencies_mhz) for row in summaries)
        details = [
            f"共{len(summaries)}个地点，每个地点一行；{with_spectrum}个地点有频谱峰值，{with_iq}个地点有关联IQ。",
            "同一行中，频谱峰值频率、峰值功率和BW_3dB按相同顺序一一对应；IQ中心频率、数据组名和文件状态也按相同顺序对应。",
            f"分析结果已写入主关联表：{association_path}",
        ]
        self._set_text(self.correspondence_details, "\n".join(details))
        self.correspondence_export_button.configure(state=tk.NORMAL if rows else tk.DISABLED)
        self.notebook.select(self.correspondence_tab)
        self._busy(False, f"频谱-IQ汇总表已生成：{len(summaries)}个地点，并已自动保存CSV。")

    @staticmethod
    def _association_csv() -> Path:
        return application_dir() / "场景地点_IQ关联表_更新版.csv"

    @staticmethod
    def _update_association_csv(path: Path, rows: tuple[LocationSpectrumIQSummary, ...]) -> None:
        """Append analysis columns to the user-maintained master association table."""
        if not path.is_file():
            raise FileNotFoundError(f"没有找到主关联表：{path}")
        backup = path.with_name(f"{path.stem}_分析前备份{path.suffix}")
        if not backup.exists():
            shutil.copy2(path, backup)
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            original_fields = list(reader.fieldnames or ())
            original_rows = list(reader)
        analysis_fields = [
            "极化方式", "频段", "频谱峰值频率(MHz)", "频谱峰值功率(dBμV/m)",
            "频谱BW_3dB(MHz)", "IQ中心频率(MHz)", "IQ数据组完整名称", "IQ文件状态", "汇总说明",
        ]
        fields = original_fields + [field for field in analysis_fields if field not in original_fields]
        summary_map = {(row.city, row.point): row for row in rows}
        for original in original_rows:
            summary = summary_map.get(((original.get("城市") or "").strip(), (original.get("地点") or "").strip()))
            if summary is None:
                continue
            original.update({
                "极化方式": summary.polarization,
                "频段": summary.band,
                "频谱峰值频率(MHz)": summary.spectrum_peak_frequencies_mhz,
                "频谱峰值功率(dBμV/m)": summary.spectrum_peak_levels_dbuv_m,
                "频谱BW_3dB(MHz)": summary.spectrum_bandwidths_3db_mhz,
                "IQ中心频率(MHz)": summary.iq_center_frequencies_mhz,
                "IQ数据组完整名称": summary.iq_recording_stems,
                "IQ文件状态": summary.iq_file_statuses,
                "汇总说明": summary.correspondence_summary,
            })
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(original_rows)
        temporary.replace(path)

    @staticmethod
    def _write_correspondence_csv(path: Path, rows: tuple[LocationSpectrumIQSummary, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow((
                "序号", "城市", "采集地点", "场景类型", "极化方式", "频段",
                "频谱峰值频率(MHz)", "频谱峰值功率(dBμV/m)", "频谱BW_3dB(MHz)",
                "IQ中心频率(MHz)", "IQ数据组名", "IQ文件状态", "汇总说明",
            ))
            for row in rows:
                writer.writerow((
                    row.serial, row.city, row.point, row.scene_type, row.polarization, row.band,
                    row.spectrum_peak_frequencies_mhz, row.spectrum_peak_levels_dbuv_m,
                    row.spectrum_bandwidths_3db_mhz, row.iq_center_frequencies_mhz,
                    row.iq_recording_stems, row.iq_file_statuses, row.correspondence_summary,
                ))

    def _load_saved_correspondence(self) -> None:
        path = self._association_csv()
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = tuple(LocationSpectrumIQSummary(
                    serial=int(row.get("序号") or index),
                    city=(row.get("城市") or "").strip(),
                    point=(row.get("地点") or "").strip(),
                    scene_type=(row.get("场景类型") or "").strip(),
                    polarization=(row.get("极化方式") or "").strip(),
                    band=(row.get("频段") or "").strip(),
                    spectrum_peak_frequencies_mhz=(row.get("频谱峰值频率(MHz)") or "").strip(),
                    spectrum_peak_levels_dbuv_m=(row.get("频谱峰值功率(dBμV/m)") or "").strip(),
                    spectrum_bandwidths_3db_mhz=(row.get("频谱BW_3dB(MHz)") or "").strip(),
                    iq_center_frequencies_mhz=(row.get("IQ中心频率(MHz)") or "").strip(),
                    iq_recording_stems=(row.get("IQ数据组完整名称") or "").strip(),
                    iq_file_statuses=(row.get("IQ文件状态") or "").strip(),
                    correspondence_summary=(row.get("汇总说明") or "").strip(),
                ) for index, row in enumerate(reader, start=1))
        except (OSError, ValueError):
            return
        self.current_correspondence = rows
        self.correspondence_tree.delete(*self.correspondence_tree.get_children())
        for row in rows:
            self.correspondence_tree.insert("", tk.END, values=(
                row.serial, row.city, row.point, row.scene_type,
                row.spectrum_peak_frequencies_mhz, row.spectrum_peak_levels_dbuv_m,
                row.spectrum_bandwidths_3db_mhz, row.iq_center_frequencies_mhz,
                row.iq_recording_stems, row.iq_file_statuses, row.correspondence_summary,
            ))
        self.correspondence_export_button.configure(state=tk.NORMAL if rows else tk.DISABLED)
        populated = sum(bool(row.spectrum_peak_frequencies_mhz or row.iq_center_frequencies_mhz) for row in rows)
        self._set_text(
            self.correspondence_details,
            f"已直接读取主关联表：{path}\n共{len(rows)}个地点，其中{populated}个地点已有频点分析结果。",
        )

    def export_correspondence(self) -> None:
        rows = self.current_correspondence
        if not rows:
            messagebox.showwarning("没有对应结果", "请先生成频谱-IQ对应表。")
            return
        selected = filedialog.asksaveasfilename(
            title="导出频谱与IQ频点对应表",
            initialdir=self.output_var.get(),
            initialfile=f"{self.correspondence_scene_var.get()}_频谱-IQ频点对应表.csv",
            defaultextension=".csv",
            filetypes=(("CSV表格", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        self._write_correspondence_csv(Path(selected), rows)
        self.progress_text.set(f"频谱-IQ对应表已导出：{selected}")
        self.status_var.set(f"频谱-IQ对应表已导出：{selected}")

    def run_typical_screening(self) -> None:
        try:
            tolerance = float(self.tolerance_var.get())
            minimum_probability = float(self.minimum_probability_var.get())
        except ValueError:
            messagebox.showerror("参数无效", "频率容差和最低出现概率必须为数字。")
            return
        arguments = (
            Path(self.database_var.get()), self.scene_var.get(), self.polarization_var.get(),
            self.band_var.get(), tolerance, minimum_probability,
        )
        self._busy(True, f"正在筛选{self.scene_var.get()}的跨场景典型信号...")
        threading.Thread(target=self._screen_worker, args=arguments, daemon=True).start()

    def _screen_worker(self, *arguments) -> None:
        try:
            self.messages.put(("screen_done", analyze_typical_signals(*arguments)))
        except Exception as exc:
            self.messages.put(("error", f"典型信号筛选失败：{exc}"))

    def _show_screening(self, result: TypicalSignalResult) -> None:
        self.current_typical_result = result
        self.typical_by_item.clear()
        self.typical_tree.delete(*self.typical_tree.get_children())
        for signal in result.signals:
            item = self.typical_tree.insert("", tk.END, values=(
                signal.rank,
                signal.category,
                f"{signal.typical_frequency_mhz:.6f}",
                f"{signal.scene_probability * 100:.1f}%",
                f"{signal.global_probability * 100:.1f}%",
                f"{signal.specificity:+.3f}",
                f"{signal.level_contrast_db:+.3f}",
                f"{signal.mean_bandwidth_3db_mhz:.6f}",
                len(signal.iq_candidates),
                f"{signal.score:.4f}",
            ))
            self.typical_by_item[item] = signal
        self.notebook.select(self.typical_tab)
        self._busy(False, f"筛选完成：{len(result.signals)}类典型信号，双击或多选后可用于复杂场景重构。")

    def _selected_typical_signals(self) -> list[TypicalSignal]:
        return [self.typical_by_item[item] for item in self.typical_tree.selection() if item in self.typical_by_item]

    def _show_typical_details(self) -> None:
        selected = self._selected_typical_signals()
        if not selected:
            return
        lines: list[str] = []
        for signal in selected[:5]:
            lines.extend((
                f"{signal.rank}. {signal.typical_frequency_mhz:.6f} MHz｜{signal.category}",
                f"场景概率 {signal.scene_probability:.3f}，全局概率 {signal.global_probability:.3f}，"
                f"特异性 {signal.specificity:+.3f}，场强增量 {signal.level_contrast_db:+.3f} dB",
                f"来源地点：{', '.join(f'{city}/{point}' for city, point in signal.location_keys)}",
            ))
            if signal.iq_candidates:
                lines.append("IQ候选：")
                for candidate in signal.iq_candidates[:8]:
                    lines.append(
                        f"  {candidate.city}/{candidate.point}｜{candidate.recording_stem}｜"
                        f"中心{candidate.center_frequency_mhz:.3f} MHz｜覆盖"
                        f"{candidate.coverage_low_mhz:.3f}～{candidate.coverage_high_mhz:.3f} MHz｜"
                        f"{'可读取' if candidate.available else '移动硬盘离线'}"
                    )
            else:
                lines.append("IQ候选：无，重构时采用参数化合成。")
            lines.append("")
        self._set_text(self.typical_details, "\n".join(lines))

    def select_recommended_signals(self) -> None:
        if not self.typical_by_item:
            messagebox.showinfo("尚无筛选结果", "请先生成跨场景典型信号。")
            return
        try:
            center = float(self.center_var.get())
            half_band = float(self.sample_rate_var.get()) / 2.0
        except ValueError:
            messagebox.showerror("参数无效", "中心频率和采样率必须为数字。")
            return
        compatible = [
            (item, signal) for item, signal in self.typical_by_item.items()
            if abs(signal.typical_frequency_mhz - center) + max(signal.mean_bandwidth_3db_mhz / 2, 0.05) < half_band
        ]
        if not compatible:
            first_item, first_signal = next(iter(self.typical_by_item.items()))
            center = first_signal.typical_frequency_mhz
            self.center_var.set(f"{center:.6f}")
            compatible = [
                (item, signal) for item, signal in self.typical_by_item.items()
                if abs(signal.typical_frequency_mhz - center)
                + max(signal.mean_bandwidth_3db_mhz / 2, 0.05) < half_band
            ]
        category_limits = {"公共背景": 2, "场景常见": 2, "场景增强": 3, "场景特有": 3}
        selected: list[str] = []
        counts: dict[str, int] = {}
        for item, signal in compatible:
            if counts.get(signal.category, 0) >= category_limits.get(signal.category, 2):
                continue
            selected.append(item)
            counts[signal.category] = counts.get(signal.category, 0) + 1
        self.typical_tree.selection_set(selected)
        self.typical_tree.focus(selected[0] if selected else "")
        self._show_typical_details()
        self.mode_var.set(RECONSTRUCTION_MODES[3])
        self.progress_text.set(f"已选择当前带宽内{len(selected)}个推荐信号。")

    def export_typical_signals(self) -> None:
        result = self.current_typical_result
        if result is None:
            messagebox.showwarning("没有筛选结果", "请先生成跨场景典型信号。")
            return
        selected = filedialog.asksaveasfilename(
            title="导出典型信号与IQ覆盖候选",
            initialdir=self.output_var.get(),
            initialfile=f"{result.scene_type}_{result.polarization}_{result.band}_典型信号.csv",
            defaultextension=".csv",
            filetypes=(("CSV表格", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        with Path(selected).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow((
                "排序", "场景类型", "信号类别", "典型频率(MHz)", "出现地点数", "场景地点数",
                "场景出现概率", "全局出现概率", "场景特异性", "提升度", "平均峰值(dBμV/m)",
                "全局中位峰值(dBμV/m)", "场强增量(dB)", "平均BW_3dB(MHz)", "综合得分",
                "IQ候选数", "可读取IQ数", "IQ候选详情",
            ))
            for signal in result.signals:
                details = "; ".join(
                    f"{candidate.city}/{candidate.point}/{candidate.recording_stem}/"
                    f"{candidate.center_frequency_mhz:.6f}MHz/"
                    f"{'可读取' if candidate.available else '离线'}"
                    for candidate in signal.iq_candidates
                )
                writer.writerow((
                    signal.rank, signal.scene_type, signal.category, signal.typical_frequency_mhz,
                    signal.occurrence_count, signal.scene_location_count, signal.scene_probability,
                    signal.global_probability, signal.specificity, signal.lift, signal.mean_level_dbuv_m,
                    signal.global_median_level_dbuv_m, signal.level_contrast_db,
                    signal.mean_bandwidth_3db_mhz, signal.score, len(signal.iq_candidates),
                    sum(candidate.available for candidate in signal.iq_candidates), details,
                ))
        self.progress_text.set(f"典型信号表已导出：{selected}")
        self.status_var.set(f"典型信号表已导出：{selected}")

    def run_reconstruction(self) -> None:
        try:
            mode = self.mode_var.get()
            duration_var = {
                RECONSTRUCTION_MODES[0]: self.duration_var,
                RECONSTRUCTION_MODES[1]: self.single_duration_var,
                RECONSTRUCTION_MODES[2]: self.multi_duration_var,
                RECONSTRUCTION_MODES[3]: self.complex_duration_var,
            }.get(mode)
            if duration_var is None:
                raise ValueError("未知重构方式")
            common = {
                "name": self.name_var.get().strip() or "场景重构信号",
                "center": float(self.center_var.get()),
                "sample_rate": float(self.sample_rate_var.get()) * 1e6,
                "duration": float(duration_var.get()) * 1e-3,
                "seed": int(self.seed_var.get()) if mode != RECONSTRUCTION_MODES[0] else 2026,
            }
            if mode == RECONSTRUCTION_MODES[0]:
                link = self.current_links.get(self.recording_var.get())
                if link is None:
                    raise ValueError("请返回“信号选择”页面，选择一个场景代表IQ后再进入实测重构")
                recording = recording_from_paths(
                    link.recording_stem, Path(link.wsm_file), (Path(link.ws1_file), Path(link.ws2_file))
                )
                parameters = (
                    common["name"], recording, float(self.measured_start_var.get()),
                    float(self.measured_source_duration_var.get()) * 1e-3,
                    common["duration"], common["sample_rate"],
                    float(self.measured_crossfade_var.get()) * 1e-3,
                    float(self.measured_field_var.get()),
                )
            elif mode == RECONSTRUCTION_MODES[1]:
                parameters = (
                    common["name"], self.modulation_var.get(), common["center"], common["sample_rate"],
                    common["duration"], float(self.offset_var.get()), float(self.level_var.get()),
                    float(self.modulation_frequency_var.get()), float(self.modulation_index_var.get()),
                    float(self.symbol_rate_var.get()), common["seed"], float(self.single_field_var.get()),
                )
            elif mode == RECONSTRUCTION_MODES[2]:
                parameters = (
                    common["name"], common["center"], common["sample_rate"], common["duration"],
                    self.component_text.get("1.0", tk.END), common["seed"], float(self.multi_field_var.get()),
                )
            elif mode == RECONSTRUCTION_MODES[3]:
                representative = self.complex_base_representative
                if representative is None:
                    raise ValueError("请先在“代表频段/最强片段”表选择一行，并设为复杂场景实采背景")
                recording = recording_from_paths(
                    representative.recording_stem,
                    Path(representative.wsm_file),
                    (Path(representative.ws1_file), Path(representative.ws2_file)),
                )
                parameters = (
                    common["name"], recording, float(self.measured_start_var.get()),
                    float(self.measured_source_duration_var.get()) * 1e-3, common["duration"],
                    common["sample_rate"], float(self.measured_crossfade_var.get()) * 1e-3,
                    self.complex_component_text.get("1.0", tk.END), float(self.complex_field_var.get()),
                    common["seed"],
                )
            else:
                raise ValueError("未知重构方式")
        except ValueError as exc:
            messagebox.showerror("重构参数无效", str(exc))
            return
        self._busy(True, f"正在执行{mode}...")
        threading.Thread(target=self._reconstruction_worker, args=(mode, parameters), daemon=True).start()

    def _reconstruction_worker(self, mode: str, parameters: tuple[object, ...]) -> None:
        try:
            if mode == RECONSTRUCTION_MODES[0]:
                result = reconstruct_measured_signal(*parameters)
            elif mode == RECONSTRUCTION_MODES[1]:
                result = reconstruct_single_modulated(*parameters)
            elif mode == RECONSTRUCTION_MODES[2]:
                result = reconstruct_multi_system(*parameters)
            else:
                result = reconstruct_hybrid_scene(*parameters)
            self.messages.put(("reconstruction_done", result))
        except Exception as exc:
            self.messages.put(("error", f"重构失败：{exc}"))

    def _show_reconstruction(self, result: ReconstructionResult) -> None:
        self._stop_preview_playback(reset=True)
        self.current_result = result
        self._set_text(self.summary_text, reconstruction_summary(result))
        self._show_figure(result)
        requested_ms = float(result.metadata.get("requested_playback_duration_s", result.duration_s)) * 1e3
        self.preview_seek.configure(to=max(requested_ms, 0.001))
        self.comparison_seek.configure(to=max(requested_ms, 0.001))
        self.preview_position_ms.set(0.0)
        self.preview_play_button.configure(state=tk.NORMAL)
        has_original = result.original_iq is not None and result.original_iq.size >= 32
        self.comparison_seek.configure(state=tk.NORMAL if has_original else tk.DISABLED)
        self.comparison_window_combo.configure(state="readonly" if has_original else tk.DISABLED)
        self._show_comparison(result)
        self._draw_preview_iq_window()
        self.export_button.configure(state=tk.NORMAL)
        if self.on_result_ready is not None:
            self.on_result_ready(result)
        self.reconstruction_notebook.select(self.comparison_tab if has_original else self.preview_tab)
        self._busy(
            False,
            f"{result.name}生成完成：缓冲区{result.iq.size:,}个复采样点，目标回放{requested_ms:g} ms。",
        )

    def _show_figure(self, result: ReconstructionResult) -> None:
        for child in self.preview_host.winfo_children():
            child.destroy()
        self.preview_canvas = FigureCanvasTkAgg(result.figure, master=self.preview_host)
        self.preview_toolbar = NavigationToolbar2Tk(self.preview_canvas, self.preview_host, pack_toolbar=False)
        self.preview_toolbar.update()
        self.preview_toolbar.pack(side=tk.TOP, fill=tk.X)
        widget = self.preview_canvas.get_tk_widget()
        widget.configure(width=360, height=240)
        widget.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))

        def resize() -> None:
            self.preview_host.update_idletasks()
            width = max(360, self.preview_host.winfo_width() - 24)
            height = max(240, self.preview_host.winfo_height() - self.preview_toolbar.winfo_height() - 18)
            widget.configure(width=width, height=height)
            widget.event_generate("<Configure>", width=width, height=height)
            self.preview_canvas.draw_idle()

        self.after_idle(resize)

    def _show_comparison(self, result: ReconstructionResult) -> None:
        for child in self.comparison_host.winfo_children():
            child.destroy()
        self.comparison_canvas = None
        self.comparison_toolbar = None
        self.comparison_figure = None
        if result.original_iq is None or result.original_iq.size < 32:
            self.comparison_status_var.set("当前模式没有一一对应的原始采集IQ。")
            ttk.Label(
                self.comparison_host,
                text=(
                    f"{reconstruction_explanation(result.mode)}\n\n"
                    "该结果是参数化生成的数字IQ，只能校验生成参数、频谱和时域特征，"
                    "不能伪造一条“原始采集信号”进行对比。"
                ),
                justify=tk.CENTER,
                foreground="#475569",
                wraplength=720,
            ).pack(expand=True, padx=24, pady=30)
            return

        self.comparison_figure = Figure(figsize=(4, 3), constrained_layout=True)
        self.comparison_figure.subplots(2, 2)
        self.comparison_canvas = FigureCanvasTkAgg(self.comparison_figure, master=self.comparison_host)
        self.comparison_toolbar = NavigationToolbar2Tk(
            self.comparison_canvas, self.comparison_host, pack_toolbar=False
        )
        self.comparison_toolbar.update()
        self.comparison_toolbar.pack(side=tk.TOP, fill=tk.X)
        widget = self.comparison_canvas.get_tk_widget()
        widget.configure(width=360, height=240)
        widget.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))

        def resize() -> None:
            if self.comparison_canvas is None or self.comparison_toolbar is None:
                return
            self.comparison_host.update_idletasks()
            width = max(360, self.comparison_host.winfo_width() - 24)
            height = max(240, self.comparison_host.winfo_height() - self.comparison_toolbar.winfo_height() - 18)
            self.comparison_figure.set_size_inches(
                width / self.comparison_figure.dpi,
                height / self.comparison_figure.dpi,
                forward=False,
            )
            widget.configure(width=width, height=height)
            widget.event_generate("<Configure>", width=width, height=height)
            self.comparison_canvas.draw_idle()

        self.after_idle(resize)

    @staticmethod
    def _cyclic_iq_window(
        iq: np.ndarray,
        sample_rate_hz: float,
        position_ms: float,
        window_ms: float,
    ) -> tuple[np.ndarray, float]:
        duration_ms = iq.size / sample_rate_hz * 1e3
        buffer_position_ms = position_ms % max(duration_ms, 1e-12)
        start = int(round(buffer_position_ms * 1e-3 * sample_rate_hz)) % iq.size
        count = max(32, int(round(window_ms * 1e-3 * sample_rate_hz)))
        indices = (start + np.arange(count, dtype=np.int64)) % iq.size
        return iq[indices], buffer_position_ms

    @staticmethod
    def _decimated_iq(iq: np.ndarray, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
        stride = max(1, math.ceil(iq.size / 5000))
        plotted = iq[::stride]
        time_ms = np.arange(plotted.size) * stride / sample_rate_hz * 1e3
        return time_ms, plotted

    @staticmethod
    def _relative_spectrum(
        iq: np.ndarray,
        sample_rate_hz: float,
        center_frequency_mhz: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        fft_points = min(iq.size, 131_072)
        window = np.hanning(fft_points)
        transformed = np.fft.fftshift(np.fft.fft(iq[:fft_points] * window))
        relative_db = 20.0 * np.log10(np.maximum(np.abs(transformed), 1e-12))
        relative_db -= float(np.max(relative_db))
        frequencies = center_frequency_mhz + np.fft.fftshift(
            np.fft.fftfreq(fft_points, d=1.0 / sample_rate_hz)
        ) / 1e6
        return frequencies, relative_db

    @staticmethod
    def _normalized_correlation(first: np.ndarray, second: np.ndarray) -> float:
        points = min(10_000, max(32, min(first.size, second.size)))
        target = np.linspace(0.0, 1.0, points)

        def interpolate(values: np.ndarray) -> np.ndarray:
            source = np.linspace(0.0, 1.0, values.size)
            real = np.interp(target, source, values.real)
            imag = np.interp(target, source, values.imag)
            centered = real + 1j * imag
            return centered - np.mean(centered)

        first_aligned = interpolate(first)
        second_aligned = interpolate(second)
        denominator = float(np.linalg.norm(first_aligned) * np.linalg.norm(second_aligned))
        if denominator <= 1e-15:
            return 0.0
        return float(min(1.0, abs(np.vdot(first_aligned, second_aligned)) / denominator))

    @staticmethod
    def _papr_db(iq: np.ndarray) -> float:
        magnitude = np.abs(iq)
        peak = float(np.max(magnitude))
        rms = float(np.sqrt(np.mean(np.square(magnitude))))
        return 20.0 * math.log10(max(peak, 1e-12) / max(rms, 1e-12))

    def _draw_comparison(self) -> None:
        result = self.current_result
        figure = self.comparison_figure
        canvas = self.comparison_canvas
        if (
            result is None
            or figure is None
            or canvas is None
            or result.original_iq is None
            or result.original_iq.size < 32
            or len(figure.axes) != 4
        ):
            return
        try:
            window_ms = max(0.01, float(self.preview_window_ms.get()))
        except ValueError:
            window_ms = 1.0
        position_ms = max(0.0, self.preview_position_ms.get())
        original_rate = float(result.original_sample_rate_hz or result.sample_rate_hz)
        original_center = float(result.original_center_frequency_mhz or result.center_frequency_mhz)
        original_window, original_position_ms = self._cyclic_iq_window(
            result.original_iq, original_rate, position_ms, window_ms
        )
        reconstructed_window, reconstructed_position_ms = self._cyclic_iq_window(
            result.iq, result.sample_rate_hz, position_ms, window_ms
        )
        original_time, original_plot = self._decimated_iq(original_window, original_rate)
        reconstructed_time, reconstructed_plot = self._decimated_iq(
            reconstructed_window, result.sample_rate_hz
        )
        original_frequency, original_db = self._relative_spectrum(
            original_window, original_rate, original_center
        )
        reconstructed_frequency, reconstructed_db = self._relative_spectrum(
            reconstructed_window, result.sample_rate_hz, result.center_frequency_mhz
        )
        correlation = self._normalized_correlation(original_window, reconstructed_window)
        original_papr = self._papr_db(original_window)
        reconstructed_papr = self._papr_db(reconstructed_window)

        original_axis, reconstructed_axis, spectrum_axis, envelope_axis = figure.axes
        for axis in figure.axes:
            axis.clear()

        original_axis.plot(original_time, original_plot.real, color="#2563eb", linewidth=0.7, label="I")
        original_axis.plot(original_time, original_plot.imag, color="#dc2626", linewidth=0.7, alpha=0.82, label="Q")
        original_axis.set(
            title="原始采集IQ（未经重构处理）",
            xlabel="窗口内时间 (ms)",
            ylabel="原始数字幅度",
            xlim=(0.0, window_ms),
        )
        original_axis.legend(loc="upper right", ncol=2)

        reconstructed_axis.plot(
            reconstructed_time, reconstructed_plot.real, color="#2563eb", linewidth=0.7, label="I"
        )
        reconstructed_axis.plot(
            reconstructed_time,
            reconstructed_plot.imag,
            color="#dc2626",
            linewidth=0.7,
            alpha=0.82,
            label="Q",
        )
        reconstructed_axis.set(
            title="重构结果IQ（回放输入）",
            xlabel="窗口内时间 (ms)",
            ylabel="重构数字幅度",
            xlim=(0.0, window_ms),
        )
        reconstructed_axis.legend(loc="upper right", ncol=2)

        spectrum_axis.plot(original_frequency, original_db, color="#64748b", linewidth=0.9, label="原始采集")
        spectrum_axis.plot(
            reconstructed_frequency,
            reconstructed_db,
            color="#2563eb",
            linewidth=0.9,
            alpha=0.88,
            label="重构结果",
        )
        spectrum_axis.set(
            title="相对频谱形状对比（各自峰值归一化）",
            xlabel="频率 (MHz)",
            ylabel="相对幅度 (dB)",
            ylim=(-100.0, 3.0),
        )
        spectrum_axis.legend(loc="upper right")

        original_envelope = np.abs(original_plot)
        reconstructed_envelope = np.abs(reconstructed_plot)
        original_envelope /= max(float(np.max(original_envelope)), 1e-12)
        reconstructed_envelope /= max(float(np.max(reconstructed_envelope)), 1e-12)
        envelope_axis.plot(
            original_time, original_envelope, color="#64748b", linewidth=0.9, label="原始采集"
        )
        envelope_axis.plot(
            reconstructed_time,
            reconstructed_envelope,
            color="#2563eb",
            linewidth=0.9,
            alpha=0.88,
            label="重构结果",
        )
        envelope_axis.set(
            title="幅度包络形状对比（各自峰值归一化）",
            xlabel="窗口内时间 (ms)",
            ylabel="归一化包络",
            xlim=(0.0, window_ms),
            ylim=(-0.03, 1.08),
        )
        envelope_axis.legend(loc="upper right")

        source_name = str(result.metadata.get("source_recording", "原始采集片段"))
        figure.suptitle(
            f"原始采集 vs 重构结果｜{source_name} → {result.name}\n"
            "时域保留各自数字幅度；频谱和包络仅比较形状",
            fontsize=11,
        )
        for axis in figure.axes:
            axis.grid(alpha=0.2)
            axis.tick_params(labelsize=8)
            axis.title.set_fontsize(10)
        self.comparison_status_var.set(
            f"位置 {position_ms:.3f} ms｜原始片段 {original_position_ms:.3f} ms｜"
            f"重构缓冲区 {reconstructed_position_ms:.3f} ms｜归一化相关度 {correlation:.3f}｜"
            f"PAPR 原始 {original_papr:.2f} dB / 重构 {reconstructed_papr:.2f} dB"
        )
        canvas.draw_idle()

    def _toggle_preview_playback(self) -> None:
        if self.current_result is None:
            return
        if self.preview_playing:
            self.preview_playing = False
            self.preview_play_button.configure(text="继续播放")
            if self.preview_play_after_id is not None:
                self.after_cancel(self.preview_play_after_id)
                self.preview_play_after_id = None
            return
        self.preview_playing = True
        self.preview_play_button.configure(text="暂停")
        self._advance_preview_playback()

    def _stop_preview_playback(self, reset: bool = True) -> None:
        self.preview_playing = False
        if self.preview_play_after_id is not None:
            self.after_cancel(self.preview_play_after_id)
            self.preview_play_after_id = None
        if hasattr(self, "preview_play_button"):
            self.preview_play_button.configure(text="播放I/Q窗口")
        if reset and hasattr(self, "preview_position_ms"):
            self.preview_position_ms.set(0.0)
            if self.current_result is not None:
                self._draw_preview_iq_window()

    def _advance_preview_playback(self) -> None:
        result = self.current_result
        if not self.preview_playing or result is None:
            return
        requested_ms = float(result.metadata.get("requested_playback_duration_s", result.duration_s)) * 1e3
        try:
            step_ms = max(0.1, float(self.preview_window_ms.get()))
        except ValueError:
            step_ms = 1.0
        position = self.preview_position_ms.get()
        if position >= requested_ms:
            position = 0.0
        self.preview_position_ms.set(position)
        self._draw_preview_iq_window()
        self.preview_position_ms.set(min(requested_ms, position + step_ms))
        self.preview_play_after_id = self.after(300, self._advance_preview_playback)

    def _draw_preview_iq_window(self) -> None:
        result = self.current_result
        canvas = self.preview_canvas
        if result is None or canvas is None or not result.figure.axes:
            return
        try:
            window_ms = max(0.01, float(self.preview_window_ms.get()))
        except ValueError:
            window_ms = 1.0
        requested_ms = float(result.metadata.get("requested_playback_duration_s", result.duration_s)) * 1e3
        buffer_ms = result.duration_s * 1e3
        position_ms = min(max(0.0, self.preview_position_ms.get()), requested_ms)
        buffer_position_ms = position_ms % max(buffer_ms, 1e-12)
        start = int(round(buffer_position_ms * 1e-3 * result.sample_rate_hz)) % result.iq.size
        count = max(32, int(round(window_ms * 1e-3 * result.sample_rate_hz)))
        indices = (start + np.arange(count, dtype=np.int64)) % result.iq.size
        window_iq = result.iq[indices]
        stride = max(1, math.ceil(window_iq.size / 5000))
        plotted = window_iq[::stride]
        time_ms = np.arange(plotted.size) * stride / result.sample_rate_hz * 1e3
        axis = result.figure.axes[0]
        if len(axis.lines) < 2:
            return
        axis.lines[0].set_data(time_ms, plotted.real)
        axis.lines[1].set_data(time_ms, plotted.imag)
        axis.set_xlim(0.0, window_ms)
        peak = max(float(np.max(np.abs(plotted.real))), float(np.max(np.abs(plotted.imag))), 0.05)
        axis.set_ylim(-1.08 * peak, 1.08 * peak)
        axis.set_title(f"重构信号I/Q波形｜目标回放位置 {position_ms:.3f} ms｜窗口 {window_ms:g} ms")
        loop_note = "，循环缓冲区" if bool(result.metadata.get("loop_playback_required", False)) else ""
        self.preview_play_status.set(
            f"位置 {position_ms:.3f}/{requested_ms:.3f} ms{loop_note}，缓冲区位置 {buffer_position_ms:.3f} ms"
        )
        self._draw_comparison()
        canvas.draw_idle()

    def export_reconstruction(self) -> None:
        if self.current_result is None:
            messagebox.showwarning("没有重构结果", "请先生成重构信号。")
            return
        selected = filedialog.askdirectory(title="选择重构结果输出根目录", initialdir=self.output_var.get())
        if not selected:
            return
        try:
            paths = save_reconstruction(self.current_result, Path(selected))
            comparison_path: Path | None = None
            if self.comparison_figure is not None and self.current_result.original_iq is not None:
                comparison_path = paths[0].with_name(f"{paths[0].stem}_原始与重构对比.png")
                self.comparison_figure.savefig(comparison_path, dpi=160)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.progress_text.set(f"已导出：{paths[0].parent}")
        self.status_var.set(f"重构信号已导出：{paths[0].parent}")
        comparison_note = "、原始与重构对比图" if comparison_path is not None else ""
        messagebox.showinfo(
            "导出完成",
            f"已保存复数IQ、交织二进制、JSON参数、预览图{comparison_note}。\n\n{paths[0].parent}",
        )

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "screen_done":
                self._show_screening(payload)  # type: ignore[arg-type]
            elif kind == "correspondence_done":
                self._show_correspondence(payload)  # type: ignore[arg-type]
            elif kind == "representative_progress":
                current, total, stem = payload  # type: ignore[misc]
                self.progress_text.set(f"正在完整扫描IQ稳定功率：{current}/{total}｜{stem}")
                self.status_var.set(self.progress_text.get())
            elif kind == "representative_done":
                data = payload  # type: ignore[assignment]
                self._show_representatives(
                    data["rows"], cached=data["cached"], updated_at=data["updated_at"],
                    workbook=data["workbook"],
                )
            elif kind == "reconstruction_done":
                self._show_reconstruction(payload)  # type: ignore[arg-type]
            elif kind == "error":
                self._busy(False, str(payload))
                messagebox.showerror("操作失败", str(payload))
        self.after(100, self._poll_messages)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state=tk.DISABLED)

    def rescale_figure(self, dpi: float) -> None:
        if self.current_result is None or self.preview_canvas is None:
            return
        self.current_result.figure.set_dpi(dpi)
        self.preview_canvas.draw_idle()
        if self.comparison_figure is not None and self.comparison_canvas is not None:
            self.comparison_figure.set_dpi(dpi)
            self.comparison_canvas.draw_idle()
