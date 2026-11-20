"""Read-only stamina quotes for previously cleared QQ sweep stages."""
from __future__ import annotations

import re

from .abyss_state import metadata
from .activity_progress import cleared, records
from .bot_views import stamina_count, stamina_detail
from .game_client import GameError
from .runtime_data import pinned
from .tasks import _ACTIVITY_TIERS, element_sweep_scenes, hunt_sweep_scenes, list_teams


STAGE_QUERY = re.compile(
    r"(?:hunt(?: (?:[火木水暗光]|Hunt(?:Fire|Earth|Ice|Dark|Light)_[0-9]{1,3}))?"
    r"|elf(?: (?:[火木水暗光]|Elf(?:Fire|Earth|Ice|Dark|Light)_[0-9]{1,3}))?)"
)
ACTIVITY_QUERY = re.compile(r"activity_sweep(?: BH[0-9]{1,5}(?:_[0-9]{1,3}_[0-9]{1,3})?)?")
ELEMENTS = dict(zip("火木水暗光", ("fire", "wood", "water", "dark", "light")))


def cost_detail(cost: int | None, stamina: int | None, *, extra_entry_cost=False) -> str:
    if cost is None:
        return "该关卡的扫荡体力未收录"
    label = f"关卡表单次参考 {cost} 体力"
    if extra_entry_cost:
        return label + " · 关卡表另列入场消耗"
    if stamina is not None and cost > 0:
        label += f" · 按体力最多 {stamina // cost} 次"
    return label


@pinned
def stage_details(client, state: dict, code: str, *, options=None) -> list[str]:
    kind, _, selector = code.partition(" ")
    stages = (element_sweep_scenes if kind == "elf" else hunt_sweep_scenes)(state)
    if selector:
        stages = [row for row in stages if row["id"] == selector or row["element"] == ELEMENTS.get(selector)]
    teams = list_teams(client)
    stamina = stamina_count(state)
    saved = records(state)
    try:
        data = metadata()
    except GameError:
        data = {}
    catalog = data.get("tables", {}).get("Scene", {})
    if options is not None:
        for row in stages:
            definition = catalog.get(row["id"]) or {}
            price = definition.get("Energy")
            if (cleared(saved.get(row["id"])) and re.fullmatch(r"[0-9]+", str(price))
                    and int(price) > 0 and not definition.get("EnterCost")):
                options.append(row["id"])
        del options[10:]
    details = [f"已解锁关卡 {len(stages)} 个，队伍 {len(teams)} 支", stamina_detail(stamina)]
    if version := data.get("version"):
        details[1] += f" · 关卡表 {version}"
    if not stages:
        return [*details, "当前账号没有匹配的已解锁关卡。"]
    for row in stages[:6]:
        scene_id = row["id"]
        record = saved[scene_id]
        stars = record.get("Stars")
        if isinstance(stars, list) and stars:
            if cleared(record):
                definition = catalog.get(scene_id) or {}
                price = definition.get("Energy")
                cost = int(price) if re.fullmatch(r"[0-9]+", str(price)) else None
                status = "已通关 · " + cost_detail(cost, stamina, extra_entry_cost=bool(definition.get("EnterCost")))
            else:
                status = "未通关"
        else:
            status = "通关状态未同步"
        details.append(f"{scene_id} · {row['name']} · {status}")
    return details


def activity_details(state: dict, code: str, *, options=None) -> list[str]:
    from .activity_progress import scenes

    selector = code.partition(" ")[2]
    stamina = stamina_count(state)
    matches = [row for row in scenes(state) if not selector or selector in {row["family"], row["id"]}]
    completed = [row for row in matches if row["cleared"]]
    if options is not None:
        options.extend(row["id"] for row in completed[-10:]
                       if row["energyKnown"] and row["energy"] > 0 and not row["extraEntryCost"]
                       and not row["serverBattle"] and not row["storyTeam"])
    balance = stamina_detail(stamina)
    if version := metadata().get("version"):
        balance += f" · 关卡表 {version}"
    if len(matches) == 1 and not completed and selector == matches[0]["id"]:
        return [balance, f"{selector} · 未通关"]
    if not completed:
        return [balance, "暂无匹配的当期已通关关卡。"]
    details = [balance, f"当期已通关关卡 {len(completed)} 个，可选关发起扫荡确认。"]
    for row in completed[-6:]:
        stage = row["stage"]
        difficulty = "普通" if 1 <= stage <= 11 else _ACTIVITY_TIERS.get(stage, "难度未知")
        cost = row["energy"] if row["energyKnown"] and row["energy"] >= 0 else None
        price = cost_detail(cost, stamina, extra_entry_cost=row["extraEntryCost"])
        details.append(f"{row['id']} · {difficulty} · {price}")
    return details
