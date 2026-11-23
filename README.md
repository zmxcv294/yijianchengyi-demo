# 一键成艺 · 本地演示源码

把活动文字、模板和图片整理成海报或公众号配图。前端使用 Vue 3、TypeScript 和 Vite，后端使用 FastAPI、SQLite、Pillow 与 Chromium 排版。

这是 [zmxcv294/yijianchengyi-demo](https://github.com/zmxcv294/yijianchengyi-demo) 的独立源码快照。**公开 GitHub 仓库不等于应用已公网部署**；当前入口是自行启动后的本机服务。

## 默认 Mock，哪些是真实实现

Mock 指用本地规则和占位素材演示处理流程：文字按规则提取并检查字段，图片生成抽象占位图或简单颜色变体，开发验证码直接显示在页面。默认不需要 API Key，不发送真实邮件或短信，也不调用真实大模型。

数据库保存、账号密码散列、图片解码与裁剪、事实确认、任务队列、服务端排版以及 PNG/JPG/PDF 文件下载均为实际实现。系统提供海报、公众号配图两类共 **12 个当前版本模板**，每类包含六种风格；文件中同时保留旧模板版本。

完整功能和限制见 [功能说明](docs/FEATURES.md)。 [模板社区待确认原型](docs/community-proposal/README.md) 可下载后离线试用。

2026-11-20 已同步最新本地修补：预览加载失败后重新生成可恢复显示；异常接口响应使用中文提示；草稿明确区分浏览器暂存与服务端保存，并显示暂存不可用状态。

## Windows 快速启动

准备 Python 3.12、uv、Node.js 22.18 及以上的22系列版本与 npm。首次安装依赖和 Chromium 需要联网。

```powershell
git clone https://github.com/zmxcv294/yijianchengyi-demo.git
cd yijianchengyi-demo
powershell -ExecutionPolicy Bypass -File .\scripts\start_demo.ps1
```

打开 **http://127.0.0.1:8000**。脚本按锁文件安装依赖、构建前端、补齐真实模板缩略图，然后启动本地 Mock 服务；不需要创建 `.env`。保持终端运行，按 Ctrl+C 停止，已有数据保留。

后续启动可跳过依赖安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_demo.ps1 -SkipInstall
```

端口已占用时，可加 `-Port 8001`，再打开脚本输出的新地址。Linux 和前后端分别开发的步骤见 [开发与测试](docs/DEVELOPMENT.md)。

## 演示一次完整创作

1. 进入“开始创作”，填写原文或选择完整活动示例，点击“分析需求并推荐模板”。
2. 手动选择模板，处理并检查信息；逐字段对照，补齐时间、地点等内容，确认事实。
3. 可上传 JPG/PNG，或从素材库选择图片；也可调整文字字号、颜色、坐标和图片适配。
4. 确认后生成，放大查看并下载 PNG、JPG 或 PDF。2 倍导出是普通像素放大。
5. 在“我的作品”中继续编辑、重命名、复制或删除。新稿先暂存在当前浏览器，保存或分析后才进入服务端作品列表。

匿名模式即可使用核心流程。注册使用醒目标识的开发验证码；登录时是否导入匿名作品由用户明确选择。模板和公共素材发布需要另一位审核员，管理员通过交互式命令创建，没有默认密码。

## 测试与文档

2026-11-20 的 npm 审计仍报告 **19 项依赖告警**（16 项中等、1 项高、2 项严重），生产依赖审计为 0 项；当前保留源锁文件，尚未升级依赖。详情见 [验证记录](docs/VALIDATION.md)。

本次同步在公开源码目录实际执行：前端 **21 项**、浏览器 E2E **6 项**通过，ESLint、类型检查与构建通过。后端未变，**343 项**通过是 2026-11-17 的历史验证，本轮未重复运行。实际结果见 [验证记录](docs/VALIDATION.md)。请按 [开发与测试](docs/DEVELOPMENT.md) 在自己的环境复现；本快照保留业务测试，不包含原项目的成员台账或内部验收流程。

- [功能与原需求剩余差距](docs/FEATURES.md)
- [开发、测试与故障排查](docs/DEVELOPMENT.md)
- [API 说明](docs/API.md)
- [部署准备、外部服务与数据备份](docs/DEPLOYMENT.md)
- [来源署名](NOTICE.md) 与 [源文件快照清单](PUBLICATION.json)

本次发布没有新增项目级开源许可证。中文字体的 [OFL 许可](assets/fonts/OFL.txt) 和 [上游来源](assets/fonts/SOURCE.md) 随文件保留。
