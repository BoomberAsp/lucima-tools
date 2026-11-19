"""Settlement and shelf checks for the QQ shop flows."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .game_client import GameMessage, TaskDeferred

STAR_QUOTE = Path(__file__).resolve().parents[1] / "assets" / "star_source_shop_quote.json"
CURRENCY = re.compile(r"[A-Za-z0-9_]{1,32}\Z")


def price(value):
    if (isinstance(value, dict) and set(value) == {"currency", "amount"}
            and isinstance(value["currency"], str) and CURRENCY.fullmatch(value["currency"])
            and type(value["amount"]) is int and 0 < value["amount"] <= 10**9):
        return value
    return None


def is_free(value):
    return isinstance(value, dict) and type(value.get("amount")) is int and value == {"currency": "", "amount": 0}


def star_quote():
    try:
        quote = json.loads(STAR_QUOTE.read_bytes())
    except (ValueError, OSError):
        return None
    if (isinstance(quote, dict) and set(quote) <= {"gameVersion", "catalog", "refresh", "equipment"}
            and all(isinstance(quote.get(key), str) and quote[key] for key in ("gameVersion", "catalog"))
            and (is_free(quote.get("refresh")) or price(quote.get("refresh")))
            and ("equipment" not in quote or price(quote["equipment"]))):
        return quote
    return None


def parse_targets(keys):
    from .tasks import BUY_TARGETS

    labels = {target["key"]: target["ids"] for target in BUY_TARGETS}
    if (not isinstance(keys, list) or len(keys) > len(labels)
            or any(not isinstance(key, str) or key not in labels for key in keys)
            or len(set(keys)) != len(keys)):
        return None
    return [sid for key in keys for sid in labels[key]]


def costs(items):
    if not isinstance(items, list):
        return None
    totals = {}
    for row in items:
        item = row.get("Item") if isinstance(row, dict) else None
        if not isinstance(item, dict):
            return None
        sid, amount = item.get("StaticID"), item.get("Count")
        if (not isinstance(sid, str) or not CURRENCY.fullmatch(sid)
                or type(amount) is not int or amount <= 0):
            return None
        totals[sid] = totals.get(sid, 0) + amount
    return totals


def equipment_price(record):
    if record.get("CostItems") in (None, []):
        return {"currency": "1", "amount": None}
    debit = costs(record.get("CostItems"))
    if debit and set(debit) == {"1"}:
        return price({"currency": "1", "amount": debit["1"]})
    return None


@dataclass
class Purchases:
    count: int = 0
    rejected: int = 0
    debits: dict = field(default_factory=dict)

    def buy(self, client, records, wanted, cancel):
        from .tasks import _buy_payload, _record_equipment, _record_goods_sid

        for record in records:
            sid = str(_record_goods_sid(record))
            if record.get("BuyCount") or _record_equipment(record) is not None or sid not in wanted:
                continue
            if cancel.is_set():
                return "cancelled"
            try:
                result = client.call("StoreHandler.BuyCommodity", _buy_payload(client, record))
            except GameMessage:
                self.rejected += 1
                continue
            except TaskDeferred:
                return "partial"
            except Exception:
                return "unconfirmed"
            items = (result.get("Drop") or {}).get("Items")
            amounts = costs(result.get("CostItems"))
            if not isinstance(items, list) or not any(
                    isinstance(row, dict) and isinstance(row.get("Item"), dict)
                    and str(row["Item"].get("StaticID")) == sid
                    and type(row["Item"].get("Count")) is int and row["Item"]["Count"] > 0 for row in items):
                return "unconfirmed"
            self.count += 1
            if amounts is None:
                return "unconfirmed"
            for sid, amount in amounts.items():
                self.debits[sid] = self.debits.get(sid, 0) + amount
            if set(amounts) - {"1"}:
                return "unexpected_cost"
        return None

    def summary(self):
        from .tasks import item_name

        text = f"自动购买 {self.count} 个货架档位。"
        if self.debits:
            text += "已确认花费 " + "、".join(f"{item_name(sid)} ×{amount}" for sid, amount in self.debits.items()) + "。"
        if self.rejected:
            text += f"游戏拒绝 {self.rejected} 档。"
        return text


def shelf_key(record, equipment, revision):
    from .tasks import _oid

    if not isinstance(equipment, dict) or not isinstance(equipment.get("StaticID"), str):
        return None
    if not revision and not _oid(equipment.get("_id")):
        return None
    fields = ("_id", "StaticID", "ClassLV", "Set", "MainProp", "SubProps", "Enhance", "EnhanceLevel")
    data = [record, {key: equipment.get(key) for key in fields}, revision]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def secret_option(record, slot):
    from .tasks import _ms, _oid, _record_equipment

    record_id = _oid(record.get("_id"))
    if not isinstance(record_id, str) or not record_id or record.get("BuyCount"):
        return None
    key = shelf_key(record_id, _record_equipment(record), _ms(record.get("LastResetTime")))
    quote = equipment_price(record)
    return {"slot": slot, "key": key, "price": quote} if key and quote else None


def star_option(equipment, slot, source, quote):
    count, revert = source.get("refreshCount"), source.get("revertTime")
    if type(count) is not int or count < 0:
        return None
    key = shelf_key(slot, equipment, [count, revert])
    return {"slot": slot, "key": key, "price": quote} if key and quote else None


def confirmed_order(code):
    try:
        order = json.loads(code)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(order, dict) or set(order) != {"shop", "slot", "key", "price", "expires"}
            or order["shop"] not in ("secret", "star") or type(order["slot"]) is not int
            or not 1 <= order["slot"] <= (6 if order["shop"] == "secret" else 3)
            or not isinstance(order["key"], str) or not re.fullmatch(r"[0-9a-f]{64}", order["key"])
            or type(order["expires"]) is not int):
        return None
    quote = order["price"]
    if order["shop"] == "secret":
        if quote == {"currency": "1", "amount": None} or price(quote) and quote["currency"] == "1":
            return order
    elif price(quote) and quote == {"currency": "109", "amount": 5}:
        return order
    return None


def buy_confirmed(client, order):
    from .tasks import (
        _buy_payload, _item_count, _record_equipment, _secret_shop_records, item_name, star_source_buy, star_source_query,
    )

    slot = order["slot"]
    expected = {field: order[field] for field in ("slot", "key", "price")}
    quote = order["price"]
    if order["expires"] <= time.time():
        return "skipped", ["购买确认已超时。"]
    if order["shop"] == "secret":
        matches = [row for row in _secret_shop_records(client.account_state or {})
                   if secret_option(row, slot) == expected]
        if len(matches) != 1:
            return "skipped", ["当前货架、装备或价格已变化。"]
        equipment = _record_equipment(matches[0])
        purchase = lambda: client.call("StoreHandler.BuyCommodity", _buy_payload(client, matches[0]))
    else:
        policy = star_quote()
        if (not policy or (client.login_version, client.login_catalog) != (policy["gameVersion"], policy["catalog"])):
            return "skipped", ["星源商店报价已失效。"]
        source = star_source_query(client, include_shelf=True)
        shelf = source.get("shelf", [])
        if not 0 < slot <= len(shelf) or star_option(shelf[slot - 1], slot, source, policy.get("equipment")) != expected:
            return "skipped", ["当前货架、装备或价格已变化。"]
        equipment = shelf[slot - 1]
        purchase = lambda: star_source_buy(client, slot - 1, include_settlement=True)
    if (order["expires"] <= time.time()
            or quote["amount"] is not None
            and _item_count(client.account_state or {}, quote["currency"]) < quote["amount"]):
        return "skipped", ["确认已超时或余额不足。"]
    try:
        result = purchase()
    except GameMessage:
        return "skipped", ["游戏拒绝购买，请检查货架和余额。"]
    except TaskDeferred:
        return "not_started", ["账号状态需要同步。"]
    except Exception:
        return "unconfirmed", ["购买结果待核对，请先在游戏内查看。"]
    if result.get("ok") is False:
        return "skipped", ["游戏拒绝购买，请检查货架和余额。"]
    debit = costs(result.get("CostItems"))
    currency = quote["currency"]
    if not debit or set(debit) != {currency} or (quote["amount"] is not None and debit[currency] != quote["amount"]):
        return "unconfirmed", ["无法核对装备购买扣款，请先在游戏内查看。"]
    items = (result.get("Drop") or {}).get("Items") or []
    if not any(isinstance(row, dict) and isinstance(row.get("Equipment"), dict)
               and row["Equipment"].get("StaticID") == equipment["StaticID"] for row in items):
        return "unconfirmed", ["已返回扣款，装备是否到账待核对。"]
    return "ok", [f"已购买第 {slot} 档装备，扣除 {debit[currency]} {item_name(currency)}。"]
