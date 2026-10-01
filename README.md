# 鲜切花冷链测量治理服务

面向云南鲜切花产区的冷链测量治理：把仪器型号、校准证书、时钟漂移、安装位置、
启停区间与采切批次（`contracts/harvest_lot.json`）关联，解决“合作社记录仪合格、
冷库探头与承运商设备判超温”的测量系统争议。

## 判定纪律

服务对每批花材给出三态结论：

- `compliant`：全部有效读数（含扩展不确定区间）在限值内，且有效数据覆盖率达标；
- `exceeded`：多支探头一致超温；
- `inconclusive`：**存在以下任一情况时不得直接形成履约结论**——
  校准过期（`calibration_expired`）、时钟未核查（`clock_unverified`）、
  采样缺口导致覆盖率不足（`sampling_gap` / `insufficient_coverage`）、
  多支探头偏差超过阈值且扩展不确定区间互不重叠（`probe_divergence`）、
  修正曲线标定范围外外推（`curve_out_of_range`）、限值被不确定区间跨越（marginal）。

## 不可篡改与可复算

- **原始读数只追加不覆盖**：`readings / assessments / reports / signatures /
  confirmations / correction_curves / evidence_packages` 均为追加只存表，
  SQLite 触发器拒绝 UPDATE/DELETE；读数按设备构成 SHA-256 哈希链。
- **补传双时间戳**：`device_time`（设备时间）与 `received_at`（接收时间）分别保存，
  `backfill=true` 标识补传；同批次推送幂等，同设备同设备时间的冲突重传全部留存，
  判定只采用最早接收的一行。
- **版本化修正曲线**：实验人员（`lab` 角色）通过标准温箱发布设备级 / 型号级曲线，
  版本单调递增；判定取读数时刻已发布的最高版本，外推区域不予采信。
- **签署冻结**：报告快照带规范 JSON 的 SHA-256 摘要；签署后到达的补传数据、
  事后发布的新曲线版本都不改变已签署报告。
- **争议复算**：`GET /api/lots/{lot}/reports/{report}/recompute` 只采用签署时固定的
  读数、证书、时钟核查与曲线，逐项返回当时采用的读数、排除理由、扩展不确定区间、
  各方签名与确认结果，并比对摘要。

## 角色与脱敏

| 角色 | 能力边界 |
| --- | --- |
| `admin`（行业协会） | 登记批次 / 设备 / 区段 / 安装，发起判定与签署，全量可见，生成轮换复校计划 |
| `lab`（实验人员） | 登记校准证书、时钟核查，发布版本化修正曲线，查看哈希链 |
| `carrier`（承运商） | 只能查看 / 上传**自己名下设备**的数据，只看自己负责区段 |
| `claims`（理赔） | 不能看原始数据与原始报告，只能获取**脱敏证据包** |

证据包把设备序列号、型号、承运商 / 实验室名称替换为“探头A/探头B/承运方”等假名，
假名映射表不出包，但保留结论、覆盖率、排除理由、不确定区间与签署摘要。

## 覆盖率比较与治理计划

- `GET /api/coverage`：按 `route_id / packaging / season` 比较有效数据覆盖率。
- `POST /api/plans/generate`：为证书过期 / 临期（30 天内）、从未校准、
  多探头偏差涉事、覆盖率长期低于 90%、时钟漂移 ≥20 ppm 的设备生成
  **复校**或**轮换**计划（幂等，不重复建单）。

## 运行

```bash
python3 service.py --check                 # 基础检查
python3 -m unittest discover -s tests -v   # 39 个契约 / 逻辑 / HTTP 测试
python3 service.py --db coldchain.db --seed-dev-tokens --port 8000
```

`--seed-dev-tokens` 仅用于开发，写入四枚演示令牌：
`dev-admin-token` / `dev-lab-token` / `dev-carrier-token` / `dev-claims-token`。
生产环境应通过 SQL 单独发放令牌，不使用该参数。

启动后 `GET /health` 返回项目标识；接口均以 `Authorization: Bearer <token>` 鉴权。

## 主要接口

```
POST /api/lots                    POST /api/segments         POST /api/installations
POST /api/instruments             POST /api/calibrations     POST /api/clock-drifts
POST /api/correction-curves       POST /api/readings
POST /api/lots/{lot}/assessments  POST /api/lots/{lot}/reports
POST /api/reports/{report}/sign   POST /api/reports/{report}/confirmations
GET  /api/lots/{lot}/reports/{report}/recompute
POST /api/lots/{lot}/reports/{report}/evidence-package
GET  /api/packages/{id}           GET  /api/coverage
POST /api/plans/generate          GET  /api/plans
GET  /api/instruments/{id}/chain  GET  /api/my/segments
```

## 代码结构

- `coldchain/store.py`：SQLite 持久化、追加只存触发器、读数哈希链。
- `coldchain/logic.py`：时钟漂移归一、曲线修正、判定引擎、签署冻结、争议复算、
  覆盖率比较、计划生成、证据包脱敏（不依赖 HTTP，可独立复算）。
- `coldchain/api.py`：HTTP 路由与令牌角色边界。
- `service.py`：服务入口。

`contracts/harvest_lot.json` 保存公开领域样例（含测量治理字段约定）；
样例不含真实个人资料、业务凭据或生产连接信息。
