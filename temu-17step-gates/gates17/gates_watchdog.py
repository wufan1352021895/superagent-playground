# -*- coding: utf-8 -*-
"""
gates_watchdog.py — 监工 CLI（第3组件）
python gates_watchdog.py --store local|sewing [--out <md路径>]
退出码：0=17步全 verified；1=存在非 verified/缺记录。
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from temu_gates import StoreContext, Watchdog, write_text, BASE  # noqa: E402
import gates_config as CFG  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="local", choices=["local", "sewing"])
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    ctx = StoreContext("store_local.json" if a.store == "local" else "store_sewing.json")
    order = CFG.LOCAL_ORDER if a.store == "local" else list(CFG.SEWING_STEPS.keys())
    wd = Watchdog(ctx, order)
    text, code = wd.report()
    print(text)
    out = a.out or os.path.join(BASE, "data", "watchdog_%s.md" % ctx.store)
    write_text(out, text + "\n")
    print("报告: %s 退出码=%d" % (out, code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
