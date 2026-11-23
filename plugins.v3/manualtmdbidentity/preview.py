"""Read-only history scanning; all host access is supplied as read callbacks."""
from app.schemas.types import MediaSource, MediaType
from app.sdk.media import resolve_media_identity


def scan_history(read_page, read_transfers, convert, exists, cancelled, check_downloader=None):
    """Return bounded, conservative conversion previews without mutating history."""
    report = {"status": "完成（仅预览）", "total": 0, "candidates": 0, "skipped": 0, "failed": 0, "ignored": 0,
              "details": [], "details_limit": 500, "truncated": False}
    seen = set()
    page = 1
    while not cancelled():
        rows = read_page(page, 100)
        if not rows:
            break
        fresh = 0
        for row in rows:
            if cancelled():
                report["status"] = "已取消"
                return report
            key = row.id
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            report["total"] += 1
            source, identity = resolve_media_identity(row)
            if source not in (MediaSource.Douban, MediaSource.Bangumi) or not identity:
                report["ignored"] += 1
                continue
            item = {"id": key, "title": row.title or "", "original": f"{source.value}:{identity}",
                    "target": None, "result": "跳过", "reason": "", "qb": "待核验", "emby": "待核验"}
            try:
                if row.type not in (MediaType.MOVIE.value, MediaType.TV.value):
                    raise PreviewSkipped("非电影或电视剧记录")
                if not row.download_hash:
                    raise PreviewSkipped("缺少下载任务hash")
                transfers = read_transfers(row.download_hash)
                if not transfers:
                    raise PreviewSkipped("没有关联整理记录，可能尚未整理或已清理")
                for transfer in transfers:
                    if not transfer.status:
                        raise PreviewSkipped("关联整理记录未成功")
                    if resolve_media_identity(transfer) != (source, identity):
                        raise PreviewSkipped("下载与整理记录身份不一致")
                    if not transfer.src or not transfer.dest or not exists(transfer.src) or not exists(transfer.dest):
                        raise PreviewSkipped("源文件或媒体库文件缺失/已删除")
                if check_downloader is not None:
                    item["qb"] = check_downloader(row)
                    if item["qb"] != "已完成":
                        raise PreviewSkipped("下载器核验未通过：" + item["qb"])
                item["target"] = convert(row)
                item["result"] = "可转换（待外部核验）"
                item["reason"] = "仅预览；Emby存在状态待核验，当前不提供历史写入"
                report["candidates"] += 1
            except PreviewSkipped as error:
                item["reason"] = str(error)
                report["skipped"] += 1
            except Exception as error:
                item["result"] = "失败"
                item["reason"] = f"读取或转换失败（{type(error).__name__}）"
                report["failed"] += 1
            if len(report["details"]) < report["details_limit"]:
                report["details"].append(item)
            else:
                report["truncated"] = True
        if len(rows) < 100 or not fresh:
            break
        page += 1
    if cancelled():
        report["status"] = "已取消"
    return report


class PreviewSkipped(ValueError):
    """A safe explanation for an ineligible history entry."""
