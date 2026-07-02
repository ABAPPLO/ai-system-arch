"""api-registry —— 接口元数据管理。

职责：
- 接口 CRUD
- 版本管理
- 生命周期（draft → reviewing → published → deprecated → retired）
- 发布到 APISIX（通过 etcd 下发）
"""
