"""Feature catalog and the initial versioned feature set.

Weights are hand-tuned so the demo has a believable spread of risk tiers.
The exploitability rule of the app: a feature revision only enters a scoring
run when ``available_at <= run.as_of``. A revision that arrives later still
exists (and is shown as "late") but does not move the score until a later run
whose cut-off covers it.
"""

from __future__ import annotations

# code -> definition
FEATURES = [
    {
        "code": "logins_30d",
        "label": "近 30 天登录次数",
        "unit": "次",
        "direction": "lower_risk",
        "description": "客户在 30 天窗口内的登录次数；越低风险越高。",
    },
    {
        "code": "support_tickets_30d",
        "label": "30 天工单数",
        "unit": "单",
        "direction": "higher_risk",
        "description": "30 天内的支持工单数量；越高风险越高。",
    },
    {
        "code": "seat_utilization",
        "label": "席位使用率",
        "unit": "%",
        "direction": "lower_risk",
        "description": "已激活席位 / 已购席位；越低风险越高。",
    },
    {
        "code": "invoice_overdue_days",
        "label": "最长逾期天数",
        "unit": "天",
        "direction": "higher_risk",
        "description": "当前账单最长逾期天数；越高风险越高。",
    },
    {
        "code": "nps_survey",
        "label": "最近 NPS 评分",
        "unit": "分",
        "direction": "lower_risk",
        "description": "最近一次 NPS 调研得分（0-10）；缺失常因未回收问卷。",
    },
]

# weight, anchor (value mapped to ~50 risk points), scale (value span per ~25 points)
FEATURE_PARAMS = {
    "logins_30d": {"weight": 0.30, "anchor": 10.0, "scale": 10.0},
    "support_tickets_30d": {"weight": 0.22, "anchor": 3.0, "scale": 3.0},
    "seat_utilization": {"weight": 0.22, "anchor": 0.45, "scale": 0.30},
    "invoice_overdue_days": {"weight": 0.16, "anchor": 10.0, "scale": 20.0},
    "nps_survey": {"weight": 0.10, "anchor": 6.0, "scale": 4.0},
}

MISSING_REASONS = [
    "not_collected",     # 从未采集
    "source_outage",     # 上游系统故障
    "below_maturity",    # 数据未满成熟窗口
    "opt_out",           # 客户拒绝提供
    "not_applicable",    # 业务上不适用
    "pending_backfill",  # 等待回填
]

MISSING_REASON_LABELS = {
    "not_collected": "未采集",
    "source_outage": "上游故障",
    "below_maturity": "未满成熟窗口",
    "opt_out": "客户拒绝",
    "not_applicable": "业务不适用",
    "pending_backfill": "等待回填",
}

TIERS = ["critical", "high", "medium", "low"]
TIER_LABELS = {"critical": "极高风险", "high": "高风险", "medium": "中风险", "low": "低风险"}

TIER_THRESHOLDS = [("critical", 75.0), ("high", 55.0), ("medium", 35.0), ("low", 0.0)]


def tier_for(score: float) -> str:
    for tier, floor in TIER_THRESHOLDS:
        if score >= floor:
            return tier
    return "low"


def feature_set_v1(conn) -> None:
    """Seed feature definitions and the baseline feature set."""
    for f in FEATURES:
        conn.execute(
            """INSERT OR IGNORE INTO feature_def(code,label,unit,direction,description)
               VALUES(:code,:label,:unit,:direction,:description)""",
            f,
        )
    row = conn.execute("SELECT COUNT(*) AS n FROM feature_set").fetchone()
    if row["n"] == 0:
        conn.execute(
            "INSERT INTO feature_set(version,name,notes,created_at,active) VALUES(1,?,?,?,1)",
            (
                "v1 基线",
                "初始权重：登录 0.30 / 工单 0.22 / 席位 0.22 / 逾期 0.16 / NPS 0.10。",
                "2026-09-01T00:00:00Z",
            ),
        )
        for f in FEATURES:
            p = FEATURE_PARAMS[f["code"]]
            conn.execute(
                "INSERT INTO feature_set_member(set_version,feature_code,weight) VALUES(1,?,?)",
                (f["code"], p["weight"]),
            )
    conn.commit()
