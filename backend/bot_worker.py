"""One-shot Lucima worker for the QQBotAgent bridge. JSON over private pipes."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys


TASKS = {
    "signin": ("month_signin",),
    "mail": ("claim_mail",),
    "base": ("reactor", "sf_potion", "sf_starforce"),
    "redeem": ("redeem_code",),
}


def exit_with_bridge(parent_pid: int) -> None:
    # A bridge crash must not leave an orphan gameload running alongside its replacement.
    import ctypes

    pr_set_pdeathsig = 1
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        raise RuntimeError("Lucima bridge parent unavailable")


def dispatch(request: dict) -> dict:
    from . import accounts, appcore
    from .game_client import DuplicateLoginError, MaintenanceError, TaskDeferred, is_duplicate_login_message
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

    task = request.get("task")
    code = request.get("code", "")
    if action != "run" or task not in TASKS or (task == "redeem" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code)):
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

    try:
        with maintenance.operation_scope():
            refresh()
            account = appcore._bootstrap_and_register(account_id, auth, auth_refresher=refresh)
            if task == "mail":
                results = appcore.run(account_id, list(TASKS[task]), {})["results"]
            else:
                toggles = TASKS[task]
                if task in {"base", "redeem"} and has_guild(account.client.account_state):
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
        return {"status": status, "auth": auth}
    except (PortalError, appcore.ApiError) as error:
        if is_duplicate_login_message(str(error)):
            return {"status": "duplicate_login", "auth": auth}
        if isinstance(error.__context__, MaintenanceError):
            return {"status": "deferred", "auth": auth}
        if isinstance(error, PortalError) or error.status == 401:
            return {"status": "auth_expired"}
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
            request = json.loads(sys.stdin.buffer.readline(16_385))
            result = dispatch(request)
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
