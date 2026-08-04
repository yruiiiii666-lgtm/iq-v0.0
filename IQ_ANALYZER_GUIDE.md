# IQ Analyzer 使用与分析说明

本文档说明 `IQ Analyzer` 软件界面中每个功能、每个参数的含义，以及各类图是如何由 IQ 数据计算得到的。本文默认数据文件为 Rohde & Schwarz `IQR-WV` 格式，文件后缀类似：

```text
miaofu1895g.ws1
miaofu1895g.ws2
miaofu1895g.wsm
```

其中 `.ws1` 和 `.ws2` 是同一次采集的两个数据卷，软件会把它们按顺序拼接成一段连续 IQ 数据来分析。

---

## 1. IQ 数据是什么

IQ 数据是复数形式的采样信号，每个采样点可以写成：

```text
x[n] = I[n] + jQ[n]
```

其中：

- `I[n]` 是同相分量，In-phase；
- `Q[n]` 是正交分量，Quadrature；
- `n` 是采样点序号；
- `j` 是虚数单位。

可以把每个 IQ 采样点理解成二维平面上的一个点：

```text
横轴 = I
纵轴 = Q
```

IQ 数据常用于描述无线电信号的复基带形式。它保留了信号的幅度和相位信息，因此可以做时域、频域、星座图、时频图等分析。

---

## 2. 数据文件读取方式

### 2.1 文件识别

软件会在 `Data` 文件夹中搜索形如：

```text
*.ws1
*.ws2
*.wsm
```

的文件，并按文件名前缀自动分组。例如：

```text
miaofu1895g.ws1
miaofu1895g.ws2
miaofu1895g.wsm
```

会被识别为一组数据，名称为：

```text
miaofu1895g
```

### 2.2 文件头信息

当前数据的 `.ws1/.ws2` 文件头中包含类似信息：

```text
TYPE:IQR-WV
SECTORSIZE:8192
RESOLUTION:16
COMPONENTS:IQ
CHANRATE0: 40000000.000
SAMPLES:...
CHANREFLVL0: -23.000
```

软件主要读取这些字段：

| 字段 | 含义 |
|---|---|
| `SECTORSIZE` | 文件头大小，当前为 `8192` 字节 |
| `RESOLUTION` | 数据位宽，当前为 `16 bit` |
| `COMPONENTS` | 数据类型，当前为 `IQ` |
| `CHANRATE0` | IQ 采样率，当前约为 `40 MS/s` |
| `SAMPLES` | 当前 volume 中的复数 IQ 点数 |
| `CHANREFLVL0` | 仪器记录的参考电平 |

### 2.3 IQ 数值读取

文件头之后的数据按小端 `int16` 交织存储：

```text
I0, Q0, I1, Q1, I2, Q2, ...
```

软件读取后会做归一化：

```text
I = 原始 I / 32768
Q = 原始 Q / 32768
```

因此界面里看到的 I/Q 幅度通常在：

```text
-1 ~ 1
```

附近。

注意：这个幅度不是伏特，也不是 dBm，而是数字满量程归一化幅度。它表示相对于 16 bit 数字采样满量程的比例。

---

## 3. 采样率、带宽与中心频率

### 3.1 采样率

界面里显示的 `Sample rate` 来自文件头字段：

```text
CHANRATE0
```

当前数据约为：

```text
40,000,000 samples/s = 40 MS/s
```

它表示每秒采集 4000 万个复数 IQ 点。

### 3.2 采样率和带宽的关系

对于复数 IQ 基带信号，采样率 `Fs` 通常对应的基带频率范围为：

```text
-Fs/2 ~ +Fs/2
```

因此当：

```text
Fs = 40 MHz
```

时，基带频率范围为：

```text
-20 MHz ~ +20 MHz
```

总频率跨度是：

```text
40 MHz
```

所以 `40 MS/s` 是采样率；对于复数 IQ 数据，它也对应约 `40 MHz` 的可表示频率跨度。

### 3.3 中心频率

当前数据文件头里的 `CHANFREQ0` 为 `0`，没有直接记录真实射频中心频率。因此软件按你的规则从文件名中提取中心频率，单位为 MHz。

例如：

