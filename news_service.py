import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import database


def parse_news_command(text):
    command = str(text or "").strip()
    if command == "查看兴趣资讯设置":
        return {"action": "view"}
    if command in {"关闭兴趣资讯", "关闭资讯彩蛋"}:
        return {"action": "disable"}
    for prefix in ("设置兴趣关键词", "设置资讯关键词"):
        if command.startswith(prefix):
            raw = command[len(prefix):].strip(" ：:")
            keywords = [
                item.strip()
                for item in re.split(r"[,，、;；]+", raw)
                if item.strip()
            ]
            if not 1 <= len(keywords) <= 5:
                return {"action": "set", "error": "请填写 1—5 个关键词"}
            return {"action": "set", "keywords": keywords}
    return None


def handle_news_command(user_open_id, text):
    command = parse_news_command(text)
    if command is None:
        return None
    if command["action"] == "view":
        preference = database.get_user_preference(user_open_id) or {}
        enabled = bool(preference.get("news_enabled"))
        keywords = preference.get("interest_keywords") or []
        return (
            "当前兴趣资讯设置：\n"
            f"- 状态：{'已开启' if enabled else '已关闭'}\n"
            f"- 关键词：{'、'.join(keywords) if keywords else '未设置'}"
        )
    if command["action"] == "disable":
        database.update_news_preference(user_open_id, enabled=False)
        return "兴趣资讯彩蛋已关闭，不会影响正常晨间计划。"
    if command.get("error"):
        return "兴趣关键词无法保存：" + command["error"]

    database.update_news_preference(
        user_open_id,
        keywords=command["keywords"],
        enabled=True,
    )
    return (
        "✅ 兴趣资讯关键词已保存并开启晨间彩蛋\n\n"
        "关键词：" + "、".join(command["keywords"])
    )


def _clean_summary(value):
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def fetch_news(keyword, limit=5, opener=None):
    if opener is None:
        opener = urllib.request.urlopen
    query = urllib.parse.quote_plus(str(keyword))
    url = f"https://www.bing.com/news/search?q={query}&format=rss"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "today-execution-agent/1.0"},
    )
    with opener(request, timeout=5) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    results = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = _clean_summary(item.findtext("description"))
        source = (item.findtext("source") or "").strip()
        parsed = urllib.parse.urlparse(link)
        if not title or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        results.append(
            {
                "title": title,
                "summary": summary or "打开原文查看详情",
                "link": link,
                "source": source or parsed.netloc,
            }
        )
        if len(results) >= limit:
            break
    return results


def build_news_section(preference, fetcher=fetch_news):
    if not preference or not preference.get("news_enabled"):
        return ""
    keywords = preference.get("interest_keywords") or []
    if not keywords:
        return ""

    items = []
    try:
        for keyword in keywords:
            for item in fetcher(keyword, limit=5):
                if item.get("link") not in {entry.get("link") for entry in items}:
                    items.append(item)
                if len(items) >= 5:
                    break
            if len(items) >= 5:
                break
    except Exception as exc:
        print(f"兴趣资讯获取失败，晨间计划继续发送：{exc!r}", flush=True)
        return ""

    if not items:
        return ""

    lines = ["📰 兴趣资讯彩蛋（外部来源）", ""]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{index}. {item['title']}",
                f"   来源：{item['source']}",
                f"   摘要：{item['summary']}",
                f"   原文：{item['link']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
