#!/usr/bin/env python3
"""
每日 AI 简报：近 24h 热点（HN + RSS）+ GitHub 高星 AI 仓库（默认 ★>10k，近 24h 内有推送）。
飞书 / 企业微信 / Server酱 Webhook 推送。

流程（择一）：
• 配置 OPENAI_API_KEY：LLM 译标题 + 生成 GitHub 百字摘要。
• 或配置腾讯密钥：TENCENT_SECRET_ID + TENCENT_SECRET_KEY（亦支持 TENCENT_TRANSLATE_SECRET_ID / KEY），需 pip 安装 tencentcloud-sdk-python；区域可用 TENCENT_TMT_REGION 或 TENCENT_TRANSLATE_REGION。
未配置时：界面中文、正文多为英文；可仅用 OPENAI 做整篇第二步（BRIEF_LLM_MODE）。

环境变量见 config.example.env；腾讯依赖见 requirements-tmt.txt。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
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

# 输出里「来源」显示名（中文为主，必要处保留西文专名）
SOURCE_LABEL_ZH: dict[str, str] = {
    "Hacker News": "黑客新闻",
    "TechCrunch AI": "TechCrunch",
    "MIT Tech Review AI": "麻省理工科技评论",
    "GitHub 高星": "GitHub 高星仓库",
}


@dataclass
class Item:
    title: str
    url: str
    source: str
    # 以下仅 GitHub 条目使用（资讯类留空）
    repo_full_name: str = ""
    repo_stars: int = 0
    repo_language: str = ""


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


def _truncate_zh(s: str, max_chars: int = 100) -> str:
    s = s.replace("\n", " ").replace("\r", "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def _parse_llm_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    return json.loads(t)


def _merge_github_repos() -> list[dict[str, Any]]:
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

    return sorted(
        by_name.values(),
        key=lambda r: r.get("stargazers_count", 0),
        reverse=True,
    )[:15]


def _github_repos_to_items_fallback_en(repos: list[dict[str, Any]]) -> list[Item]:
    """无 API 时的 GitHub 行：英文索引 + 中文提示。"""
    out: list[Item] = []
    for repo in repos:
        name = repo.get("full_name") or ""
        html = repo.get("html_url") or ""
        desc = (repo.get("description") or "").strip()
        stars = int(repo.get("stargazers_count") or 0)
        lang = repo.get("language") or ""
        line = f"{name} ★{stars}"
        if lang:
            line += f" [{lang}]"
        if desc:
            line += f" — {desc[:120]}{'…' if len(desc) > 120 else ''}"
        out.append(
            Item(
                title=_github_fallback_summary_hint() + "\n" + line,
                url=html,
                source="GitHub 高星",
                repo_full_name=name,
                repo_stars=stars,
                repo_language=lang or "—",
            )
        )
    return out


def _github_repos_to_items_with_summaries(
    repos: list[dict[str, Any]], summaries: dict[str, str]
) -> list[Item]:
    out: list[Item] = []
    for repo in repos:
        name = repo.get("full_name") or ""
        html = repo.get("html_url") or ""
        stars = int(repo.get("stargazers_count") or 0)
        lang = repo.get("language") or "—"
        raw_sum = (summaries.get(name) or "").strip() or "（模型未返回该仓库摘要）"
        summary = _truncate_zh(raw_sum, 100)
        out.append(
            Item(
                title=summary,
                url=html,
                source="GitHub 高星",
                repo_full_name=name,
                repo_stars=stars,
                repo_language=lang,
            )
        )
    return out


def fetch_github_repos() -> list[Item]:
    return _github_repos_to_items_fallback_en(_merge_github_repos())


def _github_fallback_summary_hint() -> str:
    """说明为何没有百字中文摘要（避免只写 OPENAI 造成误解）。"""
    has_oai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    has_sid = bool(_env_tencent_secret_id())
    has_sk = bool(_env_tencent_secret_key())
    sdk_ok = _tencent_sdk_import() is not None
    parts = [
        "（未能生成百字内中文摘要：",
    ]
    if not has_oai and not (has_sid and has_sk and sdk_ok):
        parts.append(
            "请配置 OPENAI_API_KEY，或配置 TENCENT_SECRET_ID+KEY 并用已安装 tencentcloud-sdk-python 的 Python 运行（如 ./tencent-env/bin/python）；"
        )
    elif has_oai and not (has_sid and has_sk and sdk_ok):
        parts.append("已配置 OpenAI 但本次未得到摘要；若需机翻可配置腾讯密钥+SDK 或设置 BRIEF_TRANSLATE_BACKEND=tencent；")
    elif has_sid and has_sk and not sdk_ok:
        parts.append("已配置腾讯密钥但当前 Python 未安装 SDK，请 pip install tencentcloud-sdk-python 或使用项目 venv/tencent-env；")
    else:
        parts.append("翻译链路未生效，请检查 .env 与 BRIEF_TRANSLATE_BACKEND；")
    parts.append("以下为英文索引）")
    return "".join(parts)


def _resolve_translate_backend() -> str:
    """
    返回 openai | tencent | none。
    BRIEF_TRANSLATE_BACKEND=openai|tencent|auto（默认 auto：有 OpenAI 则优先 OpenAI，否则腾讯）
    """
    use_openai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    use_tmt = tencent_translate_configured()
    b = os.environ.get("BRIEF_TRANSLATE_BACKEND", "auto").strip().lower()
    if b == "tencent":
        if use_tmt:
            return "tencent"
        if use_openai:
            print(
                "[warn] BRIEF_TRANSLATE_BACKEND=tencent 但腾讯不可用，回退 OpenAI。",
                file=sys.stderr,
            )
            return "openai"
        return "none"
    if b == "openai":
        if use_openai:
            return "openai"
        if use_tmt:
            print(
                "[warn] BRIEF_TRANSLATE_BACKEND=openai 但 OpenAI 未配置，回退腾讯翻译。",
                file=sys.stderr,
            )
            return "tencent"
        return "none"
    if use_openai:
        return "openai"
    if use_tmt:
        return "tencent"
    return "none"


def _source_label(source: str) -> str:
    return SOURCE_LABEL_ZH.get(source, source)


def build_digest_items(hn: list[Item], rss: list[Item], gh: list[Item]) -> str:
    """界面文案全中文。资讯标题、GitHub 摘要是否中文取决于是否调用 LLM。"""
    lines = [
        f"📅 人工智能日报 {datetime.now().strftime('%Y-%m-%d %H:%M')}（近二十四小时）",
        "",
        "【热点与资讯】",
    ]
    seen: set[str] = set()
    for it in hn[:8]:
        key = it.title.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• 来源：{_source_label(it.source)}")
        lines.append(f"  标题：{it.title}")
        if it.url:
            lines.append(f"  链接：{it.url}")
    for it in rss[:8]:
        key = it.title.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• 来源：{_source_label(it.source)}")
        lines.append(f"  标题：{it.title}")
        if it.url:
            lines.append(f"  链接：{it.url}")
    lines.append("")
    ms = github_min_stars()
    lines.append("【GitHub 高星人工智能仓库】")
    lines.append(
        f"说明：星标（Stars）不低于 {ms}、近二十四小时内有代码推送，按星标从高到低排列。"
    )
    if not gh:
        lines.append("• 暂无数据，或 GitHub 接口受限（可配置 GITHUB_TOKEN 提高限额）。")
    for it in gh[:12]:
        lines.append(f"• {_source_label(it.source)}")
        if it.repo_full_name:
            lines.append(f"  仓库全名：{it.repo_full_name}")
            lines.append(f"  星标数量：{it.repo_stars}")
            lines.append(f"  编程语言：{it.repo_language}")
            lines.append(f"  摘要：{it.title}")
        else:
            lines.append(f"  摘要：{it.title}")
        if it.url:
            lines.append(f"  链接：{it.url}")
    return "\n".join(lines)


def prepend_chinese_notice_for_english_digest(digest: str) -> str:
    notice = (
        "【说明】当前未配置 OPENAI_API_KEY，也未正确配置腾讯翻译（TENCENT_SECRET_ID 或 TENCENT_TRANSLATE_SECRET_ID 等，且已安装 SDK）：\n"
        "资讯标题与 GitHub 行为英文原文，仅界面为中文。\n"
        "可选：在 .env 中配置腾讯密钥并执行 pip install -r scripts/requirements-tmt.txt；或配置 OPENAI_API_KEY。\n"
        "────────────\n\n"
    )
    return notice + digest


def _env_tencent_secret_id() -> str:
    return (
        os.environ.get("TENCENT_SECRET_ID", "").strip()
        or os.environ.get("TENCENT_TRANSLATE_SECRET_ID", "").strip()
    )


def _env_tencent_secret_key() -> str:
    return (
        os.environ.get("TENCENT_SECRET_KEY", "").strip()
        or os.environ.get("TENCENT_TRANSLATE_SECRET_KEY", "").strip()
    )


def _env_tencent_region() -> str:
    return (
        os.environ.get("TENCENT_TMT_REGION", "").strip()
        or os.environ.get("TENCENT_TRANSLATE_REGION", "").strip()
        or "ap-guangzhou"
    )


def _tencent_sdk_import():
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.tmt.v20180321 import models, tmt_client

        return credential, ClientProfile, HttpProfile, models, tmt_client
    except ImportError:
        return None


def tencent_translate_configured() -> bool:
    if not _env_tencent_secret_id():
        return False
    if not _env_tencent_secret_key():
        return False
    return _tencent_sdk_import() is not None


def _tencent_tmt_client() -> Any | None:
    mod = _tencent_sdk_import()
    if not mod:
        return None
    credential, ClientProfile, HttpProfile, _models, tmt_client = mod
    sid = _env_tencent_secret_id()
    sk = _env_tencent_secret_key()
    region = _env_tencent_region()
    if not sid or not sk:
        return None
    cred = credential.Credential(sid, sk)
    hp = HttpProfile()
    hp.endpoint = "tmt.tencentcloudapi.com"
    cp = ClientProfile()
    cp.httpProfile = hp
    return tmt_client.TmtClient(cred, region, cp)


def _tencent_text_translate_one(
    client: Any, models: Any, text: str, *, pause_sec: float = 0.22
) -> str:
    """单条 TextTranslate；失败返回原文。pause_sec 用于避免触发每秒 5 次 QPS 限制。"""
    if pause_sec > 0:
        time.sleep(pause_sec)
    if not text.strip():
        return text
    req = models.TextTranslateRequest()
    req.SourceText = text[:2000]
    req.Source = "auto"
    req.Target = "zh"
    req.ProjectId = 0
    try:
        resp = client.TextTranslate(req)
        return (resp.TargetText or text).strip() or text
    except Exception as e:
        print(f"[warn] 腾讯翻译单条失败: {e}", file=sys.stderr)
        return text


def tencent_translate_batch_list(strings: list[str]) -> list[str]:
    """auto → zh，顺序与输入一致；失败条回退原文。兼容无 Batch API 的旧版 SDK。"""
    if not strings:
        return []
    client = _tencent_tmt_client()
    mod = _tencent_sdk_import()
    if not client or not mod:
        return list(strings)
    _credential, _cp, _hp, models, _tc = mod
    batch_cls = getattr(models, "TextTranslateBatchRequest", None)
    if batch_cls is not None and hasattr(client, "TextTranslateBatch"):
        out: list[str] = []
        chunk_max = 45
        for i in range(0, len(strings), chunk_max):
            chunk = strings[i : i + chunk_max]
            req = batch_cls()
            req.Source = "auto"
            req.Target = "zh"
            req.ProjectId = 0
            req.SourceTextList = chunk
            try:
                resp = client.TextTranslateBatch(req)
                tl = list(resp.TargetTextList) if getattr(resp, "TargetTextList", None) else []
                if len(tl) != len(chunk):
                    print(
                        "[warn] 腾讯翻译批量返回条数不一致，本批改单条重试。",
                        file=sys.stderr,
                    )
                    for t in chunk:
                        out.append(
                            _tencent_text_translate_one(client, models, t, pause_sec=0.22)
                        )
                else:
                    out.extend(tl)
            except Exception as e:
                print(f"[warn] 腾讯翻译批量失败，改单条: {e}", file=sys.stderr)
                for t in chunk:
                    out.append(
                        _tencent_text_translate_one(client, models, t, pause_sec=0.22)
                    )
        return out
    out_one: list[str] = []
    for t in strings:
        out_one.append(_tencent_text_translate_one(client, models, t, pause_sec=0.22))
    return out_one


def tencent_translate_news_items(items: list[Item]) -> list[Item]:
    if not items:
        return items
    zh_titles = tencent_translate_batch_list([it.title for it in items])
    if len(zh_titles) != len(items):
        return items
    return [
        Item(
            title=str(z).strip(),
            url=it.url,
            source=it.source,
            repo_full_name=it.repo_full_name,
            repo_stars=it.repo_stars,
            repo_language=it.repo_language,
        )
        for it, z in zip(items, zh_titles)
    ]


def tencent_github_summaries_tmt(repos: list[dict[str, Any]]) -> dict[str, str]:
    """将仓库说明机译为中文后截断至百字（非 LLM 摘要）。"""
    if not repos:
        return {}
    texts: list[str] = []
    keys: list[str] = []
    for r in repos:
        fn = r.get("full_name") or ""
        if not fn:
            continue
        keys.append(fn)
        desc = (r.get("description") or "").strip()
        texts.append(desc[:1800] if desc else f"GitHub repository {fn}")
    if not texts:
        return {}
    zh_list = tencent_translate_batch_list(texts)
    if len(zh_list) != len(keys):
        return {}
    return {fn: _truncate_zh(z, 100) for fn, z in zip(keys, zh_list)}


def _openai_chat(system: str, user_text: str) -> str | None:
    """调用 OpenAI 兼容 chat/completions。失败返回 None。"""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text[:14000]},
        ],
        "temperature": 0.35,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[warn] LLM 调用失败: {e}", file=sys.stderr)
        return None


def llm_translate_news_items(items: list[Item]) -> list[Item]:
    """将资讯标题译为中文；失败则返回原列表。"""
    if not items or not os.environ.get("OPENAI_API_KEY", "").strip():
        return items
    arr = [
        {"序号": i + 1, "来源": it.source, "外文标题": it.title} for i, it in enumerate(items)
    ]
    system = (
        "输入为 JSON 数组，每项含「序号」「来源」「外文标题」。请将「外文标题」译为简洁通顺的**简体中文**标题。\n"
        "输出**仅**为 JSON 字符串数组，顺序与序号一致，元素个数必须与输入条数完全相同。\n"
        "不要 markdown 代码块，不要解释。"
    )
    raw = _openai_chat(system, json.dumps(arr, ensure_ascii=False))
    if not raw:
        return items
    try:
        titles = _parse_llm_json(raw)
        if not isinstance(titles, list) or len(titles) != len(items):
            return items
        out: list[Item] = []
        for it, t in zip(items, titles):
            out.append(
                Item(
                    title=str(t).strip(),
                    url=it.url,
                    source=it.source,
                    repo_full_name=it.repo_full_name,
                    repo_stars=it.repo_stars,
                    repo_language=it.repo_language,
                )
            )
        return out
    except (json.JSONDecodeError, TypeError, ValueError):
        return items


def llm_github_summaries_zh(repos: list[dict[str, Any]]) -> dict[str, str]:
    """每个仓库不超过 100 个汉字的摘要；失败返回空字典。"""
    if not repos or not os.environ.get("OPENAI_API_KEY", "").strip():
        return {}
    arr = [
        {
            "full_name": r.get("full_name"),
            "项目说明_原文": (r.get("description") or "")[:500],
            "主要语言": r.get("language") or "",
        }
        for r in repos
    ]
    system = (
        "输入为 JSON 数组，每项对应一个 GitHub 仓库（含 full_name、项目说明_原文、主要语言）。\n"
        "请为每个仓库写**不超过100个汉字**的简体中文摘要，概括用途与特点，语气客观。\n"
        "输出**仅**为 JSON 对象：键必须与 full_name 完全一致，值为摘要字符串；不要星标数字、不要链接、不要换行。\n"
        "不要 markdown 代码块，不要解释。"
    )
    raw = _openai_chat(system, json.dumps(arr, ensure_ascii=False))
    if not raw:
        return {}
    try:
        obj = _parse_llm_json(raw)
        if not isinstance(obj, dict):
            return {}
        return {str(k): _truncate_zh(str(v), 100) for k, v in obj.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def llm_second_step_chinese(digest_en: str) -> str | None:
    """
    第二步：把已生成的英文 digest 转为中文。
    BRIEF_LLM_MODE=translate → 尽量忠实翻译；否则 brief → 编辑整理成简报（默认）。
    """
    mode = os.environ.get("BRIEF_LLM_MODE", "brief").strip().lower()
    if mode == "translate":
        system = (
            "你是专业英中翻译。用户给的是「AI 日报」英文素材（可能含少量中文标签行）。\n"
            "任务：译为**通篇简体中文**，尽量直译、不增删事实；保持列表与分段结构；章节标题用【热点与资讯】【GitHub 高星人工智能仓库】。\n"
            "专有名词首次出现建议「中文（English）」；仓库全名 owner/repo、编程语言名（如 Python、TypeScript）保持英文。\n"
            "所有 http(s):// 开头的 URL 必须与原文**逐字一致**完整保留，每条链接单独一行，勿编造链接。\n"
            "不要输出与翻译无关的前言后记。"
        )
        label = "translate（直译）"
    else:
        system = (
            "你是双语科技编辑。用户会给你一份「AI 日报」原始素材（多为英文标题与 GitHub 英文描述）。\n"
            "请输出一份**通篇简体中文**的简报，严格保留下列结构（照抄章节标题行）：\n"
            "第一行：📅 人工智能日报 YYYY-MM-DD HH:MM（近二十四小时）\n"
            "空行\n"
            "【热点与资讯】\n"
            "然后逐条：用中文概括每条资讯的核心事实；人名、公司名、产品名、法律与平台名等专有名词用「中文译名或通用称呼（English 原文）」形式，"
            "例如：Anthropic（Anthropic）、GitHub（GitHub）、Meta（Meta）。\n"
            "每条末尾单独一行给出链接，与素材中 URL 完全一致，勿改写。\n"
            "空行\n"
            "【GitHub 高星人工智能仓库】\n"
            "说明一行：星标门槛与「近二十四小时内有推送」与素材一致。\n"
            "逐条：仓库全名保持 owner/repo 英文；每条用**不超过100个汉字**的中文摘要介绍项目；技术词如 RAG、LLM、API、GPU 等可保留英文或写「中文（English）」。\n"
            "每条附完整 GitHub 链接。\n"
            "要求：不要输出英文大段复述；不要编造素材中没有的链接；总长度适中，适合 IM 推送。"
        )
        label = "brief（编辑整理）"
    print(f"[info] 第二步：英文 digest → 中文（模式 {label}）…", file=sys.stderr)
    return _openai_chat(system, digest_en)


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
    env_path = os.path.join(script_dir, ".env")
    load_dotenv_file(env_path)

    hn = fetch_hn_stories()
    rss = fetch_rss_items()
    merged = _merge_github_repos()
    gh_fb = _github_repos_to_items_fallback_en(merged)

    hn_orig = copy.deepcopy(hn)
    rss_orig = copy.deepcopy(rss)
    gh_fb_ref = copy.deepcopy(gh_fb)

    backend = _resolve_translate_backend()
    has_llm = backend == "openai"
    has_tmt = backend == "tencent"
    sums: dict[str, str] | None = None
    if _env_tencent_secret_id() and not tencent_translate_configured():
        if not _env_tencent_secret_key():
            print(
                "[warn] 已设置腾讯 SecretId 但缺少 SecretKey（TENCENT_SECRET_KEY 或 TENCENT_TRANSLATE_SECRET_KEY）。",
                file=sys.stderr,
            )
        elif _tencent_sdk_import() is None:
            print(
                "[warn] 使用腾讯翻译需安装：pip install -r scripts/requirements-tmt.txt，"
                "并用带 SDK 的 Python 运行（如 ./tencent-env/bin/python）。",
                file=sys.stderr,
            )

    if has_llm:
        print(
            "[info] OpenAI：资讯标题中文化 + GitHub 百字摘要（模型生成）…",
            file=sys.stderr,
        )
        hn = llm_translate_news_items(hn)
        rss = llm_translate_news_items(rss)
        sums = llm_github_summaries_zh(merged)
        if not sums and tencent_translate_configured():
            print(
                "[info] OpenAI 未返回 GitHub 摘要，改用腾讯翻译生成百字摘要…",
                file=sys.stderr,
            )
            sums = tencent_github_summaries_tmt(merged)
        gh = (
            _github_repos_to_items_with_summaries(merged, sums)
            if sums
            else gh_fb
        )
    elif has_tmt:
        print("[info] 腾讯翻译君：批量机翻标题与仓库说明（摘要截断至百字）…", file=sys.stderr)
        hn = tencent_translate_news_items(hn)
        rss = tencent_translate_news_items(rss)
        sums = tencent_github_summaries_tmt(merged)
        gh = (
            _github_repos_to_items_with_summaries(merged, sums)
            if sums
            else gh_fb
        )

    if has_llm or has_tmt:
        digest = build_digest_items(hn, rss, gh)
        digest_ref = build_digest_items(hn_orig, rss_orig, gh_fb_ref)
        append_en = os.environ.get("BRIEF_APPEND_ENGLISH_ORIGINAL", "1").strip().lower()
        if append_en in ("0", "false", "no", "off"):
            full_text = digest
        else:
            full_text = (
                digest
                + "\n\n────────────\n【采编参考·抓取原文】\n\n"
                + digest_ref
            )
    else:
        digest = build_digest_items(hn, rss, gh_fb)
        zh_brief = llm_second_step_chinese(digest)
        if zh_brief:
            append_en = os.environ.get("BRIEF_APPEND_ENGLISH_ORIGINAL", "1").strip().lower()
            if append_en in ("0", "false", "no", "off"):
                full_text = zh_brief
            else:
                full_text = (
                    zh_brief
                    + "\n\n────────────\n【采编参考·抓取原文】\n\n"
                    + digest
                )
        else:
            full_text = prepend_chinese_notice_for_english_digest(digest)

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
