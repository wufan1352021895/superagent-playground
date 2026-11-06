# -*- coding: utf-8 -*-
"""
crosscheck.py — 第3道 独立第二信源交叉
换入口/API/导出再数一遍，两信源一致才信；不一致 -> suspect（判 False）。
计数源类型：
  {"kind":"csv_column","file":...,"column":"货号","distinct":true}
  {"kind":"txt_lines","file":...,"match":regex可选,"distinct":true}
  {"kind":"json_len","file":...,"path":"a.b"}
  {"kind":"json_field","file":...,"path":"x"}   # 值本身是数字
"""
import json, re, csv

from temu_gates import render


def _count_from(ctx, src):
    gp = ctx.guard_path(render(src["file"]))
    kind = src.get("kind")
    if kind == "csv_column":
        with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.DictReader(f))
        col = None
        if rows:
            col = src["column"] if src["column"] in rows[0] else None
            if col is None:
                for k in rows[0]:
                    if src["column"] in k:
                        col = k
                        break
        if col is None:
            return None
        vals = [r[col] for r in rows if r.get(col)]
        if src.get("filter"):
            vals = [v for v in vals if re.search(src["filter"], str(v))]
        if src.get("distinct", True):
            vals = set(vals)
        return len(vals)
    if kind == "txt_lines":
        with open(gp, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if src.get("match"):
            rx = re.compile(src["match"])
            lines = [ln for ln in lines if rx.search(ln)]
        if src.get("skip_header"):
            lines = lines[1:]
        if src.get("distinct", True):
            lines = set(lines)
        return len(lines)
    if kind in ("json_len", "json_field"):
        with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
            obj = json.load(f)
        from temu_gates import _json_get
        val, err = _json_get(obj, src.get("path", ""))
        if err:
            return None
        if kind == "json_len" and isinstance(val, (list, dict)):
            return len(val)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    raise ValueError("未知计数源 kind=%s" % kind)


def run_crosscheck(ctx, cfg):
    out = {"layer": "L5", "type": cfg.get("type", "two_sources")}
    a = b = None
    err = []
    for tag, src in (("a", cfg["a"]), ("b", cfg["b"])):
        try:
            v = _count_from(ctx, src)
            if tag == "a":
                a = v
            else:
                b = v
            if v is None:
                err.append("%s 源计数失败: %s" % (tag, src["file"]))
        except Exception as e:  # noqa: BLE001
            err.append("%s 源异常: %s" % (tag, str(e)[:80]))
    out["counts"] = {"a": a, "b": b}
    out["sources"] = {"a": cfg["a"]["file"], "b": cfg["b"]["file"]}
    if a is None or b is None:
        out["ok"] = None
        out["note"] = "; ".join(err) or "计数缺失"
        return out
    tol = int(cfg.get("tolerance", 0))
    if abs(a - b) <= tol:
        out["ok"] = True
        out["note"] = "两信源一致: %d == %d" % (a, b)
    else:
        out["ok"] = False
        out["note"] = "两信源不一致: %d != %d (tol=%d)" % (a, b, tol)
    return out
