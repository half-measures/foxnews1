# foxcomments

Scrapes the comments section of a Fox News article to JSON.

Fox News comments run on Fox's own "hedgehog" platform (`api.community.fox.com`). No login is needed to read them.

## Setup

```
pip install -r requirements.txt
```

## Usage

```
python -m foxcomments "https://www.foxnews.com/politics/some-article-slug"
```

Output goes to `output/<slug>.json` by default.

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `output/<slug>.json` | Output path |
| `--rate` | `1.0` | Max requests per second |
| `--burst` | `1` | Token-bucket burst size |
| `--max-pages` | all | Limit top-level comment pages (20 comments each) |
| `--no-replies` | | Skip replies (one extra request per top-level comment) |
| `--no-reactions` | | Skip Agree/Disagree counts |
| `-v` | | Debug logging |

You can also pass the comment-embed UUID directly instead of a URL.

## How it works

1. Loads the article HTML and reads the id from `<hedgehog-comment-embed id="...">`.
2. `GET /v2/post/0/{articleId}/topics/tree/all/{cursor}`: top-level comments, newest first, 20 per page.
3. `GET /v2/topics/0/{commentId}/replies/all/{offset}`: replies to each comment.
4. `POST /v3/0/reaction/metrics` `{"value": [ids]}`: reaction counts, in batches of 50.

Every request goes through a token-bucket `RateLimiter`. A 429 or 5xx response triggers a backoff, which uses `Retry-After` if the server sends it. Otherwise the wait grows exponentially, and the bucket is drained so later requests slow down too.

## Output shape

```json
{
  "article": {"id": "...", "url": "...", "title": "..."},
  "scraped_at": "2026-09-16T23:40:00+00:00",
  "counts": {"top_level_comments": 354, "replies": 613},
  "comments": [
    {
      "id": "...", "body": "...", "created": "...", "updated": "...",
      "edited": false, "deleted": false, "pinned": false, "score": 0,
      "parent_comment_id": null,
      "author": {"id": "...", "username": "...", "display_name": "..."},
      "images": [], "videos": [],
      "reaction_total": 8, "reactions": {"Agree": 5, "Disagree": 3},
      "replies": [ { "...same fields, parent_comment_id set..." } ]
    }
  ]
}
```
