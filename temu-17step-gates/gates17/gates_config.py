# -*- coding: utf-8 -*-
"""
gates_config.py — 两台机器 17 步的判定配置（机·步号·产物·对账·交叉·黄金样本）

本机 17 步（schedule.json / Temu_12steps_status.json 步号）：
  1备货 2待创建 3·2-3_14天 4查违规 5活动机遇 6活动商品 7站点库存0-1
  8体积重量 9·3个月SKC 10资质待待 11·2-8质检 12商品信息待确认
  14品质分差下架退货 15待快递揽收 29在售货号 34查在售数据 42销量近7日0

吴凡机 17 步（schedule.sewing.json 的 bat）：
  bat1备货 bat3待创建 bat34·03 bat4查违规 bat5活动机遇 bat7活动商品 bat8站点库存0-1
  bat9体积重量 bat10·3个月SKC bat11资质待待 bat12·2-8质检 bat13商品信息待确认
  bat24品质分差下架退货 bat19待快递揽收 bat33在售货号 bat16查在售数据 bat47销量近7日0
  （镜像小票按其自身 step 号落盘，映射见 SEWING_STEPS）
"""
import os

LOCAL = r"C:\Users\Administrator\Desktop"
REP = LOCAL + r"\Temu中控台\reports"
MIRROR = LOCAL + r"\Temu中控台\store_mirror\sewing-tools"
PULL = LOCAL + r"\Temu中控台\gates17\wufan_pull"

READBACK_DENY = ["Traceback \\(most recent call last\\)"]

