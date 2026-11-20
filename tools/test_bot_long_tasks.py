import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from backend import bot_long_tasks as long
from backend import bot_shop_purchase as shop
from backend.bot_worker import dispatch
from backend.game_client import GameError, TaskDeferred


class ShopPurchasesTests(unittest.TestCase):
    def test_confirmed_secret_refresh_buys_only_selected_ordinary_items(self):
        quote = json.loads(long.SECRET_SHOP_QUOTE.read_bytes())
        quote = {key: quote[key] for key in ("gameVersion", "catalog", "currency", "unitPrice")}
        code = json.dumps({"count": 1, "quote": quote, "budget": 3, "scheme": None, "targets": ["recruit"]})
        args = long.secret_shop_paid_order(code)
        token = {"Item": {"StaticID": "5", "Count": 5}}
        other = {"Item": {"StaticID": "6", "Count": 5}}
        records = [{"DropResult": {"Items": [item]}, "Store": "SecretShop"} for item in (token, other)]
        client = SimpleNamespace(login_version=quote["gameVersion"], login_catalog=quote["catalog"],
                                 account_state={"VIPInfo": {"LV": 7}, "AccountSaveData": {"RandomStoreDayRefreshCount": 0},
                                                "ItemContainer": {"Items": [{"StaticID": "2", "Count": 3}]}},
                                 _auth_data=lambda data: data, call=Mock(side_effect=[
                                     {"Records": records, "CostItems": [{"Item": {"StaticID": "2", "Count": 3}}]},
                                     {"Drop": {"Items": [token]}, "CostItems": [{"Item": {"StaticID": "1", "Count": 20000}}]},
                                 ]))
        status, details = long.refresh_secret_paid(client, args, threading.Event(), Mock())
        self.assertEqual(status, "ok")
        self.assertIn("自动购买 1", details[1])
        self.assertIn("金币 ×20000", details[1])
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
                         ["StoreHandler.ResetRandomStore", "StoreHandler.BuyCommodity"])
        self.assertEqual(client.call.call_args.args[1]["Record"]["DropResult"]["Items"], [token])

    def test_secret_equipment_purchase_accepts_verified_fingerprint_and_rejects_changed_shelf(self):
        equipment = {"StaticID": "EQ01", "Set": "Speed", "MainProp": {"Value": 12}}
        record = {"Store": "SecretShop", "_id": {"$oid": "record"}, "LastResetTime": {"$date": 100},
                  "DropResult": {"Items": [{"Equipment": equipment}]}}
        option = shop.secret_option(record, 2)
        self.assertEqual(option["price"], {"currency": "1", "amount": None})
        order = shop.confirmed_order(json.dumps({**option, "shop": "secret", "expires": 10**12}))
        client = SimpleNamespace(account_state={"StoreRecordContainer": {"Records": [record]}},
                                 _auth_data=lambda data: data, call=Mock(return_value={
                                     "Drop": {"Items": [{"Equipment": equipment}]},
                                     "CostItems": [{"Item": {"StaticID": "1", "Count": 500}}],
                                 }))
        record["LastResetTime"]["$date"] = 101
        self.assertEqual(shop.buy_confirmed(client, order)[0], "skipped")
        client.call.assert_not_called()
        record["LastResetTime"]["$date"] = 100
        self.assertIn("500 金币", shop.buy_confirmed(client, order)[1][0])
        client.call.assert_called_once()

    def test_star_source_free_refresh_pauses_before_buying_exact_equipment(self):
        policy = shop.star_quote()
        scheme = {"slots": {"ring": [{"sets": ["Speed"]}]}}
        args = long.star_source_order(json.dumps({"count": 3, "quote": policy, "budget": 0, "scheme": scheme}))
        ring = {"StaticID": "MATCH01", "Set": "Speed"}
        shelf = {"shelf": [ring], "refreshCount": 9, "revertTime": 100}
        result = {"ok": True, **shelf, "matches": [{"shopIndex": 0, "setCN": "速度"}], "costItems": []}
        client = SimpleNamespace(login_version=policy["gameVersion"], login_catalog=policy["catalog"],
                                 account_state={"ItemContainer": {"Items": [{"StaticID": "109", "Count": 20}]}},
                                 call=Mock())
        options = []
        settled = {"ok": True, "Drop": {"Items": [{"Equipment": ring}]},
                   "CostItems": [{"Item": {"StaticID": "109", "Count": 5}}]}
        with patch("backend.tasks.star_source_query", side_effect=[{"ok": True, "refreshCount": 8}, shelf, shelf]), patch(
            "backend.tasks.star_source_refresh", return_value=result,
        ) as refresh, patch("backend.tasks.star_source_buy", return_value=settled) as buy:
            status, details = long.refresh_star_source(client, args, threading.Event(), Mock(), options=options)
            self.assertEqual(status, "shop_hit")
            self.assertIn("不消耗货币", details[0])
            refresh.assert_called_once()
            buy.assert_not_called()
            order = {**options[0], "shop": "star", "expires": 10**12}
            self.assertEqual(shop.buy_confirmed(client, {**order, "key": "0" * 64})[0], "skipped")
            buy.assert_not_called()
            self.assertIn("5 彩虹星源", shop.buy_confirmed(client, order)[1][0])
            buy.assert_called_once_with(client, 0, include_settlement=True)


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

    def test_new_account_gate_does_not_report_unknown_purchase_settlement(self):
        for steps, expected in (([TaskDeferred("day changed")], "not_started"),
                                ([self.first, TaskDeferred("day changed")], "partial")):
            with self.subTest(expected=expected):
                self.client.call.reset_mock()
                self.client.call.side_effect = steps
                status, details = long.buy(self.client, self.picks, self.cancel, self.progress)
                self.assertEqual(status, expected)
                self.assertIn("未提交", details[-1])
                self.assertEqual(len(steps), len(details))
                if expected == "partial":
                    self.assertIn("FriendShip3 已确认 1", details[0])

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

    def test_guard_before_room_creation_does_not_report_unknown_battle(self):
        args = long.parse("abyss", "MysteriousRealm_1 H001 H003")
        with patch("backend.tasks.normalize_abyss_config", return_value={"waves": {}}), patch(
            "backend.abyss_battle.run_abyss_battle", side_effect=TaskDeferred("day changed"),
        ):
            status, details = long.abyss(SimpleNamespace(account_state={}), args, threading.Event(), Mock())
        self.assertEqual(status, "not_started")
        self.assertIn("未提交", details[0])


