# 背景行人 POI 重规划补强

本次保留背景行人自主 POI 决策，不关闭功能、不屏蔽 SUMO 警告。规则或 LLM 仍从后端提供的有限候选中选择动作和目标；Skill 不负责道路计算，也不调用 TraCI。热点需求文件中标记 `crowdsim.itinerary_locked=true` 的访客保持原有到访—停留—离开行程。

## 1. 候选与可达性

- 路网加载行人专用连接，构建过滤行人通行权限的拓扑索引。每个 POI 目标第一次使用时反向搜索一次，之后做集合查询。不能经过禁止行人的道路连接两个区域。
- 静态索引只是预筛；最终步行路线仍由 SUMO `findIntermodalRoute` 验证，使用空 `modes`（纯步行）。不采用车辆最短路代替行人寻路。
- 路线几何按起始道路、目标道路、到达位置缓存，最多 4096 项。每名行人的当前位置独立计算剩余距离估计；缓存不共享旧的行人位置或整份候选。估计成本是路段距离 / 1.35 m/s，用于排序，不是实测到达时间。
- SUMO 返回空路线时负缓存 30 秒；通信或查询异常不当作永久不连通。权限、路网及 POI 配置发生变化后需重置/重启运行，不能继续使用旧索引。
- 仅在行人到达决策周期时刷新候选（默认每 2 秒），不是每个 0.5 秒运动帧重新寻路。内部路口边、计划停留阶段、重规划冷却期间暂缓生成候选，保留原行程；普通步行阶段仍可由规则/LLM 重新决策。
- activity 候选必须同时具有“当前位置→活动点”和“活动点→后续目标”的有效步行路线。按活动计划顺序尝试后续活动和已知出口，不循环回选之前的活动。首选 POI 不可达或关闭时回退到其余有效候选；全部不可达时继续原 SUMO 行程。

## 2. 到达位置与行程执行

- 启动时校验 POI 道路、行人权限、位置、开放时间和停留区间。`position: "end"` 映射到行人道路长度内侧 1 厘米，避免端点精度问题；越界、负数、NaN 和无穷位置在请求 SUMO 前拒绝。
- `RouteCandidate` 和 `BehaviorPlan` 带有 `arrival_position`、`next_arrival_position`、`next_target_id`。Skill 只复制这些可信字段，模型仍只输出 `action/target_id/reason/confidence`，动作集合不变。
- 执行前验证两段路线、两处到达位置及停留时间。新路线不继承旧目的地的 `arrivalPos`，没有离开路线时不先修改当前阶段。
- 同一时间边界保存原有剩余阶段、移除未来阶段、保留当前位置的临时等待锚点、安装新步行阶段，再追加活动停留和后续步行。中间不调用 `simulationStep`。重复改目标会替换未来阶段，不累计重复等待或步行。
- 临时锚点用于规避 SUMO 1.27 阶段转换继承旧目的地出发位置的问题，实测版本为 1.27.0。旧 SUMO 若不支持保留该锚点会明确报错，不能忽略后继续推进。参考 [SUMO 1.27 Person 阶段转换源码](https://github.com/eclipse-sumo/sumo/blob/v1_27_0/src/libsumo/Person.cpp) 和 [阶段移除源码](https://github.com/eclipse-sumo/sumo/blob/v1_27_0/src/microsim/transportables/MSTransportable.cpp)。
- 中途写入失败时尝试恢复原行程，结果为 `rejected` 并注明已恢复；恢复也失败或行程已修改但后续操作失败时结果为 `partial_failure`，运行进入 `ERROR`，不伪装成继续原路线成功。
- SUMO 完成活动步行和等待阶段、进入最后一段后续步行时才推进活动计划。中间的 `continue` 不清除活动进度；已完成的活动不会因为计划为空被重新初始化。

## 3. 冷却与诊断

连续失败按仿真时间冷却 10、20、40、60 秒（上限 60 秒）；成功执行重规划后清除冷却。冷却只限制重规划，不影响继续、减速、等待及正常 SUMO 运动。已离开的行人会清理执行器缓存。

`get_status` 的 `routing`、每帧 `metrics.routing`、运行目录 `summary.json` 提供聚合信息：

- `sumo_route_queries`：实际精确寻路请求次数。
- `route_cache_hits` / `negative_cache_hits`：正/负缓存命中次数。
- `topology_rejections`：预筛拒绝次数，不代表已向 SUMO 请求或行人被删除。
- `unreachable_pairs`：当前缓存中的不同不可达道路对数量，缓存有上限；不是警告行数。
- `sumo_unreachable` / `query_errors`：SUMO 无路线结果与查询异常次数。
- `candidate_refresh_deferred` / `goal_fallbacks`：候选暂缓与首选目标回退次数。
- `execution.route_plans_applied`：实际执行成功的重规划次数。
- `execution.route_failures`：启动冷却的失败轮次，包括无可用候选，不等于 TraCI 执行错误次数。
- `execution.rejected` / `itinerary_rollbacks` / `partial_failure`：执行拒绝、恢复及严重部分失败次数。
- `execution.cooldown_people`、`last_route_error`：当前冷却人数与最近一次失败原因。

不通过持续打印相同警告做诊断；真实 SUMO 警告仍保存在 `sumo.log`。决策仍逐次完整记录，但新运行的存储现已单独实现 [无损拆表](decision_log_format.md)，不改变 POI 行为，也不清理历史文件。

## 测试与联调

在项目根目录激活 `sumo` 环境后运行（不调用真实 DeepSeek API）：

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s pedestrian_decision_skill/tests -v
python scripts/run_experiment.py --mode rule --count 120 --steps 2400
```

第三条是抽样 120 人的热点场景冒烟测试，不等于完整 1930 人热点效果验收。查看命令返回的运行目录内 `sumo.log` 和 `summary.json`，重点检查不连通/位置警告、执行拒绝/部分失败、人口守恒和路线查询数量。此实验按步数停止，输出 `CLOSED` 不表示运行错误。

前端联调无需修改页面：重启后端服务，再在前端重置并重新运行，避免旧 SUMO 进程、旧路网索引或旧行程继续生效。此次未调用付费 API；若要验证真实 LLM 路径，再使用已配置的 `--mode llm` 启动参数。
