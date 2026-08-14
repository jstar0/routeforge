# RouteForge

**Plan electric collection routes under battery, charging, time-window, and two-compartment constraints, and get the diagnostics that show why a plan is or is not feasible.**

RouteForge models two independent payload compartments, load-dependent energy use, charging stations, and service windows, and searches with a constraint-driven ant-colony metaheuristic. Every result carries the route, cost decomposition, convergence history, and explicit constraint diagnostics, so an infeasible plan explains itself instead of disappearing.

## 本地 API 工作台后端

先构建工作台客户端，再从同源启动 API 和前端：

```bash
cd web && pnpm install --frozen-lockfile && pnpm build
cd .. && uv run routeforge serve --host 127.0.0.1 --port 5203 --runtime-dir runtime/routeforge
```

`POST /api/v1/scenarios` 创建不可变场景 revision；`PATCH /api/v1/scenarios/{id}` 必须带
`If-Match`。`POST /api/v1/solve-runs` 返回 `202` 与 job id，随后通过
`GET /api/v1/solve-runs/{id}` 查询，或连接
`GET /api/v1/solve-runs/{id}/events` 接收可重放 SSE（`Last-Event-ID` 可断线续读）。
`POST /api/v1/solve-runs/{id}/cancel` 会在当前完整 ACO iteration 后停下，并保存
`complete=false` 的最后完整结果；重启服务遗留的运行任务标为 `interrupted`。每个完成或取消
的结果都登记为可下载 JSON artifact，供后续工作台和导出流程使用。

这是平面距离实验工具，不是道路导航或生产调度。为使取消在交互中可预期，API 会拒绝超出
本地工作量预算的 customer × ants × iterations 组合，并提示应缩小哪一项。

工作台同源服务于根路径 `/`；它支持点位和车辆约束编辑、JSON 导入、草稿验证、revision 保存、
滚轮缩放/平移和移动端“点位 / 约束 / 方案”页面。编辑中的草稿不会改写既有方案；保存后才生成
新 revision，冲突时可保留当前草稿并另存副本。

生产客户端构建输出到 Python 包的 `static/` 目录并进入 wheel，所以独立安装后的
`routeforge serve` 不依赖旁边的 `web/` 源目录。

![固定 seed 的优化方案与 greedy 基线叠加图](docs/images/routeforge-overlay.png)

实线表示 ACO 优化路线，虚线表示同一次求解生成的 greedy 基线；同一车辆在两层中使用相同颜色。截图来自 12 客户合成场景，数字可由 `uv run routeforge demo --seed 17` 复现，不代表道路导航或真实车队结果。

The solver and evaluator use only the Python standard library. The local workbench adds the declared FastAPI and uvicorn dependencies; `uv sync` installs them without a solver service, broker, database server, or hidden dataset.
