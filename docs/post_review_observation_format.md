# 事后基线观测格式

事后示例和事中导入使用相同的规范字段。原始事中文件保留在 GridFS；事后仅筛选人数、全局密度/速度、局部密度/速度和边界密度差，不导入行为或心理指标。

| 规范字段 | 事中来源 | 口径 / 单位 |
| --- | --- | --- |
| `timeSeconds` | `time_seconds` | 仿真相对秒，保留小数，不移动起点或四舍五入 |
| `sourceSnapshotId` | `snapshot_id` | 原始采样标识；合成输入没有原始快照时不填写 |
| `population` | 全局 `person_count` | 当前观测范围人数；不用 `network_person_count` 替代 |
| `density` | 全局 `density_person_per_m2` | 人/m² |
| `meanSpeed` | 全局 `avg_speed_mps` | m/s；不用移动人群平均速度替代 |
| `localDensity` | 网格 `density_person_per_m2` | 同一快照有效网格的最大值，人/m² |
| `localMeanSpeed` | 网格 `avg_speed_mps` | 同一快照有效网格平均速度的最小值，m/s；空网格缺测不补零 |
| `boundaryDensityDifference` | 边界 `absolute_difference_person_per_m2` | 同一快照 `status=valid` 的相邻网格边界密度绝对差最大值，人/m² |

这些局部摘要沿用事中 `values_at` 的口径；不是某个任意固定网格的曲线。局部和边界文件与全局文件按快照和时间严格对齐，并核对 `observation_samples.jsonl` 的提交行数。源文件必须属于同一实验，通过归档和 GridFS 两处 SHA256 校验。缺少对应源文件时保留 `null` 并记录限制，存在文件但校验失败时拒绝导入。

`pressureProxy` 保留为旧接口兼容字段；当前事中归档未测此指标，必须为 `null`，不能拿心理压力 `stress_avg` 替代。速度为 0 是有效观测，不能当成缺失。未观测值使用 JSON `null` 或原生事中 CSV 的空单元格，不填写估算值。普通手工 CSV 可显式填写 `null`。

规范 JSON 顶层仍为 `name`、`sourceKind`、`sourceNote`、`observations`、`metadata`。`regionId` 表示实际观测范围；全局归档为 `global`。纪念塔合成输入继续保留 A/B/C 分区及原始流量、人口属性等工程输入，不能将分区人数误称为全局实测。原文未测量的连续指标保持为空，之后的 SUMO 输出与输入分开保存。

数据库保存完整观测。浏览器 `localStorage` 只保存数据集 ID、版本、来源绑定、记录数和精确统计摘要；刷新后通过带工作区范围和版本校验的分页时间线读取完整数据。迁移只缩小已有后端引用的事后缓存，不删除用户对话或其他阶段数据。
