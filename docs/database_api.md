# CrowdSim 数据库接入说明

## 服务结构

数据库功能沿用 `crowdsim-backend` 仓库，集中在 `data_service/` 中。数据服务独立运行，负责 MongoDB 连接和共享数据集接口；`crowdsim/` 继续负责 SUMO 仿真，`dialogue_service/` 和 `postanalysis_api/` 保留现有职责。

```text
crowdsim-backend/
├── crowdsim/                 SUMO 仿真
├── dialogue_service/         对话服务
├── postanalysis_api/         事后接口
└── data_service/
    ├── __main__.py           python -m data_service 入口
    ├── server.py             独立 HTTP 服务
    ├── api.py                数据库与共享数据集接口
    ├── repository.py         MongoDB 读写、数据校验和版本控制
    ├── config.py             后端环境变量与 .env 配置
    ├── connectors/           预留气象连接器
    └── crawlers/             预留独立爬虫与定时任务
```

数据服务不加载 SUMO，也不启动爬虫。后续爬虫应提供独立进程入口，由任务调度工具执行，并通过数据服务接口或共享仓储写入数据库。

## 数据链路与端口

本机开发时，前后端交互与数据库连接使用不同端口：

```text
前端（通过开发代理调用 HTTP 接口）
  → 本机 127.0.0.1:8768：Python 数据服务
  → 本机 127.0.0.1:27018：SSH 隧道入口
  → 服务器 127.0.0.1:27018：MongoDB
```

`8768` 是数据服务的 HTTP 监听端口；后端通过本机 `27018` 连接数据库，SSH 隧道再将连接转发到服务器的 `27018`。启动数据服务时须保持 SSH 端口映射隧道运行。

前端只调用 HTTP 接口，数据库连接和账号密码均由后端处理。`127.0.0.1:27018` 已被 SSH 隧道占用，数据服务使用独立的 `8768` 端口；HTTP 与 MongoDB 使用不同协议，两个端口不需要一致。

| 端口 | 服务 | 用途 |
| --- | --- | --- |
| 本机 8080 | 前端开发服务 | 浏览器页面和开发代理 |
| 本机 8768 | data_service | 前端调用数据库 HTTP 接口 |
| 本机 27018 | SSH 隧道入口 | 后端连接远程 MongoDB |
| 服务器 27018 | MongoDB | 数据存储与查询 |
| 服务器 3000 | SSH | 建立到数据库服务器的隧道 |
| 本机 8765 | SUMO 仿真服务 | 仿真 WebSocket 和现有事后接口 |

## 启动步骤

### 1. 建立 SSH 隧道

在一个终端中执行以下命令，并保持该进程运行。如果同一隧道已经在后台运行，无需重复启动。

```sh
ssh -N -L 127.0.0.1:27018:127.0.0.1:27018 -p 3000 hyq@211.81.55.186
```

这里第一个 `127.0.0.1:27018` 是本机监听地址，第二个 `127.0.0.1:27018` 是 SSH 服务器视角下的 MongoDB 地址。

### 2. 安装数据服务依赖

在 `crowdsim-backend` 仓库根目录执行以下命令。数据库依赖与其他后端依赖统一维护在根目录的 `requirements.txt` 中，数据服务仍独立运行，无需启动 SUMO。

```sh
python -m pip install -r requirements.txt
```

本工作区已有 `.venv`，可以直接使用对应解释器安装依赖：

```sh
cd /Users/hyq/Documents/MyCode/TJU/Rain/crowdsim-backend
.venv/bin/python -m pip install -r requirements.txt
```

### 3. 配置数据库连接

数据服务读取仓库根目录的 `.env`，系统环境变量优先于 `.env` 中的同名配置。如果尚未创建 `.env`，可复制 `.env.example` 为 `.env`，再填写数据库密码；已有配置无需覆盖。`.env.example` 是供其他开发者参考的配置模板，不包含实际密码。

```dotenv
CROWDSIM_MONGO_HOST=127.0.0.1
CROWDSIM_MONGO_PORT=27018
CROWDSIM_MONGO_DATABASE=crowdsim
CROWDSIM_MONGO_AUTH_SOURCE=admin
CROWDSIM_MONGO_USERNAME=user
CROWDSIM_MONGO_PASSWORD=<填写数据库密码>
```

当前服务器的业务数据库是 `crowdsim`，账号的认证数据库是 `admin`，两者用途不同。`.env` 已被 Git 忽略，账号密码不应放入前端配置或提交到仓库。

也可以设置 `CROWDSIM_MONGO_URI`。设置该变量后，连接地址和认证参数使用 URI 中的配置，业务数据库仍由 `CROWDSIM_MONGO_DATABASE` 指定。

### 4. 启动数据服务

在仓库根目录执行：

```sh
python -m data_service --host 127.0.0.1 --port 8768
```

本工作区可以直接使用以下命令：

```sh
cd /Users/hyq/Documents/MyCode/TJU/Rain/crowdsim-backend
.venv/bin/python -m data_service --host 127.0.0.1 --port 8768
```

`python -m data_service.server` 是等效入口。服务默认监听 `127.0.0.1:8768`，保持进程运行即可。使用数据库无需启动 SUMO；需要仿真时，再单独启动原有 `8765` 服务。

### 5. 打开前端

当前工作区前端位于 `MACE-FE2/frontend`。启动前端开发服务后，打开 `http://localhost:8080`，进入 CrowdSim（Rain City），依次点击「知识库」→「知识资源」中的「数据库」卡片 →「CrowdSim 数据库」标签。

