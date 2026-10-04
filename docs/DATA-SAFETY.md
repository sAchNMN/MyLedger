# MyLedger 数据安全方案

> 适用场景：AI 主导编码开发记账软件 → 用真实账单数据本地测试 → 最终把代码推到 GitHub。
>
> 最后更新：2026-10-04

---

## 0. 结论（30 秒版）

**一条铁律：真实账单永远不进 Git 仓库。**
仓库里只有代码和假数据。真实数据住在仓库之外，按需要加密。

**三层防线，各管一件事：**

| 层 | 防的是什么 | 手段 | 现状 |
|---|---|---|---|
| 结构层 | 数据误入仓库 | 数据目录在仓库外 + `.gitignore` | ✅ 已配好 |
| 闸门层 | 手滑 `git add .` | `.githooks/pre-commit` 敏感内容扫描 | ✅ 已配好 |
| 设备层 | 整机/硬盘被拿走 | BitLocker 全盘加密 | ⚠️ 待你开启 |
| 加码层 | 文件被单独复制走 | 7z AES-256 加密容器 | ✅ 工具已就绪 |

**为什么必须这么严：Git 历史不可撤回。** 文件提交过一次，即使后来删掉再提交，历史里仍然有完整副本，`git log -p` 就能还原。防泄漏只能在写入之前做，事后补救代价极高。

---

## 1. 威胁模型：你防的到底是谁

搞不清敌人，防线就是摆设。记账软件的真实威胁按概率排序：

| 概率 | 威胁场景 | 哪层防线管用 |
|---|---|---|
| 高 | 手滑把真实 CSV 提交，推上 GitHub | 闸门层 + 结构层 |
| 高 | 把账单发给云端 AI / 第三方在线工具处理 | 只能靠习惯，见第 7 节 |
| 中 | 备份盘、U 盘、移动硬盘丢失或转手 | 加码层（7z 加密容器） |
| 中 | 电脑送修、丢失、被他人临时使用 | 设备层（BitLocker） |
| 中 | vault 目录被云盘自动同步到公网 | 结构层（`vault status` 会检查） |
| 低 | 被定点入侵、恶意软件窃取 | BitLocker + 系统更新，无法完全防住 |
| 低 | GitHub 账号被盗，private 仓库被拖 | 结构层（仓库里根本没数据） |

**关键判断：你不需要防住所有人，你需要让「文件被拿走」不等于「数据被拿走」。** 这就是加密的全部意义。

---

## 2. 目录布局

```
C:\Users\34759\
├── MyLedgerVault\                    ← 真实数据区，在 Git 仓库之外
│   ├── myledger-vault.7z             密文容器（AES-256 + 加密文件名）
│   └── plain\                        解密后的工作区，用完必须 close
│
└── Desktop\MyLedger\                 ← Git 仓库，只放代码 + 假数据
    ├── imports\                      真实账单导入位（被忽略，仅留 .gitkeep）
    ├── data\                         运行时数据（被忽略，仅留 .gitkeep）
    ├── fixtures\                     脱敏假数据 ← 唯一允许提交的「数据」
    ├── .githooks\pre-commit          提交闸门（纯 shell 内置命令，无外部依赖）
    ├── scripts\
    │   ├── setup.cmd                 ★ 双击即可安装闸门
    │   ├── install-git-hooks.ps1     安装逻辑 + 正反双向自检
    │   ├── vault.cmd                 ★ 双击查看 vault 状态
    │   └── vault.ps1                 加解密真实账单（7z AES-256）
    ├── docs\DATA-SAFETY.md           本文件
    ├── .gitignore
    └── .gitattributes
```

**为什么要分成两个位置：** 因为「在仓库里但被忽略」和「在仓库外」的安全等级完全不同。被忽略的文件仍然躺在工作区，一次 `git add -f`、一次改错 `.gitignore`、一次换工具导入，就会漏。物理隔离没有这个问题。

---

## 3. 三层防线怎么用

### 3.1 结构层：数据出仓库

已通过 `.gitignore` 落地。核心规则：

