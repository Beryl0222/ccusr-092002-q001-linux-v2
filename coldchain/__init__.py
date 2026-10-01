"""鲜切花冷链测量治理服务。

模块划分：
- store：SQLite 持久化，追加只存的原始读数 / 判定 / 报告 / 修正曲线。
- logic：时钟漂移、修正曲线、判定引擎、签署冻结、复算、覆盖率与计划。
- api：HTTP 接口与基于令牌的角色边界。
"""

SERVICE_ID = "flower-cold-chain"
