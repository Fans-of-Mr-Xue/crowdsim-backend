# 纪念塔事后完整测试：数据、执行、存储与分析

当前字段与能力范围统一见[执行与分析共同契约](post_execution_analysis_contract.md)（2026-10-30核对）；需同事补充的SUMO/TraCI、测量、证据及后端恢复工作见[对接清单](post_sumo_handoff.md)。下文历史验收仍保留原记录，不代表待扩展能力已经实现。

上传文件：[shanghai_memorial_post_dataset.json](examples/shanghai_memorial_post_dataset.json)。这是从前端 `Simulation_data_ShanghaiMemorialTower_detail.md` 逐行规范化的**合成参考数据**，不是历史监测或伤亡数据。文件可直接在事后需求界定中上传，无需补填不存在的密度、速度或压力。

## 1. 数据内容与边界

- 20个时点 × A/B/C三个区域，共60条；从2014-12-31 20:00到2015-01-01 01:30，Asia/Shanghai，19,800秒。
- 人数与区间进入/离开按 300/800 等比例缩放并取整，同一时刻合计峰值为 300 人；两套独立人口比例保留原值。取整后的收支残差如实记录。
- `observations` 使用 `timeSeconds/regionId/population/density/meanSpeed/pressureProxy/sourceTime/state`。三个未观测指标为显式 `null`。
- `metadata.sourceRecords` 保留源行号、进出流量、年龄/国籍边际分布、应急策略和人数收支残差。16处残差原样记录，未自动修平。
- `sourceSha256` 标识原始Markdown。上传原件存入GridFS；数据版本同时覆盖观测值和执行元数据，不能修改流量而沿用旧版本。

执行地图已获用户确认：A→M1西侧连接通道（`178411801#0`），B→M2南侧连接通道（`177931018#0`），C→M3（`monument_m3`）。**M3是西侧中部工程假设入口，不是真实北侧通道。**使用现有路网文件，不修改事中场景或菜单。

原文没有个人轨迹，工程重建采用：区间进入均匀分布、区间离开按FIFO分配停留时间、沿既有环道用共同随机数分散停留位置，由SUMO striping实际产生运动。年龄和国籍不推导联合分布或行为参数。参考人数只用于误差评估，不在每帧强制回填；入口原始存量与测试模型测量范围不完全相同，当前属于未做拟合校准的工程测试模型，不能将仿真指标直接解释为历史观测。

