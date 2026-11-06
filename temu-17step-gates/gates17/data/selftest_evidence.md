# 9 道判定组件自检 (2026-11-06 15:50:04)

## 第1道 动作回读 click_verify（抓没点上/误选中/遮挡；回读死必须抛错）
用例1 正常点击，状态变化 -> 应通过
  [PASS] 正常点击
用例2 按钮被遮挡，点击无效 -> 必须报错
  [PASS] 遮挡 -> ClickNotAppliedError: 第1道动作回读失败 [action]: 点击后页面状态未变化 after={'cart': 0}
用例3 误选中（点到了错误行），目标状态未变 -> 必须报错
  [PASS] 误选中 -> ClickNotAppliedError: 第1道动作回读失败 [action]: 点击后页面状态未变化 after={'selected': [], 'cart': 0}
用例4 回读通道恒失败 -> 必须抛 ReadbackDeadError
  [PASS] 回读死 -> RuntimeError: page dead
用例5 脚本自称成功但回读死 -> 仍必须抛错
  [PASS] 假成功+回读死 -> RuntimeError: page dead
click_verify selftest: PASS 5/5

## 第6道 故障注入（遮挡/改名/断网/平台拒绝/回读死 -> 不得假成功）
用例1 无故障 -> verified
  [PASS] 正常链路 (None) -> verdict=verified
用例2 遮挡：点了没反应 -> 第1道必须抛错
  [PASS] 遮挡 (occluded) -> RAISED ClickNotAppliedError: 第1道动作回读失败 [sim-submit]: 点击后页面状态未变化 after={'selected': [], 'c
用例3 元素改名 -> 必须抛错（不许静默当成功）
  [PASS] 元素改名 (renamed) -> RAISED ElementRenamedError: selector .btn-submit 不存在（元素已改名）
用例4 断网：平台回读失败 -> 必须抛错
  [PASS] 断网 (offline) -> RAISED NetworkDownError: 平台回读超时
用例5 平台拒绝 -> 必须抛错
  [PASS] 平台拒绝 (rejected) -> RAISED PlatformRejectError: 平台拒绝备货申请：商品处于清退中，不可创建备货单
用例6 回读恒失败 -> 必须抛错（杀假成功）
  [PASS] 回读恒失败 (readback_dead) -> RAISED RuntimeError: readback channel dead
用例7 业务自报成功但页面没变 -> 识破假成功
  [PASS] 假成功被识破 -> ClickNotAppliedError
fault_inject selftest: PASS 7/7

## 外加 幂等防重复（同条件再跑不得重复提交/下架）
用例1 首次提交 SKU-A -> 放行
  [PASS] 首跑放行
用例2 同日同 SKU 再提交 -> 必须拦截（防重复）
  [PASS] 再跑拦截
用例3 不同 SKU -> 放行
  [PASS] 异SKU放行
用例4 换天 -> 放行（天粒度幂等）
  [PASS] 换天放行
用例5 下架动作同样幂等
  [PASS] 下架幂等
idempotency selftest: PASS 5/5

## 第5道 小批量灰度+真人首件（无 token 批量必拦）
用例1 无 token 直接批量 -> 必须被拦
  [PASS] 批量无token -> GrayBlocked: 第5道灰度拦截 [pccoderun]: 无真人首件确认 token (C:\Users\Administrator\AppData\Loc
用例2 首件>1 条 -> 必须被拦
  [PASS] 首件多条 -> GrayBlocked: 第5道灰度拦截 [pccoderun]: 首件必须恰好 1 条，收到 2 条
用例3 首件 1 条（演练模式）-> 产出人工核对清单
  [PASS] 首件1条
用例4 有人工核对清单但未 confirm，批量仍被拦
  [PASS] 清单≠token -> GrayBlocked: 第5道灰度拦截 [pccoderun]: 无真人首件确认 token (C:\Users\Administrator\AppData\Loc
用例5 人工 confirm 后批量放行
  [PASS] confirm后放行
用例6 撤回 token 后批量再次被拦
  [PASS] 撤回后拦截 -> GrayBlocked: 第5道灰度拦截 [pccoderun]: 无真人首件确认 token (C:\Users\Administrator\AppData\Loc
gray selftest: PASS 6/6

## 禁混店拦截（判定文件越店即拒）
  [PASS] 本机上下文读吴凡机镜像小票 -> MixedStoreViolation
  [PASS] sewing 上下文读本机查在售数据 -> MixedStoreViolation
  [PASS] 各自读自家文件 -> 放行

## 第4道 黄金样本固化（今天互斥清单 -> 标准答案）
  [PASS] 固化样本 10 个（是=6 否=4），冻结于 2026-11-06 15:50:04

## 自检总结论: ALL PASS
