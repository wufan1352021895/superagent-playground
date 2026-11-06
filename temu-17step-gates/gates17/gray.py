# -*- coding: utf-8 -*-
"""
gray.py — 第5道 小批量灰度+真人首件
资金/下架类动作（pc码批量换物流、SKC下架+退货申请）：
  1) 先真做 1 条（first-piece），产出 人工核对清单；
  2) 人工核对确认后写入 token，才允许放开批量；
  3) 无 token 的批量一律 GrayBlocked，绝不误放行。
只做闸门与登记，不执行任何真实资金/下架动作。
"""
import os, json, hashlib, datetime

from temu_gates import BASE, DATA, now_str, write_text


class GrayBlocked(Exception):
    """第5道灰度闸门：无真人首件确认，禁止批量"""


TOKEN_DIR = os.path.join(DATA, "tokens")


def _token_path(op, day):
    return os.path.join(TOKEN_DIR, "%s_%s.token" % (op, day))


def require_batch_token(op, day=None):
    """批量放行闸门：必须有真人核对 token，否则抛 GrayBlocked"""
    day = day or datetime.date.today().isoformat()
    p = _token_path(op, day)
    if not os.path.exists(p):
        raise GrayBlocked(
            "第5道灰度拦截 [%s]: 无真人首件确认 token (%s)。先跑 first_piece 1 条并人工核对。"
            % (op, p)
        )
    with open(p, "r", encoding="utf-8") as f:
        tok = json.load(f)
    if tok.get("op") != op or tok.get("day") != day or not tok.get("confirmed_by"):
        raise GrayBlocked("第5道灰度拦截 [%s]: token 无效（字段缺失/日期不符）" % op)
    return tok


def first_piece(op, params, executor=None):
    """
    真做首件：executor(params_first) 只允许 1 条；
    产出人工核对清单（供人确认后写 token）。executor 为 None 时仅产出核对清单（演练模式）。
    """
    day = datetime.date.today().isoformat()
    if not params:
        raise ValueError("first_piece 需要首件参数")
    items = params if isinstance(params, list) else [params]
    if len(items) != 1:
        raise GrayBlocked("第5道灰度拦截 [%s]: 首件必须恰好 1 条，收到 %d 条" % (op, len(items)))
    result = None
    if executor is not None:
        result = executor(items[0])
    checklist = {
        "ts": now_str(),
        "op": op,
        "day": day,
        "first_item": items[0],
        "executor_result": result,
        "human_action": "人工核对首件结果后，运行: python gray.py confirm --op %s --day %s --by <姓名> 放开批量" % (op, day),
    }
    cp = os.path.join(TOKEN_DIR, "checklist_%s_%s.json" % (op, day))
    write_text(cp, json.dumps(checklist, ensure_ascii=False, indent=2))
    return checklist


def confirm(op, by, day=None):
    """人工核对确认 -> 写 token（批量才被放行）"""
    day = day or datetime.date.today().isoformat()
    os.makedirs(TOKEN_DIR, exist_ok=True)
    tok = {"op": op, "day": day, "confirmed_by": by, "ts": now_str(),
           "first_piece_checklist": "checklist_%s_%s.json" % (op, day)}
    write_text(_token_path(op, day), json.dumps(tok, ensure_ascii=False, indent=2))
    return tok


def revoke(op, day=None):
    """撤回放行（发现异常时用）"""
    day = day or datetime.date.today().isoformat()
    p = _token_path(op, day)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


# ---------------------------------------------------------------
# selftest：证明「无 token 批量必被拦、有 token 才放行」，全程不碰真实业务
# ---------------------------------------------------------------
def selftest(log=print):
    import tempfile, shutil
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

    # 用隔离 TOKEN_DIR，不污染真实 token
    global TOKEN_DIR
    old_dir = TOKEN_DIR
    TOKEN_DIR = os.path.join(tempfile.mkdtemp(prefix="gates17_gray_"), "tokens")
    try:
        day = "2099-12-31"
        log("用例1 无 token 直接批量 -> 必须被拦")
        case("批量无token", lambda: require_batch_token("pccoderun", day), True)

        log("用例2 首件>1 条 -> 必须被拦")
        case("首件多条", lambda: first_piece("pccoderun", ["PC1", "PC2"]), True)

        log("用例3 首件 1 条（演练模式）-> 产出人工核对清单")
        def ok_first():
            cl = first_piece("pccoderun", {"pc": "PC2610050017460", "day": "tomorrow"})
            assert cl["human_action"], "清单缺人工动作"
        case("首件1条", ok_first, False)

        log("用例4 有人工核对清单但未 confirm，批量仍被拦")
        case("清单≠token", lambda: require_batch_token("pccoderun", day), True)

        log("用例5 人工 confirm 后批量放行")
        def ok_confirm():
            confirm("pccoderun", "真人-吴凡", day)
            tok = require_batch_token("pccoderun", day)
            assert tok["confirmed_by"] == "真人-吴凡"
        case("confirm后放行", ok_confirm, False)

        log("用例6 撤回 token 后批量再次被拦")
        def ok_revoke():
            assert revoke("pccoderun", day)
            require_batch_token("pccoderun", day)
        case("撤回后拦截", ok_revoke, True)
    finally:
        shutil.rmtree(os.path.dirname(TOKEN_DIR), ignore_errors=True)
        TOKEN_DIR = old_dir

    passed = sum(1 for r in results if r)
    log("gray selftest: PASS %d/%d" % (passed, len(results)))
    return passed == len(results)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "confirm":
        # python gray.py confirm --op pccoderun --by 姓名 [--day 2026-11-06]
        op = by = None
        day = None
        args = sys.argv[2:]
        for i, a in enumerate(args):
            if a == "--op":
                op = args[i + 1]
            elif a == "--by":
                by = args[i + 1]
            elif a == "--day":
                day = args[i + 1]
        print(json.dumps(confirm(op, by, day), ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        raise SystemExit(0 if selftest() else 1)
    else:
        raise SystemExit(0 if selftest() else 1)
