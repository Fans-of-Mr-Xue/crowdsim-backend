# 无损决策日志拆表（版本 1）

新实验默认使用 `decision_tables_jsonl` 格式，不引入数据库。一次决策仍在原来的 `record_decision()` 入口记录；没有减少记录次数、抽样、截断邻居或删除字段。仿真、POI 和 LLM Skill 行为不变。

## 文件与关联

同一个 `runs/<run_id>/` 内包含六个表：

| 文件 | `value` 内容 |
| --- | --- |
| `decisions.jsonl` | 原始记录的动态 context、完整候选、plan 引用、完整 execution，以及其余字段 |
| `decision_profiles.jsonl` | 完整画像；同一行人画像变化后保存新版本 |
| `decision_plans.jsonl` | 完整 BehaviorPlan，其中非空路线改为路线引用 |
| `decision_routes.jsonl` | 完整、有序的道路 ID 数组，包含重复道路 |
| `decision_person_ids.jsonl` | 一个原始行人 ID 字符串，不转成整数 |
| `decision_neighbors.jsonl` | 完整、有序的邻居 ID 数组，以行人 ID 表中的整数编号表示 |

每个文件的每行都是 `{"id":0,"value":...}`。`id` 在本表内从 0 递增，只在当前运行目录有效；原始数据全部位于 `value`，避免与原始 `id` 等字段冲突。

被替换的字段使用 `{"$ref":"profiles","id":0}` 这样的引用。`$ref` 可取 `profiles`、`plans`、`routes`、`neighbors`；其编号指向相应表的 `id`，不是行人编号。

主表替换的字段仅为：

- `value.context.profile`：完整画像引用。
- `value.plan`、`value.context.state.current_plan`：完整计划引用。
- `value.context.observation.neighbour_ids`：非空邻居列表引用。
- `value.candidates[]` 中非空的 `edges`、`route_edges`、`next_route_edges`：路线引用；候选其他内容仍在主表。

计划表中上述三个非空路线字段同样替换为路线引用。空数组、`null` 和缺失字段保持原样，不互相转换。对象之外的意外 JSON 值也保留。

邻居表示示例：

```json
{"id":0,"value":"001"}
{"id":1,"value":"1"}
```

上述两行属于 `decision_person_ids.jsonl`。原邻居列表 `["001","1","001"]` 在 `decision_neighbors.jsonl` 中保存为：

```json
{"id":0,"value":{"encoding":"person_ids","ids":[0,1,0]}}
```

顺序、重复项和前导零全部保留。若意外出现非字符串邻居 ID，整份列表保存为 `{"encoding":"original","ids":[...]}`，不做类型强制转换。

## 不丢失信息的边界

保留的是旧记录的 JSON 信息，不是旧文本的空格布局。仍使用原来的 dataclass、set、Path JSON 转换规则，使用紧凑 UTF-8 JSON 输出。不会合并整数 `1` 与浮点数 `1.0`，也不会改变道路或邻居顺序、异常执行详情、未知扩展字段、路线位置哨兵值和缺失值。

记录时立即冻结整个输入，后续行人状态修改不会影响已记录内容。沿用原有时序：context.state 是记录入口调用时的状态，当前后端在计划执行后调用此入口；本次没有将它改为执行前状态。

新计划与 `state.current_plan` 内容相同时共享计划编号。新计划被拒绝时，两者可能不同，各自保存，不能推定两者永远相同。

现有 `profiles.jsonl` 保留原用途，不取代完整且可版本化的 `decision_profiles.jsonl`。命令、消息、轨迹、指标、生命周期等日志保持现有格式。

## 去重、缓存与写入

画像、计划、路线和邻居列表使用完整序列化字节比较去重，不仅比较哈希。缓存上限分别为 4、4、16、32 MiB，计入估算的索引开销；这不是整个进程的精确内存上限。行人 ID 映射随本次运行保留。

缓存淘汰后，相同内容再次出现可能新增一行，但旧行和旧引用不会失效，不丢失信息。由于有缓存上限，实际压缩率不保证达到离线全量去重的估算值。计划一般包含每次变化的时间等字段，主要通过复用同次决策的 current_plan 降低重复。

六个文件使用长期打开的缓冲句柄，而不是每次决策打开文件。每 256 条决策或写入时距上次 flush 达到 1 秒，执行一次 flush，先依赖表、后主表。无新写入时没有后台定时器。正常关闭会 flush 并关闭所有句柄；重置关闭旧运行，创建新目录和新编号；初始化或 tick 出错则标记 aborted 并释放资源。

这里的 flush 不是 fsync，也不是跨文件事务。进程被强杀、断电或磁盘损坏时，不保证缓冲尾部完整或多文件原子性；不支持续写已有运行。文件写入失败可能留下未被主表引用的依赖行，不会把失败记录冒充完整记录。

## 清单与诊断

原 `manifest.json` 增加 `decision_log_format`、`decision_log_version`、`decision_log_manifest`、`decision_log_replay_supported`。

`decision_manifest.json` 记录格式版本、状态（recording / complete / aborted）、六个文件名、记录数、字节数、SHA-256、引用规则、缓存命中与淘汰统计。运行开始先写初始清单，关闭时更新为最终清单；运行中的磁盘清单不是实时检查点。只有成功关闭后的 complete 清单才是最终文件统计，aborted 统计不能视为完整性保证。

