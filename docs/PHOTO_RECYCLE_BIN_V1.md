# 照片回收站 v1：本地实现与验证

日期：2026-09-28。分支：`feature/photo-recycle-bin`。基于 `v1.0.0-rc.2`，基准提交 `e9b422c4810b571a20961b2eba74edb7b708c176`。

本轮仅本地开发与测试。未部署，未修改生产数据库，未删除生产 OSS 文件。没有定时任务或自动过期清理。

## 真实存储结构与增量字段

项目使用 JSON 文件，不是 SQL/ORM。照片上传记录仍在 `data/photos.json.photos`，评论仍在 `data/comments.json`，书本仍在 `data/book-layout.json`。

- 上传照片新增 `deletedAt`，正常为 `null`，回收站为 UTC ISO 时间。
- 内置照片没有独立数据库记录，使用 `photos.json.seedStates[photoId].deletedAt` 保存同一状态。
- `purgeStartedAt` 和 `purgeCompletedKeys` 只在管理员明确执行永久删除时创建，用于故障后的安全重试。
- 未增加 `deletedBy`、`purgeAfter`，未修改 photoId、对象键、标题、年份或排序。
- 历史 `hiddenSeedIds` 原样保留，不将旧隐藏照片自动恢复或自动纳入新回收站。

迁移脚本 `scripts/migrate_photo_recycle_bin.py` 默认只检查；`--apply` 先创建原字节备份、校验并刷盘，再原子写入。重复执行无变化。

```powershell
python scripts/migrate_photo_recycle_bin.py --data-file <本地隔离目录>/photos.json
python scripts/migrate_photo_recycle_bin.py --data-file <本地隔离目录>/photos.json --apply
```

本轮已在隔离的本地浏览器测试数据上执行，备份为：

`recycle-work/browser-data/photos.json.pre-recycle-20260928T071211960002Z.bak`

此备份与测试数据位于仓库外，不提交 Git；单元测试另验证备份原字节一致、dry-run 不写入、增量应用和重复执行。

## 管理入口与 API

后台新增 `/admin/trash`，并在照片、书本、音乐、评论后台导航中增加回收站。

| 接口 | 行为 |
| --- | --- |
| `DELETE /api/manage/photos/<id>` | 移入回收站；重复操作保持原删除时间 |
| `GET /api/manage/photos` | 正常照片列表 |
| `GET /api/manage/photos?includeTrash=1` | 供书本/评论管理识别回收站引用 |
| `GET /api/manage/trash/photos` | 回收站照片，按删除时间倒序；含评论总数、书本引用提示 |
| `POST /api/manage/photos/<id>/restore` | 恢复原记录 |
| `DELETE /api/manage/trash/photos/<id>` | 永久删除；仅允许回收站照片 |

以上数据和操作 API 均使用现有管理员会话鉴权。回收站页面复用既有登录风格，支持搜索、年份筛选、24 张分页、自定义永久删除确认、空状态和失败重试提示。

## 软删除与恢复

软删除只设置状态，不调用 OSS，不删除本地照片、评论或任何书本关系。

公开照片 API、管理正常列表、公开原图/缩略图接口、照片评论读写接口均在后端判断照片是否正常。公开评论计数仍只统计正常照片上的 approved 评论。普通访客访问回收站照片图片/评论返回统一 404，不返回回收站或存储信息。

书本保留 photoId、slot、page id、layout、order、精选 commentId。前台对缺失照片保留空白位置，其他照片照常显示；不请求回收站照片资源。精选评论还要求其照片正常，故回收站期间不公开。后台显示“照片已在回收站”、名称、ID、日期和恢复按钮。

恢复将 deletedAt 设回 null，使用同一 ID、OSS key、评论和书本关系。已验证第 5 页第 2 个槽位自动恢复，以及原精选留言自动恢复。

## 永久删除的安全边界

1. 鉴权、确认回收站状态。
2. 收集原图/缩略图键，校验照片命名空间、共享对象引用、旧待删除队列与本地路径。
3. 读取并准备全部评论与书本关联；损坏的关联数据阻止操作。
4. 先持久化永久清理开始状态，再调用原有 OSS helper 删除资源。
5. 每个成功对象键写入清理进度；NoSuchKey 可安全重试，权限/网络/异常状态不能视为成功。
6. 对象均处理成功后删除旧本地文件。
7. 用持久化提交日志协调照片、评论、书本三个 JSON 文件：解除关联精选留言，移除目标 photoId 的槽位引用，删除其全部状态评论，删除照片记录。
8. 保留所有书页；`photos: []` 表示空页。其他槽位与手动留言保持不变。

