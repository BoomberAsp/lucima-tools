import threading
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from backend import accounts, appcore, config
from backend.bot_worker import dispatch, phase_two
from backend.game_client import GameClient, GameError
from backend.tasks import task_dispatch


class PhaseTwoTests(unittest.TestCase):
    def test_restore_validates_the_account_from_portal(self):
        saved = {"user_id": "test-owner", "refresh_token": "dummy-refresh"}
        with patch("backend.portal.portal_refresh_v2", return_value={**saved, "access_token": "dummy-access"}) as refresh:
            outcome = dispatch({"action": "resume", "auth": saved})
            refresh.assert_called_once_with(saved)
        self.assertEqual(outcome["status"], "ok")
        with patch("backend.portal.portal_refresh_v2", return_value={**saved, "user_id": "another"}):
            self.assertEqual(dispatch({"action": "resume", "auth": saved}), {"status": "login_failed"})

    def test_claim_dispatch_never_redispatches(self):
        state = {"ArkHighCommandData": {"DispatchedQuests": [{"StaticID": "A", "FinishTime": 1}]}}
        client = SimpleNamespace(account_state=state, _auth_data=lambda data: data,
                                 call=Mock(return_value={"ArkHighCommandData": {"DispatchedQuests": []}}))
        result = task_dispatch(client, {"claimOnly": True})
        self.assertEqual(result["count"], 1)
        client.call.assert_called_once_with("ArkHighCommandHandler.RewardQuest", {"QuestStaticID": "A"})

    def test_donation_uses_single_unit_cap(self):
        account = SimpleNamespace(client=SimpleNamespace(account_state={}), lock=threading.Lock(),
                                  _request_scope=lambda: nullcontext(), require_current_day=Mock(),
                                  prepare_guild_for_qq_locked=Mock(), guild_runtime={})
        with patch("backend.guild.has_guild", return_value=True), patch("backend.guild.GuildService") as guild:
            guild.return_value.donate_if_due.return_value = 1
            result, details = phase_two(account, "donate", "gold")
        self.assertEqual(result, "ok")
        self.assertIn("5000", details[0])
        guild.return_value.donate_if_due.assert_called_once_with("gold", max_count=1)

    def test_unspecified_free_quota_does_not_initiate_purchase(self):
        account = SimpleNamespace(client=SimpleNamespace(account_state={}), lock=threading.Lock(),
                                  _request_scope=lambda: nullcontext(), require_current_day=Mock())
        with patch("backend.tasks._summon_record", return_value={}), patch("backend.tasks.task_free_summon") as summon:
            status, _ = phase_two(account, "recruit", "")
        self.assertEqual(status, "deferred")
        summon.assert_not_called()

    def test_renewal_uses_auth_expired_only_for_explicit_rejection(self):
        def timeout(_request):
            raise httpx.ConnectTimeout("fixture timeout")

        cases = (
            (lambda request: httpx.Response(401, json={"message": "rejected"}), "auth_expired"),
            (lambda request: httpx.Response(401, text="gateway page"), "auth_unavailable"),
            (lambda request: httpx.Response(429, json={}), "auth_unavailable"),
            (lambda request: httpx.Response(503, json={}), "auth_unavailable"),
            (lambda request: httpx.Response(200, json={"data": []}), "auth_unavailable"),
            (timeout, "auth_unavailable"),
        )
        auth = {"user_id": "fixture", "access_token": "old", "refresh_token": "fixture", "device_id": "fixture"}
        for handler, expected in cases:
            for action in ("run", "resume"):
                request = {"action": action, "task": "reactor", "auth": auth}
                client = httpx.Client(transport=httpx.MockTransport(handler))
                with self.subTest(expected=expected, action=action), patch(
                    "backend.portal.httpx.Client", return_value=client,
                ), patch("backend.portal.access_token_needs_refresh", return_value=True), patch(
                    "backend.appcore._bootstrap_and_register",
                ) as login:
                    self.assertEqual(dispatch(request)["status"], expected)
                    login.assert_not_called()

    def test_portal_failure_during_bootstrap_preserves_authorization(self):
        from backend.portal import PortalError

        def fail(*args, **kwargs):
            raise appcore.ApiError(401, "private") from PortalError("private upstream failure")

        auth = {"user_id": "fixture", "access_token": "fixture"}
        with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
            "backend.appcore._bootstrap_and_register", side_effect=fail,
        ):
            self.assertEqual(dispatch({"action": "run", "task": "reactor", "auth": auth}),
                             {"status": "auth_unavailable", "auth": auth})

    def test_joined_account_preflight_unlocks_operations_without_running_other_tasks(self):
        self.enterContext(patch.object(config, "load_accounts", return_value={}))
        cases = (("reactor", ""), ("potion", ""), ("starforce", ""), ("friend_support", ""),
                 ("tesseract", ""), ("sweep_illusion", "1"), ("account_view", "equip_event"),
                 ("refresh_secret", "1 free"), ("refresh_star_source", "1"), ("buy_batch", "FriendShip3:1:24:100"))
        for task, code in cases:
            with self.subTest(task=task):
                client = GameClient("")
                client.aid, client.session_id = "fixture-aid", "fixture-session"
                client._replace_account_state({
                    "Info": {"GuildSubInfo": {"_id": {"$oid": "fixture-guild"}}}, "Utc": int(time.time()),
                    "ArkStarForceLabData": {"NextCanReceiveTesseractTime": {"$date": 1}},
                    "StoreRecordContainer": {"Records": [{"_id": "record1", "StaticID": "FriendShip3", "BuyCount": 0}]},
                    "ItemContainer": {"Items": [{"StaticID": "24", "Count": 1000}]},
                })
                account = accounts.Account("fixture", client, None)
                self.addCleanup(account.close)
                original = account.prepare_guild_for_qq_locked

                def prepare_twice():
                    original()
                    original()

                request = {"action": "run", "task": task, "code": code,
                           "auth": {"user_id": "fixture", "access_token": "fixture"}}
                with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
                    "backend.appcore._bootstrap_and_register", return_value=account,
                ), patch.object(account, "prepare_guild_for_qq_locked", side_effect=prepare_twice), patch(
                    "backend.guild.GuildService.enter", return_value={"checkedIn": False},
                ) as enter, patch.object(account, "_claim_guild_locked") as unrequested, patch(
                    "backend.accounts.run_toggle_tasks", side_effect=self.toggle_after_preflight,
                ), patch("backend.tasks.abyss_progress", return_value={"sync": 1, "floor": 80, "record": {}}), patch.object(
                    client, "_call_serial", side_effect=self.snapshot_response,
                ) as game:
                    self.assertEqual(dispatch(request)["status"], "ok")
                    enter.assert_called_once_with(checkin=False)
                    unrequested.assert_not_called()
                    game.assert_called()
                    game.reset_mock()
                    enter.side_effect = GameError("private")
                    account.guild_runtime.pop("enteredRevision", None)
                    result = dispatch(request)
                    self.assertEqual(result["status"], "not_started")
                    self.assertEqual(result["details"], ["佣兵团同步失败。"])
                    game.assert_not_called()

    @staticmethod
    def toggle_after_preflight(client, task_ids, params, runtime, on_partial):
        client.call("Test.Toggle")
        return [{"id": task_ids[0], "ok": True}]

    @staticmethod
    def snapshot_response(route, data):
        if route == "StoreHandler.ResetRandomStore":
            return {"Records": [{"Store": "SecretShop", "StaticID": "fixture"}]}
        if route == "StoreHandler.BuyCommodity":
            return {"CommodityRecord": {"StaticID": "FriendShip3", "_id": "record1", "BuyCount": 1},
                    "CostItems": [{"Item": {"StaticID": "24", "Count": 100}}]}
        if route == "CustomEquipHandler.Query":
            return {"RefreshCount": 1}
        if route == "CustomEquipHandler.RefreshEquip":
            return {"RefreshCount": 2}
        if route.endswith("PurityScene"):
            return {"CostItems": [{"Item": {"StaticID": "39", "Count": 1}}]}
        return {}
