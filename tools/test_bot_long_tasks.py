import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import bot_long_tasks as long
from backend.bot_worker import dispatch


class PurchaseTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.progress = Mock()
        self.records = [{"_id": "record1", "StaticID": "FriendShip3", "BuyCount": 0},
                        {"_id": "record2", "StaticID": "FriendShip4", "BuyCount": 0}]
        self.client = SimpleNamespace(account_state={
            "StoreRecordContainer": {"Records": self.records},
            "ItemContainer": {"Items": [{"StaticID": "24", "Count": 300}]},
        }, _auth_data=lambda data: data, call=Mock())
        self.picks = long.parse("buy_batch", "FriendShip3:1:24:100 FriendShip4:1:24:100")
        self.first = {"CommodityRecord": {**self.records[0], "BuyCount": 1},
                      "CostItems": [{"Item": {"StaticID": "24", "Count": 100}}]}

    def test_price_change_and_absent_record_stop_before_first_request(self):
        for action in ("FriendShip3:1:24:99", "FriendShip3:1:24:100 FriendShip10:1:24:1000"):
            status, _ = long.buy(self.client, long.parse("buy_batch", action), self.cancel, self.progress)
            self.assertEqual(status, "skipped")
        self.client.call.assert_not_called()

    def test_disconnect_keeps_confirmed_purchase_and_never_retries(self):
        self.client.call.side_effect = [self.first, TimeoutError]
        status, details = long.buy(self.client, self.picks, self.cancel, self.progress)
        self.assertEqual(status, "unconfirmed")
        self.assertIn("FriendShip3 已确认 1", details[0])
        self.assertIn("FriendShip4", details[1])
        self.assertEqual(self.client.call.call_count, 2)
        route, data = self.client.call.call_args_list[0].args
        self.assertEqual((route, data["Count"], data["Record"]["_id"]),
                         ("StoreHandler.BuyCommodity", 1, "record1"))

    def test_changed_debit_stops_the_batch(self):
        self.client.call.return_value = {**self.first, "CostItems": [{"Item": {"StaticID": "24", "Count": 101}}]}
        status, _ = long.buy(self.client, self.picks, self.cancel, self.progress)
        self.assertEqual(status, "unexpected_cost")
        self.client.call.assert_called_once()

    def test_cancellation_between_purchases_preserves_first_settlement(self):
        def finished(*args):
            self.cancel.set()
            return self.first

        self.client.call.side_effect = finished
        status, details = long.buy(self.client, self.picks, self.cancel, self.progress)
        self.assertEqual(status, "cancelled")
        self.assertIn("FriendShip3 已确认", details[0])
        self.client.call.assert_called_once()


class AbyssTests(unittest.TestCase):
    def test_single_floor_uses_confirmed_roster_without_auto_advance(self):
        args = long.parse("abyss", "MysteriousRealm_1 H001,-,H002 H003")
        cancel = threading.Event()
        waves = {"upper": ["role1", None, "role2", None], "lower": ["role3", None, None, None]}
        params = {"sceneId": args[0], "waves": waves}
        client = SimpleNamespace(account_state={})
        with patch("backend.tasks.normalize_abyss_config", return_value=params) as validate, patch(
                "backend.abyss_battle.run_abyss_battle", return_value={"ok": True, "raw": {"Result": "Win"}}) as run:
            self.assertEqual(long.abyss(client, args, cancel, Mock())[0], "ok")
        validate.assert_called_once_with(client.account_state, *args)
        run.assert_called_once()
        self.assertEqual(set(run.call_args.args[1]), {"sceneId", "waves", "_cancel_event", "_on_progress"})


class ShopRefreshTests(unittest.TestCase):
    def test_reference_quotes_need_no_game_session(self):
        with patch("backend.appcore._bootstrap_and_register") as login:
            result = dispatch({"action": "run", "task": "long_info", "code": "shop"})
        login.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertIn("参考价", result["details"][0])
        self.assertIn("单价", result["details"][1])

    def test_game_login_failure_is_distinguished_from_unknown_settlement(self):
        from backend.appcore import ApiError

        request = {"action": "run", "task": "long_info", "code": "activity",
                   "auth": {"user_id": "fixture", "access_token": "fixture"}}
        with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
            "backend.appcore._bootstrap_and_register", side_effect=ApiError(400, "raw upstream data"),
        ):
            result = dispatch(request)
        self.assertEqual(result["status"], "game_login_failed")
        self.assertNotIn("raw upstream data", str(result))

    def test_secret_shop_uses_only_the_free_refresh(self):
        client = SimpleNamespace(account_state={}, _auth_data=lambda data: data, call=Mock(return_value={
            "Records": [{"Store": "SecretShop", "StaticID": "fixture"}],
        }))
        status, details = long.refresh_shop(client, "refresh_secret", threading.Event())
        self.assertEqual(status, "ok")
        client.call.assert_called_once_with("StoreHandler.ResetRandomStore", {"StoreID": "SecretShop", "IsUseGold": 0})
        self.assertIn("自动购买 0 件", details[0])

    def test_star_source_reads_state_then_refreshes_once(self):
        client = SimpleNamespace(_auth_data=lambda data: data, call=Mock(side_effect=[
            {"Data": {"RefreshCount": 2}}, {"Data": {"RefreshCount": 3}},
        ]))
        self.assertEqual(long.refresh_shop(client, "refresh_star_source", threading.Event())[0], "ok")
        self.assertEqual([call.args[0] for call in client.call.call_args_list], [
            "CustomEquipHandler.Query", "CustomEquipHandler.RefreshEquip",
        ])

    def test_star_source_skips_if_refresh_count_is_unavailable(self):
        client = SimpleNamespace(_auth_data=lambda data: data, call=Mock(return_value={"Data": {}}))
        self.assertEqual(long.refresh_shop(client, "refresh_star_source", threading.Event())[0], "skipped")
        client.call.assert_called_once()
