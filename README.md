# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：旧单记录模型的状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/ledger.py`：巨灾事件账本规则，事实校验、转换检查和容量重算（纯函数）。
- `src/repository.py`：SQLite建表、事务、账本事实与检查点读写。
- `src/service.py`：用例编排、权限检查、乐观并发、账本重算与旧数据回填。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和账本测试。

## 巨灾事件账本

再保合约（treaty）、巨灾事件与赔案（claim）接成可重算账本：

- 赔案提交、核定、结算、撤回都只向`ledger_facts`追加事实，赔案状态与容量占用是回放结果。
- 每次追加后按事件顺序（核定事实先后）重算整份合约的容量分配；容量不足的赔案进入等待队列，已有结算撤回后释放占用，等待中的赔案按剩余额度自动补入。
- 同一合约下`claim_ref`唯一、事实带幂等键，并发或重复提交只占用一次容量。
- 每次重算把投影与检查点（最后完整账本）落库；写入失败报`ledger_write_failed`，重试或读取时从检查点继续重算追平，不重复占用。
- 旧`records`数据缺赔案版本时按审计时间线回填为账本事实；时间线版本缺口、核定金额缺失或状态对不上时，赔案停在`pending_review`（待核对），不参与容量分配并计入差异。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：旧记录状态统计与账本汇总（可用容量、等待队列、差异）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

账本接口：

- `POST /api/ledger/treaties`：创建合约，请求体`{"treaty_ref","event_id","attachment","limit","cession_pct","reinstatement_pct"}`。
- `GET /api/ledger/treaties`：合约列表，含可用容量、等待数、待处理金额和差异。
- `GET /api/ledger/treaties/{id}`：合约详情，含赔案、等待队列、差异和检查点。
- `GET /api/ledger/treaties/{id}/facts`：事实时间线。
- `POST /api/ledger/treaties/{id}/claims`：提交赔案，请求体`{"claim_ref","reported_loss","idempotency_key"?}`，重复提交返回既有赔案。
- `POST /api/ledger/treaties/{id}/rebuild`：从事实重建账本（管理员）。
- `POST /api/ledger/claims/{id}/facts`：追加事实，请求体`{"fact_type":"approved|settled|withdrawn","data":{...},"idempotency_key"?}`。
- `POST /api/ledger/backfill`：按审计时间线回填旧记录（管理员）。
- `GET /api/ledger/stats`：账本汇总统计。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`underwriter`建合约，`claims_officer`提交与核定，`finance`结算与撤回，`admin`全部及回填重建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、容量分配与等待队列、结算撤回补入、并发与重复提交单次占用、写失败从检查点续算、旧数据回填与待核对。
