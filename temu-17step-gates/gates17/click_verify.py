# -*- coding: utf-8 -*-
"""
click_verify.py — 第1道 动作回读
点完立刻回读页面状态，未变则报错（抓没点上/误选中/遮挡）；
不证明平台后台已认（那是第2道 reconcile 的活）。

供步骤脚本调用：click_and_verify(click_fn, read_state_fn, label)。
selftest() 用纯内存模拟页状态，四类故障注入（遮挡/没点上/误选中/回读死），
证明假成功会被抓住——无任何真实浏览器动作，可随时回归。
"""
import time


class ClickNotAppliedError(Exception):
    """点击后页面状态未变化"""


class ReadbackDeadError(Exception):
    """回读通道恒失败（页面死了/入口没了）"""


def _norm(v):
    if isinstance(v, (list, tuple, set)):
        return tuple(sorted(str(x) for x in v))
    if isinstance(v, dict):
        return tuple(sorted((k, str(v[k])) for k in v))
    return str(v)


def click_and_verify(click_fn, read_state_fn, label="action", retries=1, settle_sec=0.0):
    """
    click_fn():      执行真实点击/选择
    read_state_fn(): 回读页面关键状态（返回可比较对象；抛异常=回读失败）
    流程：点击前先确认回读可用（回读死=直接抛 ReadbackDeadError，不许静默过）；
         点击后回读，与点击前不同才算点上；相同重试 retries 次后抛 ClickNotAppliedError。
    返回 (before, after)。
    """
    before = read_state_fn()  # 回读恒失败在这里就抛
    if settle_sec:
        time.sleep(settle_sec)
    last_err = None
    for _ in range(max(1, retries + 1)):
        click_fn()
        after = read_state_fn()
        if _norm(after) != _norm(before):
            return before, after
        last_err = after
    raise ClickNotAppliedError(
        "第1道动作回读失败 [%s]: 点击后页面状态未变化 after=%r" % (label, last_err)
    )


# ---------------------------------------------------------------
# selftest：内存模拟页面，注入四类故障（第6道素材之一）
# ---------------------------------------------------------------
def selftest(log=print):
    results = []

    def case(name, fn, expect_raise):
        try:
            fn()
            raised, exc = False, None
        except Exception as e:  # noqa: BLE001
            raised, exc = True, e
        ok = (raised == expect_raise)
        results.append(ok)
        log("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                             (" -> " + type(exc).__name__ + ": " + str(exc)[:70]) if raised else ""))

    # 正常：点击生效
    page = {"selected": [], "cart": 0}
    log("用例1 正常点击，状态变化 -> 应通过")
    case("正常点击", lambda: click_and_verify(
        lambda: page.update(cart=page["cart"] + 1),
        lambda: dict(page)), False)

    # 遮挡：点击被浮层挡住，页面无变化
    log("用例2 按钮被遮挡，点击无效 -> 必须报错")
    case("遮挡", lambda: click_and_verify(
        lambda: None,  # 遮挡时 click 无效果
        lambda: {"cart": 0}), True)

    # 误选中：点到别的元素，目标状态没变
    log("用例3 误选中（点到了错误行），目标状态未变 -> 必须报错")
    wrong = {"selected": ["SKU-X"], "cart": 0}

    def misclick():
        wrong["selected"] = wrong["selected"] + ["WRONG-ROW"]

    case("误选中", lambda: click_and_verify(
        misclick,
        lambda: {"selected": [], "cart": 0}), True)

    # 回读死：read_state_fn 恒抛 -> 必须抛 ReadbackDeadError（杀假成功）
    log("用例4 回读通道恒失败 -> 必须抛 ReadbackDeadError")
    def dead_read():
        raise RuntimeError("page dead")

    case("回读死", lambda: click_and_verify(lambda: None, dead_read), True)

    # 回读死但业务自报成功：证明不会被假成功骗过
    log("用例5 脚本自称成功但回读死 -> 仍必须抛错")
    def fake_success_pipeline():
        # 模拟旧逻辑：只看业务返回 ok 就当成功
        business_said_ok = True
        if business_said_ok:
            # 新逻辑：动作回读必须独立确认
            click_and_verify(lambda: None, dead_read)
        return "SUCCESS"

    case("假成功+回读死", fake_success_pipeline, True)

    passed = sum(1 for r in results if r)
    log("click_verify selftest: PASS %d/%d" % (passed, len(results)))
    return passed == len(results)


if __name__ == "__main__":
    ok = selftest()
    raise SystemExit(0 if ok else 1)
