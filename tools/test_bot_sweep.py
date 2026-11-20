import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import bot_long_tasks as long
from backend import bot_sweep as sweep
from backend.bot_sweep_quotes import activity_details, stage_details
from backend.bot_worker import dispatch, phase_two
from backend.game_client import GameError, TaskDeferred


CATALOG = {"version": "fixture", "tables": {"Scene": {
    "HuntFire_10": {"Energy": "12"},
    "HuntFire_9": {"Energy": "12", "EnterCost": "1"},
    "ElfIce_4": {"Energy": "10"},
}}}
AUTH = {"user_id": "fixture", "access_token": "fixture"}


def hunt_state(stamina=100, stars=None):
    return {
        "Teams": {"Settings": [{"ID": "team1", "TeamSetting": {"RolePosMap": {"r1": 0}}}]},
        "ItemContainer": {"Items": [{"StaticID": "3", "Count": stamina}]},
        "SceneDataContainer": {"Scenes": [
            {"StaticID": "HuntFire_10", "Stars": [1, 0, 0] if stars is None else stars},
            {"StaticID": "HuntFire_9", "Stars": [1]},
            {"StaticID": "ElfIce_4", "Stars": [1]},
        ]},
    }


def settlement(price=12, drops=None):
    return {"ok": True, "costItems": [{"Item": {"StaticID": "3", "Count": price}}],
            "dropItems": [{"Item": {"StaticID": "1", "Count": 5}}] if drops is None else drops}


def activity_row(**overrides):
    return {**{"id": "BH188_1_13", "family": "BH188", "chapter": 1, "stage": 13, "cleared": True,
               "energy": 15, "energyKnown": True, "extraEntryCost": False, "storyTeam": False,
               "serverBattle": False, "name": "关卡13", "activityName": "活动", "preScene": ""}, **overrides}


def fake_account(state):
    return SimpleNamespace(client=SimpleNamespace(account_state=state, connection_status=lambda: {}),
                           lock=threading.Lock(), _request_scope=nullcontext,
                           require_current_day=Mock(), prepare_guild_for_qq_locked=Mock())


class SweepOrderTests(unittest.TestCase):
    def test_parse_accepts_only_matching_kind_and_bounded_numbers(self):
        self.assertEqual(long.parse("sweep_hunt", "HuntFire_10 team1 3 36"), ("HuntFire_10", "team1", 3, 36))
        self.assertEqual(long.parse("sweep_elf", "ElfIce_4 team1 99 9999"), ("ElfIce_4", "team1", 99, 9999))
        self.assertEqual(long.parse("sweep_activity", "BH188_1_13 team1 2 30"), ("BH188_1_13", "team1", 2, 30))
        for task, code in (
                ("sweep_hunt", "ElfIce_4 team1 1 10"), ("sweep_elf", "HuntFire_10 team1 1 12"),
                ("sweep_activity", "BH188 team1 1 15"), ("sweep_hunt", "HuntFire_10 team1 0 12"),
                ("sweep_hunt", "HuntFire_10 team1 100 12"), ("sweep_hunt", "HuntFire_10 team1 1 10000"),
                ("sweep_hunt", "HuntFire_10 队伍 1 12"), ("sweep_hunt", "HuntFire_10 team1 1"),
                ("sweep_hunt", ""), ("activity", "HuntFire_10 team1 1 12")):
            with self.subTest(task=task, code=code):
                self.assertIsNone(long.parse(task, code))

    def test_invalid_order_never_opens_a_game_session(self):
        with patch("backend.appcore._bootstrap_and_register") as login:
            for task, code in (("sweep_hunt", "ElfIce_4 team1 1 10"), ("sweep_activity", "bad"),
                               ("sweep_elf", "ElfIce_4 team1 1 10 extra")):
                with self.subTest(task=task):
                    result = dispatch({"action": "run", "task": task, "code": code, "auth": AUTH})
                    self.assertEqual(result["status"], "invalid")
            login.assert_not_called()


class HuntSweepTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.progress = Mock()
        self.client = SimpleNamespace(account_state=hunt_state())

    def run_sweep(self, args, battle, state=None):
        if state is not None:
            self.client.account_state = state
        with patch("backend.bot_sweep.metadata", return_value=CATALOG), patch(
                "backend.bot_sweep.sweep_once", side_effect=battle) as once, patch(
                "backend.config.get_request_delay_config", return_value={"ms": 0, "jitter": 0}):
            status, details = sweep.run_sweep(self.client, args, self.cancel, self.progress)
        return status, details, once

    def test_batch_confirms_exact_stamina_debit_without_quick_battle_tickets(self):
        drops = [{"Item": {"StaticID": "1", "Count": 5}}, {"Equipment": {"StaticID": "EQ1"}}]
        status, details, once = self.run_sweep(("HuntFire_10", "team1", 3, 36), [settlement(drops=drops)] * 3)
        self.assertEqual(status, "ok")
        self.assertEqual(once.call_count, 3)
        for call in once.call_args_list:
            self.assertEqual(call.args, (self.client, "HuntFire_10", "team1"))
            self.assertIs(call.kwargs["quick_battle"], False)
        self.assertIn("已确认扫荡 3 次", details[0])
        self.assertIn("扣除 36 体力（上限 36）", details[0])
        self.assertIn("装备 3 件", details[1])
        self.assertEqual(self.progress.call_count, 3)

    def test_budget_and_stamina_stop_before_the_unaffordable_round(self):
        status, details, once = self.run_sweep(("HuntFire_10", "team1", 5, 36), [settlement()] * 5)
        self.assertEqual((status, once.call_count), ("partial", 3))
        self.assertIn("下一次扫荡会超出体力上限", details[-1])
        def draining(client, scene, team, quick_battle=True):
            # 真实客户端会在 call() 内同步扣减快照体力。
            client.account_state["ItemContainer"]["Items"][0]["Count"] -= 12
            return settlement()

        status, details, once = self.run_sweep(("HuntFire_10", "team1", 5, 9999), draining,
                                               state=hunt_state(stamina=30))
        self.assertEqual((status, once.call_count), ("partial", 2))
        self.assertIn("体力不足", details[-1])

    def test_changed_debit_stops_and_keeps_confirmed_rounds(self):
        status, details, once = self.run_sweep(("HuntFire_10", "team1", 3, 36),
                                               [settlement(), settlement(price=13)])
        self.assertEqual((status, once.call_count), ("unexpected_cost", 2))
        self.assertIn("已确认扫荡 1 次", details[0])
        self.assertIn("标价不符", details[-1])
        status, _, once = self.run_sweep(("HuntFire_10", "team1", 1, 12),
                                         [{**settlement(), "costItems": None}])
        self.assertEqual((status, once.call_count), ("unconfirmed", 1))

    def test_eligibility_and_price_gates_run_before_any_battle_request(self):
        cases = (
            (("HuntFire_10", "team1", 1, 12), hunt_state(stars=[0]), "not_started"),
            (("HuntFire_11", "team1", 1, 12), hunt_state(), "not_started"),
            (("HuntFire_9", "team1", 1, 12), hunt_state(), "not_started"),
            (("HuntFire_10", "teamX", 1, 12), hunt_state(), "not_started"),
            (("HuntFire_10", "team1", 1, 12), hunt_state(stamina=5), "skipped"),
            (("HuntFire_10", "team1", 1, 11), hunt_state(), "not_started"),
        )
        for args, state, expected in cases:
            with self.subTest(args=args):
                status, _, once = self.run_sweep(args, [settlement()], state=state)
                self.assertEqual(status, expected)
                once.assert_not_called()
        with patch("backend.bot_sweep.metadata", return_value={"tables": {"Scene": {}}}), patch(
                "backend.bot_sweep.sweep_once") as once:
            status, _ = sweep.run_sweep(self.client, ("HuntFire_10", "team1", 1, 12), self.cancel, self.progress)
            self.assertEqual(status, "not_started")
            once.assert_not_called()

    def test_failure_paths_never_leak_raw_game_text(self):
        status, details, _ = self.run_sweep(
            ("HuntFire_10", "team1", 1, 12),
            [{"ok": False, "detail": "RAW SERVER TEXT", "costItems": None, "dropItems": []}])
        self.assertEqual(status, "skipped")
        self.assertNotIn("RAW", str(details))
        status, _, once = self.run_sweep(("HuntFire_10", "team1", 2, 24),
                                         [settlement(), TaskDeferred("private")])
        self.assertEqual((status, once.call_count), ("partial", 2))
        status, _, once = self.run_sweep(("HuntFire_10", "team1", 2, 24), [settlement(), TimeoutError])
        self.assertEqual((status, once.call_count), ("unconfirmed", 2))
        status, _, once = self.run_sweep(("HuntFire_10", "team1", 2, 24), [settlement(), GameError("private")])
        self.assertEqual((status, once.call_count), ("partial", 2))
        self.cancel.set()
        status, _, once = self.run_sweep(("HuntFire_10", "team1", 1, 12), [settlement()])
        self.assertEqual(status, "cancelled")
        once.assert_not_called()

    def test_dispatch_routes_sweep_orders_through_the_long_task_runner(self):
        account = fake_account(hunt_state())
        with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
                "backend.appcore._bootstrap_and_register", return_value=account), patch(
                "backend.bot_sweep.metadata", return_value=CATALOG), patch(
                "backend.bot_sweep.sweep_once", return_value=settlement()) as once, patch(
                "backend.config.get_request_delay_config", return_value={"ms": 0, "jitter": 0}):
            result = dispatch({"action": "run", "task": "sweep_hunt", "code": "HuntFire_10 team1 2 24",
                               "auth": AUTH}, cancel=threading.Event(), progress=Mock())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(once.call_count, 2)
        account.prepare_guild_for_qq_locked.assert_called_once()


class ActivitySweepTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.progress = Mock()
        self.client = SimpleNamespace(account_state=hunt_state())

    def run_sweep(self, args, battle, live):
        with patch("backend.bot_sweep.scenes", side_effect=live), patch(
                "backend.bot_sweep.activity_once", side_effect=battle) as once, patch(
                "backend.config.get_request_delay_config", return_value={"ms": 0, "jitter": 0}):
            status, details = sweep.run_sweep(self.client, args, self.cancel, self.progress)
        return status, details, once

    def test_activity_sweep_rechecks_liveness_every_round(self):
        row = activity_row()
        status, details, once = self.run_sweep(("BH188_1_13", "team1", 2, 30),
                                               [settlement(price=15)] * 2, [[row]] * 3)
        self.assertEqual(status, "ok")
        self.assertEqual(once.call_count, 2)
        self.assertIs(once.call_args.kwargs["quick_battle"], False)
        self.assertIn("扣除 30 体力（上限 30）", details[0])
        status, details, once = self.run_sweep(("BH188_1_13", "team1", 3, 45),
                                               [settlement(price=15)] * 3, [[row], [row], [row], []])
        self.assertEqual((status, once.call_count), ("partial", 2))
        self.assertIn("活动已结束", details[-1])
        self.assertIn("已确认扫荡 2 次", details[0])

    def test_activity_gates_refuse_unsupported_or_uncleared_stages(self):
        for overrides in ({"cleared": False}, {"serverBattle": True}, {"storyTeam": True},
                          {"extraEntryCost": True}, {"energyKnown": False}, {"energy": 0}):
            with self.subTest(overrides=overrides):
                status, _, once = self.run_sweep(("BH188_1_13", "team1", 1, 15),
                                                 [settlement(price=15)], [[activity_row(**overrides)]])
                self.assertEqual(status, "not_started")
                once.assert_not_called()
        status, _, once = self.run_sweep(("BH188_1_13", "team1", 1, 15), [settlement(price=15)], [[]])
        self.assertEqual(status, "not_started")
        once.assert_not_called()


class SweepOptionsTests(unittest.TestCase):
    def test_stage_quotes_collect_only_cleared_priced_stages(self):
        state = hunt_state()
        client = SimpleNamespace(account_state=state)
        options = []
        with patch("backend.bot_sweep_quotes.metadata", return_value=CATALOG):
            stage_details(client, state, "hunt", options=options)
            self.assertEqual(options, ["HuntFire_10"])
            options.clear()
            stage_details(client, state, "elf", options=options)
            self.assertEqual(options, ["ElfIce_4"])
            options.clear()
            stage_details(client, hunt_state(stars=[0]), "hunt", options=options)
            self.assertEqual(options, [])

    def test_activity_quotes_collect_sweepable_current_stages(self):
        rows = [activity_row(), activity_row(id="BH188_1_14", serverBattle=True),
                activity_row(id="BH188_1_15", extraEntryCost=True),
                activity_row(id="BH188_1_16", storyTeam=True), activity_row(id="BH188_1_17", cleared=False)]
        options = []
        with patch("backend.activity_progress.scenes", return_value=rows), patch(
                "backend.bot_sweep_quotes.metadata", return_value=CATALOG):
            activity_details(hunt_state(), "activity_sweep", options=options)
        self.assertEqual(options, ["BH188_1_13"])

    def test_worker_returns_sweep_options_for_stage_and_activity_queries(self):
        account = fake_account(hunt_state())
        with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
                "backend.appcore._bootstrap_and_register", return_value=account), patch(
                "backend.bot_sweep_quotes.metadata", return_value=CATALOG):
            result = dispatch({"action": "run", "task": "stages", "code": "hunt", "auth": AUTH})
            self.assertEqual(result["sweepOptions"], ["hunt", ["HuntFire_10"]])
        rows = [activity_row()]
        with patch("backend.portal.access_token_needs_refresh", return_value=False), patch(
                "backend.appcore._bootstrap_and_register", return_value=account), patch(
                "backend.activity_progress.scenes", return_value=rows), patch(
                "backend.bot_sweep_quotes.metadata", return_value=CATALOG):
            result = dispatch({"action": "run", "task": "long_info", "code": "activity_sweep", "auth": AUTH})
            self.assertEqual(result["sweepOptions"], ["activity", ["BH188_1_13"]])
            self.assertNotIn("battleOptions", result)

    def test_phase_two_forwards_options_to_stage_quotes(self):
        account = fake_account(hunt_state())
        options = []
        with patch("backend.bot_sweep_quotes.metadata", return_value=CATALOG):
            status, _ = phase_two(account, "stages", "hunt", options=options)
        self.assertEqual((status, options), ("ok", ["HuntFire_10"]))


if __name__ == "__main__":
    unittest.main()
