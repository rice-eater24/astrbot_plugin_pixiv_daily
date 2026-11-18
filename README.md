# astrbot_plugin_pixiv_daily

每天定时把 Pixiv 排行榜的画作推送到群或私聊。

## 指令

| 指令 | 作用 |
|---|---|
| `/pixiv` | 只向**当前会话**推送一次 |
| `/pixiv 订阅` | 把当前会话加入每日推送 |
| `/pixiv 退订` | 取消订阅 |
| `/pixiv 状态` | 定时器状态、下次时间、缓存占用 |
| `/pixiv 清理` | 手动清理图片缓存 |

手动推送只影响当前会话，不会打扰其他订阅群；定时任务才会向全部订阅推送。

除这四个子命令外，`/pixiv` 后面跟任何内容都按「推送一次」处理。

## 快速验证

1. `/pixiv 订阅` → 提示已订阅
2. `/pixiv 状态` → 确认「定时任务：运行中」
3. `/pixiv` → 当前会话收到一批图

定时任务和手动推送走的是同一个发送函数，能通就说明定时没问题。

## 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| enable | true | 定时推送开关 |
| push_time | 08:30 | 每天几点推送 |
| count | 10 | 推送张数 |
| mode | daily | 每日 / 每周 / 每月 / 新人 / 原创 |
| content | all | 全部 / 插画 / 漫画 |
| filter_r18 | true | 过滤 R18 |
| images_per_message | 3 | 每条消息几张图 |
| show_url | false | 文字里附带链接 |
| download_images | true | 下载图片后发送 |
| image_dir | 空 | 图片保存目录 |
| keep_days | 7 | 缓存保留天数 |
| cleanup_enable | true | 定时清理 |
| cleanup_time | 04:00 | 每天几点清理 |
| max_cache_mb | 500 | 缓存容量上限 |
| proxy | 空 | 代理地址 |
| proxy_fallback | true | 代理失败自动直连 |
| cookie | 空 | 可选，`PHPSESSID=xxx` |
| timeout | 30 | 请求超时 |

## 几个说明

**图片为什么先下载**
Pixiv 图片域名有防盗链，直接发链接会裂图。插件带 `Referer` 下载到本地再发送。

**代理**
国内服务器访问 `pixiv.net` 需要代理。代理挂了会自动尝试直连（可关）。

**403**
换节点，或在配置里填 Cookie（浏览器登录后复制 `PHPSESSID`）。

**只收到文字没图**
看日志里 `[pixiv]` 开头的行，会写明是 403、超时还是类型异常。

## 许可

MIT
