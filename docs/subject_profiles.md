# 主体库数据库结构

主体库使用 MongoDB `crowdsim.subject_profiles` 集合。人群、协同角色和车辆共用一个集合，每个主体一条文档；典型角色与行为指标作为内嵌数据保存，读取一个主体即可获得完整画像。

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `_id` / API `id` | string | 稳定记录标识，重命名不改变 ID |
| `name`, `type`, `topic`, `status` | string | 名称、资源类型、分类、状态 |
| `summary`, `updated` | string | 页面说明与可编辑的展示日期 |
| `tags` | string[] | 主体标签 |
| `details` | string[][] | 结构化属性，每项为 `[属性名, 属性值]` |
| `agentClass` | object | `classId`、模型、默认数量、能力、通用/状态/实例字段及实例示例；保留原仿真属性 |
| `agentPortrait` | object | 角色、图标、主题色、标签、基础属性、行为指标、典型角色、来源及依据说明 |
| `origin` | string | `builtin` 内置导入或 `custom` 用户新增，由后端维护 |
| `revision` | int | 乐观锁版本，每次修改/删除递增 |
| `is_deleted` | bool | 软删除，API 不返回已删除主体 |
| `sort_order` | int | 内置主体保留原顺序，新增主体排列在其后 |
| `created_at`, `updated_at` | ISO 8601 UTC string | 后端创建/更新时间，独立于页面展示日期 |

画像内嵌字段：`facts` 为 `[属性名, 属性值][]`；`metrics` 为 `[指标名, 0–100 数值, 说明][]`；`personas` 包含 `name/meta/badges/action`；`sources` 包含 `title/publisher/supports/url`。单个主体配置上限 512 KiB。

索引：可见主体的 `name` 唯一索引（`is_deleted: false`）；`(is_deleted, sort_order)` 目录索引；`(topic, is_deleted)` 分类索引。`agentClass.classId` 属于可编辑仿真属性，不作为数据库主键。

## 初始化与接口

`python -m data_service` 的 8768 数据服务初始化集合，将 `data_service/resources/subject_profiles.json` 中原前端 11 个完整主体导入。仅补充不存在的 ID，重启不覆盖编辑，也不恢复删除。

- `GET /api/crowdSim/subject-profiles`：完整目录与实际统计，可传 `search`。
- `GET /api/crowdSim/subject-profiles/{id}`：读取单个主体。
- `POST /api/crowdSim/subject-profiles`：新增主体。
- `PUT /api/crowdSim/subject-profiles/{id}`：保存完整配置，需要 `revision`。
- `DELETE /api/crowdSim/subject-profiles/{id}`：软删除，请求体 `{ "revision": 1 }`。

成功返回 `{ "data": ... }`；校验失败 400，不存在 404，同名或版本冲突 409，数据库暂不可用 503。兼容同路径 `/crowdSim` 前缀。

前端仅从后端读取目录，不使用静态画像或浏览器数据替代数据库。修改可点击“保存”，关闭窗口或用于智能体建模时也会先等待保存完成；失败保留草稿。多主体修改逐项保存，遇到冲突停止，已成功项目保留新版本号，未成功项目仍留在页面。浏览器旧版画像覆盖与自建主体显示导入入口，成功写入数据库后才清理对应旧配置。

主体库不调用模型或启动 SUMO，不新增进程或依赖文件。测试：`python -m pytest -q tests/test_subject_profiles.py`。

前端保存状态、失败草稿、重试、共享刷新及旧配置导入测试：在前端目录运行 `node --test tests/subjectLibraryPersistence.test.mjs`，无需构建。
