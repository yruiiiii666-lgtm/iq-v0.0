# IQ 数据分析与回放软件开发聊天总结

本文档总结本次聊天中围绕 `software\data` 文件夹中的 IQ 测量数据所做的分析、软件开发、功能解释和后续实验室回放思路。

---

## 1. 数据基本情况

`software\data` 文件夹中包含多组 Rohde & Schwarz `IQR-WV` 格式 IQ 数据，每组通常由三个文件组成：

```text
<name>.ws1
<name>.ws2
<name>.wsm
```

例如：

```text
miaofu1895g.ws1
miaofu1895g.ws2
miaofu1895g.wsm
```

其中：

- `.ws1` 和 `.ws2` 是主要 IQ 数据文件，体积约 1.5 GB；
- `.wsm` 是辅助元数据文件，只有几十字节；
- 软件会把同名的 `.ws1` 和 `.ws2` 当作连续数据卷读取。

已识别的数据组包括：

```text
miaofu1895g
miaofu253g
miaofu869m
miaofu873m
```

文件头中解析到的关键信息包括：

```text
TYPE:IQR-WV
SECTORSIZE:8192
RESOLUTION:16
COMPONENTS:IQ
CHANRATE0: 40000000.000
```

因此当前数据按如下方式读取：

```text
8192 字节文件头之后
小端 int16
I,Q,I,Q,... 交织排列
```

读取后做归一化：

```text
I = 原始 I / 32768
Q = 原始 Q / 32768
```

---

## 2. 采样率、带宽和中心频率

文件头中的：

```text
CHANRATE0: 40000000.000
```

表示采样率约为：

```text
40 MS/s
```

对复数 IQ 基带数据，采样率 `Fs` 对应的双边频率范围是：

```text
-Fs/2 ~ +Fs/2
```

因此 `40 MS/s` 对应的基带频率跨度约为：

```text
-20 MHz ~ +20 MHz
```

也就是总带宽约 `40 MHz`。

文件头中的 `CHANFREQ0` 为 `0`，没有记录真实射频中心频率。后来确定：

```text
文件名中的数字就是真实中心频率，单位 MHz
```

例如：

| 数据组 | 中心频率 |
|---|---:|
| `miaofu1895g` | `1895 MHz` |
| `miaofu253g` | `253 MHz` |
| `miaofu869m` | `869 MHz` |
| `miaofu873m` | `873 MHz` |

实际频率坐标计算方式：

```text
实际频率 = 文件名中心频率 + 基带频率
```

例如 `miaofu1895g`：

```text
中心频率 = 1895 MHz
采样率 = 40 MHz
频率范围 = 1875 MHz ~ 1915 MHz
```

---

## 3. 已开发的软件

已新建文件夹：

```text
iq_analyzer
```

主要文件包括：

```text
iq_reader.py
plot_iq.py
iq_embedded.py
iq_analyzer_gui.py
run_gui.bat
README.md
IQ_ANALYZER_GUIDE.md
```

### 3.1 `iq_reader.py`

负责读取和解析 IQ 数据。

主要功能：

- 自动发现 `.ws1/.ws2/.wsm` 文件组；
- 解析 Rohde & Schwarz `IQR-WV` 文件头；
- 获取采样率、采样点数、参考电平；
- 从文件名提取中心频率；
- 用 `numpy.memmap` 按需读取大文件；
- 支持按采样点窗口读取；
- 支持对大数据做等间隔抽样。

### 3.2 `plot_iq.py`

命令行批量分析与出图脚本。

支持输出：

- Summary 文本；
- 时域图；
- 星座图；
- PSD 频谱图；
- 时频图；
- 直方图。

命令示例：

```powershell
python .\iq_analyzer\plot_iq.py --data-dir .\data --recording all --out-dir .\iq_analyzer\output
```

### 3.3 `iq_embedded.py`

GUI 内嵌 Matplotlib 绘图模块。

该模块负责直接生成 Matplotlib Figure，而不是先保存 PNG 再贴到界面里。

这样右侧图窗可以：

- 自适应窗口；
- 缩放；
- 平移；
- 保存图片；
- 查看坐标。

### 3.4 `iq_analyzer_gui.py`

主 GUI 软件。

界面分为：

- 左侧控制区；
- 右侧结果显示区。

左侧控制区支持：

- 选择数据目录；
- 选择输出目录；
- 选择 recording；
- 设置分析窗口；
- 设置绘图类型；
- 设置 Playback 参数；
- 启动分析；
- 启动、暂停、停止播放。

右侧结果区支持：

- Summary；
- Playback；
- Log；
- Time；
- Constellation；
- Spectrum；
- Spectrogram；
- Histogram。

---

## 4. GUI 分析功能

### 4.1 Summary

显示当前选中时间段的统计信息。

包括：

