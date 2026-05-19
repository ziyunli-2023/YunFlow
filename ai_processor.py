"""AI processor — translation (EN→ZH) and digest summarization via DeepSeek API."""

import json
import logging

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
    return _client


def _extract_json_array(raw: str):
    raw = (raw or "").strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("No JSON array in response")
    return json.loads(raw[start:end + 1])


def translate_batch(posts: list[dict]) -> list[dict]:
    """
    Translate up to 5 posts per call.
    Input: list of dicts with 'title' and 'summary' keys.
    Returns: list of dicts with 'title_zh' and 'summary_zh' keys.
    """
    if not config.DEEPSEEK_API_KEY or not posts:
        return posts  # Return original posts so caller can use title/summary

    items_text = "\n".join(
        f"{i+1}. title: {p.get('title','')}\n   summary: {p.get('summary','')[:300]}"
        for i, p in enumerate(posts[:5])
    )

    prompt = f"""你是AI领域专业翻译。将以下英文AI资讯的标题和摘要翻译成简体中文。
技术术语规则：LLM、RLHF、fine-tuning、prompt、transformer、token、benchmark 等保留英文或使用业界通用译法。

{items_text}

严格按以下 JSON 数组格式返回，不要添加任何其他内容：
[{{"title_zh": "...", "summary_zh": "..."}}, ...]"""

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        results = _extract_json_array(raw)
        while len(results) < len(posts):
            results.append({})
        return results[:len(posts)]
    except Exception as e:
        logger.error("translate_batch failed: %s", e)
        return posts  # Return original posts on failure for graceful fallback


def _translate_texts_once(texts: list[str]) -> list[str]:
    """
    Single-pass batch translation. Returns originals for any slot that fails.
    """
    indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indexed:
        return list(texts)

    items_text = "\n".join(f"{n+1}. {t[:500]}" for n, (_, t) in enumerate(indexed))
    prompt = f"""你是AI/科技领域专业翻译。将下面编号的英文内容逐条翻译成简体中文。
技术术语规则：LLM、RLHF、fine-tuning、prompt、transformer、token、benchmark 等保留英文或使用业界通用译法。
保持简洁、忠实原文，不要添加解释。

{items_text}

严格按以下 JSON 数组格式返回，按相同顺序，长度必须为 {len(indexed)}，不要添加任何其他内容：
["译文1", "译文2", ...]"""

    out = list(texts)
    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        results = _extract_json_array(raw)
        for (orig_idx, _), translated in zip(indexed, results):
            if isinstance(translated, str) and translated.strip():
                out[orig_idx] = translated.strip()
    except Exception as e:
        raise ValueError(str(e)) from e
    return out


def translate_texts(texts: list[str]) -> list[str]:
    """
    On-demand translator for arbitrary English snippets → simplified Chinese.
    Used for post titles/summaries and tweet text when the background worker
    hasn't translated them yet. Returns a list of the same length; on failure
    or empty input the original text is returned for that slot.
    """
    if not config.DEEPSEEK_API_KEY or not texts:
        return list(texts)

    try:
        return _translate_texts_once(texts)
    except Exception as e:
        logger.error("translate_texts failed for batch size %d: %s", len(texts), e)
        if len(texts) <= 1:
            return list(texts)

        mid = len(texts) // 2
        left = translate_texts(texts[:mid])
        right = translate_texts(texts[mid:])
        return left + right