- `imports/**`、`data/**` 全部忽略，只保留 `.gitkeep` 占位
- 所有账单相关扩展名硬忽略：`csv / tsv / xls / xlsx / db / sqlite / 7z / ...`
- 密钥与凭据硬忽略：`*.key / *.pem / .env / secrets/ / ...`
- 图片默认忽略（账单截图常含卡号），只对 `assets/**`、`docs/img/**` 开口子

**不要把账单截图塞进 `assets/` 或 `docs/img/`**，那两个目录在忽略清单之外。

### 3.2 闸门层：提交前拦截

安装只需一次。**双击 `scripts\setup.cmd`** 即可，或在命令行跑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-git-hooks.ps1
```

为什么必须带 `-ExecutionPolicy Bypass`：Windows 默认执行策略是 Restricted，会直接拒绝运行本地 `.ps1`，报「在此系统上禁止运行脚本」。Bypass 只对本次进程生效，不修改系统设置，比 `Set-ExecutionPolicy` 干净。`setup.cmd` 里已经帮你带上了。

脚本会做五件事：检查脚本编码与换行符 → 把 `core.hooksPath` 指向 `.githooks` → 空暂存区放行自检（1/2）→ 伪造 `imports/__selftest__.csv` 验证能拦下（2/2）→ 清理残留。

**只有两项自检都通过、且退出码为 0，才算装好。** 装好后 `git commit` 只要命中下面任意一条就会被拒绝：

1. 路径在黑名单目录内：`imports/` `data/` `vault/` `secrets/` `exports/` `credentials/`
2. 扩展名在黑名单内：账单表格、数据库、加密包、密钥、`.env`
3. 文件大于 2 MB（账单/数据库误拖入的典型信号）
4. 内容命中：私钥头、`AKIA...` / `ghp_...` / `sk-...` API 密钥、18 位身份证号、16–19 位银行卡号、11 位手机号

**`fixtures/` 是唯一的例外**，且只豁免第 1、2 条：脱敏后的合成样例本来就要进仓库，
否则解析器没法测。第 3、4 条对它照常生效——里面不许出现真实的身份证号、卡号、手机号。
换句话说：`fixtures/` 里的 csv/xlsx 可以提交，但里面不能有真东西。

想临时绕过（不建议）：

```bash
MYLEDGER_ALLOW_SENSITIVE=YES_I_AM_SURE git commit -m "..."
```

`core.hooksPath` 是本地配置，不随仓库推送。**换机器或重新克隆后，必须重新跑一次 `setup.cmd`**，否则闸门不存在。

### 3.3 设备层：BitLocker

这一步只能你自己在系统里开，脚本替代不了。

1. 设置 → 隐私和安全性 → 设备加密（或控制面板 → BitLocker 驱动器加密）
2. 对 C 盘开启，恢复密钥**抄到纸上或存到手机里，不要存在同一台电脑上**
3. 用管理员身份跑 `manage-bde -status C:` 确认 `Protection On`

这是唯一能防「硬盘被拆下来直接读」的手段。**没有 BitLocker，前面两层在物理接触面前全部失效。**

---

## 4. 用真实数据测试的两种模式

### 模式 A：脱敏 fixture（默认，推荐）

真实数据不出仓库外，仓库里用结构相同、值全假的样例。

脱敏规则：

| 字段 | 处理方式 |
|---|---|
| 姓名 / 昵称 | 替换为「用户A」这类占位符 |
| 手机号 | `138****0000` |
| 身份证号 | 整列删除或替换为固定假号 |
| 银行卡号 / 账号 | 只保留后 4 位，其余替换 |
| 交易单号 / 商户单号 | 重新生成随机串 |
| 商户名称 | 替换为泛化类型（「连锁超市」「外卖平台」） |
| 金额 | 保留两位小数结构，数值整体缩放或替换 |
| 日期时间 | 整体平移固定天数（保持间隔关系） |

**表头可以原样保留**——程序要靠它解析。表头不含个人信息，闸门也不会因为表头拦你。

### 模式 B：加密容器（需要用完整真实数据时）

双击 `scripts\vault.cmd` 会直接显示状态。带参数时这样用（命令行）：

```powershell
# 1. 首次创建 vault
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 init

# 2. 把导出的真实账单放进 %USERPROFILE%\MyLedgerVault\plain\
#    然后打包加密（交互输入两遍密码）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 add

