# 凡凡和涵涵照片纪念册

原生 HTML/CSS/JavaScript + Flask。生产使用 Gunicorn/systemd、Nginx 和私有阿里云 OSS。

本仓库保存已核对的代码，不保存生产数据、私人照片/音频、数据库备份或服务器凭证。

## 运行与数据

- 入口：`app.py`；前端：`public/`。
- 运行依赖：`requirements.txt`。
- 数据：`data/photos.json`、`data/book-layout.json`、`data/music.json`、`data/comments.json`。
- 历史照片文件：`public/photos/`、`uploads/`，不纳入 Git；生产原文件继续保留。
- 环境示例：`.env.example`，真实配置只留在服务器 `/etc/photo-ring.env`。
- 不得用本地 `data/` 或 `uploads/` 覆盖生产数据。

## 版本与回滚

当前候选版本说明见 `docs/releases/v1.0.0-rc.1.md`。
Git checkout 只恢复代码；生产数据、OSS 对象及私有配置必须独立保留。

`install.sh` 仅用于首次安装。现有 `deploy-safe.sh` 没有完整纳入评论模块的安装清单；此次封板按原样保留，不应直接拿它完成评论版本部署或回滚。未来操作须按 Release 文档复核完整文件清单、数据兼容性和备份，不能直接覆盖整个生产目录。

本次封板没有执行任何部署、migration、依赖升级或服务重启。
