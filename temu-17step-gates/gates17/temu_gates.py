# -*- coding: utf-8 -*-
"""
temu_gates.py — 17 步脚本 9 道判定核心引擎（本机/吴凡机共用，禁混店）

9 道判定对应关系：
  判定器   -> Judge（分层 L0~L6 出 verdict）
  产物登记 -> ArtifactRegistry（sha256 登记，store 隔离）
  监工     -> gates_watchdog.py / Watchdog（汇总 17 步最新判定，不合格即退出非 0）
  第1道    -> click_verify.py   动作回读
  第2道    -> reconcile.py      平台回读/业务对账（金等式）
  第3道    -> crosscheck.py     独立第二信源交叉
  第4道    -> golden.py         黄金样本+回归
  第5道    -> gray.py           小批量灰度+真人首件
  第6道    -> fault_inject.py   故障注入（杀假成功）
  外加     -> idempotency.py    幂等防重复

禁混店：StoreContext.guard_path() 强制所有被判定文件属于本店根目录；
       吴凡机(sewing)只允许 store_mirror\\sewing-tools 与 gates17\\wufan_pull 下的文件，
       本机(local)只允许本机 Desktop 业务树。混店即抛 MixedStoreViolation，判定 suspect。
"""
import os, re, json, glob, time, hashlib, datetime, traceback

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)


class GatesError(Exception):
    pass


class MixedStoreViolation(GatesError):
    """禁混店：文件不属于当前店上下文"""


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def sha256_file(path, _cache={}):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def render(p):
    """路径模板渲染：{date} -> 今天(yyyyMMdd，业务文件命名习惯), {iso} -> 今天(ISO), {date-1} -> 昨天"""
    p = p.replace("{date}", today_str().replace("-", ""))
    y = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    p = p.replace("{date-1}", y)
    p = p.replace("{iso}", today_str())
    return p


def load_store_json(name):
    with open(os.path.join(BASE, name), "r", encoding="utf-8") as f:
        return json.load(f)


class StoreContext:
    """店上下文：禁混店的唯一裁判"""

    def __init__(self, store_file):
        cfg = load_store_json(store_file)
        self.store = cfg["store"]
        self.store_name = cfg.get("store_name", self.store)
        self.machine = cfg.get("machine", "")
        self.roots = [os.path.normpath(render(r)) for r in cfg.get("roots", [])]
        # 禁混店：显式黑名单根（即使落在本店大白根下，也一律拒绝）
        self.deny_roots = [os.path.normpath(render(r)) for r in cfg.get("deny_roots", [])]
        self.registry_file = os.path.join(DATA, cfg.get("registry", "artifacts_%s.jsonl" % self.store))
        self.verdicts_file = os.path.join(DATA, cfg.get("verdicts", "verdicts_%s.jsonl" % self.store))
        self.idem_file = os.path.join(DATA, cfg.get("idem", "idem_%s.jsonl" % self.store))

    def guard_path(self, path):
        """禁混店闸门：path 必须落在本店 roots 下，且不在 deny_roots 内"""
        p = os.path.normpath(os.path.abspath(render(str(path))))
        for root in self.deny_roots:
            try:
                if os.path.commonpath([p, root]) == root:
                    raise MixedStoreViolation(
                        "禁混店拦截(deny): %s 属于其他店数据区，拒绝在店 %s(%s) 上下文判定"
                        % (p, self.store, self.store_name)
                    )
            except ValueError:
                continue
        for root in self.roots:
            try:
                if os.path.commonpath([p, root]) == root:
                    return p
            except ValueError:
                continue
        raise MixedStoreViolation(
            "禁混店拦截: %s 不在店 %s(%s) 的数据根内 %s" % (p, self.store, self.store_name, self.roots)
        )

    def is_guarded_ok(self, path):
        try:
            self.guard_path(path)
            return True
        except MixedStoreViolation:
            return False


