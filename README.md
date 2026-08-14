# Web3 空投雷达（终极版）

基于异步并发、多数据源聚合的 Telegram 空投监控机器人。

## 快速部署
1. Fork 或克隆本仓库。
2. 在 Render 创建 Web Service，连接仓库，设置 Root Directory 为 `render-app`（重要！）。
3. 添加环境变量：`TG_BOT_TOKEN`、`TG_CHAT_ID`、`RENDER_EXTERNAL_URL`。
4. 在 GitHub Secrets 中添加相同的 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`。
5. 部署后，在 Telegram 中向 Bot 发送 `/scan` 测试。

## 自动采集
GitHub Actions 每5分钟自动运行，无需额外操作。

## 命令
- `/scan` – 手动扫描并推送
- `/status` – 查看已推送项目总数