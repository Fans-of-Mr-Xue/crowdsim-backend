# 第二类：用户人流配置驱动 SUMO

第二类使用冻结方案中的 `class2.flow_design` 初始化行人。纪念塔仍为唯一研究场景；原固定聚集分组和随机离场布局已替换。第一类的生成器、活动时序和入口保持原行为。

## 执行输入

继续使用 `POST /api/v1/during/class2/requirements`，请求包含：

- `source`：原会话与冻结快照 ID。
- `problemType: 2`、`projectTitle`、`phenomenon`（研究目的）、`requestedLocation`、`durationSeconds`、可选 `seed`。
- `count`、`populationDistributions`、`metricIds`：冻结快照的总人数、公共人口构成和观测指标。
- `flowDesign`：地图保存的人流结构，版本为 1；每股包含 `id/name/count/density/speed/origin/destination`。
- `mapFingerprint`：随前端地图发布的 `geometrySha256`。

每股人数为正整数，最多20股，总人数不超过10000且必须与 `count` 相等。密度、速度、路网版本、点位及道路容量均由后端独立校验；密度上限20人/平方米和速度上限2.5米/秒只是输入范围，无法容纳的布局会明确拒绝。缺少人流配置不会回退到旧演示。

接口只准备不可变执行记录，不启动或替换 SUMO。原 HTTP 回执和 WebSocket 生命周期继续复用，`class2_demo` 内部绑定承载新的人流执行记录，其场景 ID 为 `memorial-tower-class2-flows-v1`。绑定包含配置、地图、布局和资源指纹。重连和重置仍核对原会话、快照和执行身份。

## 地图与布局

`frontend/scripts/export_memorial_flow_network.py` 同时导出前端地图和后端 `config/during_presets/memorial_flow_network_v1.json`。数据保留裁剪路段对应的原始车道、几何起止位置、SUMO道路长度、局部坐标原点和累积距离。前后端使用同一组累积距离，避免跨语言浮点误差改变人数分配。

后端布局算法与地图预览一致：按 `人数/密度` 分配起点附近可步行面积，按路段面积分配人数并布置横纵向位置；目的地使用相同密度规划分散停留点。同一阶段的多股布局统一占用道路区间。后端自行生成行人坐标，不接受客户端发送的逐人坐标列表。

裁剪路段位置会映射回原始SUMO道路位置，并校正几何长度与SUMO长度差异。实际步行路线复用 `PositionAwarePedestrianRouter`；不可达路线在启动SUMO前拒绝。

## 运动与停留

所有行人 `depart=0`。插入准备步使用数值上近于静止的速度上限，READY恢复用户配置的自由步行速度但不推进仿真。人口画像处理仅在本第二类运行内保留这份速度；第一类仍使用原画像速度。

开始后各股人流前往已分配的目的地。有效步行速度受拥堵、避让及现有控制约束。第二类专用控制器在目的地前减速，并以小于0.06米的纵向到达容差保持停留。到达后继续保留为零速walking行人，参与行人相互作用和密度/流速观测，不通过删除行人实现到达。技术保留行程和安全停留阶段防止SUMO在边界上自动结束人员行程，它们不会作为用户的新离场计划执行。

`metrics.class2_flows` 提供每股人数、配置速度、行走和到达停留人数。`population.arrived`仍保留原来的SUMO行程结束语义，不用来表示已到达并停留。达到设定时长后保留最后现场。初始化真实引擎时刻通常仍为0.5秒，前端沿用只在未开始时显示为0的规则。

## 验证

不需要浏览器或完整服务即可核对真实SUMO生成结果：

```bash
# 前端目录，导出合成快照及真实地图预览坐标
node scripts/export_class2_sumo_cases.mjs /tmp/crowdsim-class2-flow-cases.json

# 后端目录，在sumo环境中运行独立TraCI核对
python scripts/check_class2_flow_sumo.py \
  --cases /tmp/crowdsim-class2-flow-cases.json \
  --output /tmp/crowdsim-class2-flow-results.json
```

检查100、300、600、700、1000人的三股人流，核对总数、分股人数、出生道路和纵向位置、二维位置、自由速度、目标道路与位置、布局面积及人数守恒；另有短时到达停留和起终点重合边界检查。所有数据均为合成方案，运行使用临时目录和独立TraCI端口，不重启现有服务。验证摘要见 [结果记录](validation/during_class2_flows.json)。

## 后续优化：首次路径计算（2026-11-13，待实施）

新第二类背景行人首次初始化仍需计算随机起点、目的地及路径。当前已消除同配置在校验、生成中的重复计算，但冷启动和后端重启后仍有首次等待；测试事中18（313名背景行人、20名活动行人）单独测量初次路径计算约8.5秒。首次路径计算的进一步优化仍留待后续；下面的运行阶段优化不修改已有初始布局及其快照指纹。

## 待处理：预设中的孤立短步道（2026-11-13）

研究范围内的道路 `178564467`（13.65米）与主步行路网不连通，其所属连通部分另有两段范围外步道。原始 SUMO 路网在后端初始提交 `58ccbf8`（2026-08-24，Git 作者 moonst）中已有该道路；第二类专用路网在前端 `6f59ab2`、后端 `03b1f65`（2026-11-05，Git 作者均为 Glaciar）首次导出时将其纳入。导出脚本按研究边界和步行权限筛选，没有排除孤立部分；后端路网预设自首次提交以来未改变。用户要求暂记问题，后续再决定是否从预设中排除或核实并修复连接关系；当前不调整道路、边界或布局指纹。连通示意图见 `outputs/road_network/class2_connectivity.png`。

## 背景行人运行阶段优化（2026-11-13）

新第二类运行时使用现有位置寻路器的真实步行连接图划分连通区域，只在当前行人可达的研究路段内按面积随机选择目的地。在READY之前准备研究路段端点的路径树，并保留足够缓存，运行过程中复用；没有修改第一类、旧第二类或SUMO碰撞模型。每个背景行人仍保留身份，到达后停留5个仿真秒再出发。

测试事中18的333人配置在无界面SUMO中运行至100秒，完成202次背景行人重新出发，人数守恒、身份及5秒停留检查通过。相同前20仿真秒的后台耗时由15.69秒降至4.42秒；优化前20秒内最慢一步为3.49秒，优化后100秒内最慢一步为0.18秒。初始化总耗时由约5.88秒变为8.37秒，换取运行中不再反复搜索整张路网。该测量包含更新帧生成及JSON序列化，不包含浏览器绘图；记录中有2条SUMO行人碰撞警告，未出现人数丢失。

可复现实测：

```bash
python scripts/check_class2_background_performance.py   --requirement runs/requirements/req-b192c617ee6272173fcf62dbef592296.json   --seconds 100 --output /tmp/class2-background-performance.json
```

详见[后台测量结果](validation/class2_background_performance.json)。
