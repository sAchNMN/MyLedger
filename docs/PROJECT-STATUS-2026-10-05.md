# MyLedger 项目状态报告

> 生成时间：2026-10-05 17:35
> 检查方式：全仓库只读盘点 + 真实退出码跑全量测试 + 只读查真实账本
> 检查范围：`C:\Users\34759\Desktop\MyLedger`（Git 仓库）、`C:\Users\34759\MyLedgerVault`（仓库外真实数据）

---

## 0. 结论

**项目处于「功能基本齐备、工程化程度不足」的状态。**

Parser / 去重 / 分类 / 加密存储 / 网页控制台这条主线已经跑通，239 笔真实账单全部进库，
**24 个测试全绿、工作区干净、没有 TODO 残留**。

真正的欠账不在功能，在三件事上：

1. **新机器复现不了** —— 没有 README、没有依赖清单（唯一的第三方依赖 PyMuPDF 无处声明）。
2. **三层防线里最后一层是空的** —— BitLocker 未确认开启（本次查不动，需管理员）。
3. **文档开始滞后于代码** —— HANDOVER 写 8 个页面，实际 9 个；最近三轮的功能没进 §9。

---

## 0.1 更新：上面三条已全部处理（18:25）

本报告的剩余部分保留当时的原始判断，这里只记结论变化：

| 原欠账 | 处理结果 |
|---|---|
| ① 缺 README + 依赖清单 | **已补**。`README.md` + `requirements.txt`（依赖唯一真源，一行 `pymupdf>=1.28.2`，并写明被拒绝的四个包及理由）。ML-001 关闭。**没有上 pyproject.toml** —— 项目不打包、不发布，多一个文件多一处同步 |
| ② BitLocker | **用户评估后决定本机不开**，不再是待办。`DATA-SAFETY.md` §3.3 与 `HANDOVER.md` §9.1 写清了代价：物理接触层面失去防护，远程入侵 / 误提交 / vault 文件被复制这三类场景不受影响。换环境（出差、送修、借人）时要重新判断 |
| ③ 文档滞后 | **已同步**。页面数 8→9；§9 补了最近几轮；ML-001 标记关闭；`P1-FIX-PLAN.md` 加了「后续更新」节，不篡改它的历史原貌 |

另外把工作区残留**移动**（不是删除）到了
`%TEMP%\MyLedger-cleanup-20261005-181200\`：tmp 里的 4 项 + 12 个旧 vault 备份
（保留最近 3 份）。16 项共 2.8 MB，配 `MANIFEST.txt` 和双击即可还原的 `undo.cmd`。

**仍然没做的**：vault 备份**轮转机制缺失的根因没修** —— `secure_store.py`
每次跑 CLI 就留一份备份，清理只治标。这条进了下面的「下一步」。

---

## 1. 关键信息（按重要性）

| # | 项 | 现状 |
|---|---|---|
| 1 | 24 个测试（23 Python + 1 Node） | **全部通过**，真实退出码逐个校验，非 tail 假通过 |
| 2 | 工作区 | **干净**。`git status -uall` 无未跟踪、无未提交改动 |
| 3 | 提交状态 | master **领先 origin/master 2 个提交**，未 push（push 由你执行，符合约定） |
| 4 | 真实账本 | 4 个批次 **239 笔**（wechat 87 + alipay 119 + bank_pdf 31 + bank_pdf 2），0 部分失败 |
| 5 | 缺失配置 | ~~无 README.md、无 pyproject.toml / requirements.txt~~ → **18:25 已补**（README + requirements.txt） |
| 6 | 调试残留 | **无**。源码零 TODO/FIXME/XXX，零 `console.log` |
| 7 | vault 备份 | 15 个 → **12 个已移到 %TEMP%，留 3 份**。但 `secure_store.py` 的轮转机制仍未实现 |
| 8 | BitLocker | **用户决定本机不开**（§0.1）。文档已记录代价与适用边界 |
| 9 | 文档滞后 | **已同步**（18:25） |

---

## 2. 技术栈与模块职责

### 2.1 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.13.14 | 托管 venv：`C:\Users\34759\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| 依赖 | **几乎纯标准库** | xlsx 是自己解析的（`sniff.py` 明确注释：不引 openpyxl 就是为了零依赖） |
| 唯一第三方 | PyMuPDF 1.28.2 | 只为银行 PDF 的词坐标还原，**破了「零依赖」的例**（HANDOVER §5.8 有记录） |
| 网页 | 无框架 | `importer\webui.py`（1973 行）内嵌 HTML/CSS/JS，标准库 `http.server`，绑 127.0.0.1 + 一次性令牌 |
| 脚本 | PowerShell 5.1 / cmd | 必须 `-ExecutionPolicy Bypass`，`.ps1` 必须 UTF-8 BOM，钩子绝对不能有 BOM 且用 LF |
| 归档加密 | 7-Zip `C:\Program Files\7-Zip\7z.exe` | `-t7z -mhe=on -mx=9`；Python 3.13 的 zipfile 解不了 WinZip AES |
| 账本存储 | 内存 SQLite + Windows DPAPI | 运行时整库在内存，退出 `serialize()` + `CryptProtectData` 加密落盘，**明文不落盘** |
| 测试 | 独立脚本，无 pytest | 每个 `tests\test_*.py` 可单独跑，退出码 0 = 通过 |

