"""Isolated account coordination regressions; no exchange or operational files."""
import multiprocessing
from contextlib import nullcontext
from pathlib import Path
import tempfile
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import subprocess
import unittest
from unittest.mock import patch

from utils.account_coordination import (CoordinatedUpbit, CoordinationError,
    account_lock, cash_claims, set_cash_claim, assert_legacy_processes_coordinated,
    register_legacy_process, raw_mutation_guard, active_multiagent_claim)


class FakeExchange:
    def __init__(self, balance=10000, response=None):
        self.balance = balance
        self.response = response
        self.submitted = 0

    def get_balance(self, ticker):
        return self.balance

    def buy_market_order(self, ticker, price, contain_req=False):
        self.submitted += 1
        return self.response

    def get_order(self, uuid):
        return {"uuid": uuid, "state": "wait"}


def reserve_from_pool(root, owner, queue):
    with account_lock(Path(root)):
        approved = cash_claims(root=Path(root)) + 6000 <= 10000
        if approved:
            set_cash_claim(owner, 6000, Path(root))
        queue.put(approved)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_cross_process_reservation_never_double_spends(self):
        # Given: two independent processes competing for one pool.
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=reserve_from_pool, args=(str(self.root), str(i), queue)) for i in range(2)]
        # When: both reserve 6,000 from 10,000.
        for process in processes:
            process.start()
        results = [queue.get(timeout=5), queue.get(timeout=5)]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        queue.close()
        # Then: exactly one can reserve.
        self.assertEqual(sorted(results), [False, True])

    def test_reservation_workers_survive_initialized_parent(self):
        # Given: the parent's adapter/SQLite are initialized and a thread is active.
        CoordinatedUpbit(FakeExchange(response={"uuid": "confirmed"}), self.root).buy_market_order("KRW-ETH", 5000)
        ready, finish = Event(), Event()
        def wait_for_release() -> None:
            ready.set()
            finish.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=1) as pool:
            background = pool.submit(wait_for_release)
            self.assertTrue(ready.wait(timeout=5))
            try:
                # When: real workers start; unsafe fork fails without crashing macOS.
                with patch("os.fork", side_effect=RuntimeError("unsafe fork attempted")):
                    self.test_cross_process_reservation_never_double_spends()
            finally:
                finish.set()
            background.result(timeout=5)
        # Then: the owner scenario verifies both exit0 and exactly one reservation.

    def test_nested_threads_keep_claim_updates_atomic(self):
        # Given: independent thread owners.
        def reserve(index):
            with account_lock(self.root):
                set_cash_claim(str(index), 1, self.root)
        # When: nested lock calls run concurrently.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(reserve, range(20)))
        # Then: all durable updates survive.
        self.assertEqual(cash_claims(root=self.root), 20)

    def test_other_system_cash_and_buy_fee_block_legacy_submission(self):
        # Given: 5,000 of 10,000 allocated elsewhere.
        set_cash_claim("new-system", 5000, self.root)
        exchange = FakeExchange()
        # When: legacy tries spending remaining 5,000 excluding fee.
        with self.assertRaises(CoordinationError):
            CoordinatedUpbit(exchange, self.root).buy_market_order("KRW-ETH", 5000)
        # Then: exchange was never mutated.
        self.assertEqual(exchange.submitted, 0)

    def test_lost_response_keeps_claim_across_new_adapter(self):
        # Given: exchange submission returns no confirmed UUID.
        exchange = FakeExchange()
        # When: a response is lost and adapter is reconstructed.
        CoordinatedUpbit(exchange, self.root).buy_market_order("KRW-ETH", 6000)
        rebuilt = CoordinatedUpbit(exchange, self.root)
        # Then: retry cannot reuse the uncertain reservation.
        with self.assertRaises(CoordinationError):
            rebuilt.buy_market_order("KRW-ETH", 6000)
        self.assertAlmostEqual(cash_claims(root=self.root), 6003)

    def test_invalid_balance_blocks_submission(self):
        # Given: malformed exchange evidence.
        exchange = FakeExchange(balance=True)
        # When: legacy requests a purchase.
        with self.assertRaises(CoordinationError):
            CoordinatedUpbit(exchange, self.root).buy_market_order("KRW-ETH", 5000)
        # Then: no mutation.
        self.assertEqual(exchange.submitted, 0)

    def test_running_unregistered_main_blocks_new_system(self):
        # Given: preexisting main process never created the coordinated client.
        with patch("utils.account_coordination._processes", return_value=[(45, "old-start", "python3 -u main.py")]):
            # When: new subsystem tries activation.
            with self.assertRaises(CoordinationError):
                assert_legacy_processes_coordinated(self.root)
        # Then: no live process or operational file was changed.
        self.assertTrue(self.root.is_dir())

    def test_reused_pid_with_different_start_is_not_coordinated(self):
        # Given: registry was written by a different incarnation of the PID.
        with patch("utils.account_coordination.os.getpid", return_value=45), patch(
                "utils.account_coordination._processes", return_value=[(45, "old-start", "python3 -u main.py")]):
            register_legacy_process(self.root)
        # When: same PID now belongs to a later main process.
        with patch("utils.account_coordination._processes", return_value=[(45, "new-start", "python3 -u main.py")]):
            # Then: a stale registry does not authorize live entry.
            with self.assertRaises(CoordinationError):
                assert_legacy_processes_coordinated(self.root)

    def test_transport_exception_retains_uncertain_cash(self):
        # Given: the connection can fail after exchange acceptance.
        exchange = FakeExchange()
        with patch.object(exchange, "buy_market_order", side_effect=TimeoutError):
            # When: submission raises rather than returning UUID.
            with self.assertRaises(TimeoutError):
                CoordinatedUpbit(exchange, self.root).buy_market_order("KRW-ETH", 5000)
        # Then: cash remains reserved without a TTL or process dependency.
        self.assertAlmostEqual(cash_claims(root=self.root), 5002.5)

    def test_ample_cash_never_allows_retry_after_ambiguous_buy_exception(self):
        # Given: ample balance cannot establish whether a lost response was filled.
        exchange = FakeExchange(balance=1_000_000)
        client = CoordinatedUpbit(exchange, self.root)
        with patch.object(exchange, "buy_market_order", side_effect=TimeoutError) as mutation:
            with self.assertRaises(TimeoutError):
                client.buy_market_order("KRW-ETH", 5000)
            # When: existing caller retries the uncertain submission.
            with self.assertRaises(CoordinationError) as caught:
                client.buy_market_order("KRW-ETH", 5000)
        # Then: exactly one exchange mutation occurred, irrespective of spare cash.
        self.assertEqual(1, mutation.call_count)
        self.assertEqual("legacy_submission_requires_reconciliation", caught.exception.reason)

    def test_confirmed_uuid_releases_only_its_submission_claim(self):
        # Given: another subsystem owns cash and exchange confirms new UUID.
        set_cash_claim("other", 1000, self.root)
        exchange = FakeExchange(response={"uuid": "accepted"})
        # When: a safely affordable legacy buy is accepted and queried.
        result = CoordinatedUpbit(exchange, self.root).buy_market_order("KRW-ETH", 5000)
        # Then: only this temporary submission claim is settled.
        self.assertEqual(result["uuid"], "accepted")
        self.assertEqual(cash_claims(root=self.root), 1000)

    def test_actual_macos_uppercase_python_command_blocks_activation(self):
        # Given: macOS runtime's actual executable spelling.
        command = "/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python -u /Users/xyz/Documents/autotrade/main.py"
        with patch("utils.account_coordination._processes", return_value=[(45, "old-start", command)]):
            # When: the unregistered daemon coexists with a new subsystem.
            with self.assertRaises(CoordinationError):
                assert_legacy_processes_coordinated(self.root)
        # Then: source changes cannot authorize the running old process.

    def test_direct_main_script_blocks_activation(self):
        # Given: a shebang-launched main script.
        with patch("utils.account_coordination._processes", return_value=[(45, "old-start", "/Users/xyz/Documents/autotrade/main.py")]):
            # When: new subsystem checks coexistence.
            with self.assertRaises(CoordinationError):
                assert_legacy_processes_coordinated(self.root)
        # Then: direct script execution is also treated as trading worker.

    def test_shell_and_inline_python_text_do_not_fake_main_process(self):
        # Given: inspection commands mention main.py without executing it.
        rows = [(45, "start", "zsh -c 'ps aux | grep python3 main.py'"),
                (46, "start", "python3 -c 'print(\"main.py\")'")]
        # When: only these nontrading commands exist.
        with patch("utils.account_coordination._processes", return_value=rows):
            assert_legacy_processes_coordinated(self.root)
        # Then: no false worker rejection occurs.

    def test_process_inspection_failure_is_coordination_failure(self):
        # Given: process inventory cannot be established.
        with patch("utils.account_coordination.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            # When: activation checks account coexistence.
            with self.assertRaises(CoordinationError) as caught:
                assert_legacy_processes_coordinated(self.root)
        # Then: caller receives the deterministic fail-closed error family.
        self.assertEqual(caught.exception.reason, "process_inspection_unavailable")

    def test_raw_mutation_is_blocked_when_an_account_claim_exists(self):
        # Given: an uncoordinated callback and a reserved account.
        calls = []
        @raw_mutation_guard
        def mutate(value):
            calls.append(value)
            return value
        # When: mutation is requested while allocated cash exists.
        with patch("utils.account_coordination.active_multiagent_claim", return_value=False), patch(
                "utils.account_coordination.account_lock", return_value=nullcontext()), patch(
                "utils.account_coordination.cash_claims", return_value=5000):
            with self.assertRaises(CoordinationError):
                mutate("order")
        # Then: no raw API mutation can occur.
        self.assertEqual([], calls)

    def test_raw_mutation_preserves_original_result_without_claims(self):
        # Given: original callback and no reserved account cash.
        calls = []
        @raw_mutation_guard
        def mutate(value):
            calls.append(value)
            return "accepted-" + value
        # When: the guarded callback executes under the shared lock.
        with patch("utils.account_coordination.active_multiagent_claim", return_value=False), patch(
                "utils.account_coordination.account_lock", return_value=nullcontext()), patch(
                "utils.account_coordination.cash_claims", return_value=0):
            result = mutate("order")
        # Then: legacy callback behavior is preserved exactly once.
        self.assertEqual(["order"], calls)
        self.assertEqual("accepted-order", result)

    def test_zero_cash_managed_owner_still_blocks_raw_mutation(self):
        # Given: a fully invested private allocation has a durable zero-cash row.
        set_cash_claim("multiagent:fully-invested", 0, self.root)
        calls = []
        @raw_mutation_guard
        def mutate():
            calls.append("cancel")
        original = active_multiagent_claim
        # When: raw cancellation attempts to bypass managed order ownership.
        with patch("utils.account_coordination.account_lock", return_value=nullcontext()), patch(
                "utils.account_coordination.active_multiagent_claim", side_effect=lambda: original(self.root)):
            with self.assertRaises(CoordinationError):
                mutate()
        # Then: the zero row remains authoritative and no callback executes.
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
