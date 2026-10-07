# Borrowboard · 邻里借用

上架 → 借出通过 → 归还；单物件同时仅一笔在借，含逾期判定。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5400 |
| API | 10400 |

0-1：`deposit` / `damage_note` / `neighbor_rating`。

## 只读侧车巡检（命令入口）

对账看板四个数：可借栏条数、active 借出借记行、`on_loan` 件数、顶细条可借数。
入口先把主库用 SQLite online backup 复制成临时侧车文件，**对账全程以
immutable 只读连接打开侧车**，业务路由不参与；侧车用完即删，主库路径
（`$DATA_DIR/borrowboard.db`）不变。前端不设巡检按钮，以命令入口为准。

```bash
# 默认 report：只出报告，对主库零写入；不一致退出 1 且报告带 item_id
docker compose run --rm backend python -m app.inspector
# 落盘报告（字段可与当时页面分栏逐项对上）
docker compose run --rm backend python -m app.inspector --report /data/inspect.json
# 显式开关才对齐：仅把「available 却有唯一 active」守卫式改回 on_loan
docker compose run --rm backend python -m app.inspector --align
```

- `mode` 在启动时一次选定（`report` / `align`）并写入报告；不存在一条修表一条不修。
- `status=on_loan` 却没有 active 行属于**残局**，报告以
  `on_loan_without_active_loan` 点出 item_id，任何模式都不补 loan 行；
  残局在场时 `--align` 整场拒绝，连可修项也不动。
- 对齐只做带前提守卫的 `UPDATE items`（要求当下仍 `available` 且恰有 1 笔
  active）；从不 INSERT/DELETE/UPDATE `loans`，不清空借还记录。巡检期间若
  页面又借出/归还，新行不属于本次副本，守卫失配则整笔事务回滚。
- 同一份主库快照连跑两次：同为成功 0，或同带同一组 `mismatch_item_ids`，
  且 `snapshot.sha256` 相同；对不上则整场判失败。
- 退出码：`0` 一致 / 对齐成功；`1` 发现不一致或对齐被拒；`2` 环境或用法错误。

