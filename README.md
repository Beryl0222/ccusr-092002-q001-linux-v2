# 鲜切花冷链履约中枢

连接种植基地、采后处理中心、冷库与销售渠道，保存鲜切花批次的温控和交接语义，并为行业协会提供冷链测量治理服务：把仪器型号、校准证书、时钟漂移、安装位置和启停区间与 `contracts/harvest_lot.json` 中的采切批次关联起来，让"花材失温还是测量系统失真"可以被复算地回答。

`contracts/harvest_lot.json` 保存公开的领域样例，用来约定外部数据的名称与层级；样例不含真实个人资料、业务凭据或生产连接信息。

## 治理规则

- **只追加存储**：原始读数只追加不覆盖；同一设备同一时刻的重复上报以更大序号追加，生效视图取最新，原始记录全部保留。补传数据的设备时间与接收时间分别保存，超时到达自动标记 `late`。
- **质量门**：校准证书过期、采样缺口超限、多探头中位偏差超限或覆盖率不足时，评估结论只能是"无法判定"（`indeterminate`），不得直接形成履约结论；被排除的读数逐条记录理由。
- **不确定区间**：按设备允差、校准不确定度与修正曲线残差合成，给出扩展不确定区间；区间跨限同样不得直接下结论。
- **修正曲线版本化**：仅实验人员可基于标准温箱发布修正曲线；报告生成时锁定所采用的曲线版本、阈值与时钟漂移，新版本不回溯改变已签署报告。
- **签署与修正**：协会、承运、委托三方确认后报告生效；此后任何修正只能产生引用原摘要的新版本，已签署报告保持原样。
- **角色视图**：承运商只能查看自己负责的区段；理赔人员获得脱敏证据包（设备序列号与所有人名称不可逆脱敏）。
- **覆盖率与计划**：按路线、包装、季节比较有效数据覆盖率；按校准到期与读数排除率生成设备复校/轮换计划。
- **争议复算**：抽取任一报告可按固化的参数与曲线版本重放，比对签署摘要，列出当时采用的读数、排除理由、不确定区间与各方确认结果；签署后的补传数据会导致摘要不一致并被显式列出。

## 运行

```bash
python3 service.py --check                 # 基础检查
python3 service.py --port 8000             # 启动服务（--data file.jsonl 可持久化）
python3 -m unittest discover -s tests -v   # 核对基础契约与治理规则
```

服务启动后，`/health` 返回项目标识。主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/lots` `/devices` `/calibrations` `/drifts` `/bindings` | 登记批次、设备、校准证书、时钟漂移、区段绑定（启停区间） |
| POST | `/readings` | 追加读数（设备时间/接收时间分别保存） |
| POST | `/curves` | 发布修正曲线（仅 `actor_role=lab`） |
| POST | `/assessments` | 评估批次区段并生成未签署报告 |
| POST | `/reports/{id}/confirm` | 一方确认（association/carrier/consignor） |
| POST | `/reports/{id}/amend` | 对已签署报告发起修正，产生新版本 |
| GET | `/reports/{id}` `/reports/{id}/status` | 报告与签署状态 |
| GET | `/reports/{id}/evidence` | 理赔脱敏证据包 |
| GET | `/lots/{id}/carrier-view?carrier_id=…` | 承运商区段视图 |
| GET | `/analytics/coverage` | 路线×包装×季节有效覆盖率 |
| GET | `/maintenance/plan` | 设备复校/轮换计划 |
| GET | `/disputes/{id}/recompute` | 争议批次复算与摘要比对 |

## 代码结构

- `coldchain/model.py` — 领域模型与时间工具
- `coldchain/store.py` — 只追加 JSONL 存储
- `coldchain/registry.py` — 设备、校准、漂移、绑定登记
- `coldchain/ingest.py` — 读数接入（追加、补传、生效视图）
- `coldchain/corrections.py` — 版本化修正曲线
- `coldchain/assessment.py` — 质量门、不确定区间与报告摘要
- `coldchain/reports.py` — 签署、确认与修正版本
- `coldchain/access.py` — 承运商视图与理赔脱敏证据包
- `coldchain/analytics.py` — 覆盖率分析与轮换/复校计划
- `coldchain/disputes.py` — 争议复算
- `coldchain/api.py` — HTTP 接口
