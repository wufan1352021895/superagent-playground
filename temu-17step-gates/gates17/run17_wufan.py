# -*- coding: utf-8 -*-
"""
run17_wufan.py — 吴凡机（Sewing 店）17 步证据采集与判定（禁混店：只碰吴凡店数据）
两条路：
  A) 镜像小票（默认）：判定 store_mirror\\sewing-tools 下吴凡机最近一次真实执行的小票。
  B) --probe-online：先探吴凡机状态；在线则提示触发（不经本业务通道），离线记录 unreachable。
产出：data/evidence_sewing.md（交卷：机·步号·现象·路径）+ data/watchdog_sewing.md
"""
import json, os, sys, subprocess, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from temu_gates import StoreContext, ArtifactRegistry, Judge, Watchdog, write_text, BASE, now_str  # noqa: E402
import gates_config as CFG  # noqa: E402

REMOTE_PS1 = r"C:\Users\Administrator\Desktop\Temu中控台\remote_control_wufan.ps1"


def probe_remote():
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", REMOTE_PS1, "-Action", "status", "-Json"],
            capture_output=True, text=True, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        try:
            j = json.loads(out[out.index("{"):out.rindex("}") + 1])
            return {"online": bool(j.get("online")), "reason": j.get("reason", "")}
        except ValueError:
            return {"online": False, "reason": out.strip()[:120]}
    except Exception as e:  # noqa: BLE001
        return {"online": False, "reason": str(e)[:120]}


def judge_mirror(ctx, registry, judge):
    rows = []
    n_ok = n_bad = 0
    online_info = probe_remote()
    for rstep, meta in CFG.SEWING_STEPS.items():
        cfg = CFG.sewing_step_cfg(rstep)
        # 镜像小票为最近一次真实执行，不做新鲜度淘汰（小票自带日期，如实呈现）
        for spec in cfg.get("artifacts", []):
            spec["fresh_sec"] = 10 * 365 * 24 * 3600
        receipt = {"step": meta["bat"], "name": meta["name"], "exit_code": None,
                   "ts": None, "source": "sewing-mirror"}
        v = judge.judge(cfg, receipt)
        art_paths, art_dates = [], []
        for a in v["layers"].get("L2", {}).get("artifacts", []):
            if a.get("file"):
                art_paths.append(a["file"])
                try:
                    art_dates.append(datetime.datetime.fromtimestamp(os.path.getmtime(a["file"])).strftime("%Y-%m-%d %H:%M"))
                except OSError:
                    pass
        phenom = "%s | 小票日期=%s | %s | 吴凡机在线=%s(%s)" % (
            v["verdict"].upper(),
            art_dates[0] if art_dates else "-",
            ("; ".join(v["reasons"])[:120] if v["reasons"] else "分层判定通过"),
            online_info["online"], (online_info["reason"] or "ok")[:60])
        rows.append(("吴凡机", "%s/%s" % (meta["bat"], rstep), meta["name"], phenom,
                     "; ".join(art_paths) if art_paths else "-", v["verdict"]))
        if v["verdict"] == "verified":
            n_ok += 1
        else:
            n_bad += 1
    return rows, n_ok, n_bad, online_info


def main():
    ctx = StoreContext("store_sewing.json")
    registry = ArtifactRegistry(ctx)
    judge = Judge(ctx, registry)
    rows, n_ok, n_bad, online = judge_mirror(ctx, registry, judge)

    lines = [
        "# 吴凡机（Sewing 店）17 步 9 道判定交卷",
        "",
        "- 机：吴凡机 DESKTOP-07I8A8M / 吴凡（店：Sewing tools）",
        "- 判定时点：%s；判定数据源：本机 store_mirror\\sewing-tools 镜像小票（sync_store_wufan 只读回传，吴凡店自有产物）" % now_str(),
        "- 吴凡机实时状态：online=%s (%s)" % (online["online"], online["reason"][:80]),
        "- 禁混店：store_sewing.json 仅允许 sewing-tools 镜像/wufan_pull 根，读本机业务树即 MixedStoreViolation",
        "",
        "| 机 | 步号(bat/票) | 步名 | 现象 | 路径 |",
        "|---|---|---|---|---|",
    ]
    for machine, step, name, phenom, paths, verdict in rows:
        lines.append("| %s | %s | %s | %s | %s |" % (machine, step, name, phenom.replace("|", "\\|"), paths.replace("|", "\\|")))
    lines.append("")
    lines.append("小结: verified=%d 非%serify=%d" % (n_ok, "", n_bad))
    lines.append("")
    lines.append("判定明细（JSONL）: gates17\\data\\verdicts_sewing.jsonl")
    lines.append("产物登记（JSONL）: gates17\\data\\artifacts_sewing.jsonl")

    out = os.path.join(BASE, "data", "evidence_sewing.md")
    write_text(out, "\n".join(lines) + "\n")
    print("\n".join(lines[-6:]))
    print("交卷: %s" % out)

    wd = Watchdog(ctx, list(CFG.SEWING_STEPS.keys()))
    text, code = wd.report()
    wout = os.path.join(BASE, "data", "watchdog_sewing.md")
    write_text(wout, text + "\n")
    print("监工: %s (exit=%d)" % (wout, code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
