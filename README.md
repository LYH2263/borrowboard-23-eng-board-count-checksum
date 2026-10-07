# Borrowboard · 邻里借用

上架 → 借出通过 → 归还；单物件同时仅一笔在借，含逾期判定。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5400 |
| API | 10400 |

0-1：`deposit` / `damage_note` / `neighbor_rating`。

## 借出库侧车巡检

命令入口（不挂在业务路由上，前端无按钮）：

```bash
docker compose exec backend python -m app.sidecar_inspect          # 只出报告(默认)
docker compose exec backend python -m app.sidecar_inspect --fix    # 对齐模式
# 本地: cd backend && DATA_DIR=<数据目录> python3 -m app.sidecar_inspect [--report r.json] [--keep-sidecar]
```

- 入口把主库复制到侧车文件再点行数：可借栏条数 `available`、顶细条可借 `top_available`、active 行 `active_loan_rows`、on_loan 件数 `on_loan_items`（另有顶细条在借/逾期 `active`/`overdue`），口径与 `/api/board` 分栏一致。对不上退出非 0，报告 `findings`/`item_ids` 带 `item_id`。
- 模式在启动时由 `--fix` 开关选定并写进报告 `mode` 字段（`report`/`fix`），同一开关连跑模式不变。默认只出报告：发现 `on_loan` 却无 active 行的残局只报告，绝不悄悄补行；`--fix` 才补录借出行（借用人 `巡检补录`）/ 纠正状态，逐条记入 `repairs`。
- 巡检进行中页面又借出/归还成功：主库新行不算副本里的历史分叉，只标 `main_changed_during_run`，不为刷绿追改主库或清空 loans；`--fix` 遇此放弃修复（`fix_aborted`）。
- 同一份主库快照连点两遍，结论须一致（同为 0 或同带同一 item_id），对不上整场失败，退出 3。
- 侧车文件用完即删（`--keep-sidecar` 保留），主库文件路径不变，主库只读打开；`--fix` 也只走 SQL 原地对齐。

退出码：`0` 一致或已对齐 · `1` 对不上（报告带 item_id）· `2` 运行错误（如主库缺失）· `3` 两遍自检不一致。
