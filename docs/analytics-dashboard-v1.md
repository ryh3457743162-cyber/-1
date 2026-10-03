# ZMF&RYH 访问统计 Dashboard v1

状态：本地开发与回归，未部署生产。日期：2026-10-03。

## 基线与边界

- 基线 tag：`v1.2.0`；commit：`5830cec11d79d03f36e6b0981f5c1e409179bb26`。
- 分支：`feature/analytics-dashboard`。没有合并其他功能分支，没有移动已有 tag。
- 本地初始工作树 clean。未被生产 tag 包含的分支尖端仅发现品牌维护分支的 `a42c16d`（v1.1.1 封板文档）和 `docs/release-v1.2.0` 的封板文档提交；均未合并到本功能。原本处于 `docs/release-v1.2.0`（文档提交 `3707bfe`），开发分支从生产 tag 创建。
- 生产不是 Git checkout。只读核验：封板清单中的 18 个运行文件 SHA256 全部匹配以上 commit。
- 实际服务：`/etc/systemd/system/photo-ring.service`；Gunicorn 两个 worker、`127.0.0.1:8000`；nginx 代理到该 upstream 并保留 Host。
- 持久化目录：`/opt/photo-ring/data`；现有 `photos.json`、`book-layout.json`、`book-cover.json`、`comments.json`、`music.json` 不改结构。
- 已存在并保留生产封板备份：`/root/photo-ring-backups/v1.2.0-freeze-20261003T062154Z`。本轮未创建或改写生产备份。
- 禁止从本地夹具覆盖生产 data；本轮无生产部署、生产统计写入、migration、OSS、nginx、HTTPS 或证书改动。

## 页面与 API

后台导航增加「访问统计」，入口 `/admin/analytics`，沿用现有管理密码和 session。

| 接口 | 权限 | 用途 |
| --- | --- | --- |
| POST `/api/analytics/pageview` | 公开，严格校验和限流 | 接收有效模式浏览事件 |
| GET `/api/manage/analytics/summary?days=7` | 管理员 | 四个卡片、7/30 天趋势、热门模式、设备/浏览器/系统分布、应用异常 |
| GET `/api/manage/analytics/visits?page=1` | 管理员 | 最近 30 天访问，固定 20 条分页 |

静态脚本通过 Flask 的显式路由提供：`/analytics.js`、`/analytics-admin.js`。没有开放整个 data 目录。
页面 HTML 只含登录壳；普通访客读上述管理员数据接口返回 401。

## 采集与口径

前台是同一 URL 下的 vanilla JavaScript 模式切换。使用小型独立脚本，显式接入启动页开关、首次进入和 `switchView`；没有资源请求推算或 MutationObserver。

- 页面白名单：`startup`、`atlas`、`flat`、`book`。
- 初次打开启动页一次 PV；进入默认书本再计一次书本 PV。启动页重新打开和真实模式切换属于有效浏览。
- 同一模式重复点击/脚本重复初始化不重复计数。刷新是新的浏览。失焦再恢复不统计新 PV。
- 打开照片、大图、评论、书本封面翻开、正文翻页、年份切换、预取、静态资源、API、健康检查不计 PV。
- 管理后台没有采集脚本；已登录管理员即使浏览公开页也不计普通 PV。识别到的常见 bot/spider/crawler/headless UA 不计 PV。
- 今日及趋势按 Asia/Shanghai 自然日。实现使用固定 UTC+08:00，适用于当前中国民用时间，无夏令时。
- 同日同一匿名浏览器多次浏览：PV 累加，UV 只加一。每日 UV 不是自然人数，也不是跨日去重人数。
- 设备、浏览器、系统及热门页面排行按 PV 统计；无法识别使用未知/其他。UA 分类属于近似，iPad 桌面 UA 等可能误分类。
- 应用异常：Flask 返回的 404 和 5xx；不计正常 400/401 等验证/鉴权失败，不计统计接口自身失败。不覆盖 nginx、OSS、浏览器 Console 或 TLS 错误。不保存异常 URL、查询参数或异常正文。

## 隐私与安全

浏览器用 `crypto.randomUUID` / `getRandomValues` 生成独立随机 ID，存储键 `zmfAnalyticsDay` 每个北京时间自然日替换。不访问评论 token。存储不可用时使用页面会话内随机 ID；无安全随机源则不采集。

