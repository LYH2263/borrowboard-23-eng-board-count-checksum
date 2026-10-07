"""只读对账逻辑：全部基于侧车快照，不碰主库连接。

四个必须互相对得上的数：
- available_panel_rows  可借栏条数      = items.status='available'
- active_loan_rows      active 行       = loans.status='active'
- on_loan_pieces        on_loan 件数    = items.status='on_loan'
- topbar.available      顶细条可借数    = /api/board 返回的 counts.available

一致态（逐件）：item 为 available ⇔ 该 item 没有 active loan；
                item 为 on_loan   ⇔ 该 item 恰有 1 笔 active loan。
"""
from datetime import date
from pathlib import Path

from app.engines.borrow_rules import classify_loans

KNOWN_ITEM_STATUS = {"available", "on_loan"}


def _load(c):
    items = [dict(r) for r in c.execute("SELECT * FROM items")]
    loans = [dict(r) for r in c.execute("SELECT * FROM loans")]
    return items, loans


def analyze(conn, today: str | None = None) -> dict:
    """对一个（只读侧车）连接做对账，返回计数与逐件 findings。"""
    today = today or date.today().isoformat()
    items, loans = _load(conn)

    active_per_item: dict[int, list[dict]] = {}
    for L in loans:
        if L.get("status") == "active":
            active_per_item.setdefault(L["item_id"], []).append(L)

    findings: list[dict] = []
    available_items, on_loan_items, other_items = [], [], []
    for it in items:
        iid, st = it["id"], it["status"]
        actives = active_per_item.pop(iid, [])
        n = len(actives)
        if st == "available":
            available_items.append(it)
            if n == 1:
                findings.append({
                    "code": "available_with_active_loan",
                    "item_id": iid, "item_title": it.get("title"),
                    "detail": f"可借栏在列但存在 active 借出借记 loan_id={actives[0]['id']}",
                    "loan_ids": [L["id"] for L in actives],
                    "auto_fixable": True,  # 状态滞后，可守卫式把 item 改回 on_loan
                })
            elif n > 1:
                findings.append({
                    "code": "multiple_active_loans",
                    "item_id": iid, "item_title": it.get("title"),
                    "detail": f"可借栏在列却有 {n} 笔 active 行，业务无法裁定留哪笔",
                    "loan_ids": [L["id"] for L in actives],
                    "auto_fixable": False,
                })
        elif st == "on_loan":
            on_loan_items.append(it)
            if n == 0:
                # 残局：status 已 on_loan，loan 行却不在了。只点出，不补行。
                findings.append({
                    "code": "on_loan_without_active_loan",
                    "item_id": iid, "item_title": it.get("title"),
                    "detail": "status=on_loan 但没有任何 active 行（残局），不自动补 loan 行",
                    "loan_ids": [],
                    "auto_fixable": False,
                })
            elif n > 1:
                findings.append({
                    "code": "multiple_active_loans",
                    "item_id": iid, "item_title": it.get("title"),
                    "detail": f"单件存在 {n} 笔 active 行，违反一物件一在借",
                    "loan_ids": [L["id"] for L in actives],
                    "auto_fixable": False,
                })
        else:
            other_items.append(it)
            findings.append({
                "code": "unexpected_item_status",
                "item_id": iid, "item_title": it.get("title"),
                "detail": f"未知 item 状态 {st!r}，既不在可借栏也不在在借件数内",
                "loan_ids": [L["id"] for L in actives],
                "auto_fixable": False,
            })

    # active 行指向已不存在的 item：悬空借记，无法靠改 items 修
    for missing_iid, actives in active_per_item.items():
        findings.append({
            "code": "active_loan_without_item",
            "item_id": missing_iid, "item_title": None,
            "detail": f"active 借出借记 loan_id={[L['id'] for L in actives]} 指向不存在的 item",
            "loan_ids": [L["id"] for L in actives],
            "auto_fixable": False,
        })

    active_loans = [L for L in loans if L.get("status") == "active"]
    returned_loans = [L for L in loans if L.get("status") == "returned"]
    cls = classify_loans(active_loans, today)

    findings.sort(key=lambda f: (f["item_id"] is None, f["item_id"], f["code"]))

    counts = {
        "available_panel_rows": len(available_items),
        "on_loan_pieces": len(on_loan_items),
        "other_status_items": len(other_items),
        "active_loan_rows": len(active_loans),
        "active_non_overdue_rows": len(cls["active"]),
        "overdue_rows": len(cls["overdue"]),
        "returned_loan_rows": len(returned_loans),
    }
    # 顶细条取自 /api/board：available 即可借栏条数，active 仅含未逾期 active
    topbar = {
        "available": len(available_items),
        "active": len(cls["active"]),
        "overdue": len(cls["overdue"]),
    }
    return {
        "counts": counts,
        "panels": {
            "available_panel_rows": counts["available_panel_rows"],
            "active_rows": counts["active_loan_rows"],
            "on_loan_pieces": counts["on_loan_pieces"],
        },
        "board_topbar": topbar,
        "checks": [
            {
                "name": "available_panel_equals_topbar",
                "left": counts["available_panel_rows"],
                "right": topbar["available"],
                "ok": counts["available_panel_rows"] == topbar["available"],
            },
            {
                "name": "active_loans_equals_on_loan_pieces",
                "left": counts["active_loan_rows"],
                "right": counts["on_loan_pieces"],
                "ok": all(f["code"] != "active_loan_without_item" for f in findings)
                      and counts["active_loan_rows"] == counts["on_loan_pieces"],
            },
            {
                "name": "topbar_active_plus_overdue_equals_active_rows",
                "left": topbar["active"] + topbar["overdue"],
                "right": counts["active_loan_rows"],
                "ok": topbar["active"] + topbar["overdue"] == counts["active_loan_rows"],
            },
        ],
        "findings": findings,
        "consistent": not findings,
    }