# 3. 清掉明文
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 close

# ---- 需要测试时 ----
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 open     # 解密到 plain\
#    让程序指向 %USERPROFILE%\MyLedgerVault\plain\ 跑测试
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 close    # 测完立刻清掉

# 随时查看状态与安全检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 status
```

等价写法：`scripts\vault.cmd open` / `scripts\vault.cmd add` / `scripts\vault.cmd close` / `scripts\vault.cmd status`。

加密参数是 `-t7z -mhe=on -mx=9`，即 7z 格式 + AES-256 + **加密文件名**。这一点已经实测过：不给密码时连归档里有哪些文件都列不出来，错误密码一个字节也解不出来。

**纪律：`open` 之后必须在同一次工作结束时 `close`。** 明文躺在磁盘上的每一分钟，都在累积风险。

`status` 会自动检查两件容易犯的错：vault 是否意外落在 Git 仓库内、路径是否命中云同步目录关键词（OneDrive / 坚果云 / 百度网盘等）。

**密码纪律：**
- 密码只存在于你脑子里，脚本、环境变量、文件里都不写
- 密码丢失 = 数据永久丢失，7z 的 AES-256 没有找回途径
- 不要在 vault 目录里放任何写着密码的文本文件

> 一处已知残留风险：7z 只支持 `-p<密码>` 这种命令行传参，同机管理员理论上可读进程命令行。单用户家用机上这个风险可接受（管理员本来就能读你所有文件），但要知道它存在。真要消除，改用 `age` 或 `gpg`（支持密钥文件、非对称加密）——那是后续加码项，不是现在的必选项。

---

## 5. 事故处置：已经提交了真实数据怎么办

**第一件事：接受现实——删文件没用。** 以下操作全部无效：

- ❌ 删掉文件再 commit 一次
- ❌ `git rm` 之后 push
- ❌ 把仓库改回 private
- ❌ 删除 GitHub 上的仓库（fork 和缓存可能还在）

因为历史提交对象仍存在于 `.git/objects` 中，会被完整推送。

**正确处置按是否推送过分两种情况：**

### 情况 1：还没推送（只在本地）

```bash
# 把那次提交整个撤掉，文件回到工作区
git reset --soft HEAD~1
# 然后从暂存区移除敏感文件，并加进 .gitignore
git rm --cached -- imports/xxx.csv
```

### 情况 2：已经推送到 GitHub

改写历史的工具已经变了，`git filter-branch` 已不推荐。两种选择：

**选项 A：删库重建（最简单，强推）**

如果能接受提交历史归零，这是最省事、最不容易出错的做法：

1. 本地把敏感数据移出仓库、修好 `.gitignore`
2. GitHub 上删除仓库
3. 重新 `git init`、新建仓库、重新推

**选项 B：改写历史**

```bash
pip install git-filter-repo
git filter-repo --path imports/xxx.csv --invert-paths
git push --force --all
git push --force --tags
```

注意：`filter-repo` 会改写所有提交哈希。所有协作者需要重新克隆。GitHub 上旧对象会在服务端保留一段时间才被 GC。

### 无论哪种情况，都必须做的事

1. **假设数据已泄露**，不要自我安慰「应该没人看到」。GitHub 的公开仓库被爬虫持续扫描，几十秒内就会被抓走。
2. 联系银行/支付平台，评估是否需要换卡、改密码
3. 修改所有用这些消费信息做过密保答案的账号
4. 如果仓库是 private，检查协作者列表和 deploy key，撤销可疑凭证

---

## 6. 推上 GitHub 前的检查清单

每次 `git push` 前跑一遍：

```bash
# 1. 确认没有任何数据文件被跟踪
git ls-files | findstr /I "csv xlsx db sqlite key pem env 7z"

# 2. 看一眼提交内容里有没有不该有的东西
git log -p --stat -3

