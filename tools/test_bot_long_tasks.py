import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

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

    def test_game_login_reports_network_and_rejection_without_upstream_payload(self):
        from backend.appcore import ApiError
        from backend.game_client import GameMessage

        request = {"action": "run", "task": "account_view", "code": "summary",
                   "auth": {"user_id": "fixture", "access_token": "fixture"}}
        for cause, status in ((httpx.ConnectTimeout("private data"), "game_login_unavailable"),
                              (GameMessage("private data"), "game_login_rejected")):
            with self.subTest(status=status):
                error = ApiError(400, "private data")
                error.__cause__ = cause
                with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
                    "backend.appcore._bootstrap_and_register", side_effect=error,
                ):
                    self.assertEqual(dispatch(request), {"status": status, "auth": request["auth"]})

    def test_secret_shop_uses_only_the_free_refresh(self):
        client = SimpleNamespace(account_state={}, _auth_data=lambda data: data, call=Mock(return_value={
            "Records": [{"Store": "SecretShop", "StaticID": "fixture"}],
        }))
        status, details = long.refresh_shop(client, "refresh_secret", threading.Event())
        self.assertEqual(status, "ok")
        client.call.assert_called_once_with("StoreHandler.ResetRandomStore", {"StoreID": "SecretShop", "IsUseGold": 0})
        self.assertIn("自动购买 0 件", details[1])

    def test_free_count_is_bounded_and_paid_refreshes_need_quotes(self):
        self.assertEqual(long.parse_shop("refresh_secret", "５ free"), (5, "free"))
        self.assertIsNone(long.parse_shop("refresh_star_source", "1.5"))
        self.assertEqual(long.parse_shop("refresh_star_source", "２００"), (200, "star"))
        self.assertEqual(long.parse_shop("refresh_secret", "200 free"), (200, "free"))
        self.assertIsNone(long.parse_shop("refresh_star_source", "201"))
        client = SimpleNamespace(account_state={}, _auth_data=lambda data: data, call=Mock(return_value={
            "Records": [{"Store": "SecretShop", "StaticID": "fixture"}],
        }))
        status, details = long.refresh_shop(client, "refresh_secret", threading.Event(), (5, "free"))
        self.assertEqual(status, "ok")
        self.assertIn("申请 5 次，实际刷新 1 次", details[0])
        client.call.assert_called_once()
        client.call.reset_mock(return_value=True)
        self.assertEqual(long.refresh_shop(client, "refresh_secret", threading.Event(), (5, "diamond"))[0],
                         "quote_unavailable")
        client.call.assert_not_called()
        client.call.return_value = {"Data": {"RefreshCount": 2}}
        self.assertEqual(long.refresh_shop(client, "refresh_star_source", threading.Event(), (200, "star"))[0],
                         "quote_unavailable")
        self.assertEqual(client.call.call_args.args[0], "CustomEquipHandler.Query")
        client.call.assert_called_once()

    def test_shop_match_returns_only_filtered_equipment_and_waits(self):
        client = SimpleNamespace(account_state={})
        scheme = {"slots": {"ring": [{"sets": ["Speed"]}]}}
        equipment = {"shopIndex": 1, "tierName": "传说", "setCN": "速度", "slotCN": "戒指", "auxScore": 62}
        with patch("backend.tasks.star_source_query", return_value={"ok": True, "refreshCount": 3}), patch(
                "backend.tasks.star_source_refresh", return_value={
                    "ok": True, "refreshCount": 4, "cards": [equipment, {"staticID": "unmatched-fixture"}],
                    "matches": [equipment],
                }) as refresh:
            status, facts = long.refresh_shop(client, "refresh_star_source", threading.Event(), (1, "star"), scheme)
        self.assertEqual(status, "shop_hit")
        self.assertIn("第 2 档 · 传说 · 速度 · 戒指 · 62 分", facts)
        self.assertNotIn("unmatched-fixture", str(facts))
        refresh.assert_called_once_with(client, scheme)

    def test_secret_shop_yields_a_match_only_after_confirming_free_refresh(self):
        client = SimpleNamespace(account_state={})
        rows = {"ok": True, "records": [{"Store": "SecretShop"}],
                "legendaryMatches": [{"index": 0, "setCN": "速度", "slotCN": "鞋子"}]}
        with patch("backend.tasks.shop_reset", return_value=rows) as reset:
            status, details = long.refresh_shop(client, "refresh_secret", threading.Event(), (200, "free"), {"slots": {}})
            self.assertEqual(status, "shop_hit")
            self.assertIn("第 1 档 · 速度 · 鞋子", details)
            self.assertEqual(reset.call_count, 1)
            rows["costItems"] = [{"Item": {"StaticID": "2", "Count": 1}}]
            self.assertEqual(long.refresh_shop(client, "refresh_secret", threading.Event(), (200, "free"), {"slots": {}})[0],
                             "unexpected_cost")

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

    def test_account_summary_excludes_raw_identity(self):
        from backend.bot_views import parse_view, view

        client = SimpleNamespace(account_state={"Info": {"LV": 42, "Name": "fixture-private", "CUID": "fixture-id"}})
        details = view(SimpleNamespace(client=client), parse_view("summary"))
        self.assertIn("42", details[0])
        self.assertNotIn("fixture", str(details))
        self.assertIsNone(parse_view("summary 2"))
