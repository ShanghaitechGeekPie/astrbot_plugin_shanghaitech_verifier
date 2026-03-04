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

通过环境变量配置管理员告警群：

```bash
export ADMIN_GROUP=123456789
```

未设置 `ADMIN_GROUP` 时，异常事件只会写日志，不会发送群告警。

## 参考

- AstrBot 接收消息：https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- AstrBot 存储：https://docs.astrbot.app/dev/star/guides/storage.html
