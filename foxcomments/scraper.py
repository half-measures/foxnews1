"""Turn raw hedgehog API pages into a clean comment tree."""

import logging
from datetime import datetime, timezone

from .client import FoxCommentsClient

log = logging.getLogger(__name__)


def _index_objects(page: dict, users: dict, comments: dict) -> None:
    for obj in page.get("objects", []):
        if obj.get("typename") == "User":
            users[obj["id"]] = obj
        elif obj.get("typename") == "Comment":
            comments[obj["id"]] = obj


def _format_comment(raw: dict, users: dict, include_raw: bool = True) -> dict:
    user = users.get(raw.get("creator"), {})
    formatted = {
        "id": raw["id"],
        "body": raw.get("body"),
        "created": raw.get("created"),
        "updated": raw.get("updated"),
        "edited": raw.get("edited"),
        "deleted": raw.get("deleted"),
        "pinned": raw.get("pinned"),
        "score": raw.get("score"),
        "parent_comment_id": raw.get("parentComment"),
        "author": {
            "id": raw.get("creator"),
            "username": user.get("username"),
            "display_name": user.get("name"),
        },
        "images": raw.get("images") or [],
        "videos": raw.get("videos") or [],
    }
    if include_raw:
        # Keep the whole API object: each article is scraped once, so a field dropped
        # here is gone for good.
        formatted["raw"] = raw
    return formatted


def _listed_ids(page: dict, raw_comments: dict) -> list[str]:
    """Comment ids from a page's list, skipping any whose object wasn't included."""
    ids = []
    for item in page.get("list2", []):
        if item.get("typename") != "Comment":
            continue
        if item["id"] in raw_comments:
            ids.append(item["id"])
        else:
            log.warning("Comment %s listed but not returned by the API; skipping", item["id"])
    return ids


def scrape_article(
    client: FoxCommentsClient,
    url_or_id: str,
    include_replies: bool = True,
    include_reactions: bool = True,
    include_raw: bool = True,
    max_pages: int | None = None,
) -> dict:
    article = client.resolve_article(url_or_id)
    log.info("Article comment id: %s", article["id"])

    users: dict = {}
    raw_comments: dict = {}
    top_ids: list[str] = []

    for n, page in enumerate(client.comment_pages(article["id"], max_pages=max_pages), 1):
        _index_objects(page, users, raw_comments)
        top_ids.extend(_listed_ids(page, raw_comments))
        log.info("Comment page %d: %d top-level comments so far", n, len(top_ids))

    top_level = []
    all_ids = list(top_ids)
    for i, cid in enumerate(top_ids, 1):
        comment = _format_comment(raw_comments[cid], users, include_raw)
        comment["replies"] = []
        if include_replies:
            reply_ids = []
            for page in client.reply_pages(cid):
                _index_objects(page, users, raw_comments)
                reply_ids.extend(_listed_ids(page, raw_comments))
            comment["replies"] = [_format_comment(raw_comments[rid], users, include_raw) for rid in reply_ids]
            all_ids.extend(reply_ids)
            if i % 10 == 0 or i == len(top_ids):
                log.info("Fetched replies for %d/%d comments", i, len(top_ids))
        top_level.append(comment)

    if include_reactions and all_ids:
        metrics = client.reaction_metrics(all_ids)
        for comment in top_level:
            for c in [comment, *comment["replies"]]:
                m = metrics.get(c["id"], {})
                c["reaction_total"] = m.get("total", 0)
                c["reactions"] = m.get("reactions", {})

    return {
        "article": article,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "top_level_comments": len(top_level),
            "replies": sum(len(c["replies"]) for c in top_level),
        },
        "comments": top_level,
    }
