# -*- coding: utf-8 -*-
"""
reconcile.py — 第2道 平台回读/业务对账（最硬）
绕开脚本自述，从平台业务事实对账：
  - restock_gold_equation: 提交数 = 平台新增备货单数 = 列表减少数，对不上即错
  - delist_onsale_absence: 下架后用商品列表/在售接口回读，确认不在在售
  - count_match: 通用计数对账（提交数 vs 平台记录数）
数据源全部走 StoreContext.guard_path（禁混店）。
"""
import json, re, csv, io, os

from temu_gates import render, _json_get


def _load_json_file(ctx, path):
    gp = ctx.guard_path(render(path))
    with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
        return json.load(f), gp


def _load_lines(ctx, path):
    gp = ctx.guard_path(render(path))
    with open(gp, "r", encoding="utf-8", errors="replace") as f:
        return [ln.strip() for ln in f if ln.strip()], gp


def _load_csv_column(ctx, path, column):
    gp = ctx.guard_path(render(path))
    with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [], gp
    key = column if column in rows[0] else None
    if key is None:
        # 容错：中文列名匹配
        for k in rows[0]:
            if column in k:
                key = k
                break
    if key is None:
        raise KeyError("CSV %s 无列 %s" % (gp, column))
    return [r[key] for r in rows if r.get(key)], gp


def restock_gold_equation(ctx, cfg):
    """金等式：提交数 == 平台新增单数 == 列表减少数"""
    out = {"layer": "L4", "type": "restock_gold_equation", "ok": None}
    # A) 脚本提交数：从提交报告取
    rep, rep_path = _load_json_file(ctx, cfg["submitted_report"])
    submitted, err = _json_get(rep, cfg.get("submitted_path", "submitted"))
    if err:
        out["ok"] = None
        out["note"] = "提交报告读取失败: %s" % err
        return out
    n_submit = len(submitted or [])
    # B) 平台新增备货单数：待备货列表/备货单记录（第二道用平台事实）
    plat_records = cfg.get("platform_records")
    n_plat = None
    if plat_records:
        obj, gp = _load_json_file(ctx, plat_records["file"])
        val, err = _json_get(obj, plat_records.get("path", ""))
        if not err and isinstance(val, list):
            n_plat = len(val)
    # C) 列表减少数：备货前后同一列表快照对比
    n_delta = None
    if cfg.get("list_before") and cfg.get("list_after"):
        lb, _ = _load_lines(ctx, cfg["list_before"]) if cfg["list_before"].endswith(".txt") else (None, None)
        la, _ = _load_lines(ctx, cfg["list_after"])
        if lb is not None:
            n_delta = len(lb) - len(la)
    out["counts"] = {"submitted": n_submit, "platform_new": n_plat, "list_delta": n_delta}
    checks = [("submitted", n_submit)]
    have_any = n_submit is not None
    if n_plat is not None:
        checks.append(("platform_new", n_plat))
    if n_delta is not None:
        checks.append(("list_delta", n_delta))
    if len(checks) >= 2 and len(set(v for _, v in checks)) == 1:
        out["ok"] = True
        out["note"] = "金等式成立: " + " = ".join("%s=%d" % c for c in checks)
    elif n_plat is None and n_delta is None:
        out["ok"] = None
        out["note"] = "平台侧事实源未配置，本次只登记提交数=%d（不宣称 verified）" % n_submit
    else:
        out["ok"] = False
        out["note"] = "金等式不成立: " + ", ".join("%s=%s" % c for c in checks)
    out["submitted_report"] = rep_path
    return out


def delist_onsale_absence(ctx, cfg):
    """下架后回读在售列表：被下架的 SKU 必须不在在售"""
    out = {"layer": "L4", "type": "delist_onsale_absence", "ok": None}
    onsale, gp = _load_csv_column(ctx, cfg["onsale_list"], cfg.get("column", "货号"))
    targets = cfg.get("delisted_skus") or []
    src = cfg.get("delisted_from")
    if src:
        obj, _ = _load_json_file(ctx, src["file"]) if src["file"].endswith(".json") else (None, None)
        if obj is not None:
            val, err = _json_get(obj, src.get("path", ""))
            if not err and isinstance(val, list):
                targets = [t.get("skc") or t.get("sku") or str(t) if isinstance(t, dict) else str(t) for t in val]
    if not targets:
        out["ok"] = None
        out["note"] = "无被下架 SKU（本次无下架动作）"
        return out
    still = [t for t in targets if t in onsale]
    out["counts"] = {"delisted": len(targets), "still_onsale": len(still), "onsale_file": gp}
    if still:
        out["ok"] = False
        out["note"] = "下架后在售列表仍见: %s" % ",".join(still[:5])
    else:
        out["ok"] = True
        out["note"] = "平台回读确认 %d 个 SKU 已全部不在在售" % len(targets)
    return out


def count_match(ctx, cfg):
    """通用：a_count == b_count（±tolerance）"""
    out = {"layer": "L4", "type": "count_match", "ok": None}
    from crosscheck import _count_from
    a = _count_from(ctx, cfg["a"])
    b = _count_from(ctx, cfg["b"])
    tol = int(cfg.get("tolerance", 0))
    out["counts"] = {"a": a, "b": b}
    if a is None or b is None:
        out["ok"] = None
        out["note"] = "计数源缺失"
        return out
    if abs(a - b) <= tol:
        out["ok"] = True
        out["note"] = "对账成立: %d == %d" % (a, b)
    else:
        out["ok"] = False
        out["note"] = "对账不成立: %d != %d" % (a, b)
    return out


_TYPES = {
    "restock_gold_equation": restock_gold_equation,
    "delist_onsale_absence": delist_onsale_absence,
    "count_match": count_match,
}


def run_reconcile(ctx, cfg):
    t = cfg.get("type")
    fn = _TYPES.get(t)
    if not fn:
        return {"layer": "L4", "type": t, "ok": None, "note": "未知对账类型 %s" % t}
    return fn(ctx, cfg)