### 2.2 模块职责（`importer\`，共 9990 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `webui.py` | 1973 | 本地网页控制台，9 个页签：概览/导入/疑似重复/报表/**明细**/未分类/分类诊断/历史修正/帮助 |
| `sources.py` | 1076 | 各渠道适配器（支付宝/微信/银行 PDF）+ 银行行的字段识别 |
| `cli.py` | 943 | 命令行入口，16 个子命令 |
| `store.py` | 690 | SQLite 层 |
| `sniff.py` | 533 | 编码嗅探、找表头行、压缩包解包、xlsx 解析 |
| `fixups.py` | 542 | 历史数据按新规则修正（`recompute` / `recompute --strip-raw`） |
| `monthly.py` | 487 | 月度批处理编排 |
| `secure_store.py` | 457 | 内存库 + DPAPI 加密落盘 |
| `categorize.py` | 457 | 分类规则 + 本地 AI 兜底 |
| `review_ui.py` | 451 | 疑似重复的本地网页确认界面 |
| `model.py` | 466 | 统一交易模型 Txn、方向与金额解析、状态判定 |
| `bank_pdf.py` | 335 | PDF 词坐标还原行与列 |
| `pipeline.py` | 343 | 单文件导入流水线（cli 与 monthly 共用） |
| `archive.py` | 297 | zip / 7z 解包处理 |
| `platformmap.py` | 228 | 平台自带分类 → 本地分类的映射（ML-011） |
| `sourcehint.py` | 125 | 渠道记忆（按文件「形状」记渠道） |
| `dedup.py` | 133 | 三层去重 |
| `credentials.py` | 109 | DPAPI 封装（只加解密，不存密码） |
| `render.py` | 113 | 终端排版与报表 |
| `audit_log.py` | 137 | 结构化审计日志（白名单，不含交易内容） |
| `power.py` | 75 | 电源闸门（未插电不跑 CPU 密集任务） |

### 2.3 三条互不依赖的链路

```
① 防泄露链路          ② 导入链路                  ③ 存储与报表
.gitignore            支付宝/微信/银行 PDF/截图      内存 SQLite（明文不落盘）
.githooks\pre-commit   → 嗅探编码与表头              → DPAPI 加密落盘
scripts\vault.ps1      → 还原成二维表                → 三层去重
BitLocker（⚠️ 空着）    → 统一成 Txn                 → 分类 → 报表/网页
                        → 去重/分类/入库
```

---

## 3. 进度盘点

### 3.1 已完成

**数据导入**
- 支付宝（加密 zip → CSV）✅
- 微信（加密 zip → XLSX，标准库自解析）✅
- 银行加密 PDF（PyMuPDF 词坐标）✅
- 表头三级识别 + 两条兜底路径（换格式不再直接失败）✅
- 列名不在别名表时的语义兜底 ✅