class ActivityTests(unittest.TestCase):
    def test_queries_offer_only_ready_activities_and_keep_sweep_reference_read_only(self):
        def stage(sid, **overrides):
            return {"id": sid, "family": sid.split("_")[0], "serverBattle": True,
                    "stage": int(sid.rsplit("_", 1)[-1]), "cleared": False, "preScene": "", "energy": 8, "energyKnown": True,
                    "extraEntryCost": False, "storyTeam": False, "activityName": "模拟活动", **overrides}

        state = {"SceneDataContainer": {"Scenes": [{"StaticID": "BH1_1_1", "Stars": [1]}]}}
        stages = [
            stage("BH1_1_1", cleared=True, energy=17), stage("BH1_1_2", preScene="BH1_1_1"),
            stage("BH2_1_1", extraEntryCost=True), stage("BH3_1_1", energyKnown=False),
            stage("BH4_1_2", preScene="BH4_1_1"), stage("BH5_1_1", serverBattle=False),
        ]
        choices = []
        with patch("backend.activity_progress.scenes", return_value=stages):
            details = long.query(state, "activity", options=choices)
            self.assertEqual(choices, ["BH1"])
            self.assertIn("BH1_1_2", details[-1])
            choices.clear()
            reference = long.query(state, "activity_sweep BH1", options=choices)
            self.assertEqual(choices, [])
            self.assertIn("BH1_1_1", reference[-1])
            self.assertIn("17 体力", reference[-1])
            self.assertTrue(all("BH1_1_2" not in row for row in reference))

    def test_abyss_options_only_include_supported_free_server_stages(self):
        ids = ["MysteriousRealm_8", "MysteriousRealm_9", "MysteriousRealm_10"]
        definitions = {sid: {"ServerBattle": "TRUE"} for sid in ids}
        definitions[ids[1]]["Energy"] = "5"
        rows = [{"id": sid, "floor": floor, "cleared": floor < 10} for floor, sid in enumerate(ids, start=8)]
        with patch("backend.tasks.abyss_scenes", return_value=rows), patch(
            "backend.abyss_state.metadata", return_value={"tables": {"Scene": definitions}},
        ):
            choices = []
            details = long.query({}, "abyss", options=choices)
        self.assertEqual(choices, [ids[0], ids[2]])
        self.assertNotIn(ids[1], details[0])
        self.assertEqual(details[1], "快照已通关楼层 8")

    def test_activity_does_not_start_when_budget_or_balance_preflight_fails(self):
        state = {"ItemContainer": {"Items": [{"StaticID": "3", "Count": 2}]}}
        stage = {"id": "BH1_1_1", "serverBattle": True, "cleared": False,
                 "preScene": "", "energy": 8, "energyKnown": True}
        with patch("backend.activity_progress.scenes", return_value=[stage]), patch(
            "backend.activity_progress.validate_scene", return_value={},
        ), patch("backend.abyss_battle.run_activity_battle") as battle:
            client = SimpleNamespace(account_state=state)
            for budget, hint in ((7, "体力上限"), (8, "账号体力不足")):
                with self.subTest(budget=budget):
                    status, details = long.activity(client, ("BH1", "0", 1, budget), threading.Event(), Mock())
                    self.assertEqual(status, "not_started")
                    self.assertIn(hint, details[-1])
            battle.assert_not_called()

    def test_second_stage_validation_failure_keeps_confirmed_stage_and_energy(self):
        from backend import activity_progress

        first, second = "BH999_1_1", "BH999_1_2"
        available = [
            {"id": first, "name": "first", "serverBattle": True, "energy": 5, "preScene": "", "cleared": False},
            {"id": second, "name": "second", "serverBattle": True, "energy": 5, "preScene": first, "cleared": False},
        ]
        state = {"ItemContainer": {"Items": [{"StaticID": "3", "Count": 40}]},
                 "SceneDataContainer": {"Scenes": []}}

        def settle(client, start_info, **kwargs):
            state["SceneDataContainer"]["Scenes"].append({"StaticID": first, "Stars": [1]})
            available[0]["cleared"] = True
            return {"ok": True, "confirmed": True}

        progress = Mock()
        with patch.object(activity_progress, "scenes", return_value=available), patch.object(
            activity_progress, "validate_scene", return_value={},
        ), patch.object(activity_progress, "start_info", side_effect=[{}, GameError("unavailable team")]), patch(
            "backend.abyss_battle.run_activity_battle", side_effect=settle,
        ) as battle, patch("backend.config.get_request_delay_config", return_value={"enabled": False}):
            status, details = long.activity(SimpleNamespace(account_state=state), ("BH999", "0", 2, 10),
                                            threading.Event(), progress)
        self.assertEqual(status, "partial")
        self.assertIn("共 5 体力", details[0])
        self.assertEqual(details[1], first)
        self.assertIn("队伍配置", details[-1])
        self.assertTrue(any("本轮确认 1 关" in item.args[0] for item in progress.call_args_list))
        battle.assert_called_once()


