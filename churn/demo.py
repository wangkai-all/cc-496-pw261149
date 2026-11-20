"""Deterministic demo replay: drives merges, labels, feedback and scoring runs
through the real engine so the seeded database has a believable history.

Every step is timestamped with the business day it represents, so risk
movement, late data, dedupe/close/reopen and backdated labels are all visible
in the resulting audit trail.
"""

from __future__ import annotations

import os

from . import seed
from .engine import (
    add_feedback,
    close_warning,
    merge_customers,
    replay_label,
    revise_feature,
    run_scoring,
)
from .store import connect, init_db


def banner(msg: str) -> None:
    print(f"\n=== {msg}")


def _open_warning_for(conn, customer_id: int):
    return conn.execute(
        "SELECT * FROM warning WHERE customer_id=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()


def build_demo(conn) -> dict:
    seed.seed_base(conn)

    banner("W1 基线全量打分（截至 2026-10-07）")
    r = run_scoring(conn, as_of=seed.ts(36, 0), trigger="initial", notes="基线全量",
                    run_at=seed.ts(36, 1))
    print(f"scored={r['scored']} alerts={r['alerts']}")

    banner("W4 周期全量（截至 2026-10-21，周期刷新可见）")
    r = run_scoring(conn, as_of=seed.ts(50, 0), trigger="manual", notes="周期全量",
                    run_at=seed.ts(50, 1))
    print(f"scored={r['scored']} changed={r['changed']} alerts={r['alerts']}")

    # Reviewer dismisses 北辰数据's alert as a false positive.
    w = _open_warning_for(conn, 1)
    if w:
        add_feedback(conn, warning_id=w["id"], verdict="invalid", note="销售说正在谈续约",
                     user="王凯", at=seed.ts(52, 10))
        close_warning(conn, warning_id=w["id"], reason="dismissed", user="王凯",
                      note="销售反馈续约在谈，先关闭观察", at=seed.ts(52, 10))
        print(f"dismissed warning #{w['id']} (北辰数据)")

    # A duplicate close on 星桥教育's alert, then a later re-open: tests dedupe & lifecycle.
    w5 = _open_warning_for(conn, 5)
    if w5:
        add_feedback(conn, warning_id=w5["id"], verdict="valid", note="已确认交付问题",
                     user="李敏", at=seed.ts(50, 10))
        print(f"feedback valid on warning #{w5['id']} (星桥教育)")

    banner("W8 客户合并：11 -> 6（旧账套并入临川医药）")
    job = merge_customers(conn, from_id=11, to_id=6, effective_at=seed.ts(60, 0),
                          available_at=seed.ts(63, 8), reason="同一法人主体合并账套",
                          as_of=seed.ts(63, 8), run_at=seed.ts(63, 8))
    print(f"merge job={job['job_id']} run={job['run']['scored']} changed={job['run']['changed']}")

    banner("W8 迟到数据：北辰数据席位补数（业务 10-28 已发生，11-04 才到）")
    r = run_scoring(conn, as_of=seed.ts(58, 12), trigger="features", notes="迟到数据重新打分",
                    run_at=seed.ts(58, 13))
    print(f"scored={r['scored']} changed={r['changed']} late_visible_only_next_week")

    banner("W9 特征修订：青禾食品席位回填（开通满 14 天，只重算这一家）")
    job = revise_feature(conn, customer_id=12, code="seat_utilization", value=0.48,
                         effective_at=seed.ts(57, 10), available_at=seed.ts(62, 9),
                         note="成熟窗口已满，席位数据回填", as_of=seed.ts(62, 9),
                         run_at=seed.ts(62, 9))
    print(f"revision job={job['job_id']} run={job['run']['scored']} affected only")

    banner("W9 标签：北辰数据确认流失（10-31 实际流失，11-06 才入系统）")
    job = replay_label(conn, customer_id=1, value="churned", effective_at=seed.ts(60, 0),
                       available_at=seed.ts(66, 9), source="csm_confirmed",
                       note="客户确认不再续约", as_of=seed.ts(66, 9), run_at=seed.ts(66, 9))
    print(f"label job={job['job_id']} feedback_updated={job['feedback_updated']}")

    banner("W10 星桥教育标签回溯（实际 10-23 流失，11-10 补录）")
    job = replay_label(conn, customer_id=5, value="churned", effective_at=seed.ts(52, 0),
                       available_at=seed.ts(70, 10), backdated=True, source="csm_confirmed",
                       note="回溯：实际 52 天前已流失，CRM 补录", as_of=seed.ts(70, 10),
                       run_at=seed.ts(70, 10))
    print(f"label job={job['job_id']} backdated feedback_updated={job['feedback_updated']}")

    banner("W10 白鹭文旅：客户续约（实际 10-25 续约，11-15 才回填系统）")
    job = replay_label(conn, customer_id=7, value="retained", effective_at=seed.ts(54, 0),
                       available_at=seed.ts(75, 9), source="crm_sync",
                       note="续约合同回填较晚", as_of=seed.ts(75, 9), run_at=seed.ts(75, 9))
    print(f"label job={job['job_id']} (late, non-backdated=crm lag)")

    banner("W10 澄海物流：标签撤销（11-02 标记流失，11-08 撤销：合同误判）")
    job = replay_label(conn, customer_id=3, value="churned", effective_at=seed.ts(57, 0),
                       available_at=seed.ts(62, 9), source="csm_confirmed",
                       note="初始标记流失", as_of=seed.ts(62, 9), run_at=seed.ts(62, 9))
    print(f"label job={job['job_id']}")
    job = replay_label(conn, customer_id=3, value="unknown", effective_at=seed.ts(57, 0),
                       available_at=seed.ts(68, 9), retract=True, source="csm_review",
                       note="撤销：合同实际已续签，标签作废", as_of=seed.ts(68, 9),
                       run_at=seed.ts(68, 9))
    print(f"retract job={job['job_id']}")

    banner("W10 全量：标签与迟到数据都已入账")
    r = run_scoring(conn, as_of=seed.ts(70, 12), trigger="manual", notes="周度全量",
                    run_at=seed.ts(70, 12))
    print(f"scored={r['scored']} changed={r['changed']} alerts={r['alerts']}")

    banner("W10 撤销一条合并，验证合并可逆")
    # merge 11 into 6 was the only merge; undo it and re-run the pair.
    m = conn.execute("SELECT id FROM customer_merge ORDER BY id DESC LIMIT 1").fetchone()
    if m:
        from .engine import undo_merge
        job = undo_merge(conn, merge_id=m["id"], as_of=seed.ts(71, 8), run_at=seed.ts(71, 8))
        print(f"undo merge job={job['job_id']} changed={job['run']['changed']}")

    banner("W11 全量复核：撤销合并之后的最终状态（截至 2026-11-16）")
    r = run_scoring(conn, as_of=seed.ts(76, 9), trigger="manual", notes="周末全量复核",
                    run_at=seed.ts(76, 9))
    print(f"scored={r['scored']} changed={r['changed']} alerts={r['alerts']}")

    return {"runs": conn.execute("SELECT COUNT(*) n FROM scoring_run").fetchone()["n"],
            "jobs": conn.execute("SELECT COUNT(*) n FROM job_run").fetchone()["n"]}


def main() -> None:
    path = os.environ.get(
        "CHURN_DB", os.path.join(os.path.dirname(__file__), "..", "data", "churn.sqlite")
    )
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = connect(path)
    init_db(conn)
    summary = build_demo(conn)
    print("\nDemo database:", os.path.abspath(path))
    print("Summary:", summary)


if __name__ == "__main__":
    main()
