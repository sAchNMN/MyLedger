# MyLedger

**个人记账工具。** 每月把支付宝、微信、银行卡的账单导进来，看清钱花在哪。

全程本地运行：不联网、不上传、不用任何云服务。账本运行时整个躺在内存里，
退出时用 Windows DPAPI 加密写盘，明文不落硬盘。

---

## 三条铁律

改这个项目之前先读这三条，它们比「代码好不好看」重要。

1. **真实账单永远不进 Git 仓库。** 仓库里只有代码和脱敏假数据（`fixtures/`）。
   真实数据住 `%USERPROFILE%\MyLedgerVault`，在仓库外。
2. **Git 历史不可撤回。** 删掉文件再提交一次没有任何用，历史对象还在 `.git/objects` 里。
   所以防线只能在**写入之前**生效 —— 见 `.gitignore` + `.githooks/pre-commit`。
3. **宁可报错停机，不可静默算错。** 解析出 0 笔必须报错；部分失败绝不当成功；
   认不出来的状态一律原样保留，不猜。

完整方案与威胁模型：`docs/DATA-SAFETY.md`。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（用到 DPAPI 做加密、7-Zip 做归档） |
| Python | 3.11 或更高（实测 3.13.14） |
| 依赖 | **只有 PyMuPDF**，见 `requirements.txt` |
| 可选 | 7-Zip（`C:\Program Files\7-Zip\7z.exe`），开 vault 容器用 |
| 可选 | Ollama，本地 AI 分类兜底用（不装也能跑，就是没有 AI 兜底） |

除 PDF 那一步外，全部使用 Python 标准库。XLSX 也是自己解析的 ——
`importer\sniff.py` 里写了原因：不引 openpyxl，是为了让这个工具保持零依赖。

---

## 快速开始

```bat
REM 1. 装依赖（只用这一个）
python -m pip install -r requirements.txt

REM 2. 装提交闸门（换机器、重新 clone 后都要做一次）
scripts\setup.cmd

REM 3. 日常用法：双击根目录 MyLedger.cmd
REM    浏览器会自动打开本地控制台 http://127.0.0.1:<随机端口>/?t=<一次性令牌>
```

网页控制台有 9 个页面：**概览 / 导入 / 疑似重复 / 报表 / 明细 / 未分类 / 分类诊断 / 历史修正 / 帮助**。
日常操作都在这里，不需要记命令。

几条写死的底线，别为了「方便」放宽：

- 只绑 `127.0.0.1`，没有提供绑别的地址的选项 —— 绑局域网等于把账本摊给整个网络。
- URI 带一次性随机令牌，防本机其它进程猜端口乱打。
- 页面零外部资源（无 CDN / 字体 / 图标库），离线可用。
- zip / PDF 密码只在内存活一次，不进日志，任务结束就清掉。
- 日志只活在服务进程内存里，关窗口就没。

---

## 命令行

网页控制台覆盖日常，命令行用于批处理与补救。

```bat
REM 单文件导入
python -m importer import 账单.csv

REM 月度批处理：扫投递目录 -> 导入 -> 出月报
python -m importer monthly

REM 解析器出 bug 时的补救路
python -m importer batches                REM 查哪一批是坏的
python -m importer drop-batch <批次号> --apply
python -m importer fix-source --apply

REM 分类
python -m importer unmatched              REM 未分类盘点（--mask 后输出可安全外发）
python -m importer setcat "商户名" 餐饮 --apply
python -m importer classify               REM 分类与状态判定诊断（只读）
python -m importer recompute              REM 按当前规则修正历史（默认只试算）

REM 其它
python -m importer report                 REM 消费汇总
python -m importer store                  REM 账本存储与加密状态
python -m importer web                    REM 开网页控制台
python -m importer sources                REM 各渠道怎么导出账单
```

跑 `python -m importer --help` 看全部 16 个子命令。

---

## 目录

```
MyLedger\
├── importer\           核心代码（23 个模块，约 1 万行）
│   ├── sources.py      各渠道适配器 + 银行行字段识别
│   ├── sniff.py        编码嗅探 / 找表头 / 解包 / xlsx 解析
│   ├── bank_pdf.py     PDF 词坐标还原行与列
│   ├── dedup.py        三层去重
│   ├── categorize.py   分类规则 + 本地 AI 兜底
│   ├── platformmap.py  平台自带分类 -> 本地分类映射
│   ├── store.py        SQLite 层
│   ├── secure_store.py 内存库 + DPAPI 加密落盘
│   ├── webui.py        本地网页控制台（9 个页面）
│   └── cli.py          命令行入口
├── MyLedger.cmd        双击启动控制台
├── scripts\            vault / 月度批处理 / 计划任务 / OCR / PDF 探针
├── tests\              24 个测试（独立脚本，退出码 0 = 通过）
├── fixtures\           脱敏合成账单（唯一允许提交的「数据」）
├── imports\            真实账单投递位（不进仓库，见 .gitignore）
└── docs\               文档（见下）

%USERPROFILE%\MyLedgerVault\      仓库外：加密账本、原始账单归档、投递目录、日志
```

