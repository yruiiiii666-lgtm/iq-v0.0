from __future__ import annotations

import ctypes
import csv
import json
import os
import queue
import threading
from pathlib import Path


def enable_high_dpi() -> None:
    """Enable native per-monitor rendering before Tk creates any Windows handles."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


enable_high_dpi()

BASE_UI_DPI = 96.0
COMPACT_UI_DPI = 120.0
MAX_UI_DPI = 144.0

UI_COLORS = {
    "app_bg": "#e8eef5",
    "surface": "#ffffff",
    "surface_alt": "#f3f6f9",
    "surface_tint": "#eaf2fb",
    "border": "#c9d4df",
    "border_strong": "#aebdcb",
    "text": "#18232e",
    "text_muted": "#59697a",
    "primary": "#245da8",
    "primary_hover": "#1e4f91",
    "primary_pressed": "#183f74",
    "primary_soft": "#dbe9f8",
    "teal": "#0f766e",
    "amber": "#b7791f",
    "danger": "#c2414b",
    "selection": "#2f6fb0",
}

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import numpy as np
from cycler import cycler
from matplotlib import rcParams
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from iq_embedded import FIGURE_BUILDERS, AnalysisData, prepare_analysis_data, summary_text
from iq_reader import IQRecording, discover_recordings, read_iq_window, recording_from_paths
from plot_iq import ALL_PLOTS
from segment_detector import DetectionResult, detect_representative_segments, result_text
from region_spectrum_features import RegionSpectrumResult, analyze_region_spectrum
from environment_spectrum import (
    BAND_ORDER, SpectrumGroup, SpectrumResult, aggregate_max_hold, discover_spectrum_groups,
    load_low_frequency_magnetic_spectrum, spectrum_result_text,
)
from spectrum_feature_library import (
    ALL_BAND,
    BuildResult,
    ComparisonResult,
    FeatureRecord,
    SpectrumFeatureAnalysis,
    analyze_spectrum_features,
    build_feature_library,
    compare_feature_records,
    library_filter_values,
    list_feature_records,
    load_feature_spectrum,
)
from scene_catalog import (
    SCENE_TYPES,
    AssociationImportResult,
    IQLocationLink,
    SceneLocation,
    import_association_csv,
    initialize_scene_catalog,
    link_iq_recording,
    list_linked_iq,
    list_linked_iq_details,
    list_scene_locations,
    refresh_iq_link_paths,
    scene_filter_values,
    sync_association_locations,
    unlink_iq_recording,
    write_association_template,
)
from reconstruction_module import ReconstructionModule
from playback_module import PlaybackModule
from runtime_paths import application_dir, default_data_dir


PLOT_LABELS = {
    "time": "时域",
    "constellation": "星座图",
    "spectrum": "频谱",
    "spectrogram": "时频图",
    "histogram": "直方图",
    "summary": "摘要",
}


class ScrollableImage(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.photo: tk.PhotoImage | None = None
        self.canvas = tk.Canvas(self, bg="#f8fafc", highlightthickness=0)
        ybar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        xbar = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def set_image(self, path: Path) -> None:
        self.canvas.delete("all")
        self.photo = tk.PhotoImage(file=str(path))
        self.canvas.create_image(16, 16, image=self.photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, self.photo.width() + 32, self.photo.height() + 32))


class ImageGallery(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.photos: list[tk.PhotoImage] = []
        self.canvas = tk.Canvas(self, bg="#ffffff", highlightthickness=0)
        self.content = ttk.Frame(self.canvas)
        ybar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=ybar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.content.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_canvas_width)
        self.set_message("还没有加载图像。请点击“生成并查看”。")

    def _sync_scroll_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_canvas_width(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        self.photos.clear()

    def set_message(self, text: str) -> None:
        self.clear()
        ttk.Label(
            self.content,
            text=text,
            foreground="#64748b",
            font=("Segoe UI", 11),
            padding=(16, 16),
        ).pack(anchor="w")

    def add_image(self, title: str, path: Path, max_width: int = 760) -> None:
        original = tk.PhotoImage(file=str(path))
        factor = max(1, (original.width() + max_width - 1) // max_width)
        photo = original.subsample(factor, factor) if factor > 1 else original
        self.photos.append(photo)

        card = ttk.Frame(self.content, padding=(12, 12, 12, 18))
        card.pack(fill=tk.X, anchor="n")
        ttk.Label(card, text=title, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        label = ttk.Label(card, image=photo)
        label.pack(anchor="w")
        ttk.Label(card, text=str(path), foreground="#64748b").pack(anchor="w", pady=(6, 0))


class FigureTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, figure, compact: bool = True) -> None:
        super().__init__(parent)
        self.figure = figure
        self._reveal_after_id: str | None = None
        self._canvas_visible = False
        self.canvas = FigureCanvasTkAgg(figure, master=self)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        self.canvas_widget = self.canvas.get_tk_widget()
        if compact:
            # Do not map the canvas until the tab has its final geometry. This
            # avoids both a large first frame and manual high-DPI conversion.
            self.canvas_widget.configure(width=360, height=220)
            self.bind("<Map>", lambda _event: self._schedule_reveal(), add="+")
        else:
            self._show_canvas()

    def _schedule_reveal(self) -> None:
        if self._canvas_visible:
            return
        if self._reveal_after_id is not None:
            self.after_cancel(self._reveal_after_id)
        self._reveal_after_id = self.after(30, self._show_canvas)

    def _show_canvas(self) -> None:
        self._reveal_after_id = None
        if self._canvas_visible or not self.winfo_exists():
            return
        self.update_idletasks()
        width = max(320, self.winfo_width() - 28)
        height = max(220, self.winfo_height() - self.toolbar.winfo_height() - 22)
        self.canvas_widget.configure(width=width, height=height)
        self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=14, pady=(8, 14))
        self._canvas_visible = True
        self.after_idle(self._trigger_native_resize)

    def _trigger_native_resize(self) -> None:
        """Let TkAgg perform its own DPI-aware resize after the canvas is mapped."""
        if not self._canvas_visible or not self.canvas_widget.winfo_exists():
            return
        self.canvas_widget.update_idletasks()
        width = self.canvas_widget.winfo_width()
        height = self.canvas_widget.winfo_height()
        if width < 80 or height < 80:
            return
        self.canvas_widget.event_generate("<Configure>", width=width, height=height)
        self.canvas.draw_idle()


class PlaybackTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: "IQAnalyzerApp") -> None:
        super().__init__(parent)
        self.app = app
        self.recording: IQRecording | None = None
        self.running = False
        self.current_sample = 0
        self.loop_start_sample = 0
        self.loop_end_sample = 0
        self.after_id: str | None = None

        self.figure = Figure(figsize=(8.0, 4.8), dpi=app.display_dpi, constrained_layout=True)
        self.ax = self.figure.subplots()
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.TOP, fill=tk.X)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._draw_empty()

    def _draw_empty(self) -> None:
        self.ax.clear()
        self.ax.text(0.5, 0.5, "请选择一组 IQ 数据，然后点击“播放”。", ha="center", va="center", transform=self.ax.transAxes)
        self.ax.set_axis_off()
        self.canvas.draw_idle()

    def configure_recording(self, recording: IQRecording | None) -> None:
        self.stop()
        self.recording = recording
        if recording is None:
            self._draw_empty()
        else:
            self.loop_start_sample = 0
            self.loop_end_sample = recording.total_samples
            self.current_sample = 0
            self._draw_empty()

    def start(self) -> None:
        recording = self.app.selected_recording()
        if recording is None:
            messagebox.showwarning("未选择数据", "请先选择一组 IQ 数据。")
            return

        try:
            start_s = float(self.app.play_start_sec.get() or "0")
            duration_s = None if not self.app.play_duration_sec.get().strip() else float(self.app.play_duration_sec.get())
            window_ms = float(self.app.play_window_ms.get())
            step_ms = float(self.app.play_step_ms.get())
            speed = float(self.app.play_speed.get())
            fps = float(self.app.play_fps.get())
            max_points = int(self.app.play_max_points.get())
            carrier_mhz = float(self.app.play_carrier_mhz.get() or "1")
        except ValueError as exc:
            messagebox.showerror("播放参数无效", str(exc))
            return

        if window_ms <= 0 or step_ms <= 0 or speed <= 0 or fps <= 0 or max_points <= 1:
            messagebox.showerror("播放参数无效", "窗口、步进、速度、FPS 和点数都必须为正数。")
            return

        self.recording = recording
        self.loop_start_sample = int(max(start_s, 0.0) * recording.sample_rate_hz)
        if duration_s is None:
            self.loop_end_sample = recording.total_samples
        else:
            self.loop_end_sample = self.loop_start_sample + int(max(duration_s, 0.0) * recording.sample_rate_hz)
        self.loop_start_sample = max(0, min(self.loop_start_sample, recording.total_samples - 1))
        self.loop_end_sample = max(self.loop_start_sample + 1, min(self.loop_end_sample, recording.total_samples))
        self.current_sample = self.loop_start_sample
        self.running = True
        self._draw_frame(window_ms, step_ms, speed, fps, max_points, carrier_mhz)

    def pause(self) -> None:
        self.running = False
        if self.after_id is not None:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.app.status_var.set("播放已暂停。")

    def stop(self) -> None:
        self.running = False
        if self.after_id is not None:
            self.after_cancel(self.after_id)
            self.after_id = None
        self.current_sample = self.loop_start_sample
        self.app.status_var.set("播放已停止。")

    def _draw_frame(self, window_ms: float, step_ms: float, speed: float, fps: float, max_points: int, carrier_mhz: float) -> None:
        if not self.running or self.recording is None:
            return

        recording = self.recording
        window_samples = max(2, int(window_ms * 1e-3 * recording.sample_rate_hz))
        step_samples = max(1, int(step_ms * speed * 1e-3 * recording.sample_rate_hz))
        if self.current_sample + window_samples > self.loop_end_sample:
            if self.app.play_loop_enabled.get():
                self.current_sample = self.loop_start_sample
            else:
                self.pause()
                self.current_sample = self.loop_start_sample
                self.app.status_var.set("播放完成。")
                return

        times, iq, stride = read_iq_window(recording, self.current_sample, window_samples, max_points=max_points)
        if iq.size == 0:
            self.current_sample = self.loop_start_sample
            return

        relative_time_ms = (times - times[0]) * 1e3
        mode = self.app.play_mode.get()
        self.ax.clear()
        self.ax.grid(True, alpha=0.25)

        if mode == "I/Q 分量":
            self.ax.plot(relative_time_ms, iq.real, label="I", linewidth=0.9)
            self.ax.plot(relative_time_ms, iq.imag, label="Q", linewidth=0.9, alpha=0.85)
            self.ax.set_ylabel("归一化幅度")
        elif mode == "幅值 |IQ|":
            self.ax.plot(relative_time_ms, np.abs(iq), color="tab:green", label="|IQ|", linewidth=0.9)
            self.ax.set_ylabel("归一化幅值")
        elif mode == "合成实值波形":
            t_local = times - times[0]
            carrier_hz = carrier_mhz * 1e6
            waveform = iq.real * np.cos(2 * np.pi * carrier_hz * t_local) - iq.imag * np.sin(2 * np.pi * carrier_hz * t_local)
            self.ax.plot(relative_time_ms, waveform, color="tab:purple", label=f"实值波形，可视载波 {carrier_mhz:g} MHz", linewidth=0.9)
            self.ax.set_ylabel("归一化幅度")
        else:
            self.ax.plot(relative_time_ms, iq.real, label="I", linewidth=0.9)
            self.ax.set_ylabel("归一化幅度")

        start_s = self.current_sample / recording.sample_rate_hz
        self.ax.set_title(f"{recording.stem} 回放，起点 {start_s:.6f} s，抽取步长 {stride}")
        self.ax.set_xlabel("当前窗口内时间 (ms)")
        self.ax.set_ylim(-1.0, 1.0)
        self.ax.legend(loc="upper right")
        self.canvas.draw_idle()
        self.app.status_var.set(f"正在播放 {recording.stem}：{start_s:.6f} s，速度 {speed:g}x")

        self.current_sample += step_samples
        interval_ms = max(10, int(1000 / fps))
        self.after_id = self.after(interval_ms, lambda: self._draw_frame(window_ms, step_ms, speed, fps, max_points, carrier_mhz))


class IQAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.display_dpi = self._configure_dpi()
        self._scale_guard_after_id: str | None = None
        self._shell_layout_after_id: str | None = None
        self.compact_shell: bool | None = None
        self.title("电磁环境数据分析软件")
        self.geometry("1180x760")
        self.minsize(980, 640)

        self.app_dir = application_dir()
        initial_data_dir = default_data_dir()
        initial_output_dir = self.app_dir / "output"
        initial_data_dir.mkdir(parents=True, exist_ok=True)
        initial_output_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = tk.StringVar(value=str(initial_data_dir.resolve()))
        self.out_dir = tk.StringVar(value=str(initial_output_dir.resolve()))
        self.spectrum_dir = tk.StringVar(value=str((initial_data_dir / "环境频谱测试").resolve()))
        self.spectrum_city = tk.StringVar()
        self.spectrum_point = tk.StringVar()
        self.spectrum_polarization = tk.StringVar()
        self.spectrum_band = tk.StringVar(value="30M-6G (All)")
        self.feature_db_path = tk.StringVar(value=str((initial_output_dir / "spectrum_feature_library.db").resolve()))
        self.feature_city = tk.StringVar(value="全部")
        self.feature_scene = tk.StringVar(value="全部")
        self.feature_polarization = tk.StringVar(value="全部")
        self.feature_band = tk.StringVar(value=ALL_BAND)
        self.feature_keyword = tk.StringVar()
        self.feature_selection_text = tk.StringVar(value="已选择 0 个测点")
        self.scene_filter = tk.StringVar(value="全部")
        self.scene_city_filter = tk.StringVar(value="全部")
        self.scene_keyword = tk.StringVar()
        self.scene_assignment = tk.StringVar(value="未分类")
        self.scene_notes = tk.StringVar()
        self.scene_selected_text = tk.StringVar(value="尚未选择地点")
        self.scene_polarization = tk.StringVar(value="垂直极化")
        self.scene_band = tk.StringVar(value=ALL_BAND)
        self.scene_iq_recording = tk.StringVar()
        self.scene_iq_count_text = tk.StringVar(value="当前地点关联 0 组 IQ 数据")
        self.region_frequency_tolerance_mhz = tk.StringVar(value="2")
        self.region_minimum_probability = tk.StringVar(value="0.3")
        self.start_sec = tk.StringVar(value="0")
        self.duration_sec = tk.StringVar(value="0.05")
        self.max_points = tk.StringVar(value="200000")
        self.spectrogram_points = tk.StringVar(value="1048576")
        self.detect_window_ms = tk.StringVar(value="2")
        self.detect_interval_ms = tk.StringVar(value="10")
        self.detect_extract_ms = tk.StringVar(value="20")
        self.detect_max_windows = tk.StringVar(value="3000")
        self.detect_progress = tk.DoubleVar(value=0.0)
        self.detect_progress_text = tk.StringVar(value="等待开始")
        self.detect_save_output = tk.BooleanVar(value=False)
        self.play_start_sec = tk.StringVar(value="0")
        self.play_duration_sec = tk.StringVar(value="0.05")
        self.play_window_ms = tk.StringVar(value="1")
        self.play_step_ms = tk.StringVar(value="0.2")
        self.play_speed = tk.StringVar(value="5")
        self.play_fps = tk.StringVar(value="20")
        self.play_max_points = tk.StringVar(value="4000")
        self.play_carrier_mhz = tk.StringVar(value="1")
        self.play_mode = tk.StringVar(value="合成实值波形")
        self.play_loop_enabled = tk.BooleanVar(value=False)
        self.recording_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.plot_vars = {name: tk.BooleanVar(value=True) for name in ALL_PLOTS}
        self.recordings: list[IQRecording] = []
        self.spectrum_groups: list[SpectrumGroup] = []
        self.feature_records: list[FeatureRecord] = []
        self.feature_record_by_item: dict[str, FeatureRecord] = {}
        self.feature_location_by_item: dict[str, SceneLocation] = {}
        self.scene_locations: list[SceneLocation] = []
        self.scene_location_by_item: dict[str, SceneLocation] = {}
        self.selected_scene_location: SceneLocation | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.figure_tabs: dict[str, FigureTab] = {}
        self.spectrum_figure_tab: FigureTab | None = None
        self.feature_comparison_tab: FigureTab | None = None
        self.scene_spectrum_tab: ttk.Frame | FigureTab | None = None
        self.scene_magnetic_spectrum_tab: FigureTab | None = None
        self.scene_feature_figure_tab: FigureTab | None = None
        self.region_feature_figure_tab: FigureTab | None = None
        self.current_region_result: RegionSpectrumResult | None = None
        self.gallery_tab: ImageGallery | None = None
        self.playback_tab: PlaybackTab | None = None
        self.detection_figure_tab: FigureTab | None = None
        self.reconstruction_module: ReconstructionModule | None = None
        self.device_playback_module: PlaybackModule | None = None

        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._shutdown_application)
        self.bind("<Configure>", self._schedule_scale_guard, add="+")
        self.bind("<Configure>", self._schedule_shell_layout, add="+")
        self.refresh_recordings()
        self.refresh_environment_spectra()
        self.refresh_feature_library()
        self.refresh_scene_catalog()
        self.after(150, self._drain_messages)

    def _shutdown_application(self) -> None:
        if self.device_playback_module is not None:
            self.device_playback_module.shutdown()
        self.destroy()

    def _configure_dpi(self) -> float:
        # Follow the current monitor's Windows scale while keeping very high
        # scaling from making the working area excessively crowded.
        dpi = self._target_ui_dpi()
        self.tk.call("tk", "scaling", dpi / 72.0)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                font = tkfont.nametofont(name)
                font.configure(family="Segoe UI", size=11)
            except tk.TclError:
                pass
        rcParams.update(
            {
                "font.family": "sans-serif",
                "font.sans-serif": ["Microsoft YaHei", "Noto Sans SC", "Segoe UI", "Arial"],
                "font.size": 11,
                "figure.dpi": dpi,
                "savefig.dpi": dpi,
                "figure.facecolor": UI_COLORS["surface"],
                "savefig.facecolor": UI_COLORS["surface"],
                "axes.facecolor": "#fbfcfe",
                "axes.edgecolor": UI_COLORS["border_strong"],
                "axes.labelcolor": UI_COLORS["text"],
                "axes.titlecolor": UI_COLORS["text"],
                "axes.prop_cycle": cycler(
                    color=[
                        UI_COLORS["primary"],
                        UI_COLORS["teal"],
                        "#d97706",
                        "#b45367",
                        "#5b6fb5",
                        "#6b7280",
                    ]
                ),
                "xtick.color": UI_COLORS["text_muted"],
                "ytick.color": UI_COLORS["text_muted"],
                "grid.color": "#d8e1ea",
                "grid.alpha": 0.72,
                "legend.facecolor": UI_COLORS["surface"],
                "legend.edgecolor": UI_COLORS["border"],
                "text.antialiased": True,
                "lines.antialiased": True,
            }
        )
        return dpi

    def _target_ui_dpi(self) -> float:
        dpi = BASE_UI_DPI
        try:
            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes = [ctypes.c_void_p]
            get_dpi.restype = ctypes.c_uint
            measured = float(get_dpi(self.winfo_id()))
            if measured > 0:
                dpi = measured
        except (AttributeError, OSError, tk.TclError):
            pass

        # A high Windows scale on a short display can consume most of the
        # vertical workspace. Select the ceiling from the monitor work area,
        # so 1080p-class screens stay compact while taller displays remain
        # comfortably readable.
        _width, work_height = self._monitor_work_area()
        if 0 < work_height <= 900:
            dpi_ceiling = BASE_UI_DPI
        elif 0 < work_height <= 1200:
            dpi_ceiling = COMPACT_UI_DPI
        else:
            dpi_ceiling = MAX_UI_DPI
        return min(dpi_ceiling, max(BASE_UI_DPI, dpi))

    def _monitor_work_area(self) -> tuple[int, int]:
        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", ctypes.c_ulong),
            ]

        try:
            user32 = ctypes.windll.user32
            monitor_from_window = user32.MonitorFromWindow
            monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            monitor_from_window.restype = ctypes.c_void_p
            monitor = monitor_from_window(self.winfo_id(), 2)  # MONITOR_DEFAULTTONEAREST
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return info.rcWork.right - info.rcWork.left, info.rcWork.bottom - info.rcWork.top
        except (AttributeError, OSError, tk.TclError):
            pass
        return 0, 0

    def _schedule_scale_guard(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._scale_guard_after_id is not None:
            self.after_cancel(self._scale_guard_after_id)
        self._scale_guard_after_id = self.after(100, self._enforce_consistent_ui_scale)

    def _schedule_shell_layout(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._shell_layout_after_id is not None:
            self.after_cancel(self._shell_layout_after_id)
        self._shell_layout_after_id = self.after(80, self._apply_shell_layout)

    def _apply_shell_layout(self) -> None:
        self._shell_layout_after_id = None
        compact = self.winfo_height() < 820
        if self.compact_shell == compact:
            return
        self.compact_shell = compact
        if compact:
            self.header.configure(padding=(18, 5, 18, 3))
            self.header_title.configure(style="CompactHeader.TLabel")
            self.header_subtitle.pack_forget()
            self.module_notebook.configure(style="Compact.Module.TNotebook")
        else:
            self.header.configure(padding=(18, 14, 18, 8))
            self.header_title.configure(style="Header.TLabel")
            self.header_subtitle.pack(anchor="w", pady=(2, 0))
            self.module_notebook.configure(style="Module.TNotebook")

    def _enforce_consistent_ui_scale(self) -> None:
        self._scale_guard_after_id = None
        target_dpi = self._target_ui_dpi()
        target_scaling = target_dpi / 72.0
        current = float(self.tk.call("tk", "scaling"))
        dpi_changed = abs(self.display_dpi - target_dpi) >= 0.5
        if abs(current - target_scaling) < 0.01 and not dpi_changed:
            return
        self.display_dpi = target_dpi
        self.tk.call("tk", "scaling", target_scaling)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family="Segoe UI", size=11)
            except tk.TclError:
                pass
        rcParams["figure.dpi"] = target_dpi
        rcParams["savefig.dpi"] = target_dpi
        self._configure_style()
        self._rescale_open_figures(target_dpi)
        if self.reconstruction_module is not None:
            self.reconstruction_module.rescale_figure(target_dpi)
        if self.device_playback_module is not None:
            self.device_playback_module.rescale_figure(target_dpi)

    def _rescale_open_figures(self, dpi: float) -> None:
        tabs: list[FigureTab | PlaybackTab | None] = [
            *self.figure_tabs.values(),
            self.spectrum_figure_tab,
            self.feature_comparison_tab,
            self.scene_spectrum_tab if isinstance(self.scene_spectrum_tab, FigureTab) else None,
            self.scene_magnetic_spectrum_tab,
            self.scene_feature_figure_tab,
            self.region_feature_figure_tab,
            self.playback_tab,
            self.detection_figure_tab,
        ]
        seen: set[int] = set()
        for tab in tabs:
            if tab is None or not hasattr(tab, "figure") or id(tab.figure) in seen:
                continue
            seen.add(id(tab.figure))
            tab.figure.set_dpi(dpi)
            if isinstance(tab, FigureTab):
                tab._trigger_native_resize()
            else:
                tab.canvas.draw_idle()

    def _configure_style(self) -> None:
        colors = UI_COLORS
        self.configure(bg=colors["app_bg"])
        self.option_add("*selectBackground", colors["selection"])
        self.option_add("*selectForeground", "#ffffff")
        self.option_add("*insertBackground", colors["text"])

        style = ttk.Style(self)
        themes = style.theme_names()
        style.theme_use("clam" if "clam" in themes else themes[0])
        style.configure(
            ".",
            font=("Segoe UI", 11),
            background=colors["surface"],
            foreground=colors["text"],
        )
        style.configure("TFrame", background=colors["surface"])
        style.configure("Header.TFrame", background=colors["app_bg"])
        style.configure("Footer.TFrame", background=colors["app_bg"])
        style.configure("Panel.TFrame", background=colors["surface"])
        style.configure("TLabel", background=colors["surface"], foreground=colors["text"])
        style.configure(
            "Header.TLabel",
            background=colors["app_bg"],
            foreground=colors["text"],
            font=("Segoe UI", 20, "bold"),
        )
        style.configure(
            "HeaderSubtle.TLabel",
            background=colors["app_bg"],
            foreground=colors["text_muted"],
        )
        style.configure(
            "CompactHeader.TLabel",
            background=colors["app_bg"],
            foreground=colors["text"],
            font=("Segoe UI", 16, "bold"),
        )
        style.configure("Subtle.TLabel", background=colors["surface"], foreground=colors["text_muted"])
        style.configure("Footer.TLabel", background=colors["app_bg"], foreground=colors["text_muted"])
        style.configure(
            "Value.TLabel",
            background=colors["surface"],
            foreground=colors["text"],
            font=("Segoe UI", 11, "bold"),
        )

        style.configure(
            "Card.TLabelframe",
            background=colors["surface"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=colors["surface"],
            foreground=colors["primary"],
            font=("Segoe UI", 11, "bold"),
        )

        style.configure(
            "TButton",
            background=colors["surface_alt"],
            foreground=colors["text"],
            bordercolor=colors["border_strong"],
            lightcolor=colors["surface_alt"],
            darkcolor=colors["border_strong"],
            padding=(8, 1),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("pressed", colors["primary_soft"]), ("active", colors["surface_tint"])],
            bordercolor=[("focus", colors["primary"]), ("active", colors["primary"])],
            foreground=[("disabled", "#8b98a6")],
        )
        style.configure(
            "Accent.TButton",
            background=colors["primary"],
            foreground="#ffffff",
            bordercolor=colors["primary"],
            lightcolor=colors["primary"],
            darkcolor=colors["primary_pressed"],
            font=("Segoe UI", 11, "bold"),
            padding=(9, 2),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", colors["border"]),
                ("pressed", colors["primary_pressed"]),
                ("active", colors["primary_hover"]),
            ],
            bordercolor=[
                ("disabled", colors["border"]),
                ("pressed", colors["primary_pressed"]),
                ("active", colors["primary_hover"]),
            ],
            foreground=[("disabled", "#f4f6f8"), ("!disabled", "#ffffff")],
        )

        style.configure(
            "TEntry",
            fieldbackground=colors["surface"],
            foreground=colors["text"],
            bordercolor=colors["border_strong"],
            insertcolor=colors["text"],
            padding=(5, 1),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", colors["primary"])],
            fieldbackground=[("disabled", colors["surface_alt"]), ("readonly", colors["surface_alt"])],
            foreground=[("disabled", colors["text_muted"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["surface"],
            background=colors["surface_alt"],
            foreground=colors["text"],
            bordercolor=colors["border_strong"],
            arrowcolor=colors["primary"],
            padding=(5, 1),
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", colors["primary"])],
            fieldbackground=[("readonly", colors["surface"])],
            selectbackground=[("readonly", colors["surface"])],
            selectforeground=[("readonly", colors["text"])],
        )
        style.configure("TCheckbutton", background=colors["surface"], foreground=colors["text"])
        style.configure("TRadiobutton", background=colors["surface"], foreground=colors["text"])
        style.map(
            "TCheckbutton",
            background=[("active", colors["surface_tint"])],
            foreground=[("active", colors["primary"])],
        )
        style.map(
            "TRadiobutton",
            background=[("active", colors["surface_tint"])],
            foreground=[("active", colors["primary"])],
        )

        style.configure(
            "TNotebook",
            background=colors["app_bg"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=colors["surface_alt"],
            foreground=colors["text_muted"],
            bordercolor=colors["border"],
            lightcolor=colors["surface_alt"],
            padding=(12, 6),
            font=("Segoe UI", 11),
        )
        style.configure("Module.TNotebook", background=colors["app_bg"], borderwidth=0)
        style.configure(
            "Module.TNotebook.Tab",
            padding=(12, 6),
            font=("Segoe UI", 11),
        )
        style.configure("Compact.Module.TNotebook", background=colors["app_bg"], borderwidth=0)
        style.configure(
            "Compact.Module.TNotebook.Tab",
            padding=(10, 3),
            font=("Segoe UI", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", colors["surface"]), ("active", colors["primary_soft"])],
            foreground=[("selected", colors["primary"]), ("active", colors["primary_hover"])],
            lightcolor=[("selected", colors["primary"])],
            expand=[("selected", (0, 0, 0, 2))],
        )
        style.configure(
            "Treeview",
            background=colors["surface"],
            fieldbackground=colors["surface"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", colors["selection"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            background=colors["surface_tint"],
            foreground="#203b57",
            bordercolor=colors["border"],
            lightcolor=colors["surface_tint"],
            darkcolor=colors["border"],
            relief="flat",
        )
        style.map(
            "Treeview.Heading",
            background=[("active", colors["primary_soft"])],
            foreground=[("active", colors["primary_hover"])],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=colors["teal"],
            troughcolor=colors["surface_tint"],
            bordercolor=colors["border"],
            lightcolor=colors["teal"],
            darkcolor=colors["teal"],
        )
        style.configure(
            "TScrollbar",
            background=colors["surface_alt"],
            troughcolor=colors["surface"],
            bordercolor=colors["border"],
            arrowcolor=colors["text_muted"],
        )
        style.configure("TPanedwindow", background=colors["app_bg"], sashwidth=6)
        style.configure("TSeparator", background=colors["border"])

        # Treeview keeps a small system row height on some high-DPI displays.
        # Derive it from the rendered font so Chinese text never touches adjacent rows.
        self.feature_table_font = tkfont.Font(self, family="Microsoft YaHei", size=10)
        self.feature_heading_font = tkfont.Font(self, family="Microsoft YaHei", size=10, weight="bold")
        feature_row_height = max(30, self.feature_table_font.metrics("linespace") + 12)
        style.configure(
            "Feature.Treeview",
            font=self.feature_table_font,
            rowheight=feature_row_height,
            background=colors["surface"],
            fieldbackground=colors["surface"],
            foreground=colors["text"],
        )
        style.configure(
            "Feature.Treeview.Heading",
            font=self.feature_heading_font,
            padding=(8, 7),
            background=colors["surface_tint"],
            foreground="#203b57",
        )
        peak_row_height = max(32, self.feature_table_font.metrics("linespace") + 14)
        style.configure(
            "Peak.Treeview",
            font=self.feature_table_font,
            rowheight=peak_row_height,
            background=colors["surface"],
            fieldbackground=colors["surface"],
            foreground=colors["text"],
        )
        style.configure(
            "Peak.Treeview.Heading",
            font=self.feature_heading_font,
            padding=(8, 8),
            background=colors["surface_tint"],
            foreground="#203b57",
        )
        for tree_style in ("Feature.Treeview", "Peak.Treeview"):
            style.map(
                tree_style,
                background=[("selected", colors["selection"])],
                foreground=[("selected", "#ffffff")],
            )

    def _build_ui(self) -> None:
        self.header = ttk.Frame(self, style="Header.TFrame", padding=(18, 14, 18, 8))
        self.header.pack(fill=tk.X)
        self.header_title = ttk.Label(
            self.header, text="电磁环境数据分析软件", style="Header.TLabel"
        )
        self.header_title.pack(anchor="w")
        self.header_subtitle = ttk.Label(
            self.header,
            text="查看 IQ 与环境频谱，提取典型场景特征，并生成可导出的重构信号。",
            style="HeaderSubtle.TLabel",
        )
        self.header_subtitle.pack(anchor="w", pady=(2, 0))

        self.module_notebook = ttk.Notebook(self, style="Module.TNotebook")
        self.module_notebook.pack(fill=tk.BOTH, expand=True, padx=18, pady=(6, 12))

        iq_module = ttk.Frame(self.module_notebook, padding=8)
        spectrum_module = ttk.Frame(self.module_notebook, padding=8)
        feature_module = ttk.Frame(self.module_notebook, padding=8)
        scene_module = ttk.Frame(self.module_notebook, padding=8)
        reconstruction_module = ttk.Frame(self.module_notebook, padding=0)
        playback_module = ttk.Frame(self.module_notebook, padding=0)
        self.module_notebook.add(iq_module, text="IQ 数据")
        self.module_notebook.add(spectrum_module, text="环境频谱")
        self.module_notebook.add(scene_module, text="场景分类")
        self.module_notebook.add(feature_module, text="频谱特征库")
        self.module_notebook.add(reconstruction_module, text="信号重构")
        self.module_notebook.add(playback_module, text="设备回放")
        self.feature_module = feature_module
        self.scene_module = scene_module

        iq_main = ttk.PanedWindow(iq_module, orient=tk.HORIZONTAL)
        iq_main.pack(fill=tk.BOTH, expand=True)
        iq_left = ttk.Frame(iq_main, style="Panel.TFrame")
        iq_right = ttk.Frame(iq_main, style="Panel.TFrame", padding=10)
        iq_main.add(iq_left, weight=0)
        iq_main.add(iq_right, weight=1)
        iq_controls = self._make_scrollable_controls(iq_left)
        self._build_controls(iq_controls)
        self._build_results(iq_right)

        spectrum_main = ttk.PanedWindow(spectrum_module, orient=tk.HORIZONTAL)
        spectrum_main.pack(fill=tk.BOTH, expand=True)
        spectrum_left = ttk.Frame(spectrum_main, style="Panel.TFrame")
        spectrum_right = ttk.Frame(spectrum_main, style="Panel.TFrame", padding=10)
        spectrum_main.add(spectrum_left, weight=0)
        spectrum_main.add(spectrum_right, weight=1)
        spectrum_controls = self._make_scrollable_controls(spectrum_left)
        self._build_spectrum_controls(spectrum_controls)
        self._build_spectrum_results(spectrum_right)

        feature_main = ttk.PanedWindow(feature_module, orient=tk.HORIZONTAL)
        feature_main.pack(fill=tk.BOTH, expand=True)
        feature_left = ttk.Frame(feature_main, style="Panel.TFrame")
        feature_right = ttk.Frame(feature_main, style="Panel.TFrame", padding=10)
        feature_main.add(feature_left, weight=0)
        feature_main.add(feature_right, weight=1)
        feature_controls = self._make_scrollable_controls(feature_left)
        self._build_feature_controls(feature_controls)
        self._build_feature_results(feature_right)

        scene_main = ttk.PanedWindow(scene_module, orient=tk.HORIZONTAL)
        scene_main.pack(fill=tk.BOTH, expand=True)
        scene_left = ttk.Frame(scene_main, style="Panel.TFrame")
        scene_right = ttk.Frame(scene_main, style="Panel.TFrame", padding=10)
        scene_main.add(scene_left, weight=0)
        scene_main.add(scene_right, weight=1)
        scene_controls = self._make_scrollable_controls(scene_left)
        self._build_scene_controls(scene_controls)
        self._build_scene_results(scene_right)

        self.reconstruction_module = ReconstructionModule(
            reconstruction_module,
            database_var=self.feature_db_path,
            output_var=self.out_dir,
            status_var=self.status_var,
            iq_root_var=self.data_dir,
        )
        self.reconstruction_module.pack(fill=tk.BOTH, expand=True)
        self.device_playback_module = PlaybackModule(
            playback_module,
            result_provider=lambda: self.reconstruction_module.current_result if self.reconstruction_module else None,
            recording_provider=self.selected_scene_recording,
            database_var=self.feature_db_path,
            output_var=self.out_dir,
            status_var=self.status_var,
            recording_context_provider=self.selected_scene_recording_context,
        )
        self.device_playback_module.pack(fill=tk.BOTH, expand=True)
        self.reconstruction_module.on_result_ready = self.device_playback_module.accept_reconstruction

        status = ttk.Frame(self, style="Footer.TFrame", padding=(18, 0, 18, 12))
        status.pack(fill=tk.X)
        ttk.Label(status, textvariable=self.status_var, style="Footer.TLabel").pack(side=tk.LEFT)
        self.after_idle(self._apply_shell_layout)

    def _make_scrollable_controls(self, parent: ttk.Frame) -> ttk.Frame:
        parent.configure(width=380)
        parent.pack_propagate(False)
        canvas = tk.Canvas(parent, bg=UI_COLORS["surface"], highlightthickness=0, width=370)
        ybar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=ybar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ybar.pack(side=tk.RIGHT, fill=tk.Y)

        controls = ttk.Frame(canvas, style="Panel.TFrame", padding=14)
        window_id = canvas.create_window((0, 0), window=controls, anchor="nw")

        def sync_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        controls.bind("<Configure>", sync_scroll_region)
        canvas.bind("<Configure>", sync_width)

        def is_inside_controls(widget: tk.Misc | None) -> bool:
            while widget is not None:
                if widget in (canvas, controls):
                    return True
                widget = getattr(widget, "master", None)
            return False

        def scroll_controls(event: tk.Event) -> str | None:
            hovered = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            if not is_inside_controls(hovered):
                return None
            delta = int(event.delta / 120) if event.delta else 0
            if delta:
                canvas.yview_scroll(-delta * 3, "units")
                return "break"
            return None

        self.bind_all("<MouseWheel>", scroll_controls, add="+")
        return controls

    def _build_controls(self, parent: ttk.Frame) -> None:
        parent.configure(width=360)

        dirs = ttk.LabelFrame(parent, text="文件夹", style="Card.TLabelframe", padding=10)
        dirs.pack(fill=tk.X)
        self._path_row(dirs, "数据", self.data_dir, self.refresh_recordings)
        self._path_row(dirs, "输出", self.out_dir, None)

        actions = ttk.LabelFrame(parent, text="运行", style="Card.TLabelframe", padding=10)
        actions.pack(fill=tk.X, pady=(12, 0))
        self.run_button = ttk.Button(actions, text="生成并查看", style="Accent.TButton", command=self.run_analysis)
        self.run_button.pack(fill=tk.X)
        ttk.Button(actions, text="打开输出文件夹", command=self.open_output_folder).pack(fill=tk.X, pady=(8, 0))

        data = ttk.LabelFrame(parent, text="IQ 数据组", style="Card.TLabelframe", padding=10)
        data.pack(fill=tk.X, pady=(12, 0))
        self.recording_combo = ttk.Combobox(data, textvariable=self.recording_var, state="readonly")
        self.recording_combo.pack(fill=tk.X)
        self.recording_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_recording_selected())
        ttk.Button(data, text="刷新数据", command=self.refresh_recordings).pack(fill=tk.X, pady=(8, 0))
        self.details_text = tk.Text(data, height=8, wrap="word", bg="#f8fafc", relief="flat", padx=8, pady=8, font=("Segoe UI", 10))
        self.details_text.pack(fill=tk.X, pady=(8, 0))

        settings = ttk.LabelFrame(parent, text="分析窗口", style="Card.TLabelframe", padding=10)
        settings.pack(fill=tk.X, pady=(12, 0))
        self._entry_row(settings, "起始时间 (s)", self.start_sec, 0)
        self._entry_row(settings, "持续时间 (s)", self.duration_sec, 1)
        self._entry_row(settings, "最大点数", self.max_points, 2)
        self._entry_row(settings, "时频图点数", self.spectrogram_points, 3)

        plots = ttk.LabelFrame(parent, text="分析内容", style="Card.TLabelframe", padding=10)
        plots.pack(fill=tk.X, pady=(12, 0))
        for row, name in enumerate(ALL_PLOTS):
            ttk.Checkbutton(plots, text=PLOT_LABELS[name], variable=self.plot_vars[name]).grid(row=row // 2, column=row % 2, sticky="w", pady=3, padx=(0, 16))

        detection = ttk.LabelFrame(parent, text="功率最强片段检测", style="Card.TLabelframe", padding=10)
        detection.pack(fill=tk.X, pady=(12, 0))
        self._entry_row(detection, "特征窗口 (ms)", self.detect_window_ms, 0)
        self._entry_row(detection, "扫描间隔 (ms)", self.detect_interval_ms, 1)
        self._entry_row(detection, "提取长度 (ms)", self.detect_extract_ms, 2)
        self._entry_row(detection, "最大窗口数", self.detect_max_windows, 3)
        self.detect_button = ttk.Button(detection, text="检测功率最强片段", command=self.run_segment_detection)
        self.detect_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            detection,
            text="保存检测结果文件",
            variable=self.detect_save_output,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.detect_progress_bar = ttk.Progressbar(
            detection, variable=self.detect_progress, maximum=100, mode="determinate"
        )
        self.detect_progress_bar.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(
            detection,
            textvariable=self.detect_progress_text,
            foreground="#64748b",
            wraplength=310,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        playback = ttk.LabelFrame(parent, text="回放预览", style="Card.TLabelframe", padding=10)
        playback.pack(fill=tk.X, pady=(12, 0))
        self._entry_row(playback, "起始时间 (s)", self.play_start_sec, 0)
        self._entry_row(playback, "循环时长 (s)", self.play_duration_sec, 1)
        self._entry_row(playback, "窗口 (ms)", self.play_window_ms, 2)
        self._entry_row(playback, "步进 (ms)", self.play_step_ms, 3)
        self._entry_row(playback, "速度 (x)", self.play_speed, 4)
        self._entry_row(playback, "FPS", self.play_fps, 5)
        self._entry_row(playback, "最大点数", self.play_max_points, 6)
        self._entry_row(playback, "可视载波 (MHz)", self.play_carrier_mhz, 7)
        ttk.Label(playback, text="模式").grid(row=8, column=0, sticky="w", pady=5, padx=(0, 8))
        mode_box = ttk.Combobox(
            playback,
            textvariable=self.play_mode,
            state="readonly",
            values=("合成实值波形", "I/Q 分量", "幅值 |IQ|"),
        )
        mode_box.grid(row=8, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(playback, text="结束后循环", variable=self.play_loop_enabled).grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(playback)
        buttons.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="播放", command=self.start_playback).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(buttons, text="暂停", command=self.pause_playback).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        ttk.Button(buttons, text="停止", command=self.stop_playback).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

    def _build_spectrum_controls(self, parent: ttk.Frame) -> None:
        parent.configure(width=360)
        spectrum = ttk.LabelFrame(parent, text="测点选择", style="Card.TLabelframe", padding=10)
        spectrum.pack(fill=tk.X)
        self._path_row(spectrum, "根目录", self.spectrum_dir, self.refresh_environment_spectra)
        self._path_row(spectrum, "输出", self.out_dir, None)
        row = spectrum.grid_size()[1]
        ttk.Label(spectrum, text="城市").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.spectrum_city_combo = ttk.Combobox(spectrum, textvariable=self.spectrum_city, state="readonly")
        self.spectrum_city_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.spectrum_city_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_spectrum_points())
        row += 1
        ttk.Label(spectrum, text="测点").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.spectrum_point_combo = ttk.Combobox(spectrum, textvariable=self.spectrum_point, state="readonly")
        self.spectrum_point_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.spectrum_point_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_spectrum_polarizations())
        row += 1
        ttk.Label(spectrum, text="极化").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.spectrum_polarization_combo = ttk.Combobox(spectrum, textvariable=self.spectrum_polarization, state="readonly")
        self.spectrum_polarization_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.spectrum_polarization_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_spectrum_bands())
        row += 1
        ttk.Label(spectrum, text="频段").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.spectrum_band_combo = ttk.Combobox(spectrum, textvariable=self.spectrum_band, state="readonly")
        self.spectrum_band_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1
        self.spectrum_button = ttk.Button(spectrum, text="生成最大值保持频谱", command=self.run_environment_spectrum)
        self.spectrum_button.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        explanation = ttk.LabelFrame(parent, text="处理说明", style="Card.TLabelframe", padding=10)
        explanation.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            explanation,
            text="对同一测点的多次扫描进行比对。\n每个频点保留最大的场强值。",
            wraplength=320,
            justify=tk.LEFT,
        ).pack(anchor="w")

    def _build_feature_controls(self, parent: ttk.Frame) -> None:
        parent.configure(width=360)
        library = ttk.LabelFrame(parent, text="特征库建立", style="Card.TLabelframe", padding=10)
        library.pack(fill=tk.X)
        self._path_row(library, "频谱根目录", self.spectrum_dir, self.refresh_environment_spectra)
        row = library.grid_size()[1]
        ttk.Label(library, text="数据库").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(library, textvariable=self.feature_db_path, width=28).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(library, text="...", width=3, command=self.choose_feature_database).grid(row=row, column=2, padx=(6, 0), pady=4)
        library.columnconfigure(1, weight=1)
        row += 1
        self.feature_build_button = ttk.Button(library, text="扫描全部测点并建立特征库", style="Accent.TButton", command=self.run_feature_build)
        self.feature_build_button.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        row += 1
        ttk.Button(library, text="刷新数据库内容", command=self.refresh_feature_library).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        filters = ttk.LabelFrame(parent, text="场景特征筛选", style="Card.TLabelframe", padding=10)
        filters.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(filters, text="场景类型").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.feature_scene_combo = ttk.Combobox(filters, textvariable=self.feature_scene, state="readonly")
        self.feature_scene_combo.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(filters, text="城市").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.feature_city_combo = ttk.Combobox(filters, textvariable=self.feature_city, state="readonly")
        self.feature_city_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(filters, text="极化").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.feature_polarization_combo = ttk.Combobox(filters, textvariable=self.feature_polarization, state="readonly")
        self.feature_polarization_combo.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(filters, text="频段").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.feature_band_combo = ttk.Combobox(filters, textvariable=self.feature_band, state="readonly")
        self.feature_band_combo.grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(filters, text="地点关键字").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        keyword_entry = ttk.Entry(filters, textvariable=self.feature_keyword)
        keyword_entry.grid(row=4, column=1, sticky="ew", pady=4)
        keyword_entry.bind("<Return>", lambda _event: self.refresh_feature_library())
        ttk.Button(filters, text="应用筛选", command=self.refresh_feature_library).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(filters, text="导出当前特征表", style="Accent.TButton", command=self.export_feature_table).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        filters.columnconfigure(1, weight=1)
        for combo in (self.feature_scene_combo, self.feature_city_combo, self.feature_polarization_combo, self.feature_band_combo):
            combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_feature_library(keep_filters=True))

        comparison = ttk.LabelFrame(parent, text="位置特征对比", style="Card.TLabelframe", padding=10)
        comparison.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(comparison, textvariable=self.feature_selection_text).pack(anchor="w")
        buttons = ttk.Frame(comparison)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="全选当前结果", command=self.select_all_feature_rows).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(buttons, text="清除选择", command=self.clear_feature_selection).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self.feature_compare_button = ttk.Button(comparison, text="对比所选测点", style="Accent.TButton", command=self.run_feature_comparison)
        self.feature_compare_button.pack(fill=tk.X, pady=(8, 0))

        explanation = ttk.LabelFrame(parent, text="特征说明", style="Card.TLabelframe", padding=10)
        explanation.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            explanation,
            text="特征库按照场景关联表汇总单地点频谱分析结果。表中地点名称来自关联表，频谱目录用于匹配原始测点。\n\n可按场景、城市、极化和频段筛选，并导出当前结果。",
            wraplength=320,
            justify=tk.LEFT,
        ).pack(anchor="w")

    def _build_scene_controls(self, parent: ttk.Frame) -> None:
        parent.configure(width=360)
        data_paths = ttk.LabelFrame(parent, text="数据文件位置", style="Card.TLabelframe", padding=10)
        data_paths.pack(fill=tk.X)
        self._visible_path_selector(data_paths, "频谱根目录", self.spectrum_dir, self._refresh_scene_spectrum_root)
        self._visible_path_selector(data_paths, "IQ 根目录", self.data_dir, self.refresh_recordings)
        ttk.Label(
            data_paths,
            text="可直接选择移动硬盘上的数据根目录；关联表中只填写相对目录。",
            foreground="#64748b",
            wraplength=310,
        ).grid(row=data_paths.grid_size()[1], column=0, columnspan=3, sticky="w", pady=(8, 0))

        browser = ttk.LabelFrame(parent, text="场景浏览", style="Card.TLabelframe", padding=10)
        browser.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(browser, text="场景类型").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.scene_filter_combo = ttk.Combobox(browser, textvariable=self.scene_filter, state="readonly")
        self.scene_filter_combo.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(browser, text="城市").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.scene_city_combo = ttk.Combobox(browser, textvariable=self.scene_city_filter, state="readonly")
        self.scene_city_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(browser, text="地点关键字").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        scene_keyword_entry = ttk.Entry(browser, textvariable=self.scene_keyword)
        scene_keyword_entry.grid(row=2, column=1, sticky="ew", pady=4)
        scene_keyword_entry.bind("<Return>", lambda _event: self.refresh_scene_catalog(keep_filters=True))
        ttk.Button(browser, text="查询地点", command=lambda: self.refresh_scene_catalog(keep_filters=True)).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        ttk.Label(
            browser,
            text="地点分类以导入的统一关联表为准。",
            foreground="#64748b",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        browser.columnconfigure(1, weight=1)
        for combo in (self.scene_filter_combo, self.scene_city_combo):
            combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_scene_catalog(keep_filters=True))

        display = ttk.LabelFrame(parent, text="数据显示", style="Card.TLabelframe", padding=10)
        display.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(display, textvariable=self.scene_selected_text, style="Value.TLabel", wraplength=310).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(display, text="极化").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.scene_polarization_combo = ttk.Combobox(display, textvariable=self.scene_polarization, state="readonly")
        self.scene_polarization_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(display, text="频段").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.scene_band_combo = ttk.Combobox(display, textvariable=self.scene_band, state="readonly")
        self.scene_band_combo.grid(row=2, column=1, sticky="ew", pady=4)
        self.scene_polarization_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_scene_bands())
        self.scene_band_combo.bind("<<ComboboxSelected>>", lambda _event: self._show_selected_scene_data())
        display.columnconfigure(1, weight=1)

        region = ttk.LabelFrame(parent, text="区域频谱融合", style="Card.TLabelframe", padding=10)
        region.pack(fill=tk.X, pady=(12, 0))
        self._entry_row(region, "频率容差 (MHz)", self.region_frequency_tolerance_mhz, 0)
        self._entry_row(region, "最低出现概率", self.region_minimum_probability, 1)
        ttk.Button(
            region, text="生成当前场景区域特征", style="Accent.TButton", command=self.run_region_feature_analysis
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(region, text="导出区域特征库", command=self.export_region_feature_table).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        ttk.Label(
            region,
            text="使用当前场景类型、极化和频段，融合该场景下所有地点。概率填写 0-1，例如 0.3 表示至少 30%。",
            foreground="#64748b", wraplength=310, justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        iq_link = ttk.LabelFrame(parent, text="关联 IQ 数据", style="Card.TLabelframe", padding=10)
        iq_link.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(iq_link, textvariable=self.scene_iq_count_text, foreground="#334155").pack(anchor="w", pady=(0, 6))
        self.scene_iq_combo = ttk.Combobox(iq_link, textvariable=self.scene_iq_recording, state="readonly")
        self.scene_iq_combo.pack(fill=tk.X)
        iq_buttons = ttk.Frame(iq_link)
        iq_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(iq_buttons, text="上一频点", command=lambda: self.shift_scene_iq(-1)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(iq_buttons, text="下一频点", command=lambda: self.shift_scene_iq(1)).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        ttk.Button(iq_link, text="在 IQ 模块分析当前频点", style="Accent.TButton", command=self.open_scene_iq).pack(fill=tk.X, pady=(8, 0))
        ttk.Label(
            iq_link,
            text="频点来自已导入的关联表，不修改 IQ 原文件。",
            foreground="#64748b",
            wraplength=310,
        ).pack(anchor="w", pady=(8, 0))

    def _build_scene_results(self, parent: ttk.Frame) -> None:
        self.scene_results_pane = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        self.scene_results_pane.pack(fill=tk.BOTH, expand=True)
        location_frame = ttk.LabelFrame(self.scene_results_pane, text="场景内采集地点", padding=6)
        columns = ("serial", "city", "point", "scene")
        self.scene_location_tree = ttk.Treeview(
            location_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=4,
            style="Feature.Treeview",
        )
        headings = {"serial": "序号", "city": "城市", "point": "采集地点", "scene": "场景类型"}
        widths = {"serial": 70, "city": 120, "point": 310, "scene": 170}
        for column in columns:
            self.scene_location_tree.heading(column, text=headings[column])
            self.scene_location_tree.column(column, width=widths[column], minwidth=90, anchor="w")
        scene_ybar = ttk.Scrollbar(location_frame, orient=tk.VERTICAL, command=self.scene_location_tree.yview)
        self.scene_location_tree.configure(yscrollcommand=scene_ybar.set)
        self.scene_location_tree.grid(row=0, column=0, sticky="nsew")
        scene_ybar.grid(row=0, column=1, sticky="ns")
        location_frame.columnconfigure(0, weight=1)
        location_frame.rowconfigure(0, weight=1)
        self.scene_location_tree.bind("<<TreeviewSelect>>", lambda _event: self._on_scene_location_selected())
        self.scene_results_pane.add(location_frame, weight=0)

        self.scene_notebook = ttk.Notebook(self.scene_results_pane)
        self.scene_results_pane.add(self.scene_notebook, weight=1)
        self._scene_sash_initialized = False

        def set_initial_scene_sash(_event=None) -> None:
            if self._scene_sash_initialized:
                return
            self._scene_sash_initialized = True
            self.after_idle(lambda: self.scene_results_pane.sashpos(0, 245))

        self.scene_results_pane.bind("<Map>", set_initial_scene_sash, add="+")
        self.scene_summary_text = tk.Text(
            self.scene_notebook, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11)
        )
        self.scene_summary_text.insert(tk.END, "请从上方选择一个采集地点。\n")
        self.scene_summary_text.configure(state=tk.DISABLED)
        self.scene_notebook.add(self.scene_summary_text, text="地点与特征摘要")

        self.scene_iq_text = tk.Text(
            self.scene_notebook, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11)
        )
        self.scene_iq_text.insert(tk.END, "选择地点后显示与该地点关联的 IQ 数据。\n")
        self.scene_iq_text.configure(state=tk.DISABLED)
        self.scene_notebook.add(self.scene_iq_text, text="关联的 IQ 数据")

        self.scene_feature_tab = ttk.Frame(self.scene_notebook)
        self.scene_notebook.add(self.scene_feature_tab, text="频谱特征")
        feature_pane = ttk.PanedWindow(self.scene_feature_tab, orient=tk.VERTICAL)
        feature_pane.pack(fill=tk.BOTH, expand=True)
        feature_top = ttk.Frame(feature_pane, padding=(10, 8))
        feature_pane.add(feature_top, weight=0)
        self.scene_feature_summary = tk.Text(
            feature_top, width=54, height=6, wrap="word", bg="#ffffff", relief="solid",
            borderwidth=1, padx=10, pady=8, font=("Segoe UI", 10),
        )
        self.scene_feature_summary.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.scene_feature_summary.insert(tk.END, "请选择一个具有频谱数据的采集地点。")
        self.scene_feature_summary.configure(state=tk.DISABLED)
        peak_columns = ("rank", "frequency", "level", "left", "right", "bandwidth")
        self.scene_peak_tree = ttk.Treeview(
            feature_top,
            columns=peak_columns,
            show="headings",
            height=4,
            style="Peak.Treeview",
        )
        peak_headings = {
            "rank": "排序", "frequency": "中心频率 (MHz)", "level": "峰值 (dBμV/m)",
            "left": "左边界 (MHz)", "right": "右边界 (MHz)", "bandwidth": "BW_3dB (MHz)",
        }
        for column in peak_columns:
            self.scene_peak_tree.heading(column, text=peak_headings[column])
            self.scene_peak_tree.column(column, width=118 if column != "rank" else 55, anchor="center", stretch=True)
        peak_ybar = ttk.Scrollbar(feature_top, orient=tk.VERTICAL, command=self.scene_peak_tree.yview)
        self.scene_peak_tree.configure(yscrollcommand=peak_ybar.set)
        self.scene_peak_tree.grid(row=0, column=1, sticky="nsew")
        peak_ybar.grid(row=0, column=2, sticky="ns")
        feature_top.columnconfigure(0, weight=2)
        feature_top.columnconfigure(1, weight=3)
        feature_top.rowconfigure(0, weight=1)
        self.scene_feature_plot_host = ttk.Frame(feature_pane)
        feature_pane.add(self.scene_feature_plot_host, weight=1)

        self.region_feature_tab = ttk.Frame(self.scene_notebook)
        self.scene_notebook.add(self.region_feature_tab, text="区域频谱特征")
        region_pane = ttk.PanedWindow(self.region_feature_tab, orient=tk.VERTICAL)
        region_pane.pack(fill=tk.BOTH, expand=True)
        region_table_frame = ttk.Frame(region_pane, padding=(10, 8))
        region_pane.add(region_table_frame, weight=0)
        region_columns = (
            "rank", "frequency", "count", "probability", "mean_level", "mean_bandwidth",
        )
        self.region_feature_tree = ttk.Treeview(
            region_table_frame, columns=region_columns, show="headings", height=6, style="Peak.Treeview"
        )
        region_headings = {
            "rank": "排序", "frequency": "典型频率 (MHz)", "count": "出现地点数",
            "probability": "出现概率", "mean_level": "平均峰值 (dBμV/m)",
            "mean_bandwidth": "平均 BW_3dB (MHz)",
        }
        for column in region_columns:
            self.region_feature_tree.heading(column, text=region_headings[column])
            self.region_feature_tree.column(column, width=125 if column != "rank" else 55, anchor="center")
        region_ybar = ttk.Scrollbar(region_table_frame, orient=tk.VERTICAL, command=self.region_feature_tree.yview)
        region_xbar = ttk.Scrollbar(region_table_frame, orient=tk.HORIZONTAL, command=self.region_feature_tree.xview)
        self.region_feature_tree.configure(yscrollcommand=region_ybar.set, xscrollcommand=region_xbar.set)
        self.region_feature_tree.grid(row=0, column=0, sticky="nsew")
        region_ybar.grid(row=0, column=1, sticky="ns")
        region_xbar.grid(row=1, column=0, sticky="ew")
        region_table_frame.columnconfigure(0, weight=1)
        region_table_frame.rowconfigure(0, weight=1)
        self.region_feature_plot_host = ttk.Frame(region_pane)
        region_pane.add(self.region_feature_plot_host, weight=1)

    def _build_results(self, parent: ttk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.summary_text = tk.Text(self.notebook, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11))
        self.summary_text.insert(tk.END, "请选择一组 IQ 数据，然后点击“生成并查看”。\n")
        self.summary_text.configure(state=tk.DISABLED)
        self.notebook.add(self.summary_text, text="摘要")

        self.detection_tab = ttk.Frame(self.notebook)
        detection_progress_frame = ttk.Frame(self.detection_tab, padding=(12, 10, 12, 6))
        detection_progress_frame.pack(fill=tk.X)
        ttk.Progressbar(
            detection_progress_frame,
            variable=self.detect_progress,
            maximum=100,
            mode="determinate",
        ).pack(fill=tk.X)
        ttk.Label(
            detection_progress_frame,
            textvariable=self.detect_progress_text,
            foreground="#64748b",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Checkbutton(
            detection_progress_frame,
            text="保存检测结果文件",
            variable=self.detect_save_output,
        ).pack(anchor="w", pady=(6, 0))
        self.detection_text = tk.Text(
            self.detection_tab,
            height=8,
            wrap="word",
            bg="#ffffff",
            relief="flat",
            padx=16,
            pady=12,
            font=("Segoe UI", 11),
        )
        self.detection_text.pack(fill=tk.X)
        self.detection_text.insert(tk.END, "运行检测后，将显示全记录中平均功率最强的片段、时域功率包络和片段频谱。\n")
        self.detection_text.configure(state=tk.DISABLED)
        ttk.Separator(self.detection_tab, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12, pady=4)
        self.detection_figure_host = ttk.Frame(self.detection_tab, padding=(8, 4, 8, 8))
        self.detection_figure_host.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.detection_tab, text="自动检测")

        self.playback_tab = PlaybackTab(self.notebook, self)
        self.notebook.add(self.playback_tab, text="回放")

        self.log = tk.Text(self.notebook, height=10, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11))
        self.notebook.add(self.log, text="日志")

    def _build_spectrum_results(self, parent: ttk.Frame) -> None:
        self.spectrum_notebook = ttk.Notebook(parent)
        self.spectrum_notebook.pack(fill=tk.BOTH, expand=True)
        self.spectrum_text = tk.Text(
            self.spectrum_notebook,
            wrap="word",
            bg="#ffffff",
            relief="flat",
            padx=16,
            pady=14,
            font=("Segoe UI", 11),
        )
        self.spectrum_text.insert(tk.END, "请选择测点，然后生成跨文件最大值保持频谱。\n")
        self.spectrum_text.configure(state=tk.DISABLED)
        self.spectrum_notebook.add(self.spectrum_text, text="摘要")

    def _build_feature_results(self, parent: ttk.Frame) -> None:
        self.feature_notebook = ttk.Notebook(parent)
        self.feature_notebook.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.Frame(self.feature_notebook)
        base_columns = (
            "scene", "city", "point", "spectrum_point", "polarization", "band",
            "peak_freq", "peak", "minimum", "mean", "median", "p95", "p99", "std", "dynamic",
            "threshold", "occupied", "peaks", "effective_bands", "effective_span", "effective_bw",
            "files",
        )
        peak_detail_columns = tuple(
            f"peak_{rank}_{field}"
            for rank in range(1, 11)
            for field in ("frequency", "level", "bandwidth")
        )
        columns = (*base_columns, *peak_detail_columns)
        self.feature_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Feature.Treeview",
        )
        headings = {
            "scene": "场景类型", "city": "城市", "point": "采集地点", "spectrum_point": "频谱数据目录",
            "polarization": "极化", "band": "频段",
            "peak_freq": "峰值频率 (MHz)", "peak": "峰值 (dBμV/m)", "p95": "P95 (dBμV/m)",
            "minimum": "最小值", "mean": "均值", "median": "中位数", "p99": "P99", "std": "标准差",
            "dynamic": "动态范围 (dB)", "threshold": "检测阈值", "occupied": "有效频点占比",
            "peaks": "主要峰值数", "effective_bands": "有效频段数", "effective_span": "频率跨度 (MHz)",
            "effective_bw": "有效总带宽 (MHz)", "files": "源文件数",
        }
        widths = {
            "scene": 105, "city": 90, "point": 170, "spectrum_point": 170, "polarization": 85,
            "band": 110, "peak_freq": 120, "peak": 110, "minimum": 85, "mean": 80,
            "median": 80, "p95": 105, "p99": 80, "std": 75, "dynamic": 105,
            "threshold": 90, "occupied": 105, "peaks": 90, "effective_bands": 90,
            "effective_span": 115, "effective_bw": 130, "files": 80,
        }
        for rank in range(1, 11):
            headings[f"peak_{rank}_frequency"] = f"峰值{rank}频率 (MHz)"
            headings[f"peak_{rank}_level"] = f"峰值{rank}场强 (dBμV/m)"
            headings[f"peak_{rank}_bandwidth"] = f"峰值{rank} BW_3dB (MHz)"
            widths[f"peak_{rank}_frequency"] = 125
            widths[f"peak_{rank}_level"] = 135
            widths[f"peak_{rank}_bandwidth"] = 145
        for column in columns:
            self.feature_tree.heading(column, text=headings[column])
            heading_width = self.feature_heading_font.measure(headings[column]) + 30
            column_width = max(widths[column], heading_width)
            self.feature_tree.column(
                column,
                width=column_width,
                minwidth=max(70, heading_width),
                anchor="center" if column not in ("scene", "city", "point", "spectrum_point") else "w",
            )
        ybar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.feature_tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.feature_tree.xview)
        self.feature_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.feature_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.feature_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_feature_selection_count())
        self.feature_notebook.add(table_frame, text="场景频谱特征总表")

        self.feature_summary_text = tk.Text(self.feature_notebook, wrap="word", bg="#ffffff", relief="flat", padx=16, pady=14, font=("Segoe UI", 11))
        self.feature_summary_text.insert(tk.END, "请先建立频谱特征库，再从特征数据表中选择两个或更多测点进行对比。\n")
        self.feature_summary_text.configure(state=tk.DISABLED)
        self.feature_notebook.add(self.feature_summary_text, text="对比摘要")

    def _path_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, after_select) -> None:
        row = parent.grid_size()[1]
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=variable, width=28).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="...", width=3, command=lambda: self.choose_folder(variable, after_select)).grid(row=row, column=2, padx=(6, 0), pady=4)
        parent.columnconfigure(1, weight=1)

    def _visible_path_selector(self, parent: ttk.Frame, label: str, variable: tk.StringVar, after_select) -> None:
        row = parent.grid_size()[1]
        ttk.Label(parent, text=label, font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(5, 2)
        )
        ttk.Button(
            parent,
            text="选择...",
            command=lambda: self.choose_folder(variable, after_select),
        ).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=(3, 2))
        ttk.Label(
            parent,
            textvariable=variable,
            foreground="#334155",
            background="#f8fafc",
            wraplength=300,
            justify=tk.LEFT,
            padding=(6, 5),
        ).grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        parent.columnconfigure(0, weight=1)

    def _entry_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)

    def choose_folder(self, variable: tk.StringVar, after_select) -> None:
        selected = filedialog.askdirectory(initialdir=variable.get() or str(Path.cwd()))
        if selected:
            variable.set(selected)
            if after_select:
                after_select()

    def choose_feature_database(self) -> None:
        current = Path(self.feature_db_path.get())
        selected = filedialog.asksaveasfilename(
            title="选择频谱特征数据库",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".db",
            filetypes=(("SQLite 数据库", "*.db"), ("所有文件", "*.*")),
        )
        if selected:
            self.feature_db_path.set(selected)
            self.refresh_feature_library()
            self.refresh_scene_catalog()

    def refresh_feature_library(self, keep_filters: bool = False) -> None:
        database_path = Path(self.feature_db_path.get())
        try:
            cities, polarizations, bands = library_filter_values(database_path)
            scenes, scene_cities = scene_filter_values(database_path)
            self.feature_scene_combo["values"] = scenes
            self.feature_city_combo["values"] = cities
            self.feature_polarization_combo["values"] = polarizations
            self.feature_band_combo["values"] = bands
            if not keep_filters or self.feature_city.get() not in cities:
                self.feature_city.set("全部")
            if not keep_filters or self.feature_scene.get() not in scenes:
                self.feature_scene.set("全部")
            if not keep_filters or self.feature_polarization.get() not in polarizations:
                self.feature_polarization.set("全部")
            if self.feature_band.get() not in bands:
                self.feature_band.set(ALL_BAND if ALL_BAND in bands else (bands[0] if bands else ""))

            records = list_feature_records(
                database_path,
                city=self.feature_city.get(),
                polarization=self.feature_polarization.get(),
                band=self.feature_band.get(),
                keyword="",
            )
            locations = list_scene_locations(
                database_path,
                scene_type=self.feature_scene.get(),
                city=self.feature_city.get(),
                keyword=self.feature_keyword.get(),
            )
        except Exception as exc:
            messagebox.showerror("读取特征库失败", str(exc))
            return

        self.feature_tree.delete(*self.feature_tree.get_children())
        self.feature_record_by_item.clear()
        self.feature_location_by_item.clear()
        records_by_spectrum_point: dict[tuple[str, str], list[FeatureRecord]] = {}
        for record in records:
            records_by_spectrum_point.setdefault((record.city, record.point), []).append(record)
        feature_rows = [
            (location, record)
            for location in locations
            for record in records_by_spectrum_point.get(
                (location.city, location.spectrum_point or location.point), []
            )
        ]
        self.feature_records = [record for _location, record in feature_rows]
        for location, record in feature_rows:
            try:
                top_peaks = json.loads(record.top_peaks_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                top_peaks = []
            peak_details: list[str] = []
            for rank in range(10):
                if rank < len(top_peaks):
                    peak = top_peaks[rank]
                    peak_details.extend((
                        f"{float(peak.get('frequency_mhz', 0.0)):.6f}",
                        f"{float(peak.get('field_dbuv_m', 0.0)):.3f}",
                        f"{float(peak.get('bandwidth_3db_mhz', 0.0)):.6f}",
                    ))
                else:
                    peak_details.extend(("", "", ""))
            item = self.feature_tree.insert(
                "",
                tk.END,
                values=(
                    location.scene_type,
                    record.city,
                    location.point,
                    record.point,
                    record.polarization,
                    record.band,
                    f"{record.peak_frequency_mhz:.3f}",
                    f"{record.peak_dbuv_m:.2f}",
                    f"{record.min_dbuv_m:.2f}",
                    f"{record.mean_dbuv_m:.2f}",
                    f"{record.median_dbuv_m:.2f}",
                    f"{record.p95_dbuv_m:.2f}",
                    f"{record.p99_dbuv_m:.2f}",
                    f"{record.std_db:.2f}",
                    f"{record.dynamic_range_db:.2f}",
                    f"{record.detection_threshold_dbuv_m:.2f}",
                    f"{record.occupied_ratio * 100:.2f}%",
                    record.strong_peak_count,
                    record.effective_band_count,
                    f"{record.effective_span_mhz:.3f}",
                    f"{record.effective_total_bandwidth_mhz:.3f}",
                    record.source_file_count,
                    *peak_details,
                ),
            )
            self.feature_record_by_item[item] = record
            self.feature_location_by_item[item] = location
        self._update_feature_selection_count()
        self.status_var.set(f"场景频谱特征库已加载，共显示 {len(feature_rows)} 条记录。")

    def export_feature_table(self) -> None:
        rows = self.feature_tree.get_children()
        if not rows:
            messagebox.showwarning("没有可导出的数据", "当前筛选条件下没有场景频谱特征。")
            return
        selected = filedialog.asksaveasfilename(
            title="导出场景频谱特征表",
            initialdir=self.out_dir.get(),
            initialfile="场景频谱特征汇总表.csv",
            defaultextension=".csv",
            filetypes=(("CSV 表格", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        columns = self.feature_tree["columns"]
        with Path(selected).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([self.feature_tree.heading(column, "text") for column in columns])
            writer.writerows(self.feature_tree.item(item, "values") for item in rows)
        self.status_var.set(f"已导出 {len(rows)} 条场景频谱特征：{selected}")
        messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条记录。\n\n{selected}")

    def _update_feature_selection_count(self) -> None:
        self.feature_selection_text.set(f"已选择 {len(self.feature_tree.selection())} 个测点")

    def select_all_feature_rows(self) -> None:
        children = self.feature_tree.get_children()
        self.feature_tree.selection_set(children)
        self._update_feature_selection_count()

    def clear_feature_selection(self) -> None:
        self.feature_tree.selection_remove(self.feature_tree.selection())
        self._update_feature_selection_count()

    def run_feature_build(self) -> None:
        try:
            groups = discover_spectrum_groups(Path(self.spectrum_dir.get()))
            scene_locations = list_scene_locations(Path(self.feature_db_path.get()))
        except Exception as exc:
            messagebox.showerror("扫描频谱目录失败", str(exc))
            return
        if not groups:
            messagebox.showwarning("没有频谱数据", "指定目录中没有发现可建立特征库的频谱 CSV。")
            return
        scene_spectrum_keys = {
            (location.city, location.spectrum_point or location.point)
            for location in scene_locations
        }
        groups = [group for group in groups if (group.city, group.point) in scene_spectrum_keys]
        if not groups:
            messagebox.showwarning(
                "没有场景频谱数据",
                "场景关联表中的地点没有匹配到频谱根目录，请检查“频谱数据目录名称”字段。",
            )
            return
        self.spectrum_groups = groups
        self.feature_build_button.configure(state=tk.DISABLED)
        self.status_var.set(f"正在为 {len(groups)} 组场景测点数据建立特征库...")
        self._set_feature_summary("正在按照场景关联表提取各地点频谱特征，请稍候...\n")
        self.module_notebook.select(self.feature_module)
        self.feature_notebook.select(self.feature_summary_text)
        thread = threading.Thread(target=self._feature_build_worker, args=(groups,), daemon=True)
        thread.start()

    def _feature_build_worker(self, groups: list[SpectrumGroup]) -> None:
        try:
            def report(done: int, total: int, label: str) -> None:
                self.messages.put(("feature_progress", (done, total, label)))

            result = build_feature_library(groups, Path(self.feature_db_path.get()), progress=report)
            self.messages.put(("feature_build_done", result))
        except Exception as exc:
            self.messages.put(("feature_build_error", str(exc)))

    def run_feature_comparison(self) -> None:
        records = [self.feature_record_by_item[item] for item in self.feature_tree.selection() if item in self.feature_record_by_item]
        if len(records) < 2:
            messagebox.showwarning("选择测点不足", "请在特征数据表中至少选择两个测点。")
            return
        if len(records) > 12:
            messagebox.showwarning("选择测点过多", "为保证图表清晰，一次最多对比 12 个测点。请使用城市、极化或关键字缩小范围。")
            return
        self.feature_compare_button.configure(state=tk.DISABLED)
        self.status_var.set(f"正在对比 {len(records)} 个测点的频谱特征...")
        thread = threading.Thread(target=self._feature_compare_worker, args=(records,), daemon=True)
        thread.start()

    def _feature_compare_worker(self, records: list[FeatureRecord]) -> None:
        try:
            result = compare_feature_records(Path(self.feature_db_path.get()), records)
            self.messages.put(("feature_compare_done", result))
        except Exception as exc:
            self.messages.put(("feature_compare_error", str(exc)))

    def refresh_scene_catalog(self, keep_filters: bool = False) -> None:
        database_path = Path(self.feature_db_path.get())
        locations = {(group.city, group.point) for group in self.spectrum_groups}
        try:
            default_association = self.app_dir / "场景地点_IQ关联表_更新版.csv"
            sync_association_locations(database_path, default_association)
            initialize_scene_catalog(database_path, locations)
            scenes, cities = scene_filter_values(database_path)
            self.scene_filter_combo["values"] = scenes
            self.scene_city_combo["values"] = cities
            if not keep_filters or self.scene_filter.get() not in scenes:
                self.scene_filter.set("全部")
            if not keep_filters or self.scene_city_filter.get() not in cities:
                self.scene_city_filter.set("全部")
            self.scene_locations = list_scene_locations(
                database_path,
                scene_type=self.scene_filter.get(),
                city=self.scene_city_filter.get(),
                keyword=self.scene_keyword.get(),
            )
        except Exception as exc:
            messagebox.showerror("读取场景分类失败", str(exc))
            return

        current_key = None
        if self.selected_scene_location is not None:
            current_key = (self.selected_scene_location.city, self.selected_scene_location.point)
        self.scene_location_tree.delete(*self.scene_location_tree.get_children())
        self.scene_location_by_item.clear()
        item_to_select = None
        for serial, location in enumerate(self.scene_locations, start=1):
            item = self.scene_location_tree.insert(
                "", tk.END, values=(serial, location.city, location.point, location.scene_type)
            )
            self.scene_location_by_item[item] = location
            if current_key == (location.city, location.point):
                item_to_select = item
        if item_to_select is not None:
            self.scene_location_tree.selection_set(item_to_select)
            self.scene_location_tree.see(item_to_select)
        if self.device_playback_module is not None:
            self.device_playback_module.refresh_raw_catalog()
        self.status_var.set(f"场景分类已加载，共显示 {len(self.scene_locations)} 个采集地点。")

    def _on_scene_location_selected(self) -> None:
        selection = self.scene_location_tree.selection()
        if not selection:
            return
        location = self.scene_location_by_item.get(selection[0])
        if location is None:
            return
        self.selected_scene_location = location
        self.scene_selected_text.set(f"{location.city} / {location.point}")
        self._update_scene_profile_options()
        self._show_selected_scene_data()

    def _scene_location_profiles(self) -> list[FeatureRecord]:
        location = self.selected_scene_location
        if location is None:
            return []
        records = list_feature_records(
            Path(self.feature_db_path.get()), city=location.city, band="全部", keyword=location.spectrum_point or location.point
        )
        spectrum_point = location.spectrum_point or location.point
        return [record for record in records if record.point == spectrum_point]

    def _update_scene_profile_options(self) -> None:
        profiles = self._scene_location_profiles()
        polarizations = sorted({record.polarization for record in profiles})
        self.scene_polarization_combo["values"] = polarizations
        if self.scene_polarization.get() not in polarizations:
            self.scene_polarization.set(polarizations[0] if polarizations else "")
        self._update_scene_bands(show_data=False)

    def _update_scene_bands(self, show_data: bool = True) -> None:
        profiles = self._scene_location_profiles()
        bands = [
            band
            for band in (*BAND_ORDER, ALL_BAND)
            if any(record.polarization == self.scene_polarization.get() and record.band == band for record in profiles)
        ]
        self.scene_band_combo["values"] = bands
        if self.scene_band.get() not in bands:
            self.scene_band.set(ALL_BAND if ALL_BAND in bands else (bands[0] if bands else ""))
        if show_data:
            self._show_selected_scene_data()

    def _show_selected_scene_data(self) -> None:
        location = self.selected_scene_location
        if location is None:
            return
        self._remove_scene_magnetic_spectrum_tab()
        profiles = self._scene_location_profiles()
        record = next(
            (
                item
                for item in profiles
                if item.polarization == self.scene_polarization.get() and item.band == self.scene_band.get()
            ),
            None,
        )
        linked_iq_details = list_linked_iq_details(Path(self.feature_db_path.get()), location.city, location.point)
        linked_by_stem = {link.recording_stem: link for link in linked_iq_details}
        prefix = location.iq_recording_prefix.casefold()
        relative_parts = tuple(
            part.casefold() for part in Path(location.iq_relative_directory).parts if part not in ("", ".")
        )
        for recording in self.recordings:
            if not prefix or not recording.stem.casefold().startswith(prefix):
                continue
            if recording.metadata_path is None or len(recording.volumes) < 2:
                continue
            parent_parts = tuple(part.casefold() for part in recording.metadata_path.parent.parts)
            if relative_parts and parent_parts[-len(relative_parts):] != relative_parts:
                continue
            linked_by_stem.setdefault(
                recording.stem,
                IQLocationLink(
                    recording.stem,
                    str(recording.metadata_path),
                    str(recording.volumes[0].path),
                    str(recording.volumes[1].path),
                ),
            )
        linked_iq_details = [linked_by_stem[stem] for stem in sorted(linked_by_stem, key=str.casefold)]
        linked_iq = [link.recording_stem for link in linked_iq_details]
        self.current_scene_iq_stems = linked_iq
        self.scene_iq_count_text.set(f"当前地点关联 {len(linked_iq)} 组 IQ 数据")
        iq_lines = [f"地点：{location.city} / {location.point}", "", "已关联 IQ 数据："]
        for link in linked_iq_details:
            iq_lines.extend(
                (
                    f"  数据组：{link.recording_stem}",
                    f"    WSM：{link.wsm_file or '未记录'}",
                    f"    WS1：{link.ws1_file or '未记录'}",
                    f"    WS2：{link.ws2_file or '未记录'}",
                )
            )
        if not linked_iq:
            iq_lines.append("  暂无。请在左侧选择 IQ 数据组并点击“关联”。")
        iq_choices = sorted({*(recording.stem for recording in self.recordings), *linked_iq}, key=str.casefold)
        self.scene_iq_combo["values"] = iq_choices
        if linked_iq and self.scene_iq_recording.get() not in linked_iq:
            self.scene_iq_recording.set(linked_iq[0])
        self._set_readonly_text(self.scene_iq_text, "\n".join(iq_lines))

        lines = [
            f"城市：{location.city}",
            f"地点：{location.point}",
            f"频谱数据目录：{location.spectrum_point or '无（仅 IQ 数据）'}",
            f"场景类型：{location.scene_type}",
            f"备注：{location.notes or '无'}",
            f"关联 IQ 数据组：{len(linked_iq)}",
            "",
        ]
        if record is None:
            if not location.spectrum_point:
                lines.append("数据状态：无频谱数据，仅有 IQ 数据。")
            else:
                lines.append("该地点当前没有匹配的频谱特征记录，请先建立或更新频谱特征库。")
            self._set_readonly_text(self.scene_summary_text, "\n".join(lines))
            self._clear_scene_spectrum_features()
            if not location.spectrum_point:
                self._show_no_scene_spectrum(location, bool(linked_iq))
            else:
                self._show_scene_magnetic_spectrum(location)
            return

        lines.extend(
            (
                f"显示数据：{record.polarization} / {record.band}",
                f"源扫描文件数：{record.source_file_count}",
                f"峰值场强：{record.peak_dbuv_m:.3f} dBμV/m @ {record.peak_frequency_mhz:.6f} MHz",
                f"均值 / 中位数：{record.mean_dbuv_m:.3f} / {record.median_dbuv_m:.3f} dBμV/m",
                f"P95 / P99：{record.p95_dbuv_m:.3f} / {record.p99_dbuv_m:.3f} dBμV/m",
                f"标准差：{record.std_db:.3f} dB",
                f"频谱质心：{record.centroid_mhz:.6f} MHz",
                f"背景场强：{record.noise_floor_dbuv_m:.3f} dBμV/m",
                f"强信号频点占比：{record.occupied_ratio * 100:.3f}%",
                f"强峰数量：{record.strong_peak_count}",
            )
        )
        self._set_readonly_text(self.scene_summary_text, "\n".join(lines))

        frequencies_hz, values = load_feature_spectrum(Path(self.feature_db_path.get()), record.id)
        analysis = analyze_spectrum_features(frequencies_hz, values)
        self._show_scene_spectrum_features(location, record, frequencies_hz, values, analysis)
        figure = Figure(figsize=(8.4, 4.1), constrained_layout=True)
        ax = figure.subplots()
        ax.plot(frequencies_hz / 1e6, values, color="#1677b8", linewidth=1.0)
        peak_index = int(np.argmax(values))
        ax.scatter(
            [frequencies_hz[peak_index] / 1e6], [values[peak_index]], color="#e85d04", s=30, zorder=4, label="全局峰值"
        )
        ax.set(
            title=f"{location.point} / {record.polarization} / {record.band}",
            xlabel="频率 (MHz)",
            ylabel="电场强度 (dBμV/m)",
        )
        ax.title.set_fontsize(12)
        ax.xaxis.label.set_fontsize(10)
        ax.yaxis.label.set_fontsize(10)
        ax.tick_params(labelsize=9)
        ax.grid(True, alpha=0.25)
        ax.legend(loc="best")
        if self.scene_spectrum_tab is not None:
            self.scene_notebook.forget(self.scene_spectrum_tab)
            self.scene_spectrum_tab.destroy()
        self.scene_spectrum_tab = FigureTab(self.scene_notebook, figure, compact=True)
        self.scene_notebook.insert(self.scene_feature_tab, self.scene_spectrum_tab, text="地点频谱")
        self._show_scene_magnetic_spectrum(location)
        self.scene_notebook.select(self.scene_spectrum_tab)

    def _remove_scene_spectrum_tab(self) -> None:
        if self.scene_spectrum_tab is not None:
            try:
                self.scene_notebook.forget(self.scene_spectrum_tab)
            except tk.TclError:
                pass
            self.scene_spectrum_tab.destroy()
            self.scene_spectrum_tab = None

    def _show_no_scene_spectrum(self, location: SceneLocation, has_iq: bool) -> None:
        self._remove_scene_spectrum_tab()
        frame = ttk.Frame(self.scene_notebook, style="Panel.TFrame")
        content = ttk.Frame(frame, style="Panel.TFrame", padding=36)
        content.place(relx=0.5, rely=0.42, anchor="center")
        ttk.Label(content, text="无频谱数据", font=("Segoe UI", 18, "bold"), background="#ffffff").pack()
        ttk.Label(
            content,
            text="该地点仅采集了关键频点 IQ 数据。" if has_iq else "该地点未关联频谱数据，IQ 文件将在选择正确根目录后自动匹配。",
            font=("Segoe UI", 11), foreground="#64748b", background="#ffffff",
        ).pack(pady=(10, 0))
        self.scene_spectrum_tab = frame
        self.scene_notebook.insert(self.scene_feature_tab, frame, text="地点频谱")
        self.scene_notebook.select(frame)

    def _remove_scene_magnetic_spectrum_tab(self) -> None:
        if self.scene_magnetic_spectrum_tab is not None:
            try:
                self.scene_notebook.forget(self.scene_magnetic_spectrum_tab)
            except tk.TclError:
                pass
            self.scene_magnetic_spectrum_tab.destroy()
            self.scene_magnetic_spectrum_tab = None

    def _show_scene_magnetic_spectrum(self, location: SceneLocation) -> None:
        magnetic = load_low_frequency_magnetic_spectrum(
            Path(self.spectrum_dir.get()), location.city, location.spectrum_point or location.point
        )
        if magnetic is None:
            return
        frequencies_mhz = magnetic.frequencies_hz / 1e6
        values = magnetic.values_dbuv_m
        peak_index = int(np.argmax(values))
        figure = Figure(figsize=(8.4, 4.1), constrained_layout=True)
        ax = figure.subplots()
        ax.plot(frequencies_mhz, values, color="#7c3aed", linewidth=1.0, label="低频磁场最大值保持")
        ax.scatter(
            [frequencies_mhz[peak_index]], [values[peak_index]],
            color="#e85d04", s=30, zorder=4, label="全局峰值",
        )
        ax.axvline(0.15, color="#94a3b8", linestyle="--", linewidth=0.9, alpha=0.8)
        ax.annotate(
            f"{frequencies_mhz[peak_index]:.6f} MHz\n{values[peak_index]:.2f} dBμA/m",
            xy=(frequencies_mhz[peak_index], values[peak_index]),
            xytext=(10, -34), textcoords="offset points",
            bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#e85d04", "alpha": 0.95},
            arrowprops={"arrowstyle": "-", "color": "#e85d04"},
        )
        ax.set_xscale("log")
        ax.set(
            title=f"{location.point} / 低频磁场 / 9 kHz-30 MHz",
            xlabel="频率 (MHz，对数坐标)", ylabel="磁场强度 (dBμA/m)",
        )
        ax.grid(True, which="both", alpha=0.22)
        ax.legend(loc="best")
        self.scene_magnetic_spectrum_tab = FigureTab(self.scene_notebook, figure, compact=True)
        self.scene_notebook.insert(
            self.scene_feature_tab, self.scene_magnetic_spectrum_tab, text="低频磁场频谱"
        )

    def run_region_feature_analysis(self) -> None:
        scene_type = self.scene_filter.get()
        if scene_type == "全部" and self.selected_scene_location is not None:
            scene_type = self.selected_scene_location.scene_type
        if not scene_type or scene_type == "全部":
            messagebox.showwarning("请选择场景", "请先在左侧选择一个具体场景类型，例如居民区或工业区。")
            return
        try:
            tolerance = float(self.region_frequency_tolerance_mhz.get())
            minimum_probability = float(self.region_minimum_probability.get())
            database_path = Path(self.feature_db_path.get())
            locations = list_scene_locations(database_path, scene_type=scene_type)
            records = list_feature_records(
                database_path,
                polarization=self.scene_polarization.get(),
                band=self.scene_band.get(),
            )
            record_map = {(record.city, record.point): record for record in records}
            location_peaks: dict[str, list[dict[str, float | int]]] = {}
            for location in locations:
                record = record_map.get((location.city, location.spectrum_point or location.point))
                if record is None:
                    continue
                try:
                    peaks = json.loads(record.top_peaks_json)
                except (TypeError, ValueError):
                    peaks = []
                if peaks:
                    location_peaks[f"{location.city}/{location.point}"] = peaks
            result = analyze_region_spectrum(
                scene_type, self.scene_polarization.get(), self.scene_band.get(),
                location_peaks, tolerance, minimum_probability,
            )
        except Exception as exc:
            messagebox.showerror("区域频谱融合失败", str(exc))
            return
        self.current_region_result = result
        self.region_feature_tree.delete(*self.region_feature_tree.get_children())
        for signal in result.signals:
            self.region_feature_tree.insert("", tk.END, values=(
                signal.rank,
                f"{signal.typical_frequency_mhz:.6f}",
                f"{signal.occurrence_count}/{signal.location_count}",
                f"{signal.occurrence_probability * 100:.2f}%",
                f"{signal.mean_level_dbuv_m:.3f}",
                f"{signal.mean_bandwidth_3db_mhz:.6f}",
            ))
        if self.region_feature_figure_tab is not None:
            self.region_feature_figure_tab.destroy()
        self.region_feature_figure_tab = FigureTab(self.region_feature_plot_host, result.figure, compact=True)
        self.region_feature_figure_tab.pack(fill=tk.BOTH, expand=True)
        self.scene_notebook.select(self.region_feature_tab)
        self.status_var.set(
            f"{scene_type}区域融合完成：{len(result.location_names)} 个地点，"
            f"筛选出 {len(result.signals)} 类典型信号。"
        )

    def export_region_feature_table(self) -> None:
        result = self.current_region_result
        if result is None:
            messagebox.showwarning("没有区域特征", "请先点击“生成当前场景区域特征”。")
            return
        selected = filedialog.asksaveasfilename(
            title="导出区域频谱特征库",
            initialdir=self.out_dir.get(),
            initialfile=f"{result.scene_type}_{result.polarization}_{result.band}_区域频谱特征.csv",
            defaultextension=".csv",
            filetypes=(("CSV 表格", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        with Path(selected).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("场景类型", result.scene_type))
            writer.writerow(("极化", result.polarization))
            writer.writerow(("频段", result.band))
            writer.writerow(("频率匹配容差 (MHz)", result.tolerance_mhz))
            writer.writerow(("最低出现概率", result.minimum_probability))
            writer.writerow(("参与地点数", len(result.location_names)))
            writer.writerow(())
            writer.writerow((
                "排序", "典型频率 (MHz)", "出现地点数", "区域地点总数", "出现概率",
                "平均峰值 (dBμV/m)", "平均 BW_3dB (MHz)",
            ))
            for signal in result.signals:
                writer.writerow((
                    signal.rank, signal.typical_frequency_mhz, signal.occurrence_count,
                    signal.location_count, signal.occurrence_probability, signal.mean_level_dbuv_m,
                    signal.mean_bandwidth_3db_mhz,
                ))
        self.status_var.set(f"区域频谱特征库已导出：{selected}")
        messagebox.showinfo("导出完成", f"已导出 {len(result.signals)} 类典型信号。\n\n{selected}")

    def _clear_scene_spectrum_features(self) -> None:
        self._set_readonly_text(self.scene_feature_summary, "当前地点没有可分析的频谱记录。")
        for item in self.scene_peak_tree.get_children():
            self.scene_peak_tree.delete(item)
        if self.scene_feature_figure_tab is not None:
            self.scene_feature_figure_tab.destroy()
            self.scene_feature_figure_tab = None

    def _show_scene_spectrum_features(
        self,
        location: SceneLocation,
        record: FeatureRecord,
        frequencies_hz: np.ndarray,
        values: np.ndarray,
        analysis: SpectrumFeatureAnalysis,
    ) -> None:
        metrics = analysis.metrics
        summary = (
            f"地点：{location.city} / {location.point}\n"
            f"数据：{record.polarization} / {record.band}\n\n"
            f"最大峰值：{metrics['peak_dbuv_m']:.3f} dBμV/m @ "
            f"{metrics['peak_frequency_mhz']:.6f} MHz\n"
            f"最小 / 均值 / 中位数：{metrics['min_dbuv_m']:.3f} / "
            f"{metrics['mean_dbuv_m']:.3f} / {metrics['median_dbuv_m']:.3f} dBμV/m\n"
            f"标准差 / 动态范围：{metrics['std_db']:.3f} / {metrics['dynamic_range_db']:.3f} dB\n"
            f"P95 / P99：{metrics['p95_dbuv_m']:.3f} / {metrics['p99_dbuv_m']:.3f} dBμV/m\n\n"
            f"检测阈值：{metrics['detection_threshold_dbuv_m']:.3f} dBμV/m "
            f"（P20 背景 + 6 dB）\n"
            f"主要峰值：{metrics['strong_peak_count']} 个\n"
            f"有效频段：{metrics['effective_band_count']} 个；总带宽 "
            f"{metrics['effective_total_bandwidth_mhz']:.3f} MHz\n"
            f"有效信号频率跨度：{metrics['effective_span_mhz']:.3f} MHz\n"
            f"有效频点占比：{metrics['occupied_ratio'] * 100:.3f}%\n"
            f"90% 能量集中频段：{metrics['energy_band_low_mhz']:.3f} - "
            f"{metrics['energy_band_high_mhz']:.3f} MHz "
            f"（带宽 {metrics['energy_bandwidth_mhz']:.3f} MHz）"
        )
        self._set_readonly_text(self.scene_feature_summary, summary)
        for item in self.scene_peak_tree.get_children():
            self.scene_peak_tree.delete(item)
        for peak in analysis.peaks:
            self.scene_peak_tree.insert("", tk.END, values=(
                peak["rank"], f"{peak['frequency_mhz']:.6f}", f"{peak['field_dbuv_m']:.3f}",
                f"{peak['left_3db_mhz']:.6f}", f"{peak['right_3db_mhz']:.6f}",
                f"{peak['bandwidth_3db_mhz']:.6f}",
            ))

        figure = Figure(figsize=(9.2, 5.2), constrained_layout=True)
        spectrum_ax, histogram_ax = figure.subplots(1, 2, gridspec_kw={"width_ratios": (2.35, 1.0)})
        frequency_mhz = frequencies_hz / 1e6
        spectrum_ax.plot(frequency_mhz, values, color="#1677b8", linewidth=0.9, label="最大值保持频谱")
        threshold = float(metrics["detection_threshold_dbuv_m"])
        spectrum_ax.axhline(threshold, color="#d1495b", linestyle="--", linewidth=1.0, label="检测阈值")
        spectrum_ax.axvspan(
            float(metrics["energy_band_low_mhz"]), float(metrics["energy_band_high_mhz"]),
            color="#f4a261", alpha=0.12, label="90% 能量集中频段",
        )
        for band in sorted(analysis.effective_bands, key=lambda item: item["peak_dbuv_m"], reverse=True)[:20]:
            spectrum_ax.axvspan(band["start_mhz"], band["end_mhz"], color="#2a9d8f", alpha=0.10)
        if analysis.peaks:
            spectrum_ax.scatter(
                [float(peak["frequency_mhz"]) for peak in analysis.peaks],
                [float(peak["field_dbuv_m"]) for peak in analysis.peaks],
                color="#e85d04", s=24, zorder=4, label="主要峰值",
            )
        spectrum_ax.set(title="频谱信号分布", xlabel="频率 (MHz)", ylabel="电场强度 (dBμV/m)")
        spectrum_ax.grid(True, alpha=0.22)
        spectrum_ax.legend(loc="best", fontsize=8)

        histogram_ax.hist(values[np.isfinite(values)], bins=60, color="#4c956c", alpha=0.82, orientation="horizontal")
        histogram_ax.axhline(float(metrics["mean_dbuv_m"]), color="#264653", linewidth=1.1, label="均值")
        histogram_ax.axhline(float(metrics["median_dbuv_m"]), color="#e9c46a", linewidth=1.1, label="中位数")
        histogram_ax.axhline(float(metrics["p95_dbuv_m"]), color="#e76f51", linewidth=1.1, label="P95")
        histogram_ax.set(title="场强分布", xlabel="频点数量", ylabel="电场强度 (dBμV/m)")
        histogram_ax.grid(True, alpha=0.18)
        histogram_ax.legend(loc="best", fontsize=8)
        figure.suptitle(f"{location.point} / {record.polarization} / {record.band}", fontsize=12)
        if self.scene_feature_figure_tab is not None:
            self.scene_feature_figure_tab.destroy()
        self.scene_feature_figure_tab = FigureTab(self.scene_feature_plot_host, figure, compact=True)
        self.scene_feature_figure_tab.pack(fill=tk.BOTH, expand=True)

    @staticmethod
    def _set_readonly_text(widget: tk.Text, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)

    def link_scene_iq(self) -> None:
        location = self.selected_scene_location
        recording = self.scene_iq_recording.get()
        if location is None or not recording:
            messagebox.showwarning("无法关联", "请先选择采集地点和 IQ 数据组。")
            return
        selected_recording = next((item for item in self.recordings if item.stem == recording), None)
        if selected_recording is None or selected_recording.metadata_path is None or len(selected_recording.volumes) < 2:
            messagebox.showerror("IQ 数据不完整", "关联需要同一数据组的 .wsm、.ws1、.ws2 三个文件。")
            return
        link_iq_recording(
            Path(self.feature_db_path.get()),
            location.city,
            location.point,
            recording,
            selected_recording.metadata_path.name,
            selected_recording.volumes[0].path.name,
            selected_recording.volumes[1].path.name,
        )
        self._show_selected_scene_data()
        self.scene_notebook.select(self.scene_iq_text)

    def unlink_scene_iq(self) -> None:
        location = self.selected_scene_location
        recording = self.scene_iq_recording.get()
        if location is None or not recording:
            messagebox.showwarning("无法解除关联", "请先选择采集地点和 IQ 数据组。")
            return
        unlink_iq_recording(Path(self.feature_db_path.get()), location.city, location.point, recording)
        self._show_selected_scene_data()
        self.scene_notebook.select(self.scene_iq_text)

    def open_scene_iq(self) -> None:
        recording = self.scene_iq_recording.get()
        if not recording:
            messagebox.showwarning("未选择 IQ 数据", "请先选择一个 IQ 数据组。")
            return
        selected_recording = next((item for item in self.recordings if item.stem == recording), None)
        if selected_recording is None and self.selected_scene_location is not None:
            links = list_linked_iq_details(
                Path(self.feature_db_path.get()),
                self.selected_scene_location.city,
                self.selected_scene_location.point,
            )
            link = next((item for item in links if item.recording_stem == recording), None)
            if link is not None:
                try:
                    selected_recording = recording_from_paths(
                        recording,
                        Path(link.wsm_file),
                        (Path(link.ws1_file), Path(link.ws2_file)),
                    )
                except Exception as exc:
                    messagebox.showerror("读取关联 IQ 数据失败", str(exc))
                    return
                self.recordings.append(selected_recording)
                self.recordings.sort(key=lambda item: item.stem.casefold())
                self.recording_combo["values"] = [item.stem for item in self.recordings]
        if selected_recording is None:
            messagebox.showerror("读取关联 IQ 数据失败", f"未找到 IQ 数据组：{recording}")
            return
        self.recording_var.set(selected_recording.stem)
        self.on_recording_selected()
        self.module_notebook.select(0)

    def shift_scene_iq(self, direction: int) -> None:
        stems = getattr(self, "current_scene_iq_stems", [])
        if not stems:
            messagebox.showinfo("没有关联 IQ 数据", "当前地点没有关联的 IQ 数据。")
            return
        current = self.scene_iq_recording.get()
        index = stems.index(current) if current in stems else 0
        self.scene_iq_recording.set(stems[(index + direction) % len(stems)])

    def export_scene_association_template(self) -> None:
        default_path = self.app_dir / "场景地点_IQ关联表_更新版.csv"
        selected = filedialog.asksaveasfilename(
            title="保存地点-IQ关联表",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            defaultextension=".csv",
            filetypes=(("CSV 关联表", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        try:
            row_count = write_association_template(Path(self.feature_db_path.get()), Path(selected))
        except Exception as exc:
            messagebox.showerror("生成关联表失败", str(exc))
            return
        self.status_var.set(f"已生成关联表，共 {row_count} 行：{selected}")
        messagebox.showinfo(
            "关联表已生成",
            "请填写“IQ数据组名称”列。一个地点需要关联多个 IQ 时，可复制为多行。\n\n"
            f"文件：{selected}",
        )

    def import_scene_association_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择填写好的地点-IQ关联表",
            initialdir=str(self.app_dir),
            filetypes=(("CSV 关联表", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        try:
            result = import_association_csv(
                Path(self.feature_db_path.get()),
                Path(selected),
                iq_root=Path(self.data_dir.get()),
            )
        except Exception as exc:
            messagebox.showerror("导入关联表失败", str(exc))
            return
        self.refresh_scene_catalog(keep_filters=True)
        self._show_association_import_result(result)

    def _show_association_import_result(self, result: AssociationImportResult) -> None:
        lines = [
            f"读取数据行：{result.row_count}",
            f"新增或更新地点：{result.location_count}",
            f"新建 IQ 关联：{result.link_count}",
            f"因 IQ 数据组不存在而跳过：{result.skipped_link_count}",
            f"问题数量：{len(result.errors)}",
        ]
        if result.errors:
            lines.extend(("", "问题明细（最多显示 20 条）：", *result.errors[:20]))
        messagebox.showinfo("关联表导入完成", "\n".join(lines))
        self.status_var.set(
            f"关联表导入完成：更新 {result.location_count} 个地点，新建 {result.link_count} 条 IQ 关联。"
        )

    def _refresh_scene_spectrum_root(self) -> None:
        self.refresh_environment_spectra()
        if self.selected_scene_location is not None:
            self._show_selected_scene_data()

    def refresh_recordings(self) -> None:
        data_root = Path(self.data_dir.get())
        path_refresh = None
        try:
            path_refresh = refresh_iq_link_paths(Path(self.feature_db_path.get()), data_root)
        except Exception as exc:
            self._log(f"场景IQ关联路径自动校正失败：{exc}")
        try:
            self.recordings = discover_recordings(data_root)
        except Exception as exc:
            messagebox.showerror("读取数据文件夹失败", str(exc))
            return
        names = [recording.stem for recording in self.recordings]
        self.recording_combo["values"] = names
        self.scene_iq_combo["values"] = names
        if names and self.scene_iq_recording.get() not in names:
            self.scene_iq_recording.set(names[0])
        if names and self.recording_var.get() not in names:
            self.recording_var.set(names[0])
        self.update_recording_details()
        if self.playback_tab is not None:
            self.playback_tab.configure_recording(self.selected_recording())
        self._log("已加载 IQ 数据组：\n" + "\n".join("  " + item.summary for item in self.recordings))
        if path_refresh is not None and path_refresh.updated_count:
            self.status_var.set(
                f"已按新IQ根目录更新 {path_refresh.updated_count} 条场景关联路径。"
            )
            self._log(
                f"场景IQ路径已自动更新 {path_refresh.updated_count} 条；"
                f"未找到 {path_refresh.missing_count} 条；同名冲突 {path_refresh.ambiguous_count} 条。"
            )
            if self.device_playback_module is not None:
                self.device_playback_module.refresh_raw_catalog()
        elif path_refresh is not None and path_refresh.unresolved_stems:
            self.status_var.set(
                f"新IQ根目录中仍有 {len(path_refresh.unresolved_stems)} 组场景IQ未找到。"
            )
            self._log("未能重定位的IQ数据组：" + "、".join(path_refresh.unresolved_stems))
        if self.selected_scene_location is not None:
            self._show_selected_scene_data()

    def on_recording_selected(self) -> None:
        self.update_recording_details()
        if self.playback_tab is not None:
            self.playback_tab.configure_recording(self.selected_recording())
        self.status_var.set(f"已选择 {self.recording_var.get()}，可点击“生成并查看”。")

    def selected_recording(self) -> IQRecording | None:
        selected_name = self.recording_var.get()
        return next((item for item in self.recordings if item.stem == selected_name), None)

    def selected_scene_recording_context(self) -> str:
        representative = self._selected_reconstruction_representative()
        if representative is not None:
            return (
                f"{representative.scene_type}｜{representative.city} / {representative.point}"
                f"｜场景代表 IQ"
            )
        location = self.selected_scene_location
        if location is None:
            return ""
        return f"{location.scene_type}｜{location.city} / {location.point}｜场景关联 IQ"

    def _selected_reconstruction_representative(self):
        module = self.reconstruction_module
        if module is None:
            return None
        selected = module._selected_representative()
        return selected if selected is not None else module.complex_base_representative

    def selected_scene_recording(self) -> IQRecording | None:
        representative = self._selected_reconstruction_representative()
        if representative is not None:
            try:
                return recording_from_paths(
                    representative.recording_stem,
                    Path(representative.wsm_file),
                    (Path(representative.ws1_file), Path(representative.ws2_file)),
                )
            except Exception as exc:
                raise ValueError(
                    f"无法读取当前选中场景代表 IQ“{representative.recording_stem}”：{exc}"
                ) from exc

        location = self.selected_scene_location
        if location is None:
            raise ValueError(
                "请先在“信号重构”模块选择场景代表 IQ，"
                "或在“场景分类”模块选择地点和关联 IQ。"
            )
        recording_stem = self.scene_iq_recording.get().strip()
        if not recording_stem:
            raise ValueError("当前场景尚未选择关联 IQ 数据。")
        linked_stems = set(getattr(self, "current_scene_iq_stems", ()))
        if recording_stem not in linked_stems:
            raise ValueError(
                f"IQ 数据“{recording_stem}”尚未关联到当前场景"
                f"“{location.city} / {location.point}”，请先完成关联。"
            )
        selected = next((item for item in self.recordings if item.stem == recording_stem), None)
        if selected is not None:
            return selected

        links = list_linked_iq_details(
            Path(self.feature_db_path.get()),
            location.city,
            location.point,
        )
        link = next((item for item in links if item.recording_stem == recording_stem), None)
        if link is None:
            raise ValueError(f"未找到当前场景 IQ 数据“{recording_stem}”的文件关联信息。")
        try:
            selected = recording_from_paths(
                recording_stem,
                Path(link.wsm_file),
                (Path(link.ws1_file), Path(link.ws2_file)),
            )
        except Exception as exc:
            raise ValueError(f"无法读取当前场景关联 IQ“{recording_stem}”：{exc}") from exc
        self.recordings.append(selected)
        self.recordings.sort(key=lambda item: item.stem.casefold())
        self.recording_combo["values"] = [item.stem for item in self.recordings]
        return selected

    def refresh_environment_spectra(self) -> None:
        try:
            self.spectrum_groups = discover_spectrum_groups(Path(self.spectrum_dir.get()))
        except Exception as exc:
            messagebox.showerror("读取频谱文件夹失败", str(exc))
            return
        cities = sorted({group.city for group in self.spectrum_groups})
        self.spectrum_city_combo["values"] = cities
        if self.spectrum_city.get() not in cities:
            self.spectrum_city.set(cities[0] if cities else "")
        self._update_spectrum_points()
        self._log(f"已加载环境频谱测点组：{len(self.spectrum_groups)}")

    def _update_spectrum_points(self) -> None:
        points = sorted({group.point for group in self.spectrum_groups if group.city == self.spectrum_city.get()})
        self.spectrum_point_combo["values"] = points
        if self.spectrum_point.get() not in points:
            self.spectrum_point.set(points[0] if points else "")
        self._update_spectrum_polarizations()

    def _update_spectrum_polarizations(self) -> None:
        polarizations = sorted(
            {
                group.polarization
                for group in self.spectrum_groups
                if group.city == self.spectrum_city.get() and group.point == self.spectrum_point.get()
            }
        )
        self.spectrum_polarization_combo["values"] = polarizations
        if self.spectrum_polarization.get() not in polarizations:
            self.spectrum_polarization.set(polarizations[0] if polarizations else "")
        self._update_spectrum_bands()

    def _selected_spectrum_group(self) -> SpectrumGroup | None:
        return next(
            (
                group
                for group in self.spectrum_groups
                if group.city == self.spectrum_city.get()
                and group.point == self.spectrum_point.get()
                and group.polarization == self.spectrum_polarization.get()
            ),
            None,
        )

    def _update_spectrum_bands(self) -> None:
        group = self._selected_spectrum_group()
        bands = (["30M-6G (All)"] + list(group.bands)) if group is not None else []
        self.spectrum_band_combo["values"] = bands
        if self.spectrum_band.get() not in bands:
            self.spectrum_band.set(bands[0] if bands else "")

    def update_recording_details(self) -> None:
        recording = self.selected_recording()
        if recording is None:
            text = "未找到 IQ 数据。"
        else:
            text = (
                f"{recording.stem}\n"
                f"数据卷数：{len(recording.volumes)}\n"
                f"样本数：{recording.total_samples:,}\n"
                f"采样率：{recording.sample_rate_hz / 1e6:.6g} MS/s\n"
                f"中心频率：{recording.center_frequency_mhz:.6g} MHz\n"
                f"频率范围：{recording.center_frequency_mhz - recording.sample_rate_hz / 2e6:.6g}"
                f" ~ {recording.center_frequency_mhz + recording.sample_rate_hz / 2e6:.6g} MHz\n"
                f"时长：{recording.duration_s:.3f} s\n"
                f"参考电平：{recording.reference_level_dbm:.3f} dBm"
            )
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(tk.END, text)
        self.details_text.configure(state=tk.DISABLED)

    def start_playback(self) -> None:
        if self.playback_tab is not None:
            self.notebook.select(self.playback_tab)
            self.playback_tab.start()

    def pause_playback(self) -> None:
        if self.playback_tab is not None:
            self.playback_tab.pause()

    def stop_playback(self) -> None:
        if self.playback_tab is not None:
            self.playback_tab.stop()

    def run_analysis(self) -> None:
        selected = self.selected_recording()
        if selected is None:
            messagebox.showwarning("未选择数据", "请先选择一组 IQ 数据。")
            return

        plots = tuple(name for name, var in self.plot_vars.items() if var.get())
        if not plots:
            messagebox.showwarning("未选择分析内容", "请至少选择一项分析内容。")
            return

        try:
            start_s = float(self.start_sec.get() or "0")
            duration_s = None if not self.duration_sec.get().strip() else float(self.duration_sec.get())
            max_points = int(self.max_points.get())
            spectrogram_points = int(self.spectrogram_points.get())
        except ValueError as exc:
            messagebox.showerror("参数无效", str(exc))
            return

        self.run_button.configure(state=tk.DISABLED)
        self.status_var.set(f"正在分析 {selected.stem} ...")
        self._set_summary("正在分析... 完成后图表会直接显示在右侧。\n")
        self._log(f"正在分析 {selected.stem} ...")
        self._clear_figure_tabs()
        thread = threading.Thread(
            target=self._run_worker,
            args=(selected, plots, start_s, duration_s, max_points, spectrogram_points),
            daemon=True,
        )
        thread.start()

    def run_segment_detection(self) -> None:
        selected = self.selected_recording()
        if selected is None:
            messagebox.showwarning("未选择数据", "请先选择一组 IQ 数据。")
            return
        try:
            window_ms = float(self.detect_window_ms.get())
            interval_ms = float(self.detect_interval_ms.get())
            extract_ms = float(self.detect_extract_ms.get())
            max_windows = int(self.detect_max_windows.get())
        except ValueError as exc:
            messagebox.showerror("检测参数无效", str(exc))
            return
        if min(window_ms, interval_ms, extract_ms, max_windows) <= 0:
            messagebox.showerror("检测参数无效", "所有检测参数都必须为正数。")
            return

        self.detect_button.configure(state=tk.DISABLED)
        self.detect_progress.set(0)
        self.detect_progress_text.set("0%　正在准备功率扫描")
        self.status_var.set(f"正在检测 {selected.stem} 的功率最强片段 ...")
        self._set_detection_text("正在扫描全记录窗口并比较平均功率...\n")
        self.notebook.select(self.detection_tab)
        thread = threading.Thread(
            target=self._detection_worker,
            args=(selected, window_ms, interval_ms, extract_ms, max_windows, self.detect_save_output.get()),
            daemon=True,
        )
        thread.start()

    def run_environment_spectrum(self) -> None:
        group = self._selected_spectrum_group()
        if group is None:
            messagebox.showwarning("未选择频谱数据", "请选择城市、测点和极化方式。")
            return
        band = self.spectrum_band.get()
        self.spectrum_button.configure(state=tk.DISABLED)
        self.status_var.set(f"正在生成 {group.point} 的最大值保持频谱 ...")
        self._set_spectrum_text("正在读取 CSV 扫描文件并进行跨文件最大值保持...\n")
        self.module_notebook.select(1)
        self.spectrum_notebook.select(self.spectrum_text)
        thread = threading.Thread(target=self._spectrum_worker, args=(group, band), daemon=True)
        thread.start()

    def _spectrum_worker(self, group: SpectrumGroup, band: str) -> None:
        try:
            result = aggregate_max_hold(group, band, Path(self.out_dir.get()))
            self.messages.put(("spectrum_done", result))
        except Exception as exc:
            self.messages.put(("spectrum_error", str(exc)))

    def _detection_worker(self, recording, window_ms, interval_ms, extract_ms, max_windows, save_output) -> None:
        try:
            def report(percent: float, stage: str) -> None:
                self.messages.put(("detection_progress", (percent, stage)))

            result = detect_representative_segments(
                recording=recording,
                output_root=Path(self.out_dir.get()),
                window_ms=window_ms,
                interval_ms=interval_ms,
                extract_ms=extract_ms,
                max_windows=max_windows,
                progress=report,
                save_output=save_output,
            )
            self.messages.put(("detection_done", result))
        except Exception as exc:
            self.messages.put(("detection_error", str(exc)))

    def _run_worker(self, recording, plots, start_s, duration_s, max_points, spectrogram_points) -> None:
        try:
            data = prepare_analysis_data(
                recording=recording,
                start_s=start_s,
                duration_s=duration_s,
                max_points=max_points,
                spectrogram_points=spectrogram_points,
                need_spectrogram="spectrogram" in plots,
            )
            figures: list[tuple[str, Figure]] = []
            build_errors: list[tuple[str, str]] = []
            for plot_name in plots:
                if plot_name == "summary":
                    continue
                builder = FIGURE_BUILDERS.get(plot_name)
                if builder is None:
                    continue
                try:
                    figures.append((plot_name, builder(data)))
                except Exception as exc:
                    build_errors.append((plot_name, str(exc)))
            self.messages.put(("done", (data, plots, figures, build_errors)))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                data, plots, figures, build_errors = payload
                self._show_analysis(data, plots, figures, build_errors)
                self.run_button.configure(state=tk.NORMAL)
                self.status_var.set(f"完成。已绘制 {data.recording.stem} 的图表。")
            elif kind == "error":
                self.run_button.configure(state=tk.NORMAL)
                self.status_var.set("分析失败。")
                self._log(f"错误：{payload}")
                messagebox.showerror("分析失败", str(payload))
            elif kind == "detection_done":
                self.detect_button.configure(state=tk.NORMAL)
                self.detect_progress.set(100)
                self.detect_progress_text.set("100%　功率最强片段检测完成")
                self._show_detection(payload)
                self.status_var.set(f"已找到 {payload.recording.stem} 的平均功率最强片段。")
            elif kind == "detection_error":
                self.detect_button.configure(state=tk.NORMAL)
                self.detect_progress.set(0)
                self.detect_progress_text.set("检测失败，请检查参数或数据文件")
                self.status_var.set("片段检测失败。")
                self._log(f"片段检测错误：{payload}")
                messagebox.showerror("片段检测失败", str(payload))
            elif kind == "detection_progress":
                percent, stage = payload
                self.detect_progress.set(percent)
                self.detect_progress_text.set(f"{percent:.0f}%　{stage}")
                self.status_var.set(f"自动检测 {percent:.0f}%：{stage}")
            elif kind == "spectrum_done":
                self.spectrum_button.configure(state=tk.NORMAL)
                self._show_environment_spectrum(payload)
                self.status_var.set(f"已生成 {payload.group.point} 的最大值保持频谱。")
            elif kind == "spectrum_error":
                self.spectrum_button.configure(state=tk.NORMAL)
                self.status_var.set("环境频谱生成失败。")
                self._log(f"频谱处理错误：{payload}")
                messagebox.showerror("环境频谱生成失败", str(payload))
            elif kind == "feature_progress":
                done, total, label = payload
                self.status_var.set(f"正在建立频谱特征库：{done}/{total}  {label}")
            elif kind == "feature_build_done":
                self.feature_build_button.configure(state=tk.NORMAL)
                self._show_feature_build_result(payload)
                self.refresh_feature_library(keep_filters=True)
                self.refresh_scene_catalog(keep_filters=True)
            elif kind == "feature_build_error":
                self.feature_build_button.configure(state=tk.NORMAL)
                self.status_var.set("频谱特征库建立失败。")
                messagebox.showerror("频谱特征库建立失败", str(payload))
            elif kind == "feature_compare_done":
                self.feature_compare_button.configure(state=tk.NORMAL)
                self._show_feature_comparison(payload)
                self.status_var.set(f"已完成 {len(payload.records)} 个测点的特征对比。")
            elif kind == "feature_compare_error":
                self.feature_compare_button.configure(state=tk.NORMAL)
                self.status_var.set("测点特征对比失败。")
                messagebox.showerror("测点特征对比失败", str(payload))
        self.after(150, self._drain_messages)

    def _show_analysis(
        self,
        data: AnalysisData,
        plots: tuple[str, ...],
        figures: list[tuple[str, Figure]],
        build_errors: list[tuple[str, str]],
    ) -> None:
        self._clear_figure_tabs()
        self._set_summary(summary_text(data))

        first_tab = None
        for plot_name, figure in figures:
            label = PLOT_LABELS.get(plot_name, plot_name.title())
            tab = FigureTab(self.notebook, figure)
            self.figure_tabs[label] = tab
            self.notebook.add(tab, text=label)
            if first_tab is None:
                first_tab = tab

        for plot_name, error in build_errors:
            self._log(f"已跳过 {PLOT_LABELS.get(plot_name, plot_name)}：{error}")

        self._log(f"已绘制 {data.recording.stem} 的结果：" + "，".join(PLOT_LABELS.get(item, item) for item in plots))
        self.notebook.select(first_tab if first_tab is not None else self.summary_text)

    def _set_summary(self, text: str) -> None:
        self.summary_text.configure(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert(tk.END, text)
        self.summary_text.configure(state=tk.DISABLED)

    def _set_detection_text(self, text: str) -> None:
        self.detection_text.configure(state=tk.NORMAL)
        self.detection_text.delete("1.0", tk.END)
        self.detection_text.insert(tk.END, text)
        self.detection_text.configure(state=tk.DISABLED)

    def _show_detection(self, result: DetectionResult) -> None:
        self._set_detection_text(result_text(result))
        strongest = result.segments[0]
        self.start_sec.set(f"{strongest.extract_start_s:.6f}")
        self.duration_sec.set(f"{strongest.duration_s:.6f}")
        if self.detection_figure_tab is not None:
            self.detection_figure_tab.destroy()
        self.detection_figure_tab = FigureTab(self.detection_figure_host, result.figure)
        self.detection_figure_tab.pack(fill=tk.BOTH, expand=True)
        self.notebook.select(self.detection_tab)
        if result.saved_to_disk:
            self._log(f"功率最强片段已保存到 {result.output_dir}")
        else:
            self._log("功率最强片段检测完成，结果仅显示在界面，未输出文件。")

    def _set_spectrum_text(self, text: str) -> None:
        self.spectrum_text.configure(state=tk.NORMAL)
        self.spectrum_text.delete("1.0", tk.END)
        self.spectrum_text.insert(tk.END, text)
        self.spectrum_text.configure(state=tk.DISABLED)

    def _show_environment_spectrum(self, result: SpectrumResult) -> None:
        self._set_spectrum_text(spectrum_result_text(result))
        if self.spectrum_figure_tab is not None:
            self.spectrum_notebook.forget(self.spectrum_figure_tab)
            self.spectrum_figure_tab.destroy()
        self.spectrum_figure_tab = FigureTab(self.spectrum_notebook, result.figure)
        self.spectrum_notebook.add(self.spectrum_figure_tab, text="最大值保持图")
        self.module_notebook.select(1)
        self.spectrum_notebook.select(self.spectrum_figure_tab)
        self._log(f"环境频谱最大值保持结果已保存到 {result.output_csv}")

    def _set_feature_summary(self, text: str) -> None:
        self.feature_summary_text.configure(state=tk.NORMAL)
        self.feature_summary_text.delete("1.0", tk.END)
        self.feature_summary_text.insert(tk.END, text)
        self.feature_summary_text.configure(state=tk.DISABLED)

    def _show_feature_build_result(self, result: BuildResult) -> None:
        lines = [
            "频谱特征库建立完成。",
            "",
            f"测点/极化数据组：{result.group_count}",
            f"数据库特征记录：{result.profile_count}",
            f"失败记录：{len(result.failed_items)}",
            f"数据库文件：{result.database_path}",
        ]
        if result.failed_items:
            lines.extend(("", "失败项目（最多显示 20 条）：", *result.failed_items[:20]))
        self._set_feature_summary("\n".join(lines))
        self.feature_notebook.select(self.feature_summary_text)
        self.status_var.set(f"频谱特征库建立完成，共 {result.profile_count} 条特征记录。")

    def _show_feature_comparison(self, result: ComparisonResult) -> None:
        self._set_feature_summary(result.summary)
        if self.feature_comparison_tab is not None:
            self.feature_notebook.forget(self.feature_comparison_tab)
            self.feature_comparison_tab.destroy()
        self.feature_comparison_tab = FigureTab(self.feature_notebook, result.figure)
        self.feature_notebook.add(self.feature_comparison_tab, text="测点对比图")
        self.module_notebook.select(self.feature_module)
        self.feature_notebook.select(self.feature_comparison_tab)

    def _clear_figure_tabs(self) -> None:
        for frame in list(self.figure_tabs.values()):
            self.notebook.forget(frame)
            frame.destroy()
        self.figure_tabs.clear()

    def _log(self, text: str) -> None:
        self.log.insert(tk.END, text + "\n\n")
        self.log.see(tk.END)

    def open_output_folder(self) -> None:
        path = Path(self.out_dir.get())
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("打开输出文件夹失败", str(exc))


if __name__ == "__main__":
    IQAnalyzerApp().mainloop()
