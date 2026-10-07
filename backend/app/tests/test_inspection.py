"""只读侧车巡检的工程化约束测试：

- 干净库：退出 0，计数与 /api/board 分栏一致；
- 不一致：退出非 0、报告带 item_id；同一快照连跑两次结论与 sha256 完全相同；
- report 模式主库零改动；align 只守卫式改 items.status，永不碰 loans；
- on_loan 无 active 残局：两种模式都不补行；
- 巡检期间主库的新借出不进入快照结论；主库在对齐瞬间变化则整笔回滚；
- 侧车文件用完即删，主库路径不变。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import seed
from app.db import connect, db_path
from app.inspection.reconcile import analyze
from app.inspection.runner import _guarded_align, run_inspection
from app.inspection.snapshot import (
    file_sha256, make_snapshot, open_readonly, remove_sidecar,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return tmp_path


def _item_status(iid):
    c = connect()
    st = c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0]
    c.close()
    return st


def _dump(table):
    c = connect()
    rows = [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY id")]
    c.close()
    return rows


def _force_lag(iid=1, borrower="邻居乙", due="2099-01-01"):
    """制造状态滞后：item 仍 available，但已存在一笔 active loan。"""
    c = connect()
    c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
        (iid, borrower, "active", due, "2026-01-01"),
    )
    c.commit()
    c.close()


def _force_stranded(iid=2):
    """制造残局：item=on_loan 却没有任何 active 行。"""
    c = connect()
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
    c.commit()
    c.close()


# ---------- 干净库 ----------

def test_clean_db_passes_and_matches_board(fresh_db):
    from datetime import date
    from app.engines.borrow_rules import classify_loans
    # 与 /api/board 完全相同的取数口径，避免为测试引入 fastapi 依赖
    c = connect()
    n_available = c.execute(
        "SELECT COUNT(*) c FROM items WHERE status='available'").fetchone()["c"]
    loans = [dict(r) for r in c.execute(
        """SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id
           WHERE loans.status='active'""")]
    c.close()
    cls = classify_loans(loans, date.today().isoformat())
    api_counts = {
        "available": n_available,
        "active": len(cls["active"]),
        "overdue": len(cls["overdue"]),
    }
    rep = run_inspection("report")
    assert rep["exit_code"] == 0 and rep["consistent"] is True
    assert rep["mode"] == "report"
    assert rep["board_topbar"]["available"] == api_counts["available"]
    assert rep["counts"]["available_panel_rows"] == api_counts["available"]
    # 种子库：3 available、1 on_loan 且恰有 1 笔 active
    assert rep["counts"]["on_loan_pieces"] == 1
    assert rep["counts"]["active_loan_rows"] == 1
    assert all(chk["ok"] for chk in rep["checks"])


# ---------- 不一致 + 确定性 ----------

def test_lag_reports_item_id_and_two_runs_identical(fresh_db):
    _force_lag(1)
    r1 = run_inspection("report")
    r2 = run_inspection("report")
    for r in (r1, r2):
        assert r["exit_code"] == 1
        assert r["mismatch_item_ids"] == [1]
        assert r["findings"][0]["code"] == "available_with_active_loan"
        assert r["mode"] == "report"
    # 同一份主库快照连跑两次：同一 item_id、同一份快照指纹
    assert r1["mismatch_item_ids"] == r2["mismatch_item_ids"]
    assert r1["snapshot"]["sha256"] == r2["snapshot"]["sha256"]
    assert [(f["code"], f["item_id"]) for f in r1["findings"]] == \
           [(f["code"], f["item_id"]) for f in r2["findings"]]
    assert r1["counts"] == r2["counts"]


def test_report_mode_never_writes_main_db(fresh_db):
    _force_lag(1)
    before = file_sha256(db_path())
    items_before, loans_before = _dump("items"), _dump("loans")
    rep = run_inspection("report")
    after = file_sha256(db_path())
    assert rep["exit_code"] == 1
    assert before == after  # 主库字节级未动
    assert _dump("items") == items_before
    assert _dump("loans") == loans_before


# ---------- align：只修可修方向，loans 永不改 ----------

def test_align_fixes_lag_then_clean(fresh_db):
    _force_lag(1)
    assert _item_status(1) == "available"
    rep = run_inspection("align")
    assert rep["mode"] == "align"
    assert rep["exit_code"] == 0 and rep["consistent"] is True
    assert rep["align"]["applied"] == [{"item_id": 1, "from": "available", "to": "on_loan"}]
    assert _item_status(1) == "on_loan"
    follow = run_inspection("report")
    assert follow["exit_code"] == 0


def test_align_never_touches_loans_table(fresh_db):
    _force_lag(1)
    loans_before = _dump("loans")
    run_inspection("align")
    assert _dump("loans") == loans_before  # 不补行、不清空、不改 status


def test_stranded_on_loan_reported_but_never_repaired(fresh_db):
    _force_stranded(2)
    r_report = run_inspection("report")
    assert r_report["exit_code"] == 1
    f = next(x for x in r_report["findings"] if x["item_id"] == 2)
    assert f["code"] == "on_loan_without_active_loan"
    assert f["auto_fixable"] is False
    assert _item_status(2) == "on_loan"
    active_loans_before = _dump("loans")

    r_align = run_inspection("align")
    assert r_align["exit_code"] == 1
    assert r_align["align"]["attempted"] is False  # 残局在场，整场不动手
    assert 2 in r_align["align"]["unfixable_item_ids"]
    assert _item_status(2) == "on_loan"          # 不悄悄改状态
    assert _dump("loans") == active_loans_before  # 绝不补 active 行

    # 同一残局连跑两次：同一 item_id
    r_report_2 = run_inspection("report")
    assert r_report_2["exit_code"] == 1
    assert r_report_2["mismatch_item_ids"] == r_report["mismatch_item_ids"] == [2]
    assert r_report_2["snapshot"]["sha256"] == r_report["snapshot"]["sha256"]


def test_mixed_fixable_and_stranded_fixes_nothing(fresh_db):
    _force_lag(1)
    _force_stranded(2)
    rep = run_inspection("align")
    assert rep["exit_code"] == 1
    assert rep["align"]["attempted"] is False
    assert _item_status(1) == "available"  # 连可修的那条也不修
    assert _item_status(2) == "on_loan"


# ---------- 快照隔离：巡检期间的业务新行不算历史分叉 ----------

def test_concurrent_lend_after_snapshot_not_in_findings(fresh_db):
    sidecar = make_snapshot(db_path())
    try:
        # 快照点完之后，页面又借出一件：写主库
        c = connect()
        c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (2, "邻居丙", "active", "2099-01-01", "2026-02-01"),
        )
        c.execute("UPDATE items SET status='on_loan' WHERE id=2")
        c.commit()
        c.close()

        rc = open_readonly(sidecar)
        result = analyze(rc)
        rc.close()
        # 副本停在借出之前：item 2 仍 available，没有新 loan，结论仍一致
        assert result["consistent"] is True
        assert result["counts"]["on_loan_pieces"] == 1
        assert all(f["item_id"] != 2 for f in result["findings"])
    finally:
        remove_sidecar(sidecar)


def test_guarded_align_aborts_on_concurrent_return(fresh_db):
    _force_lag(1)
    sidecar = make_snapshot(db_path())
    try:
        rc = open_readonly(sidecar)
        findings = analyze(rc)["findings"]
        rc.close()
    finally:
        remove_sidecar(sidecar)
    assert [f["item_id"] for f in findings] == [1]

    # 对齐前业务侧恰好归还成功：loan→returned，item→available
    c = connect()
    c.execute("UPDATE loans SET status='returned', returned_at='2026-03-01' WHERE item_id=1")
    c.commit()
    c.close()

    info = _guarded_align(db_path(), findings)
    assert info["aborted"] is not None
    assert info["applied"] == []
    assert info["aborted_item_id"] == 1
    # 回滚后未把已归还件错误翻回 on_loan，loans 归还结果也保留
    assert _item_status(1) == "available"
    rep = run_inspection("report")
    assert rep["exit_code"] == 0


# ---------- 只读连接、侧车清理、路径 ----------

def test_sidecar_connection_is_physically_readonly(fresh_db):
    sidecar = make_snapshot(db_path())
    try:
        c = open_readonly(sidecar)
        with pytest.raises(sqlite3.OperationalError):
            c.execute("UPDATE items SET status='on_loan' WHERE id=1")
        c.close()
    finally:
        remove_sidecar(sidecar)


def test_sidecar_file_deleted_after_run(fresh_db):
    _force_lag(1)
    rep = run_inspection("report")
    sidecar = Path(rep["snapshot"]["sidecar"])
    assert rep["snapshot"]["deleted_after_run"] is True
    assert not sidecar.exists()
    assert not Path(str(sidecar) + "-wal").exists()


def test_main_db_path_unchanged(fresh_db, tmp_path):
    rep = run_inspection("report")
    assert rep["main_db"] == str(db_path())
    assert db_path() == tmp_path / "borrowboard.db"


def test_report_file_written_with_mode_and_counts(fresh_db, tmp_path):
    out = tmp_path / "r" / "report.json"
    rep = run_inspection("report", report_path=out)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["mode"] == "report"
    assert on_disk["counts"] == rep["counts"]
    assert on_disk["board_topbar"]["available"] == rep["counts"]["available_panel_rows"]


# ---------- CLI 冒烟 ----------

def test_cli_exit_codes(fresh_db):
    env = {**os.environ, "DATA_DIR": str(fresh_db)}

    def run(*args):
        p = subprocess.run(
            [sys.executable, "-m", "app.inspector", "--quiet", *args],
            cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
        )
        return p.returncode, json.loads(p.stdout)

    code, rep = run()
    assert code == 0 and rep["consistent"] is True and rep["mode"] == "report"

    _force_lag(1)
    code, rep = run()
    assert code == 1 and rep["mismatch_item_ids"] == [1]

    out = fresh_db / "report.json"
    code, rep = run("--report", str(out))
    assert code == 1 and Path(rep["snapshot"]["sidecar"]).exists() is False
    assert json.loads(out.read_text(encoding="utf-8"))["mismatch_item_ids"] == [1]