def generate_daily_briefing(posts_by_category: dict, lang: str = "zh") -> dict:
    """
    Generate a structured daily briefing with bullet points per category.
    Input: {category: [post_dicts]} from storage.get_recent_posts_by_category()
    Returns: {"sections": [{"category": str, "label": str, "icon": str, "points": [str]}]}
    """
    if not config.DEEPSEEK_API_KEY:
        return {"sections": []}

    CATEGORY_META = {
        "zh": {
            "polymarket":  {"label": "预测市场",   "icon": "🎯"},
            "venture":     {"label": "创投圈",     "icon": "💰"},
            "us_stock":    {"label": "美股",       "icon": "📈"},
            "trump":       {"label": "特朗普动向", "icon": "🇺🇸"},
            "geopolitics": {"label": "地缘政治",   "icon": "🌍"},
            "ai":          {"label": "AI 前沿",    "icon": "🤖"},
            "papers":      {"label": "AI 论文",    "icon": "📄"},
            "web3":        {"label": "Web3",       "icon": "⛓️"},
        },
        "en": {
            "polymarket":  {"label": "Prediction Markets", "icon": "🎯"},
            "venture":     {"label": "Venture",            "icon": "💰"},
            "us_stock":    {"label": "US Stocks",          "icon": "📈"},
            "trump":       {"label": "Trump Watch",        "icon": "🇺🇸"},
            "geopolitics": {"label": "Geopolitics",        "icon": "🌍"},
            "ai":          {"label": "AI",                 "icon": "🤖"},
            "papers":      {"label": "Papers",             "icon": "📄"},
            "web3":        {"label": "Web3",               "icon": "⛓️"},
        },
    }
    meta_map = CATEGORY_META.get(lang, CATEGORY_META["zh"])

    # Build news text per category — pass all candidates, let AI pick the best
    sections_input = []
    for cat, meta in meta_map.items():
        posts = posts_by_category.get(cat, [])
        if not posts:
            no_data = "No data" if lang == "en" else "暂无数据"
            sections_input.append(f"[{meta['label']}] {no_data}")
            continue
        lines = [f"[{meta['label']}]"]
        for i, p in enumerate(posts):
            if cat == "polymarket":
                summary = (p.get("summary") or "").split(" | Ends")[0]
                lines.append(f"{i+1}. {p.get('title', '')} ({summary})")
            else:
                title = p.get("title") if lang == "en" else (p.get("title_zh") or p.get("title", ""))
                source = p.get("source", "")
                lines.append(f"{i+1}. [{source}] {title}")
        sections_input.append("\n".join(lines))

    news_text = "\n\n".join(sections_input)

    # Build a flat url_map: cat -> {1-based index -> url}
    url_map: dict[str, dict[int, str]] = {}
    for cat, posts in posts_by_category.items():
        url_map[cat] = {i + 1: p.get("url", "") for i, p in enumerate(posts)}

    if lang == "en":
        prompt = f"""The following are today's news candidates by category (more than needed — you must select the most important ones):

{news_text}

Your task: For each category, select only the genuinely important items and write a punchy bullet for each.
General selection criteria: global impact, specific numbers/names, market-moving events, breakthroughs — NOT routine updates or minor news.

Trump Watch vs Geopolitics — strict split (no overlap allowed):
- Trump Watch: Trump's OWN actions and statements — orders he signs, tariffs he imposes, deals he announces, summits he attends, threats/warnings he issues. The subject of the bullet must be Trump himself.
- Geopolitics: The conflict or situation itself — military strikes, frontline movements, ceasefires, negotiations between other parties, casualties, troop deployments. Trump may be mentioned as context, but must NOT be the subject.
- If Trump announces a ceasefire → Trump Watch. If fighting escalates on the ground → Geopolitics. Never write the same event in both sections.

Trump Watch additional filter: SKIP entertainment tweets, movie/TV promotions, rally speeches, and personal/family content.
Requirements:
- Number of points per category: 1–4, based on how much genuinely important news exists. Do NOT pad with minor items to hit 4.
- Each bullet under 20 words, direct and specific
- Must reference the actual event, company, or number — no vague summaries
- Venture/IPO: prioritize IPO filings (S-1), pricing, listing dates, unicorn funding rounds; always include company name and valuation or raise amount
- Geopolitics: cover all major conflicts and tensions (Middle East, Russia-Ukraine, China-Taiwan, North Korea, etc.) — must include location, parties involved, or specific development; skip routine diplomatic noise
- For categories with no data, use src 0 and text "No major updates"
- Return strictly in this JSON format, no extra content:

{{"sections": [
  {{"category": "polymarket",  "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 3}}]}},
  {{"category": "venture",     "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 6}}]}},
  {{"category": "us_stock",    "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 4}}]}},
  {{"category": "trump",       "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 3}}]}},
  {{"category": "geopolitics", "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 2}}]}},
  {{"category": "ai",          "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 5}}]}},
  {{"category": "papers",      "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 3}}]}},
  {{"category": "web3",        "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 4}}]}}
]}}"""
    else:
        prompt = f"""以下是今日各板块的候选资讯（数量超出需要，你需要主动挑选最重要的）：

{news_text}

你的任务：每个板块从候选项中挑出真正重要的内容，写成简洁有力的速报要点。
挑选标准：全球影响力、具体数字/事件/人名、市场震动、重大突破——日常更新、常规动态不选。

特朗普动向 vs 地缘政治——严格分工，禁止重叠：
- 特朗普动向：特朗普本人的行动与表态——他签署的命令、他宣布的关税/协议、他出席的峰会、他发出的威胁。每条要点的主语必须是特朗普本人。
- 地缘政治：冲突/局势本身的发展——军事打击、前线动态、停火谈判（其他当事方）、伤亡、兵力部署。特朗普可作为背景提及，但不得作为主语。
- 判断示例：特朗普宣布停火方案 → 特朗普动向；前线爆发交火 → 地缘政治。同一事件绝不在两个板块同时出现。
- 特朗普动向额外过滤：娱乐推文、电影宣传、竞选集会、个人生活内容一律不选。

写作要求：
- 每个板块条数：1~4 条，根据实际重要新闻数量决定，不要为凑数选无关紧要的内容
- 每条 35 字以内，直接点名事件、数字、公司
- 地缘政治：覆盖所有重大冲突与紧张局势（中东、俄乌、台海、朝鲜半岛等）——必须包含地点、当事方或具体进展；外交常规动态不选
- 预测市场：必须给出概率和成交量，用"市场押注"、"赔率显示"等措辞，体现分歧与戏剧性
- 美股：突出涨跌幅、重大事件、具体公司名
- 创投/IPO：优先选 IPO 申请（S-1 filing）、定价、上市日期、独角兽融资；必须给出公司名、估值或募资金额
- AI：突出产品发布、能力突破、重大合作
- 没有数据的板块用 src 0，text 填"暂无重要动态"
- 严格按以下 JSON 格式返回，不要添加其他内容：

{{"sections": [
  {{"category": "polymarket",  "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 3}}]}},
  {{"category": "venture",     "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 6}}]}},
  {{"category": "us_stock",    "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 4}}]}},
  {{"category": "trump",       "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 3}}]}},
  {{"category": "geopolitics", "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 2}}]}},
  {{"category": "ai",          "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 5}}]}},
  {{"category": "papers",      "points": [{{"text": "...", "src": 2}}, {{"text": "...", "src": 3}}]}},
  {{"category": "web3",        "points": [{{"text": "...", "src": 1}}, {{"text": "...", "src": 4}}]}}
]}}"""

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.6,
        )
        raw = resp.choices[0].message.content.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON object in response")
        data = json.loads(raw[start:end + 1])
        # Attach label/icon metadata and resolve src index -> url
        for sec in data.get("sections", []):
            cat = sec.get("category", "")
            meta = meta_map.get(cat, {})
            sec["label"] = meta.get("label", cat)
            sec["icon"] = meta.get("icon", "📌")
            cat_urls = url_map.get(cat, {})
            resolved = []
            for pt in sec.get("points", []):
                if isinstance(pt, dict):
                    url = cat_urls.get(int(pt.get("src") or 0), "") or ""
                    resolved.append({"text": pt.get("text", ""), "url": url})
                else:
                    resolved.append({"text": str(pt), "url": ""})
            sec["points"] = resolved
        return data
    except Exception as e:
        logger.error("generate_daily_briefing failed: %s", e)
        return {"sections": []}


