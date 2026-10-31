# 领域模型：周边实时路况

前端地图使用高德 JS API 的官方 `AMap.TileLayer.Traffic`，复用现有地图配置。范围支持 500 米、1 公里、2 公里；显示场地边界、道路颜色及图层读取时间。地图可见时每 180 秒刷新一次。原场地步行路网在“场地关系”页签保留。

路段数值接口：`GET /api/crowdSim/analysis-tasks/{task_id}/domain-portrait/traffic?radius=1000`。查询中心由任务 WGS84 场地边界换算为 GCJ02；上游只接收坐标、范围、查询配置及服务凭证，不发送任务名称、研究方案和人群资料。接口不会保存或修改任务。

可选的后端配置：

```dotenv
CROWDSIM_AMAP_WEB_KEY=已开通交通态势查询服务的Web服务Key
# 仅在该Key启用了数字签名时配置；不是JS安全密钥
CROWDSIM_AMAP_WEB_PRIVATE_KEY=
```

修改后重启数据服务。JS 地图 Key 与 Web 服务 Key 的类型不同；仅配置 JS Key 时可看地图，数值接口返回 `unconfigured`，不生成占位车速或拥堵比例。凭证只放后端 `.env`，不写入任务或前端。数值接口缓存 120 秒，错误缓存 30 秒，支持手动查询；上游异常返回 `unavailable`。

成功返回道路名称、方向、状态、平均车速（km/h）、GCJ02 折线和区域路况比例。`fetched_at` 是本平台获取时间；上游未给采样时间，`source_observed_at` 保持 null。没有速度的路段保持 null；空列表不表示畅通。该接口没有车辆数和步行人数。

新的多源输入默认 `transport_use: "context"`：实时路况作为当前到达条件的证据，人数估计暂由历史尺度换算和天气情景系数形成；到达时序采用明确标注的均匀假设。旧的断面模拟人数不参与该模式的总量或时序计算。原 `transport` 字段保留为旧版资料契约，不能当作实时路况。历史保存的输入未指定此字段时继续按原计数方法计算，避免改变旧版本的含义。变更领域资料后仍需点击“用于本任务实验”更新未来实验输入。

官方文档：

- [JS 实时路况图层](https://lbs.amap.com/api/javascript-api-v2/guide/layers/official-layers)
- [交通态势查询（高级 Web 服务）](https://lbs.amap.com/api/webservice/guide/api-advanced/traffic-situation-inquiry)
- [可选请求签名](https://lbs.amap.com/faq/quota-key/key/41181/)
