# P1 修复清单（按小块推进）

这份文件是只读审计报告的落地版。审计结论：项目健康度 **58/100**，5 个 P1、6 个 P2、1 个 P3，
未发现已证实的 P0 远程漏洞。

本轮只做 **5 个 P1**，按审计给出的顺序执行。每完成一小块就：
改代码 → 用**纯合成数据**写回归测试并跑绿 → 单独 `git commit`。
P2/P3、依赖锁定、测试入口统一等工程化项**本轮不动**。

不用真实账单、不读数据库内容、不读日志内容。

---

## 一、审计要点（浓缩）

1. **最高风险是数据完整性**：部分交易失败后源文件仍可能被移动 → 漏记且不再重试。
2. **第二风险是敏感副本扩散**：原始行、后台日志、OCR 产物、明文迁移库都在留个人财务信息。
3. **复核界面存在确认丢失风险**：HTTP 4xx/5xx 不抛异常，页面照样跳到下一条。
4. 架构分层清晰，但缺统一状态机、依赖锁定、端到端测试。
5. 报告本身没有读取真实账单 / 数据库 / 日志内容 / 密钥 / Token，也没有修改文件。

长期方向（本轮不做）：字段分级 L0–L4、统一结果枚举
（success / partial_success / retryable_failure / permanent_failure / cancelled）。

---

## 二、小块拆解

### 阶段 A —— ML-006 部分写入失败仍被视为成功（P1，最高优先）

> 症状：100 笔里挂了 2 笔，`outcome.ok = True`，monthly 照样把源文件移进 `_done`，
> 那 2 笔永远不会被重试。

| 块 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| A1 | `batch` 表增加 `status` 列 + 状态流转（含老库 `ALTER` 迁移） | `importer/store.py` | ✅ |
| A2 | `ImportOutcome` 加状态机，`ok` 由 `status` 派生 | `importer/pipeline.py` | ✅ |
| A3 | 只有 `success` 才允许移动源文件；失败留在原地并给重试说明 | `importer/monthly.py` | ✅ |
| A4 | `import` 子命令报告部分失败 + 退出码 | `importer/cli.py` | ✅ |
| A0 | （测试抓出来的额外缺陷）同一秒重跑同一文件 `batch_id` 撞主键 | `importer/store.py` | ✅ |

验收（合成数据）：3 笔交易、第 2 笔故意写失败 → 源文件仍在原地、批次为
`partial_failed`、重跑不产生重复。**已通过**：
`tests/test_batch_status.py`、`tests/test_import_state.py`、
`tests/test_monthly_archive.py`、`tests/test_cli_import.py`（失败注入用真实 SQLite 触发器）。

### 阶段 B —— ML-007 / ML-004 原始交易内容进入日志（P1 + P2）

> 症状：`skipped` 里带 `"raw": joined[:120]`，reason 里还内嵌原始单元格值；
> `monthly-run.ps1` 把子进程全部 stdout/stderr 追加进长期日志。

| 块 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| B1 | 解析失败改成结构化错误码，删掉原始行与原始值 | `importer/sources.py` | ✅ |
| B2 | 跳过行的打印走白名单（行号 + 字段 + 错误码） | `importer/pipeline.py`、`importer/model.py` | ✅ |
| B3 | 新增结构化审计日志模块（只写白名单字段） | `importer/audit_log.py`（新） | ✅ |
| B4 | 后台执行器改为传日志文件，不再收全部 stdout | `scripts/monthly-run.ps1`、`importer/monthly.py`、`importer/cli.py` | ✅ |
| B0 | （顺带）`scripts/*.ps1` 的 UTF-8 BOM 守卫：无 BOM 时 PS 5.1 按 ANSI 读，中文注释会读成语法错误 | `tests/test_background_log.py` | ✅ |

> B1 与 B2 合并成一次提交：删掉 `reason` 字段后 pipeline 的打印会变成 `None`，
> 两块必须同时落地才算「没退化」。

验收（合成数据）：构造含 `商户_TEST` / `123.45` / `TEST-001` / `TEST_NOTE` 的失败行，
扫日志确认这四类字符串一个都不出现。**已通过**：
`tests/test_parse_issues.py`（解析结果与打印文本）、
`tests/test_audit_log.py`（日志白名单）、
`tests/test_background_log.py`（真跑 `monthly-run.ps1`，按字节扫日志文件）。

### 阶段 C —— ML-008 复核请求失败仍跳过当前记录（P1）