| 文件名 | 提取的中心频率 |
|---|---:|
| `miaofu1895g` | `1895 MHz` |
| `miaofu253g` | `253 MHz` |
| `miaofu869m` | `869 MHz` |
| `miaofu873m` | `873 MHz` |

如果中心频率为 `Fc`，采样率为 `Fs`，那么实际频率坐标为：

```text
实际频率 = Fc + 基带频率
```

例如 `miaofu1895g`：

```text
中心频率 Fc = 1895 MHz
采样率 Fs = 40 MHz
基带范围 = -20 MHz ~ +20 MHz
实际频率范围 = 1875 MHz ~ 1915 MHz
```

---

## 4. 界面功能说明

### 4.1 Data

`Data` 是 IQ 数据所在文件夹。

通常选择：

```text
software\data
```

软件会在该目录下自动查找 `.ws1/.ws2/.wsm` 文件。

### 4.2 Output

`Output` 是输出目录。

当前 GUI 主要是在右侧直接绘图。命令行模式或手动保存图片时，会使用这个目录保存图像和统计结果。

### 4.3 Generate and View

点击后，软件会：

1. 读取当前选择的 recording；
2. 根据 `Start` 和 `Duration` 选取时间段；
3. 根据 `Max points` 对数据进行等间隔抽样；
4. 计算统计量和频谱；
5. 在右侧生成对应图页。

### 4.4 Open Output Folder

打开输出目录，方便查看保存的图像或命令行批量分析结果。

### 4.5 Playback

`Playback` 是新增的时域循环播放功能。

它不会改变原来的 Summary、Time、Spectrum 等分析功能，而是在右侧 `Playback` 标签页中动态显示一小段时域数据。默认播放到设定时间段末尾后会自动暂停；如果勾选 `Loop when finished`，则会自动回到起点继续播放。

播放可以显示三种模式：

| 模式 | 含义 |
|---|---|
| `Synthesized real waveform` | 用 I/Q 合成一个实值时域波形 |
| `I/Q components` | 同时播放 I 和 Q 两路归一化采样值 |
| `Magnitude |IQ|` | 播放复数幅度包络 |

### 4.6 Recording

选择要分析的数据组。

例如：

```text
miaofu1895g
miaofu253g
miaofu869m
miaofu873m
```

软件会自动把同名的 `.ws1` 和 `.ws2` 当成连续数据读取。

### 4.7 Refresh Data

重新扫描 `Data` 文件夹。

当你更换数据目录、添加新文件或删除文件后，需要点击这个按钮。

---

## 5. 参数说明

### 5.1 Start (s)

分析开始时间，单位是秒。

例如：

```text
Start = 5
```

表示从整段数据的第 5 秒开始分析。

它对应的采样点序号为：

```text
start_sample = Start × sample_rate
```

例如采样率为 `40 MS/s`：

```text
Start = 5 s
start_sample = 5 × 40,000,000 = 200,000,000
```

### 5.2 Duration (s)

分析时间长度，单位是秒。

例如：

```text
Duration = 0.05
```

表示从 `Start` 开始，分析后面 `0.05 s` 的数据。

原始采样点数为：

```text
sample_count = Duration × sample_rate
```

如果采样率为 `40 MS/s`：

```text
Duration = 0.05 s
sample_count = 0.05 × 40,000,000 = 2,000,000
```

也就是说，这段时间内原始包含 200 万个 IQ 点。

如果 `Duration` 留空，则表示从 `Start` 开始一直分析到文件末尾。不过数据很大，通常不建议一开始就分析全文件。

### 5.3 Max points

`Max points` 控制普通绘图和统计最多使用多少个点。

由于原始 IQ 数据非常大，如果直接画全部点，会非常慢，也不利于观察。因此软件会对选中的时间段做等间隔抽样。

抽样步长为：

```text
stride = ceil(选中时间段原始点数 / Max points)
```

然后取：

```text
第 0 个点、第 stride 个点、第 2×stride 个点、第 3×stride 个点 ...
```

例如：

```text
采样率 = 40 MS/s
Duration = 0.05 s
原始点数 = 2,000,000
Max points = 200,000
```

则：

```text
stride = ceil(2,000,000 / 200,000) = 10
```

