# 本地服务发现目录（svcdir）

一个运行在 Flask + SQLite 上的小型服务发现目录：运维人员可以在网页上修改
「服务名 → 目标地址」的映射、创建命名备份、并从一个备份恢复。另有 `/resolve`
查询 API 供客户端使用。网页还支持对单个服务当前发布的 `host:port` 发起后台
连通性核验（限时 2 秒的 `HTTP GET /healthz`）。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

- 网页：<http://localhost:8000/>
- API：`GET http://localhost:8000/resolve?name=orders-api`
- 健康检查：`GET /healthz`
- SQLite 数据与备份都位于命名卷 `svcdir-data` 的 `/data/directory.sqlite3`
  （备份是同一数据库里的版本快照，所以备份与恢复天然原子）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
SVCDIR_DB=./data/directory.sqlite3 python3 -m svcdir   # 开发用内置服务器
# 或： gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 svcdir.__main__:app
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVCDIR_DB` | `data/directory.sqlite3` | SQLite 文件路径 |
| `SVCDIR_RESTORE_LEASE_SECONDS` | `30` | 恢复租约时长（进程异常死亡后的兜底回收上限） |
| `SVCDIR_PROBE_WORKERS` | `8` | 后台连通性核验线程池大小（1-32） |
| `PORT` | `8000` | 仅用于 `python -m svcdir` |

## 页面与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页：目录、修改表单、备份列表、恢复按钮 |
| GET | `/resolve?name=<服务名>` | 返回单个服务的地址 |
| GET | `/resolve` | 返回整个当前目录 |
| GET | `/api/state` | 当前版本、完整目录、恢复状态、备份列表与每服务最新核验（页面轮询此接口） |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/services` | `{"service": ..., "address": ..., "note": ...}` 新增/修改 |
| DELETE | `/api/services/<名>` | 删除 |
| POST | `/api/backups` | `{"name": ...}` 创建命名备份 |
| GET | `/api/backups/<名>/validate` | 校验备份（不修改目录） |
| POST | `/api/backups/<名>/restore` | 从备份恢复 |
| POST | `/api/services/<名>/probes` | 对当前发布地址发起一次后台核验（202，立即返回任务） |
| GET | `/api/services/<名>/probes/latest` | 该服务最新一次核验记录 |
| GET | `/api/probes` | 每个服务最新一条核验记录 |

浏览器表单走 `POST /services`、`/backups`、`/backups/<名>/restore`，
成功后 303 跳回页面并显示结果或错误。核验由页面上的「核验」按钮通过
`fetch` 调用上述 JSON 接口发起，不触发表单跳转。

