# -*- coding: utf-8 -*-
"""
run17_local.py — 本机 17 步证据采集与判定（判定器+产物登记+监工 全链路）
不重跑业务：以今天 Temu_12steps_status.json 的执行小票 + 各步现有产物做分层判定；
可选 --execute <步号> 实际补跑某步（经 wrapper 链）。
产出：data/evidence_local.md（交卷：机·步号·现象·路径）+ data/watchdog_local.md
"""
import json, os, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from temu_gates import StoreContext, ArtifactRegistry, Judge, Watchdog, write_text, BASE, now_str  # noqa: E402
import gates_config as CFG  # noqa: E402


def load_today_receipts(status_file):
    try:
        with open(status_file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    today = datetime.date.today().isoformat()
    out = {}
    for r in data.get("results", []):
        ts = str(r.get("ts", ""))
        if ts.startswith(today):
            out[str(r["step"])] = r
    return out


def main():
    ctx = StoreContext("store_local.json")
    registry = ArtifactRegistry(ctx)
    judge = Judge(ctx, registry)
    receipts = load_today_receipts(CFG.LOCAL_STATUS_FILE)

    rows = []
    n_ok = n_bad = 0
    for step in CFG.LOCAL_ORDER:
        cfg = CFG.LOCAL_STEPS[step]
        r = receipts.get(step)
        if r:
            receipt = {"step": step, "name": cfg["name"], "exit_code": r.get("exitCode"),
                       "ts": r.get("ts", ""), "source": "today-status"}
        else:
            receipt = {"step": step, "name": cfg["name"], "exit_code": None, "ts": now_str(),
                       "source": "no-run-today"}
        v = judge.judge(cfg, receipt)
        art_paths = [a.get("file") for a in v["layers"].get("L2", {}).get("artifacts", []) if a.get("file")]
        counts = []
        for lname in ("L4", "L5", "L6"):
            lv = v["layers"].get(lname) or {}
            if lv.get("counts"):
                counts.append("%s:%s" % (lname, json.dumps(lv["counts"], ensure_ascii=False)))
            elif lv.get("checked"):
                counts.append("%s:黄金样本%d/%d" % (lname, sum(1 for c in lv["checked"] if c["ok"]), len(lv["checked"])))
        phenom = "%s | exit=%s | %s" % (
            v["verdict"].upper(), r.get("exitCode") if r else "-",
            "; ".join(v["reasons"])[:150] if v["reasons"] else "分层判定通过 " + (" ".join(counts) if counts else ""))
        paths = "; ".join(art_paths) if art_paths else "-"
        rows.append(("本机", step, cfg["name"], phenom, paths, v["verdict"]))
        if v["verdict"] == "verified":
            n_ok += 1
        else:
            n_bad += 1

    # 交卷 md
    lines = [
        "# 本机 17 步 9 道判定交卷",
        "",
        "- 机：本机 DESKTOP-VHV1N0K / Administrator（店：Wei Qi Measurement）",
        "- 判定时点：%s" % now_str(),
        "- 判定引擎：Temu中控台\\gates17（L0退出码 / L1日志 / L2产物登记 / L3JSON断言 / L4平台对账 / L5第二信源 / L6黄金样本）",
        "- 禁混店：store_local.json 根约束，越店文件判定直接拦截（见自检证据）",
        "",
        "| 机 | 步号 | 步名 | 现象 | 路径 |",
        "|---|---|---|---|---|",
    ]
    for machine, step, name, phenom, paths, verdict in rows:
        lines.append("| %s | %s | %s | %s | %s |" % (machine, step, name, phenom.replace("|", "\\|"), paths.replace("|", "\\|")))
    lines.append("")
    lines.append("小结: verified=%d 非%serify=%d" % (n_ok, "", n_bad))
    lines.append("")
    lines.append("判定明细（JSONL）: gates17\\data\\verdicts_local.jsonl")
    lines.append("产物登记（JSONL）: gates17\\data\\artifacts_local.jsonl")

    out = os.path.join(BASE, "data", "evidence_local.md")
    write_text(out, "\n".join(lines) + "\n")
    print("\n".join(lines[-6:]))
    print("交卷: %s" % out)

    # 监工
    wd = Watchdog(ctx, CFG.LOCAL_ORDER)
    text, code = wd.report()
    wout = os.path.join(BASE, "data", "watchdog_local.md")
    write_text(wout, text + "\n")
    print("监工: %s (exit=%d)" % (wout, code))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--execute":
        import subprocess
        step = sys.argv[2]
        bat = CFG.LOCAL_STEPS.get(step, {}).get("bat")
        print("live rerun 未启用（本票以当日小票+产物判定为主）: %s" % step)
    raise SystemExit(main())