也就是每 10 个点取 1 个，最后用于显示和普通统计的是约 20 万个点。

注意：

- 这是等间隔抽样，不是随机抽样；
- 它适合快速观察整体波形和分布；
- 如果有非常窄的瞬态尖峰，可能被抽样跳过；
- 如果要严格统计完整时间段，可以把 `Max points` 调大，或者后续增加“完整统计”功能。

### 5.4 Spectrogram pts

`Spectrogram pts` 控制时频图使用多少个连续 IQ 点。

时频图需要对连续数据做短时傅里叶变换，因此它不能像普通时域图那样随便隔点抽样，而是需要取一段连续数据。

例如：

```text
Spectrogram pts = 1,048,576
```

表示最多取 1,048,576 个连续 IQ 点来计算时频图。

点数越大：

- 能覆盖更长时间；
- 计算更慢；
- 内存占用更高；
- 时频图细节可能更丰富。

点数越小：

- 计算更快；
- 适合快速预览；
- 时频图时间覆盖范围更短。

### 5.5 Playback Start (s)

播放循环的起始时间，单位秒。

它和分析窗口里的 `Start (s)` 类似，但只用于 Playback 页。

对应采样点：

```text
play_start_sample = Playback Start × sample_rate
```

### 5.6 Playback Loop duration (s)

播放循环的总长度，单位秒。

例如：

```text
Playback Start = 0
Loop duration = 0.05
```

表示 Playback 会在 `0 ~ 0.05 s` 之间循环播放。

如果播放位置到达这个区间末尾，软件会自动回到起点。

### 5.7 Playback Window (ms)

每一帧显示多长的时域窗口，单位毫秒。

例如：

```text
Window = 1 ms
```

表示每一帧画当前时刻后面 `1 ms` 的数据。

窗口越大：

- 每帧显示的时间范围越长；
- 细节可能更密集；
- 绘图可能更慢。

窗口越小：

- 更适合观察局部快速变化；
- 播放更轻快。

### 5.8 Playback Step (ms)

每一帧向前移动多少时间，单位毫秒。

例如：

```text
Step = 0.2 ms
```

表示每刷新一帧，播放位置向前移动 `0.2 ms`。

如果设置了 `Speed (x)`，实际每帧前进时间为：

```text
actual_step = Step × Speed
```

例如：

```text
Step = 0.2 ms
Speed = 5
```

则每帧实际前进：

```text
1.0 ms
```

如果：

```text
Window = 1 ms
Step = 0.2 ms
```

那么相邻帧之间会有重叠，播放看起来更连续。

### 5.9 Playback Speed (x)

播放速度倍数。

它不会改变每帧窗口长度，只改变每帧向前推进的距离：

```text
actual_step = Step × Speed
```

速度越大，播放位置推进越快。默认值为：

```text
5x
```

如果觉得播放太慢，可以继续增大，比如 `10`、`20`。

### 5.10 Playback FPS

播放刷新率，单位帧每秒。

例如：

```text
FPS = 10
```

表示每秒更新约 10 帧。

FPS 越高，播放越流畅，但 CPU 和磁盘读取压力越大。默认值为 `20`。

### 5.11 Playback Max points

每一帧最多显示多少个点。

播放时每帧会从当前时间窗口中读取数据。如果窗口内原始点数太多，软件会按等间隔抽样，只显示不超过 `Playback Max points` 的点。

例如：

```text
Window = 1 ms
Sample rate = 40 MS/s
原始点数 = 40,000
Playback Max points = 4,000
```

则每 10 个点取 1 个用于播放显示。

### 5.12 Visual carrier (MHz)

该参数只用于 `Synthesized real waveform` 模式。

IQ 数据可以按照下面公式合成一个实值时域波形：

```text
y(t) = I(t) cos(2πf_c t) - Q(t) sin(2πf_c t)
```

这里的 `f_c` 在软件中使用的是 `Visual carrier (MHz)`。

注意：这个参数是**可视化载波频率**，不是文件名里的真实射频中心频率。

原因是当前采样率约为 `40 MS/s`，真实中心频率可能是 `1895 MHz`、`869 MHz` 等，远高于采样率的一半。如果直接用真实射频频率在 40 MS/s 数据上画实值载波，会发生严重混叠，图形不具备直观物理意义。

