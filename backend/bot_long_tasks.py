"""Bounded QQ operations using the existing authenticated game client."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

from .game_client import GameError, TaskDeferred
from .runtime_data import pinned


LONG_TASKS = {"activity", "abyss", "buy_batch", "refresh_secret_paid"}
SHOP_TASKS = {"refresh_secret", "refresh_star_source"}
QUERIES = {"activity", "abyss"}
MAX_SHOP_ROUNDS = 200
STAR_SOURCE_QUOTE_UNAVAILABLE = "当前缺少可核实的星源商店刷新报价，本次未提交刷新。"
SECRET_SHOP_QUOTE = Path(__file__).resolve().parents[1] / "assets" / "secret_shop_diamond_quote.json"


def secret_shop_paid_order(code):
    try:
        order = json.loads(code)
        policy = json.loads(SECRET_SHOP_QUOTE.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(order, dict) or set(order) != {"count", "quote", "budget", "scheme"}:
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
    return count, quote, limits, scheme


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
    if code.startswith("{"):
        try:
            body = json.loads(code)
        except (ValueError, RecursionError):
            return None
        if not isinstance(body, dict) or set(body) != {"order", "scheme"}:
            return None
        code, scheme = body["order"], body["scheme"]
        if not isinstance(code, str) or not isinstance(scheme, dict) or not isinstance(scheme.get("slots"), dict):
            return None
    order = parse_shop(task, code)
    return (order, scheme) if order else None


def parse(task: str, code: str):
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


def query(state, kind):
    from .activity_progress import cleared, records, scenes
    from .tasks import abyss_scenes

    if kind == "activity":
        saved = records(state)
        families = list(dict.fromkeys(row["family"] for row in scenes(state)
                                      if row["serverBattle"] and not row["cleared"]
                                      and (not row["preScene"] or cleared(saved.get(row["preScene"])))))
        teams = [str(row["ID"]) for row in state.get("Teams", {}).get("Settings", [])
                 if row.get("TeamSetting", {}).get("RolePosMap")]
        return ["活动 ID " + ("、".join(families[:12]) or "暂无"), "队伍 ID " + ("、".join(teams[:12]) or "暂无")]
    return [row["id"] for row in abyss_scenes(state)[:8]] or ["暂无可选关卡"]


def match_details(cards):
    """Keep the result card display-only. It never authorizes a purchase by position."""
    if not isinstance(cards, list):
        return []
    matches = []
    for card in cards:
        if not isinstance(card, dict) or type(position := card.get("index")) is not int or not 0 <= position < 6:
            continue
        fields = [card.get(key) for key in ("tierName", "setCN", "slotCN", "staticID")]
        label = " · ".join(value[:30] for value in fields if isinstance(value, str) and value)
        if not label:
            continue
        score = card.get("auxScore")
        suffix = f" · {score} 分" if type(score) in {int, float} and 0 <= score <= 10000 else ""
        matches.append(f"第 {position + 1} 档 · {label}{suffix}")
    return matches[:6]


def refresh_shop(client, task, cancel, order=(1, "free"), scheme=None, *, prepare=None):
    from .tasks import (
        _record_goods_sid, item_name, secret_shop_next_free, shop_reset,
    )
    count, mode = order
    if task == "refresh_star_source":
        return "quote_unavailable", [STAR_SOURCE_QUOTE_UNAVAILABLE]
    if mode != "free":
        return "quote_unavailable", ["当前游戏数据未提供可核实的钻石刷新报价。"]
    next_free = secret_shop_next_free(client.account_state or {})
    if next_free is not None and next_free > time.time() * 1000:
        return "skipped", [f"申请 {count} 次，实际刷新 0 次。免费额度仍在冷却。"]
    if prepare is not None:
        prepare()
    if cancel.is_set():
        return "cancelled", []
    result = shop_reset(client, use_gold=False, auto_buy_sids=[], equipment_scheme=scheme)
    if not result.get("ok") and str(result.get("error", "")).replace(" ", "").casefold() == "cantreset":
        return "skipped", ["游戏返回免费刷新未就绪。"]
    records = result.get("records") or []
    details = [f"申请 {count} 次，实际刷新 1 次。新货架 {len(records)} 档。", "自动购买 0 件。"]
    for index, record in enumerate(records[:6], 1):
        sid = _record_goods_sid(record)
        details.append(f"第 {index} 档 · {item_name(str(sid)) if sid else '装备'}")
    if result.get("costItems"):
        return "unexpected_cost", details
    if records and result.get("ok"):
        matches = match_details(result.get("legendaryMatches")) if scheme else []
        if matches:
            return "shop_hit", [details[0], f"命中 {len(matches)} 件 · 自动购买 0 件。", *matches]
        return "ok", details
    return "unconfirmed", ["游戏未返回可确认的秘密商店新货架。"]


def refresh_secret_paid(client, args, cancel, progress):
    from .tasks import _item_count, shop_reset

    count, quote, limits, scheme = args
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

    def receipt():
        return [f"申请 {count} 次，已确认刷新 {confirmed} 次，扣除 {confirmed * unit} 颗钻石。",
                "自动购买 0 件。"]

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
        matches = match_details(result.get("legendaryMatches")) if scheme else []
        if matches:
            return "shop_hit", [*receipt(), f"命中 {len(matches)} 件装备。", *matches]
        if round_number < target and cancel.wait(0.3):
            return "cancelled", receipt()
    if confirmed < count:
        return "partial", [*receipt(), "已达到今日剩余额度或可负担次数。"]
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
        "scene_validation": "关卡配置未通过检查。",
        "team_validation": "关卡或队伍配置未通过检查。",
        "account_sync": "账号状态需要重新同步。",
    }.get(result.get("stopReason"))
    if reason:
        return ("partial" if completed else "not_started"), [*details, reason]
    return ("partial" if completed else "skipped"), [*details, "推进已停止，请查看游戏内活动与前置关卡"]


def abyss(client, args, cancel, progress):
    from .abyss_battle import run_abyss_battle
    from .abyss_state import enabled, metadata
    from .tasks import normalize_abyss_config

    scene, waves = args
    definition = metadata()["tables"]["Scene"].get(scene) or {}
    if (not enabled(definition.get("ServerBattle")) or definition.get("EnterCost")
            or definition.get("Energy") not in (None, "", "0", 0)):
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
def run(account, task, args, cancel, progress):
    with account.lock, account._request_scope():
        account.require_current_day()
        if cancel.is_set():
            return "cancelled", []
        if task == "long_info":
            return "ok", query(account.client.account_state or {}, args)
        if task in SHOP_TASKS:
            order, scheme = args
            return refresh_shop(account.client, task, cancel, order, scheme, prepare=account.prepare_guild_for_qq_locked)
        account.prepare_guild_for_qq_locked()
        runner = {"activity": activity, "abyss": abyss, "buy_batch": buy,
                  "refresh_secret_paid": refresh_secret_paid}[task]
        return runner(account.client, args, cancel, progress)
