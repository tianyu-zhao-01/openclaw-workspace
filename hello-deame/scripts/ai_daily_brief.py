#!/usr/bin/env python3
"""
每日 AI 简报：近 24h 热点（HN + RSS）+ GitHub 高星 AI 仓库（默认 ★>10k，近 24h 内有推送）。
飞书 / 企业微信 / Server酱 Webhook 推送。

环境变量见同目录 config.example.env（复制为 .env）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

RSS_FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
]

HN_QUERY = "AI OR LLM OR OpenAI OR Claude OR Gemini OR machine learning"

GITHUB_AI_TOPICS = (
    "machine-learning",
    "llm",
    "deep-learning",
    "artificial-intelligence",
)


@dataclass
class Item:
    title: str
    url: str
    source: str


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ai-daily-brief/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def _http_get_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ai-daily-brief/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode(errors="replace")


def since_unix_24h() -> int:
    return int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())


def fetch_hn_stories() -> list[Item]:
    since = since_unix_24h()
    q = urllib.parse.quote(HN_QUERY)
    url = (
        f"https://hn.algolia.com/api/v1/search?tags=story&numericFilters=created_at_i>{since}"
        f"&query={q}&hitsPerPage=15"
    )
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"[warn] HN: {e}", file=sys.stderr)
        return []
    out: list[Item] = []
    for hit in data.get("hits") or []:
        title = hit.get("title") or hit.get("story_title") or ""
        u = hit.get("url") or ""
        if not title:
            continue
        if not u:
            oid = hit.get("objectID") or hit.get("story_id")
            if oid:
                u = f"https://news.ycombinator.com/item?id={oid}"
        out.append(Item(title=title.strip(), url=u, source="Hacker News"))
    return out


def _parse_rss_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def fetch_rss_items() -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out: list[Item] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for name, feed_url in RSS_FEEDS:
        try:
            xml_text = _http_get_text(feed_url)
        except urllib.error.URLError as e:
            print(f"[warn] RSS {name}: {e}", file=sys.stderr)
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            print(f"[warn] RSS parse {name}: {e}", file=sys.stderr)
            continue
        channel = root.find("channel")
        if channel is not None:
            entries = channel.findall("item")
        else:
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for el in entries[:20]:
            if root.tag.endswith("feed"):
                title_el = el.find("atom:title", ns)
                link_el = el.find("atom:link", ns)
                updated_el = el.find("atom:updated", ns)
                title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
                url = ""
                if link_el is not None:
                    url = link_el.get("href") or ""
                pub = _parse_rss_date(updated_el.text if updated_el is not None else None)
            else:
                t = el.find("title")
                l = el.find("link")
                p = el.find("pubDate")
                title = (t.text or "").strip() if t is not None and t.text else ""
                url = (l.text or "").strip() if l is not None and l.text else ""
                pub = _parse_rss_date(p.text if p is not None else None)
            if not title:
                continue
            if pub is not None:
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub < cutoff:
                    continue
            out.append(Item(title=title, url=url, source=name))
    return out


def github_date_str() -> str:
    d = (datetime.now(timezone.utc) - timedelta(hours=24)).date()
    return d.isoformat()


def github_min_stars() -> int:
    raw = os.environ.get("GITHUB_MIN_STARS", "10000").strip()
    try:
        n = int(raw)
        return max(1000, n)
    except ValueError:
        return 10000


def github_topic_list() -> tuple[str, ...]:
    raw = os.environ.get("GITHUB_AI_TOPICS", "").strip()
    if raw:
        parts = tuple(t.strip() for t in raw.split(",") if t.strip())
        if parts:
            return parts
    return GITHUB_AI_TOPICS


def _repos_to_items(repos: list[dict[str, Any]]) -> list[Item]:
    out: list[Item] = []
    for repo in repos:
        name = repo.get("full_name") or ""
        html = repo.get("html_url") or ""
        desc = (repo.get("description") or "").strip()
        stars = repo.get("stargazers_count", 0)
        lang = repo.get("language") or ""
        line = f"{name} ★{stars}"
        if lang:
            line += f" [{lang}]"
        if desc:
            line += f" — {desc[:120]}{'…' if len(desc) > 120 else ''}"
        out.append(Item(title=line, url=html, source="GitHub 高星"))
    return out


def fetch_github_repos() -> list[Item]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    date_s = github_date_str()
    min_s = github_min_stars()
    headers = {
        "User-Agent": "ai-daily-brief",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    by_name: dict[str, dict[str, Any]] = {}
    for topic in github_topic_list():
        q = f"topic:{topic} stars:>{min_s} pushed:>{date_s}"
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": "15"}
        )
        try:
            data = _http_get_json(url, headers=headers)
        except urllib.error.HTTPError as e:
            print(f"[warn] GitHub topic={topic} HTTP {e.code}", file=sys.stderr)
            continue
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"[warn] GitHub topic={topic}: {e}", file=sys.stderr)
            continue
        for repo in data.get("items") or []:
            fn = repo.get("full_name") or ""
            if not fn:
                continue
            if repo.get("stargazers_count", 0) < min_s:
                continue
            prev = by_name.get(fn)
            if prev is None or repo.get("stargazers_count", 0) >= prev.get(
                "stargazers_count", 0
            ):
                by_name[fn] = repo

    merged = sorted(
        by_name.values(),
        key=lambda r: r.get("stargazers_count", 0),
        reverse=True,
    )[:15]
    return _repos_to_items(merged)


def build_digest_items(hn: list[Item], rss: list[Item], gh: list[Item]) -> str:
    lines = [
        f"📅 AI 日报 {datetime.now().strftime('%Y-%m-%d %H:%M')}（近 24 小时）",
        "",
        "【热点 / 资讯】",
    ]
    seen: set[str] = set()
    for it in hn[:8]:
        key = it.title.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• [{it.source}] {it.title}")
        if it.url:
            lines.append(f"  {it.url}")
    for it in rss[:8]:
        key = it.title.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• [{it.source}] {it.title}")
        if it.url:
            lines.append(f"  {it.url}")
    lines.append("")
    lines.append(
        f"【GitHub 明星 AI 项目（★>{github_min_stars()}，近 24h 内有推送，按星标排序）】"
    )
    if not gh:
        lines.append("• （无结果或 API 受限，可配置 GITHUB_TOKEN）")
    for it in gh[:12]:
        lines.append(f"• {it.title}")
        if it.url:
            lines.append(f"  {it.url}")
    return "\n".join(lines)


def llm_summarize_zh(digest: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是科技编辑。根据下列素材用简体中文写两段：第一段概括过去24小时AI领域动态；"
                    "第二段简要介绍GitHub高星AI项目。总字数400字内。"
                ),
            },
            {"role": "user", "content": digest[:12000]},
        ],
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[warn] LLM 总结失败: {e}", file=sys.stderr)
        return None


def send_feishu(webhook: str, text: str) -> None:
    payload = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    data = json.loads(raw) if raw else {}
    if "code" in data and data["code"] != 0:
        raise RuntimeError(f"飞书返回: {data}")
    if "StatusCode" in data and data["StatusCode"] != 0:
        raise RuntimeError(f"飞书返回: {data}")


def send_wecom(webhook: str, text: str) -> None:
    payload = {"msgtype": "text", "text": {"content": text}}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return
    if data.get("errcode") not in (0, None):
        raise RuntimeError(f"企业微信返回: {data}")


def load_dotenv_file(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


def main() -> int:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv_file(os.path.join(script_dir, ".env"))

    hn = fetch_hn_stories()
    rss = fetch_rss_items()
    gh = fetch_github_repos()
    digest = build_digest_items(hn, rss, gh)

    summary = llm_summarize_zh(digest)
    if summary:
        full_text = "━━ 综述 ━━\n" + summary + "\n\n━━ 明细 ━━\n" + digest
    else:
        full_text = digest

    feishu_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    wechat_url = os.environ.get("WECHAT_WEBHOOK_URL", "").strip()

    if not feishu_url and not wechat_url:
        print(full_text)
        print(
            "\n[提示] 未设置 FEISHU_WEBHOOK_URL / WECHAT_WEBHOOK_URL，仅打印到 stdout。",
            file=sys.stderr,
        )
        return 0

    errors: list[str] = []
    if feishu_url:
        try:
            send_feishu(feishu_url, full_text)
        except Exception as e:
            errors.append(f"飞书: {e}")
    if wechat_url:
        try:
            if "sctapi.ftqq.com" in wechat_url or "sc.ftqq.com" in wechat_url:
                form = urllib.parse.urlencode(
                    {"title": "AI 日报", "desp": full_text.replace("\n", "\n\n")}
                ).encode()
                req = urllib.request.Request(
                    wechat_url,
                    data=form,
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                urllib.request.urlopen(req, timeout=30).read()
            else:
                send_wecom(wechat_url, full_text)
        except Exception as e:
            errors.append(f"微信通道: {e}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("已发送。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
