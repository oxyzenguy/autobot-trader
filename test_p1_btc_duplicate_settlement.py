# -*- coding: utf-8 -*-
"""Regression test for BTC duplicate settlement defect P1 (R3-01).
Ensures that a terminal order without new fill does not cause duplicate DB rows.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import paper_grid_xrp as paper
from strategy import btc_accumulator as btc
from utils import db_logger, system_auditor as auditor
from test_p1_btc_fill import FakeExchange, DueDatetime

class TestBTCDuplicateSettlement(unittest.TestCase):
    def setUp(self):
        # create isolated temporary directory
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_path = Path(self.temp_dir.name)
        # patch paths for BTC state and DB
        self.patcher_state = patch.multiple(
            btc,
            BTC_STATE_FILE=str(self.work_path / 'btc.json'),
            DB_PATH=str(self.work_path / 'btc.db'),
            datetime=DueDatetime,
        )
        self.patcher_db = patch.object(db_logger, 'DB_PATH', str(self.work_path / 'btc.db'))
        self.patcher_check = patch.object(btc, 'check_btc_tiered_status', return_value={
            'tier': 1,
            'buy_krw': 10000,
            'tier_reason': 'gate',
            'is_valid': True,
        })
        self.patcher_alert = patch.object(main, 'send_telegram_alert')
        self.patcher_sync = patch('utils.sync_manager.trigger_snapshot_sync')
        # start patches
        self.patcher_state.start()
        self.patcher_db.start()
        self.patcher_check.start()
        self.patcher_alert.start()
        self.patcher_sync.start()
        # initialize exchange and run first buy
        self.exchange = FakeExchange()
        btc.execute_btc_tiered_buy_if_due(self.exchange)
        # simulate order fill
        self.exchange.order.update(state='done', executed_volume='0.0001', trades=[{'funds': '10000'}])
        # inject failure on state file replace (to simulate persistence failure)
        self.original_replace = btc.os.replace
        def fail_replace(src, dst):
            if str(dst) == btc.BTC_STATE_FILE:
                raise OSError('gate injected settlement persistence failure')
            return self.original_replace(src, dst)
        self.patcher_os_replace = patch.object(btc.os, 'replace', side_effect=fail_replace)
        self.patcher_os_replace.start()
        # run settlement which should record DB row but fail state save
        btc.execute_btc_tiered_buy_if_due(self.exchange)
        # stop the replace failure for subsequent runs
        self.patcher_os_replace.stop()
        # run again to ensure no duplicate DB rows
        btc.execute_btc_tiered_buy_if_due(self.exchange)

    def tearDown(self):
        # stop all patches
        self.patcher_state.stop()
        self.patcher_db.stop()
        self.patcher_check.stop()
        self.patcher_alert.stop()
        self.patcher_sync.stop()
        self.temp_dir.cleanup()

    def test_no_duplicate_rows(self):
        # verify DB contains exactly one row with correct volume/funds
        db_path = str(self.work_path / 'btc.db')
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute('SELECT COUNT(*), SUM(volume), SUM(cost_or_revenue) FROM trades').fetchone()
        finally:
            conn.close()
        count, sum_vol, sum_funds = rows
        self.assertEqual(count, 1, 'There should be exactly one trade record')
        self.assertAlmostEqual(sum_vol, 0.0001, places=7)
        self.assertEqual(sum_funds, 10000)

if __name__ == '__main__':
    unittest.main()
