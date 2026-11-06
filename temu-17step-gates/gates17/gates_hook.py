# -*- coding: utf-8 -*-
"""
gates_hook.py — 17 步 wrapper 的判定钩子（判定器+产物登记 落地点）
用法（由 install_gates_hooks.ps1 注入 wrapper，业务跑完后调用）：
  python gates_hook.py --store local --step <N> --name "<步名>" --exitcode <ec> \
      [--rundir <wrapper RunDir>] [--logfile <额外日志>]
行为：
  1) 产物登记：按 gates_config 找到该步产物 -> sha256 登记 artifacts_{store}.jsonl
  2) 判定：Judge 分层判定 -> verdicts_{store}.jsonl
  3) 强制：verdict=failed 时本钩子以退出码 3 结束（wrapper 将业务退出码改写为非 0，
     杀掉「进程 0 但业务已坏」的假成功）；verdict!=failed 时透传业务退出码。
  4) 钩子自身异常：记录 hook_error，退出码透传业务码（不拦正常业务，但监工可见）。
"""
import argparse, json, sys, os, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from temu_gates import StoreContext, ArtifactRegistry, Judge, now_str, write_text  # noqa: E402
import gates_config as CFG  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="local", choices=["local", "sewing"])
    ap.add_argument("--step", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--exitcode", type=int, default=0)
    ap.add_argument("--rundir", default="")
    ap.add_argument("--logfile", default="")
    ap.add_argument("--evidence", default="", help="追加写证据行(md)的路径")
    a = ap.parse_args()

    store_file = "store_local.json" if a.store == "local" else "store_sewing.json"
    ctx = StoreContext(store_file)
    registry = ArtifactRegistry(ctx)
    judge = Judge(ctx, registry)

    cfg = (CFG.LOCAL_STEPS if a.store == "local" else {}).get(str(a.step), {})
    receipt = {
        "step": a.step,
        "name": a.name or cfg.get("name", ""),
        "exit_code": a.exitcode,
        "ts": now_str(),
        "source": "wrapper-hook",
    }
    if a.rundir:
        runlog = os.path.join(a.rundir, "run.log")
        if os.path.isfile(runlog):
            receipt["log_files"] = [runlog]
    if a.logfile and os.path.isfile(a.logfile):
        receipt.setdefault("log_files", []).append(a.logfile)

    try:
        v = judge.judge(cfg, receipt)
        print("[gates-hook] step=%s verdict=%s reasons=%s" % (a.step, v["verdict"], "; ".join(v["reasons"])[:160]))
        hook_err = None
    except Exception as e:  # noqa: BLE001
        v = {"ts": now_str(), "store": ctx.store, "step": a.step, "name": a.name,
             "verdict": "unverified", "reasons": ["hook_error: %s" % e], "layers": {}, "trigger": "wrapper-hook"}
        hook_err = traceback.format_exc()[-500:]
        print("[gates-hook][ERROR] %s" % e)
        with open(ctx.verdicts_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    if a.evidence:
        row = "- 步 %s %s | 判定=%s | %s | 证据=%s\n" % (
            a.step, receipt["name"], v["verdict"],
            ("; ".join(v.get("reasons", []))[:120] or "分层判定通过"),
            v.get("layers", {}).get("L2", {}).get("artifacts", []) and
            "; ".join(x.get("file", "") for x in v["layers"]["L2"]["artifacts"] if x.get("file")) or "-")
        try:
            with open(a.evidence, "a", encoding="utf-8") as f:
                f.write(row)
        except OSError:
            pass

    if hook_err is None and v["verdict"] == "failed":
        # 第1组件判定器强制：业务失败不得以 0 收场（杀假成功）
        return 3
    return a.exitcode


if __name__ == "__main__":
    raise SystemExit(main())
