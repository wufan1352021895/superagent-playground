# -*- coding: utf-8 -*-
"""
idempotency.py — 外加：幂等防重复
同条件再跑不得重复提交/下架。
账本 idem_{store}.jsonl，key = sha256(store|step|op|业务键|day)。
check_and_record(key) 返回 (allowed, prior)；已存在 -> allowed=False（跳过提交）。
"""
import os, json, hashlib, datetime

from temu_gates import BASE, DATA, now_str


class DuplicateOperation(Exception):
    """幂等闸门：同条件重复操作"""


def make_key(store, step, op, biz_key, day=None):
    day = day or datetime.date.today().isoformat()
    raw = "|".join([str(store), str(step), str(op), str(biz_key), str(day)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw


class IdemLedger:
    def __init__(self, ctx):
        self.ctx = ctx
        self.file = ctx.idem_file

    def _load(self):
        rows = []
        if os.path.exists(self.file):
            with open(self.file, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            rows.append(json.loads(ln))
                        except json.JSONDecodeError:
                            pass
        return rows

    def check_and_record(self, step, op, biz_key, day=None, meta=None):
        """返回 (allowed, prior_row_or_None)。allowed=False 表示同条件已做过，必须跳过。"""
        key, raw = make_key(self.ctx.store, step, op, biz_key, day)
        rows = self._load()
        prior = next((r for r in rows if r.get("key") == key), None)
        if prior:
            return False, prior
        row = {"ts": now_str(), "store": self.ctx.store, "key": key, "raw": raw,
               "step": str(step), "op": op, "biz_key": str(biz_key), "day": day}
        if meta:
            row["meta"] = meta
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True, None

    def reset_key(self, step, op, biz_key, day=None):
        """仅在人工明确要求重跑时使用：按 raw 前缀删除账本行"""
        _, raw = make_key(self.ctx.store, step, op, biz_key, day)
        rows = self._load()
        keep = [r for r in rows if r.get("raw") != raw]
        with open(self.file, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(rows) - len(keep)


# ---------------------------------------------------------------
# selftest：同 key 二次提交必须被拦
# ---------------------------------------------------------------
def selftest(log=print):
    import tempfile, shutil
    from temu_gates import StoreContext
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

    tmp = tempfile.mkdtemp(prefix="gates17_idem_")
    ctx = StoreContext.__new__(StoreContext)
    ctx.store = "selftest"
    ctx.store_name = "selftest"
    ctx.machine = "selftest"
    ctx.roots = [tmp]
    ctx.registry_file = os.path.join(tmp, "a.jsonl")
    ctx.verdicts_file = os.path.join(tmp, "v.jsonl")
    ctx.idem_file = os.path.join(tmp, "i.jsonl")
    try:
        led = IdemLedger(ctx)
        log("用例1 首次提交 SKU-A -> 放行")
        case("首跑放行", lambda: (lambda r: (_ for _ in ()).throw(AssertionError("应放行")) if not r[0] else None)(led.check_and_record("1", "restock_submit", "SKU-A")), False)

        log("用例2 同日同 SKU 再提交 -> 必须拦截（防重复）")
        def dup():
            allowed, prior = led.check_and_record("1", "restock_submit", "SKU-A")
            assert not allowed, "重复提交未拦截"
            assert prior, "缺先例记录"
        case("再跑拦截", dup, False)

        log("用例3 不同 SKU -> 放行")
        def other():
            allowed, _ = led.check_and_record("1", "restock_submit", "SKU-B")
            assert allowed
        case("异SKU放行", other, False)

        log("用例4 换天 -> 放行（天粒度幂等）")
        def nextday():
            allowed, _ = led.check_and_record("1", "restock_submit", "SKU-A", day="2099-01-01")
            assert allowed
        case("换天放行", nextday, False)

        log("用例5 下架动作同样幂等")
        def delist():
            a1, _ = led.check_and_record("14", "skc_delist", "SKC-1")
            a2, _ = led.check_and_record("14", "skc_delist", "SKC-1")
            assert a1 and not a2
        case("下架幂等", delist, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r)
    log("idempotency selftest: PASS %d/%d" % (passed, len(results)))
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
