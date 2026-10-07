"""巡检执行器：复制快照 → 只读对账 → （仅 align）守卫式修复 → 报告。

mode 在启动时一次选定并写进报告：
- report（默认）：从头到尾不打开主库的写连接；
- align：只允许把"available 却有唯一 active loan"的 item 状态守卫式改回
  on_loan。绝不 INSERT/DELETE/UPDATE loans，绝不补行、不清空记录。
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect, db_path
from app.inspection.reconcile import analyze
from app.inspection.snapshot import (
    file_sha256, make_snapshot, open_readonly, remove_sidecar,
)

ALIGNABLE = "available_with_active_loan"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def analyze_live_snapshot() -> tuple[dict, Path, str, int]:
    """复制主库→侧车，只读分析。返回 (分析结果, 侧车路径, sha256, 字节数)。"""
    main = db_path()
    sidecar = make_snapshot(main)
    try:
        sha = file_sha256(sidecar)
        size = sidecar.stat().st_size
        c = open_readonly(sidecar)
        try:
            result = analyze(c)
        finally:
            c.close()
    except BaseException:
        remove_sidecar(sidecar)
        raise
    return result, sidecar, sha, size


def _guarded_align(main: Path, findings: list[dict]) -> dict:
    """在主库上事务内做全有或全无的守卫式修复。

    每条修复都要求主库当下仍满足快照时的前提（status=available 且恰有
    1 笔 active）；巡检期间若该件被借出/归还导致前提变化，rowcount=0，
    整笔事务回滚——不会出现修一半。
    """
    targets = [f for f in findings if f["code"] == ALIGNABLE]
    applied: list[dict] = []
    c = sqlite3.connect(str(main))
    try:
        c.execute("BEGIN IMMEDIATE")
        for f in targets:
            iid = f["item_id"]
            cur = c.execute(
                "UPDATE items SET status='on_loan' "
                "WHERE id=? AND status='available' "
                "AND 1 = (SELECT COUNT(*) FROM loans "
                "         WHERE item_id=? AND status='active')",
                (iid, iid),
            )
            if cur.rowcount != 1:
                # 主库已偏离快照（期间发生借出/归还等），放弃整场对齐
                c.execute("ROLLBACK")
                return {
                    "attempted": True, "applied": [],
                    "aborted": f"item_id={iid} 在对齐时已偏离快照，事务已回滚，未改任何行",
                    "aborted_item_id": iid,
                }
            applied.append({"item_id": iid, "from": "available", "to": "on_loan"})
        c.execute("COMMIT")
        return {"attempted": True, "applied": applied, "aborted": None}
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def run_inspection(mode: str = "report", report_path: str | Path | None = None) -> dict:
    """mode 启动时选定：'report'（默认只读）或 'align'。返回完整报告。"""
    if mode not in ("report", "align"):
        raise ValueError(f"未知模式 {mode!r}，只允许 report / align")
    main = db_path()
    started = _now()

    result, sidecar, sha, size = analyze_live_snapshot()
    align_info = {"attempted": False, "applied": [], "aborted": None}
    post = None
    pre_snapshot = {"sidecar": str(sidecar), "sha256": sha, "bytes": size}

    unfixable = [f for f in result["findings"] if not f["auto_fixable"]]
    if mode == "align" and result["findings"] and not unfixable:
        # 只有当全部异常都可由 item 状态守卫修复时才动手；
        # 残局（on_loan 无 active）、多重 active、悬空借记一律不修。
        align_info = _guarded_align(main, result["findings"])
        if align_info["aborted"] is None and align_info["applied"]:
            remove_sidecar(sidecar)
            post_result, sidecar, sha, size = analyze_live_snapshot()
            post = {
                "consistent": post_result["consistent"],
                "counts": post_result["counts"],
                "board_topbar": post_result["board_topbar"],
                "findings": post_result["findings"],
                "snapshot": {"sidecar": str(sidecar), "sha256": sha, "bytes": size},
            }
            result = post_result
    elif mode == "align" and unfixable:
        align_info = {
            "attempted": False, "applied": [],
            "aborted": "存在不可自动修复的异常（含 on_loan 无 active 残局），默认不补行，整场不修",
            "unfixable_item_ids": sorted(
                {f["item_id"] for f in unfixable if f["item_id"] is not None}),
        }

    consistent = bool(result["consistent"]) and align_info["aborted"] is None
    exit_code = 0 if consistent else 1
    report = {
        "tool": "borrowboard-inspector",
        "mode": mode,  # 启动时选定，整场不变
        "started_at": started,
        "finished_at": _now(),
        "exit_code": exit_code,
        "main_db": str(main),
        "snapshot": {
            "sidecar": pre_snapshot["sidecar"],
            "sha256": pre_snapshot["sha256"],
            "bytes": pre_snapshot["bytes"],
            "readonly": True,
            "deleted_after_run": True,
        },
        "counts": result["counts"],
        "panels": result["panels"],
        "board_topbar": result["board_topbar"],
        "checks": result["checks"],
        "findings": result["findings"],
        "mismatch_item_ids": sorted(
            {f["item_id"] for f in result["findings"] if f["item_id"] is not None}),
        "consistent": consistent,
        "align": align_info,
        "post_align": post,
    }
    remove_sidecar(sidecar)

    if report_path:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
