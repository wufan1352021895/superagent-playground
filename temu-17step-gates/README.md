# 本机+吴凡机 17 步脚本补齐 9 道判定（禁混店）

对应 issue #2（本机+吴凡各17步补齐9道判定）。2026-11-06 重新执行完成。

## 9 道判定 ↔ 交付物映射

| # | 判定 | 落地 | 证据 |
|---|------|------|------|
| 1 | 判定器 | `gates17/temu_gates.py` Judge：L0退出码/L1日志反证/L2产物/L3JSON断言/L4平台对账/L5第二信源/L6黄金样本 → verdict ∈ verified/suspect/unverified/failed | `data/verdicts_local.jsonl`、`data/verdicts_sewing.jsonl` |
| 2 | 产物登记 | `ArtifactRegistry`：每步产物 sha256 登记，混店文件拒登记 | `data/artifacts_local.jsonl`、`data/artifacts_sewing.jsonl` |
| 3 | 监工 | `gates_watchdog.py`：17 步最新判定汇总，非 verified 即退出 1 | `data/watchdog_local.md`、`data/watchdog_sewing.md` |
| 4(第1道) | 动作回读 click_verify | `click_verify.py`：点完立刻回读页面状态，未变即抛错；回读死必抛 ReadbackDeadError | `data/selftest_evidence.md` 5/5 PASS |
| 5(第2道) | 平台回读/业务对账 | `reconcile.py`：备货金等式（提交=平台新增=列表减少）、下架后回读在售列表、通用计数对账 | 本机步42 L4：platform_total=28 = exported=28 |
| 6(第3道) | 独立第二信源交叉 | `crosscheck.py`：换入口/导出再数一遍 | 本机步29：在售中货号.txt 38 = 查在售数据 CSV 38 行；步34：CSV 38 = txt 38 |
| 7(第4道) | 黄金样本+回归 | `golden.py` + `data/golden_samples_local.json`：互斥清单固化 10 个标准答案（6是/4否），改完脚本必跑 | `data/selftest_evidence.md`、步29/34 L6 全符 |
| 8(第5道) | 小批量灰度+真人首件 | `gray.py`：资金/下架批量必须持真人 token（`confirm --op <op> --by <姓名>`）；无 token 一律 GrayBlocked | `data/selftest_evidence.md` 6/6 PASS |
| 9(第6道) | 故障注入（杀假成功） | `fault_inject.py`：遮挡/改名/断网/平台拒绝/回读恒失败 7 用例，全部不得假成功 | `data/selftest_evidence.md` 7/7 PASS |
| 外加 | 幂等防重复 | `idempotency.py`：sha256(store\|step\|op\|业务键\|day) 账本，同条件再跑必拦 | `data/selftest_evidence.md` 5/5 PASS |
| 贯穿 | 禁混店 | `store_local.json`/`store_sewing.json` 根+黑名单（deny_roots），越店文件判定/登记一律 MixedStoreViolation | `data/selftest_evidence.md` 拦截用例 PASS |

## 真实接入（可见动手）

- `install_gates_hooks.ps1` 已把判定钩子注入本机 17 步 wrapper（备份 `.bak-gates17-20261106_155627`；sales7 主脚本单独注入，差异见 `data/hooks_diff.txt`）。此后每次步执行：产物登记 → 分层判定 → **verdict=failed 时退出码改写 1（杀假成功）**；钩子自身异常只记录不拦业务（fail-open）。
- 钩子实测：步14（品质分差下架退货）今日调度 status=ok 但小票明写「部分失败/有失败项」→ 钩子判 failed 并以退出码 3 收场（wrapper 改写为 1）。这是判定器当场抓到的假成功，监工已标 FAIL 待补跑。
- 吴凡机侧：`run17_wufan.py` 经 sync_store_wufan 只读镜像通道取吴凡店自有小票判定（禁混店：sewing 上下文读本机业务树即拦截）。

## 交卷（机·步号·现象·路径）

- 本机：`交卷_本机_17步.md` — 17 步全判定，verified=15；**2 个真问题**：
  - 步1 备货 FAILED（exit=-1，AI 处理超时；`succeeded=false`，如实判失败）
  - 步14 品质分差下架退货 FAILED（status=ok 但小票「部分失败」，假成功被抓，需补跑）
- 吴凡机：`交卷_吴凡机_17步.md` — verified=17/17（基于吴凡店自有小票，日期 2026-11-04；吴凡机当日不在线 unreachable 100.84.203.94/192.168.0.109，如实标注）

## 复跑方式

```powershell
# 组件自检（可随时回归，不碰真实业务）
python gates17\selftest_all.py
# 本机 17 步判定交卷
python gates17\run17_local.py
# 吴凡机 17 步判定交卷（自动探在线状态）
python gates17\run17_wufan.py
# 监工
python gates17\gates_watchdog.py --store local
python gates17\gates_watchdog.py --store sewing
# 第5道：人工核对首件后放开批量
python gates17\gray.py confirm --op pccoderun --by <姓名>
```

## 边界与诚实说明

- 第2道备货金等式：本机步1 配置已就位（提交数从 `report_step01_restock_latest.json` 读取）；今日该步业务本身失败（AI 超时），平台侧计数源（待备货列表快照）在步1 成功跑通后即可闭环，判 `failed` 如实呈现，未粉饰。
- 吴凡机当日离线：判定基于其最近一次真实执行（11-04）的自有小票，小票日期已逐行标注；在线后重跑 `run17_wufan.py` 即可用当天小票刷新。
- 未改任何真实资金/下架业务逻辑；`gray.py` 只做闸门与登记，token 由真人确认后写入。
