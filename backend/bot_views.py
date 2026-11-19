"""Allowed account snapshot fields for the Lucima QQ navigation pages."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from .runtime_data import pinned


VIEWS = {"summary", "resources", "heroes", "items", "teams", "guild", "weekly", "guild_shop",
         "abyss_sweep", "arena", "vote", "equip_event", "guild_aid", "guild_captain"}
VIEW_RE = re.compile(r"([a-z_]+)(?: ([1-9][0-9]{0,2})(?: ([^\x00-\x1f]{1,32}))?)?")
GUILD_ORDER = re.compile(r"([A-Za-z0-9_]{1,32}) ([1-9]|10) (29|30) ([1-9][0-9]{0,6})")
PAGE_SIZE = 5


def parse_view(code: str):
    match = VIEW_RE.fullmatch(code)
    if not match:
        return None
    name, page, query = match.groups()
    if name not in VIEWS or page and name not in {"heroes", "items", "teams"}:
        return None
    return name, int(page or 1), (query or "").strip().casefold()


@pinned
def view(account, args):
    from .appcore import _roster, summarize
    from .guild import GuildService, has_guild
    from .guild_quests import GuildWeeklyService
    from .tasks import _oid, item_name, list_teams

    name, page, query = args
    client = account.client
    state = client.account_state or {}
    if name == "arena":
        from .tasks import arena_next_times, arena_npcs

        available = arena_next_times(state)
        now = int(time.time() * 1000)
        npcs = arena_npcs()
        ready = sum((available.get(npc["sceneId"]) or now) <= now for npc in npcs)
        return [f"竞技场 NPC {len(npcs)} 名，快照中可挑战 {ready} 名。", *[npc["name"] for npc in npcs[:6]]]
    if name == "vote":
        from .voting import status

        snapshot = status(state)
        return [f"{row['name']} · 选票 {row['tickets']}" for row in snapshot["activities"][:6]] or [snapshot["note"]]
    if name == "equip_event":
        from .tasks import customized_equip_query

        result = customized_equip_query(client)
        if not result.get("ok"):
            return ["游戏暂未提供当前装备活动状态。"]
        return [f"当前 {result['setCN'] or '未选套装'} · {result['slotCN'] or '未选部位'}",
                "有待选副属性。" if result.get("hasCandidate") else "当前没有待选副属性。"]
    if name == "abyss_sweep":
        from .tasks import abyss_progress

        progress = abyss_progress(state)
        return [f"虚拟幻境最高通关 {progress['floor']} 层，当前意识同步器 {progress['sync']} 个。"]
    if name in {"summary", "resources"}:
        status = summarize(state)
        level = status.get("level")
        if name == "summary":
            now = datetime.now(timezone(timedelta(hours=8)))
            return [f"角色等级 {level if type(level) is int else '未知'} · 游戏会话已建立",
                    f"快照时间 {now:%m-%d %H:%M:%S}（北京时间）"]
        currencies = status["currencies"]
        return [f"体力 {status['stamina']['current']} · 旗帜 {status['flag']['current']}",
                f"金币 {currencies['gold']} · 钻石 {currencies['diamond']} · 能源电池 {currencies['battery']}",
                f"招募契约 {currencies['recruit']} · 神秘契约 {currencies['mystery']} · 银河契约 {currencies['galaxy']}"]
    if name in {"guild", "weekly", "guild_shop", "guild_aid", "guild_captain"}:
        if not has_guild(state):
            return ["当前账号未加入佣兵团。"]
        account.refresh_guild_locked(log_success=False, checkin=False)
        guild = GuildService(client, account.guild_runtime)
        if name == "guild_aid":
            from .guild_aid import GuildAidService

            snapshot = GuildAidService(guild).snapshot()
            request = ("今日已发布求助" if snapshot["requested"] else "今日未发布求助") if snapshot["requestKnown"] else "求助状态待同步"
            reward = snapshot["reward"] if snapshot["reward"] is not None else "未同步"
            return [request, f"援助奖励 {reward}/{snapshot['limit']}"]
        if name == "guild_captain":
            from .guild_captain_store import GuildCaptainStoreService

            snapshot = GuildCaptainStoreService(guild).snapshot()
            if not snapshot["access"]["allowed"]:
                return ["当前佣兵团身份没有团长商店权限。"]
            return [f"{row['name']} · 货币 {row['currency']} · 单价 {row['price']}" for row in snapshot["goods"][:6]]
        if name == "guild_shop":
            from .guild_store import GuildStoreService

            visible = [row for row in GuildStoreService(guild).snapshot()["goods"] if row["available"] and not row["reset"]]
            return [f"团员商店开放 {len(visible)} 项。", *[
                f"{row['id']} {row['name']} · 货币 {row['currency']} · 单价 {row['price']} · 可买 {row['buyable']}"
                for row in visible[:6]
            ]]
        if name == "weekly":
            snapshot = GuildWeeklyService(guild).snapshot()
            ready = sum(row["state"] == "ready" for row in snapshot["personal"])
            return [f"可领个人周任务 {ready} 项。", f"全团积分 {snapshot.get('score', '未同步')}"]
        snapshot = guild.snapshot()
        checkin = "已签到" if snapshot["checkedIn"] else "未签到"
        return [f"佣兵团等级 {snapshot['level']} · 成员 {snapshot['memberCount']}", f"今日{checkin}"]

    if name == "teams":
        role_names = {str(_oid(row.get("_id"))): item_name(str(row.get("StaticID") or ""))
                      for row in (state.get("RoleDataContainer") or {}).get("Roles", [])}
        settings = {str(row.get("ID")): (row.get("TeamSetting") or {}).get("RolePosMap") or {}
                    for row in (state.get("Teams") or {}).get("Settings", [])}
        rows = []
        for team in list_teams(client):
            positions = sorted((pos, role_names.get(str(role), "未知")) for role, pos in settings.get(team["id"], {}).items()
                               if type(pos) is int and 0 <= pos <= 4)
            roles = "、".join(f"{pos + 1}号位 {label}" for pos, label in positions[:4])
            rows.append((team["id"], team["name"], f"ID {team['id']}  {roles}".strip()))
    else:
        rows = [(row["id"], row["name"], f"等级 {row['level']}" if name == "heroes" else f"× {row['count']}")
                for row in _roster(state)[name]]
    matches = [row for row in rows if not query or any(query in str(value).casefold() for value in row[:2])]
    pages = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        return [f"匹配 {len(matches)} 项，共 {pages} 页。请返回上一页。"]
    start = (page - 1) * PAGE_SIZE
    return [f"第 {page}/{pages} 页 · 共 {len(matches)} 项", *[
        f"{label} · {detail}" for _, label, detail in matches[start:start + PAGE_SIZE]
    ]]


@pinned
def filter_preview(state, scheme):
    from .tasks import UnknownEquipmentError, equipment_matches_filter_scheme, parse_equipment

    matches, unavailable = [], 0
    for equipment in (state.get("EquipmentContainer") or {}).get("Datas") or []:
        if not isinstance(equipment, dict):
            continue
        try:
            if equipment_matches_filter_scheme(equipment, scheme):
                matches.append(parse_equipment(equipment))
        except UnknownEquipmentError:
            unavailable += 1
    matches.sort(key=lambda card: card.get("auxScore") or 0, reverse=True)
    details = [f"方案命中 {len(matches)} 件装备，无法识别 {unavailable} 件。"]
    for card in matches[:6]:
        details.append(f"{card['setCN']} · {card['slotCN']} · {card.get('auxScore', 0)} 分")
    return details


def run_account_action(account, task, args):
    from .game_client import GameError
    from .tasks import _claim_tesseract, _collect_items, _item_count, _oid, abyss_progress, task_abyss_sweep

    client = account.client
    state = client.account_state or {}
    if task == "tesseract":
        result = _claim_tesseract(client)
        return ("skipped" if result.get("skipped") else "ok" if result.get("ok") else "unconfirmed"), []
    if task == "sweep_illusion":
        progress = abyss_progress(state)
        if progress["sync"] > args:
            return "skipped", [f"持有 {progress['sync']} 个意识同步器，超过确认上限 {args}。"]
        result = task_abyss_sweep(client)
        if not result.get("ok"):
            return "unconfirmed", []
        if result.get("skipped"):
            return "skipped", result.get("lines", [])
        costs = {}
        response = result.get("raw") or {}
        _collect_items(response.get("CostItems"), costs)
        if any(str(key) != "39" for key in costs) or sum(costs.values()) > args:
            return "unexpected_cost", []
        if costs.get("39") != progress["sync"]:
            return "unconfirmed", ["未能核对意识同步器扣款。"]
        return "ok", result.get("lines", [])

    from .guild import GuildService, has_guild
    from .guild_store import GuildStoreService

    identifier, count, currency, unit = args
    quantity = int(count)
    if not has_guild(state):
        return "skipped", ["当前账号未加入佣兵团。"]
    account.refresh_guild_locked(log_success=False, checkin=False)
    store = GuildStoreService(GuildService(client, account.guild_runtime))
    row = next((row for row in store.snapshot()["goods"] if row["id"] == identifier), None)
    if (not row or row["reset"] or row["currency"] != currency or row["price"] != int(unit)
            or row["buyable"] < quantity or row["blocked"]):
        return "skipped", ["当前团员商店报价或购买条件与确认参数不符。"]
    records = ((client.account_state or {}).get("StoreRecordContainer") or {}).get("Records") or []
    matches = [r for r in records if isinstance(r, dict) and r.get("Store") == "MemberShop" and r.get("StaticID") == identifier]
    if len(matches) != 1 or not _oid(matches[0].get("_id")):
        return "skipped", ["当前账号没有唯一的商品购买记录。"]
    before = _item_count(client.account_state, currency)
    try:
        purchased = store.buy(identifier, quantity)
    except GameError:
        return "unconfirmed", ["未能核对团员商店结算，请先查看游戏内状态。"]
    maximum = int(unit) * quantity
    spent = before - _item_count(client.account_state, currency)
    if spent > maximum:
        return "unexpected_cost", [f"实际扣款 {spent} 超过确认预算 {maximum}。"]
    if spent != maximum or purchased != quantity:
        return "unconfirmed", ["游戏已返回购买记录，扣款或数量未与确认报价完全一致。"]
    return "ok", [f"已确认购买 {row['name']} × {purchased}，扣除货币 {currency} × {spent}。"]