**去重与正确性**
- 三层去重（L3 曾长期损坏，已修）✅
- 批次状态机：部分失败 ≠ 成功，不许归档 ✅
- 方向判定：只压支出侧；状态（未成立/已退款）压成中性 ✅
- 代持转账：进账算收入、出账不算支出 ✅

**分类**
- 优先级链：人工指定 > 平台分类 > 关键词规则 > 原值 ✅
- 平台自带分类（支付宝「交易分类」/微信「交易类型」）✅
- 交易状态判定（`failed → refunded → ok` 顺序不可变）✅
- 未分类盘点 `unmatched`（`--mask` 可安全外发）✅
- 手工指定 `setcat`（按商户，可回填历史、可撤销）✅

**存储与安全**
- 明文库迁移：先校验后隔离，校验不过就撤销 ✅
- raw 最小化：不再存整行原文（`--strip-raw` 可清理历史）✅
- 后台日志白名单（不抄子进程输出）✅
- 结构化审计日志 ✅
- zip / PDF 密码用完即忘 ✅

**交互**
- 本地网页控制台 9 个页面（双击 `MyLedger.cmd`）✅
- 明细页（点开看具体哪几笔）+ 展开/收起 ✅
- 疑似重复人工确认界面 ✅
- 月度批处理 + 登录自动触发脚本（本次未能验证任务是否真装上）
- 解析器出 bug 的补救路：`batches` / `drop-batch` / `fix-source` ✅
- 两个脱敏探针：`bank-pdf-probe.py` / `bank-pdf-columns.py` / `bank-pdf-words.py` ✅

**只读审计的 P1**
- ML-004 / ML-006 / ML-007 / ML-008 / ML-009 / ML-010 全部修完，各有回归测试 ✅

### 3.2 进行中

| 项 | 状态 |
|---|---|
| **未分类交易归位** | 工具链齐了（`unmatched` → `setcat`），**卡在你自己的时间**。这不是代码问题 |
| **OCR 银行截图** | 代码在（`scripts\bank-ocr.ps1` + `importer ocr`），但当前输入质量下**不可用**（§6.6：引擎能读金额读不出日期） |

### 3.3 未开始（有明确意愿，代码没动）

| 编号 | 优先 | 事项 |
|---|---|---|
| — | 高 | 开 BitLocker（三层防线唯一空的一层） |
| 中 3 | 中 | 按**单笔**指定分类（现在只能按商户） |
| 中 4 | 中 | 分类规则带优先级数字（现在是「关键词 + 顺序」） |
| 中 5 | 中 | `monthly` 的密码提示去重（多文件时啰嗦） |
| ML-001 | P2 | 标准依赖清单与锁定 ← **本次确认仍缺失** |
| ML-003 | P2 | 提交闸门可被 `MYLEDGER_ALLOW_SENSITIVE` 绕过且不记审计 |
| ML-005 | P2 | 本地 AI 请求里带完整商户名 |
| ML-011 | P2 | OCR 明文产物（`*.raw.txt` / `*.rows.tsv` / `*.ocr.log`）无保留期限 |
| ML-012 | P2 | 7-Zip 密码进命令行参数 |
| ML-013 | P3 | 银行多金额行 / 无方向时的业务误判边界 |
| 低 6 | 低 | 跨年截图年份推测 |
| 低 7 | 低 | SQLCipher 级加密（等模型稳定） |
| 低 8 | 低 | 报表可视化（现在终端表格） |

---

## 4. 质量检查结果

### 4.1 测试（真实退出码，逐个跑）

```
for t in tests/test_*.py: python "$t"; rc=$?     → 23 个全 0
node tests/js/test_detail_toggle.js              → 0
```

