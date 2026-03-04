# astrbot_plugin_shanghaitech_verifier

ShanghaiTech 进群学号校验插件（AIOCQHTTP）。

## 功能

- 在 `data/students.json` 维护学号索引（10 位学号为 key）。
- 监听进群申请事件（`request/group/add`）。
- 将进群答案解析为学号并执行规则：
	- 学号必须是 10 位数字；
	- 学号必须存在于索引；
	- 类别必须是 `本科生`；
	- `count` 必须是 `0`（防止重复/小号）。
- 校验通过：自动放行，并将该学号 `count + 1`。
- 校验失败：立即发送异常消息到 `ADMIN_GROUP`，但不自动拒绝入群（留给管理员手动处理）。

## 数据文件

- 索引文件：`data/students.json`
- 字段结构：
	- `name`
	- `email`
	- `category`（如：本科生、研究生）
	- `count`（默认 `0`）

## 配置

通过 AstrBot 插件配置（`_conf_schema.json`）设置：

- `admin_group`：管理员告警群号（QQ 群号）
- `debug_log`：是否输出调试日志（默认 `true`）

未设置 `admin_group` 时，异常事件只会写日志，不会发送群告警。
开启 `debug_log` 后，会在日志中输出事件接收与过滤过程，便于排查“收不到消息”。

## 参考

- AstrBot 接收消息：https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- AstrBot 存储：https://docs.astrbot.app/dev/star/guides/storage.html
