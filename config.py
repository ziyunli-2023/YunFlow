"""Configuration: tracked X accounts, RSS feeds, and API settings."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── X API credentials ──────────────────────────────────────────────────────
# Only Bearer Token is needed for reading public timelines (app-only auth)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")

# ── Polling intervals ──────────────────────────────────────────────────────
# Free tier: 1 req/15min app-wide. 8 users × 1 req = 8 reqs per cycle.
# At 1800s interval: 8 reqs/30min = comfortably within free limits.
X_POLL_INTERVAL = 1800  # 30 min

# RSS tiered intervals (seconds) by tier level
RSS_POLL_INTERVALS = {1: 1800, 2: 3600, 3: 7200}

# ── Tracked X accounts ─────────────────────────────────────────────────────
# Each entry: username, priority (1/2/3), category
# NOTE: X monitor requires Basic tier ($100/mo). Active accounts listed here
# are ready to enable once API access is upgraded.
TRACKED_X_USERS = [
    # ── 特朗普 / Trump ──────────────────────────────────────────────────────
    {"username": "realDonaldTrump", "priority": 1, "category": "trump"},   # Donald Trump
    # ── AI 圈 行业领袖 ──────────────────────────────────────────────────────
    {"username": "sama",           "priority": 1, "category": "founder"},     # Sam Altman
    {"username": "DarioAmodei",    "priority": 1, "category": "founder"},     # Dario Amodei
    {"username": "demishassabis",  "priority": 1, "category": "founder"},     # Demis Hassabis
    {"username": "ylecun",         "priority": 1, "category": "researcher"},  # Yann LeCun
    {"username": "AndrewYNg",      "priority": 1, "category": "academic"},    # Andrew Ng
    # ── AI 圈 开源/教育 ─────────────────────────────────────────────────────
    {"username": "karpathy",       "priority": 1, "category": "researcher"},  # Andrej Karpathy
    {"username": "jeremyphoward",  "priority": 2, "category": "academic"},    # Jeremy Howard
    # ── AI 圈 投资人 ────────────────────────────────────────────────────────
    {"username": "paulg",          "priority": 2, "category": "investor"},    # Paul Graham
    {"username": "eladgil",        "priority": 2, "category": "investor"},    # Elad Gil
    {"username": "garrytan",       "priority": 2, "category": "investor"},    # Garry Tan
    # ── Web3 圈 行业领袖 ────────────────────────────────────────────────────
    {"username": "VitalikButerin", "priority": 1, "category": "web3"},        # Vitalik Buterin
    {"username": "brian_armstrong","priority": 2, "category": "web3"},        # Brian Armstrong
    {"username": "cz_binance",     "priority": 2, "category": "web3"},        # CZ
    # ── Web3 圈 技术开发者 ──────────────────────────────────────────────────
    {"username": "haydenzadams",   "priority": 2, "category": "web3"},        # Hayden Adams (Uniswap)
    {"username": "AndreCronjeTech","priority": 2, "category": "web3"},        # Andre Cronje
    {"username": "StaniKulechov",  "priority": 2, "category": "web3"},        # Stani Kulechov (Aave)
    # ── Web3 圈 投资人 ──────────────────────────────────────────────────────
    {"username": "cdixon",         "priority": 2, "category": "web3"},        # Chris Dixon (a16z)
    {"username": "naval",          "priority": 2, "category": "web3"},        # Naval Ravikant
    {"username": "balajis",        "priority": 2, "category": "web3"},        # Balaji Srinivasan
    {"username": "fehrsam",        "priority": 2, "category": "web3"},        # Fred Ehrsam (Paradigm)
    # ── Web3 圈 KOL/媒体 ────────────────────────────────────────────────────
    {"username": "RyanSAdams",     "priority": 3, "category": "web3"},        # Ryan Sean Adams (Bankless)
    {"username": "TrustlessState", "priority": 3, "category": "web3"},        # David Hoffman (Bankless)
    {"username": "sassal0x",       "priority": 3, "category": "web3"},        # Anthony Sassano
    {"username": "laurashin",      "priority": 3, "category": "web3"},        # Laura Shin
]

# Keywords used to filter Priority-3 accounts (only save if tweet contains one)
X_NOISE_KEYWORDS = {
    "llm", "gpt", "claude", "gemini", "model", "paper", "research",
    "alignment", "safety", "agent", "benchmark", "rlhf", "training",
    "inference", "transformer", "multimodal", "reasoning", "openai",
    "anthropic", "deepmind", "mistral", "llama", "neural", "dataset",
}

# ── RSS feeds ──────────────────────────────────────────────────────────────
# Each entry: name, url, tier (1/2/3), is_arxiv (optional)
RSS_FEEDS = [
    # ── AI — Personal blogs (Tier 1, poll every 30 min) ────────────────────
    {"name": "Sam Altman",         "url": "https://blog.samaltman.com/posts.atom",                                                             "tier": 1, "category": "ai"},
    {"name": "Paul Graham",        "url": "https://paulgraham.com/rss.html",                                                                   "tier": 1, "category": "ai"},
    {"name": "Dwarkesh Podcast",   "url": "https://www.dwarkesh.com/feed",                                                                   "tier": 1, "category": "ai", "podcast": True, "site": "https://www.dwarkesh.com"},
    {"name": "Lex Fridman Podcast","url": "https://lexfridman.com/feed/podcast/",                                                               "tier": 1, "category": "ai", "podcast": True, "site": "https://lexfridman.com"},
    {"name": "No Priors",          "url": "https://nopriors.com/feed",                                                                          "tier": 1, "category": "ai", "podcast": True, "site": "https://nopriors.com"},
    {"name": "Latent Space",       "url": "https://www.latent.space/feed",                                                                      "tier": 1, "category": "ai", "podcast": True, "site": "https://www.latent.space"},
    # ── AI — Lab blogs + deep newsletters (Tier 1, poll every 30 min) ──────
    {"name": "OpenAI",             "url": "https://openai.com/news/rss.xml",                                                                   "tier": 1, "category": "ai"},
    {"name": "Anthropic",          "url": "https://raw.githubusercontent.com/0xSMW/rss-feeds/main/feeds/feed_anthropic_news.xml",              "tier": 1, "category": "ai"},
    {"name": "Google DeepMind",    "url": "https://deepmind.google/blog/rss.xml",                                                              "tier": 1, "category": "ai"},
    {"name": "Meta Engineering",    "url": "https://engineering.fb.com/feed/",                                                                   "tier": 1, "category": "ai"},
    {"name": "Hugging Face",       "url": "https://huggingface.co/blog/feed.xml",                                                              "tier": 1, "category": "ai"},
    {"name": "Import AI",          "url": "https://importai.substack.com/feed",                                                                "tier": 1, "category": "ai"},
    {"name": "Interconnects",      "url": "https://www.interconnects.ai/feed",                                                                 "tier": 1, "category": "ai"},
    {"name": "Ahead of AI",        "url": "https://magazine.sebastianraschka.com/feed",                                                        "tier": 1, "category": "ai"},
    {"name": "The Batch",          "url": "https://www.deeplearning.ai/the-batch/feed.xml",                                                    "tier": 1, "category": "ai"},
    # ── AI — Research & industry blogs (Tier 2, poll every 60 min) ─────────
    {"name": "Google AI",          "url": "https://blog.google/technology/ai/rss/",                                                            "tier": 2, "category": "ai"},
    {"name": "Google Research",    "url": "https://research.google/blog/rss/",                                                                 "tier": 2, "category": "ai"},
    {"name": "AWS ML",             "url": "https://aws.amazon.com/blogs/machine-learning/feed/",                                               "tier": 2, "category": "ai"},
    {"name": "BAIR",               "url": "https://bair.berkeley.edu/blog/feed.xml",                                                           "tier": 2, "category": "ai"},
    {"name": "Last Week in AI",    "url": "https://lastweekin.ai/feed",                                                                        "tier": 2, "category": "ai"},
    {"name": "Marcus on AI",       "url": "https://garymarcus.substack.com/feed",                                                              "tier": 2, "category": "ai"},
    {"name": "Chollet Substack",   "url": "https://fchollet.substack.com/feed",                                                                "tier": 2, "category": "ai"},
    {"name": "The Decoder",        "url": "https://the-decoder.com/feed/",                                                                     "tier": 2, "category": "ai"},
    {"name": "Microsoft AI Blog",  "url": "https://blogs.microsoft.com/ai/feed/",                                                              "tier": 2, "category": "ai"},
    {"name": "NVIDIA Blog",        "url": "https://developer.nvidia.com/blog/feed/",                                                           "tier": 2, "category": "ai"},
    {"name": "Stanford HAI",       "url": "https://hai.stanford.edu/news/rss.xml",                                                             "tier": 2, "category": "ai"},
    {"name": "MIT Tech Review AI", "url": "https://www.technologyreview.com/feed/",                                                            "tier": 2, "category": "ai"},
    # ── AI — High-volume news sites (Tier 3, poll every 2 hr) ───────────────
    {"name": "VentureBeat AI",     "url": "https://venturebeat.com/category/ai/feed/",                                                         "tier": 3, "category": "ai"},
    {"name": "The Verge AI",       "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",                                 "tier": 3, "category": "ai"},
    {"name": "Wired AI",           "url": "https://www.wired.com/feed/tag/ai/latest/rss",                                                      "tier": 3, "category": "ai"},
    {"name": "Towards Data Science","url": "https://towardsdatascience.com/feed/",                                                             "tier": 3, "category": "ai"},
    # ── AI — ArXiv (keyword + author/org whitelist filtered, see ARXIV_AUTHOR_WHITELIST) ─
    {"name": "arXiv cs.AI",        "url": "http://arxiv.org/rss/cs.AI",  "tier": 2, "category": "papers", "is_arxiv": True},
    {"name": "arXiv cs.LG",        "url": "http://arxiv.org/rss/cs.LG",  "tier": 2, "category": "papers", "is_arxiv": True},

    # ── Web3 / Crypto (poll every 60 min) ───────────────────────────────────
    {"name": "CoinDesk",           "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",                                                   "tier": 2, "category": "web3"},
    {"name": "CoinTelegraph",      "url": "https://cointelegraph.com/rss",                                                                     "tier": 2, "category": "web3"},
    {"name": "The Block",          "url": "https://www.theblock.co/rss.xml",                                                                   "tier": 2, "category": "web3"},
    {"name": "Decrypt",            "url": "https://decrypt.co/feed",                                                                           "tier": 2, "category": "web3"},

    # ── 创投圈 / Venture (poll every 60 min) ─────────────────────────────────
    {"name": "Y Combinator",       "url": "https://www.ycombinator.com/blog/rss.xml",                                                          "tier": 1, "category": "venture"},
    {"name": "a16z",               "url": "https://a16z.substack.com/feed",                                                                    "tier": 1, "category": "venture"},
    {"name": "Sequoia Capital",    "url": "https://www.sequoiacap.com/feed/",                                                                  "tier": 1, "category": "venture"},
    {"name": "Lightspeed",         "url": "https://lsvp.com/feed/",                                                                           "tier": 2, "category": "venture"},
    {"name": "TechCrunch",         "url": "https://techcrunch.com/feed/",                                                                      "tier": 2, "category": "venture"},
    {"name": "TechCrunch IPO",     "url": "https://techcrunch.com/tag/ipo/feed/",                                                              "tier": 1, "category": "venture"},
    {"name": "Crunchbase News",    "url": "https://news.crunchbase.com/feed/",                                                                 "tier": 2, "category": "venture"},
    {"name": "StrictlyVC",         "url": "https://strictlyvc.com/feed/",                                                                      "tier": 2, "category": "venture"},
    {"name": "IPO Scoop",          "url": "https://www.iposcoop.com/feed/",                                                                    "tier": 1, "category": "venture"},
    {"name": "IPO Watch",          "url": "https://news.google.com/rss/search?q=IPO+filing+technology&hl=en-US&gl=US&ceid=US:en",              "tier": 1, "category": "venture"},
    {"name": "IPO Reuters",        "url": "https://news.google.com/rss/search?q=IPO+site:reuters.com&hl=en-US&gl=US&ceid=US:en",              "tier": 1, "category": "venture"},

    # ── 地缘政治 / Geopolitics (Tier 1, poll every 30 min) ──────────────────
    {"name": "Geo Middle East",   "url": "https://news.google.com/rss/search?q=Iran+OR+Israel+OR+Gaza+OR+%22Middle+East%22+war+OR+strike+OR+conflict&hl=en-US&gl=US&ceid=US:en",      "tier": 1, "category": "geopolitics"},
    {"name": "Geo Russia Ukraine","url": "https://news.google.com/rss/search?q=Russia+Ukraine+war+OR+Zelensky+OR+Putin+ceasefire+OR+frontline&hl=en-US&gl=US&ceid=US:en",             "tier": 1, "category": "geopolitics"},
    {"name": "Geo China Taiwan",  "url": "https://news.google.com/rss/search?q=China+Taiwan+OR+%22South+China+Sea%22+OR+%22PLA%22+military&hl=en-US&gl=US&ceid=US:en",               "tier": 1, "category": "geopolitics"},
    {"name": "Geo North Korea",   "url": "https://news.google.com/rss/search?q=%22North+Korea%22+missile+OR+nuclear+OR+Kim+Jong&hl=en-US&gl=US&ceid=US:en",                           "tier": 1, "category": "geopolitics"},
    {"name": "Geo Reuters",       "url": "https://news.google.com/rss/search?q=geopolitics+OR+conflict+OR+war+site:reuters.com&hl=en-US&gl=US&ceid=US:en",                            "tier": 1, "category": "geopolitics"},
    {"name": "Geo BBC World",     "url": "https://news.google.com/rss/search?q=war+OR+conflict+OR+crisis+site:bbc.com&hl=en-US&gl=US&ceid=US:en",                                     "tier": 1, "category": "geopolitics"},

    # ── 特朗普动向 / Trump Watch (Tier 1, poll every 30 min) ─────────────────
    {"name": "Trump/CNBC",         "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15837362",                                                      "tier": 1, "category": "trump"},
    {"name": "Trump News",         "url": "https://news.google.com/rss/search?q=Trump&hl=en-US&gl=US&ceid=US:en",                                                                      "tier": 1, "category": "trump"},
    {"name": "Trump Policy",       "url": "https://news.google.com/rss/search?q=%22Trump+signs%22+OR+%22Trump+tariff%22+OR+%22Trump+says%22+OR+%22Trump+executive%22&hl=en-US&gl=US&ceid=US:en", "tier": 1, "category": "trump"},
    {"name": "White House",        "url": "https://news.google.com/rss/search?q=%22White+House%22+Trump&hl=en-US&gl=US&ceid=US:en",                                                   "tier": 1, "category": "trump"},

    # ── 美股 / US Stocks (poll every 60 min) ────────────────────────────────
    {"name": "MarketWatch",        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",                                             "tier": 2, "category": "us_stock"},
    {"name": "CNBC Markets",       "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",                                              "tier": 2, "category": "us_stock"},
    {"name": "Reuters Business",   "url": "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com/business&hl=en-US&gl=US&ceid=US:en", "tier": 2, "category": "us_stock"},

    # ── Web3 圈官方博客 (poll every 60 min) ─────────────────────────────────
    {"name": "Paradigm",           "url": "https://paradigm.substack.com/feed",                                                                "tier": 2, "category": "web3"},
    {"name": "CoinCenter",         "url": "https://coincenter.org/feed",                                                                       "tier": 2, "category": "web3"},

    # ── 论文 / 技术报告 — 实验室博客 RSS (Tier 2, poll every 60 min) ────────
    # 这些是国内外 AI 实验室的官方博客/新闻 RSS,用作"技术报告"信号。
    # category="papers" 让 UI 把它们归入 📄 论文 类目;papers_monitor 会再标 is_paper=1。
    {"name": "Apple ML Research",   "url": "https://machinelearning.apple.com/rss.xml",                                                        "tier": 2, "category": "papers"},
    {"name": "Allen AI (AI2)",      "url": "https://allenai.org/blog.atom",                                                                    "tier": 2, "category": "papers"},
    {"name": "Cohere",              "url": "https://cohere.com/blog/rss.xml",                                                                  "tier": 2, "category": "papers"},
    {"name": "Microsoft Research",  "url": "https://www.microsoft.com/en-us/research/feed/",                                                   "tier": 2, "category": "papers"},
    {"name": "DeepSeek 技术报告",   "url": "https://api-docs.deepseek.com/news/feed.xml",                                                      "tier": 2, "category": "papers"},
    {"name": "Qwen 技术报告",       "url": "https://qwenlm.github.io/blog/index.xml",                                                          "tier": 2, "category": "papers"},
    {"name": "Moonshot Kimi",       "url": "https://github.com/MoonshotAI/Kimi-K2/releases.atom",                                              "tier": 2, "category": "papers"},
    {"name": "01.AI Yi",            "url": "https://github.com/01-ai/Yi/releases.atom",                                                        "tier": 2, "category": "papers"},
    {"name": "ByteDance Seed",      "url": "https://github.com/bytedance/Seed-OSS/releases.atom",                                              "tier": 2, "category": "papers"},
    {"name": "智谱 GLM",            "url": "https://github.com/THUDM/GLM-4.5/releases.atom",                                                   "tier": 2, "category": "papers"},

    # ── 论文 — 扩展 arXiv 分类 (Tier 2, 关键词 + 白名单双重过滤) ──────────────
    {"name": "arXiv cs.CL",        "url": "http://arxiv.org/rss/cs.CL",  "tier": 2, "category": "papers", "is_arxiv": True},
    {"name": "arXiv cs.CV",        "url": "http://arxiv.org/rss/cs.CV",  "tier": 2, "category": "papers", "is_arxiv": True},
    {"name": "arXiv stat.ML",      "url": "http://arxiv.org/rss/stat.ML", "tier": 2, "category": "papers", "is_arxiv": True},
    {"name": "arXiv cs.MA",        "url": "http://arxiv.org/rss/cs.MA",  "tier": 2, "category": "papers", "is_arxiv": True},
]

# Keywords for ArXiv filtering (only store papers matching at least one)
ARXIV_KEYWORDS = {
    "language model", "large language", "llm", "alignment", "reasoning",
    "agent", "rlhf", "multimodal", "instruction tuning", "chain-of-thought",
    "in-context learning", "fine-tuning", "reinforcement learning from human",
    "vision-language", "text-to-image", "diffusion model", "transformer",
    "retrieval-augmented", "hallucination", "jailbreak", "safety",
}

# Second arXiv filter layer: papers must mention a top-tier institution
# or a known frontier lab in title/summary/authors. Cuts ~80% of arXiv noise.
ARXIV_AUTHOR_WHITELIST = {
    # Industrial labs
    "openai", "anthropic", "deepmind", "google research", "google brain",
    "meta ai", "fair ", "facebook ai", "microsoft research", "msr ",
    "nvidia", "apple ", "allen institute", "ai2 ", "mistral",
    "cohere", "stability ai", "runway", "xai", "x.ai",
    # Chinese labs
    "deepseek", "alibaba", "qwen", "bytedance", "doubao", "seed-",
    "tencent", "baidu", "moonshot", "kimi", "zhipu", "thudm",
    "01.ai", "01-ai", "yi-", "baichuan", "01-ai",
    # Top universities
    "mit ", "stanford", "berkeley", "cmu", "carnegie mellon",
    "princeton", "harvard", "oxford", "cambridge", "eth zurich",
    "tsinghua", "peking university", "sjtu", "fudan",
}

# HuggingFace org IDs of frontier AI labs.
# Used by papers_monitor.py to fetch each lab's papers via /api/papers?author={org_id}.
# Map: HF org id → human-readable Chinese label used as `source`.
PAPER_LAB_ORG_IDS = {
    # 国外
    "deepmind":          "DeepMind 技术报告",
    "openai":            "OpenAI 技术报告",
    "Anthropic":         "Anthropic 技术报告",
    "facebook":          "Meta FAIR 技术报告",
    "microsoft":         "MSR 技术报告",
    "apple":             "Apple ML 技术报告",
    "allenai":           "AI2 技术报告",
    "mistralai":         "Mistral 技术报告",
    "CohereForAI":       "Cohere 技术报告",
    "stabilityai":       "Stability AI 技术报告",
    # 中国
    "deepseek-ai":       "DeepSeek 技术报告",
    "Qwen":              "Qwen 技术报告",
    "bytedance-research":"字节跳动 Seed 技术报告",
    "ByteDance-Seed":    "字节跳动 Seed 技术报告",
    "THUDM":             "智谱/THUDM 技术报告",
    "moonshotai":        "Moonshot Kimi 技术报告",
    "01-ai":             "01.AI Yi 技术报告",
    "baichuan-inc":      "百川 技术报告",
}

# Polymarket polling interval (seconds)
POLYMARKET_POLL_INTERVAL = 1800   # 30 min

# Paper monitor polling intervals (seconds)
PAPERS_DAILY_INTERVAL    = 1800   # 30 min — refetch HF Daily Papers list
PAPERS_LAB_INTERVAL      = 3600   # 60 min — refetch each lab's HF org papers
PAPERS_REFRESH_INTERVAL  = 21600  # 6 hr  — refresh upvotes/scores for hot papers
PAPERS_HN_INTERVAL       = 3600   # 60 min — Algolia HN scan

# ── DeepSeek API ──────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# ── Storage ────────────────────────────────────────────────────────────────
DB_PATH = "news.db"

# ── Email notifications (Gmail SMTP) ───────────────────────────────────────
EMAIL_ENABLED      = os.getenv("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_SENDER       = os.getenv("EMAIL_SENDER", "")        # your Gmail address
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")  # Gmail App Password
# Comma-separated list of recipients, e.g. "a@x.com,b@x.com"
EMAIL_RECIPIENTS: list[str] = [r.strip() for r in os.getenv("EMAIL_RECIPIENT", "").split(",") if r.strip()]
EMAIL_RECIPIENT = EMAIL_RECIPIENTS[0] if EMAIL_RECIPIENTS else ""  # backwards compat

# ── Web dashboard ──────────────────────────────────────────────────────────
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

# ── Subscription / Auth ───────────────────────────────────────────────────
# BASE_URL is used in Magic Link emails to build absolute click-through URLs.
# In dev it defaults to http://localhost:WEB_PORT; in prod set BASE_URL to your
# public origin (e.g. https://news.example.com).
BASE_URL                = os.getenv("BASE_URL", f"http://localhost:{WEB_PORT}")
SESSION_COOKIE_NAME     = os.getenv("SESSION_COOKIE_NAME", "yunflow_session")
SESSION_TTL_DAYS        = int(os.getenv("SESSION_TTL_DAYS", "30"))
MAGIC_LINK_TTL_MINUTES  = int(os.getenv("MAGIC_LINK_TTL_MINUTES", "15"))

# Email verification code (alternative to Magic Link).
# Code is 6 numeric digits, stored as HMAC-SHA256(code, LOGIN_CODE_HMAC_KEY).
# LOGIN_CODE_HMAC_KEY MUST be set in prod — falls back to EMAIL_APP_PASSWORD
# so existing deployments don't need a new env var, but explicit is better.
LOGIN_CODE_TTL_MINUTES      = int(os.getenv("LOGIN_CODE_TTL_MINUTES", "10"))
LOGIN_CODE_MAX_ATTEMPTS     = int(os.getenv("LOGIN_CODE_MAX_ATTEMPTS", "5"))
LOGIN_CODE_COOLDOWN_SECONDS = int(os.getenv("LOGIN_CODE_COOLDOWN_SECONDS", "60"))
LOGIN_CODE_HMAC_KEY         = os.getenv("LOGIN_CODE_HMAC_KEY", "") or os.getenv("EMAIL_APP_PASSWORD", "")

# Admin allowlist — emails that can access /admin to review membership requests.
# Comma-separated, e.g. "you@example.com,partner@example.com". Empty list = no
# one can access /admin via the web (CLI invite.py --requests still works).
ADMIN_EMAILS: list[str] = [
    e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
]

# ── Earnings calendar (Finnhub) ────────────────────────────────────────────
# Free tier: 60 req/min. Earnings + IPO calendars are free; economic calendar is paid.
# Get a key at https://finnhub.io/dashboard (free signup).
FINNHUB_API_KEY      = os.getenv("FINNHUB_API_KEY", "")
EARNINGS_REFRESH_HOUR = 6        # UTC hour to refresh once a day
EARNINGS_WINDOW_DAYS  = 45       # how far forward to fetch
EARNINGS_PROFILE_TTL_DAYS = 30   # how long to cache /stock/profile2 data

# Default filters when query params are not supplied
# Filter logic: (market_cap >= min OR industry IN whitelist OR symbol IN watchlist)
EARNINGS_DEFAULT_MIN_CAP_M = 10000   # $10B+ by default (web page)
# Stricter threshold for email digests so a heavy earnings day doesn't
# produce a 100-row list — only mega-cap or watchlist names go to email.
EARNINGS_EMAIL_MIN_CAP_M   = 50000   # $50B+ for email
EARNINGS_INDUSTRIES_DEFAULT = [
    "Semiconductors", "Technology", "Software", "Media",
    "Communication Services", "Communication", "Internet Content & Information",
    "Consumer Cyclical", "Consumer Electronics",
    "Financial Services", "Banks", "Insurance",
    "Healthcare", "Biotechnology", "Pharmaceuticals",
    "Energy", "Aerospace & Defense", "Retail",
]
# Always-included hot tickers (mega-cap + China ADRs + AI/Web3 leaders)
EARNINGS_WATCHLIST = [
    # US mega-cap tech / "Magnificent 7"
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AMZN", "TSLA",
    # Other big tech / semis
    "AVGO", "AMD", "INTC", "ORCL", "CRM", "ADBE", "QCOM", "TXN", "ARM", "ASML",
    # AI / cloud / data infra
    "PLTR", "SNOW", "CRWD", "NET", "DDOG", "MDB", "PANW", "ZS", "S",
    # China ADRs (中概股 — 用户关注)
    "BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI", "BILI", "TCOM", "TME",
    # Crypto / Web3
    "COIN", "MSTR", "MARA", "RIOT", "HOOD",
    # Mega-cap finance / consumer benchmarks
    "JPM", "BAC", "GS", "WMT", "COST", "DIS", "NFLX", "UBER", "ABNB", "SPOT",
]