| 测试 | 覆盖 |
|---|---|
| `test_dedup.py` | 三层去重回归 |
| `test_manual_category.py` | 分类优先级与未分类盘点 |
| `test_platform_category.py` | 平台分类 + 状态判定（12 组） |
| `test_batch_status.py` / `test_import_state.py` | 批次状态机 / 部分失败不许归档 |
| `test_store_migration.py` | 明文库迁移校验与撤销 |
| `test_raw_minimization.py` / `test_raw_cleanup.py` / `test_legacy_raw_compat.py` | raw 最小化与老库三种形态兼容 |
| `test_webui.py` / `test_webui_detail.py` | 令牌鉴权、密码不泄露、并发 409、明细页 |
| `test_zip_aes.py` / `test_zip_pdf.py` / `test_bank_abc_pdf.py` | AES zip、装着 PDF 的 zip、银行 PDF 左移 bug |
| `test_format_variants.py` / `test_compact_date.py` / `test_parse_issues.py` | 换格式、紧凑日期、0 笔报错口径 |
| `test_audit_log.py` / `test_background_log.py` | 日志白名单 |
| `test_review_api.py` / `test_review_ui_js.py` | 复核接口契约与前端失败态 |
| `test_source_hint.py` / `test_cli_import.py` / `test_monthly_archive.py` | 渠道记忆、CLI 退出码、月度归档 |

**纪律提醒**：必须**串行**跑。有共用临时 vault / 固定端口的，并行会抢资源报假失败。

### 4.2 构建 / Lint

**没有构建步骤，也没有 lint 工具。** 项目是脚本式 Python，`python -m importer` 直接跑。
`--help` 正常，16 个子命令全部注册成功。

### 4.3 未提交改动

```
## master...origin/master [ahead 2]
git status -uall  → 空
```

领先的两个提交（**未 push，等你执行**）：
- `2b79b74` feat: 网页加「明细」页 —— 点开就能看具体是哪几笔
- `797d84b` fix: 「明细」页的展开按钮点了没反应

分支：`master` 唯一分支 + 保底 tag `archive/worktree-4bed63b`。远端 `origin` → `github.com/sAchNMN/MyLedger.git`。
**无新 worktree**（按 2026-10-05 的事故教训，不再建）。

### 4.4 调试残留

**干净。** 全仓扫描 `TODO|FIXME|XXX|HACK`（排除 `.git`/`__pycache__`）：
命中的全是**注释里在讲某个 bug 的来龙去脉**（共 27 处，有价值，别删），
不是未完成的坑。 Examples：`test_zip_aes.py:9`「压缩包 None 本身就是 bug」、
`importer\sources.py:252`「raw 看起来像调试信息所以没人清理」。

`console.log`：**零**。Python 里的 `print(` 都是正常的用户输出。

### 4.5 依赖风险

| 问题 | 说明 | 现状 |
|---|---|---|
| 无依赖清单 | 唯一的 PyMuPDF 没写在任何地方。新机器 clone 下来跑 `python -m importer import x.pdf` 会 ImportError，且报错不一定是「没装 pymupdf」 | **18:25 已修**：`requirements.txt`，含被拒绝的四个包及理由 |
| venv 被污染 | 托管 venv 里混着 httpx / numpy / pillow / opencv / playwright / python-pptx / safetensors 等一堆**与本项目无关**的包（其他项目共用）。好处是刚好没踩坑，坏处是**无法验证项目真的只需 PyMuPDF** | 未解决。要验的话得在干净 venv 里装一遍 `requirements.txt` 跑全量测试 |

### 4.6 工作区残留（18:25 已清理）