运行诊断与 `summary.json` 增加 `decision_log`，包含主表决策数、六表总字节数、各表行数、flush 次数、缓存淘汰数、关闭状态。运行中 total_bytes 为已交给缓冲写入器的数据量，不一定已经落盘，也不包含其他日志与清单大小。

结束时先关闭 SUMO，再关闭决策日志，最后通过临时文件替换发布 `summary.json`。正常关闭后的总结与实时 diagnostics 一致：state=CLOSED、engine.closed=true、decision_log.closed=true。CLOSED 表示资源已关闭；若此前仿真出错，last_error 仍保留，不能将 CLOSED 等同于实验成功。

关闭失败时仍尝试其余清理并写错误总结，state=ERROR；close_errors 按 engine、decision_log、summary 阶段保存错误，engine.close_error 和 decision_log.error 提供各自原因。SUMO 关闭失败保留句柄和 closed=false，后续 close 可重试；已成功关闭的资源不重复操作。日志写入失败即使句柄已关闭也保留失败，不会在重复 close 时变成成功。总结写入失败向调用方抛出错误并保留内存诊断，后续可重试发布；磁盘持续不可写时无法保证错误总结落盘。历史 summary 不自动改写。

## 兼容读取与基础回放

`crowdsim/infrastructure/decision_log_reader.py` 提供 `DecisionLogReader(run_directory)`、`iter_plans()` 和 `iter_plan_batches()`。旧日志逐行提取内联 plan；新日志严格识别格式/版本，先验证六表存在且清单 complete，再校验主表、计划表、路线表的行数、字节数和 SHA-256。只解析主表实际引用的 proposed plan，不把 state.current_plan 或未引用计划当成新决策。

计划、路线表扫描一次建立 `array('Q')` 字节位置索引，每行索引占 8 字节；随后按需 seek 读取，默认 JSON 字节缓存预算 4 MiB。不会把所有计划对象或全部主表加载进内存。读取器不复原画像、邻居和完整 context，也不声明其他三个依赖表的内容已校验。应在 `with DecisionLogReader(path) as reader:` 中使用并及时关闭迭代器；同一读取器用于一个读取流程，不用于并发迭代。

`scripts/replay_experiment.py` 同时兼容旧内联格式 `decision_inline_jsonl`（版本 0）和拆表格式（版本 1）。计划和 CSV 轨迹按时间批次读取，最多保留当前批次和预读批次；不调用规则引擎或 LLM 生成新计划。保留计划执行前的边界准备及调度时间更新，然后执行已保存计划。时间倒序、重复轨迹、非法引用、未消费尾部或损坏数据明确失败，不静默跳过。缺失/空值虽被存储保留，但不是合法 BehaviorPlan 的记录无法作为计划回放。

回放在执行前校验原配置与归档需求的 SHA-256，并检查 SUMO 版本。历史配置被修改时明确失败，需要恢复记录时配置；路网、画像配置和代码版本仍需使用兼容的原实验环境。这不是历史环境自动恢复功能。基础回放暂不执行保存的外部命令，非空 commands.jsonl 会明确失败，避免忽略事件/策略后误报通过。

回放报告版本为 2，包含源运行 ID/目录、输入格式/版本、源清单哈希、输入文件大小及修改时间、消费计数、位置/速度误差、总差异数（只保留前 50 条差异样例）、人口账本匹配及新回放运行目录。默认写在新回放目录的 `replay_report.json`，不会写回源目录；校验失败且尚未创建回放运行时只输出 JSON，也可通过 `--output /path/report.json` 显式指定报告位置。

`run_acceptance.py` 的 F16 按格式分别展示带源运行标识的报告证据。只用新格式报告证明新格式回放；不识别格式的历史报告、源文件变化后的报告会被忽略。最新有效新格式报告通过时 pass，失败时 fail，没有有效新格式证据时 partial。它读取保存的证据，不自动发起新回放，也不会把旧格式成功冒充新格式通过。

新运行清单与诊断标注 `replay_supported=true`，表示格式已具备且通过基础真实 SUMO 回放验证，不保证任意旧环境、事件或命令实验都可精确复现。之前生成的 `false` 清单不改写；读取器按已支持的格式/版本判断能力，仍能读取它们。

历史约 2.7 GB 的 `decisions.jsonl` 不会自动改写或删除。只有新运行使用新格式。

## 验证

在 sumo 环境运行：

```bash
python -m unittest tests.test_decision_table_writer tests.test_simulation_loop -v
python -m unittest tests.test_decision_log_reader tests.test_replay_experiment -v
python -m unittest discover -s tests
python -m unittest discover -s pedestrian_decision_skill/tests
python scripts/run_experiment.py --mode rule --count 20 --steps 120
python scripts/replay_experiment.py runs/<source_run_id>
```

运行实验的命令是短时真实 SUMO 冒烟验证，不是完整热点效果验收，也不调用 DeepSeek。查看六个表、两个清单以及 `summary.json` 的 `decision_log`；预期状态为 complete，主表 records 与清单行数一致。将返回的 run_id 代入最后的回放命令，查看新回放目录中的报告。

开发验证时，将历史 2.7 GB 日志的前 5000 条记录仅写入临时目录：原文本 18,518,217 字节，六表合计 10,480,358 字节，减少约 43.4%。此结果只是该样本的实测，不能外推为完整日志的固定压缩率；历史原文件的大小与修改时间保持不变，临时输出已由测试流程清理。