---

## 开发

**跑测试**（每个脚本独立，退出码 0 = 通过）：

```bat
for %t in (tests\test_*.py) do python "%t"
```

```bash
# bash
for t in tests/test_*.py; do python "$t"; done
```

另有 1 个 Node 测试（`tests\js\test_detail_toggle.js`，验前端展开/收起逻辑）。

**必须串行跑。** 有共用临时 vault 和固定端口的，并行会互相抢，
报出来的失败是**假警报**。

**新增挂自动跑却没有构建步骤**：项目是脚本式 Python，`python -m importer` 直接跑。
也没有配 lint 工具。

**改代码的几条约定**（踩过坑才有的，详见 `docs/HANDOVER.md` §6）：

| 文件类型 | BOM | 换行 |
|---|---|---|
| `*.ps1` | 必须有 UTF-8 BOM | CRLF |
| `*.cmd` / `*.bat` | 无，且**只能写 ASCII**（65001 下中文注释会让 cmd 吞行） | CRLF |
| `.githooks/*`、`*.sh` | 绝对不能有 BOM | LF |

提交信息用 Conventional Commits：类型前缀英文，描述中文。

---

## 数据放哪 / 怎么不泄露

| 路径 | 用途 | 进仓库 |
|---|---|---|
| `imports/` | 真实账单导入位 | 否（只留 `.gitkeep`） |
| `data/` | 运行时数据 | 否（只留 `.gitkeep`） |
| `fixtures/` | 脱敏假数据 | **是**，唯一允许提交的数据 |
| `%USERPROFILE%\MyLedgerVault` | 加密的 vault（`ledger.db.enc` + `raw/` + `inbox/`） | 不适用，仓库外 |

三层防线：`.gitignore` → `.githooks/pre-commit` 闸门 → 7-Zip vault + DPAPI。
**物理接触层面的防护（BitLocker）按使用者环境决定，本项目不强制要求。**

---

## 已知限制

- **银行截图 OCR**：代码在（`scripts\bank-ocr.ps1` + `importer ocr`），
  但 Windows OCR 引擎能读金额、读不出日期，**当前输入质量下实际不可用**。
- 人工指定分类只到**商户**粒度，同一商户要拆两个分类做不到。
- 分类规则靠「关键词 + 顺序」，规则变多后顺序难维护。
- 报表是终端表格，没有可视化。
- **没装月度自动触发**（刻意不装）：账单不一定每月固定时间到，忘了也无所谓 ——
  流水线幂等，攒几个月一次性导完也不会算重。见 `docs/HANDOVER.md` §5.7。

不是限制的几件事（避免以为是缺陷）：

- **vault 备份会自动轮转**：写库前备份密文，只保留最近 3 份，更早的移到
  `%TEMP%\MyLedger-backup-rotation\`（移动，不是删除）。
- **闸门绕过会留痕**：万一真走了 `MYLEDGER_ALLOW_SENSITIVE=YES_I_AM_SURE` 那条路，
  每次都会往 `.workbuddy/audit/gate-bypass.log` 记一条（时间 / 分支 / 提交者 /
  被放行了什么）。这个文件不进仓库、不会被推送。

---

## 文档

| 文档 | 内容 |
|---|---|
| `docs/HANDOVER.md` | **交接文档，先读这篇。** 架构、关键设计决策（每条都有理由）、20 个踩过的坑、验证方法、命令速查 |
| `docs/DATA-SAFETY.md` | 威胁模型、三层防线的完整方案、脱敏输出怎么做 |
| `docs/IMPORT-DESIGN.md` | 导入链路的设计细节：识别、去噪、去重、分类、误导入补救 |
| `docs/P1-FIX-PLAN.md` | 只读审计的修复清单（5 个 P1 已修完，余下 P2/P3 在 HANDOVER §9） |
| `docs/PROJECT-STATUS-2026-10-05.md` | 项目状态报告：进度盘点、测试结果、风险与下一步 |

---

## 当前状态

2026-10-05：239 笔真实账单在库（微信 87 + 支付宝 119 + 银行 PDF 33），
24 个测试全通过，工作区干净。功能主线已跑通，欠账在工程化（依赖清单刚补上）
和少量 P2 / P3 上。详见 `docs/PROJECT-STATUS-2026-10-05.md`。
