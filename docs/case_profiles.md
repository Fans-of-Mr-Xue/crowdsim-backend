# 案例库持久化与 HTML 详情页

案例库使用现有 `crowdsim` 数据库中的统一集合 `case_profiles`。一条文档代表一个案例，不按事件建立新表。目录、详情、新增、编辑、删除均调用独立数据服务（8768），不启动 SUMO 或模型推理。

## 数据结构

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `_id` | string | 稳定案例 ID；已有案例沿用原事件 ID，新案例使用 UUID |
| `name`, `type`, `topic`, `status`, `summary`, `tags` | string / array | 名称、类型、专题、整理状态、摘要、标签 |
| `eventId`, `eventDate`, `eventEndDate` | string | 原事件标识、开始和结束时间；保留原有时间精度 |
| `locationName`, `locationType`, `country`, `city` | string | 地点、空间类型、国家 / 地区、城市 |
| `coordinates` | number[] | `[纬度, 经度]`，也可留空 |
| `details` | string[][] | 可编辑的属性名称和值，保留原有详细描述 |
| `sourceLabel`, `sourceUrl`, `sourceLinks`, `datasetLinks` | string / object[] | 信息来源、网页、数据集地址及说明 |
| `timeline` | object[] | 时间线，`timestamp` / `content`，保留原有额外属性 |
| `eventRecord` | object | 原事件完整数据，包括等级、位置、说明、时间线等 |
| `analysisProfile` | object | 原案例完整分析数据；目录编辑不会丢弃这部分数据 |
| `projectProfile` | object | 专题项目资料；MADRAS 的机位、观测范围、视频、密度时序、图片及来源全部保留 |
| `extensions` | object | 后续扩展的案例属性 |
| `linkedPlanIds`, `linkedModelIds` | string[] | 预留预案与模型的稳定 ID；现阶段默认空数组，不推断关联 |
| `detail_pages` | object[] | HTML 版本元数据：ID、GridFS 文件 ID、文件名、大小、SHA256、上传时间、来源 |
| `active_page_id` | string | 当前使用的 HTML 版本 ID |
| `revision`, `origin`, `created_at`, `updated_at`, `is_deleted` | int / string / bool | 乐观锁、内置 / 自建来源、记录时间、软删除 |

索引：未删除案例名称唯一；删除状态与目录排序；事件 ID 与删除状态；专题与地点类型。名称修改不改变稳定 ID。同名未删除案例不可重复创建。

HTML 内容保存在 GridFS bucket `case_pages`，MongoDB 对应 `case_pages.files` 与 `case_pages.chunks`。`case_profiles` 只存版本元数据和引用。软删除案例不销毁附件，也不会在重启时自动恢复该案例。

## 初始迁移

`data_service/resources/case_profiles.json` 包含原前端全部 12 个案例及原事件、分析和专题数据。地点类型根据已有地点描述初始化；没有依据的国家、城市、网页等字段留空，由用户补充。

启动数据服务时创建集合和索引，仅补入不存在的固定 ID，不覆盖已编辑或已删除记录。里昂灯光节的原专题资料完整保留在 `projectProfile`；其单文件详情页为 `resources/case_pages/madras-lyon-2022.html`，首次初始化挂载到该案例。已上传的替代页面不会被内置页面覆盖。

需要重新生成内置 HTML 时可执行 `python -m data_service.services.case_detail_html`。该命令只生成附件源文件；不会覆盖数据库已有附件。图片内嵌为 data URI，机位地图使用公开底图，证据图和视频保留原公开地址。

浏览器旧版自建案例可在案例库点击“导入”。每条案例成功入库后才移除对应本地记录；失败的记录继续保留。前端正常新增、编辑、删除均写数据库，浏览器不再承担案例库的正式存储。

## HTTP 接口

前缀 `/api/crowdSim/cases`，亦兼容 `/crowdSim/cases`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 全部未删除案例与实际统计 |
| POST | `/` | 新建案例 |
| GET | `/{id}` | 完整案例和详情页版本 |
| PUT | `/{id}` | 更新案例字段，必须附 `revision` |
| DELETE | `/{id}` | 软删除，JSON `{ "revision": 1 }` |
| PUT | `/{id}/links` | 预留挂载接口，JSON `{ "revision": 1, "linkedPlanIds": [], "linkedModelIds": [] }` |
| POST | `/{id}/pages` | multipart：`file` 和 `revision`，挂载新的 HTML 版本 |
| GET | `/{id}/pages/{pageId}` | 查看指定版本，`?download=1` 下载原文件 |

关联接口整体替换两个 ID 数组，只校验格式并去重；后续实现关联业务时再加入资源存在性检查和展示功能。普通案例编辑未传关联字段时保留已存的 ID。

JSON 配置最多 4 MiB；未知顶层字段拒绝写入，扩展属性放在 `extensions`。并发修改返回 409，缺失资源返回 404，数据库尚未就绪返回 503。请求与响应采用现有 `data` / `message` 包装。

## 用户上传的详情页

支持单个 UTF-8 `.html` / `.htm` 文件，非空且最多 16 MiB。资源应使用内嵌 CSS / JavaScript、data URI 图片或公开网络地址；上传单个 HTML 不会同时上传相对路径引用的本地文件夹。

新页面成为当前版本，历史页面仍可查看和下载。当前页面相同 SHA256 的重复上传不增加版本。上传时校验案例版本；并发失败会清理刚写入的 GridFS 文件。案例与附件 ID 必须匹配，不能借另一案例 URL 访问文件。

页面在 iframe 中展示，并由服务端 CSP 同时施加 sandbox。允许页内脚本和公开资源展示，但不授予 `allow-same-origin`，不能访问平台 origin 的 Cookie / 存储 / 父页面，也禁止表单和任意 fetch 请求。网络地图、公开视频等外部资源仍依赖其原服务可用性。

## 验证

`python -m pytest -q tests/test_case_profiles.py` 覆盖 12 个案例完整迁移、重启不覆盖编辑 / 删除、关联 ID 保留、乐观锁、HTML 重复上传、旧版本、并发上传文件清理、文件有效性、HTTP CRUD 和页面隔离响应头。测试使用 mongomock / 临时 HTTP 服务，不承担正式仿真实验职责。
