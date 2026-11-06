# -*- coding: utf-8 -*-
"""
selftest_all.py — 9 道判定组件自检（可随时回归，全部内存/临时目录，不碰真实业务）
覆盖：第1道 click_verify、第6道 故障注入、幂等、第5道灰度、禁混店拦截、黄金样本固化。
产出：data/selftest_evidence.md
"""
import os, sys, io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from temu_gates import StoreContext, write_text, BASE, now_str  # noqa: E402
import click_verify, fault_inject, idempotency, gray, golden  # noqa: E402
import gates_config as CFG  # noqa: E402

ROWS = []


def log(msg):
    print(msg)
    ROWS.append(msg)


def main():
    log("# 9 道判定组件自检 (%s)" % now_str())
    log("")
    log("## 第1道 动作回读 click_verify（抓没点上/误选中/遮挡；回读死必须抛错）")
    ok1 = click_verify.selftest(log)
    log("")
    log("## 第6道 故障注入（遮挡/改名/断网/平台拒绝/回读死 -> 不得假成功）")
    ok6 = fault_inject.selftest(log)
    log("")
    log("## 外加 幂等防重复（同条件再跑不得重复提交/下架）")
    oki = idempotency.selftest(log)
    log("")
    log("## 第5道 小批量灰度+真人首件（无 token 批量必拦）")
    okg = gray.selftest(log)
    log("")
    log("## 禁混店拦截（判定文件越店即拒）")
    local_ctx = StoreContext("store_local.json")
    sewing_ctx = StoreContext("store_sewing.json")
    results = []

    def guarded(ctx, path, expect_block):
        try:
            ctx.guard_path(path)
            blocked = False
        except Exception as e:  # noqa: BLE001
            blocked = type(e).__name__ == "MixedStoreViolation"
        return blocked == expect_block

    # 本机上下文判定吴凡机镜像文件 -> 必须拦
    r1 = guarded(local_ctx, CFG.MIRROR + r"\reports\report_step01_restock_latest.json", True)
    results.append(r1)
    log("  [%s] 本机上下文读吴凡机镜像小票 -> MixedStoreViolation" % ("PASS" if r1 else "FAIL"))
    # sewing 上下文读本机业务文件 -> 必须拦
    r2 = guarded(sewing_ctx, CFG.LOCAL + r"\查在售数据\查在售数据_2026-11-06.csv", True)
    results.append(r2)
    log("  [%s] sewing 上下文读本机查在售数据 -> MixedStoreViolation" % ("PASS" if r2 else "FAIL"))
    # 各自读自家 -> 放行
    r3 = guarded(local_ctx, CFG.LOCAL + r"\查在售数据\查在售数据_2026-11-06.csv", False)
    r4 = guarded(sewing_ctx, CFG.MIRROR + r"\reports\report_step01_restock_latest.json", False)
    results.append(r3 and r4)
    log("  [%s] 各自读自家文件 -> 放行" % ("PASS" if (r3 and r4) else "FAIL"))
    okm = all(results)

    log("")
    log("## 第4道 黄金样本固化（今天互斥清单 -> 标准答案）")
    try:
        data = golden.refresh_golden_from_files(
            local_ctx,
            CFG.LOCAL + r"\txt\在售中货号.txt",
            CFG.LOCAL + r"\txt\不在在售中不在上新.txt")
        log("  [PASS] 固化样本 %d 个（是=%d 否=%d），冻结于 %s" % (
            len(data["samples"]),
            sum(1 for s in data["samples"] if s["expect_onsale"] == "是"),
            sum(1 for s in data["samples"] if s["expect_onsale"] == "否"),
            data["frozen_at"]))
        okg4 = True
    except Exception as e:  # noqa: BLE001
        log("  [FAIL] 黄金样本固化失败: %s" % e)
        okg4 = False

    log("")
    overall = all([ok1, ok6, oki, okg, okm, okg4])
    log("## 自检总结论: %s" % ("ALL PASS" if overall else "存在 FAIL"))
    out = os.path.join(BASE, "data", "selftest_evidence.md")
    write_text(out, "\n".join(ROWS) + "\n")
    print("证据: %s" % out)
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
