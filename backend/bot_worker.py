"""One-shot Lucima worker for the QQBotAgent bridge. JSON over private pipes."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time

import httpx


TASKS = {
    "signin": ("month_signin",),
    "mail": ("claim_mail",),
    "base": ("reactor", "sf_potion", "sf_starforce"),
    "redeem": ("redeem_code",),
    "dispatch": ("dispatch",),
    "achievement": ("daily_achievement",),
    "friend_support": ("friend_support",),
    "send_meal": ("send_meal",),
    "reactor": ("reactor",),
    "potion": ("sf_potion",),
    "starforce": ("sf_starforce",),
}
PHASE_TWO = {"guild", "guild_weekly", "donate", "recruit", "stages", "equipment"}
DONATION_CAPS = {"gold": 5000, "courage": 1}


def phase_two(account, task: str, arg: str) -> tuple[str, list[str]]:
    """Return only bounded, selected receipt fields from the authenticated snapshot."""
    from .guild import GUILD_DONATIONS, GuildService, has_guild
    from .guild_quests import GuildWeeklyService
    from .tasks import (
        _summon_record, element_sweep_scenes, hunt_sweep_scenes, list_teams, parse_equipment,
        task_free_summon,
    )

    client = account.client
    state = client.account_state or {}
    with account.lock, account._request_scope():
        account.require_current_day()
        if task == "stages":
            scenes = (element_sweep_scenes if arg == "elf" else hunt_sweep_scenes)(state)
            teams = list_teams(client)
            return "ok", [f"已解锁关卡 {len(scenes)} 个，队伍 {len(teams)} 支",
                          *[row["id"] for row in scenes[:6]]]
        if task == "equipment":
            query = arg.casefold()
            matches = []
            for item in (state.get("EquipmentContainer") or {}).get("Datas") or []:
                if not isinstance(item, dict):
                    continue
                row = parse_equipment(item)
                if not query or query in str(row.get("setCN") or "").casefold() or query == str(row.get("set") or "").casefold():
                    matches.append(row)
            matches.sort(key=lambda row: row.get("auxScore") or 0, reverse=True)
            detail = [f"匹配 {len(matches)} 件，按副属性分数排列"]
            for row in matches[:6]:
                detail.append(f"{row['name']} · {row['setCN']} · +{row['enhance']} · {row['auxScore']} 分")
            return "ok", detail
        if task == "recruit":
            record = _summon_record(state)
            used = (record or {}).get("FreeBuyCount")
            if type(used) is not int or used < 0:
                return "deferred", ["游戏快照未明确返回免费次数，已停止"]
            if used:
                return "skipped", ["本日免费次数已使用"]
            if has_guild(state):
                account.refresh_guild_locked(log_success=False, checkin=False)
            result = task_free_summon(client)
            response = result.get("raw")
            if isinstance(response, dict) and response.get("CostItems"):
                return "unexpected_cost", ["游戏返回了消耗记录，请核对账户余额"]
            confirmed = (response.get("CommodityRecord") or {}) if isinstance(response, dict) else {}
            if result.get("ok") and confirmed.get("FreeBuyCount") == used + 1:
                return "ok", ["游戏已确认免费招募"]
            return "unconfirmed", []
        if not has_guild(state):
            return "skipped", ["当前账号未加入佣兵团"]
        account.refresh_guild_locked(log_success=False, checkin=task == "guild")
        guild = GuildService(client, account.guild_runtime)
        if task == "guild":
            return ("ok", ["今日佣兵团签到已确认"]) if guild.snapshot()["checkedIn"] else ("unconfirmed", [])
        if task == "guild_weekly":
            weekly = GuildWeeklyService(guild)
            ready = [row["id"] for row in weekly.snapshot()["personal"] if row["state"] == "ready"]
            personal = sum(weekly.claim_personal(quest) for quest in ready[:8])
            tiers = weekly.claim_tiers()
            status = "ok" if personal or tiers else "skipped"
            return status, [f"确认领取个人周任务 {personal} 项，全团积分奖励 {tiers} 档"]
        rule = GUILD_DONATIONS[arg]
        if rule["cost"] != DONATION_CAPS[arg]:
            return "deferred", ["单次捐赠价格已变化，已停止"]
        donated = guild.donate_if_due(arg, max_count=1)
        if not donated:
            return "skipped", ["今日额度、签到状态或资源余额暂不满足"]
        return "ok", [f"已确认捐赠 {rule['name']} {rule['cost']}，1 次"]


def exit_with_bridge(parent_pid: int) -> None:
    # A bridge crash must not leave an orphan gameload running alongside its replacement.
    import ctypes

    pr_set_pdeathsig = 1
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        raise RuntimeError("Lucima bridge parent unavailable")


def dispatch(request: dict, *, cancel=None, progress=None) -> dict:
    from . import accounts, appcore
    from . import bot_long_tasks, bot_views
    from .game_client import DuplicateLoginError, GameMessage, MaintenanceError, TaskDeferred, is_duplicate_login_message
    from .guild import has_guild
    from .maintenance import service as maintenance
    from .portal import PortalError, access_token_needs_refresh, portal_login_v2, portal_refresh_v2

    action = request.get("action")
    if action == "login":
        try:
            auth = portal_login_v2(request["account"], request["password"], request["device_id"])
        except PortalError:
            return {"status": "login_failed"}
        return {"status": "ok", "auth": auth}
    if action == "resume":
        saved = request["auth"]
        try:
            auth = portal_refresh_v2(saved)
        except PortalError:
            return {"status": "login_failed"}
        if auth.get("user_id") != saved.get("user_id"):
            return {"status": "login_failed"}
        return {"status": "ok", "auth": auth}

    task = request.get("task")
    code = request.get("code", "")
    if not isinstance(task, str):
        return {"status": "invalid"}
    account_actions = {"tesseract", "sweep_illusion", "member_purchase"}
    extended_tasks = (bot_long_tasks.LONG_TASKS | bot_long_tasks.SHOP_TASKS | account_actions
                      | {"long_info", "account_view", "filter_preview"})
    if (action != "run" or task not in (TASKS.keys() | PHASE_TWO | extended_tasks)
            or not isinstance(code, str) or len(code) > (2300 if task in bot_long_tasks.SHOP_TASKS | {"filter_preview"} else 255)
            or (task == "redeem" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code))
            or (task == "donate" and code not in DONATION_CAPS)
            or (task == "stages" and code not in {"hunt", "elf"})
            or (task == "equipment" and len(code) > 24)
            or (task in TASKS.keys() - {"redeem"} and code)):
        return {"status": "invalid"}
    if task == "long_info" and code == "shop":
        return {"status": "ok", "details": bot_long_tasks.shop_reference()}
    args = None
    if task == "tesseract" and code:
        return {"status": "invalid"}
    if task == "member_purchase":
        match = bot_views.GUILD_ORDER.fullmatch(code)
        if not match:
            return {"status": "invalid"}
        args = match.groups()
    elif task == "sweep_illusion":
        if not re.fullmatch(r"[1-9][0-9]{0,2}", code):
            return {"status": "invalid"}
        args = int(code)
    elif task == "filter_preview":
        try:
            args = json.loads(code)
        except (ValueError, RecursionError):
            return {"status": "invalid"}
        if not isinstance(args, dict) or not isinstance(args.get("slots"), dict):
            return {"status": "invalid"}
    elif task == "account_view":
        args = bot_views.parse_view(code)
        if args is None:
            return {"status": "invalid"}
    elif task in bot_long_tasks.SHOP_TASKS:
        args = bot_long_tasks.parse_shop_request(task, code)
        if args is None:
            return {"status": "invalid"}
    elif task in bot_long_tasks.LONG_TASKS or task == "long_info":
        args = code if task == "long_info" and code in bot_long_tasks.QUERIES else bot_long_tasks.parse(task, code)
        if args is None:
            return {"status": "invalid"}
    auth = request["auth"]
    account_id = auth["user_id"]

    def refresh(force: bool = False) -> dict:
        if force or access_token_needs_refresh(auth["access_token"]):
            updated = portal_refresh_v2(auth)
            if updated.get("user_id") != account_id:
                raise PortalError("账号身份已变化")
            auth.update(updated)
        return auth

    bootstrapped = False
    try:
        with maintenance.operation_scope():
            if cancel is not None and cancel.is_set():
                return {"status": "cancelled"}
            refresh()
            account = appcore._bootstrap_and_register(account_id, auth, auth_refresher=refresh)
            bootstrapped = True
            if task in account_actions:
                with account.lock, account._request_scope():
                    account.require_current_day()
                    status, details = bot_views.run_account_action(account, task, args)
                return {"status": status, "auth": auth, "details": details}
            if task == "account_view":
                with account.lock, account._request_scope():
                    account.require_current_day()
                    return {"status": "ok", "auth": auth, "details": bot_views.view(account, args)}
            if task == "filter_preview":
                with account.lock, account._request_scope():
                    account.require_current_day()
                    details = bot_views.filter_preview(account.client.account_state or {}, args)
                return {"status": "ok", "auth": auth, "details": details}
            if task in extended_tasks:
                status, details = bot_long_tasks.run(account, task, args, cancel or threading.Event(),
                                                      progress or (lambda message: None))
                if (status in {"ok", "skipped", "cancelled"}
                        and account.client.connection_status().get("reason") == "duplicate_login"):
                    status = "duplicate_login"
                return {"status": status, "auth": auth, "details": details}
            if task in PHASE_TWO:
                status, details = phase_two(account, task, code)
                if account.client.connection_status().get("reason") == "duplicate_login":
                    status = "duplicate_login"
                return {"status": status, "auth": auth, "details": details}
            if task in {"mail", "dispatch"}:
                if task == "dispatch" and has_guild(account.client.account_state):
                    with account.lock:
                        account.refresh_guild_locked(log_success=False, checkin=False)
                results = appcore.run(account_id, list(TASKS[task]), {"dispatch": {"claimOnly": True}})["results"]
            else:
                toggles = TASKS[task]
                if task in {"base", "redeem", "achievement"} and has_guild(account.client.account_state):
                    with account.lock:
                        account.refresh_guild_locked(log_success=False, checkin=False)
                account.toggles.update(dict.fromkeys(toggles, True))
                results = account.run_toggles(
                    list(toggles), "QQ·", log_to_buffer=False,
                    single_redeem_code=code if task == "redeem" else None,
                )
        connection_reason = account.client.connection_status().get("reason")
        if connection_reason == "duplicate_login":
            return {"status": "duplicate_login", "auth": auth}
        if (connection_reason == "maintenance" or len(results) != len(TASKS[task])
                or any(item.get("deferred") for item in results)):
            return {"status": "deferred", "auth": auth}
        # Upstream error messages and snapshots are never returned to the bot.
        status = "unconfirmed"
        if task == "redeem" and results[0].get("codeStatus") in {"expired", "invalid"}:
            status = "invalid_code"
        elif all(item.get("ok") for item in results):
            status = "skipped" if all(item.get("skipped") for item in results) else "ok"
        details = [f"已领取 {results[0]['count']} 项派遣奖励"] if task == "dispatch" and results[0].get("count") else []
        return {"status": status, "auth": auth, "details": details}
    except (PortalError, appcore.ApiError) as error:
        if is_duplicate_login_message(str(error)):
            return {"status": "duplicate_login", "auth": auth}
        if isinstance(error.__context__, MaintenanceError):
            return {"status": "deferred", "auth": auth}
        if isinstance(error, PortalError) or error.status == 401:
            return {"status": "auth_expired"}
        if not bootstrapped:
            cause = error.__cause__
            if (isinstance(cause, httpx.RequestError)
                    or isinstance(cause, httpx.HTTPStatusError) and cause.response.is_server_error):
                return {"status": "game_login_unavailable", "auth": auth}
            if isinstance(cause, (GameMessage, httpx.HTTPStatusError)):
                return {"status": "game_login_rejected", "auth": auth}
            return {"status": "game_login_failed", "auth": auth}
        return {"status": "unconfirmed", "auth": auth}
    except DuplicateLoginError:
        return {"status": "duplicate_login", "auth": auth}
    except (MaintenanceError, TaskDeferred):
        return {"status": "deferred", "auth": auth}
    finally:
        accounts.remove(account_id)


def main() -> None:
    logging.disable(logging.CRITICAL)
    result = {"status": "worker_error"}
    try:
        if os.environ.get("ARK_DATA_DIR") and os.environ.get("ARK_PLATFORM") == "linux":
            exit_with_bridge(int(sys.argv[1]))
            cancel = threading.Event()
            signal.signal(signal.SIGTERM, lambda signum, frame: cancel.set())
            request = json.loads(sys.stdin.buffer.readline(16_385))
            last = 0.0

            def progress(message):
                nonlocal last
                now = time.monotonic()
                if now - last >= 2:
                    print(json.dumps({"progress": message[:160]}, ensure_ascii=True), flush=True)
                    last = now

            result = dispatch(request, cancel=cancel, progress=progress if request.get("stream") else None)
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