服务端保存 HMAC-SHA256，每日日期和用途加入摘要，访客 ID 与事件 ID 分开处理。无客户端原始标识、完整 IP、完整 UA、URL/query、Referer、地理位置、设备指纹或个人身份落库。后台只展示访客摘要前 12 位，日期隔离。

独立 HMAC 密钥来自安全环境变量，不进仓库、不返回客户端、不打日志。原始 IP 只在当前请求内用于连接来源限流摘要，限流表保留约数小时至下一次清理；不存 IP 明文。不信任 X-Forwarded-For，也不依靠 IP 计算 UV。

字段必须恰好是 `page/date/visitorId/eventId`；UUID v4、真实 ISO 日期、当前自然日检查；单个请求最多 1024 字节。页面类型不接受任意 URL。来源校验按 Host authority，兼容 nginx HTTPS → 内部 HTTP；不采用未经信任的 Forwarded headers。

重复事件以当日事件摘要唯一约束抑制。限流在 SQLite 写事务内，多个 Gunicorn worker 共用：

- 匿名访客 120 次/小时；
- 连接来源 10000 次/小时（代理后共享，额度不低于每日全站额度，避免过早漏计）；
- 全站 10000 次/自然日。

达到上限返回 429，数据可能漏计，业务页面不受影响。防刷量不能完全区分伪装访客；没有宣称抗 DDoS 或精确自然人识别。

## SQLite 数据结构与保留

默认 `data/analytics.sqlite3`，可用 `ANALYTICS_DATABASE` 指定非公开持久化路径。仅使用 Python 标准库，不增加第三方数据库或图表依赖。

| 表 | 内容 | 保留 |
| --- | --- | --- |
| visits | 时间、页面、当日访客/事件摘要、设备分类 | 含今天 30 个自然日 |
| daily | 每日 PV/UV | 含今天 365 天 |
| dimensions | 每日模式、设备、浏览器、系统 PV | 365 天 |
| errors | 日期、HTTP 状态、数量 | 365 天 |
| visitors | 当日精确 UV 去重摘要集合 | 今天和昨天 |
| limits | 临时限流计数和摘要 | 清理时淘汰旧小时/旧日 |
| maintenance | 最后清理日期 | 少量控制记录 |

`user_version=1`，`CREATE TABLE/INDEX IF NOT EXISTS`，可重复初始化。遇到未知 schema 版本拒绝写入，不重建数据。
每次操作独立连接，WAL、`busy_timeout=150ms`、参数化 SQL、`BEGIN IMMEDIATE` 原子更新访问/去重/汇总；读多项指标使用同一快照。
数据库不可写时页面采集返回安全 202（accepted=false），后台提供错误状态；不泄露异常堆栈，日志最多每分钟一条通用警告。公开采集 fetch 不等待渲染，有 2 秒超时；普通失败不重试，跨午夜最多补一次。

当天首次有效写入触发清理。为保证网站完全无访问时也能清理，另提供**尚未安装**的每日 systemd maintenance 模板。它仅清理统计数据库，没有照片自动清理或回收站任务。
备份采用 SQLite Online Backup API，包含 WAL 的一致快照；禁止把正在写入的 `.sqlite3` 主文件孤立复制当作完整备份。