def generate_digest_summary(items: list[dict], lang: str = "zh") -> list[str]:
    """
    Generate a bullet-point digest of a batch of news items.
    Input: list of {"type": "post"|"tweet", "item"|"data": {...}} dicts.
    Returns: list of short bullets (3-6 points), or [] on failure.
    """
    if not config.DEEPSEEK_API_KEY or not items:
        return []

    candidates = items[:40]
    url_index: dict[int, str] = {}
    lines = []
    for i, item in enumerate(candidates):
        d = item.get("item") or item.get("data") or item
        url_index[i + 1] = d.get("url", "")
        # Build engagement signal string
        signals = []
        if d.get("hn_score"):
            signals.append(f"HN:{d['hn_score']}")
        if d.get("hf_upvotes"):
            signals.append(f"👍{d['hf_upvotes']}")
        if d.get("paper_score"):
            signals.append(f"score:{d['paper_score']:.1f}")
        sig_str = f" [{', '.join(signals)}]" if signals else ""
        if lang == "en":
            summary_snippet = (d.get("summary") or d.get("summary_zh") or "")[:120]
        else:
            summary_snippet = (d.get("summary_zh") or d.get("summary") or "")[:120]
        if item.get("type") == "tweet":
            lines.append(f"{i+1}. [Tweet @{d.get('username','')}]{sig_str} {d.get('text','')[:150]}")
        else:
            title = d.get("title") if lang == "en" else (d.get("title_zh") or d.get("title", ""))
            if summary_snippet:
                lines.append(f"{i+1}. [{d.get('source','')}]{sig_str} {title} — {summary_snippet}")
            else:
                lines.append(f"{i+1}. [{d.get('source','')}]{sig_str} {title}")
    news_list = "\n".join(lines)

    if lang == "en":
        prompt = f"""You are a sharp analyst selecting the most valuable news items from today's feed.

News list (numbered):
{news_list}

Select 6-10 highlights, covering domains in this priority order (skip only if no relevant item exists):
① US stocks/macro ② Geopolitics ③ VC/startup ④ Prediction markets ⑤ AI ⑥ Web3

Rules:
- One highlight per domain where possible
- Prioritize: specific data/numbers, surprising findings, concrete decisions or releases
- Avoid: generic trend summaries, vague predictions, pure opinion pieces
- Each point under 25 words — lead with the key fact, not the source name

Return strictly as a JSON array, no extra text:
[{{"text": "highlight1", "src": 2}}, {{"text": "highlight2", "src": 5}}]"""
    else:
        prompt = f"""你是一位信息密度极高的资讯分析师，从今天的资讯中为每个领域挑出最值得关注的看点。

资讯列表（已编号）：
{news_list}

选出 6~10 条看点，按以下优先级顺序覆盖各领域（该领域无相关资讯时可跳过）：
① 美股/宏观 ② 地缘政治 ③ 风投/创业 ④ 预测市场 ⑤ AI ⑥ Web3

要求：
- 每个领域尽量各出一条
- 优先选：有具体数据/数字的、令人意外的、有实质进展的内容
- 避免：泛泛趋势总结、模糊预言、纯观点文章
- 每条 30 字以内，直接说核心事实，不要说"某来源报道了XX"

严格按以下 JSON 数组格式返回，不要任何额外文字：
[{{"text": "看点1", "src": 2}}, {{"text": "看点2", "src": 5}}]"""

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.5,
        )
        raw = resp.choices[0].message.content.strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("No JSON array in response")
        parsed = json.loads(raw[start:end + 1])
        result = []
        for b in parsed:
            if isinstance(b, dict):
                text = str(b.get("text", "")).strip()
                url = url_index.get(int(b.get("src") or 0), "") or ""
            else:
                text = str(b).strip()
                url = ""
            if text:
                result.append({"text": text, "url": url})
        return result
    except Exception as e:
        logger.error("generate_digest_summary failed: %s", e)
        return []