查询响应示例：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "orders-api", "address": "10.0.0.12:8080"}
```

## 一致性与恢复语义

实现要点（对应需求逐条）：

- **备份是同一提交点的完整目录。** 目录从不原地修改：每次修改都写入一个
  *完整的新版本*（先复制当前版本的全部行，再改一行），最后一条
  `UPDATE meta SET value=<新版本>` 才是发布。备份行只固定「版本号」，因此
  创建备份期间持续写入不会让备份变成半个目录。
- **恢复前校验。** 恢复分为三步：①在一个写事务里登记「恢复中」租约；②只读
  事务里校验备份（条目数、每行服务名/地址合法性、重复服务名、SHA-256 摘要与
  备份记录比对）；③一个原子写事务创建完整的新版本并切换 `current_version`。
  校验失败 → 直接返回 422，目录一个字节都不动。
- **恢复期间查询仍返回当前完整目录。** `current_version` 只在最后一步切换，
  查询永远读它；整个查询在读事务快照里完成，因此不会读到混排的数据。
- **新修改明确拒绝，已受理的修改先结算。** 登记恢复意图需要写锁，所有已受理
  的修改必然先提交完成；之后到达的修改看到有效租约，返回 `409
  restore_in_progress`（网页也会明确提示）。
- **恢复后新查询只见备份地址；先前受理的查询不混用两版。** 切换后新查询用新
  版本；一个在切换前开始的查询持有自己的读快照（`store.read_view()`），
  即使内部重复读取也仍是同一版本。
- **切换中断后重启只呈现原版或完整备份版。** 新版本与版本号切换在同一事务内
  提交；崩在提交前 → 仍是原版，崩在提交后 → 是完整备份版，旧版本的行永不删除。
  恢复者用租约 + 操作系统文件锁（`<db>.restore.lock`）标记存活：进程死亡时
  内核释放锁，重启后立即回收被遗弃的「恢复中」标记，不必等租约到期。
- **并发恢复。** 文件锁 + 数据库内的 `restore_state` 双重互斥，第二个恢复请求
  得到 409。

## 连通性核验语义

网页上每个服务行都有「核验」按钮。实现要点（对应需求逐条）：

- **后台限时 2 秒的 `GET /healthz`。** `POST /api/services/<名>/probes` 只在
  SQLite 的 `probes` 表里登记一条 `running` 任务便立即返回 `202`；真正的 HTTP
  请求由有界线程池（`SVCDIR_PROBE_WORKERS`，默认 8）在后台执行。核验期间目录
  编辑、备份/恢复与 `/resolve` 查询照常可用，互不阻塞。
- **结果五分类。** 每条记录展示所测地址、发起核验时的目录版本，以及：
  `ok_200`（HTTP 200）、`bad_status`（非 200，记录实际状态码）、
  `connect_failed`（DNS/连接/协议失败）、`timeout`（连接或响应超过 2 秒）、
  `unprobeable`（地址不是可探测的 `host:port`，例如带协议、路径、缺端口、
  端口越界等）。2 秒是**整个请求**（建连 + 发请求 + 收响应头）的硬上限，
  而不是单个 socket 操作的超时。
- **改址/删后重建/恢复后旧结果只能显示为过期。** 每条核验在创建时固定当时的
  目录版本号与地址字符串；读取时只要当前版本号已变化（任何编辑、删除后重建、
  备份恢复都会产生新版本），或该服务当前地址与所测地址不一致，就标记
  `fresh=false`，页面显示「已过期」。即使重建后地址字符串完全相同，旧结果也
  不再代表当前状态。
- **连续核验只展示最新任务。** 每次核验都是 `probes` 表里独立的一行（自增 id）；
  列表接口按服务取 `MAX(id)`。较早发起的任务无论先后完成，都只更新自己那一行，
  永远无法覆盖更新任务的结果。
- **进程重启：未完成标中断，已完成可查看。** 核验线程是进程内资源，进程死亡后
  不可能再写库；启动时 `initialize()` 把所有遗留的 `running` 行一次性改写为
  `interrupted`（带 `finished_at`）。已完成的终端结果原样保留，仍可通过
  `/api/probes`、按 id 查询或页面查看。旧进程的「僵尸」工作线程若在重启后尝试
  回写，会因该行已不是 `running` 而收到 409，不能复活中断记录。
- **不影响选址。** 核验结果只写独立的 `probes` 表，从不修改 `entries` 或
  `current_version`；`/resolve` 的响应不含任何核验字段，选址行为与核验完全无关。
- **历史有界。** 每个服务仅保留最近 50 条核验记录（`PROBE_HISTORY_PER_SERVICE`），
  超出部分在新建任务时于同一事务内删除。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

目录/备份/恢复的关键交错场景覆盖在 `tests/test_svcdir.py`：

- `test_writes_rejected_while_restore_in_progress_and_settled_before`
- `test_query_accepted_before_switch_keeps_one_version`
- `test_concurrent_writers_during_restore_never_mix`
- `test_cross_process_readers_and_writer_during_restore`（独立进程 + WAL 快照）
- `test_corrupt_backup_is_rejected_and_directory_unchanged`
- `test_backup_with_missing_rows_is_rejected`
- `test_interrupted_switch_presents_original_or_complete_backup`
- `test_killed_restorer_leaves_original_and_restart_can_retry`（SIGKILL 恢复进程）
- `test_expired_restore_lease_is_reclaimed_after_restart`
- `test_dead_restorer_is_reclaimed_immediately_on_restart`
- `test_restore_can_run_after_prior_abandoned_marker`
- `test_live_restore_lease_survives_sibling_process_initialization`
- `test_backup_row_pins_one_version_during_continuous_writes`
- `test_http_rejects_writes_and_serves_reads_during_restore`
- `test_api_flow` / `test_html_page_and_forms` / `test_html_escapes_hostile_names`
- `test_api_validation_errors_are_json` / `test_edge_case_inputs_are_rejected_cleanly`

连通性核验测试在 `tests/test_probes.py`，覆盖：

- `test_probe_200` / `test_probe_non_200` / `test_probe_connect_failure`
  / `test_probe_timeout_is_capped_at_two_seconds`
  / `test_probe_unprobeable_address_is_a_result_not_an_error`
- `test_parse_rejects_unprobeable`（地址不可探测的各种形态）
- `test_probe_goes_stale_after_address_change`
- `test_probe_goes_stale_even_when_address_is_republished_identical`
- `test_probe_goes_stale_after_delete_and_recreate`
- `test_probe_goes_stale_after_restore`
- `test_only_latest_probe_per_service_is_listed`（乱序完成，旧任务不覆盖新任务）
- `test_manager_slow_older_task_never_overwrites_newer`（真实线程下的最新优先）
- `test_running_probes_marked_interrupted_on_restart`
- `test_late_worker_write_after_restart_does_not_resurrect_interrupted`
- `test_probes_allowed_during_restore_and_do_not_block_operations`
- `test_http_start_probe_returns_202_and_background_result`
- `test_http_directory_operations_stay_available_while_probe_running`
- `test_probe_result_does_not_change_resolve`
- `test_html_page_renders_probe_controls_and_keeps_csp`

## 布局

```
svcdir/errors.py     错误类型 → HTTP 状态码
svcdir/store.py      目录版本、命名备份、校验、恢复、核验记录持久化（核心逻辑）
svcdir/probing.py    地址可探测性解析、2 秒 GET /healthz、后台线程池 ProbeManager
svcdir/web.py        Flask 路由：网页 + API（含核验接口）
svcdir/templates/    页面模板
svcdir/static/       app.js（核验按钮与轮询渲染）、style.css
tests/test_svcdir.py 目录/备份/恢复自测（含并发交错）
tests/test_probes.py 连通性核验自测（含真实本地 HTTP 服务与线程交错）
Dockerfile / docker-compose.yml
```

## 已知限制与未完成项

- **未在 Docker 中实际构建/运行。** 本环境没有 Docker（`docker` 命令不存在），
  因此 Dockerfile 与 compose 文件只做了静态检查。应用本身已用
  `python -m svcdir` 与 gunicorn 在真实 HTTP 上跑通。
- 单容器部署，恢复互斥依赖同一文件系统上的 `flock`；若把数据目录放到不支持
  `flock` 的网络文件系统，会退化为「租约到期才算失效」。
- 备份保存在同一 SQLite 数据库中，可防止误改目录，但不能防磁盘/文件损坏；
  需要异地容灾应另行导出备份文件。
- 未做用户认证与 HTTPS——按需求这是本地运维工具，请只在内网使用。核验请求由
  服务端主动发往目录中登记的地址，请勿让不可信人员写入地址（SSRF 面）。
- 核验只探测明文 HTTP 的 `GET /healthz`，不支持 HTTPS、自定义路径或主机名
  快照；地址必须是可解析的 `host:port`，否则记录为「地址不可探测」。
- 恢复的「已受理查询」保证依赖单个查询请求只在一个读事务里读完（当前实现如此）；
  没有为客户端提供跨多个独立 HTTP 请求的版本固定参数。
