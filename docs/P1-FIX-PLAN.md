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
| E1 | `raw` 只留白名单字段 + 脱敏摘要，不再存整行 | `importer/sources.py`、`importer/model.py` | ⬜ |
| E2 | 生产模式隐私回归 + 老库兼容（`fixups` 仍能重推方向） | `tests/`、`importer/store.py` | ⬜ |

验收（合成数据）：合成含敏感列的账单导入后，库里查不到那些列的值；
老库（带 `原始` 字段）仍能正常重推方向。

---

## 三、测试接缝（seam）

测试只打这些公开边界，不碰内部实现：

| 接缝 | 位置 |
|---|---|
| 单文件导入结果 | `pipeline.import_file()` 的返回值 |
| 月度批处理行为 | `monthly.run(vault)` 的返回值 + 文件系统事实（源文件位置） |
| 复核 HTTP 接口 | `review_ui.build_server()` 起的本地服务（GET `/state`、POST `/decide`） |
| 存储迁移 | `secure_store.open_ledger(vault)` 的返回值 + vault 目录里的事实 |
| 解析结果 | `sources.PARSERS[...](path)` 返回的 `ParseResult` |
| 审计日志内容 | 日志文件内容（白名单字段） |

## 四、完成标准

- [ ] 任意交易写入失败都会让批次进入失败或部分失败状态
- [ ] 失败源文件不会被移动到完成目录
- [ ] 日志中没有交易原始字段
- [ ] 复核请求失败不会跳过记录
- [ ] 明文数据库迁移后有验证和处置流程
- [ ] 生产模式不保存完整原始交易行
- [ ] 上述每条都有对应的合成数据回归测试