# 3. 全历史扫描敏感特征（一次性彻底检查）
git log -p --all | findstr /R "BEGIN.*PRIVATE KEY"
```

对照清单：

- [ ] `git ls-files` 输出里没有任何 `.csv / .xlsx / .db / .7z / .key / .env`
- [ ] 仓库是 **private**（记账软件代码本身也可能暴露攻击面）
- [ ] 没有任何硬编码的 API 密钥、数据库连接串
- [ ] `fixtures/` 里的样例数据确认已脱敏，无真实姓名/手机号/卡号
- [ ] 提交闸门已在当前机器生效（跑一次安装脚本的自检）
- [ ] `%USERPROFILE%\MyLedgerVault` 不在任何云盘同步目录内

---

## 7. 明确不推荐的做法

| 做法 | 为什么不行 |
|---|---|
| 把真实账单放 private 仓库 | private 不等于安全：协作者可见、账号被盗即全泄、克隆到任何机器都落盘明文 |
| 只靠 `.gitignore` 挡 | 一次 `git add -f`、一次改错规则、一次换工具就穿透。必须有闸门兜底 |
| 把真实账单发给云端 AI 处理 | 数据离开你的机器就脱离了你的控制，服务条款里通常有训练/留存条款 |
| 密码写进代码或提交 `.env` | 密钥和密文同处一地 = 钥匙插在锁上。闸门会拦，但别依赖闸门 |
| 把 vault 放 OneDrive / 坚果云 | 密文自动上传云端，且同步冲突会损坏归档 |
| 用「改名成 .txt」这类小聪明 | 混淆不等于加密，任何文本工具都能打开 |
| 在 GitHub Actions 里处理真实数据 | CI 日志和 artifact 都可能被其他协作者或服务商读取 |

---

## 8. 后续可以加码的

软件成型、真实数据成为常态之后：

1. **数据库级加密（SQLCipher）** —— 把 SQLite 换成 SQLCipher，密钥在程序启动时输入。数据在磁盘上始终是密文，不需要手动 `add/close`，体验最好。代价是引入依赖、密钥管理要设计。
2. **密钥派生** —— 用户密码 → Argon2/scrypt → 数据库密钥，不存明文密钥。
3. **自动清理明文** —— 程序退出时自动删除临时解密目录。
4. **审计日志本地化** —— 不联网上报，避免账目信息外流。

这几项等到数据模型稳定后再做，现在上属于过度设计。

---

## 9. 已知踩坑（Windows 专属，别重犯）

这几条都是搭这套防线时真实踩出来的，写下来免得以后改脚本又栽进去。

### 9.1 `.ps1` 必须有 UTF-8 BOM

Windows PowerShell 5.1 读取无 BOM 的 `.ps1` 时会按系统 ANSI 代码页（简体中文即 GBK）解码，
中文注释直接变乱码，乱码还会吃掉字符串引号，报出「表达式或语句中包含意外的标记」这类
看不懂的语法错误。**保存 `.ps1` 时一律带 UTF-8 BOM。**

反过来说，`.githooks/pre-commit` 是 shell 脚本，**绝对不能有 BOM** —— BOM 会破坏 shebang，
Git 会拒绝执行。一个是 BOM 必须有，一个是绝对不能有，别搞混。

### 9.2 钩子的 shebang 用 `#!/bin/sh`，不要用 `#!/usr/bin/env bash`

Windows 上 PATH 里的 `bash` 往往是 WSL 转发器
（`%LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe`）。`env bash` 会优先命中它，
钩子被 WSL 接管后直接失败——闸门看起来装了，实际一行都没跑。

Git for Windows 会把 `/bin/sh` 可靠地映射到自带的 `sh.exe`（实测是 Bash 5.3），
所以用 `#!/bin/sh`。脚本开头另有一段守卫，处理 Debian 系 Linux 上 `/bin/sh` 其实是 dash 的情况。

### 9.3 钩子里不要用 `head` / `grep` / `sed` / `cat`

Git for Windows 执行钩子时，PATH 未必包含 Git 自带的 coreutils。实测 `head` 直接报
`command not found`。后果很恶劣：报错写 stderr，退出码可能仍是 0，闸门静默失效。

所以钩子只用 shell 内置命令：正则用 `[[ =~ ]]`，字符串判断用 `case`，不用任何外部工具。

### 9.4 `2>&1` + `$ErrorActionPreference='Stop'` 是个陷阱