# ---------------- 本机 17 步 ----------------
LOCAL_STEPS = {
    "1": {
        "name": "01. 备货",
        "artifacts": [{"glob": REP + r"\report_step01_restock_latest.json", "min_bytes": 200}],
        "json_assert": [{"file": REP + r"\report_step01_restock_latest.json", "path": "succeeded", "eq": True}],
        "reconcile": {"type": "restock_gold_equation",
                      "submitted_report": REP + r"\report_step01_restock_latest.json",
                      "submitted_path": "submitted"},
    },
    "2": {
        "name": "02. 待创建",
        "artifacts": [{"glob": REP + r"\report_step02_stock_latest.json", "min_bytes": 100}],
        "json_assert": [{"file": REP + r"\report_step02_stock_latest.json", "path": "summary", "not_empty": True}],
    },
    "3": {
        "name": "03. 2-3_14天",
        "artifacts": [{"glob": REP + r"\report_step03_final_result.json", "min_bytes": 100}],
        "json_assert": [{"file": REP + r"\report_step03_final_result.json", "path": "success", "eq": True}],
    },
    "4": {
        "name": "04. 查违规",
        "artifacts": [
            {"glob": LOCAL + r"\查违规-精简\scripts\temu_sales_compliance_match.md", "min_bytes": 100},
        ],
        "log": {
            "log_files": [LOCAL + r"\查违规-精简\logs\latest-run.log"],
            "deny_log": READBACK_DENY,
            "require_log_any": [r"golden-04\] verdict=GREEN", r"对账通过"],
        },
    },
    "5": {
        "name": "05. 活动机遇",
        "artifacts": [{"glob": REP + r"\report_step05_readback_step05_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step05_readback_step05_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "6": {
        "name": "06. 活动商品",
        "artifacts": [{"glob": REP + r"\report_step06_readback_step06_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step06_readback_step06_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "7": {
        "name": "07. 站点库存0-1",
        "artifacts": [{"glob": REP + r"\report_step07_readback_step07_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step07_readback_step07_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "8": {
        "name": "08. 体积重量",
        "artifacts": [{"glob": REP + r"\report_step08_readback_step08_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step08_readback_step08_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "9": {
        "name": "09. 3个月SKC",
        "artifacts": [{"glob": LOCAL + r"\3个月\Temu_SKC_迁移包_019ef887\scripts\readback_step09_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": LOCAL + r"\3个月\Temu_SKC_迁移包_019ef887\scripts\readback_step09_latest.json",
                         "path": "verdict", "eq": "GREEN"}],
    },
    "10": {
        "name": "10. 资质待待",
        "artifacts": [{"glob": REP + r"\report_step10_readback_step10_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step10_readback_step10_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "11": {
        "name": "11. 2-8质检",
        "artifacts": [{"glob": REP + r"\report_step11_readback_step11_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step11_readback_step11_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "12": {
        "name": "12. 商品信息待确认",
        "artifacts": [{"glob": REP + r"\report_step12_readback_step12_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step12_readback_step12_latest.json", "path": "verdict", "eq": "GREEN"}],
        "log": {
            "log_files": [REP + r"\report_step12_label_report_latest.txt"],
            "deny_log": [r"部分失败", r"有失败项"],
        },
    },
    "14": {
        "name": "13. 品质分差下架退货（下架敏感，第5道灰度门管控）",
        "kind": "delist",
        "artifacts": [{"glob": REP + r"\report_step14_label_report_latest.txt", "min_bytes": 50}],
        "log": {
            "log_files": [REP + r"\report_step14_label_report_latest.txt"],
            "deny_log": [r"部分失败", r"有失败项", r"下架.*失败 [1-9]"],
        },
        "reconcile": {"type": "delist_onsale_absence",
                      "onsale_list": LOCAL + r"\查在售数据\查在售数据_{date}.csv",
                      "column": "货号"},
    },
    "15": {
        "name": "14. 待快递揽收",
        "artifacts": [{"glob": REP + r"\report_step15_readback_step14_latest.json", "min_bytes": 80}],
        "json_assert": [{"file": REP + r"\report_step15_readback_step14_latest.json", "path": "verdict", "eq": "GREEN"}],
    },
    "29": {
        "name": "在售货号",
        "artifacts": [
            {"glob": LOCAL + r"\txt\在售中货号.txt", "min_bytes": 4, "min_records": 5},
            {"glob": LOCAL + r"\txt\不在在售中不在上新.txt", "min_bytes": 4},
        ],
        "crosscheck": {
            "type": "onsale_two_pipelines",
            "a": {"kind": "txt_lines", "file": LOCAL + r"\txt\在售中货号.txt", "distinct": False},
            "b": {"kind": "csv_column", "file": LOCAL + r"\查在售数据\查在售数据_{date}.csv",
                  "column": "货号", "distinct": False},
        },
        "golden": {"type": "sku_classification",
                   "csv": LOCAL + r"\查在售数据\查在售数据_{date}.csv"},
    },
    "34": {
        "name": "查在售数据",
        "artifacts": [
            {"glob": LOCAL + r"\查在售数据\查在售数据_{date}.csv", "min_bytes": 200, "min_records": 5},
            {"glob": LOCAL + r"\查在售数据\查在售数据_{date}.txt", "min_bytes": 100},
        ],
        "crosscheck": {
            "type": "txt_vs_csv_rows",
            "a": {"kind": "csv_column", "file": LOCAL + r"\查在售数据\查在售数据_{date}.csv",
                  "column": "货号", "distinct": False},
            "b": {"kind": "txt_lines", "file": LOCAL + r"\查在售数据\查在售数据_{date}.txt",
                  "match": r"^\d+\t", "distinct": False},
        },
        "golden": {"type": "sku_classification",
                   "csv": LOCAL + r"\查在售数据\查在售数据_{date}.csv"},
    },
    "42": {
        "name": "销量近7日0",
        "artifacts": [
            {"glob": REP + r"\report_step42_readback_step42_latest.json", "min_bytes": 80},
            {"glob": LOCAL + r"\销量近7日0\查在售数据_{date}_v2.csv", "min_bytes": 200, "min_records": 3},
        ],
        "json_assert": [{"file": REP + r"\report_step42_readback_step42_latest.json", "path": "verdict", "eq": "GREEN"}],
        "reconcile": {"type": "count_match",
                      "a": {"kind": "json_field", "file": REP + r"\report_step42_readback_step42_latest.json",
                            "path": "numbers.platform_total"},
                      "b": {"kind": "json_field", "file": REP + r"\report_step42_readback_step42_latest.json",
                            "path": "numbers.exported"}},
        "crosscheck": {
            "type": "export_vs_readback",
            "a": {"kind": "csv_column", "file": LOCAL + r"\销量近7日0\查在售数据_{date}_v2.csv",
                  "column": "货号", "distinct": False},
            "b": {"kind": "json_field", "file": REP + r"\report_step42_readback_step42_latest.json",
                  "path": "numbers.collected"},
        },
    },
}

LOCAL_ORDER = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
               "14", "15", "29", "34", "42"]

# 本机各步今日（判定时点）的执行小票（Temu_12steps_status.json 步号 -> 摘要）
LOCAL_STATUS_FILE = LOCAL + r"\Temu12步运行器\Temu_12steps_status.json"

# ---------------- 吴凡机 17 步（bat -> 镜像小票）----------------
# 镜像小票按 sewing 侧自身 receipt step 号命名；映射：sewing receipt step -> (bat, 名称)
SEWING_STEPS = {
    "01": {"bat": "bat1",  "name": "01. 备货 (吴凡机)"},
    "02": {"bat": "bat3",  "name": "02. 待创建 (吴凡机)"},
    "03": {"bat": "bat34", "name": "03. 2-3_14天 (吴凡机)"},
    "04": {"bat": "bat4",  "name": "04. 查违规 (吴凡机)"},
    "05": {"bat": "bat5",  "name": "05. 活动机遇 (吴凡机)"},
    "06": {"bat": "bat7",  "name": "06. 活动商品 (吴凡机)"},
    "07": {"bat": "bat8",  "name": "07. 站点库存0-1 (吴凡机)"},
    "08": {"bat": "bat9",  "name": "08. 体积重量 (吴凡机)"},
    "09": {"bat": "bat10", "name": "09. 3个月SKC (吴凡机)"},
    "10": {"bat": "bat11", "name": "10. 资质待待 (吴凡机)"},
    "11": {"bat": "bat12", "name": "11. 2-8质检 (吴凡机)"},
    "12": {"bat": "bat13", "name": "12. 商品信息待确认 (吴凡机)"},
    "14": {"bat": "bat24", "name": "品质分差下架退货 (吴凡机)"},
    "15": {"bat": "bat19", "name": "待快递揽收 (吴凡机)"},
    "29": {"bat": "bat33", "name": "在售货号 (吴凡机)"},
    "34": {"bat": "bat16", "name": "查在售数据 (吴凡机)"},
    "42": {"bat": "bat47", "name": "销量近7日0 (吴凡机)"},
}

def sewing_step_cfg(receipt_step):
    """吴凡机单步判定配置：镜像小票为产物；无新鲜度要求（离线期小票为最近一次真实执行）"""
    r = MIRROR + "\\reports"
    base = {
        "01": {"artifacts": [{"glob": r + "\\report_step01_restock_latest.json", "min_bytes": 200}],
               "json_assert": [{"file": r + "\\report_step01_restock_latest.json", "path": "succeeded", "eq": True}]},
        "02": {"artifacts": [{"glob": r + "\\report_step02_stock_latest.json", "min_bytes": 100}],
               "json_assert": [{"file": r + "\\report_step02_stock_latest.json", "path": "summary", "not_empty": True}]},
        "03": {"artifacts": [{"glob": r + "\\report_step03_final_result.json", "min_bytes": 100}],
               "json_assert": [{"file": r + "\\report_step03_final_result.json", "path": "success", "eq": True}]},
        "05": {"artifacts": [{"glob": r + "\\report_step05_last_activity_result.json", "min_bytes": 80}]},
    }
    # 其余步：标签小票 + （如有）readback 小票
    cfg = base.get(receipt_step, {})
    arts = list(cfg.get("artifacts", []))
    # 01/03 的镜像里无标签小票（restock/final_result json 已断言），标签降级为可选
    label_required = receipt_step not in ("01", "03")
    arts.append({"glob": r + ("\\report_step%s_label_report_latest.txt" % receipt_step),
                 "min_bytes": 40, "required": label_required})
    cfg["artifacts"] = arts
    cfg.setdefault("log", {
        "log_files": [r + ("\\report_step%s_label_report_latest.txt" % receipt_step)],
        "deny_log": [r"部分失败", r"有失败项"],
    })
    cfg["_fresh_sec"] = None  # 离线期小票不按新鲜度判
    return cfg


# 第5道 灰度门管控的敏感操作（资金/下架）
SENSITIVE_OPS = {
    "pccoderun": "PC码批量换物流（资金相关）",
    "skcretrun": "SKC下架+退货申请（下架敏感）",
}