# ------------------------------------------------------------------
# 产物登记（第2组件）
# ------------------------------------------------------------------
class ArtifactRegistry:
    def __init__(self, ctx: StoreContext):
        self.ctx = ctx

    def register(self, step, path, source="local-run", note=""):
        p = self.ctx.guard_path(path)  # 混店文件拒绝登记
        if not os.path.isfile(p):
            return None
        st = os.stat(p)
        row = {
            "ts": now_str(),
            "store": self.ctx.store,
            "store_name": self.ctx.store_name,
            "step": str(step),
            "path": p,
            "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "sha256": sha256_file(p),
            "source": source,
        }
        if note:
            row["note"] = note
        with open(self.ctx.registry_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def register_many(self, step, paths, source="local-run"):
        rows = []
        for p in paths:
            r = self.register(step, p, source=source)
            if r:
                rows.append(r)
        return rows


# ------------------------------------------------------------------
# 判定器（第1组件）
# ------------------------------------------------------------------
def _json_get(obj, dotted):
    """支持 a.b.[2].c 点路径取值"""
    cur = obj
    for part in dotted.split("."):
        if part == "":
            continue
        m = re.fullmatch(r"\[(\d+)\]", part)
        if m:
            idx = int(m.group(1))
            if not isinstance(cur, list) or idx >= len(cur):
                return None, "路径 %s 越界" % dotted
            cur = cur[idx]
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None, "路径 %s 缺字段 %s" % (dotted, part)
    return cur, None


def _glob_one(pattern):
    hits = sorted(glob.glob(render(pattern)))
    return hits


def _count_records(path):
    """非空行数"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return -1


class Judge:
    def __init__(self, ctx: StoreContext, registry: ArtifactRegistry = None):
        self.ctx = ctx
        self.registry = registry or ArtifactRegistry(ctx)

    def judge(self, step_cfg, receipt):
        """
        step_cfg: {
            name, kind,
            artifacts: [{glob, min_bytes?, min_records?, fresh_sec?, required?}],
            json_assert: [{file, path, eq?|not_empty?|gte?|exists?}],
            log: {log_files:[...], require_log_any:[regex], deny_log:[regex]},
            reconcile: {type:..., ...}          # 第2道
            crosscheck: {type:..., a:{...}, b:{...}}   # 第3道
            golden: {type:sku_classification, sources:[...], samples:[{sku, expect_onsale}]}  # 第4道
        }
        receipt: {step, name, exit_code, ts, run_dir?, log_files?: [...]}
        返回 verdict dict，并落 verdicts_{store}.jsonl
        """
        layers = {}
        reasons = []
        step = str(receipt.get("step", step_cfg.get("step", "?")))
        name = receipt.get("name") or step_cfg.get("name", "")

        # ---- L0 进程层 ----
        ec = receipt.get("exit_code")
        if ec is None:
            l0_ok = None  # 无退出码（镜像小票/证据模式）：不判进程层，交产物/日志/对账层
            layers["L0"] = {"ok": None, "exit_code": None, "note": "无退出码，跳过进程层判定"}
        else:
            l0_ok = (int(ec) == 0)
            layers["L0"] = {"ok": l0_ok, "exit_code": ec}
            if not l0_ok:
                reasons.append("L0 退出码 %s != 0" % ec)

        # ---- L1 日志层 ----
        log_cfg = step_cfg.get("log") or {}
        log_files = [render(x) for x in (receipt.get("log_files") or log_cfg.get("log_files") or [])]
        guarded_logs = []
        for lf in log_files:
            try:
                guarded_logs.append(self.ctx.guard_path(lf))
            except MixedStoreViolation as e:
                reasons.append(str(e))
        texts = []
        for lf in guarded_logs:
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
            except OSError:
                pass
        blob = "\n".join(texts)
        l1 = {"ok": True, "checked_files": len(guarded_logs)}
        if log_cfg.get("deny_log"):
            deny_hits = []
            for rx in log_cfg["deny_log"]:
                m = re.search(rx, blob, re.M)
                if m:
                    deny_hits.append(m.group(0)[:80])
            if deny_hits:
                l1["ok"] = False
                l1["deny_hits"] = deny_hits
                reasons.append("L1 日志反证命中: %s" % "; ".join(deny_hits[:3]))
        if log_cfg.get("require_log_any"):
            need = log_cfg["require_log_any"]
            if not blob.strip():
                l1["ok"] = False
                l1["note"] = "无日志可查"
                reasons.append("L1 找不到任何日志，无法证明成功")
            elif not any(re.search(rx, blob, re.M) for rx in need):
                l1["ok"] = False
                l1["note"] = "完成标记未命中"
                reasons.append("L1 完成标记未命中 %s" % need)
        layers["L1"] = l1

        # ---- L2 产物层 + 产物登记 ----
        art_results = []
        registered = []
        l2_ok = True
        for spec in step_cfg.get("artifacts") or []:
            hits = _glob_one(spec["glob"])
            if not hits:
                art_results.append({"glob": spec["glob"], "found": False})
                if spec.get("required", True):
                    l2_ok = False
                    reasons.append("L2 产物缺失: %s" % spec["glob"])
                continue
            fresh = spec.get("fresh_sec", "day")
            best = None
            for h in hits:
                try:
                    self.ctx.guard_path(h)
                except MixedStoreViolation as e:
                    reasons.append(str(e))
                    l2_ok = False
                    continue
                mt = os.path.getmtime(h)
                if best is None or mt > os.path.getmtime(best):
                    best = h
            if best is None:
                continue
            rows = self.registry.register_many(step, [best], source=receipt.get("source", "judge"))
            registered.extend(rows)
            st = os.stat(best)
            age = time.time() - st.st_mtime
            rec = {
                "glob": spec["glob"],
                "file": best,
                "size": st.st_size,
                "age_sec": int(age),
                "found": True,
            }
            if st.st_size < spec.get("min_bytes", 1):
                rec["ok"] = False
                rec["why"] = "过小"
                l2_ok = False
                reasons.append("L2 产物过小: %s (%dB)" % (best, st.st_size))
            elif spec.get("min_records") and _count_records(best) < spec["min_records"]:
                rec["ok"] = False
                rec["why"] = "记录数不足"
                l2_ok = False
                reasons.append("L2 产物记录数不足: %s" % best)
            elif fresh == "day" and not datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d") == today_str():
                rec["ok"] = False
                rec["why"] = "非今日产物"
                l2_ok = False
                reasons.append("L2 产物非今日: %s" % best)
            elif isinstance(fresh, int) and age > fresh:
                rec["ok"] = False
                rec["why"] = "过期"
                l2_ok = False
                reasons.append("L2 产物过期: %s (age=%ds>%ds)" % (best, int(age), fresh))
            else:
                rec["ok"] = True
            art_results.append(rec)
        layers["L2"] = {"ok": l2_ok, "artifacts": art_results, "registered": len(registered)}

        # ---- L3 JSON 断言层 ----
        l3_ok = True
        l3_results = []
        for spec in step_cfg.get("json_assert") or []:
            fp = spec["file"]
            try:
                gp = self.ctx.guard_path(fp)
                with open(gp, "r", encoding="utf-8-sig", errors="replace") as f:
                    obj = json.load(f)
                val, err = _json_get(obj, spec["path"])
                rec = {"file": gp, "path": spec["path"], "value": val}
                if err:
                    rec["ok"] = False
                    rec["why"] = err
                    l3_ok = False
                    reasons.append("L3 %s" % err)
                elif "eq" in spec and val != spec["eq"]:
                    rec["ok"] = False
                    rec["why"] = "!= %r" % (spec["eq"],)
                    l3_ok = False
                    reasons.append("L3 %s.%s=%r != %r" % (os.path.basename(gp), spec["path"], val, spec["eq"]))
                elif spec.get("not_empty") and (val in (None, "", [], {}) ):
                    rec["ok"] = False
                    rec["why"] = "空"
                    l3_ok = False
                    reasons.append("L3 %s.%s 为空" % (os.path.basename(gp), spec["path"]))
                elif "gte" in spec:
                    try:
                        if not (float(val) >= float(spec["gte"])):
                            raise ValueError
                    except (TypeError, ValueError):
                        rec["ok"] = False
                        rec["why"] = "< %s" % spec["gte"]
                        l3_ok = False
                        reasons.append("L3 %s.%s=%r < %s" % (os.path.basename(gp), spec["path"], val, spec["gte"]))
                    else:
                        rec["ok"] = True
                else:
                    rec["ok"] = True
            except MixedStoreViolation as e:
                reasons.append(str(e))
                l3_ok = False
            except (OSError, json.JSONDecodeError) as e:
                rec = {"file": fp, "path": spec["path"], "ok": False, "why": str(e)[:120]}
                l3_ok = False
                reasons.append("L3 读取/解析失败 %s: %s" % (fp, str(e)[:80]))
            l3_results.append(rec)
        layers["L3"] = {"ok": l3_ok, "asserts": l3_results}

        # ---- L4 平台回读/业务对账（第2道）----
        l4 = {"ok": None, "note": "未配置"}
        if step_cfg.get("reconcile"):
            try:
                from reconcile import run_reconcile
                l4 = run_reconcile(self.ctx, step_cfg["reconcile"])
                if l4["ok"] is False:
                    reasons.append("L4 业务对账不一致: %s" % l4.get("note", ""))
            except Exception as e:
                l4 = {"ok": None, "note": "reconcile 异常: %s" % str(e)[:100], "err": traceback.format_exc()[-400:]}
        layers["L4"] = l4

        # ---- L5 独立第二信源（第3道）----
        l5 = {"ok": None, "note": "未配置"}
        if step_cfg.get("crosscheck"):
            try:
                from crosscheck import run_crosscheck
                l5 = run_crosscheck(self.ctx, step_cfg["crosscheck"])
                if l5["ok"] is False:
                    reasons.append("L5 两信源不一致: %s" % l5.get("note", ""))
            except Exception as e:
                l5 = {"ok": None, "note": "crosscheck 异常: %s" % str(e)[:100]}
        layers["L5"] = l5

        # ---- L6 黄金样本（第4道）----
        l6 = {"ok": None, "note": "未配置"}
        if step_cfg.get("golden"):
            try:
                from golden import run_golden
                l6 = run_golden(self.ctx, step_cfg["golden"])
                if l6["ok"] is False:
                    reasons.append("L6 黄金样本不符: %s" % l6.get("note", ""))
            except Exception as e:
                l6 = {"ok": None, "note": "golden 异常: %s" % str(e)[:100]}
        layers["L6"] = l6

        # ---- 汇总判定 ----
        hard_fail = (l0_ok is False) or (not l1["ok"]) or (not l3_ok) or (l4["ok"] is False) or (l6["ok"] is False)
        suspect = (not l2_ok) or (l5["ok"] is False)
        cfg_any = any([
            step_cfg.get("artifacts"), step_cfg.get("json_assert"), log_cfg,
            step_cfg.get("reconcile"), step_cfg.get("crosscheck"), step_cfg.get("golden"),
        ])
        if not cfg_any:
            verdict = "unverified"
            reasons.append("该步未配置任何判定规则")
        elif hard_fail:
            verdict = "failed"
        elif suspect:
            verdict = "suspect"
        else:
            verdict = "verified"

        v = {
            "ts": now_str(),
            "store": self.ctx.store,
            "step": step,
            "name": name,
            "verdict": verdict,
            "layers": layers,
            "reasons": reasons,
            "registered": len(registered),
            "trigger": receipt.get("source", "gates"),
        }
        with open(self.ctx.verdicts_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
        return v


# ------------------------------------------------------------------
# 监工（第3组件）
# ------------------------------------------------------------------
class Watchdog:
    def __init__(self, ctx: StoreContext, steps_order):
        self.ctx = ctx
        self.steps_order = [str(s) for s in steps_order]

    def latest_per_step(self):
        latest = {}
        try:
            with open(self.ctx.verdicts_file, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    latest[row["step"]] = row  # 后写覆盖 = 最新
        except OSError:
            pass
        return latest

    def report(self):
        latest = self.latest_per_step()
        lines = ["# 监工报告 (%s) 店=%s 机=%s" % (now_str(), self.ctx.store, self.ctx.machine), ""]
        n_ok = n_bad = n_missing = 0
        for s in self.steps_order:
            row = latest.get(s)
            if not row:
                lines.append("- 步 %s: 无判定记录 [MISSING]" % s)
                n_missing += 1
            else:
                mark = {"verified": "OK", "suspect": "SUSPECT", "failed": "FAIL",
                        "unverified": "UNVERIFIED"}.get(row["verdict"], row["verdict"])
                if row["verdict"] == "verified":
                    n_ok += 1
                else:
                    n_bad += 1
                why = "; ".join(row.get("reasons", [])[:2])
                lines.append("- 步 %s %s: %s %s" % (s, row.get("name", ""), mark, ("| " + why) if why else ""))
        lines.append("")
        lines.append("结论: OK=%d 非%serify=%d 无记录=%d" % (n_ok, "", n_bad, n_missing))
        exit_code = 0 if (n_bad == 0 and n_missing == 0) else 1
        return "\n".join(lines), exit_code


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
