"""Read-only scanning and lifecycle contracts, using actual SDK identity types."""
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from app.schemas.types import MediaSource
import app.plugins.manualtmdbidentity as module
from app.plugins.manualtmdbidentity.preview import scan_history


def history(id=1, source=MediaSource.Douban):
    return NS(id=id, title="测试电影", media_source=source, media_id="123", type="电影", download_hash="hash")


def transfer():
    return NS(media_source=MediaSource.Douban, media_id="123", status=True, src="/download/a.mkv", dest="/link/a.mkv")


class PreviewTests(TestCase):
    def scan(self, rows, transfers=None, exists=lambda path: True):
        return scan_history(lambda page, count: rows if page == 1 else [], lambda hash_: transfers if transfers is not None else [transfer()],
                            lambda row: "themoviedb:456", exists, lambda: False)

    def test_candidate_does_not_mutate_host_records(self):
        row, linked = history(), transfer()
        before = (vars(row).copy(), vars(linked).copy())
        report = self.scan([row], [linked])
        self.assertEqual((vars(row), vars(linked)), before)
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["details"][0]["qb"], "待核验")
        self.assertIn("仅预览", report["details"][0]["reason"])

    def test_pagination_reads_all_rows_with_page_size_100(self):
        requests = []
        def page(number, size):
            requests.append((number, size))
            return [history(id=(number-1)*100+i, source=MediaSource.TMDB) for i in range(100 if number == 1 else 2)] if number < 3 else []
        report = scan_history(page, lambda h: [], lambda row: None, lambda path: True, lambda: False)
        self.assertEqual(report["total"], 102)
        self.assertEqual(report["ignored"], 102)
        self.assertEqual(requests, [(1, 100), (2, 100)])

    def test_deleted_library_file_is_skipped_before_conversion(self):
        report = self.scan([history()], exists=lambda path: path.startswith("/download/"))
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["candidates"], 0)
        self.assertIn("缺失", report["details"][0]["reason"])

    def test_mismatched_history_identity_is_skipped(self):
        linked = transfer()
        linked.media_id = "999"
        report = self.scan([history()], [linked])
        self.assertEqual(report["skipped"], 1)
        self.assertIn("不一致", report["details"][0]["reason"])

    def test_missing_transfer_is_not_a_candidate(self):
        self.assertEqual(self.scan([history()], [])['skipped'], 1)

    def test_one_row_failure_does_not_stop_rest(self):
        def convert(row):
            if row.id == 1:
                raise OSError("private endpoint")
            return "themoviedb:456"
        report = scan_history(lambda page, count: [history(1), history(2)] if page == 1 else [],
                              lambda h: [transfer()], convert, lambda path: True, lambda: False)
        self.assertEqual((report['failed'], report['candidates']), (1, 1))
        self.assertNotIn("private endpoint", str(report))

    def plugin(self):
        p = object.__new__(module.ManualTmdbIdentity)
        p.update_config = lambda config: setattr(self, "saved_config", config)
        p.save_data = lambda key, value: setattr(self, "report", value)
        p.get_data = lambda key: None
        return p

    def test_scan_once_resets_even_when_scheduling_fails_and_disabled(self):
        p = self.plugin()
        with patch.object(module.scheduler_sdk, 'add_plugin_once_job', return_value=False):
            p.init_plugin({'enabled': False, 'scan_once': True})
        self.assertFalse(self.saved_config['scan_once'])
        self.assertIn('调度器不可用', self.report['status'])

    def test_reload_cancels_old_generation_and_pending_job(self):
        p = self.plugin()
        p.init_plugin({'enabled': False})
        old = p._generation
        with patch.object(module.scheduler_sdk, 'remove_plugin_once_job') as remove:
            p.init_plugin({'enabled': False})
        remove.assert_called_once()
        with patch.object(module, 'scan_history') as scan:
            p._run_history_scan(old)
        scan.assert_not_called()

    def test_running_scan_is_single_flight(self):
        p = self.plugin()
        p.init_plugin({'enabled': False})
        p._scan_running = True
        with patch.object(module, 'scan_history') as scan:
            p._run_history_scan(p._generation)
        scan.assert_not_called()

    def test_read_failure_is_recorded_and_running_flag_released(self):
        p = self.plugin()
        p.init_plugin({'enabled': False})
        with patch.object(module, 'DownloadHistoryOper', side_effect=OSError('credentials')):
            p._run_history_scan(p._generation)
        self.assertIn('扫描失败', self.report['status'])
        self.assertFalse(p._scan_running)
        self.assertNotIn('credentials', str(self.report))

    def test_incomplete_qb_task_is_skipped(self):
        report = scan_history(lambda page, count: [history()] if page == 1 else [], lambda h: [transfer()],
                              lambda row: "themoviedb:456", lambda path: True, lambda: False,
                              lambda row: "未完成")
        self.assertEqual(report['candidates'], 0)
        self.assertEqual(report['details'][0]['qb'], '未完成')

    def test_qb_progress_uses_percent_not_fraction(self):
        chain = NS(list_torrents=lambda **kwargs: [NS(selected_completed=None, progress=1)])
        with patch.object(module, 'DownloadChain', return_value=chain):
            self.assertEqual(module.ManualTmdbIdentity._preview_qb(history()), '未完成')
        chain.list_torrents = lambda **kwargs: [NS(selected_completed=None, progress=100)]
        with patch.object(module, 'DownloadChain', return_value=chain):
            self.assertEqual(module.ManualTmdbIdentity._preview_qb(history()), '已完成')

    def test_qb_multiple_instances_do_not_claim_completed(self):
        chain = NS(list_torrents=lambda **kwargs: [NS(progress=100), NS(progress=100)])
        with patch.object(module, 'DownloadChain', return_value=chain):
            self.assertIn('待核验', module.ManualTmdbIdentity._preview_qb(history()))

    def test_real_sdk_scheduler_signature_and_bound_callback(self):
        import inspect
        p = self.plugin()
        inspect.signature(module.scheduler_sdk.add_plugin_once_job).bind(
            p.__class__.__name__, 'history_preview', p._run_history_scan, '历史身份只读预览', delay_seconds=3)
        with patch.object(module.scheduler_sdk, 'add_plugin_once_job', return_value=True) as add:
            p.init_plugin({'enabled': False, 'scan_once': True})
        self.assertIs(add.call_args.args[2].__self__, p)
        self.assertFalse(self.saved_config['scan_once'])

    def test_stop_marks_running_preview_cancelled(self):
        p = self.plugin()
        p.init_plugin({'enabled': False})
        p._scan_running = True
        with patch.object(module.scheduler_sdk, 'remove_plugin_once_job'):
            p.stop_service()
        self.assertIn('已取消', self.report['status'])