本机SUMO的普通person等待阶段会输出道路边偏移坐标（[官方MSStageWaiting实现](https://raw.githubusercontent.com/eclipse-sumo/sumo/main/src/microsim/transportables/MSStageWaiting.cpp)也采用此行为）。因此实际控制器在行走阶段接近分散停留点时设速度上限0；到释放时刻只移除未来等待阶段并恢复原速度。没有moveTo、坐标推进或瞬移；SUMO保留真实行走和拥堵交互。原始轨迹与真实hold/release事件一起保存，未到达目标点的人员不会被硬搬到停留点。

## 2. 三类设计如何变成 SUMO 输入

需求界定的每个组保存 `execution`，随后纳入方案快照：

```json
{
  "startSeconds": 13800,
  "compliance": 0.7,
  "delaySeconds": 0,
  "referenceDesignId": "F-00"
}
```

| 类型 | 问题与配置 | 比较与分析 |
|---|---|---|
| 观察 | 完整提前开放、引导的世界线；同一源流量、模型、路网和种子 | 与F-00配对；报告指标分布、有效重复数、均值、标准差、配对效应和Bootstrap区间 |
| 干预 | 固定其他条件，仅改变启动时点、遵从比例或响应延迟之一；不同水平新增不同组 | 明确参照组；后端拒绝多个独立因素同时变化。开放/引导时点同步平移视为同一个启动时点因素 |
| 机制 | 相对完整观察组，仅将遵从比例设0，或仅改变响应延迟 | 固定开放时间及其他控制，按相同种子比较完整/消融模块；不自动当作自然间接效应 |

启动时点从20:00计时，范围1—19799整数；遵从比例0—1；响应延迟0—19800整数。观察参照F-00，干预/机制通常参照对应的OBS设计组。执行矩阵将设计组ID明确映射为CF组ID，保留 `sourceGroupId`，不能遗漏快照中的组。

F-00工程设定：14700秒（00:05）开放C并启动引导，1500秒响应延迟，遵从比例0.4。这是可追溯的工程设定，不是原文测得的比例。建议模板中的13800、14100、14400秒等也是待用户核对的实验水平。

运行页读取真实 `/capabilities`，绑定 `memorial-post-test`、`memorial-post-striping`、模型版本、路网SHA派生的几何版本及 `post-controls/1.0`。后端将 `open_gate` 和 `guidance` 编译为行人路径、停留与释放计划，生成每次独立运行的 `demand.rou.xml`、`input.json` 和 `command.json`，直接启动SUMO CLI。轨迹来自SUMO FCD，离场信息来自tripinfo；没有Python移动人员、定时器完成或生成预设结果。

## 3. 在现有前端测试

1. 后端代码更新后重启 `python start_backend.py`，保持终端打开，以保留27018数据库与8800模型转发。
2. 新建事后对话，上传规范JSON，核对60条数据、区域、时间窗、合成性质和缺失指标，确认事实基线。
3. 参考目的：“在同一源流量及19,800秒内，检验提前开放和引导C测试通道对峰值密度及风险暴露的影响，识别启动时点的单因子效应，并消融引导响应模块。”完善范围、参照和用途后确认。
4. 在右侧依次设计观察、干预和机制组，核对明确秒数、遵从比例、延迟及参照。修改观察参照组会使依赖它的确认失效。
5. 确认完整方案，进入既有方案快照页核对工程参数。再到实验运行页载入执行矩阵，逐组核对并勾选确认，保存工程执行方案，然后点击“启动真实SUMO批次”。
6. 实际完成次数更新进度。查看单次运行可加载MongoDB中的轨迹片段重演、指标、人数校准误差和右删失状态。
7. 沿既有观察→干预→机制→报告页面读取同一对话和批次的数据，可下载分析JSON。默认不再用旧演示数值填充真实结果；旧界面代码保留在显式 `preview=1` 演示入口。

每类6个重复的默认示例可完成工程功能测试，但不足以支持稳定的因果发现。PC要求至少20个独立F-00种子和完整、非恒定特征；可在确认方案时增加重复次数。不能把配对反事实世界线当作额外独立样本。

执行矩阵包含已确认设计的`reviewPlanVersion` SHA-256。后端拒绝过期快照，并把当时的目的、三类组设计和工程参数完整冻结到任务的`reviewSnapshot`；后续修改同一对话的方案不会改写已有批次。页面会区分当前方案与先前快照的批次。

## 4. 接口链路

统一前缀为 `http://127.0.0.1:8765/api/v1/post`。POST请求头包含 `Content-Type: application/json`、`Idempotency-Key`、`X-CrowdSim-User` 和 `X-CrowdSim-Workspace`。同一对话始终使用相同工作区，前端为`post-`加清理后的对话ID。

| 顺序 | 请求 | 结果 |
|---|---|---|
| 1 | `POST /datasets` | 登记规范JSON，返回真实datasetId及版本；前端自动附rawFile入GridFS |
| 2 | `POST /review-plans` | 保存用户已确认的三类设计和快照 |
| 3 | `GET /review-plans/{planId}/execution` | 由已确认的execution生成明确数值矩阵；旧草案返回可编辑建议 |
| 4 | `POST /review-plans/{planId}/execution`，body为`{"confirmed":true,"execution":{...}}` | 校验数据版本、设计组引用、参照和单因子/消融隔离，创建真实草稿批次 |
| 5 | `POST /experiments/{experimentId}/start` | 进入队列，每次独立进程执行，相同种子配对 |
| 6 | `GET /experiments/{id}`、`/events?after=0`、`/runs` | 状态、游标事件和实际运行列表 |
| 7 | `GET /runs/{runId}`、`/trajectory?after=-1&limit=10`、`/archive` | 指标、轨迹分块和真实归档集合名 |
| 8 | `GET /results/{id}/metrics`、`comparison`、`observation`、`intervention`、`mechanism`、`causal-graph` | 从已封存结果读取算法分析；未完成返回409 |

单次仿真仍可通过 `POST /simulations`、`POST /simulations/{id}/start` 执行同一F-00模型，使用`GET /simulations/{id}/runs`查看运行；`/results/{simulationId}/metrics`读取结果。

独立接口请求形状示例：[shanghai_memorial_post_execution.json](examples/shanghai_memorial_post_execution.json)。其中`UPLOAD_DATASET_ID`需替换为实际上传返回ID；前端链路自动处理，不需要用户编辑上传文件。

## 5. MongoDB存储

使用现有 `.env` 中的数据库（默认crowdsim），保留已有集合和数据。Mongo没有真正的子表，按用户选择采用总索引加每次运行独立集合：

```text
post
  runId / runName / ownerId / workspaceId / experimentType / status / collections

post_<UTC时间戳>_<实验类型>_<短ID>             明细与输入配置
post_trajectory_<UTC时间戳>_<实验类型>_<短ID>  轨迹分块
post_result_<UTC时间戳>_<实验类型>_<短ID>      指标和分析结果
```

时间戳形状为`20261023T103012123456Z`，包含微秒并追加短ID防冲突。实验类型为simulation、experiment、baseline、observation、intervention、mechanism。批次与每个“组×种子”独立运行均有索引，以同一runId关联各自三类集合，批次ID与设计组/种子关联到单次运行。轨迹按帧和人数分块，避免单文档超过Mongo大小上限；API按游标读取，不一次加载全部轨迹。

原有`postanalysis_*`集合继续提供数据集、方案、任务、运行、结果与幂等查询。动态集合名仅由后端生成；客户端不能指定集合名，所有读取按用户/工作区校验。原始上传文件在`postanalysis_files` GridFS bucket。

完整编译清单（含个人初始/停留位置与释放计划）及`demand.rou.xml`也进入同一GridFS bucket，文件ID、SHA-256和实际启动命令写入明细集合。轨迹分块使用标记为`zlib-json-utf8/1`的无损压缩，每块解压后最多8 MiB；API校验并还原普通JSON帧，旧未压缩记录仍可读。任务列表不重复返回全部轨迹或控制事件，单次详情保留真实hold/release事件。

当前工程执行方法明确限定为观察的配对蒙特卡洛、干预的OFAT和机制消融。已有设计中选择了RCT、析因、中介或级联方法时，后端会要求显式调整或接入相应算法，不能把这些名字静默当作已实现方法。

## 6. 测量与算法

- 仿真步长0.5秒，FCD采样5秒。固定5米网格与SUMO可行人车道宽度、节点几何求交得到有效面积；保存真实区域网格映射和几何版本。
- 密度为网格人数/有效面积；速度为人员-时间加权均值，包含零速人员。压力为密度×网格速度向量方差，单位`人/m²·(m/s)²`，不是牛顿接触力。
- 高风险阈值密度≥3.5，保存去重人数和人秒暴露。拥堵为密度≥3.5且速度≤0.5持续至少10秒，跨网格累计单位`cell*s`。
- 全部到达时间仅在本次窗口内所有计划人员确实到达时给值，否则null并标记右删失；不将未完成写成窗口长度或0。
- 配对差为实验组减参照组，只取同种子交集；至少2个配对才给2000次配对Bootstrap区间，少于20个配对标记描述性估计。
- PC-Stable骨架使用Fisher-Z条件独立检验，α=0.05，支持2—8个特征，至少20个完整独立基线种子。仅标记未屏蔽碰撞结构，其余边保留无向，不伪造方向。假设、样本不足和退化特征状态随结果保存。
- 机制输出为预定义模块消融效应，不自动等价于中介因果效应；SUMO striping不提供跌倒、伤亡或机械接触压力模型。

## 7. 可复现检查

不执行npm构建。离线契约/算法测试与真实执行验收分开：

```cmd
python -m unittest discover -s tests -p "test_post*.py"
python scripts/run_post_acceptance.py
python scripts/run_post_acceptance.py --profile memorial --repeats 1
python scripts/validate_post_sumo.py
```

第一条检查规范、隔离、采样和算法。第二条执行18人/600秒的8次真实SUMO小样本，验证入库和分析；报告在`runs/postanalysis/acceptance-smoke.json`。第三条使用完整原始规范数据和19,800秒窗口，对默认五个CF组加F-00各运行一次，报告在`runs/postanalysis/acceptance-memorial.json`；这是完整时窗的工程验收，单种子不当作统计结论。真实入库验收需保持Mongo转发可用，记录保留在独立测试工作区，不删除既有数据。第四条在Mongo暂不可用时显式验证完整SUMO与分析链路，保存至`runs/postanalysis/offline-validation/report.json`，标记`local-validation-only`，不宣称验证数据库；生产服务不会自动回退到本地文件。

完整时窗验收的等待上限按每次计划运行15分钟累计，包含远端轨迹归档耗时；可通过`--timeout-seconds`指定整个验收的等待上限。小样本默认上限仍为180秒。

## 8. 本机真实入库验收（2026-10-23）

已完成8次小样本验收，以及完整纪念塔时窗的6次真实SUMO运行（F-00、观察1组、干预3水平、机制1组）。完整批次为`exp_4dedd18c8df24196b8f2415e80a27e8f`，位于独立工作区`post-acceptance-2390068f6025`；测试记录保留，既有数据未删除。

完整批次的7条post索引均为completed，关联各自明细、轨迹与结果集合。已核对4,752个无损压缩轨迹分块的连续序号、每次轨迹首尾时间0和19,795秒、12份编译输入文件的SHA-256，以及GridFS中原始上传文件与规范JSON逐字节一致。6次指标与此前独立离线验证一致。

两种验收配置共22个HTTP查询通过，覆盖任务、运行列表、指标、比较、三类分析、因果图状态、归档和轨迹。另已验证真实HTTP上传与方案冻结、显式参数确认、幂等重放、过期方案拒绝和工作区隔离；运行中的三类分析正确返回409/RESULT_NOT_READY。

本机报告：`runs/postanalysis/acceptance-smoke.json`、`acceptance-memorial.json`、`acceptance-storage-http.json`、`acceptance-http.json`和`acceptance-running-guard.json`。这些报告和运行文件保留在本地，不加入仓库。

当前模型尚未拟合校准，完整运行存在持续拥堵，6次疏散时间均为null并标记右删失。此次每组只有1个种子，PC返回insufficient_data；这是工程链路验收结果，不能作为历史重建或统计因果结论。前端浏览器交互与用户手动构建仍待验收。

## 9. 需求界定工作台与可编辑执行参数

完整纪念塔基线接入后，在右侧“实验组设计”直接编辑每组重复次数、共同种子起点、七个输出指标，以及2—8个PC特征的编号、指标、区域、窗口和汇总方式。当前执行器仅串行运行；场景、路网与数据版本由后端绑定。每组可编辑启动秒数、遵从比例、响应延迟、参照观察组和关联分析特征。观察参照F-00；OFAT只能改变一个控制因素；机制组以完整观察组为参照，只改变遵从比例或延迟中的一项。

已确认的方案保存`executionSettings`（seedStart、parallelism、metricIds、pcFeatures）及各组`execution`、`manipulatedFeatureId`；重复数仍使用`counterfactualSettings.repeatCount`。这些字段纳入确认签名和只读方案快照。修改公共运行/分析配置后，三类设计需要重新确认；删除参照组或分析特征时显示缺口，不自动切换引用。输入不完整时保留原输入供修正，不静默填成默认值。

新方案在运行页点击“载入已确认执行方案”即可获得与工作台一致的执行矩阵，无需再编辑JSON。后端冻结方案快照，并拒绝与之不一致的执行参数；如需修改，返回需求界定编辑并重新确认。没有这些新字段的已有方案仍沿用原模板流程。

案例演示使用与规范上传JSON一致的60条记录、19,800秒及300人合计峰值，按10步展示：接入数据、确认基线、说明目的、确认目的、变量及运行参数、观察、干预、机制、分析路径、方案快照。案例包含观察1组、干预3水平、机制1组，每组3次，共18次计划运行；预设案例只在本地展示，不会上传、提交或启动真实仿真。重新演示会创建独立对话并保留旧对话。

本机工作台对接验收记录为`runs/postanalysis/workbench-contract-check.json`：真实MongoDB保存方案后，种子7654、重复2次、区域A的自定义PC窗口、已选指标和5个执行组均保持一致；执行参数篡改、过期快照被拒绝，原批次快照保持不变。此验收仅创建运行草稿，不代表新执行了一批SUMO。后端需重启以载入新模板及校验代码。
