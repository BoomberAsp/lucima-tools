"""QQ paid sweeps (hunt/elf/activity) reusing the desktop battle settlement path.

正常扫荡：IsQuickBattle=0，按关卡表 Energy 标价逐轮核对 CostItems 实际扣费。
活动扫荡每轮重新校验活动仍在当期；任何一轮扣费与标价不符立即停止。
"""
from __future__ import annotations

import random
import re

from . import config
from .abyss_state import metadata
from .activity_progress import cleared, records, scenes
from .bot_shop_purchase import costs
from .bot_views import stamina_count
from .game_client import GameError, TaskDeferred
from .runtime_data import pinned
from .tasks import _collect_items, _fmt_items, activity_once, element_sweep_scenes, hunt_sweep_scenes, sweep_once


TASKS = {"sweep_hunt": "hunt", "sweep_elf": "elf", "sweep_activity": "activity"}
SCENE_IDS = {
    "hunt": re.compile(r"Hunt(?:Fire|Earth|Ice|Dark|Light)_[0-9]{1,3}"),
    "elf": re.compile(r"Elf(?:Fire|Earth|Ice|Dark|Light)_[0-9]{1,3}"),
    "activity": re.compile(r"BH[0-9]{1,5}_[0-9]{1,3}_[0-9]{1,3}"),
}
ORDER = re.compile(r"([A-Za-z0-9_]{1,32}) ([A-Za-z0-9_-]{1,24}) ([1-9][0-9]?) ([0-9]{1,4})")
STAMINA = "3"


def kind_of(scene_id: str):
    return next((kind for kind, pattern in SCENE_IDS.items() if pattern.fullmatch(scene_id)), None)


def parse_order(task: str, code: str):
    kind = TASKS.get(task)
    match = ORDER.fullmatch(code) if kind else None
    if not match:
        return None
    scene, team, count, budget = match.groups()
    if not SCENE_IDS[kind].fullmatch(scene):
        return None
    return scene, team, int(count), int(budget)


def sweep_price(state: dict, kind: str, scene: str):
    """Return (price, stop_message). price=None 时 stop_message 说明拒绝原因。"""
    if kind == "activity":
        row = next((row for row in scenes(state) if row["id"] == scene), None)
        if row is None:
            return None, "活动已结束或关卡不在当期，不能扫荡。"
        if not row["cleared"]:
            return None, "该活动关卡尚未通关，不能扫荡。"
        if row["serverBattle"]:
            return None, "该关卡需要服务端战斗，QQ 扫荡未开放。"
        if row["storyTeam"]:
            return None, "剧情队伍关卡请在游戏内扫荡。"
        if row["extraEntryCost"]:
            return None, "该关卡另列入场消耗，QQ 扫荡未开放。"
        if not row["energyKnown"] or row["energy"] <= 0:
            return None, "该关卡的扫荡体力标价未收录。"
        return row["energy"], ""
    unlocked = (element_sweep_scenes if kind == "elf" else hunt_sweep_scenes)(state)
    if not any(row["id"] == scene for row in unlocked):
        return None, "关卡尚未解锁，不能扫荡。"
    if not cleared(records(state).get(scene)):
        return None, "关卡尚未通关，不能扫荡。"
    definition = metadata()["tables"]["Scene"].get(scene) or {}
    if definition.get("EnterCost"):
        return None, "该关卡另列入场消耗，QQ 扫荡未开放。"
    price = definition.get("Energy")
    if not re.fullmatch(r"[0-9]+", str(price)) or int(price) <= 0:
        return None, "该关卡的扫荡体力标价未收录。"
    return int(price), ""


@pinned
def run_sweep(client, args, cancel, progress):
    scene, team, count, budget = args
    kind = kind_of(scene)
    state = client.account_state or {}
    settings = (state.get("Teams") or {}).get("Settings") or []
    if not any(str(row.get("ID")) == team and (row.get("TeamSetting") or {}).get("RolePosMap")
               for row in settings if isinstance(row, dict)):
        return "not_started", ["队伍不存在或未配置角色，请先在游戏内设置队伍。"]
    try:
        price, stop = sweep_price(state, kind, scene)
    except GameError:
        return "not_started", ["关卡表暂不可用，未能核对体力标价。"]
    if price is None:
        return "not_started", [stop]
    stamina = stamina_count(state)
    if stamina is None:
        return "not_started", ["账号快照体力未同步，请先刷新状态。"]
    if budget < price:
        return "not_started", [f"体力上限 {budget} 低于单次标价 {price}，未提交扫荡。"]
    if stamina < price:
        return "skipped", [f"当前体力 {stamina} 不足单次标价 {price}，未提交扫荡。"]

    confirmed, counter, equips = 0, {}, 0

    def receipt():
        got = _fmt_items(counter) if counter else ""
        loot = "、".join(filter(None, [got, f"装备 {equips} 件" if equips else ""]))
        return [f"{scene} · 申请 {count} 次，已确认扫荡 {confirmed} 次，"
                f"扣除 {confirmed * price} 体力（上限 {budget}）。",
                ("获得 " + loot) if loot else "掉落待核对。"]

    for round_number in range(1, count + 1):
        if cancel.is_set():
            return "cancelled", receipt()
        if confirmed * price + price > budget:
            return ("partial" if confirmed else "not_started"), [*receipt(), "下一次扫荡会超出体力上限。"]
        stamina = stamina_count(client.account_state or {})
        if stamina is None:
            return ("partial" if confirmed else "not_started"), [*receipt(), "账号快照体力未同步，已停止。"]
        if stamina < price:
            return ("partial" if confirmed else "skipped"), [*receipt(), "体力不足，已停止。"]
        if kind == "activity":
            try:
                live = any(row["id"] == scene for row in scenes(client.account_state or {}))
            except GameError:
                return ("partial" if confirmed else "not_started"), [*receipt(), "关卡表暂不可用，已停止。"]
            if not live:
                return ("partial" if confirmed else "not_started"), [*receipt(), "活动已结束，扫荡已停止。"]
        try:
            battle = sweep_once if kind in {"hunt", "elf"} else activity_once
            result = battle(client, scene, team, quick_battle=False)
        except TaskDeferred:
            return ("partial" if confirmed else "not_started"), [*receipt(), "账号状态需要重新同步。"]
        except GameError:
            return ("partial" if confirmed else "not_started"), [*receipt(), "关卡或队伍配置已变化。"]
        except Exception:
            return "unconfirmed", [*receipt(), f"第 {round_number} 次结算待核对。"]
        if not result.get("ok"):
            return ("partial" if confirmed else "skipped"), [*receipt(), "游戏未确认本次扫荡，已停止。"]
        debit = costs(result.get("costItems"))
        if debit is None:
            return "unconfirmed", [*receipt(), f"第 {round_number} 次扣费待核对。"]
        if debit != {STAMINA: price}:
            return "unexpected_cost", [*receipt(), f"第 {round_number} 次实际扣费与关卡表标价不符。"]
        confirmed += 1
        for entry in result.get("dropItems") or []:
            if isinstance(entry, dict) and entry.get("Equipment") is not None:
                equips += 1
            else:
                _collect_items([entry], counter)
        progress(receipt()[0])
        if round_number < count:
            delay = config.get_request_delay_config()
            ms = max(0.0, float(delay.get("ms") or 0) + random.uniform(-1, 1) * float(delay.get("jitter") or 0))
            if cancel.wait(ms / 1000):
                return "cancelled", receipt()
    return "ok", receipt()