所以 Playback 中默认使用较低的可视化载波，例如：

```text
1 MHz
```

用来观察 IQ 合成实值波形后的时域形态。

---

## 6. Summary 统计结果说明

`Summary` 显示当前选中时间段的统计结果。

需要注意：当前 Summary 使用的是 `Max points` 抽样后的数据，而不是选中时间段的全部原始点。

### 6.1 Recording

当前分析的数据组名称。

### 6.2 Volumes

数据卷数量。

当前数据一般是：

```text
2
```

对应：

```text
.ws1 + .ws2
```

### 6.3 Total complex samples

整段 recording 中复数 IQ 点总数。

一个复数 IQ 点包括一个 I 和一个 Q。

### 6.4 Sample rate

采样率，单位 Hz。

来自文件头：

```text
CHANRATE0
```

### 6.5 Center frequency

中心频率，单位 MHz。

当前由文件名中的数字提取。

### 6.6 Frequency span

实际频率范围：

```text
center_frequency - sample_rate/2
到
center_frequency + sample_rate/2
```

例如：

```text
中心频率 = 1895 MHz
采样率 = 40 MHz
频率范围 = 1875 MHz ~ 1915 MHz
```

### 6.7 Full duration

整段 recording 的总时长：

```text
Full duration = Total samples / Sample rate
```

### 6.8 Selected start

当前分析窗口的起始时间。

来自界面参数：

```text
Start (s)
```

### 6.9 Selected duration

当前分析窗口的持续时间。

来自界面参数：

```text
Duration (s)
```

### 6.10 Displayed/analyzed points

实际用于绘图和统计的点数。

如果原始窗口点数大于 `Max points`，这里显示的点数通常接近 `Max points`。

### 6.11 Decimation stride

抽样步长。

例如：

```text
Decimation stride = 10
```

表示每 10 个原始点取 1 个。

### 6.12 I mean/std 和 Q mean/std

I、Q 两路的均值和标准差。

均值：

```text
mean(I)
mean(Q)
```

可以用于观察是否存在 DC 偏置。

如果 `I mean` 或 `Q mean` 明显偏离 0，说明信号可能存在直流偏移。

标准差：

```text
std(I)
std(Q)
```

反映 I/Q 数据的波动强度。

### 6.13 Magnitude min/mean/max

复数幅度：

```text
|IQ| = sqrt(I^2 + Q^2)
```

该项显示 `|IQ|` 的最小值、平均值和最大值。

### 6.14 Power mean

平均功率，按归一化 IQ 计算：

```text
power = |IQ|^2 = I^2 + Q^2
Power mean = mean(I^2 + Q^2)
```

这是相对数字满量程的功率，不是直接的瓦特或 dBm。

### 6.15 Peak magnitude

峰值幅度，单位 dBFS。

dBFS 是相对于数字满量程的分贝值：

```text
Peak magnitude = 20 × log10(max(|IQ|))
```

如果结果接近 `0 dBFS`，表示信号接近数字满量程，可能有削顶风险。

### 6.16 RMS magnitude

均方根幅度，单位 dBFS：

```text
RMS = sqrt(mean(|IQ|^2))
RMS magnitude = 20 × log10(RMS)
```

它比峰值更能反映信号整体能量水平。

---

## 7. Time 图说明

`Time` 页面包含三个时域图：

1. I/Q 随时间变化；
2. 复数幅度 `|IQ|` 随时间变化；
3. 相位随时间变化。

### 7.1 横轴 Time

横轴是时间，单位秒：

```text
time = sample_index / sample_rate
```

如果 `Start = 5 s`，那么横轴会从约 `5 s` 开始。

### 7.2 第一幅图：I 和 Q

第一幅图画的是：

```text
I[n]
Q[n]
```

也就是复数 IQ 的实部和虚部。

它们来自原始 `int16` 数据归一化：

```text
I = 原始 I / 32768
Q = 原始 Q / 32768
```

因此幅度通常在：

```text
-1 ~ 1
```

附近。