官方依据：[SQLite WAL](https://www.sqlite.org/wal.html)、[SQLite 在线备份](https://www.sqlite.org/backup.html)、[Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)。

## 配置

`.env.example` 增加：

```dotenv
ANALYTICS_ENABLED=0
ANALYTICS_HMAC_KEY=
# 可选，默认在项目 data 目录
# ANALYTICS_DATABASE=/opt/photo-ring/data/analytics.sqlite3
```

生产启用需要将开关设为 1，并在现有安全服务器配置中创建独立、高随机度、至少 32 字符密钥。默认关闭且没有合格密钥时不创建统计数据库。
不要重复使用其他业务身份、将真实密钥放入 `.env.example` 或复制本地统计 DB 到生产。密钥变化会改变当天去重摘要，应避免频繁轮换，并记入维护记录。

## 本地测试

完整 Python 回归 **95 passed / 0 failed / 0 errors**（既有 70 项 + 本次 25 项）。
JavaScript **12 passed / 0 failed**（本次采集 8 项 + 既有照片日期 4 项）。

覆盖首次 PV、同日 UV、北京午夜、SPA 模式、重复事件、管理员/API/静态排除、非法日期和超大请求、权限、机器人、跨站来源、nginx HTTPS Origin、三个独立进程并发、真实 SQLite 写锁、故障降级、30/365 天清理、趋势和分页、WAL 一致备份、幂等初始化、未知 schema 拒绝。

浏览器：实际 CSS viewport 1440、390、375；有/无访问数据；7/30 天、PV/UV、圆点日期提示、分页、刷新、管理会话过期、阻断统计请求后的中文错误/恢复。
联合操作：启动页/图集/平面/书本/封面；音乐暂停/继续、模式切换、一个 audio；评论提交与审核；照片元数据保存；照片回收站软删除/恢复；后台排版/封面/音乐/评论/回收站页面。
ICP/公安 Footer 保持正常流；未改 Footer CSS。375/390 的文档未发现横向溢出；最近记录可在自身容器横向滚动查看列。

没有真实 iPhone/Android 或旧学校机房电脑验收；仅浏览器尺寸模拟。未模拟生产 nginx 层流量错误。故障注入的网络拒绝属于预期结果；恢复后无新增网站脚本错误。
截图与隔离夹具保存在本地工作区 `analytics-work`，截图含合成访问数据，不含生产统计。

## 后续部署步骤（本轮未执行）

1. 等待用户明确部署授权，复核生产仍为预期版本及 18 个文件哈希，检查分支差异和测试。
2. 按已有流程完整备份所有业务 JSON/事务记录、校验 JSON 与 SHA256，保留 v1.2.0 回滚点；不得覆盖线上 data。
3. 仅部署本分支运行文件：新增 Python 模块、采集/admin 静态文件、app 注册和导航变更；不修改 nginx/HTTPS/OSS/照片文件。
4. 在安全环境配置中设置统计专用开关和 HMAC 密钥；确认 `photo-ring` 用户可写 data。默认路径兼容既有 systemd `ReadWritePaths`。
5. 用既有 venv 执行幂等初始化：

   ```sh
   python scripts/analytics_maintenance.py init --database /opt/photo-ring/data/analytics.sqlite3
   ```

   这只初始化新的独立统计库，不是照片/评论 JSON migration。
6. 按既有实际服务 `photo-ring` 更新应用并检查域名、脚本加载、匿名事件 202、管理员 API 200、未登录 401、业务健康和日志。
7. 如果采用默认 DB 路径，经部署授权后安装 `scripts/photo-ring-analytics-cleanup.service/.timer` 到 `/etc/systemd/system`，检查 `photo-ring` 用户、systemd 支持 `ExecCondition`、命令路径及备份；仅启用这一项每日统计清理，不重复建 cron。使用自定义 DB 路径时需同步模板路径与沙箱写目录。
8. 生产只采集真实浏览，不导入合成记录；重新测 1440/390/375 和真实手机。完成后再讨论版本封板。

## 备份与回滚

后续 SQLite 备份示例（本轮只在本地合成库验证）：

```sh
python scripts/analytics_maintenance.py backup \
  --database /opt/photo-ring/data/analytics.sqlite3 \
  --output /root/photo-ring-backups/analytics-DATETIME/analytics.sqlite3
```

输出包含路径、大小、SHA256、`integrity_check=ok`。拒绝覆盖已有备份文件或写入 public。备份目录需 0700，文件及校验清单 0600，并确保备份执行用户有目录权限；不提交 Git。

异常时优先在安全配置关闭统计（需必要应用 reload），并停止新增统计 cleanup timer。业务 JSON 未变，可按已有部署流程恢复 v1.2.0 运行文件；不回滚/清空业务 JSON，不删 OSS。
独立 SQLite 文件保留到非公开目录；如需恢复统计数据，停止统计写入后从验证的在线备份恢复，并处理该统计库的 WAL/SHM，绝不能将旧主文件覆盖到仍写入中的数据库。代码回滚不会恢复已经按保留政策清理的匿名原始记录。
不移动历史 tag，不 force push，不创建本次正式版本。

## 生产部署前复核补充

共享反向代理来源额度提高到每小时 10000，保持访客每小时 120 和每日全站 10000 不变。Dashboard 展示今日采集量、上限是否触及、限流拒收次数及分类；仅记录汇总，不增加身份数据。新增两项回归测试，未修改业务 JSON 或 schema。
