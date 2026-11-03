# 看板发布前检查清单（用户问题汇总）

本清单由 `generate.py` 的 `quality_gate()` 在每次发布（写回 index.html 前）自动核对，并打印到 GitHub Actions 日志。任一【关键】项不通过会 `sys.exit(1)` 阻断发布，避免带病上线。

## 关键项（不通过 → 阻断发布）

1. **五页分栏布局**：按市场分为五页——存储市场 / Token 市场 / 算力市场 / 京东政企 / 周月总结（各 `data-tab` + `panel-{id}` 容器，id 为 storage/token/compute/jd/review），禁止旧三页导航标记（`pageNews`/`pagePrice`/`pageReview` 或 `data-page="news"/price/review`）；`#reviewPanel`/`#footer`/`.toolbar` 与五个面板容器齐全。内容数据不变，仅布局分栏。
2. **旧价格模块已移除**：`priceModule` 数据键永不再出现（用户 10-21 决定移除旧「存储价格变动」独立模块）。
3. **内存价格变化模块**：位于「存储市场」页内（`memoryPrice` 顶层键 + `renderMemoryPrice()` / `mp-table` 渲染），数据合法（weeks 与每条 prices 列数一致、价格为非负数或空值 `null`、链接为 https）、不得退回独立价格页。A股配色：涨=红、跌=绿。本期某型号未抓到时该格为 `null`，前端显示「—」且不参与涨跌计算，**绝不拿上一期价格冒充本期价格**。
4. **总结区存在**：`review` 板块（type=review）存在，用于承载周报/月报。
5. **总结为可读中文散文**：周/月总结（含 `weeks`/`months` 历史归档）不得是 JSON 碎片 / 代码块 / 空行退化；所有条目标题与摘要必须是干净文本。
6. **四市场板块齐全**：`storage` / `token` / `compute` / `jd` 四个 id 缺一不可。

## 常规项（由代码逻辑保证，仅记录核对）

7. 存储 / Token / 算力均分 `国外 + 国内` 两组（KEYWORDS 齐备）。
8. `updatedAt` == 目标日 `BIZ_STR`。
9. 全部条目链接为合法 `http(s)`（死链/不可达/404/5xx 已在 `verify_before_publish` 过滤剔除）。
10. 标题—链接稳定配对（`_idx` 序号配对，绝不错位）。
11. 厂商源按关键词卡严（`_relevant` 对所有来源含官方生效）。
12. 算力严格分国内外；官网优先（`official` > `media`）。
13. 外文标题/要点统一翻译为书面化中文摘要。
14. 某板块当日无更新时，展示官方关注渠道（不空白）。
15. 只收录发布日 == 目标日的资讯，绝不追溯、绝不杜撰。
16. 每日北京时间 24:00 更新（cron `0 16 * * *` UTC）。
17. 周报每周日 24:00 生成（`build_review`：`BIZ_DATE.weekday()==6`）；月报每月最后一天 24:00 生成（`is_last_day(BIZ_DATE)`）。
18. 内存价格每周四自动更新（本机抓取 → 云端合并部署，用户零操作）：
    - 本机 WorkBuddy 自动化（每周四 09:30）跑 `run_daily.ps1`：复用本机京东登录态（Playwright + Edge + 反爬字体解码）抓取 24 款价格，写入 `D:\市场分析\内存价格跟进.xlsx`；随后跑 `weekly_price_sync.py` 生成 `price_snapshot.json`（**只含 SKU 与价格数字**）并提交到仓库。
    - 云端 `price-update.yml`：`push(price_snapshot.json)` 立即触发 + cron `0 2 * * 4` UTC（北京周四 10:00）兜底 → `update_price.py` 以 index.html 现有 `memoryPrice` 为基准**累积历史**（不从 xlsx 重建，否则会丢掉新一期），按 SKU 精确合并快照为最新一期，只改 `memoryPrice`，其余数据一字不动。
    - 安全红线：`jd_state.json` / cookies / 京东账号密码**绝不进入公开仓库**（Pages 公开可见）。
    - 有效价格 < 8 条时脚本拒绝提交，看板保留上一期；单款缺失显示「—」。不编造数字、不伪造空列。
    - 登录态过期时（价格被京东打码成 `1??9`、输出 `NO_PTKEY`/`LOGIN_EXPIRED`）唯一人工动作是跑 `jd_login.ps1` 用京东 App 重新扫码，不是每周都要做。
19. 历史保留 30 天。
20. 媒体源相关性"暂不收紧"（扩源但相关性卡严，后续可能再收紧）。

## 网址输出约定

每次改动看板后，回复末尾必须附**纯文本**网址（不加 `**`、不加 `[]()`、不加反引号）：

    https://g6374.github.io/market-dashboard/

github.io 服务器在境外，国内偶尔连不上，强刷 Ctrl+F5 或换网络即可。