这里没有物理单位，因为当前文件中没有给出“ADC 数字值到电压”的换算系数，也没有完整的接收链路增益标定。因此只能画归一化数字幅度。

如果需要换算成电压，需要知道仪器或接收系统的标定关系，例如：

```text
1.0 full-scale 对应多少 V
```

如果需要换算成 dBm，则还需要知道阻抗、增益、衰减、参考电平等完整链路信息。

### 7.3 第二幅图：|IQ|

第二幅图画的是复数幅度：

```text
|IQ| = sqrt(I^2 + Q^2)
```

它表示每个采样点在 IQ 平面上离原点的距离。

用途：

- 看信号包络是否稳定；
- 看是否有突发信号；
- 看是否有明显削顶；
- 看信号强弱随时间是否变化。

### 7.4 第三幅图：Phase

第三幅图画的是相位：

```text
phase = unwrap(angle(I + jQ))
```

其中：

```text
angle(I + jQ) = atan2(Q, I)
```

`unwrap` 用于消除相位从 `π` 跳到 `-π` 的突变，使相位曲线更连续。

用途：

- 观察频率偏移；
- 观察相位连续性；
- 观察调制特征；
- 发现相位突变或跳变。

如果相位近似线性上升或下降，通常说明存在频偏。相位斜率与频率偏移有关：

```text
frequency offset = phase_slope / (2π)
```

---

## 8. Constellation 星座图说明

星座图是把一段时间内所有 IQ 点画在二维平面上：

```text
横轴 = I
纵轴 = Q
```

每个采样点对应图上的一个点。

### 8.1 图怎么得到

对选中时间段的数据：

```text
x[n] = I[n] + jQ[n]
```

直接绘制点：

```text
(I[n], Q[n])
```

为了避免点太多，星座图最多会选取一部分点显示。

### 8.2 图怎么看

常见现象：

- 点集中在原点附近：信号弱或接近噪声；
- 点形成圆形云团：可能是噪声、频偏、未同步调制信号；
- 点形成环状：可能存在单音、载波或持续相位旋转；
- 点集中在有限几个位置：可能是 PSK/QAM 等数字调制；
- 点整体偏离原点：可能存在 DC 偏置；
- 点云呈椭圆：可能存在 I/Q 增益不平衡或相位不正交。

### 8.3 分析用途

星座图适合观察：

- IQ 是否平衡；
- 是否有 DC 偏置；
- 调制形式的大致形态；
- 噪声散布；
- 信号是否削顶或失真。

---

## 8A. Playback 循环播放说明

`Playback` 页用于把时域数据按小窗口连续播放，适合观察信号随时间的局部变化。

### 8A.1 播放循环怎么实现

设定：

```text
Playback Start = Ts
Loop duration = Td
Window = Tw
Step = Ts_step
```

软件会从：

```text
Ts
```

开始，每一帧读取长度为：

```text
Tw
```

的一小段 IQ 数据并绘图。下一帧的起点向前移动：

```text
Ts_step
```

当播放位置超过：

```text
Ts + Td
```

时，默认会自动暂停。如果勾选 `Loop when finished`，播放位置会重新回到：

```text
Ts
```

因此形成循环播放。

播放过程中纵轴固定为：

```text
-1 ~ 1
```

这样每一帧的幅度尺度保持一致，便于观察幅度变化，不会因为自动缩放造成视觉跳动。

### 8A.2 I/Q components 模式

该模式直接播放：

```text
I(t)
Q(t)
```

可以观察 I、Q 两路本身是否平稳、是否削顶、是否存在明显跳变。

### 8A.3 Magnitude |IQ| 模式

该模式播放：

```text
|IQ| = sqrt(I^2 + Q^2)
```

也就是包络。

它适合观察：

- 突发信号；
- 包络起伏；
- 幅度调制；
- 信号是否间歇出现。

### 8A.4 Synthesized real waveform 模式

IQ 数据可以合成一个实值时域波形：

```text
y(t) = I(t) cos(2πf_c t) - Q(t) sin(2πf_c t)
```

其中：

- `I(t)` 是 I 分量；
- `Q(t)` 是 Q 分量；
- `f_c` 是载波频率；
- `y(t)` 是合成后的实值波形。

