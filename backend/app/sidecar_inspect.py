"""借出库只读侧车巡检(命令入口,不挂在业务路由上)。

用法:
    python -m app.sidecar_inspect                # 只出报告(默认,绝不写主库)
    python -m app.sidecar_inspect --fix          # 对齐模式: 对残局补行/正状态
    python -m app.sidecar_inspect --report r.json --keep-sidecar

流程: 主库 -> sqlite 备份到侧车文件 -> 同一份快照连点两遍行数(自检)
-> 侧车与主库只读对账 -> 出 JSON 报告。

退出码:
    0  四数对得上(或 --fix 已对齐)
    1  对不上, 报告 findings/item_ids 带 item_id
    2  运行错误(如主库不存在、快照失败)
    3  同一快照两遍结论不一致, 整场失败

约定:
    - 主库文件路径不变: 只读打开 / 备份, 绝不改名、替换或清空; --fix 也只走 SQL 原地对齐。
    - 巡检进行中页面又借出/归还成功: 主库新行不算副本里的历史分叉,
      只标 main_changed_during_run; 不为刷绿追改主库, --fix 遇此直接放弃修复。
    - 侧车文件用完即删(--keep-sidecar 保留)。
"""

import argparse
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app.db import db_path
from app.engines.borrow_rules import classify_loans

TOOL = "borrowboard-sidecar-inspect"
REPAIR_BORROWER = "巡检补录"

EXIT_OK = 0
EXIT_DIVERGED = 1
EXIT_ERROR = 2
EXIT_SELF_CHECK = 3


