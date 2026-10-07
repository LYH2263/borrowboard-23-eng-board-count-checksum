import json
import sqlite3
from pathlib import Path

import pytest

from app import seed
from app import sidecar_inspect as si
from app.db import db_path


@pytest.fixture()
def env_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return db_path()


def _exec(path, *stmts):
    c = sqlite3.connect(path)
    with c:
        for s in stmts:
            c.execute(s)
    c.close()


def _rows(path, sql):
    c = sqlite3.connect(path)
    rows = c.execute(sql).fetchall()
    c.close()
    return rows


def _item_status(path, iid):
    return _rows(path, f"SELECT status FROM items WHERE id={iid}")[0][0]


def _active_loans_of(path, iid):
    return _rows(path, f"SELECT COUNT(*) FROM loans WHERE item_id={iid} AND status='active'")[0][0]


def test_clean_db_exit0_counts_match_board(env_db):
    report, code = si.run()
    assert code == 0 and report["ok"] and report["mode"] == "report"
    c = report["counts"]
    # 与看板分栏同口径: 可借 3 件; 种子借出应还 2020-06-01, 顶细条计入逾期
    assert c["available"] == c["top_available"] == 3
    assert c["active_loan_rows"] == c["on_loan_items"] == 1
    assert c["active"] == 0 and c["overdue"] == 1
    assert report["item_ids"] == [] and report["self_check_ok"]
    assert not report["main_changed_during_run"]
    # 侧车用完即删, 主库路径不变
    assert report["sidecar"]["removed"] and not Path(report["sidecar"]["path"]).exists()
    assert db_path() == env_db


def test_orphan_on_loan_exit1_with_item_id_no_silent_repair(env_db):
    _exec(env_db, "UPDATE items SET status='on_loan' WHERE id=1")
    loans_before = _rows(env_db, "SELECT * FROM loans ORDER BY id")
    report, code = si.run()
    assert code == 1 and not report["ok"]
    assert report["item_ids"] == [1]
    assert report["findings"][0]["kind"] == "orphan_on_loan"
    assert report["findings"][0]["item_id"] == 1
    # 默认只出报告: 不悄悄补行, 主库一行未动
    assert _rows(env_db, "SELECT * FROM loans ORDER BY id") == loans_before
    assert _item_status(env_db, 1) == "on_loan"


def test_same_snapshot_same_conclusion_twice(env_db):
    _exec(env_db, "UPDATE items SET status='on_loan' WHERE id=1")
    r1, c1 = si.run()
    r2, c2 = si.run()
    assert c1 == c2 == 1
    assert r1["item_ids"] == r2["item_ids"] == [1]
    assert r1["mode"] == r2["mode"] == "report"


def test_fix_aligns_only_with_switch(env_db):
    _exec(env_db, "UPDATE items SET status='on_loan' WHERE id=1")
    report, code = si.run(fix=True)
    assert report["mode"] == "fix" and code == 0
    assert report["post_fix_ok"] and report["remaining_item_ids"] == []
    assert any(r["item_id"] == 1 and r["action"] == "insert_compensating_loan"
               for r in report["repairs"])
    row = _rows(env_db, "SELECT borrower, status FROM loans "
                        "WHERE item_id=1 AND status='active'")[0]
    assert row == ("巡检补录", "active")
    # 对齐后再跑只出报告: 同库连跑同为成功 0
    again, code2 = si.run()
    assert code2 == 0 and again["item_ids"] == [] and again["mode"] == "report"


def test_active_loan_on_available_item(env_db):
    _exec(env_db, "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) "
                  "VALUES (2,'邻居乙','active','2099-01-01','2026-01-01')")
    report, code = si.run()
    assert code == 1 and report["item_ids"] == [2]
    assert report["findings"][0]["kind"] == "active_loan_item_not_on_loan"
    fixed, code2 = si.run(fix=True)
    assert code2 == 0 and _item_status(env_db, 2) == "on_loan"


def test_page_write_during_run_is_not_divergence(env_db):
    def lend_item_1(main):
        _exec(main,
              "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) "
              "VALUES (1,'邻居丙','active','2099-01-01','2026-10-07')",
              "UPDATE items SET status='on_loan' WHERE id=1")

    report, code = si.run(_after_snapshot=lend_item_1)
    # 巡检期间页面借出成功: 新行不算副本里的历史分叉, 也不为刷绿改主库
    assert code == 0 and report["ok"]
    assert report["main_changed_during_run"]
    assert _item_status(env_db, 1) == "on_loan"
    assert _active_loans_of(env_db, 1) == 1


def test_fix_aborts_when_main_moves_during_run(env_db):
    _exec(env_db, "UPDATE items SET status='on_loan' WHERE id=1")

    def page_return(main):
        _exec(main, "UPDATE items SET status='available' WHERE id=1")

    report, code = si.run(fix=True, _after_snapshot=page_return)
    assert code == 1 and report["fix_aborted"] and report["repairs"] == []
    assert report["main_changed_during_run"]
    assert _item_status(env_db, 1) == "available"  # 未追改主库
    assert _active_loans_of(env_db, 1) == 0        # 也未补行


def test_self_check_mismatch_fails_whole_run(env_db, monkeypatch):
    real = si.analyze
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        result = real(path)
        if calls["n"] == 2:
            result = {**result, "ok": False, "item_ids": [999]}
        return result

    monkeypatch.setattr(si, "analyze", flaky)
    report, code = si.run()
    assert code == 3 and not report["self_check_ok"]


def test_missing_main_db_exit2(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    report, code = si.run()
    assert code == 2 and report["error"] == "main_db_missing"


def test_report_file_written(env_db, tmp_path):
    out = tmp_path / "report.json"
    report, code = si.run(report_path=out)
    assert code == 0
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["counts"] == report["counts"]
    assert on_disk["mode"] == "report"