在无线电系统中，如果 `f_c` 使用真实射频中心频率，那么这个公式表示把复基带 IQ 上变频到真实射频后的实信号。

但当前软件 Playback 中使用的是：

```text
Visual carrier (MHz)
```

作为可视化载波，而不是文件名中的真实中心频率。

这样做是因为真实中心频率可能是几百 MHz 或几 GHz，而数据采样率只有约 `40 MS/s`。直接用真实射频载波画图会混叠，不能直观表示真实射频波形。

因此 `Synthesized real waveform` 的用途是：

- 用较低的可视化载波展示 IQ 合成后的实值波形形态；
- 辅助理解 I/Q 如何组合成一个实信号；
- 观察包络和相位变化对实值波形的影响。

它不是对真实 1895 MHz 或 869 MHz 射频波形的逐点重建。

---

## 9. Spectrum 频谱图说明

频谱图显示信号功率随频率的分布。

### 9.1 频谱怎么计算

软件使用 Welch 方法计算功率谱密度 PSD：

```text
freq, psd = scipy.signal.welch(...)
```

Welch 方法会把信号分段、加窗、做 FFT，然后平均，以获得比较平滑的频谱估计。

当前使用：

```text
window = hann
return_onesided = False
scaling = density
```

因为 IQ 是复数信号，所以频率范围是双边频谱：

```text
-Fs/2 ~ +Fs/2
```

### 9.2 横轴 Frequency

横轴是实际频率，单位 MHz。

计算方式：

```text
实际频率 = 文件名提取的中心频率 + 基带频率
```

例如：

```text
中心频率 = 1895 MHz
基带频率 = +3 MHz
实际频率 = 1898 MHz
```

### 9.3 纵轴 PSD

纵轴是相对功率谱密度：

```text
PSD(dB) = 10 × log10(psd)
```

这里的 dB 是相对值，不是绝对 dBm。原因是当前 IQ 幅度是归一化数字值，没有完整的功率标定。

### 9.4 中心频率线

图中的红色虚线表示中心频率：

```text
Center frequency
```

它来自文件名数字。

### 9.5 最大峰值点

软件会在 PSD 中寻找最大值：

```text
peak_index = argmax(psd)
peak_frequency = frequency[peak_index]
peak_power = PSD[peak_index]
```

然后在图上标出：

```text
Peak
频率 MHz
功率 dB
```

### 9.6 分析用途

频谱图可用于：

- 判断信号主要能量集中在哪个频率；
- 观察是否存在频偏；
- 观察信号带宽；
- 观察杂散、干扰、谐波；
- 比较不同采集文件的频谱差异。

---

## 10. Spectrogram 时频图说明

时频图显示频率成分如何随时间变化。

### 10.1 时频图怎么计算

软件使用短时傅里叶变换思想：

```text
scipy.signal.spectrogram(...)
```

把一段连续 IQ 数据切成很多短窗口，每个窗口做频谱，然后按时间排列。

当前使用：

```text
window = hann
noverlap = nperseg / 2
return_onesided = False
mode = psd
```

### 10.2 横轴 Time

横轴是当前选中窗口内部的时间，单位 ms。

注意：这里的时间是相对于当前 spectrogram 输入片段的时间，不一定是整段文件的绝对时间。

### 10.3 纵轴 Frequency

纵轴是实际频率，单位 MHz：

```text
实际频率 = 中心频率 + 基带频率
```

### 10.4 颜色

颜色表示该时间、该频率处的功率谱密度大小：

```text
10 × log10(PSD)
```

颜色越亮或越接近高值，说明该时刻该频率处能量越强。

### 10.5 最大峰值点

软件会在整张时频矩阵里寻找最大值：

```text
peak = max(spec)
```

并标出：

```text
Peak frequency
Peak time
Peak power
```

### 10.6 分析用途

时频图适合分析：

- 跳频信号；
- 扫频信号；
- 突发信号；
- 信号是否随时间漂移；
- 干扰是否只在某些时刻出现；
- 频谱是否稳定。

---

## 11. Histogram 幅度分布图说明

Histogram 页面有两个图：

1. I/Q 幅度分布；
2. `|IQ|` 幅度 dBFS 分布。

