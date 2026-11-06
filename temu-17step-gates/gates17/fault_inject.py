# -*- coding: utf-8 -*-
"""
fault_inject.py — 第6道 故障注入（杀假成功）
对由真实判定组件组成的模拟流水线注入四类故障：
  1) 遮挡：点击无效（click_verify 必须抛错）
  2) 断网：平台回读超时/失败（第2道必须判 无法确认，不得宣称成功）
  3) 元素改名：选择器找不到元素（不得静默跳过当成功）
  4) 平台失败：提交被平台拒绝（业务判定必须 failed）
并验证：回读恒失败时流水线必须抛错，不得输出成功。
全部为内存模拟，不触碰真实平台。
"""
from click_verify import click_and_verify, ClickNotAppliedError, ReadbackDeadError


class ElementRenamedError(Exception):
    """元素改名：选择器失配"""


class PlatformRejectError(Exception):
    """平台拒绝（如：商品处于清退中，不可创建备货单）"""


class NetworkDownError(Exception):
    """断网"""


class SimPipeline:
    """模拟流水线：click -> readback -> platform_reconcile -> verdict"""

    def __init__(self):
        self.state = {"selected": [], "cart": 0, "onsale": ["SKU-A"]}
        self.fault = None  # None | 'occluded' | 'renamed' | 'offline' | 'rejected' | 'readback_dead'

    def click(self, target):
        if self.fault == "occluded":
            return  # 被遮挡，点了没效果
        if self.fault == "renamed":
            raise ElementRenamedError("selector .btn-submit 不存在（元素已改名）")
        self.state["cart"] += 1
        self.state["selected"] = self.state["selected"] + [target]

    def read_state(self):
        if self.fault == "readback_dead":
            raise RuntimeError("readback channel dead")
        if self.fault == "offline":
            raise NetworkDownError("平台回读超时")
        return dict(self.state)

    def platform_reconcile(self, submitted):
        if self.fault == "offline":
            raise NetworkDownError("平台新增单数拉取失败")
        if self.fault == "rejected":
            raise PlatformRejectError("平台拒绝备货申请：商品处于清退中，不可创建备货单")
        # 正常：平台新增单数与提交数一致
        return {"platform_new": len(submitted), "list_delta": len(submitted)}


def run_pipeline(p, target="SKU-A"):
    """带全部门判定跑一遍；任何一道不过 -> 抛错/返回 failed，绝不输出成功"""
    # 第1道：动作回读
    click_and_verify(lambda: p.click(target), lambda: p.read_state(), label="sim-submit")
    # 第6道也覆盖第2道：平台对账
    rec = p.platform_reconcile([target])
    if rec["platform_new"] != 1:
        return {"verdict": "failed", "why": "金等式不成立"}
    return {"verdict": "verified"}


def selftest(log=print):
    results = []

    def case(name, fault, expect):
        p = SimPipeline()
        p.fault = fault
        try:
            r = run_pipeline(p)
            raised, exc, verdict = False, None, r.get("verdict")
        except Exception as e:  # noqa: BLE001
            raised, exc, verdict = True, e, None
        ok = (verdict == expect) if not expect.startswith("RAISE") else raised
        results.append(ok)
        det = ("RAISED " + type(exc).__name__ + ": " + str(exc)[:60]) if raised else ("verdict=" + str(verdict))
        log("  [%s] %s (%s) -> %s" % ("PASS" if ok else "FAIL", name, fault, det))

    log("用例1 无故障 -> verified")
    case("正常链路", None, "verified")
    log("用例2 遮挡：点了没反应 -> 第1道必须抛错")
    case("遮挡", "occluded", "RAISE")
    log("用例3 元素改名 -> 必须抛错（不许静默当成功）")
    case("元素改名", "renamed", "RAISE")
    log("用例4 断网：平台回读失败 -> 必须抛错")
    case("断网", "offline", "RAISE")
    log("用例5 平台拒绝 -> 必须抛错")
    case("平台拒绝", "rejected", "RAISE")
    log("用例6 回读恒失败 -> 必须抛错（杀假成功）")
    case("回读恒失败", "readback_dead", "RAISE")

    # 用例7：假成功样本（旧逻辑只看业务返回）必须被第1道识破
    log("用例7 业务自报成功但页面没变 -> 识破假成功")
    p = SimPipeline()
    p.fault = "occluded"
    try:
        ok_fake = True
        try:
            run_pipeline(p)
        except ClickNotAppliedError:
            ok_fake = False
        results.append(not ok_fake)
        log("  [%s] 假成功被识破 -> %s" % ("PASS" if not ok_fake else "FAIL",
                                          "ClickNotAppliedError" if not ok_fake else "误报成功!"))
    except Exception as e:  # noqa: BLE001
        results.append(False)
        log("  [FAIL] 用例7 异常 %s" % e)

    passed = sum(1 for r in results if r)
    log("fault_inject selftest: PASS %d/%d" % (passed, len(results)))
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