内置照片永久删除后保留旧机制的隐藏 ID 标记，避免固定前端照片目录再次显示该 ID；删除其本地文件及 seedOss/seedStates 状态。

发生部分失败时：返回清晰失败提示，保留照片记录、关联和已完成键，允许重试永久删除。已经开始永久删除的照片暂不能恢复，因为资源可能部分删除或删除结果未知；恢复不会伪装成完整照片。

三个 JSON 文件没有 SQL 事务。`photo_lifecycle.py` 提供跨进程锁、刷盘原子写入和提交日志。元数据写入中断后，下次相关请求先完成已经获准的提交；恢复失败时返回 503，避免公开半完成关联。这不是自动清空回收站，也不会主动删除其他照片或 OSS 对象。

旧 OSS 清理重试接口排除仍被照片引用的键，保护正常及回收站资源。图片响应采用 `private, no-cache` 重新检查状态。已经下载到浏览器的图片无法追溯撤回。

## 本地验证结果

完整 unittest：**41 项通过，0 failures，0 errors**。其中新增回收站测试 20 项；其余为现有书本、OSS、音乐、评论与审核测试。

新增覆盖：软删除、恢复、公开过滤、图片直接 URL、评论读写阻断、四种评论状态保留、回收站排序/计数、鉴权、先进入回收站限制、原槽位与精选留言恢复、永久关联清理、空页及手动留言保留、OSS 故障、部分失败重试、NoSuchKey、共享/错误命名空间保护、提交中断恢复、迁移备份/幂等、旧编辑器保存保护、写盘失败前禁止删对象、关联数据损坏保护、旧清理队列保护、SDK 异常状态检测。

浏览器使用仅绑定 `127.0.0.1:8021` 的隔离测试服务。照片全部生成，音乐为静音测试文件；OSS 对象使用内存替身，真实 SDK Bucket 入口显式阻断。

- 启动页、图集、平面、书本、大图、评论 Drawer 正常。
- 本地发表评论仍为 pending，仅本人可见，不改变公开计数。
- 移入回收站后正常列表减少，回收站仍显示缩略图、全部状态评论数量与书本引用提示。
- 后台评论可识别回收站照片，禁止新设精选留言。
- 书本后台及回收站恢复均成功；原第 5 页第 2 个槽位恢复。
- 前台 Network：回收站期间测试书本仅请求正常照片缩略图，未请求回收站照片，也不显示关联精选留言。
- 永久删除确认和取消通过浏览器验证；实际删除清理、对象不存在、部分失败及重试通过隔离接口单元测试验证。
- 生成图片上传成功；照片库选择、槽位移动、拖入槽位、保存排版通过。
- rc.2 模板 grid 高度正常，照片库卡片正常。
- 唯一全局 audio，音乐滑块不翻页、暂停后切换模式仍暂停；后台试听正常。
- Console 未发现新增 JS Error/Unhandled Promise。初次未登录的 401 属预期；既有 favicon 缺失不在本次范围。
- 六个 HTML 内联脚本及五个 Python 文件语法检查通过。
- 桌面、小屏响应式检查通过；**尚未进行真实 iPhone/Android 测试**，手机双页留言字号保持不变。

单元测试故意模拟的故障会输出错误日志，这是失败保护用例的预期行为，不代表测试失败。

## 文件范围

新增：`photo_lifecycle.py`、`public/trash-admin.html`、`scripts/migrate_photo_recycle_bin.py`、`tests/test_photo_recycle_bin.py`、本说明。

修改：`app.py`、`oss_storage.py`、`public/manage.html`、`public/book-admin.html`、`public/comments-admin.html`、`public/music-admin.html`、`public/index.html`、`tests/test_book_layout.py`、`tests/test_oss_integration.py`。

音乐后台仅新增导航；未修改音乐播放器、字体、翻页算法、布局定义或 OSS 上传/读取架构。现有 OSS delete helper 只补充返回状态检测。

## 以后获准部署时的检查

本轮不执行以下事项：

- 核对生产实际代码和数据，先备份照片、评论、书本 JSON 与旧文件。
- 审核部署清单必须包含新增 `photo_lifecycle.py`、回收站页面，以及当前实际使用的 `comment_store.py`；仓库旧 `deploy-safe.sh` 的模块列表不完整，不能原样用于本功能。
- 迁移先 dry-run，仅执行增量 metadata migration，不重新导入照片或上传/移动 OSS 对象。
- 对实际服务器多 worker、私有 OSS 权限和真实删除响应做受控验证；永久删除只使用明确批准的测试照片。
- 数据回滚必须连同照片/评论/书本及未完成日志一起评估；永久删除完成后的 OSS 文件无法靠只回滚代码恢复。

当前稳定 rc.2 标签没有改动。未执行 Git push 或生产部署。
