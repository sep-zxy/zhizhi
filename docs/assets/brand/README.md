# 知枝品牌资源

用户已于 2026-10-08 确认项目名称「知枝」，图标方向为「Git 分支长出叶片」。

- **项目名称：** 知枝
- **仓库名：** `zhizhi`，公开仓库为 <https://github.com/sep-zxy/zhizhi>。
- **拼音：** Zhizhi
- **副标题：** 从代码实践中长出自己的知识。
- **定位：** 从真实代码变化与主动探讨中提炼知识，通过理解练习，沉淀为自己的知识 Wiki。

## 图标设计

提交节点与分支对应真实开发经历；分支末端的新叶对应理解与知识成长。使用项目现有松绿色，不依赖字母或文字，小尺寸时也能保留基本轮廓。

| 资源 | 用途 |
| --- | --- |
| `icon.svg` | 应用图标原稿，512 × 512，松绿色圆角底 |
| `icon.png` | README、项目头像，512 × 512 |
| `mark.svg` | 透明底松绿色标志，文档与品牌排版 |
| `preview.png` | 图标、大中小尺寸与项目名称对照图 |
| `../../../viewer/public/favicon.svg` | 浏览器标签页图标 |
| `../../../viewer/public/icons/growth.svg` | Web/PWA 图标副本 |
| `../../../viewer/public/icons/growth-192.png` | PWA、桌面窗口与托盘图标 |
| `../../../viewer/public/icons/growth-512.png` | PWA 图标 |
| `../../../desktop/build/icon.ico` | Windows 安装包图标，含 16/24/32/48/64/128/256 尺寸 |
| `../../../desktop/build/icon.icns` | macOS 应用与安装包图标 |

主色 `#0F766E`，浅色 `#F4FAF8`，文字色 `#153B35`。SVG 是原稿，PNG 与 ICO 为派生资源；修改图形时同步更新消费者，避免各入口出现不同图标。

## 品牌与技术名称

本轮更新应用展示名称与图标。`ahadiff` Python 包、CLI、环境变量、现有 `growth-*` 资源名、Electron `appId` 和数据目录继续沿用，避免未经迁移改变运行入口或已有数据。后续开源 README 应区分「知枝」产品和 AhaDiff 分析底座，并保留上游许可证与归属说明。

Electron 将 `userData` 固定在系统应用数据目录下的 `growth-companion-desktop`，与当前使用的目录一致，避免展示名变化导致默认路径变化。

名称只做了公开检索，不代表商标或域名的可用性结论。
