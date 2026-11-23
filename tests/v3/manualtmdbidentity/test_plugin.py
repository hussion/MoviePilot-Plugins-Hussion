"""Run with the MoviePilot host interpreter and normal plugin namespace."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import app.plugins.manualtmdbidentity as module
from app.schemas import ResourceDownloadEventData
from app.schemas.types import MediaSource, MediaType
from app.sdk.media import MediaInfo


class IdentityTests(TestCase):
    def setUp(self):
        remover = patch.object(module.scheduler_sdk, "remove_plugin_once_job", return_value=True)
        remover.start()
        self.addCleanup(remover.stop)
        self.plugin = object.__new__(module.ManualTmdbIdentity)
        self.plugin.init_plugin({"enabled": True, "notify": True})
        self.records = []
        self.plugin.get_data = lambda key: self.records
        self.plugin.save_data = lambda key, rows: setattr(self, "records", rows)
        self.messages = []
        self.plugin.post_message = lambda **kwargs: self.messages.append(kwargs)
        self.original = MediaInfo(media_source=MediaSource.Douban, media_id="36847425", type=MediaType.MOVIE, title="可能的爱")
        self.target = MediaInfo(media_source=MediaSource.TMDB, media_id="1483525", type=MediaType.MOVIE, title="可能的爱", tmdb_id=1483525)
        self.meta = SimpleNamespace(begin_season=1, season_list=[1], episode_list=[1, 2])
        self.context = SimpleNamespace(media_info=self.original, meta_info=self.meta, torrent_info=object())
        self.data = ResourceDownloadEventData(context=self.context, origin="Manual", episodes={1, 2})
        self.event = SimpleNamespace(event_data=self.data)

    def run_conversion(self, mapped=None, target=None):
        chain = SimpleNamespace(convert_media_identity=lambda **kw: mapped or {"id": 1483525}, recognize_media=lambda **kw: target or self.target)
        with patch.object(module, "MediaChain", return_value=chain):
            self.plugin.on_resource_download(self.event)

    def test_movie_preserves_captured_instance_and_detaches_target(self):
        identity = id(self.original)
        target_before = deepcopy(vars(self.target))
        self.run_conversion()
        self.assertEqual(id(self.context.media_info), identity)
        self.assertEqual(self.original.media_source, MediaSource.TMDB)
        self.assertEqual(self.original.media_id, "1483525")
        self.assertEqual(vars(self.target), target_before)
        self.assertEqual(self.records[-1]["target"], "themoviedb:1483525")
        self.assertFalse(self.messages)

    def test_failure_leaves_every_media_field_unchanged(self):
        before = deepcopy(vars(self.original))
        with patch.object(module, "MediaChain", side_effect=OSError("private endpoint")):
            self.plugin.on_resource_download(self.event)
        self.assertEqual(vars(self.original), before)
        self.assertEqual(len(self.messages), 1)
        self.assertNotIn("private endpoint", str(self.messages))

    def test_cancelled_and_subscription_do_not_convert(self):
        with patch.object(module, "MediaChain") as chain:
            self.data.cancel = True
            self.plugin.on_resource_download(self.event)
            self.data.cancel = False
            self.data.origin = "Subscribe"
            self.plugin.on_resource_download(self.event)
            chain.assert_not_called()

    def test_series_validates_season_and_selected_episodes(self):
        self.original.type = self.target.type = MediaType.TV
        self.original.season = 1
        self.target.seasons = {1: [1, 2, 3]}
        self.original.seasons = {1: [1, 2, 3]}
        before_meta = deepcopy(vars(self.meta))
        self.run_conversion(mapped={"id": 1483525, "season": 1})
        self.assertEqual(self.original.media_source, MediaSource.TMDB)
        self.assertEqual(vars(self.meta), before_meta)
        self.assertEqual(self.data.episodes, {1, 2})

    def test_shifted_season_keeps_original(self):
        self.original.type = self.target.type = MediaType.TV
        self.original.season = 1
        self.target.seasons = {2: [1, 2]}
        before = deepcopy(vars(self.original))
        self.run_conversion(mapped={"id": 1483525, "season": 2})
        self.assertEqual(vars(self.original), before)
        self.assertFalse(self.records[-1]["converted"])

    def test_disable_while_lookup_in_flight_prevents_commit(self):
        chain = SimpleNamespace(convert_media_identity=lambda **kw: {"id": 1483525}, recognize_media=lambda **kw: (self.plugin.stop_service() or self.target))
        before = deepcopy(vars(self.original))
        with patch.object(module, "MediaChain", return_value=chain):
            self.plugin.on_resource_download(self.event)
        self.assertEqual(vars(self.original), before)

    def test_history_bounded(self):
        self.records = [{"old": i} for i in range(100)]
        self.run_conversion()
        self.assertEqual(len(self.records), 100)
        self.assertTrue(self.records[-1]["converted"])

    def test_missing_explicit_season_keeps_original(self):
        self.original.type = MediaType.TV
        self.meta.begin_season = None
        with patch.object(module, "MediaChain") as chain:
            self.plugin.on_resource_download(self.event)
            chain.assert_not_called()
        self.assertEqual(self.original.media_source, MediaSource.Douban)

    def test_bangumi_movie_uses_same_conversion(self):
        self.original.media_source = MediaSource.Bangumi
        self.original.media_id = "12345"
        self.run_conversion()
        self.assertEqual(self.original.media_source, MediaSource.TMDB)
        self.assertEqual(self.records[-1]["original"], "bangumi:12345")

    def test_stop_before_init_is_safe(self):
        plugin = object.__new__(module.ManualTmdbIdentity)
        plugin.stop_service()
        self.assertFalse(plugin.get_state())

    def test_split_cour_episode_sets_keep_original(self):
        self.original.type = self.target.type = MediaType.TV
        self.original.season = 1
        self.original.seasons = {1: list(range(1, 13))}
        self.target.seasons = {1: list(range(1, 25))}
        before = deepcopy(vars(self.original))
        self.run_conversion(mapped={"id": 1483525, "season": 1})
        self.assertEqual(vars(self.original), before)

    def test_actual_host_prepare_uses_converted_copy_without_polluting_input(self):
        import ast
        import inspect
        import textwrap
        from pathlib import Path
        import app.chain.download.submission as host
        source = inspect.getsource(host.DownloadSubmissionOwner._prepare_download_single)
        tree = ast.parse(textwrap.dedent(source))
        tree.body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
        namespace = dict(vars(host))
        namespace["MediaChain"] = lambda: SimpleNamespace(supplement_tmdb_info=lambda media, meta: deepcopy(media))
        exec(compile(ast.fix_missing_locations(tree), "actual-host-prepare", "exec"), namespace)
        original = self.original
        self.context.torrent_info = SimpleNamespace(site_downloader=None)
        self.context.meta_info = SimpleNamespace(episode_list=[])
        def event_hook(context, *args):
            event = SimpleNamespace(event_data=ResourceDownloadEventData(context=context, origin="Manual"))
            self.plugin.on_resource_download(event)
            return None, None
        owner = SimpleNamespace(_apply_resource_download_event=event_hook,
            _record_music_album_track_keys=lambda *args: None,
            _resolve_media_download_dir=lambda **kwargs: ("local", Path("/test-download"), None))
        helper = SimpleNamespace(get_fileinfo_from_torrent_content=lambda content: ("", ["movie.mkv"]))
        chain = SimpleNamespace(convert_media_identity=lambda **kw: {"id": 1483525}, recognize_media=lambda **kw: self.target)
        with patch.object(module, "MediaChain", return_value=chain):
            namespace["TorrentHelper"] = lambda: helper
            prepared, error = namespace["_prepare_download_single"](owner, context=self.context,
                torrent_file=None, torrent_content=b"synthetic-torrent", episodes=None, channel=None,
                source="Manual", downloader="QB", save_path=None, userid=None, username=None)
        self.assertIsNone(error)
        self.assertEqual(prepared.media.media_source, MediaSource.TMDB)
        self.assertEqual(prepared.media.media_id, "1483525")
        self.assertIs(prepared.media, self.context.media_info)
        self.assertIsNot(prepared.media, original)
        self.assertEqual(original.media_source, MediaSource.Douban)
        self.assertEqual(original.media_id, "36847425")
