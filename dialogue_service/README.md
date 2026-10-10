# CrowdSim 本机对话服务

该目录是独立的**本机对话存储服务**，不修改原有 SUMO/WebSocket 仿真服务。SUMO 默认使用 `8765`，事后反事实入口使用 `8766`；本服务只监听 `127.0.0.1:8767`，负责保存和读取对话记录。

前端页面继续使用 `8080`。前端的 `/crowdsim-dialog-api` 只代理到 `8767` 读写对话记录；模型请求走 `/qwen-api` 代理到本机 `8800` 的 OpenAI 兼容接口。统一启动入口会检查并建立所需的 SSH 隧道；对话存储进程本身不建立 SSH 连接，也不转发模型请求：

```text
CrowdSim 页面 :8080 ──对话记录──→ 存储服务 :8767
CrowdSim 页面 :8080 ──模型请求──→ 本机 vLLM :8800
```

## 启动

```powershell
python -m dialogue_service.server
```

日常使用在 `crowdsim-backend` 仓库根目录执行 `./run.sh`，统一入口会启动本服务、数据服务、SUMO 和所需的 SSH 隧道；上面的命令仅用于单独启动对话存储。前端不再自动启动后端进程。如果已运行本服务，统一入口会检查 `/health` 后复用其 `8767` 端口；其他服务占用此端口时会明确报错。

开发时请在启动前端的同一台电脑上打开 Vite 的 `localhost:8080` 地址。本机 `8800` 可以是本机运行的模型，也可以是用户自行建立的 SSH `-L 8800:localhost:8800` 转发；统一入口会按 `.env` 中的 `CROWDSIM_MODEL_SSH_*` 配置自动建立转发；单独启动对话存储时仍需手动保证模型连接可用。

模型 API 如需 Bearer 密钥，可在本机设置 `CROWDSIM_MODEL_API_KEY` 环境变量后启动前端 Vite 服务；密钥由开发代理加到模型请求中，不写入浏览器代码。

主对话快照写入 `runs/dialogues/*.json`，工作台对话写入 `runs/dialogues/workbench/*.json`。`runs/` 已由 crowdsim-backend 现有 Git 排除规则忽略。

这里的 `runs` 指 `crowdsim-backend/runs`，与 `CrowdSim` 前端仓库的目录不同。后端启动时创建对话目录；前端每次保存会话后，会将会话快照同步为 JSON 文件。模型服务公布的实际 ID 为 `qwen3.8-flash-next`，页面仍显示 `Qwen3.8-Flash-Next`。

存储服务启动后将其请求日志实时输出到终端，并写入本机 `runs/dialogue_logs/service.log`（最多 5 MB，保留 3 份轮转备份）。对话正文保存在上述 JSON 文件，服务日志不重复记录正文或 API 密钥。vLLM 请求由前端开发代理直接发送到 `8800`，不会经过 `8767`。