| 块 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| C1 | `/decide` 明确契约：`ok/id/updated`、`404 not_found`、幂等；`resolve` 加守卫 | `importer/review_ui.py`、`importer/store.py` | ✅ |
| C2 | 前端检查 `response.ok`，失败保留当前条并提示，防止重复提交 | `importer/review_ui.py` | ✅ |
| C0 | （测试抓出来的额外缺陷）重复提交重复计数、提交次数一到就提前关界面 | `importer/review_ui.py` | ✅ |

验收（合成数据）：模拟 200 / 400 / 404 / 403 / 500 / 重复提交，只有 200 才前进一条。
**已通过**：`tests/test_review_api.py`（真实 HTTP 打后端）、
`tests/test_review_ui_js.py`（Node 里真跑页面脚本，11 个场景；
反向验证过它抓得住 HEAD 版本的缺陷）。

### 阶段 D —— ML-009 明文数据库迁移后仍然保留（P1）

| 块 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| D1 | 迁移后校验密文可独立打开：schema + 笔数 + 金额合计；同时检测 `-wal/-shm` | `importer/secure_store.py` | ✅ |
| D2 | 校验通过后把明文三件套移入隔离目录（不删），残留每次启动强提醒 | `importer/secure_store.py`、`importer/cli.py` | ✅ |

验收（合成数据）：合成明文库 → 迁移 → 明文主文件与 `-wal/-shm` 都进隔离目录 →
密文重新打开后笔数与金额合计一致 → 迁移失败时明文原样保留。
**已通过**：`tests/test_store_migration.py`（含用打桩 `protect()` 制造坏密文、
冒名顶替的 sqlite 文件、手动把明文拷回来、`store` 子命令的报告）。

### 阶段 E —— ML-010 原始交易行完整入库（P1）

| 块 | 内容 | 涉及文件 | 状态 |
|---|---|---|---|
| E1 | `raw` 只留白名单字段 + 脱敏摘要，不再存整行 | `importer/sources.py`、`importer/model.py` | ✅ |
| E2 | 生产模式隐私回归 + 老库兼容（`fixups` 仍能重推方向） | `tests/`、`importer/fixups.py` | ✅ |
| E3 | 历史数据里已有的整行原文也能清掉（`recompute --strip-raw`，默认只试算） | `importer/fixups.py`、`importer/cli.py` | ✅ |
| E0 | （测试抓出来的额外缺陷）raw 为 `{}` 的老行会被 recompute 猜成中性，支出静默消失 | `importer/fixups.py` | ✅ |

验收（合成数据）：把标记串只放进「报表用不到的列」（对方账号 / 交易分类 / 扩展列），
导入后全库所有表的所有列里都找不到它们；老库四种 raw 形态都能正确重推方向。
**已通过**：`tests/test_raw_minimization.py`、`tests/test_legacy_raw_compat.py`、
`tests/test_raw_cleanup.py`。

> E1 只让**新导入**的行不再存整行；已经入库的数据还是老格式，所以补了 E3。
> 清理历史 raw 有个不能省的顺序：**先把方向值搬进新格式的「方向列」，
> 再丢掉整行** —— 反了就让 recompute 的双向性彻底断掉。

---

## 三、过程中额外发现并修掉的缺陷

这四条不在审计报告里，都是「先写会失败的测试」时被测试抓出来的，
每一条都有独立的回归测试：

| 编号 | 缺陷 | 后果 |
|---|---|---|
| A0 | `batch_id` = 秒级时间戳 + sha 前 6 位，同一秒重跑同一文件撞主键 | 重试路径直接崩，源文件永远处理不了 |
| B0 | 重写 `.ps1` 时丢 UTF-8 BOM | PowerShell 5.1 按 GBK 解码，中文注释变成语法错误 |
| C0 | `/decide` 重复提交重复计数；计数一到总数就 `shutdown()` | 手快双击一次就把确认界面提前关掉 |
| E0 | `fixups._derive_direction` 在「查不到方向值」时继续往下走 | `parse_direction("")` 返回中性 → `recompute --apply` 把一批支出从报表里抹掉 |

---

## 四、测试接缝（seam）

测试只打这些公开边界，不碰内部实现：

| 接缝 | 位置 |
|---|---|
| 单文件导入结果 | `pipeline.import_file()` 的返回值 + 批次状态 |
| 月度批处理行为 | `monthly.run(vault)` 的返回值 + 文件系统事实（源文件位置） |
| 复核 HTTP 接口 | `review_ui.build_server()` 起的本地服务（GET `/state`、POST `/decide`） |
| 复核页面逻辑 | 页面里的 `<script>` 在 Node 里用 DOM/fetch 桩真跑 |
| 存储迁移 | `secure_store.open_ledger(vault)` 的返回值 + vault 目录里的事实 |
| 解析结果 | `sources.PARSERS[...](path)` 返回的 `ParseResult`（含 `txn.raw`） |
| 历史数据修正 | `fixups.recompute()` / `fixups.strip_raw()` 的返回值 + 库内事实 |
| 审计日志内容 | 日志文件内容（白名单字段） |

