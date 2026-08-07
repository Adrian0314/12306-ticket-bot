# 12306 高铁票订票助手

基于 Selenium + CDP（Chrome DevTools Protocol）的 12306 查票 / 订票脚本。

> ⚠️ **免责声明**：本项目仅供个人学习参考，请勿用于商业用途或规模化抢票。使用本脚本产生的任何后果由使用者自行承担。

## 功能

- 浏览器扫码登录，自动检测登录状态
- 多订单批量配置，同线路自动合并
- 两种抢票模式：按起售时间 / 登录后统一倒计时
- CDP 拦截查票接口，直接解析原始 JSON，比 DOM 解析更快更稳
- 席别、出发时段筛选；查票失败自动重试
- 订单数据独立配置，主脚本不含任何个人信息

## 环境要求

- Python 3.8+
- Chrome 浏览器
- 12306 账号（需扫码登录）

## 安装

```bash
pip install selenium
```

## 使用方法

1. 复制 `config.example.json` 为 `config.json`（`config.json` 已被 `.gitignore` 排除，不会误提交个人信息）
2. 在 `config.json` 里填入你的订单（出发站、到达站、日期、乘车人）
3. 运行：

```bash
python ticket_bot_v4.py
```

4. 浏览器打开 12306 登录页，手机 App 扫码登录
5. 脚本自动按配置查票、选人、下单

## 配置说明

`config.json` 字段含义：

| 字段 | 说明 |
|------|------|
| `mode` | `"sale_time"` = 每笔订单按起售时间抢；`"timer"` = 登录后统一倒计时 |
| `delay_seconds` | `timer` 模式下登录后等待的秒数 |
| `orders` | 订单列表，每笔一个对象 |
| `enabled` | `true` = 抢这单 / `false` = 跳过 |
| `from_st` / `to_st` | 出发 / 到达站名（中文） |
| `date` | 出发日期，格式 `YYYY-MM-DD`（最多提前 15 天） |
| `sale_time` | 起售时间，格式 `HH:MM`（`sale_time` 模式生效） |
| `depart_time_range` | 出发时段，如 `"08:00-12:00"`；留空 = 不限时段 |
| `seat_type` | 席别：二等座 / 一等座 / 商务座 / 硬座 / 无座 |
| `passengers` | 乘车人列表，`name` 必须与 12306 常用联系人一致 |

## 原理

1. Selenium 启动真实 Chrome，扫码登录 12306
2. CDP 监听 `queryG` / `queryZ` 查票接口响应，毫秒级拿到原始 JSON
3. 解析车次、席位余票，按配置筛选目标车次
4. 下单流程走浏览器自动化（选人、选席别、提交、确认）

## 目录结构

- `ticket_bot_v4.py` — 主脚本
- `config.example.json` — 订单配置示例（随仓库分发）
- `config.json` — 本地订单配置（已在 `.gitignore` 中，个人信息只填在这里）

## License

[MIT](LICENSE)
