# 看板发布前检查清单（用户三天问题汇总）

本清单由 `generate.py` 的 `quality_gate()` 在每次发布（写回 index.html 前）自动核对，并打印到 GitHub Actions 日志。任一【关键】项不通过会 `sys.exit(1)` 阻断发布，避免带病上线。

## 关键项（不通过 → 阻断发布）

1. **单页结构**：四市场动态 + 总结在同一页，不得出现三页导航（`pageNews`/`pagePrice`/`pageReview` 或 `data-page`），且主容器 `#grid`/`#reviewPanel`/`#footer`/`.toolbar` 齐全。
2. **价格模块已移除**：`priceModule` 数据键永不再出现（用户 10-21 决定移除）。
3. **总结区存在**：`review` 板块（type=review）存在，用于承载周报/月报。
4. **四市场板块齐全**：`storage` / `token` / `compute` / `jd` 四个 id 缺一不可。

## 常规项（由代码逻辑保证，仅记录核对）

5. 存储 / Token / 算力均分 `国外 + 国内` 两组（KEYWORDS 齐备）。
6. `updatedAt` == 目标日 `BIZ_STR`。
7. 全部条目链接为合法 `http(s)`（死链/不可达/404/5xx 已在 `verify_before_publish` 过滤剔除）。
8. 标题—链接稳定配对（`_idx` 序号配对，绝不错位）。
9. 厂商源按关键词卡严（`_relevant` 对所有来源含官方生效）。
10. 算力严格分国内外；官网优先（`official` > `media`）。
11. 外文标题/要点统一翻译为书面化中文摘要。
12. 某板块当日无更新时，展示官方关注渠道（不空白）。
13. 只收录发布日 == 目标日的资讯，绝不追溯、绝不杜撰。
14. 每日北京时间 24:00 更新（cron `0 16 * * *` UTC）。
15. 周报每周日 24:00 生成（`build_review`：`BIZ_DATE.weekday()==6`）；月报每月最后一天 24:00 生成（`is_last_day(BIZ_DATE)`）。
16. 历史保留 30 天。
17. 媒体源相关性"暂不收紧"（扩源但相关性卡严，后续可能再收紧）。

## 网址输出约定

每次改动看板后，回复末尾必须附**纯文本**网址（不加 `**`、不加 `[]()`、不加反引号）：

    https://g6374.github.io/market-dashboard/

github.io 服务器在境外，国内偶尔连不上，强刷 Ctrl+F5 或换网络即可。
