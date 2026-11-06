# -*- coding: utf-8 -*-
"""
golden.py — 第4道 黄金样本+回归
固定已知答案商品，脚本改完必跑，结果须与标准答案一致。
类型 sku_classification：黄金货号在流水线产物(查在售数据 CSV)里的「在售」值必须等于标准答案。
标准答案存 golden_samples_{store}.json，由产物登记快照固化。
"""
import json, csv, os

from temu_gates import render, BASE, DATA, write_text


def load_samples(store):
    p = os.path.join(DATA, "golden_samples_%s.json" % store)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def run_golden(ctx, cfg):
    out = {"layer": "L6", "type": cfg.get("type", "sku_classification")}
    samples = cfg.get("samples")
    if not samples:
        s = load_samples(ctx.store)
        if not s:
            out["ok"] = None
            out["note"] = "无黄金样本文件"
            return out
        samples = s.get("samples", [])
    csvf = cfg.get("csv")
    if not csvf:
        out["ok"] = None
        out["note"] = "未指定流水线产物 CSV"
        return out
    gp = ctx.guard_path(render(csvf))
    rows = {}
    with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            huohao = (r.get("货号") or r.get("SKU") or "").strip()
            if huohao:
                rows[huohao] = r
    checked = []
    bad = []
    for s in samples:
        sku = str(s["sku"]).strip()
        expect = str(s["expect_onsale"])
        row = rows.get(sku)
        got = (row.get("在售") or "").strip() if row else "缺席"
        # 期望在售=是：必须出现在产物且标「是」；
        # 期望在售=否：不得被流水线判成「是」（缺席/否 都算对）。
        ok = (got == expect) if expect == "是" else (got != "是")
        checked.append({"sku": sku, "expect": expect, "got": got, "ok": ok})
        if not ok:
            bad.append("%s(期望%s 实得%s)" % (sku, expect, got))
    out["checked"] = checked
    if bad:
        out["ok"] = False
        out["note"] = "黄金样本不符: " + "; ".join(bad[:5])
    else:
        out["ok"] = True
        out["note"] = "黄金样本 %d/%d 全部与标准答案一致" % (len(checked), len(checked))
    return out


def refresh_golden_from_files(ctx, onsale_txt, offsale_txt, out_name=None):
    """从两份互斥清单固化黄金样本（登记快照），并返回样本 dict"""
    on, p1 = [], render(onsale_txt)
    off, p2 = [], render(offsale_txt)
    for p, bucket in ((p1, on), (p2, off)):
        ctx.guard_path(p)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            bucket.extend(ln.strip() for ln in f if ln.strip())
    on, off = sorted(set(on)), sorted(set(off))
    overlap = set(on) & set(off)
    if overlap:
        raise ValueError("黄金样本两清单重叠: %s" % overlap)
    data = {
        "frozen_at": __import__("temu_gates").now_str(),
        "store": ctx.store,
        "sources": {"onsale": p1, "offsale": p2},
        "samples": ([{"sku": s, "expect_onsale": "是"} for s in on[:6]] +
                    [{"sku": s, "expect_onsale": "否"} for s in off[:4]]),
    }
    out_name = out_name or ("golden_samples_%s.json" % ctx.store)
    write_text(os.path.join(DATA, out_name), json.dumps(data, ensure_ascii=False, indent=2))
    return data
