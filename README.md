# Google Flow 会员渠道网关

## 1. 用途

Video 和 Image 领域服务通过执行监听调用组织绑定的 Google Flow 账号。管理页由 Studio 代理加载。网关使用本地 MCP、持久 SQLite 队列和经过校验的媒体缓存。正式 Artifact 由领域服务写入。

## 2. 安装与离线测试

从本目录运行，使用 Python 3.11 或更高版本，并将 FFmpeg 的 `ffmpeg` 和 `ffprobe` 加入 PATH。测试使用隔离数据库、进程内和环回 MCP 服务、假时钟。三项 Video Artifact 响应丢失测试只读加载相邻工作区的 [Video 发布器](../verdantflare-app-video/services/video-mcp-server/app/artifact_output.py)，并用内存传输代替 Artifact 服务。

```text
python -m pip install .
python -m pip check
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

从空输出目录构建管理包。启动命令供配置好 GPU 调度器、持久卷和受管桌面控制程序的运行环境使用。

```text
python -m google_flow_gateway.dashboard --source dashboard --output dist/dashboard
python -m google_flow_gateway.runtime
```

## 3. 监听与调用

- `8000` 提供执行接口。可信领域代理先核验项目写权限，从验证后的上下文注入 `X-Organization-Id`、`X-Project-Id`、`X-User-Id`、`X-Project-Write` 和内部 Bearer。代理必须覆盖外部来值。
- `POST /v1/calls` 接收 `idempotency_key`、`capability`（image 或 video）、`model`、`parameters` 和可选整数 `budget_limit`。`POST /v1/jobs` 接收相同的生成参数，用 `request_id` 标识同一调用。按相同主体及原键查询状态。媒体输入通过 `PUT /v1/inputs` 传字节、Content-Type 和 X-Content-SHA256，再在 `parameters.reference_images` 中引用同主体的摘要。
- `8001` 提供管理接口和带摘要的 Dashboard 资源。Studio 管理代理注入独立 Bearer、用户、组织和 Station 的可信上下文。
- `18000` 是 gflow 的环回 MCP 监听。daemon 令牌由网关按进程生成。授权失效时由管理员在 Studio 桌面恢复会话。

## 4. 环境变量

- `GOOGLE_FLOW_INTERNAL_TOKEN`、`GOOGLE_FLOW_MANAGEMENT_TOKEN` 为独立的服务令牌。`GOOGLE_FLOW_ORGANIZATION_ID`、`GOOGLE_FLOW_STATION_ID` 标识绑定范围。
- `GOOGLE_FLOW_STATE_DIR` 存放队列、审计、发现状态和已验证缓存。`GFLOW_CLI_HOME` 存放 gflow 自有状态，`GFLOW_CLI_OUTPUT_DIR` 存放 gflow 输出。`GOOGLE_FLOW_DASHBOARD_DIR` 指向已构建的管理包。
- `GOOGLE_FLOW_CACHE_RETENTION_HOURS` 为已验证结果缓存保留时间，默认 72 小时。
- `GOOGLE_FLOW_MODEL_CREDITS` 为模型到单任务整数报价的 JSON 对象。缺少模型报价时，使用请求中的单笔预留上限。上游返回已生成媒体但缺少实际扣费时，媒体可读取，实际扣费显示未知并保留预算。
- `GOOGLE_FLOW_SCHEDULER_URL`、`GOOGLE_FLOW_SCHEDULER_TOKEN`、`GOOGLE_FLOW_PEAK_BUDGET_MIB` 配置 GPU 调度服务与显存预留。`GOOGLE_FLOW_POD_UID`、`GOOGLE_FLOW_CONTAINER_ID` 由运行环境注入。`GOOGLE_FLOW_LEASE_RECONCILE_TIMEOUT_MINUTES` 是旧租约对账上限，默认 30 分钟。
- `GOOGLE_FLOW_DESKTOP_CONTROL` 指向镜像提供的受管桌面程序，支持 start、stop-and-wait-for-zero-gpu-processes、status、open、close、login。停止命令成功表示已核实 GPU 进程归零。后三个交互操作及 status 返回 JSON。`GOOGLE_FLOW_TURN_URLS` 与 `GOOGLE_FLOW_TURN_SECRET` 用于短期 relay 凭据。
- `CHANNEL_DISCOVERY_ETCD_URL`、`CHANNEL_DISCOVERY_SCOPE_ID`、`CHANNEL_EXECUTION_URL`、`ETCD_USERNAME` 与 `ETCD_PASSWORD` 配置渠道发现。内部网络客户端使用直连配置。

## 5. 设计与验收入口

- [开发指南](../docs/design/app/google-flow/details/google-flow.development-guide.md)
- [技术白皮书](../docs/design/app/google-flow/details/google-flow.technical-whitepaper.md)
- [锁卷验收 S1](../docs/acceptance/accept_20261117_google-flow-mcp.md)
- [T3.2 执行记录](../docs/changes/change_20261119_build-google-flow-gateway.md)