def snapshot(main: Path, sidecar: Path) -> None:
    """把主库一致性备份到侧车文件(源库只读打开, 备份期间页面写入不影响副本)。"""
    src = sqlite3.connect(f"file:{main}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(sidecar)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _board_counts(conn) -> dict:
    """与 /api/board 同口径: 可借栏、顶细条可借、在借、逾期; 另点 active 行与 on_loan 件数。"""
    today = date.today().isoformat()
    available = conn.execute(
        "SELECT id FROM items WHERE status='available' ORDER BY id").fetchall()
    loan_rows = [dict(r) for r in conn.execute(
        "SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id "
        "WHERE loans.status='active' ORDER BY loans.id")]
    cls = classify_loans(loan_rows, today)
    return {
        "available": len(available),       # 可借栏条数
        "top_available": len(available),   # 顶细条"可借"(= /api/board counts.available)
        "active": len(cls["active"]),      # 顶细条"在借"
        "overdue": len(cls["overdue"]),    # 顶细条"逾期"
        "active_loan_rows": conn.execute(
            "SELECT COUNT(*) c FROM loans WHERE status='active'").fetchone()["c"],
        "on_loan_items": conn.execute(
            "SELECT COUNT(*) c FROM items WHERE status='on_loan'").fetchone()["c"],
    }


def _reconcile(conn) -> list[dict]:
    """逐件对账 items.status 与 loans active 行, 返回带 item_id 的 findings(按 item_id 有序)。"""
    findings = []
    items = conn.execute("SELECT id, status FROM items ORDER BY id").fetchall()
    active_by_item: dict[int, list[int]] = {}
    for r in conn.execute("SELECT id, item_id FROM loans WHERE status='active' ORDER BY id"):
        active_by_item.setdefault(r["item_id"], []).append(r["id"])
    known = set()
    for it in items:
        iid = it["id"]
        known.add(iid)
        loan_ids = active_by_item.get(iid, [])
        n = len(loan_ids)
        if it["status"] == "on_loan" and n == 0:
            findings.append({"item_id": iid, "kind": "orphan_on_loan",
                             "detail": "items.status=on_loan 但没有 active 借出行(残局)"})
        elif it["status"] == "on_loan" and n > 1:
            findings.append({"item_id": iid, "kind": "multiple_active_loans",
                             "loan_ids": loan_ids,
                             "detail": f"on_loan 物品有 {n} 条 active 借出行"})
        elif it["status"] != "on_loan" and n > 0:
            findings.append({"item_id": iid, "kind": "active_loan_item_not_on_loan",
                             "loan_ids": loan_ids,
                             "detail": f"items.status={it['status']} 但有 {n} 条 active 借出行"})
    for dangling in sorted(set(active_by_item) - known):
        findings.append({"item_id": dangling, "kind": "active_loan_missing_item",
                         "loan_ids": active_by_item[dangling],
                         "detail": "active 借出行指向不存在的物品"})
    return findings


def analyze(db_file) -> dict:
    """点行数 + 逐件对账。只读打开, 可作用于侧车或主库。"""
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = _board_counts(conn)
        findings = _reconcile(conn)
        if counts["available"] != counts["top_available"]:
            findings.append({"item_id": None, "kind": "top_bar_mismatch",
                             "detail": "顶细条可借数与可借栏条数不一致"})
        if counts["active_loan_rows"] != counts["on_loan_items"] and not findings:
            findings.append({"item_id": None, "kind": "count_mismatch",
                             "detail": "active 行数与 on_loan 件数不一致"})
    finally:
        conn.close()
    item_ids = sorted({f["item_id"] for f in findings if f.get("item_id") is not None})
    return {"ok": not findings, "counts": counts, "findings": findings, "item_ids": item_ids}


def signature_of(db_file) -> dict:
    """对账签名: items/loans 的 (id,status) 全量有序快照, 用于察觉巡检期间主库被改写。"""
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        items = [tuple(r) for r in conn.execute("SELECT id, status FROM items ORDER BY id")]
        loans = [tuple(r) for r in conn.execute(
            "SELECT id, item_id, status FROM loans ORDER BY id")]
    finally:
        conn.close()
    return {"items": items, "loans": loans}


def plan_repairs(findings: list[dict]) -> list[dict]:
    """由 findings 推出确定性修复动作; 不认识的 kind 不自动修, 留在报告里。"""
    repairs = []
    for f in findings:
        kind = f["kind"]
        if kind == "orphan_on_loan":
            repairs.append({"item_id": f["item_id"], "action": "insert_compensating_loan",
                            "borrower": REPAIR_BORROWER})
        elif kind == "active_loan_item_not_on_loan":
            repairs.append({"item_id": f["item_id"], "action": "set_item_on_loan"})
        elif kind == "multiple_active_loans":
            keep = min(f["loan_ids"])
            repairs.append({"item_id": f["item_id"], "action": "close_extra_active_loans",
                            "keep_loan_id": keep,
                            "close_loan_ids": [l for l in f["loan_ids"] if l != keep]})
        elif kind == "active_loan_missing_item":
            repairs.append({"item_id": f["item_id"], "action": "close_dangling_loans",
                            "close_loan_ids": list(f["loan_ids"])})
    return repairs


def apply_repairs(main: Path, repairs: list[dict], *, today: str, now_iso: str) -> list[dict]:
    """--fix 专用: 单事务原地对齐主库(只 INSERT/UPDATE, 不清表不改文件路径)。"""
    conn = sqlite3.connect(main)
    try:
        with conn:
            for r in repairs:
                action = r["action"]
                if action == "insert_compensating_loan":
                    cur = conn.execute(
                        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) "
                        "VALUES (?,?,?,?,?)",
                        (r["item_id"], REPAIR_BORROWER, "active", today, now_iso))
                    r["loan_id"] = cur.lastrowid
                elif action == "set_item_on_loan":
                    conn.execute("UPDATE items SET status='on_loan' WHERE id=?",
                                 (r["item_id"],))
                elif action in ("close_extra_active_loans", "close_dangling_loans"):
                    for lid in r["close_loan_ids"]:
                        conn.execute(
                            "UPDATE loans SET status='returned', returned_at=? WHERE id=?",
                            (now_iso, lid))
    finally:
        conn.close()
    return repairs


def _expected_signature(sig: dict, applied: list[dict]) -> dict:
    """快照签名 + 本次修复动作 = 修复后主库应有签名; 对不上说明修复期间主库又被外部改写。"""
    items = dict(sig["items"])
    loans = {lid: (item_id, status) for lid, item_id, status in sig["loans"]}
    for r in applied:
        action = r["action"]
        if action == "insert_compensating_loan":
            loans[r["loan_id"]] = (r["item_id"], "active")
        elif action == "set_item_on_loan":
            items[r["item_id"]] = "on_loan"
        elif action in ("close_extra_active_loans", "close_dangling_loans"):
            for lid in r["close_loan_ids"]:
                loans[lid] = (loans[lid][0], "returned")
    return {"items": sorted(items.items()),
            "loans": sorted((lid, item_id, status) for lid, (item_id, status) in loans.items())}


