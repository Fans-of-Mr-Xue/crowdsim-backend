# 按需开启开销监测

正常启动默认不采集、不上报、不保存开销监测数据，也不创建系统采样线程或安装 TraCI 监测钩子。
轨迹、需求、决策、观测指标、SUMO 日志等业务记录照常保存。旧的 `CROWDSIM_PERF`、
`CROWDSIM_SYSTEM_PERF` 环境变量不再控制监测，环境中遗留 `=1` 也不会开启它。

## 启动方式

```bash
# 正常使用：关闭监测
./run.sh

# 前后端联合测试：启动后按原流程在前端配置、开始仿真
./run.sh --monitor-performance

# 同一诊断配置但关闭系统采样，便于评估采样本身的影响
./run.sh --monitor-performance --performance-no-system

# 延长后端汇总和系统采样间隔（现实秒，最小 5）
./run.sh --monitor-performance --performance-interval 10

# 也可只启动仿真服务；不启动前端时不会有前端性能数据
python crowdsim_overlay_server.py --scenario hotspot --monitor-performance

# 原有离线实验脚本也支持开关
python scripts/run_experiment.py --steps 120 --monitor-performance
python scripts/run_hotspot_experiment.py --monitor-performance
```

单独设置 `--performance-interval`（非默认值）或 `--performance-no-system` 而不添加
`--monitor-performance` 会报参数错误。监测选项属于本次服务启动配置，重置仿真、切换需求和
创建新 run 均沿用它；不能靠前端命令或环境变量临时开启。统一启动器复用已有 SUMO 服务前会
核对监测配置，不一致时提示先关闭并重启，避免开关未生效。更新前的旧服务也需要重启一次。

现有离线脚本保持各自的场景、需求生成方式和运行行为，不因启用监测而更改为前端场景。
做开关对照或前后端对照时，须另行确保地点、需求、人群初始化方式、种子、人数、步长、
倍率和比较阶段一致。离线循环不包含 WebSocket、前端渲染或交互播放等待。

## 记录位置与含义

每次测试保存到 `runs/<run_id>/`，manifest 中记录开关、间隔和版本。

| 文件 | 内容 |
| --- | --- |
| `performance.backend.jsonl` | 每个窗口的实际倍率、各阶段墙钟/当前线程 CPU 时间、TraCI 请求次数/耗时、邻居计算量、消息大小、日志增长和决策日志统计 |
| `performance.system.jsonl` | macOS 整机 CPU/GPU、相关进程 CPU/RSS、热压力、内存压缩与换页速率、后端/SUMO 磁盘 I/O、采样自身耗时 |
| `performance.frontend.jsonl` | 连接前端时已有的 JSON 处理、Canvas/地图更新、帧间隔、长任务等聚合数据 |
| `performance.power.txt`、`performance.power.metadata.json` | 独立管理员采集脚本的原始频率/功耗/热压力输出及采集时间、run ID 和退出状态；不会自动生成 |

后端新增细分项包括 `sumo_advance`（仅推进）、`sumo_snapshot`（读取快照）、
`traci_roundtrip`（本连接每次 `_sendCmd` 请求，count 即请求次数）、
`neighbour_candidate_comparisons`（同道路上的候选距离比较次数）、`neighbours_per_person`、
`max_people_on_edge`，以及轨迹、指标、观测、决策写入和决策缓冲 flush 的耗时。
TraCI 私有钩子若不兼容，adapter 的 `command_monitor_status` 会标记 unavailable；
不增加额外查询，也不改变请求顺序、返回值或异常。

`stages` 中时长单位为 ms，`mean/max/total` 为墙钟时间，`cpu_mean/cpu_max/cpu_total`
为当前线程 CPU 时间；其他项由 `unit` 指明 bytes、people、comparisons、edges 等。
部分计时范围嵌套（例如推进/快照包含 TraCI，写入包含 flush），不可直接全部相加。
异步等待阶段的线程 CPU 时间也可能包含该线程执行的其他任务，不能视为独占 CPU。
`python_process_cpu_seconds` 包含诊断线程，不含 SUMO 子进程；子进程使用系统日志判断。

`actual_speed_factor` = 窗口内推进的仿真秒数 / RUNNING 循环的现实秒数；交互循环包含计算、
发送和播放等待，离线循环包含 tick 执行。READY/PAUSED 不计入这个运行倍率。
按现实时间汇总而非保存逐人逐请求计时历史，关闭/结束时保存剩余窗口。

系统采样独立线程按低频率执行，不调用 TraCI。内存数据使用 Mach/sysctl 原生接口，
进程 I/O 使用 libproc；不为这些新项额外启动轮询命令。换页和压缩记录累计页数与区间速率，
首个样本、计数回退、进程更换或读取失败后重新建立基线时不会伪装为零。
swap 页数乘页大小是逻辑换页字节量，不等于磁盘实际传输字节；磁盘 I/O 另看 libproc。
日志 `growth_bytes` 是可见文件大小增长，可能受缓冲影响，不等于物理磁盘 I/O。

采样自身有 `collection_wall_ms`、`collection_thread_cpu_ms` 和逐传感器耗时；前一个诊断
文件写入的耗时在下一条 backend 记录的 `diagnostic_previous_write` 中保存。
这些数据和 `--performance-no-system` 可用于判断监测自身的影响，不承诺测量开销为零。

浏览器组覆盖整机相关应用，整机 GPU 和 WindowServer 也受其他窗口影响，不能直接当作本页面
独占开销。热压力和电池温度不是 CPU/GPU 芯片温度。无权限、不支持或缺失的数据保存为
unavailable/null，保留原因；不会补造温度、频率或功耗。系统采样当前仅支持 macOS，其他
平台记录不可用说明后退出；后端阶段计时仍可使用。诊断文件写入失败不终止业务仿真。

## 可选频率与功耗采集

在仿真已创建 run、manifest 标记监测开启后，另开终端：

```bash
sudo /opt/miniconda3/envs/sumo/bin/python scripts/collect_power_metrics.py \
  --run-dir runs/<run_id> --duration 600 --interval 5
```

只有这个独立脚本使用管理员权限，后端按普通用户运行。脚本核对 run 的开关，默认最多采集
600 现实秒，参数最大 3600 秒；输出文件使用独占创建，不覆盖已有采集。不自动提权，不修改
系统设置。保留 powermetrics 原始平台输出：支持的 CPU/GPU 频率、功耗和热压力字段以本机
系统实际提供为准，未提供的芯片温度仍然不可用。额外采集也有开销，应单独做对照。

本次验证使用模拟快照、连接、计数器和子进程，检查参数、默认禁用、线程关闭、差分基线、
日志一致性及前端探针；尚未执行新的 800 人实测或管理员功耗采集。
