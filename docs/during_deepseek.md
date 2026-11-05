# 事中备用 DeepSeek 与独立启动

事中输入框提供 GLM 和 DeepSeek 两个选项。GLM 配置及共享模型菜单保持原样；事前、事后不会看到事中专用 DeepSeek。事中选择独立保存，发送、主动重试、工具链、策略生成和总览文字生成使用该选择。每条请求开始时固定模型，历史产物记录实际模型名。

在后端 `.env` 配置（不要提交 Key）：

```dotenv
CROWDSIM_DURING_DEEPSEEK_ENABLED=true
CROWDSIM_DURING_DEEPSEEK_BASE_URL=https://api.deepseek.com
CROWDSIM_DURING_DEEPSEEK_MODEL=deepseek-flash
CROWDSIM_DURING_DEEPSEEK_API_KEY=填写自己的Key
CROWDSIM_DURING_DEEPSEEK_PROXY=
CROWDSIM_DURING_DEEPSEEK_DECISION_TIMEOUT=8
```

前端仅传模型 ID。Key 由后端读取。DeepSeek 分支关闭思考并移除 GLM 扩展参数；GLM 分支原样调用原网关。失败不会自动切换供应商。

若同时运行 VPN 与代理，代理 DNS 返回的虚拟地址可能被 VPN 路由截走，表现为 DeepSeek 连接立即中断。可将 `CROWDSIM_DURING_DEEPSEEK_PROXY` 配置为本机已开启的 HTTP/HTTPS 代理地址（例如本机代理监听于 1082 时填写 `http://127.0.0.1:1082`）。通过代理建立 HTTPS 隧道时由代理解析 DeepSeek 域名，继续校验官方服务器证书；不固定上游 IP，也不修改系统 DNS。此变量仅作用于事中 DeepSeek 网关，不影响 GLM、事前、事后或仿真个体决策请求。不要填写 SOCKS 地址。

修改后退出当前后端启动器并重新运行 `./run_during.sh`，使网关加载新配置。关闭本机代理后应将此项清空并重启后端，恢复直接连接。网关会分别报告 DNS、连接、TLS、代理和超时错误，不返回密钥或上游响应正文。

## 机房停电时

先在原启动器终端按 Ctrl+C，再在后端目录、已安装依赖的 `sumo` 环境运行：

```bash
./run_during.sh --check
./run_during.sh
```

仍需单独启动前端 `npm run dev`；修改 Vite 代理后应重启前端。独立入口启动本机8765仿真、8766实验控制、8767对话存储、8768事中模型网关，不连接 MongoDB、不建立 SSH 隧道、不构造事后应用。DeepSeek 调用及在线地图仍需公网。

`--restart-services` 仅重启归属已核对的本机服务，活动实验/仿真和仍存活的旧启动器会阻止重启。遇到完整后端占用端口时，先退出原启动器；独立入口不会将完整服务替换掉。

新观察批次在首次提交时冻结所选个体决策模型，后续切换聊天模型不会改变运行中的批次。旧批次保留旧行为；新建批次才采用新选择。未选择 LLM-driven 的个体仍使用规则决策。凭据不进入实验配置。

## 恢复或停用

服务器恢复后先退出独立入口，再使用原 `./run.sh`，在事中菜单切回 GLM。正常数据服务也提供事中专用模型路由。

保留备用功能时无需删代码；将 `CROWDSIM_DURING_DEEPSEEK_ENABLED=false` 并重启后端即可禁用 DeepSeek（菜单显示不可用），不会修改已有消息、报告或原 GLM 配置。已有 DeepSeek 选择会明确报不可用，不会静默产生其他模型调用。