def _conclusion(result: dict) -> str:
    return json.dumps({"ok": result["ok"], "item_ids": result["item_ids"],
                       "counts": result["counts"], "findings": result["findings"]},
                      ensure_ascii=False, sort_keys=True)


def _remove_sidecar(sidecar: Path) -> None:
    for suffix in ("", "-wal", "-journal", "-shm"):
        Path(str(sidecar) + suffix).unlink(missing_ok=True)


def _emit(report: dict, report_path) -> dict:
    if report_path:
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def run(*, fix: bool = False, keep_sidecar: bool = False, report_path=None,
        _after_snapshot=None) -> tuple[dict, int]:
    """巡检主流程。_after_snapshot: 测试钩子, 快照后注入一次主库写入, 模拟页面借还。"""
    mode = "fix" if fix else "report"  # 启动时由开关选定; 同一开关连跑模式不变
    main = db_path()  # 主库路径: 只读/备份用, 不改名不替换
    report = {"tool": TOOL, "mode": mode, "date": date.today().isoformat(),
              "main_db": str(main), "ok": False}
    if not main.exists():
        report["error"] = "main_db_missing"
        return _emit(report, report_path), EXIT_ERROR
    sidecar = main.with_name(f"{main.stem}.inspect-{os.getpid()}{main.suffix}")
    try:
        try:
            snapshot(main, sidecar)
        except sqlite3.Error as e:
            report["error"] = f"snapshot_failed: {e}"
            return _emit(report, report_path), EXIT_ERROR
        if _after_snapshot is not None:
            _after_snapshot(main)
        first = analyze(sidecar)
        second = analyze(sidecar)  # 同一份主库快照连点两遍, 结论必须一致
        self_ok = _conclusion(first) == _conclusion(second)
        report.update({
            "counts": first["counts"],
            "findings": first["findings"],
            "item_ids": first["item_ids"],
            "self_check_ok": self_ok,
            "sidecar": {"path": str(sidecar), "removed": not keep_sidecar},
            "main_changed_during_run": False,
            "repairs": [],
            "post_fix_ok": None,
        })
        if not self_ok:
            return _emit(report, report_path), EXIT_SELF_CHECK  # 第二次对不上, 整场失败
        snap_sig = signature_of(sidecar)
        changed = signature_of(main) != snap_sig
        report["main_changed_during_run"] = changed
        report["ok"] = first["ok"]
        if not first["ok"]:
            if fix and not changed:
                applied = apply_repairs(
                    main, plan_repairs(first["findings"]),
                    today=report["date"],
                    now_iso=datetime.now(timezone.utc).isoformat())
                post = analyze(main)  # 修复后只读复查主库
                report.update({
                    "repairs": applied,
                    "post_fix_ok": post["ok"],
                    "counts_after_fix": post["counts"],
                    "remaining_item_ids": post["item_ids"],
                    "findings_after_fix": post["findings"],
                    "main_changed_during_run":
                        signature_of(main) != _expected_signature(snap_sig, applied),
                })
                report["ok"] = post["ok"]
                return _emit(report, report_path), EXIT_OK if post["ok"] else EXIT_DIVERGED
            if fix and changed:
                # 巡检期间主库被页面改写: 放弃修复, 绝不为刷绿追改主库或清空 loans
                report["fix_aborted"] = True
            # report 模式: 默认不悄悄补行, 只把残局写进报告
            return _emit(report, report_path), EXIT_DIVERGED
        return _emit(report, report_path), EXIT_OK
    finally:
        if not keep_sidecar:
            _remove_sidecar(sidecar)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m app.sidecar_inspect",
        description="借出库只读侧车巡检: 复制主库到侧车点行数, 默认只出报告")
    p.add_argument("--fix", action="store_true",
                   help="对齐模式: 对残局补行/正状态(默认只出报告不改库)")
    p.add_argument("--report", metavar="PATH", help="报告同时写入该 JSON 文件")
    p.add_argument("--keep-sidecar", action="store_true", help="保留侧车文件(默认用完即删)")
    args = p.parse_args(argv)
    report, code = run(fix=args.fix, keep_sidecar=args.keep_sidecar,
                       report_path=args.report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
