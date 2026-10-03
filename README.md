# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

巨灾事件、再保合约和赔案已接成**可重算账本**：核定、结算和撤回都只向事件账本追加事实，
容量分配按事实顺序重新计算，合约可用容量、恢复保费和待处理金额始终可以从账本重算对齐。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费计算。
- `src/ledger.py`：账本事实、按事件顺序重算容量、旧数据回填推导（纯函数）。
- `src/repository.py`：SQLite建表、事务、事实追加与快照重算。
- `src/service.py`：用例编排、权限检查、乐观并发、事件串行化和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、账本重算和失败场景测试。

## 账本模型

- 每个巨灾事件一条只追加的事实流（`ledger_facts`）：合约绑定、赔案提交、核定、
  结算、拒绝、撤回都追加一条事实，历史事实永不修改。
- 每次追加在**同一事务**内重算事件快照（`ledger_snapshots`）：可用容量、等待队列、
  恢复保费、待处理金额。任何一步失败整体回滚，账本停留在最后一份完整快照，
  客户端用同一版本号重试即可，重复提交不会重复占用容量。
- 容量口径：事件可用容量 = Σ 已绑定合约(层宽×分保比例) − 既往累计(`aggregate_prior`)
  − 已分配摊回。按核定顺序先到先得，容量不足的赔案进入等待队列；
  已结算赔案撤回后释放额度，等待中的赔案按剩余额度自动补入。
- 同一巨灾事件的记账动作在进程内按事件串行、数据库内同事务串行，
  并发提交只能占用一次容量；同一事件下赔案号唯一（已关闭赔案可复用）。
- 等待队列中的赔案不能结算；赔案事件必须与合约巨灾事件一致。

## 旧数据回填

`POST /api/backfill`（管理员）按审计时间线为缺赔案版本的旧记录补齐账本事实：

- 时间线必须从`created`开始、赔案版本连续、状态机可解释且与当前状态一致；
- 缺创建事件、版本断档、状态对不上或提交键冲突时不臆造事实，只追加一条
  待核对事实，赔案停在`pending_review`，不参与容量分配并计入差异；
- 回填幂等，已入账的记录自动跳过；快照缺失或落后时读取端会自动从事实重建，
  也可显式调用`POST /api/events/{event_id}/rebuild`（管理员）。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数；返回`items`（每条带`allocation`）
  和`events`（每个事件的可用容量、等待队列、差异数）。
- `GET /api/records/{id}`：记录详情，带`allocation`、`event_ledger`和`discrepancies`。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/events/{event_id}/ledger`：事件账本（快照、事实流、差异列表）。
- `POST /api/events/{event_id}/rebuild`：从事实流重建事件快照（管理员）。
- `POST /api/backfill`：旧数据回填（管理员）。
- `GET /api/stats`：状态统计与账本汇总（可用容量、等待赔案数、待核对数、差异数）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  动作包括`bind`、`submit_claim`、`calculate`、`settle`、`reject`、`withdraw`
  （撤回：`claim_submitted`/`calculated`/`settled` → `withdrawn`，释放占用容量）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、等待队列与撤回补入、
并发提交容量只占一次、写入失败后从最后完整账本重试、旧数据回填与待核对。
