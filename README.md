# 鲜切花冷链履约中枢

连接种植基地、采后处理中心、冷库与销售渠道，保存鲜切花批次的温控和交接语义。

`contracts/harvest_lot.json` 保存公开的领域样例，用来约定外部数据的名称与层级；样例不含真实个人资料、业务凭据或生产连接信息。

执行 `python3 service.py --check` 可检查服务身份，运行 `python3 -m unittest discover -s tests -v` 可核对基础契约。服务启动后，`/health` 返回项目标识。