- recording 名称；
- volume 数量；
- 总采样点数；
- 采样率；
- 中心频率；
- 频率范围；
- 总时长；
- 分析起点；
- 分析时长；
- 参考电平；
- 实际用于绘图和统计的点数；
- 抽样步长；
- I/Q 均值和标准差；
- `|IQ|` 最小值、平均值、最大值；
- 平均功率；
- 峰值幅度；
- RMS 幅度。

注意：当前 Summary 基于 `Max points` 抽样后的数据统计，不一定是完整时间段全部点统计。

### 4.2 Time

Time 页面包含三个图：

1. I/Q 随时间变化；
2. 复数幅度 `|IQ|` 随时间变化；
3. 相位随时间变化。

I 和 Q 的幅度是归一化数字幅度：

```text
I = 原始 I / 32768
Q = 原始 Q / 32768
```

所以幅度一般在：

```text
-1 ~ 1
```

这里没有电压或 dBm 单位，因为文件中没有完整的 ADC 电压标定、链路增益和阻抗信息。

复数幅度计算：

```text
|IQ| = sqrt(I^2 + Q^2)
```

相位计算：

```text
phase = unwrap(angle(I + jQ))
```

Time 图可用于观察：

- 信号幅度是否稳定；
- 是否有突发；
- 是否有削顶；
- 相位是否连续；
- 是否存在频率偏移。

### 4.3 Constellation

星座图把每个 IQ 点画在二维平面中：

```text
横轴 = I
纵轴 = Q
```

用途：

- 看 IQ 分布；
- 判断是否有 DC 偏置；
- 判断是否有 I/Q 不平衡；
- 观察调制形态；
- 观察噪声散布。

### 4.4 Spectrum

频谱图使用 Welch 方法计算 PSD：

```text
scipy.signal.welch(...)
```

频谱横轴已改为实际频率 MHz：

```text
实际频率 = 文件名中心频率 + 基带频率
```

图中会显示：

- 中心频率虚线；
- 最大 PSD 峰值点；
- 峰值频率；
- 峰值相对功率。

峰值点计算：

```text
peak_index = argmax(psd)
peak_frequency = frequency[peak_index]
```

峰值标注已优化为自动调整位置，避免文字跑到图外。

### 4.5 Spectrogram

时频图使用：

```text
scipy.signal.spectrogram(...)
```

横轴是时间，纵轴是实际频率 MHz，颜色表示 PSD 强度。

用于观察：

- 信号是否随时间漂移；
- 是否有突发；
- 是否扫频；
- 是否跳频；
- 干扰是否只在某些时刻出现。

时频图中也会标出整张图中的最大能量点。

### 4.6 Histogram

直方图包含：

1. I/Q 归一化幅度分布；
2. `|IQ|` dBFS 分布。

用于观察：

- I/Q 是否以 0 为中心；
- 是否存在 DC 偏置；
- 是否有削顶；
- 幅度分布和动态范围。

---

## 5. GUI 参数说明

### 5.1 Start (s)

分析窗口起始时间，单位秒。

对应采样点：

```text
start_sample = Start × sample_rate
```

### 5.2 Duration (s)

分析窗口时长，单位秒。

对应原始点数：

```text
sample_count = Duration × sample_rate
```

例如：

```text
Duration = 0.05 s
sample_rate = 40 MS/s
sample_count = 2,000,000
```

### 5.3 Max points

普通分析和绘图最多使用的点数。

如果选中时间段原始点数大于 `Max points`，软件会等间隔抽样：

```text
stride = ceil(原始点数 / Max points)
```

例如：

```text
原始点数 = 2,000,000
Max points = 200,000
stride = 10
```

表示每 10 个点取 1 个。

### 5.4 Spectrogram pts

时频图使用的连续 IQ 点数。

点数越大，时频图覆盖时间越长，计算越慢。

---

## 6. Playback 功能

后来新增了 `Playback` 循环/播放功能，用于动态播放一小段时域数据。

### 6.1 Playback 显示模式

支持三种模式：

| 模式 | 含义 |
|---|---|
| `Synthesized real waveform` | 由 IQ 合成实值波形 |
| `I/Q components` | 同时播放 I 和 Q |
| `Magnitude |IQ|` | 播放包络 |

### 6.2 IQ 合成实值波形

IQ 可以合成一个实值时域波形：

```text
y(t) = I(t) cos(2πf_c t) - Q(t) sin(2πf_c t)
```

当前 Playback 中的 `f_c` 是：

```text
Visual carrier (MHz)
```

它是可视化载波，不是文件名中的真实中心频率。

原因是采样率约为 `40 MS/s`，真实中心频率可能是 `1895 MHz` 或 `869 MHz`。如果直接用真实射频中心频率画时域载波，会发生混叠，不适合直观显示。

### 6.3 Playback 参数

Playback 参数包括：

| 参数 | 含义 |
|---|---|
| `Start (s)` | 播放起点 |
| `Loop duration (s)` | 播放区间长度 |
| `Window (ms)` | 每帧显示的时间窗口 |
| `Step (ms)` | 基础前进步长 |
| `Speed (x)` | 播放速度倍数 |
| `FPS` | 每秒刷新帧数 |
| `Max points` | 每帧最多显示点数 |
| `Visual carrier (MHz)` | 合成实值波形时使用的可视化载波 |

