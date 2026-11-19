# 事后复用事中执行链与行为证据

执行协议：`post-during-runtime/1.0`，2026-11-19。

## 执行边界

事后新运行使用 `workers/native_runtime.py` 调用原有 `SimulationRuntime`，复用个体决策调度、规则/模型入口、信息接收与信任判定、动作执行及 SUMO 运动。事中模块、进程清理逻辑没有改动。每个组/种子创建独立实例，并在成功、失败、取消时调用原有 `runtime.close()`。

当前绑定纪念塔事中默认规则运行链；没有把旧归档中缺失的人工社会模型、认知或引导历史当成已知事实。导入上下文明确区分当前配置与历史配置，记录源数据版本、配置哈希和运行时行为模块哈希。自定义人工社会文本修改仍需工程映射，不能静默生效。

上传观测是事实分布校准目标。只以首帧空间分布初始化，后续位置与行为由运行产生，不按历史轨迹逐帧强制回放。F-00没有额外原生控制动作；这表示当前事实重构的显式假设，不代表证实历史上不存在控制。

所有组共享初始化分布、个体属性采样和种子序列。反事实措施在运行边界执行，不再用预设遵从比例在编译时决定出口。运行完成并不等于F-00拟合验收通过，也不等于影响路径已被因果确证。

## Agent 类型

SUMO物理类别是 `pedestrian`。纪念塔当前预设有8个角色：通勤者5%、工作服务者5%、居民日常10%、消费者10%、社交游客20%、休闲游客30%、公共事务参与者10%、过境行人10%。每个人分别采样步速、移动能力、风险/拥挤容忍度、耐心、遵从倾向、信息信任等属性。角色不是8套独立运动模型，不能据角色名称虚构行为差异。实际角色及个人属性以本次归档的 profiles 为准。

## 原生动作契约

`GET /capabilities` 返回原生动作契约和类型。统一设计继续使用schemaVersion=2，但每组execution使用：

```json
{
  "referenceDesignId": "F-00",
  "actions": [
    {
      "id": "guide-1",
      "actionType": "publish_guidance",
      "atSeconds": 120,
      "target": {"regionId": "使用executionContext.nativeTargets.guidanceRegionId"},
      "parameters": {"message": "请有序疏散", "command": "disperse", "durationSeconds": 120}
    }
  ]
}
```

动作编号在组内唯一。参照之间保留相同动作编号，才能比较单个参数。F-00的 `baselineExecution={"actions":[]}`。单因素对照只改变一个输入参数或增删一个动作；动作消融相对完整策略删除一个动作，其余输入一致。空动作数组可用于消融或零差异对照。

支持：`publish_guidance`、`set_inflow_rate`、`set_edge_capacity`、`set_edge_risk_weight`、`reroute_group`、`set_route_distribution`、`observe_only`。参数及其工程含义沿用 `crowdsim/domain/action_contract.py` 和现有执行器。例如set_edge_capacity目前通过速度上限系数作用于道路上的行人，不能描述为已经改变物理道路宽度。

引导的 `target.regionId` 关联活动/事件；覆盖范围可选SUMO米制的x、y、radius，必须完整填写，半径为正。缺省全局覆盖。消息接收、信任与实际动作分别记录，不能把消息已发出或默认的understood模型标记当成真实人员认知证据。

旧版open_gate/guidance三参数快照仍可读取，但新运行返回 `NATIVE_ACTION_MAPPING_REQUIRED`，须重新设置动作并确认。旧执行协议的已完成运行不能续入新版批次，也不能与新版F-00混算。前端保留历史工程草案，要求明确确认重新映射，不自动把遵从比例翻译成实际响应率。

## 数据库与上传字段

已有区域观测表不强制增加列。原字段timeSeconds、regionId、population及可选density、meanSpeed等继续作为校准目标，原上传文件不修改。

新运行在 `postanalysis_runs` 保存executionProtocol、runtimeActions、behaviorEvidence。完整证据文件流式写入现有 `postanalysis_files` GridFS，运行文档仅保存文件ID、SHA256、大小和记录数量，避免将完整个体历史塞进一个BSON文档。

行为JSONL区分：

| kind | 内容 |
| --- | --- |
| motion | personId、时间、坐标、道路、速度、相邻实际采样间隔计算的加速度、移动/停止/加减速、已知事件、活动状态 |
| message | message_id、person_id、delivered_at、trusted及模型的understood标记 |
| decision | 决策前profile/state/observation/candidates、proposed plan、实际execution及其status |
| intervention | 原生场景措施的计划/实际执行时点、输入与回执 |

实验批次暂停/继续等用户操作不写入这条行为证据流。正常活动驻留与场景限流标记只用于区分停止原因及测量口径，不当成人群决策或用户操作造成的机制效应。

保留原有标准决策分表、个体profile、消息与人员生命周期文件，完整归档后的文件可通过：

`GET /runs/{runId}/behavior-evidence?name=behavior_evidence.jsonl&offset=0&limit=65536`

读取。先按owner/workspace校验运行权限，再按该运行的工件清单查文件；返回base64字节块、nextOffset、eof及SHA256，最大块256KiB。客户端先拼接字节后解码UTF-8，不能把每块独立当成完整JSON行。路径和fileId不能由客户端任意指定。

若需要说明历史真实个体行为，须另提供历史的personId/时间/坐标或道路/速度，以及消息接收和决策执行记录；不能从区域人数曲线补造。新增重跑证据也不能冒充原始历史记录。

## 当前分析范围

本次完成执行链、原生动作配置及证据归档/读取，为后续源算法适配提供输入。现有配对效应保留，行为证据计数不等于路径权重或机制效应。不能把控制参数、消息提及率、人数占比或LLM解释直接当成原机制算法的动因权重。原powergame三类算法的完整迁移仍需在这些真实输入之上逐项接入与验证。
