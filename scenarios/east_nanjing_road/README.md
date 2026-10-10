# 南京东路外滩路口 SUMO 行人场景

依据已确认的“南侧边界连接示意图”建立独立路网。图中的道路与节点编号直接作为 SUMO edge/junction ID：31 条道路、24 个节点、23 个真实 walking area。J20 是只有一条接入道路的端点，没有交汇 walking area。

## 路网与宽度

- R01 接 J26；R03 接 J27；中部 R02 经 J08—R07—J07 接入。三处入口独立。
- R13、R18 接 R25；R29、R30 连接 J11/J15 与 R26 的 J28/J29。
- R25c 截止于 J30，R26c 截止于 J31；R31 沿原预设区域南侧边界连接两点。删除了南侧尾段及原 J24。
- 道路采用一条可供行人双向通行的 lane。保留源道路第一条允许行人的 lane 的宽度，新增 R29/R30/R31 采用相邻人行道路相同的 2 米。R05/R06/R14/R15/R24 为 3.2 米，其余为 2 米。这里是行人场景，不包含机动车交通及信号相位。
- walking area 至少 4 米；四向 J11/J15 为 5 米；J01/J03/J19 保留原有 6.4 米宽度。具体值见 `junction_widths.csv`。

宽度指 SUMO striping 模型的 lane `width`。路口 shape/内部连接由 netconvert 生成，生成脚本在每次构建后重新执行宽度规则，防止重建丢失拓宽。与[既有路口修正记录](../../docs/2026-09-23_人民英雄纪念塔场景与前端接管修改记录.md)采用相同验证口径。

保留 Bund 的米制投影与坐标偏移，预设区域保持原有 WGS84 坐标；未修改 `shanghai_bund/bund.net.xml`、纪念塔需求或前端预设。`road_network.json` 是此场景的 WGS84 道路面导出文件，前端另存为 `static/crowd_sim/east_nanjing_road_network.json`，根据已提交需求切换。

## 运行与重建

在后端仓库根目录执行，使用已安装 SUMO、sumolib、traci 的 Python 环境：

```bash
sumo-gui -c scenarios/east_nanjing_road/east_nanjing.sumocfg
python scripts/build_east_nanjing_network.py
python scripts/validate_east_nanjing_network.py
python -m unittest tests.test_east_nanjing_network -v
python scripts/plot_east_nanjing_network.py
python scripts/export_frontend_road_network.py --network scenarios/east_nanjing_road/east_nanjing.net.xml --output scenarios/east_nanjing_road/road_network.json
```

`east_nanjing.sumocfg` 直接运行时引用 68 人的通行演示需求 `demo.rou.xml`，时间上限 1800 秒。
后端服务运行时会用本轮生成需求替换该演示文件：按已提交人数和画像生成行人，全部在初始化
时进入场景，起点按道路长度分配，路径根据当前路网的实际行人连接求解。
`python crowdsim_overlay_server.py` 启动后，前端提交 `east-nanjing-road` 需求即可自动使用该场景；
也支持显式 `--scenario east-nanjing-road`。服务采用 `generated_hotspot` 模式，加载本目录的
`crowd_hotspots.json`，热点为陈毅广场 `chen_yi_square`；需求中的事件类别仍为元数据。

## 陈毅广场聚集配置

- 聚集与测量道路：R25b、R29、R26b、R30。目标位置按可用步行面积分层分配，避开道路两端 2 米。
- 进入通道：R13、R18、R25a、R26a、R25c、R26c；南侧两个通道以道路 to 端为区域外侧。
- 初始刷新：全部 31 条普通道路按长度分配，全部 depart=0；人数和画像使用已提交需求，额外背景行人为 0。
- 到达、停留与离场：沿实际路网到达分配位置后原地等待；活动窗口 600～800 秒；各访客在 800～890 秒间按分配时刻释放。离场目的地在 R01/R02/R03/R22/R31 中均匀分配，继续现有第二段 walk，无需原路返回。刷新、绝对释放时间、动态择路和阶段判断参数与纪念塔一致。
- 聚集范围只包含四条周边道路；本次未增加广场内部步行面，未修改路网几何、预设边界或路口宽度。
- 初始化消息明确输出当前热点、模式和坐标；前端事件定位陈毅广场，同时保留需求区域及路网版本校验。

本轮仅进行配置校验、离线路线生成、适配器替身单元测试和前端构建，未启动 SUMO 或
WebSocket/TraCI，也未验证真实聚集容量。下面的历史通行结果不代表当前热点聚集负载验证。

## 验证结果（2026-10-09）

使用本机 SUMO 1.27.0、固定种子 20260908：

- 6 项自动测试通过，检查已确认图的拓扑、截断位置、投影、宽度、全部道路间寻路、重复生成以及源文件不变。
- 23/23 路口通过独立压力测试，每个有序方向组合 4 人，共 440 人；全部到达，最大连续停步 13.6 秒，低于既定 15 秒检查门槛。
- 全路网运行 930 组有序道路对、62 条道路双向测试及 6 组强制连接测试，共 998 人全部到达；1400 仿真秒结束，最大连续停步 8 秒。
- 未触发自动解堵。普通、crossing、narrow jamtime 均为 3601 秒；条带 0.55 米、对向预留 0.5；严格检查路线，关闭车辆 teleport。
- TraCI 地理坐标转换与 ResearchNetwork 投影一致。道路面导出没有无效/退化 walking-area 多边形。

完整报告为 `validation_report.json`，与实际网络的 SHA256 绑定。这些结果验证当前固定负载下的几何通行，不表示任意高密度都不会发生容量拥堵。

本机 macOS SUMO bundle 的 PROJ 数据库位于嵌套目录；验证脚本在未设置 `PROJ_DATA` / `PROJ_LIB` 时查找 bundle 自带数据库，仅影响本次脚本进程。其他环境通常由 SUMO 安装/启动器设置。
