"""网页「未分类」页里的建议分类：批量拍板那条链。

命令行有 `importer suggest`，但用户的日常入口是网页（双击 MyLedger.cmd），
功能不进网页等于没做。所以这里验的是**网页那条链**，不是 CLI：

  * `suggest_data()` —— 只读建议
  * `act_setcat_batch()` —— 一次性采纳多个商户（试算 / 执行）

四件必须守住的事：

1. **遮蔽模式不能漏名字。** 依据原文里嵌着**另一个**商户的名字
   （「名字里含已归类的「某餐饮店」」）。遮蔽了商户列却在依据列把
   别人的名字放出去，"可安全外发"就成了空话 —— 这条是硬校验，
   扫整个 JSON 里有没有出现任何已知商户名。
2. **遮蔽模式下不能采纳。** 提交的是页面上的商户名，打了码再提交
   会把「某·」这种东西写进缓存表。前端不给点，后端也要能兜住。
3. **试算不改历史，执行才改。** 但两者**都写缓存** ——
   那是「以后导入自动生效」的依据，和回不回填历史是两件事。
4. **批量只备份一次。** 分几次写会堆出一批只差一两个商户的备份，
   恢复时根本分不清该用哪份。

用法：C:\\Users\\34759\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\test_webui_suggest.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import secure_store, suggest  # noqa: E402
from importer.categorize import normalize_text  # noqa: E402
from importer.webui import act_setcat_batch, suggest_data  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def _ins(led, ts, amount, direction, counterparty, category,
         review="", item="", source="alipay"):
    # 刻意用裸 SQL：led.add_txn 会自己算指纹、走一遍去重判定，
    # 造不出「已确认的重复」这种状态。
    led.conn.execute(
        "INSERT INTO txn (source, ts, amount, direction, counterparty,"
        " item, category, review, fingerprint)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (source, ts, amount, direction, counterparty, item, category,
         review, f"fp-{ts}-{amount}-{counterparty}"))


def _make_ledger(tmp: str) -> str:
    """造账本：3 个已归类的商户（当依据来源）+ 3 个未分类的 + 1 笔重复。"""
    vault = os.path.join(tmp, "vault")
    with secure_store.open_encrypted(vault, save_on_exit=True) as led:
        # 已归类 —— 建议的依据从这里来
        _ins(led, "2026-09-03 12:00:00", "35.20", "expense", "某餐饮店",
             "餐饮", item="午餐")
        _ins(led, "2026-09-03 13:00:00", "3.00", "expense", "武汉地铁",
             "交通", item="乘车")
        _ins(led, "2026-09-04 09:00:00", "12.00", "expense", "某便利店",
             "日用百货", item="日用品")
        # 未分类 1：名字含已归类的「某餐饮店」→ 该有建议
        _ins(led, "2026-09-05 12:00:00", "48.00", "expense",
             "某餐饮店（分店）", "未分类", item="晚餐")
        # 未分类 2：和已归类的「武汉地铁」相似但不含它 → 走 similar 分支
        # （写成「武汉地铁集团」就会被 contains 先截胡，那是正确行为，
        #   但那样这条用例就测不到 similar 了）
        _ins(led, "2026-09-06 08:00:00", "4.00", "expense", "武汉轨道地铁",
             "未分类", item="乘车")
        # 未分类 3：八竿子打不着 → **不该**有建议
        _ins(led, "2026-09-07 20:00:00", "156.00", "expense", "完全没见过的店",
             "未分类", item="日用杂货")
        # 已确认重复的未分类笔：不该出现在建议里，也不该被采纳
        _ins(led, "2026-09-08 20:00:00", "88.00", "expense", "某重复商户",
             "未分类", review="confirmed_dup", item="重复")
    return vault


def _by_merchant(rows: list) -> dict:
    # 键要归一化：normalize_text 会把全角括号「（）」转成半角「()」，
    # 直接拿原始名字去查会 KeyError（这个坑在 test_suggest.py 里踩过一次）。
    return {normalize_text(r["merchant"]): r for r in rows}


def _row(mapping: dict, name: str) -> dict:
    return _by_merchant(mapping)[normalize_text(name)]


def case_readonly():
    print("\n[1] suggest_data 是只读的（刷页面不该动账本）")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        enc = secure_store.encrypted_path(vault)
        before = os.path.getsize(enc)
        d = suggest_data(vault)
        check("刷完大小没变", os.path.getsize(enc), before)
        check("返回了商户", d["totals"]["merchants"] > 0, True)


def case_suggestion_present():
    print("\n[2] 有依据的给建议，没依据的留空（不瞎猜）")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        m = suggest_data(vault)["rows"]
        hit = _row(m, "某餐饮店（分店）")
        check("子串命中 → 有分类", hit["category"], "餐饮")
        check("来源是 contains", hit["source"], "contains")
        check("依据写了是谁", "某餐饮店" in hit["evidence"], True)
        sim = _row(m, "武汉轨道地铁")
        check("相似命中 → 有分类", sim["category"], "交通")
        check("来源是 similar", sim["source"], "similar")
        none = _row(m, "完全没见过的店")
        check("没依据 → 分类为空", none["category"], "")
        check("没依据 → 依据为空", none["evidence"], "")
        check("商品名样本带出来了", none["sample_item"], "日用杂货")


def case_confirmed_dup_excluded():
    print("\n[3] 已确认的重复既不出现在建议里，也不当依据来源")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        m = suggest_data(vault)["rows"]
        check("重复商户不在建议里", normalize_text("某重复商户") in _by_merchant(m), False)


def case_mask_leaks_nothing():
    print("\n[4] 遮蔽模式：整个响应里不许出现任何真实商户名")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = suggest_data(vault, mask=True)
        blob = json.dumps(d, ensure_ascii=False)
        names = ["某餐饮店", "武汉地铁", "某便利店", "完全没见过的店"]
        leaked = [n for n in names if n in blob]
        check("一个都没漏", leaked, [])
        # 依据要换成不点名的说法，而不是整句打码
        ev = [r["evidence"] for r in d["rows"] if r["evidence"]]
        check("依据还在（只是不点名）", len(ev) > 0, True)
        check("依据里没有「某餐饮店」", any("某餐饮店" in e for e in ev), False)
        # 商品名不打码（打码规则套不上），直接不给
        check("遮蔽模式不显示商品名",
              all(not r["sample_item"] for r in d["rows"]), True)


def case_batch_dry_run():
    print("\n[5] 试算：写缓存、不改历史、不备份")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        log: list[str] = []
        res = act_setcat_batch(
            vault,
            {"items": [{"merchant": "完全没见过的店", "category": "购物"},
                       {"merchant": "某重复商户", "category": "其他"}],
             "apply": False},
            log.append)
        check("采纳了 2 个商户", res["merchants"], 2)
        check("标记为未落库", res["applied"], False)

        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            from importer.categorize import manual_map
            manual = manual_map(led)
            check("缓存写进去了", manual.get("完全没见过的店"), "购物")
            cat = led.conn.execute(
                "SELECT category FROM txn WHERE counterparty = ?",
                ("完全没见过的店",)).fetchone()["category"]
        check("历史交易没动", cat, "未分类")

        enc = secure_store.encrypted_path(vault)
        folder = os.path.dirname(enc)
        backups = [n for n in os.listdir(folder) if ".backup-" in n]
        check("试算不产生备份", backups, [])


def case_batch_apply():
    print("\n[6] 执行：回填历史 + 只备份一次")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        items = [{"merchant": "完全没见过的店", "category": "购物"},
                 {"merchant": "某重复商户", "category": "其他"}]
        log: list[str] = []
        res = act_setcat_batch(vault, {"items": items, "apply": True}, log.append)
        check("标记为已落库", res["applied"], True)
        check("改动了 1 笔（重复那笔排除在外）", res["changed"], 1)

        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            cat = led.conn.execute(
                "SELECT category FROM txn WHERE counterparty = ?",
                ("完全没见过的店",)).fetchone()["category"]
        check("历史已回填", cat, "购物")

        enc = secure_store.encrypted_path(vault)
        folder = os.path.dirname(enc)
        backups = [n for n in os.listdir(folder) if ".backup-" in n]
        check("只备份了一份", len(backups), 1)


def case_batch_rejects_bad_category():
    print("\n[7] 非法分类整批拦下，一条都不写")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        items = [{"merchant": "完全没见过的店", "category": "购物"},
                 {"merchant": "某餐饮店（分店）", "category": "随便瞎写的"}]
        log: list[str] = []
        raised = ""
        try:
            act_setcat_batch(vault, {"items": items, "apply": True}, log.append)
        except ValueError as exc:
            raised = str(exc)
        check("抛了 ValueError", bool(raised), True)
        check("错误信息点名是哪个商户", "某餐饮店（分店）" in raised, True)

        # 关键：前面那条**也不能**被写进去
        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            from importer.categorize import manual_map
            check("合法的也没写进去", manual_map(led).get("完全没见过的店"), None)


def case_batch_rejects_empty():
    print("\n[8] 空选择 / 畸形参数不能静默通过")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        log: list[str] = []
        for bad in ({}, {"items": []}, {"items": None},
                    {"items": ["不是字典"]}, {"items": [{"merchant": "   "}]}):
            try:
                act_setcat_batch(vault, bad, log.append)
                check(f"拒绝了 {bad!r}", False, True)
            except ValueError:
                check(f"拒绝了 {bad!r}", True, True)


def case_batch_only_one_backup_for_many():
    print("\n[9] 10 个商户一次采纳，也只备份一份密文")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        items = [{"merchant": f"批量商户{i}", "category": "购物"} for i in range(10)]
        with secure_store.open_encrypted(vault, save_on_exit=True) as led:
            for i in range(10):
                led.conn.execute(
                    "INSERT INTO txn (source, ts, amount, direction, counterparty,"
                    " item, category, review, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("alipay", f"2026-09-10 0{i}:00:00", "10.00", "expense",
                     f"批量商户{i}", "杂货", "", "", f"fp-batch-{i}"))
        log: list[str] = []
        res = act_setcat_batch(vault, {"items": items, "apply": True}, log.append)
        check("10 个都采纳了", res["merchants"], 10)
        check("改了 10 笔", res["changed"], 10)
        enc = secure_store.encrypted_path(vault)
        folder = os.path.dirname(enc)
        check("备份仍然只有一份",
              len([n for n in os.listdir(folder) if ".backup-" in n]), 1)


def case_evidence_label():
    print("\n[10] 不点名的依据：遮蔽模式用它，且里面没有商户名")
    for src in (suggest.SOURCE_SELF, suggest.SOURCE_CONTAINS,
                suggest.SOURCE_SIMILAR, suggest.SOURCE_AI):
        label = suggest.evidence_label(src)
        check(f"{src} 有说法", bool(label), True)
        check(f"{src} 不点名", "「" in label, False)
    check("none 是空的", suggest.evidence_label(suggest.SOURCE_NONE), "")


def case_self_history_is_evidence():
    print("\n[11] 商户自己的历史归类就是依据（不该被「跳过自己」漏掉）")
    # 真实账本里出现过：同一个商户，早先某批被规则认出来了，
    # 后面某批没认出来 —— 于是它「既已归类又未分类」。
    # 以前 _guess_from_known 里有 `if other == merchant: continue` 跳过自己，
    # 结果答案就在库里却报「没依据」。
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        with secure_store.open_encrypted(vault, save_on_exit=True) as led:
            _ins(led, "2026-09-01 12:00:00", "30.00", "expense",
                 "反复出现的店", "餐饮", item="午餐")
            _ins(led, "2026-09-11 12:00:00", "31.00", "expense",
                 "反复出现的店", "未分类", item="晚餐")

        d = suggest_data(vault)
        row = _row(d["rows"], "反复出现的店")
        check("给了建议", row["category"], "餐饮")
        check("来源是 self", row["source"], suggest.SOURCE_SELF)
        check("依据写明是自己的历史", "自己有" in row["evidence"], True)
        check("self 置信度高于子串命中", row["confidence"] > suggest.CONF_CONTAINS, True)


def case_mask_short_name():
    print("\n[12] 单字商户名在遮蔽模式下整个打掉（不能原样漏出去）")
    # 真实账单里出现过单字母商户名，遮蔽后原样出现在输出里 —— 就是这么发现的。
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        with secure_store.open_encrypted(vault, save_on_exit=True) as led:
            _ins(led, "2026-09-02 12:00:00", "9.00", "expense", "F",
                 "未分类", item="单字母商户")

        plain = suggest_data(vault)
        check("单字商户确实在列表里",
              normalize_text("F") in _by_merchant(plain["rows"]), True)

        d = suggest_data(vault, mask=True)
        blob = json.dumps(d, ensure_ascii=False)
        check("遮蔽后响应里没有 F", "F" in blob, False)
        check("遮蔽成 1 个点（长度保留）",
              sorted(_by_merchant(d["rows"])), ["·"])


def main() -> int:
    print()
    print("=" * 78)
    print("网页「未分类」页：建议分类 + 批量拍板")
    print("=" * 78)
    case_readonly()
    case_suggestion_present()
    case_confirmed_dup_excluded()
    case_mask_leaks_nothing()
    case_batch_dry_run()
    case_batch_apply()
    case_batch_rejects_bad_category()
    case_batch_rejects_empty()
    case_batch_only_one_backup_for_many()
    case_evidence_label()
    case_self_history_is_evidence()
    case_mask_short_name()

    print()
    print("=" * 78)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
