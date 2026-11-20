"""Bounded QQ operations using the existing authenticated game client."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

from . import bot_shop_purchase as purchases
from .game_client import GameError, TaskDeferred
from .runtime_data import pinned


LONG_TASKS = {"activity", "abyss", "buy_batch", "refresh_secret_paid", "refresh_star_source", "buy_shop_equipment",
              "sweep_hunt", "sweep_elf", "sweep_activity"}
SHOP_TASKS = {"refresh_secret", "refresh_star_source"}
MAX_SHOP_ROUNDS = 200
STAR_SOURCE_QUOTE_UNAVAILABLE = "当前缺少可核实的星源商店刷新报价，本次未提交刷新。"
SECRET_SHOP_QUOTE = Path(__file__).resolve().parents[1] / "assets" / "secret_shop_diamond_quote.json"


def secret_shop_paid_order(code):
    try:
        order = json.loads(code)
        policy = json.loads(SECRET_SHOP_QUOTE.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    required = {"count", "quote", "budget", "scheme"}
    if not isinstance(order, dict) or not required <= order.keys() <= required | {"targets"}:
        return None
    count, scheme = order["count"], order["scheme"]
    if type(count) is not int or not 1 <= count <= MAX_SHOP_ROUNDS:
        return None
    if scheme is not None and (not isinstance(scheme, dict) or not isinstance(scheme.get("slots"), dict)):
        return None
    if not isinstance(policy, dict):
        return None
    quote = {key: policy.get(key) for key in ("gameVersion", "catalog", "currency", "unitPrice")}
    if (quote["currency"] != "2" or type(quote["unitPrice"]) is not int or quote["unitPrice"] < 1
            or not isinstance(quote["gameVersion"], str) or not isinstance(quote["catalog"], str)
            or not isinstance(order["quote"], dict) or type(order["quote"].get("unitPrice")) is not int
            or order["quote"] != quote or type(order["budget"]) is not int
            or order["budget"] != count * quote["unitPrice"]):
        return None
    limits = policy.get("vipDailyLimits")
    if not isinstance(limits, list) or not limits or any(type(value) is not int or value < 0 for value in limits):
        return None
    targets = purchases.parse_targets(order.get("targets", []))
    return (count, quote, limits, scheme, targets) if targets is not None else None


def star_source_order(code):
    try:
        order = json.loads(code)
    except (ValueError, RecursionError):
        return None
    policy = purchases.star_quote()
    if (not policy or not isinstance(order, dict) or set(order) != {"count", "quote", "budget", "scheme"}
            or order["quote"] != policy or type(order["count"]) is not int
            or not 1 <= order["count"] <= MAX_SHOP_ROUNDS
            or type(order["budget"]) is not int or order["budget"] != order["count"] * policy["refresh"]["amount"]
            or not isinstance(order["scheme"], dict) or not isinstance(order["scheme"].get("slots"), dict)
            or not order["scheme"]["slots"]):
        return None
    return order["count"], policy, order["scheme"]


def parse_shop(task: str, code: str):
    words = unicodedata.normalize("NFKC", code).split()
    if not words:
        return 1, "free" if task == "refresh_secret" else "star"
    if not re.fullmatch(r"[1-9][0-9]{0,2}", words[0]) or int(words[0]) > MAX_SHOP_ROUNDS:
        return None
    count = int(words[0])
    if task == "refresh_star_source" and len(words) == 1:
        return count, "star"
    if task == "refresh_secret" and (len(words) == 1 or len(words) == 2 and words[1] in {"free", "diamond"}):
        return count, words[1] if len(words) == 2 else ("free" if count == 1 else "choose")
    return None


def parse_shop_request(task: str, code: str):
    scheme = None
    targets = []
    if code.startswith("{"):
        try:
            body = json.loads(code)
        except (ValueError, RecursionError):
            return None
        required = {"order", "scheme"}
        if not isinstance(body, dict) or not required <= body.keys() <= required | {"targets"}:
            return None
        code, scheme = body["order"], body["scheme"]
        targets = purchases.parse_targets(body.get("targets", []))
        if (not isinstance(code, str) or targets is None
                or scheme is not None and (not isinstance(scheme, dict) or not isinstance(scheme.get("slots"), dict))):
            return None
    order = parse_shop(task, code)
    return (order, scheme, targets) if order else None


def parse(task: str, code: str):
    from .bot_sweep import TASKS as SWEEP_TASKS, parse_order

    if task in SWEEP_TASKS:
        return parse_order(task, code)
    parts = code.split()
    if task == "activity" and len(parts) == 4:
        family, team, count, energy = parts
        if (re.fullmatch(r"BH[0-9]{1,5}", family) and re.fullmatch(r"[A-Za-z0-9_-]{1,24}", team)
                and re.fullmatch(r"[1-3]", count) and re.fullmatch(r"[0-9]{1,3}", energy)):
            return family, team, int(count), int(energy)
    if task == "abyss" and len(parts) == 3:
        scene, *teams = parts
        slots = [team.split(",") for team in teams]
        if re.fullmatch(r"MysteriousRealm_[0-9]{1,2}", scene) and all(
                1 <= len(team) <= 4 and any(role != "-" for role in team)
                and all(re.fullmatch(r"[A-Za-z0-9_]{1,24}|-", role) for role in team) for team in slots):
            return scene, {half: [None if role == "-" else role for role in team]
                           for half, team in zip(("upper", "lower"), slots)}
    if task == "buy_batch" and 1 <= len(parts) <= 5:
        picks = []
        for part in parts:
            match = re.fullmatch(r"([A-Za-z0-9_]{1,32}):([1-9]|10):([0-9]{1,8}):([0-9]{1,8})", part)
            if not match:
                return None
            sid, count, item, price = match.groups()
            picks.append((sid, int(count), item, int(price)))
        if len({pick[0] for pick in picks}) == len(picks):
            return picks
    return None


def shop_rows(state):
    from .tasks import store_shelves

    return {row["id"]: row for store in store_shelves(state) for row in store["goods"]
            if row["hasRecord"] and row["id"] != "VIPGIFT_VIPQuick4"}


@pinned
def shop_reference():
    from .tasks import store_shelves

    goods = [row for shelf in store_shelves({}) for row in shelf["goods"]
             if row["id"] != "VIPGIFT_VIPQuick4"]
    return ["静态商品参考价，余额和可购数量在执行前核对。", *[
        f"{row['id']} {row['name']} · 货币 {row['cost']['item']} · 单价 {row['cost']['count']}"
        for row in goods[:7]
    ]]


def free_abyss_challenge(definition):
    from .game_data import enabled

    return (enabled(definition.get("ServerBattle")) and not definition.get("EnterCost")
            and definition.get("Energy") in (None, "", "0", 0))


def is_query(code: str) -> bool:
    from .bot_sweep_quotes import ACTIVITY_QUERY

    return code in {"activity", "abyss"} or ACTIVITY_QUERY.fullmatch(code) is not None


def query(state, kind, *, options=None):
    from .activity_progress import ready_to_start, records, scenes
    from .abyss_state import metadata
    from .bot_sweep_quotes import activity_details
    from .bot_views import stamina_count, stamina_detail
    from .tasks import abyss_scenes

    if kind.partition(" ")[0] == "activity_sweep":
        return activity_details(state, kind, options=options)
    if kind == "activity":
        saved = records(state)
        next_stages = {}
        for row in scenes(state):
            if row["serverBattle"] and ready_to_start(row, saved):
                next_stages.setdefault(row["family"], row)
        candidates = [row for row in next_stages.values()
                      if row["energyKnown"] and row["energy"] >= 0 and not row["extraEntryCost"]]
        if options is not None:
            options.extend(row["family"] for row in candidates[:10])
        teams = [str(row["ID"]) for row in state.get("Teams", {}).get("Settings", [])
                 if row.get("TeamSetting", {}).get("RolePosMap")]
        return ["活动 ID " + ("、".join(row["family"] for row in candidates[:12]) or "暂无"),
                "队伍 ID " + ("、".join(teams[:12]) or "暂无"),
                stamina_detail(stamina_count(state)),
                *[f"{row['activityName']} · 下一关 {row['id']} · 标价 {row['energy']} 体力"
                  + (" · 剧情队伍填 0" if row["storyTeam"] else "") for row in candidates[:5]]]
    definitions = metadata()["tables"]["Scene"]
    candidates = [row for row in abyss_scenes(state) if free_abyss_challenge(definitions.get(row["id"]) or {})][:10]
    stages = [row["id"] for row in candidates]
    if options is not None:
        options.extend(stages)
    if not stages:
        return ["暂无可选关卡"]
    details = ["可挑战楼层 " + "、".join(stages[index:index + 5]) for index in range(0, len(stages), 5)]
    cleared = "、".join(str(row["floor"]) for row in candidates if row.get("cleared")) or "暂无"
    return [*details, f"快照已通关楼层 {cleared}", "挑战体力上限 0。"]


def match_details(cards, position_key="index"):
    if not isinstance(cards, list):
        return []
    matches = []
    for card in cards:
        if not isinstance(card, dict) or type(position := card.get(position_key)) is not int or not 0 <= position < 6:
            continue
        fields = [card.get(key) for key in ("tierName", "setCN", "slotCN", "staticID")]
        label = " · ".join(value[:30] for value in fields if isinstance(value, str) and value)
        if not label:
            continue
        score = card.get("auxScore")
        suffix = f" · {score} 分" if type(score) in {int, float} and 0 <= score <= 10000 else ""
        matches.append(f"第 {position + 1} 档 · {label}{suffix}")
    return matches[:6]


def secret_hits(result, scheme, options):
    cards = result.get("legendaryMatches") if scheme else []
    matches = match_details(cards)
    found = []
    if matches and options is not None:
        records = result["records"]
        for card in cards:
            index = card.get("index") if isinstance(card, dict) else None
            if type(index) is int and 0 <= index < min(len(records), 6):
                if option := purchases.secret_option(records[index], index + 1):
                    found.append(option)
        options.extend(found)
    if matches and len(found) < len(matches):
        matches.append("部分命中装备的货架或结算货币无法核对，请在游戏内查看。")
    return matches


def refresh_shop(client, task, cancel, order=(1, "free"), scheme=None, targets=(), *, prepare=None, options=None):
    from .tasks import (
        UnknownEquipmentError, _equipment_matches_in_shelf, _record_goods_sid, _secret_shop_records,
        item_name, secret_shop_next_free, shop_reset,
    )
    count, mode = order
    if task == "refresh_star_source":
        return "quote_unavailable", [STAR_SOURCE_QUOTE_UNAVAILABLE]
    if mode != "free":
        return "quote_unavailable", ["当前游戏数据未提供可核实的钻石刷新报价。"]
    next_free = secret_shop_next_free(client.account_state or {})
    cooling = next_free is not None and next_free > time.time() * 1000
    ledger = purchases.Purchases()
    if prepare is not None and (targets or not cooling):
        prepare()
    if cancel.is_set():
        return "cancelled", []
    current_records = _secret_shop_records(client.account_state or {})
    if targets and (stop := ledger.buy(client, current_records, targets, cancel)):
        return stop, [ledger.summary(), "当前货架购买未完整确认，未刷新。"]

    def cooldown_result(message):
        details = [message, ledger.summary()]
        if scheme:
            try:
                cards = _equipment_matches_in_shelf(current_records, scheme)
            except UnknownEquipmentError:
                return ("partial" if ledger.count else "skipped"), [*details, "当前货架的装备数据待核对。"]
            current = {"records": current_records, "legendaryMatches": cards}
            if matches := secret_hits(current, scheme, options):
                return "shop_hit", [*details, *matches]
        return ("ok" if ledger.count else "skipped"), details

    if cooling:
        return cooldown_result(f"申请 {count} 次，实际刷新 0 次。免费额度仍在冷却。")
    result = shop_reset(client, use_gold=False, auto_buy_sids=[], equipment_scheme=scheme)
    if not result.get("ok") and str(result.get("error", "")).replace(" ", "").casefold() == "cantreset":
        return cooldown_result("游戏返回免费刷新未就绪。")
    records = result.get("records") or []
    details = [f"申请 {count} 次，实际刷新 1 次。新货架 {len(records)} 档。"]
    for index, record in enumerate(records[:6], 1):
        sid = _record_goods_sid(record)
        details.append(f"第 {index} 档 · {item_name(str(sid)) if sid else '装备'}")
    if result.get("costItems"):
        return "unexpected_cost", [details[0], ledger.summary(), *details[1:]]
    if records and result.get("ok"):
        stop = ledger.buy(client, records, targets, cancel) if targets else None
        summary = ledger.summary() if targets else "自动购买 0 件。"
        if stop:
            return stop, [details[0], summary, "部分购买结算待核对，后续交易已停止。"]
        matches = secret_hits(result, scheme, options)
        if matches:
            return "shop_hit", [details[0], summary, *matches]
        return "ok", [details[0], summary, *details[1:]]
    return "unconfirmed", ["游戏未返回可确认的秘密商店新货架。", ledger.summary()]


def refresh_secret_paid(client, args, cancel, progress, *, options=None):
    from .tasks import _item_count, shop_reset

    count, quote, limits, scheme, targets = args
    if (client.login_version, client.login_catalog) != (quote["gameVersion"], quote["catalog"]):
        return "quote_unavailable", ["游戏版本或资源目录已变化，本次未提交钻石刷新。"]
    state = client.account_state or {}
    vip, save = state.get("VIPInfo"), state.get("AccountSaveData")
    level = vip.get("LV") if isinstance(vip, dict) else None
    used = save.get("RandomStoreDayRefreshCount") if isinstance(save, dict) else None
    if type(level) is not int or not 0 <= level < len(limits) or type(used) is not int or used < 0:
        return "not_started", ["当前账号的每日钻石刷新额度无法核实。"]
    unit, currency = quote["unitPrice"], quote["currency"]
    available = min(max(0, limits[level] - used), max(0, _item_count(state, currency)) // unit)
    target = min(count, available)
    confirmed = 0
    ledger = purchases.Purchases()

    def receipt():
        return [f"申请 {count} 次，已确认刷新 {confirmed} 次，扣除 {confirmed * unit} 颗钻石。",
                ledger.summary() if targets else "自动购买 0 件。"]

    if not target:
        return "skipped", [*receipt(), "今日剩余额度或钻石余额不足。"]
    for round_number in range(1, target + 1):
        if cancel.is_set():
            return "cancelled", receipt()
        try:
            result = shop_reset(client, use_gold=True, auto_buy_sids=[], equipment_scheme=scheme)
        except TaskDeferred:
            return ("partial" if confirmed else "not_started"), [*receipt(), "账号状态需要同步。"]
        except Exception:
            return "unconfirmed", [*receipt(), f"第 {round_number} 次结算待核对。"]
        costs = result.get("costItems")
        if not isinstance(costs, list) or not costs or any(
                not isinstance(row, dict) or not isinstance(row.get("Item"), dict)
                or type(row["Item"].get("Count")) is not int or row["Item"]["Count"] <= 0 for row in costs):
            return "unconfirmed", [*receipt(), f"第 {round_number} 次扣款待核对。"]
        if any(str(row["Item"].get("StaticID")) != currency for row in costs) or sum(
                row["Item"]["Count"] for row in costs) != unit:
            return "unexpected_cost", [*receipt(), f"第 {round_number} 次实际扣款与确认报价不符。"]
        if not result.get("records"):
            return "unconfirmed", [*receipt(), f"第 {round_number} 次已确认扣除 {unit} 颗钻石，货架结果待核对。"]
        confirmed += 1
        progress(receipt()[0])
        if not result.get("ok"):
            return "partial", [*receipt(), "本轮货架的装备筛选未完成，已停止后续刷新。"]
        if targets and (stop := ledger.buy(client, result["records"], targets, cancel)):
            return stop, [*receipt(), "本轮购买未完整确认，已停止后续刷新。"]
        matches = secret_hits(result, scheme, options)
        if matches:
            return "shop_hit", [*receipt(), *matches]
        if round_number < target and cancel.wait(0.3):
            return "cancelled", receipt()
    if confirmed < count:
        return "partial", [*receipt(), "已达到今日剩余额度或可负担次数。"]
    return "ok", receipt()


def refresh_star_source(client, args, cancel, progress, *, options=None):
    from .tasks import _item_count, item_name, star_source_query, star_source_refresh

    count, quote, scheme = args
    if (client.login_version, client.login_catalog) != (quote["gameVersion"], quote["catalog"]):
        return "quote_unavailable", ["星源商店报价对应的游戏版本已变化。"]
    currency, unit = quote["refresh"]["currency"], quote["refresh"]["amount"]
    initial = star_source_query(client)
    used = initial.get("refreshCount")
    if not initial.get("ok") or type(used) is not int or used < 0:
        return "not_started", ["星源商店刷新计数无法核实。"]
    confirmed = 0

    def receipt():
        spent = f"扣除 {confirmed * unit} {item_name(currency)}" if unit else "刷新不消耗货币"
        return [f"申请 {count} 次，已确认刷新 {confirmed} 次，{spent}。"]

    for i in range(count):
        if cancel.is_set():
            return "cancelled", receipt()
        if unit and _item_count(client.account_state or {}, currency) < unit:
            return ("partial" if confirmed else "skipped"), [*receipt(), "刷新货币余额不足。"]
        try:
            result = star_source_refresh(client, scheme, include_shelf=True)
        except TaskDeferred:
            return ("partial" if confirmed else "not_started"), [*receipt(), "账号状态需要同步。"]
        except Exception:
            return "unconfirmed", [*receipt(), "本轮刷新结果待核对。"]
        if not result.get("ok"):
            return ("partial" if confirmed else "skipped"), [*receipt(), "游戏拒绝刷新。"]
        debit = purchases.costs(result.get("costItems"))
        if debit is None or type(result.get("refreshCount")) is not int:
            return "unconfirmed", [*receipt(), "本轮刷新扣款或计数待核对。"]
        if debit != ({currency: unit} if unit else {}):
            return "unexpected_cost", [*receipt(), "本轮实际扣款与报价不符。"]
        if result["refreshCount"] != used + 1 or not result.get("shelf"):
            return "unconfirmed", [*receipt(), "星源商店货架已变化，结果待核对。"]
        used += 1
        confirmed += 1
        progress(receipt()[0])
        matches = match_details(result["matches"], "shopIndex")
        if matches:
            found = []
            for card in result["matches"]:
                slot = card["shopIndex"]
                if 0 <= slot < len(result["shelf"]):
                    if option := purchases.star_option(result["shelf"][slot], slot + 1, result, quote.get("equipment")):
                        found.append(option)
            if options is not None:
                options.extend(found)
            if len(found) < len(matches):
                matches.append("缺少可核实报价或货架标识的装备需在游戏内购买。")
            return "shop_hit", [*receipt(), *matches]
        if i + 1 < count and cancel.wait(0.3):
            return "cancelled", receipt()
    return "ok", receipt()


def buy(client, picks, cancel, progress):
    from .tasks import _buy_payload, _collect_items, _item_count, _oid

    details = []
    totals = {}

    def checked_quote(state, pick, shelves):
        sid, count, item, price = pick
        row = shelves.get(sid)
        if not row or row["cost"]["item"] != item or row["cost"]["count"] != price:
            return None
        if row["remain"] is not None and row["remain"] < count:
            return None
        records = (state.get("StoreRecordContainer") or {}).get("Records") or []
        record = next((rec for rec in records if rec.get("StaticID") == sid), None)
        if record and _oid(record.get("_id")) and type(record.get("BuyCount")) is int:
            return record
        return None

    state = client.account_state or {}
    shelves = shop_rows(state)
    for pick in picks:
        if not checked_quote(state, pick, shelves):
            return "skipped", [f"{pick[0]} 的报价、购买记录或额度不符，未提交购买"]
        _, count, item, price = pick
        totals[item] = totals.get(item, 0) + count * price
    if any(_item_count(state, item) < total for item, total in totals.items()):
        return "skipped", ["当前货币余额低于本批报价，未提交购买"]

    for index, pick in enumerate(picks, 1):
        if cancel.is_set():
            return "cancelled", details
        sid, count, item, price = pick
        state = client.account_state or {}
        record = checked_quote(state, pick, shop_rows(state))
        if not record or _item_count(state, item) < count * price:
            return ("partial" if details else "skipped"), [*details, f"{sid} 的报价、额度或余额已变化"]
        progress(f"第 {index}/{len(picks)} 档 {sid}，已确认 {len(details)} 档")
        if cancel.is_set():
            return "cancelled", details
        try:
            response = client.call("StoreHandler.BuyCommodity", _buy_payload(client, record, count))
            updated = response.get("CommodityRecord") or {}
            spent = {}
            _collect_items(response.get("CostItems"), spent)
        except TaskDeferred:
            return ("partial" if details else "not_started"), [*details, "账号状态需要同步，本档购买请求未提交。"]
        except Exception:
            return "unconfirmed", [*details, f"{sid} 的购买请求未获得可核对的结算"]
        expected = {item: count * price} if price else {}
        if spent != expected:
            if spent:
                return "unexpected_cost", [*details, f"{sid} 的返回扣款与确认报价不符"]
            return "unconfirmed", [*details, f"{sid} 的扣款未确认"]
        if (updated.get("StaticID") != sid or _oid(updated.get("_id")) != _oid(record.get("_id"))
                or updated.get("BuyCount") != record["BuyCount"] + count):
            return "unconfirmed", [*details, f"{sid} 的购买数量未确认"]
        details.append(f"{sid} 已确认 {count} 份，消耗货币 {item} × {count * price}")
        progress(f"已确认 {len(details)}/{len(picks)} 档")
    return "ok", details


def activity(client, args, cancel, progress):
    from . import activity_progress

    family, team, count, energy = args

    def report(data):
        scene = data.get("sceneId")
        if scene:
            progress(f"活动 {family} · {scene} · 本轮确认 {data['confirmedCount']} 关"
                     f" · 按标价 {data['confirmedEnergy']} 体力")

    try:
        result = activity_progress.run(client, family, team, cancel_event=cancel, on_progress=report,
                                       server_limits=(count, energy))
    except TaskDeferred:
        return "not_started", ["账号状态需要重新同步。"]
    except GameError:
        return "not_started", ["当前活动、队伍或前置条件不可用。"]
    completed = result["completedScenes"]
    details = [f"确认通关 {len(completed)}/{count} 关，按关卡标价共 {result['confirmedEnergy']} 体力（预算 {energy}）",
               *completed]
    if getattr(client, "_activity_pending", None):
        return "unconfirmed", [*details, f"{result['lastSceneId']} 的结算待核对"]
    if cancel.is_set():
        return "cancelled", details
    if result["ok"]:
        return ("ok" if completed else "skipped"), details
    reason = {
        "activity_closed": "活动已结束。",
        "prerequisite": "暂无满足前置条件的服务端战斗关卡。",
        "energy_unknown": "下一关体力标价待核实。",
        "energy_budget": "下一关会超出本次体力上限。",
        "stamina_unknown": "账号体力未能同步。",
        "stamina": "账号体力不足以进入下一关。",
        "extra_cost": "下一关需要额外入场资源。",
        "scene_validation": "关卡配置未通过检查。",
        "team_validation": "关卡或队伍配置未通过检查。",
        "account_sync": "账号状态需要重新同步。",
    }.get(result.get("stopReason"))
    if reason:
        return ("partial" if completed else "not_started"), [*details, reason]
    return ("partial" if completed else "skipped"), [*details, "推进已停止，请查看游戏内活动与前置关卡"]


def abyss(client, args, cancel, progress):
    from .abyss_battle import run_abyss_battle
    from .abyss_state import metadata
    from .tasks import normalize_abyss_config

    scene, waves = args
    definition = metadata()["tables"]["Scene"].get(scene) or {}
    if not free_abyss_challenge(definition):
        return "skipped", ["此关卡的服务端战斗或入场消耗不符合本次授权"]
    try:
        params = normalize_abyss_config(client.account_state, scene, waves)
    except GameError:
        return "skipped", ["关卡未解锁或上下半阵容当前不可用"]
    if cancel.is_set():
        return "cancelled", []
    try:
        result = run_abyss_battle(client, {**params, "_cancel_event": cancel,
                                  "_on_progress": lambda data: progress(f"{scene} · 第 {data['wave'] + 1} 半场")})
    except TaskDeferred:
        return "not_started", ["账号状态需要同步，挑战请求未提交。"]
    except Exception:
        return "unconfirmed", [f"{scene} 的结算待核对"]
    outcome = str((result.get("raw") or {}).get("Result") or "").lower()
    if outcome == "win" and result.get("ok"):
        return "ok", [f"{scene} 服务端已返回胜利结算"]
    if outcome in {"lose", "defeat", "draw"}:
        return "skipped", [f"{scene} 服务端已结算，本次未通关"]
    return "unconfirmed", [f"{scene} 的结算待核对"]


@pinned
def run(account, task, args, cancel, progress, *, options=None):
    with account.lock, account._request_scope():
        account.require_current_day()
        if cancel.is_set():
            return "cancelled", []
        if task == "long_info":
            return "ok", query(account.client.account_state or {}, args, options=options)
        if task == "refresh_secret":
            order, scheme, targets = args
            return refresh_shop(account.client, task, cancel, order, scheme, targets,
                                prepare=account.prepare_guild_for_qq_locked, options=options)
        account.prepare_guild_for_qq_locked()
        if task == "buy_shop_equipment":
            return purchases.buy_confirmed(account.client, args)
        if task in {"refresh_secret_paid", "refresh_star_source"}:
            refresh = refresh_secret_paid if task == "refresh_secret_paid" else refresh_star_source
            return refresh(account.client, args, cancel, progress, options=options)
        if task in {"sweep_hunt", "sweep_elf", "sweep_activity"}:
            from .bot_sweep import run_sweep

            return run_sweep(account.client, args, cancel, progress)
        runner = {"activity": activity, "abyss": abyss, "buy_batch": buy}[task]
        return runner(account.client, args, cancel, progress)