class ShopRefreshTests(unittest.TestCase):
    def test_secret_shop_paid_order_requires_an_exact_quote_and_budget(self):
        policy = json.loads(long.SECRET_SHOP_QUOTE.read_bytes())
        quote = {key: policy[key] for key in ("gameVersion", "catalog", "currency", "unitPrice")}
        order = {"count": 90, "quote": quote, "budget": 270, "scheme": None}
        self.assertEqual(long.secret_shop_paid_order(json.dumps(order))[:3], (90, quote, policy["vipDailyLimits"]))
        for unapproved in ({**order, "budget": 269}, {**order, "count": 201},
                           {**order, "quote": {**quote, "unitPrice": 2}}):
            with patch("backend.appcore._bootstrap_and_register") as login:
                result = dispatch({"action": "run", "task": "refresh_secret_paid", "code": json.dumps(unapproved)})
            self.assertEqual(result["status"], "quote_unavailable")
            login.assert_not_called()

    def test_paid_secret_shop_stops_at_quota_and_on_changed_debit_or_equipment_hit(self):
        policy = json.loads(long.SECRET_SHOP_QUOTE.read_bytes())
        quote = {key: policy[key] for key in ("gameVersion", "catalog", "currency", "unitPrice")}
        scheme = {"slots": {"ring": [{"sets": ["Speed"]}]}}
        args = long.secret_shop_paid_order(json.dumps({"count": 90, "quote": quote, "budget": 270, "scheme": scheme}))
        account_state = {"VIPInfo": {"LV": 7}, "AccountSaveData": {"RandomStoreDayRefreshCount": 88},
                         "ItemContainer": {"Items": [{"StaticID": "2", "Count": 300}]}}
        client = SimpleNamespace(login_version=quote["gameVersion"], login_catalog=quote["catalog"],
                                 account_state=account_state)
        paid = {"ok": True, "records": [{"Store": "SecretShop"}],
                "costItems": [{"Item": {"StaticID": "2", "Count": 3}}]}
        cases = (
            ([paid, paid], "partial", "已确认刷新 2 次", 2),
            ([paid, {**paid, "costItems": [{"Item": {"StaticID": "2", "Count": 4}}]}],
             "unexpected_cost", "已确认刷新 1 次", 2),
            ([{**paid, "legendaryMatches": [{"index": 1, "setCN": "速度", "slotCN": "戒指"}]}],
             "shop_hit", "已确认刷新 1 次", 1),
        )
        for results, expected, summary, calls in cases:
            cancel = Mock(spec=threading.Event, **{"is_set.return_value": False, "wait.return_value": False})
            with self.subTest(status=expected), patch("backend.tasks.shop_reset", side_effect=results) as reset:
                status, details = long.refresh_secret_paid(client, args, cancel, Mock())
                self.assertEqual(status, expected)
                self.assertIn(summary, details[0])
                self.assertEqual(reset.call_count, calls)
                self.assertTrue(all(call.kwargs == {"use_gold": True, "auto_buy_sids": [],
                                                    "equipment_scheme": scheme} for call in reset.call_args_list))
        client.login_catalog = "catalog_999999"
        with patch("backend.tasks.shop_reset") as reset:
            self.assertEqual(long.refresh_secret_paid(client, args, threading.Event(), Mock())[0], "quote_unavailable")
            reset.assert_not_called()
        client.login_catalog = quote["catalog"]
        client.account_state["ItemContainer"]["Items"][0]["Count"] = 2
        with patch("backend.tasks.shop_reset") as reset:
            self.assertEqual(long.refresh_secret_paid(client, args, threading.Event(), Mock())[0], "skipped")
            reset.assert_not_called()

    def test_secret_shop_cooldown_needs_no_live_guild_preflight(self):
        client = SimpleNamespace(account_state={}, call=Mock())
        prepare = Mock()
        with patch("backend.tasks.secret_shop_next_free", return_value=10**15):
            status, details = long.refresh_shop(client, "refresh_secret", threading.Event(), prepare=prepare)
        self.assertEqual(status, "skipped")
        self.assertIn("冷却", details[0])
        prepare.assert_not_called()
        client.call.assert_not_called()

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
        client.call.assert_not_called()

    def test_star_source_refresh_never_opens_a_session_without_verified_quote(self):
        with patch("backend.appcore._bootstrap_and_register") as login, patch(
            "backend.tasks.star_source_refresh",
        ) as refresh:
            for code in ("", "1", "200", '{"order":"1","scheme":{"slots":{}}}'):
                with self.subTest(code=code):
                    result = dispatch({"action": "run", "task": "refresh_star_source", "code": code})
                    self.assertEqual(result["status"], "quote_unavailable")
            login.assert_not_called()
            refresh.assert_not_called()

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

    def test_star_source_status_reads_state_without_refreshing_or_using_product_price(self):
        from backend.bot_views import parse_view, view

        query = {"Data": {"RefreshCount": 2, "PriceItemID": "109", "Price": 5}}
        client = SimpleNamespace(account_state={}, _auth_data=lambda data: data, call=Mock(return_value=query))
        account = SimpleNamespace(client=client, prepare_guild_for_qq_locked=Mock())
        details = view(account, parse_view("star_source"))
        self.assertIn(" 2", details[0])
        self.assertNotIn("109", str(details))
        account.prepare_guild_for_qq_locked.assert_called_once()
        client.call.assert_called_once_with("CustomEquipHandler.Query", {"CommodityID": "RainbowStarSourceBox10"})

    def test_account_summary_excludes_raw_identity(self):
        from backend.bot_views import parse_view, view

        client = SimpleNamespace(account_state={"Info": {"LV": 42, "Name": "fixture-private", "CUID": "fixture-id"}})
        details = view(SimpleNamespace(client=client), parse_view("summary"))
        self.assertIn("42", details[0])
        self.assertNotIn("fixture", str(details))
        self.assertIsNone(parse_view("summary 2"))
