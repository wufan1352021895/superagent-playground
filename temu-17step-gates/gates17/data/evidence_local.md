# 本机 17 步 9 道判定交卷

- 机：本机 DESKTOP-VHV1N0K / Administrator（店：Wei Qi Measurement）
- 判定时点：2026-11-06 15:52:23
- 判定引擎：Temu中控台\gates17（L0退出码 / L1日志 / L2产物登记 / L3JSON断言 / L4平台对账 / L5第二信源 / L6黄金样本）
- 禁混店：store_local.json 根约束，越店文件判定直接拦截（见自检证据）

| 机 | 步号 | 步名 | 现象 | 路径 |
|---|---|---|---|---|
| 本机 | 1 | 01. 备货 | FAILED \| exit=-1 \| L0 退出码 -1 != 0; L3 report_step01_restock_latest.json.succeeded=False != True | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step01_restock_latest.json |
| 本机 | 2 | 02. 待创建 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step02_stock_latest.json |
| 本机 | 3 | 03. 2-3_14天 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step03_final_result.json |
| 本机 | 4 | 04. 查违规 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\查违规-精简\scripts\temu_sales_compliance_match.md |
| 本机 | 5 | 05. 活动机遇 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step05_readback_step05_latest.json |
| 本机 | 6 | 06. 活动商品 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step06_readback_step06_latest.json |
| 本机 | 7 | 07. 站点库存0-1 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step07_readback_step07_latest.json |
| 本机 | 8 | 08. 体积重量 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step08_readback_step08_latest.json |
| 本机 | 9 | 09. 3个月SKC | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\3个月\Temu_SKC_迁移包_019ef887\scripts\readback_step09_latest.json |
| 本机 | 10 | 10. 资质待待 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step10_readback_step10_latest.json |
| 本机 | 11 | 11. 2-8质检 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step11_readback_step11_latest.json |
| 本机 | 12 | 12. 商品信息待确认 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step12_readback_step12_latest.json |
| 本机 | 14 | 13. 品质分差下架退货（下架敏感，第5道灰度门管控） | FAILED \| exit=0 \| L1 日志反证命中: 部分失败; 有失败项 | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step14_label_report_latest.txt |
| 本机 | 15 | 14. 待快递揽收 | VERIFIED \| exit=0 \| 分层判定通过  | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step15_readback_step14_latest.json |
| 本机 | 29 | 在售货号 | VERIFIED \| exit=0 \| 分层判定通过 L5:{"a": 38, "b": 38} L6:黄金样本10/10 | C:\Users\Administrator\Desktop\txt\在售中货号.txt; C:\Users\Administrator\Desktop\txt\不在在售中不在上新.txt |
| 本机 | 34 | 查在售数据 | VERIFIED \| exit=0 \| 分层判定通过 L5:{"a": 38, "b": 38} L6:黄金样本10/10 | C:\Users\Administrator\Desktop\查在售数据\查在售数据_20261106.csv; C:\Users\Administrator\Desktop\查在售数据\查在售数据_20261106.txt |
| 本机 | 42 | 销量近7日0 | VERIFIED \| exit=0 \| 分层判定通过 L4:{"a": 28, "b": 28} L5:{"a": 28, "b": 28} | C:\Users\Administrator\Desktop\Temu中控台\reports\report_step42_readback_step42_latest.json; C:\Users\Administrator\Desktop\销量近7日0\查在售数据_20261106_v2.csv |

小结: verified=15 非erify=2

判定明细（JSONL）: gates17\data\verdicts_local.jsonl
产物登记（JSONL）: gates17\data\artifacts_local.jsonl
