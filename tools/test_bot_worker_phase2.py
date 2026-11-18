import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.bot_worker import phase_two
from backend.tasks import task_dispatch


class PhaseTwoTests(unittest.TestCase):
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
                                  refresh_guild_locked=Mock(), guild_runtime={})
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