## 五、完成标准

- [x] 任意交易写入失败都会让批次进入失败或部分失败状态
- [x] 失败源文件不会被移动到完成目录
- [x] 日志中没有交易原始字段
- [x] 复核请求失败不会跳过记录
- [x] 明文数据库迁移后有验证和处置流程
- [x] 生产模式不保存完整原始交易行（含历史数据清理路径）
- [x] 上述每条都有对应的合成数据回归测试

## 六、修复后的状态（自评，不是重新审计）

审计给的维度与分数是修复前的。下面是我按同样的维度重新估的分，
依据是「这一维度的主要缺陷是否有回归测试钉住」。这是自评，不是第三方审计。

| 维度 | 审计 | 现在 | 依据 |
|---|---:|---:|---|
| 架构清晰度 | 76 | 82 | 多了批次状态机、审计日志两个明确边界；`raw`/失败记录的契约写进了模块文档 |
| 数据正确性 | 55 | 78 | 部分是部分、失败是失败，归档条件单一来源且有端到端测试；顺手修掉两处静默数据损坏 |
| 隐私保护 | 52 | 74 | 解析失败、打印文本、后台日志、`raw`、明文迁移副本五条泄漏路径都堵上；OCR 产物仍是 P2 |
| 测试充分性 | 42 | 80 | 1 个测试文件 → 14 个，失败/恢复/隐私路径都有专门构造的数据；仍缺统一入口与 CI |
| 可维护性 | 62 | 68 | 状态与日志契约明确了；依赖清单、脚本解释器路径配置化仍未做 |
| DevOps 可复现性 | 45 | 48 | 基本没动（本轮不碰 P2）；只多了一个需要 Node 的前端测试 |
| 运维安全 | 58 | 74 | 后台日志最小化；提交闸门绕过、7z 密码进命令行仍是 P2 |
| **综合** | **58** | **约 72** | 5 个 P1 全部关闭；P2/P3 未动 |

**仍然不适合无条件处理真实数据**：BitLocker 未开、依赖未锁定、
OCR 产物没有保留期限（ML-011）、提交闸门仍可绕过（ML-003）。
这些是 P2 及以上的事，本轮按约定没有动。

## 七、本轮没做的事（明确记录，避免误以为已解决）

1. 只读审计里 6 个 P2 与 1 个 P3 —— 全未修改。
2. 没有引入依赖清单 / 锁文件（ML-001），也没有统一测试入口。
3. 没有读取、修改或验证任何真实账单、真实数据库、真实日志。
4. 没有做性能、并发、长时间运行方面的验证。
5. `tests/test_review_ui_js.py` 需要一个 Node 运行时；
   找不到时它会明确打印 SKIP，不会假装通过。

---

## 八、后续更新（2026-10-05）

本文档是**那一轮修复的历史快照**，保留原样以便追溯当时的判断。
之后状态变化的只有两件事，记录在这里避免与上文的表格冲突：

1. **ML-001（依赖清单）已关闭。** 新增 `requirements.txt` 作为依赖的唯一真源
   —— 只有 `pymupdf>=1.28.2` 一行，文件里写明了为什么只有一行，以及
   openpyxl / pandas / pytest / requests 被明确拒绝的理由。
   同时新增 `README.md`，把「新机器怎么跑起来」补齐了。
   **仍然没有 `pyproject.toml`**：本项目不打包、不发布，`python -m importer`
   直接跑，requirements.txt 已经够，多一个文件就多一处需要同步。
   上面「DevOps 可复现性」一栏因此从 48 上抬，但**没有锁文件**，
   距离「换台机器能一模一样复现」仍有距离。
2. **BitLocker 不再列为待办。** 用户评估使用环境后决定本机不开。
   这不是「被遗忘」，是一个有理由的决定 —— 代价与剩余风险写在
   `DATA-SAFETY.md` §3.3：物理接触层面失去防护，其余场景不受影响。
   上面「仍然不适合无条件处理真实数据」那句，现在只剩 ML-011 和 ML-003
   两项仍然成立。

其余 P2 / P3（ML-003 / ML-005 / ML-011 / ML-012 / ML-013）**状态不变，均未修改**。
当前完整的进度盘点见 `docs/PROJECT-STATUS-2026-10-05.md`。