| 路径 | 处理 |
|---|---|
| `tmp\merge-msg.txt` | 已移到 `%TEMP%\MyLedger-cleanup-20261005-181200\tmp\` |
| `tmp\run_test_patched.py` | 同上 |
| `tmp\bde.txt` | 同上（本次盘点时自己生成的 BitLocker 探测输出） |
| `tmp\tests\` | 同上，整目录移动 |
| `MyLedgerVault\*.backup-*` × 12 | 已移到 `...\vault-backups\`，**保留最近 3 份** |

明细见 `%TEMP%\MyLedger-cleanup-20261005-181200\MANIFEST.txt`，
一键还原跑同目录的 `undo.cmd`。全部移动，无删除。

---

## 5. 真实账本现状（只读）

```
批次 20261004-225326-68b8de  wechat     87/87    87  ✅
批次 20261004-225349-d1b05a  alipay    119/119  119  ✅
批次 20261004-235213-3d7a1a  bank_pdf   31/31    31  ✅
批次 20261005-161910-faaac6  bank_pdf    2/2      2  ✅
```

- 写入/解析全部相等 → **没有任何一批出现部分失败**。
- 库中数量 = 写入数量 → **没有交易被后删或判重吞掉**。
- 加密账本 256.2 KB，DPAPI 绑定当前 Windows 用户。

**待收的两个尾**：
1. 未分类商户还有一批（金额都很小，0.01 ~ 5.00 居多）——`unmatched` 看完用 `setcat` 归位。
2. **状态词「已收钱」认不出来**，`classify` 诊断把它单列出来了 → 加进 `importer\model.py` 的 `_STATUS_*` 词表即可。
3. 8 笔 / 8,576.61 元落在「未成立/已退款/未完成」里不计支出 —— **这是设计内的**，不是 bug。

---

## 6. 风险与建议

| 级别 | 风险 | 建议 | 18:25 状态 |
|---|---|---|---|
| **高** | 三层防线少一层：BitLocker 没确认开 | 以管理员身份开 PowerShell 确认；这层守的是「笔记本被物理接触」 | **决定不开**（用户评估使用环境）。已写入 `DATA-SAFETY.md` §3.3 与 `HANDOVER.md` §9.1 |
| **高** | 新机器复现不了：无 README + 无依赖清单 | `README.md` + `requirements.txt`（一行 `pymupdf>=1.28.2`）。ML-001 | **已修** |
| 中 | vault 备份无限增长 | 加保留策略：只留最近 N 次。现在是每跑一次 CLI 就一份 | 手工清了 12 份，**根因（缺轮转）未修** |
| 中 | 文档滞后于代码 | HANDOVER 的「8 个页面」改成 9 个；§9 补最近几轮 | **已同步** |
| 中 | 计划任务状态未验证 | `schtasks` 被本机沙箱黑名单拦。你自己跑一次：`powershell -ExecutionPolicy Bypass -File "C:\Users\34759\Desktop\MyLedger\scripts\schedule-monthly.ps1" status` | 未验证，仍需你跑 |
| 低 | ML-003：闸门可被环境变量绕过且不记审计 | 真要防就改成「绕过时强制写审计日志」 | 未动 |
| 低 | venv 混装 | 建议在干净 venv 里装一遍 `requirements.txt` 验「只装 pymupdf 就能跑」 | 未动 |

---

## 7. 下一步建议（按优先级）

> 18:25 复核：**第 1、3、4 条已做**（README + 依赖清单、残留清理、文档同步），
> **第 2 条 BitLocker 用户决定不做**。下面按当前状态重新排过：

### 现在最该做的

1. **给 `secure_store.py` 加备份轮转**（清理只治了标）
   - 现在是每次 CLI 运行都写一份 `ledger.db.enc.backup-<时间戳>`，永不清理
   - 建议：只保留最近 N 份（N=3 就够，和这次清理留的数量一致），
     或改成「同一天内只保留最新一份」
   - 这次手工清理不能替代它 —— 清理完还会再堆

2. **收掉 §5 里那两个小尾巴**（几分钟的事）
   - 状态词「已收钱」认不出来 → 加进 `importer\model.py` 的 `_STATUS_*` 词表
   - 未分类商户归位（`unmatched` → `setcat`）—— 这一步只有你能做，商户名不出本机

3. **ML-003：闸门绕过不留痕**
   - 现在设 `MYLEDGER_ALLOW_SENSITIVE=YES_I_AM_SURE` 就能绕过且不记审计
   - 改成「绕过时强制写一条审计日志」，成本低、补的是信任缺口

### 已完成的（18:25）

- ~~README + 依赖清单~~ → `README.md` + `requirements.txt`，ML-001 关闭
- ~~清理工作区残留~~ → 16 项移到 `%TEMP%`，配 `MANIFEST.txt` + `undo.cmd`
- ~~同步文档~~ → HANDOVER / DATA-SAFETY / P1-FIX-PLAN / 本报告

### 决定不做的

- **BitLocker** —— 使用环境不需要。理由与边界见 `DATA-SAFETY.md` §3.3

**不建议现在碰的**：ML-013（银行业务误判边界）、SQLCipher、报表可视化。都要等数据模型稳定，现在做是过度设计。