PowerShell 里 native 命令（git、7z）的 stderr，在 `$ErrorActionPreference='Stop'` 下会被
包装成 `NativeCommandError` 并当作**终止性错误**抛出。而闸门被拦截时本来就会输出到 stderr，
于是自检必然崩在半路。脚本里凡是调用 native 命令并需要读 stderr 的地方，都临时降级为
`Continue`，跑完再恢复。

### 9.5 清理动作不要依赖回收站

自检的清理步骤如果走 `Remove-Item`，在部分安全策略/沙箱环境下会因回收站操作失败而抛异常，
结果暂存区留下幽灵条目（工作区文件没了，索引里还挂着），下次提交就会把它带进去。
清理一律用 `.NET` 的 `[System.IO.File]::Delete()`，结果确定。

### 9.6 中文输出建议在 Git Bash 里看

钩子的提示信息是 UTF-8。Git Bash（mintty）是 UTF-8 终端，显示正常；
在 `cmd.exe` / Windows PowerShell 默认的 GBK 代码页里可能显示为乱码。
**提交时用 Git Bash**，或先 `chcp 65001`。

### 9.7 闸门会把自己的源码判成私钥（自我误报）

钩子里那条检测私钥的正则，如果写成完整字面量，`pre-commit` 文件自身就包含了
那段标记词，于是它每次都会把自己判成私钥——**结果是谁也提交不了任何东西**。
更坑的是：在注释里解释这个坑的时候，如果把标记词原样写出来，会再次触发。

规避办法是把标记词拆成两段拼接（`PRIV''ATE`），正则完全等价，但源码里不再出现
可被自身匹配的连续字面量。改这个文件时记住：**不要在注释里复述那个标记词。**

### 9.8 命令替换会把二进制文件的 NUL 丢掉并打印警告

`content=$(git cat-file -p ":$f")` 读到 xlsx / zip / db 这类含 NUL 的文件时，
bash 会丢掉 NUL 并往 stderr 打一行 `warning: ignored null byte in input`。
这行警告每次提交都会出现，会训练人忽略钩子的输出——比报错更糟。

改用 `IFS= read -r -d '' -n "$MAX_SCAN" content < <(git cat-file -p ":$f")`：
`read` 遇到 NUL 就停，变量里永远不会出现 NUL；文本文件没有 NUL，行为与原来一致。

### 9.9 加白名单要同时改两个地方

给 `fixtures/` 开豁免时，只改 `.gitignore` 不够，只改钩子也不够——
**两处都改，否则要么文件提交不上去，要么闸门把合法样例拦下**。
另外 gitignore 是「最后匹配者生效」，`!fixtures/**` 之后要记得把
`fixtures/private/**` 重新排除一次。

---

## 10. 本方案的验证记录

不是「写完了应该能跑」，而是实际跑过。当前状态：

| 验证项 | 方法 | 结果 |
|---|---|---|
| 脚本语法 | PowerShell Parser 解析两个 `.ps1` | 通过 |
| 钩子语法 | `bash -n .githooks/pre-commit` | 退出码 0 |
| 闸门安装 | `-ExecutionPolicy Bypass -File` 完整执行 | 退出码 0，自检 1/2、2/2 均通过 |
| 拦截敏感路径 | 临时仓库提交 `imports/fake_bill.csv` | 拒绝，历史为空 |
| 放行正常代码 | 临时仓库提交 `app.py` | 成功提交 |
| 拦截身份证号 | `config_sample.txt` 内嵌 18 位身份证 | 拒绝 |
| 拦截手机号 | `profile.txt` 内嵌 11 位手机号 | 拒绝 |
| 拦截银行卡号 | `account.txt` 内嵌 19 位卡号 | 拒绝 |
| 7z 加密打包 | `-t7z -mhe=on` | 退出码 0 |
| 文件名已加密 | 无密码列归档 | 退出码 255，列不出来 |
| 密码正确可读 | 带密码列归档 | 退出码 0 |
| 密码错误解不开 | 错误密码解压 | 退出码 2，解出 0 个文件 |
| 密码正确可还原 | 正确密码解压 | 1 个文件，字节数一致 |
| vault 状态检查 | `vault.ps1 status` | 正常，Git / 云盘检查生效 |

**唯一没做的：BitLocker。** 那需要你在系统设置里开，脚本无法代劳。这是三层防线里唯一还空着的一层。