```sh
cd /Users/hyq/Documents/MyCode/TJU/Rain/MACE-FE2/frontend
npm run dev
```

页面支持检测数据库连接、执行临时读写测试、新建共享数据集、查询以及查看和编辑。保存后的数据写入服务器 MongoDB，其他连接到同一数据库的用户刷新后即可读取。

## 前端代理与部署

前端请求 `/api/crowdSim/database/*` 和 `/api/crowdSim/datasets*`，Vite 将这两类请求转发到 `http://127.0.0.1:8768`。如果数据服务地址改变，可通过前端服务端配置变量 `CROWDSIM_DATABASE_API_TARGET` 修改代理目标，并重启前端开发服务。

Vite 开发代理不包含在静态构建产物中。部署前端静态页面时，需要在 Web 服务器或反向代理中配置相同的路径转发，让浏览器继续通过同源 HTTP 接口访问数据服务。

多人使用时，可以部署共享的数据 HTTP 服务，由该后端连接 MongoDB。使用 SSH 隧道的部署方式下，隧道只需运行在数据服务所在机器上，浏览器用户不需要直接连接数据库。

## HTTP 接口

成功响应使用 `{"data": ...}`，业务错误响应使用 `{"message": ...}`。服务也支持不含 `/api` 前缀的 `/crowdSim/...` 同名路径，方便已有反向代理移除前缀。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/crowdSim/database/status` | 检查连接，返回数据库名称和集合数量 |
| POST | `/api/crowdSim/database/smoke-test` | 写入临时记录、读回并删除，返回检查结果 |
| GET | `/api/crowdSim/datasets` | 分页查询数据集列表 |
| POST | `/api/crowdSim/datasets` | 创建数据集 |
| GET | `/api/crowdSim/datasets/{id}` | 读取数据集详情和记录 |
| PUT | `/api/crowdSim/datasets/{id}` | 完整更新数据集，需要提供 revision |

数据集列表支持 `page`、`page_size`、`search` 和 `source` 参数。`search` 按名称进行不区分大小写的字面匹配，`source` 按来源精确匹配；默认每页 20 条，最多每页 100 条。列表响应包含 `items`、`total`、`page` 和 `page_size`，不携带完整 `records`，详情接口返回记录内容。

新建数据集的请求示例：

```json
{
  "name": "降雨观测",
  "source": "manual",
  "description": "手工整理的观测数据",
  "records": [
    {"date": "2026-10-09", "rain_mm": 12}
  ]
}
```

`name` 为必填名称，`source` 和 `description` 可省略，`records` 默认空数组且每条记录必须是 JSON 对象。服务返回 `id`、`record_count`、`created_at`、`updated_at` 和从 1 开始的 `revision`。数据存入 `crowdsim.datasets`，使用自动生成的字符串 ID。

更新使用 PUT，发送完整的 `name`、`source`、`description` 和 `records`，并附上读取时的 `revision`。更新成功后版本递增；如果其他用户已修改该数据集，接口返回 409，保留当前数据，需刷新后重新编辑。

名称最长 200 字符，来源最长 200 字符，说明最长 4000 字符；请求体上限为 8 MiB，单个数据集最多包含 10000 条对象记录。记录字段名不能包含点、空字符或以 `$` 开头，浮点数必须为有限数，整数必须位于 64 位有符号范围内。

| 状态码 | 含义 |
| --- | --- |
| 200 | 查询、更新或连通性测试请求成功；测试结果需检查响应字段 |
| 201 | 数据集创建成功 |
| 400 | 请求参数或数据不合法 |
| 404 | 数据集不存在 |
| 409 | 更新版本冲突 |
| 413 | 请求体超过大小限制 |
| 503 | 数据库暂不可用，需要检查配置、认证和 SSH 隧道 |

## 验证与排查

数据服务启动后，可以直接检测连接：

```sh
curl http://127.0.0.1:8768/api/crowdSim/database/status
curl -X POST http://127.0.0.1:8768/api/crowdSim/database/smoke-test
```

连接正常时，状态接口返回 `connected: true`、`database: "crowdsim"` 和 `collection_count`。读写测试返回 `write_ok`、`read_ok` 和 `cleanup_ok`，三项均为 `true` 表示临时记录已成功写入、读回并清理。测试只操作本次生成的临时记录，不会删除已有数据集。

如果启动时提示端口占用，先确认是否已有数据服务监听 `8768`，或已有 SSH 隧道监听 `27018`，避免重复启动。如果状态接口返回 503，检查 SSH 隧道是否运行、MongoDB 端口是否为 `27018`、业务库是否为 `crowdsim`，以及账号密码和认证库 `admin` 是否正确。如果直接访问 `8768` 正常但前端失败，检查前端代理目标和开发服务是否已重启。

## 后续爬虫接入

爬虫可调用相同 HTTP 接口，也可以在独立进程中复用仓储。直接使用仓储的示例：

```python
from data_service.repository import DatasetRepository

repository = DatasetRepository()
try:
    saved = repository.create({
        "name": "采集结果",
        "source": "crawler",
        "records": [{"value": 12}],
    })
    result = repository.get(saved["id"])
finally:
    repository.close()
```

当前已实现共享数据集的新增、读取和更新。`connectors/` 与 `crawlers/` 仅预留模块边界，尚未实现 NOAA/NASA 自动采集、自动入库或定时任务；前端现有天气请求仍使用原来的服务。原有浏览器本地知识库数据迁移和仿真日志数据库化也尚未启用。