### 11.1 I/Q 幅度分布

对选中时间段中的所有 I 和 Q 值统计直方图。

横轴是归一化幅度：

```text
-1 ~ 1
```

纵轴是出现次数。

用途：

- 判断 I/Q 是否以 0 为中心；
- 判断是否存在 DC 偏置；
- 判断噪声分布形态；
- 判断是否有削顶。

如果 I/Q 分布明显偏向正值或负值，可能说明存在直流偏置。

### 11.2 |IQ| dBFS 分布

先计算：

```text
|IQ| = sqrt(I^2 + Q^2)
```

再换算为：

```text
magnitude_dBFS = 20 × log10(|IQ|)
```

横轴是相对于数字满量程的幅度，单位 dBFS。

用途：

- 看信号幅度主要集中在哪个范围；
- 判断峰值和平均幅度差异；
- 观察是否存在异常大幅度点；
- 观察动态范围。

---

## 12. 为什么很多量没有物理单位

当前软件读取的是数字 IQ 文件。文件中有 16 bit 数字采样值和参考电平信息，但没有完整给出以下标定关系：

- ADC 满量程对应多少伏特；
- 接收链路增益；
- 衰减器设置；
- 天线或前端增益；
- 阻抗；
- 仪器内部从数字幅度到 dBm 的精确换算方式。

因此软件当前显示的是归一化数字量：

```text
I, Q: normalized full-scale
|IQ|: normalized full-scale
Power: normalized power
PSD: relative dB
```

这些量适合做相对分析，比如比较不同文件、找频谱峰值、看信号形态、看频偏和变化趋势。

如果需要绝对功率，例如 dBm，需要进一步知道仪器标定关系和采集设置。

---

## 13. 常见分析思路

### 13.1 看有没有明显信号

先看：

```text
Spectrum
```

如果频谱中有明显高峰，说明该频率附近有较强成分。

### 13.2 看信号是不是随时间变化

看：

```text
Spectrogram
```

如果亮线随时间移动，可能是扫频或频率漂移。

如果亮块只在短时间出现，可能是突发信号。

### 13.3 看信号幅度是否稳定

看：

```text
Time -> |IQ|
```

如果包络平稳，说明幅度较稳定。

如果包络有周期性起伏，可能有调制、衰落或干扰。

### 13.4 看是否有 DC 偏置或 IQ 不平衡

看：

```text
Constellation
Histogram
Summary 中的 I/Q mean
```

如果星座图整体偏离原点，或者 I/Q 均值明显不为 0，可能有 DC 偏置。

如果星座点云明显椭圆，可能有 I/Q 不平衡。

### 13.5 看是否接近满量程

看：

```text
Summary -> Peak magnitude
Time -> I/Q
Histogram
```

如果峰值接近 `0 dBFS`，或 I/Q 经常贴近 `-1`、`+1`，可能存在削顶风险。

---

## 14. 当前软件的限制

1. `Summary` 当前基于抽样点统计，不一定是完整时间段全部点统计；
2. 频谱峰值是基于 Welch PSD 最大 bin，不一定等于真实信号中心频率；
3. 时频图峰值是整张 spectrogram 矩阵中的最大点；
4. 幅度是归一化数字幅度，不是绝对电压或 dBm；
5. 中心频率按文件名数字提取，如果文件名规则变化，需要修改解析规则；
6. 如果信号很窄或瞬态很短，普通抽样可能漏掉，需要使用更小时间窗口或增加专门的峰值保持分析。

---

## 15. 建议使用流程

推荐按下面流程分析：

1. 选择 `Data` 文件夹；
2. 选择 `Recording`；
3. 先设置：

```text
Start = 0
Duration = 0.05
Max points = 200000
Spectrogram pts = 1048576
```

4. 点击 `Generate and View`；
5. 先看 `Spectrum`，确认主要频率成分；
6. 再看 `Spectrogram`，确认信号是否随时间变化；
7. 看 `Time` 中的 `|IQ|`，确认幅度是否稳定；
8. 看 `Constellation` 和 `Histogram`，检查 IQ 分布、偏置和失真；
9. 如果发现感兴趣的时间段，再调整 `Start` 和 `Duration` 做局部细看。
