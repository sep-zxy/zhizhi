# 安全政策

## 支持范围

优先处理最新知枝桌面版本和 `main` 中的问题。模型服务、同步云及用户自建的 ReMe / CodeGraph 服务分别由对应部署方维护。

## 报告安全问题

请通过仓库 [Security → Advisories → Report a vulnerability](https://github.com/sep-zxy/zhizhi/security/advisories/new) 私下报告，包含受影响版本、复现条件、影响范围和最小复现材料。

不要在公开 Issue 中披露可利用细节、API 密钥、私人代码、工作区数据库或用户数据。提交日志前先脱敏。

## 数据与边界

默认工作区保存在本地；调用远程模型或启用同步后，相应内容会发送给已配置的服务。请审阅授权范围和来源，定期备份工作区与 Obsidian 库。

应用的本机服务只监听 loopback，桌面 renderer 保持沙箱与禁用 Node 集成。源码仓库不携带用户凭证或运行数据。