def generate_joke(items: list[dict], lang: str = "zh") -> list[str]:
    """
    Generate up to 10 jokes grounded in today's real news (score >= 4 kept).
    Returns a list of joke strings (pool for frontend cycling), or [] on failure.
    """
    if not config.DEEPSEEK_API_KEY or not items:
        return []

    candidates = items[:30]
    lines = []
    for i, item in enumerate(candidates):
        d = item.get("item") or item.get("data") or item
        if lang == "en":
            title = d.get("title") or d.get("title_zh") or ""
            summary = (d.get("summary") or d.get("summary_zh") or "")[:100]
        else:
            title = d.get("title_zh") or d.get("title") or ""
            summary = (d.get("summary_zh") or d.get("summary") or "")[:100]
        text = d.get("text", "")
        if text:
            lines.append(f"{i+1}. {text[:150]}")
        elif summary:
            lines.append(f"{i+1}. {title} — {summary}")
        else:
            lines.append(f"{i+1}. {title}")
    news_list = "\n".join(lines)

    if lang == "en":
        prompt = f"""You are a sharp-tongued financial comedy writer who turns real news into witty jokes.

Today's news (use only these facts, no fabrication):
{news_list}

Task: Write up to 10 jokes, each based on a different news item.
- Any format: dialogue, one-liner, dark irony, satirical observation
- Sharp, punchy, with a twist — make people smirk
- Max 60 words each
- Honestly score each joke 1–5; only give high scores if it's genuinely funny
- If a joke isn't landing, score it low — don't pad the count

Return strictly as a JSON array, no extra text:
[{{"joke": "joke text", "score": 4}}, ...]"""
    else:
        prompt = f"""你是一位毒舌财经脱口秀编剧，擅长把今天真实发生的新闻事件改编成幽默段子。

今天的新闻（只能从这里取材，不能编造）：
{news_list}

任务：尽量写10条段子，每条取材自不同的新闻事件。
- 形式不限：对话、冷笑话、神转折、讽刺评论均可
- 语言要辛辣、有反转、让人会心一笑
- 每条不超过70个汉字
- 对每条段子诚实打分（1~5分），只有真的好笑才算高分
- 如果某条写得不好，score 打低分即可，不要强行编凑数

严格按以下 JSON 数组返回，不要任何额外文字：
[{{"joke": "段子内容", "score": 4}}, ...]"""

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.9,
        )
        raw = resp.choices[0].message.content.strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("No JSON array")
        parsed = json.loads(raw[start:end + 1])
        cleaned = []
        for item in parsed[:10]:
            joke = str(item.get("joke", "")).strip()
            score = int(item.get("score", 0))
            if joke and score >= 4:
                cleaned.append(joke)
        return cleaned
    except Exception as e:
        logger.error("generate_joke failed: %s", e)
        return []
