"""Allowed account snapshot fields for the Lucima QQ navigation pages."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from .runtime_data import pinned


VIEWS = {"summary", "profile", "profile_full", "resources", "heroes", "items", "teams", "guild", "weekly", "guild_shop",
         "abyss_sweep", "arena", "vote", "equip_event", "guild_aid", "guild_captain", "star_source"}
VIEW_RE = re.compile(r"([a-z_]+)(?: ([1-9][0-9]{0,2})(?: ([^\x00-\x1f]{1,32}))?)?")
GUILD_ORDER = re.compile(r"([A-Za-z0-9_]{1,32}) ([1-9]|10) (29|30) ([1-9][0-9]{0,6})")
ILLUSION_SWEEP_ORDER = re.compile(r"([1-9][0-9]{0,2})(?: ([1-9][0-9]{0,3}))?")
PAGE_SIZE = 5


def parse_view(code: str):
    match = VIEW_RE.fullmatch(code)
    if not match:
        return None
    name, page, query = match.groups()
    if name not in VIEWS or page and name not in {"heroes", "items", "teams"}:
        return None
    return name, int(page or 1), (query or "").strip().casefold()


def stamina_count(state: dict) -> int | None:
    from .appcore import CURRENCY_IDS

    items = (state.get("ItemContainer") or {}).get("Items") or []
    for item in items:
        if isinstance(item, dict) and item.get("StaticID") == CURRENCY_IDS["stamina"]:
            count = item.get("Count")
            if type(count) is int and count >= 0:
                return count
    return None


def stamina_detail(count: int | None) -> str:
    return "账号快照体力未同步" if count is None else f"账号快照体力 {count}"


def profile_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(" " if unicodedata.category(char).startswith("C") else char
                            for char in value[:limit * 2]).split())[:limit]


@pinned
def account_profile(state: dict, *, full: bool) -> dict | None:
    from .appcore import CURRENCY_IDS, _roster, summarize

    def count(value):
        return value if type(value) is int and value >= 0 else None

    def avatar(value):
        return value.removeprefix("/assets/avatars/") if isinstance(value, str) else ""

    info = state.get("Info")
    if not isinstance(info, dict) or not info:
        return None
    roles = (state.get("RoleDataContainer") or {}).get("Roles")
    items = (state.get("ItemContainer") or {}).get("Items")
    snapshot = {**state, "RoleDataContainer": {"Roles": roles if isinstance(roles, list) else []},
                "ItemContainer": {"Items": items if isinstance(items, list) else []}}
    summary = summarize(snapshot)
    roster = _roster(snapshot)
    item_ids = {str(item.get("StaticID")) for item in snapshot["ItemContainer"]["Items"]}
    profile = {"name": profile_text(summary["name"], 40), "level": count(summary["level"]),
               "avatar": avatar(summary["avatar"]), "sampled_at": int(time.time()),
               "heroes_total": len(roster["heroes"]) if isinstance(roles, list) else None,
               "items_total": len(roster["items"]) if isinstance(items, list) else None}
    for key in ("stamina", "flag"):
        if CURRENCY_IDS[key] in item_ids:
            profile[key] = {field: count(summary[key][field]) for field in ("current", "max")}
    if full:
        uid = summary["cuid"]
        if type(uid) in (int, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(uid)):
            profile["uid"] = str(uid)
        if isinstance(items, list):
            profile["currencies"] = {key: count(value) for key, value in summary["currencies"].items()}
        profile["heroes"] = [
            {"name": profile_text(row["name"], 28), "level": count(row["level"]),
             "grade": row["grade"], "avatar": avatar(row["avatar"])}
            for row in roster["heroes"][:5]
        ]
    return profile


@pinned
def view(account, args, *, illusion_quote=None):
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

        account.prepare_guild_for_qq_locked()
        result = customized_equip_query(client)
        if not result.get("ok"):
            return ["游戏暂未提供当前装备活动状态。"]
        return [f"当前 {result['setCN'] or '未选套装'} · {result['slotCN'] or '未选部位'}",
                "有待选副属性。" if result.get("hasCandidate") else "当前没有待选副属性。"]
    if name == "star_source":
        from .tasks import star_source_query

        account.prepare_guild_for_qq_locked()
        result = star_source_query(client)
        count = result.get("refreshCount")
        count_text = str(count) if result.get("ok") and type(count) is int and count >= 0 else "未能同步"
        return [f"星源商店刷新计数 {count_text}。"]
    if name == "abyss_sweep":
        from .tasks import ABYSS_MIN_FLOOR, abyss_progress

        progress = abyss_progress(state)
        floor, count = progress["floor"], progress["sync"]
        details = [f"虚拟幻境最高通关 {floor} 层，当前意识同步器 {count} 个。"]
        if floor < ABYSS_MIN_FLOOR:
            details.append(f"QQ 扫荡需先通关第 {ABYSS_MIN_FLOOR} 层。")
        elif count <= 0:
            details.append("当前没有可消耗的意识同步器。")
        else:
            details.append("快速扫荡使用全部意识同步器，体力消耗 0。")
            code = f"{count} {floor}"
            if illusion_quote is not None and ILLUSION_SWEEP_ORDER.fullmatch(code):
                illusion_quote.update(floor=floor, count=count)
        return details
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
        account.prepare_guild_for_qq_locked()
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
        rows = [(row["id"], row["name"], f"ID {row['id']} · 等级 {row['level']}" if name == "heroes" else f"× {row['count']}")
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
    from .tasks import _claim_tesseract, _collect_items, _item_count, _oid

    client = account.client
    state = client.account_state or {}
    if task == "tesseract":
        account.prepare_guild_for_qq_locked()
        result = _claim_tesseract(client)
        return ("skipped" if result.get("skipped") else "ok" if result.get("ok") else "unconfirmed"), []
    if task == "sweep_illusion":
        from .bot_shop_purchase import costs
        from .tasks import SYNC_ITEM_SID, abyss_progress, task_abyss_sweep

        cap, floor = args
        account.prepare_guild_for_qq_locked()
        progress = abyss_progress(client.account_state or {})
        if floor is not None and (progress["floor"] != floor or progress["sync"] != cap):
            return "skipped", ["最高通关楼层或意识同步器数量已变化，请重新发送 /lucima 幻境扫荡。"]
        if progress["sync"] > cap:
            return "skipped", [f"持有 {progress['sync']} 个意识同步器，超过确认上限 {cap}。"]
        result = task_abyss_sweep(client)
        if not result.get("ok"):
            return "unconfirmed", ["游戏未确认本次扫荡，请先刷新幻境状态。"]
        if result.get("skipped"):
            return "skipped", result.get("lines", [])
        response = result.get("raw") or {}
        spent = costs(response.get("CostItems"))
        if spent and (set(spent) != {SYNC_ITEM_SID} or spent[SYNC_ITEM_SID] > cap):
            return "unexpected_cost", ["游戏回执的扣费超出确认范围。"]
        if spent != {SYNC_ITEM_SID: progress["sync"]}:
            return "unconfirmed", ["未能核对意识同步器扣款，请先刷新幻境状态。"]
        return "ok", [*result.get("lines", []), "本次体力消耗 0。"]

    from .guild import GuildService, has_guild
    from .guild_store import GuildStoreService

    identifier, count, currency, unit = args
    quantity = int(count)
    if not has_guild(state):
        return "skipped", ["当前账号未加入佣兵团。"]
    account.prepare_guild_for_qq_locked()
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