实际每帧前进时间：

```text
actual_step = Step × Speed
```

### 6.4 Playback 优化

根据反馈，Playback 做了优化：

- 新增 `Speed (x)`；
- 默认速度改为 `5x`；
- 默认 FPS 改为 `20`；
- 播放到设定区间末尾后自动暂停；
- 新增 `Loop when finished` 复选框，勾选后才循环；
- 播放过程中纵轴固定为 `-1 ~ 1`，方便观察幅度变化。

---

## 7. `.wsm` 文件说明

`.wsm` 是辅助元数据文件，不是真正的 IQ 数据本体。

例如 `miaofu1895g.wsm` 内容大致为：

```text
RMH  0  miaofu1895g;1;1;0;0
AGC  0  CH0:Lev -23.00 dBm;
```

它主要记录：

- 数据组名称；
- AGC 或通道电平信息。

真正的大体积 IQ 数据在：

```text
.ws1
.ws2
```

中。

---

## 8. 实验室回放思路

后来讨论了如何把约 55 个地点采集到的 IQ 数据用于实验室回放。

### 8.1 直接 IQ 回放

如果目标是让实验室接收机看到和现场某个地点类似的输入，可以做 IQ record-and-playback。

基本链路：

```text
IQ 文件
-> SDR 或矢量信号源
-> 设置采样率
-> 设置中心频率
-> RF 输出
-> 衰减器
-> 被测接收机
```

信号源内部会完成：

```text
s_RF(t) = Re{ x_BB(t) exp(j2πf_c t) }
```

即：

```text
s_RF(t) = I(t) cos(2πf_c t) - Q(t) sin(2πf_c t)
```

### 8.2 回放需要注意

需要设备支持：

- 对应中心频率；
- 40 MS/s 复采样率；
- 可控输出功率；
- 足够带宽。

实验室连接建议：

```text
SDR/信号源 RF OUT
-> 衰减器 30~60 dB
-> 被测接收机 RF IN
```

避免未经许可空口发射。

### 8.3 直接回放的含义

直接回放某地点 IQ，复现的是：

```text
该地点接收天线口看到的最终信号
```

它已经包含：

- 路径损耗；
- 多径；
- 噪声；
- 干扰；
- 频偏；
- 接收链路影响。

它适合测试接收机或算法在该地点信号下的表现。

但它不等于完整复现传播环境。如果要让任意新发射信号经过同样信道，需要做信道估计或信道模拟器。

---

## 9. 55 个地点数据的规律分析建议

建议对 55 个地点批量提取特征，形成表格。

每个地点可提取：

```text
recording
center_mhz
sample_rate
duration
rms_dbfs
peak_dbfs
papr_db
peak_freq_mhz
peak_offset_mhz
occupied_bandwidth_mhz
noise_floor_db
snr_db
dc_offset_i
dc_offset_q
iq_imbalance
burst_ratio
```

可分析规律：

1. 强度规律：哪个地点信号强、哪个地点弱；
2. 频偏规律：峰值是否偏离中心；
3. 带宽规律：不同地点带宽是否一致；
4. 时间规律：是否有突发、周期性、衰落；
5. 干扰规律：哪些地点频谱更复杂；
6. 聚类：把地点分成几类典型场景。

### 9.1 代表性回放集

可从 55 个地点中选出：

```text
最强地点
最弱地点
平均地点
频偏最大地点
干扰最强地点
突发最明显地点
频谱最干净地点
频谱最复杂地点
```

用于实验室回放测试，比盲目回放全部 55 个地点更有实验价值。

---

## 10. 后续建议开发功能

建议后续继续给软件增加：

1. 批量分析 55 个地点；
2. 自动输出 CSV 指标表；
3. 自动排序和筛选典型地点；
4. 自动生成回放文件；
5. 支持导出 GNU Radio / USRP 可用的 complex64 或 sc16 IQ 文件；
6. 支持生成回放配置清单；
7. 支持完整时间段统计，而不是只统计抽样点；
8. 支持峰值保持抽样，避免漏掉短时突发；
9. 支持 SNR、占用带宽、PAPR、频偏等高级指标；
10. 支持地点聚类和典型场景选择。

---

## 11. 当前软件使用方式

启动 GUI：

```powershell
python .\iq_analyzer\iq_analyzer_gui.py
```

或双击：

```text
iq_analyzer\run_gui.bat
```

命令行批量分析：

```powershell
python .\iq_analyzer\plot_iq.py --data-dir .\data --recording all --out-dir .\iq_analyzer\output
```

---

## 12. 已生成的重要文档

详细说明文档：

```text
iq_analyzer\IQ_ANALYZER_GUIDE.md
```

本聊天总结文档：

```text
iq_analyzer\CHAT_SUMMARY.md
```
