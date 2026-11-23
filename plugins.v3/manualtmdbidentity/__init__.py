"""Unify supported manual download identities before downloader submission."""
from copy import deepcopy
from datetime import datetime
from threading import RLock

from app.chain.media import MediaChain
from app.schemas import ResourceDownloadEventData
from app.schemas.types import ChainEventType, MediaSource, MediaType, MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, resolve_media_identity
from app.sdk.plugin import _PluginBase


class _ConversionSkipped(ValueError):
    """A safe, plugin-authored explanation for retaining the original identity."""


class ManualTmdbIdentity(_PluginBase):
    """Convert manual Douban/Bangumi downloads without altering torrent selection."""

    plugin_name = "手动下载 TMDB 身份统一"
    plugin_desc = "手动下载前统一豆瓣、Bangumi影视身份；无法确认时保留原身份并提示。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.0"
    plugin_author = "hussion"
    author_url = "https://github.com/hussion"
    plugin_config_prefix = "manualtmdbidentity_"
    plugin_order = 5
    auth_level = 1

    def init_plugin(self, config=None):
        """Reload settings without starting background resources."""
        if not hasattr(self, "_lock"):
            self._lock = RLock()
        with self._lock:
            self._enabled = bool((config or {}).get("enabled", False))
            self._notify = bool((config or {}).get("notify", True))

    def get_state(self):
        """Expose whether the event handler is enabled."""
        return getattr(self, "_enabled", False)

    def stop_service(self):
        """Prevent a conversion in flight from committing after disable."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            self._enabled = False
            return
        with lock:
            self._enabled = False

    @staticmethod
    def get_command():
        """No chat commands are needed."""
        return []

    def get_api(self):
        """Use the built-in configuration and page APIs."""
        return []

    def get_form(self):
        """Provide the small native configuration form."""
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
            {"component": "VSwitch", "props": {"model": "notify", "label": "转换失败通知"}},
            {"component": "VAlert", "props": {"type": "info", "text":
                "仅处理MP手动下载。无法可靠转换时保留原身份继续；不修改已有历史，不执行删除。复杂季集映射暂不转换。"}},
        ]}], {"enabled": False, "notify": True}

    def get_page(self):
        """Show bounded conversion outcomes without credentials."""
        with self._lock:
            records = list(self.get_data("history") or [])
        content = [{"component": "VAlert", "props": {"type": "info", "text":
            "最近100条处理结果。保留原身份的任务仍可能遇到媒体库删除身份不一致。"}}]
        for row in reversed(records):
            content.append({"component": "VAlert", "props": {
                "type": "success" if row["converted"] else "warning",
                "text": f"{row['time']} {row['title']}｜{row['original']} → {row.get('target') or '保留原身份'}｜{row['reason']}"}})
        return [{"component": "VCol", "content": content}]

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event):
        """Convert before submission; commit all fields to the captured media instance."""
        if not self.get_state() or not event or not isinstance(event.event_data, ResourceDownloadEventData):
            return
        data = event.event_data
        if data.cancel or str(data.origin or "").casefold() != "manual":
            return
        context = data.context
        original = getattr(context, "media_info", None)
        if not isinstance(original, MediaInfo) or original.type not in (MediaType.MOVIE, MediaType.TV):
            return
        source, identity = resolve_media_identity(original)
        if source not in (MediaSource.Douban, MediaSource.Bangumi) or not identity or not str(identity).isdigit():
            return
        target = None
        try:
            snapshot = deepcopy(original)
            meta = deepcopy(getattr(context, "meta_info", None))
            season, episodes = self._selection(snapshot, meta, data)
            chain = MediaChain()
            mapped = chain.convert_media_identity(target_source=MediaSource.TMDB,
                media_source=source, media_id=identity, mtype=snapshot.type, season=season)
            if not isinstance(mapped, dict) or not mapped.get("id"):
                raise _ConversionSkipped("未找到可靠的TMDB对应条目")
            projected_season = mapped.get("season")
            if snapshot.type == MediaType.TV and projected_season is not None and projected_season != season:
                raise _ConversionSkipped("转换结果季号与当前资源不一致")
            converted = chain.recognize_media(meta=meta, mtype=snapshot.type,
                media_source=MediaSource.TMDB, media_id=str(mapped["id"]), cache=False)
            if not isinstance(converted, MediaInfo) or converted.type != snapshot.type:
                raise _ConversionSkipped("TMDB完整信息或媒体类型核验失败")
            new_source, new_id = resolve_media_identity(converted)
            if new_source != MediaSource.TMDB or str(new_id) != str(mapped["id"]):
                raise _ConversionSkipped("TMDB主身份核验失败")
            converted = deepcopy(converted)
            if snapshot.type == MediaType.TV:
                available = converted.seasons.get(season)
                source_episodes = snapshot.seasons.get(season)
                if not source_episodes or not available or set(source_episodes) != set(available):
                    raise _ConversionSkipped("来源与目标季的完整集号集合无法确认一致")
                if not episodes or not set(episodes).issubset(set(available)):
                    raise _ConversionSkipped("目标季或选集无法完整核验")
                converted.season = season
            converted.scrape_source = snapshot.scrape_source
            effective = getattr(getattr(snapshot, "classification", None), "effective", None)
            if effective is not None and effective.source in {"manual", "subscription"}:
                for name in ("classification", "category", "library_category"):
                    setattr(converted, name, deepcopy(getattr(snapshot, name)))
            replacement = deepcopy(vars(converted))
            with self._lock:
                if not self._enabled or data.cancel or context.media_info is not original:
                    return
                # The host captures this instance before dispatch; replacing context alone is insufficient.
                object.__setattr__(original, "__dict__", replacement)
            target = f"themoviedb:{new_id}"
            reason = "转换成功"
        except Exception as error:
            reason = str(error) if isinstance(error, _ConversionSkipped) else f"转换暂不可用（{type(error).__name__}）"
            logger.warning(f"【{self.plugin_name}】{source.value}:{identity} 保留原身份：{reason}")
        self._record(snapshot if 'snapshot' in locals() else original, source, identity, target, reason)

    @staticmethod
    def _selection(media, meta, data):
        """Reject ambiguous seasons and selected episode shifts before any mutation."""
        if media.type == MediaType.MOVIE:
            return None, []
        if meta is None or getattr(media, "episode_group", None):
            raise _ConversionSkipped("缺少季集信息或使用自定义剧集组")
        if getattr(meta, "begin_season", None) is None:
            raise _ConversionSkipped("种子未明确标注季号")
        seasons = getattr(meta, "season_list", None) or []
        if len(set(seasons)) != 1:
            raise _ConversionSkipped("仅自动转换明确的单季资源")
        season = seasons[0]
        if not isinstance(season, int) or isinstance(season, bool) or season < 0:
            raise _ConversionSkipped("季号无效")
        if media.season is not None and media.season != season:
            raise _ConversionSkipped("来源条目与种子季号不一致")
        episodes = data.episodes or getattr(meta, "episode_list", None) or []
        if not episodes or any(not isinstance(ep, int) or isinstance(ep, bool) or ep < 0 for ep in episodes):
            raise _ConversionSkipped("缺少可核验的明确选集")
        return season, sorted(set(episodes))

    def _record(self, media, source, identity, target, reason):
        """Retain bounded outcomes; reporting errors never interrupt a download."""
        row = {"time": datetime.now().isoformat(timespec="seconds"), "title": media.title or "",
               "original": f"{source.value}:{identity}", "target": target,
               "converted": bool(target), "reason": reason}
        try:
            with self._lock:
                records = list(self.get_data("history") or [])
                self.save_data("history", (records + [row])[-100:])
            if not target and self._notify:
                self.post_message(mtype=MessageType.Plugin, title="手动下载保留原身份",
                    text=f"{row['title']}｜{row['original']}\n{reason}。下载继续，后续自动删除可能仍需核对。")
        except Exception as error:
            logger.warning(f"【{self.plugin_name}】记录结果失败：{type(error).__name__}")
