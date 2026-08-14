"""
Airdrop Radar Bot - 多源聚合 + 链上监听 + 智能评分
支持数据源：AlphaDrops, Binance Alpha, DropsEarn RSS, GitHub Trending
兼容 SQLite / PostgreSQL
"""

import asyncio
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from contextlib import contextmanager

import requests
import feedparser
from parsel import Selector
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------- 数据库适配层 ----------
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
import sqlite3

# 日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- 配置 ----------
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")  # 默认推送目标
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///airdrop.db")  # 支持 PostgreSQL
INFURA_PROJECT_ID = os.getenv("INFURA_PROJECT_ID")  # 可选，用于链上监听
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")  # 可选

# ---------- SQLAlchemy 模型 ----------
Base = declarative_base()

class PushedProject(Base):
    __tablename__ = 'pushed'
    name = Column(String, primary_key=True)
    pushed_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String, default='alphadrops')
    score = Column(Float, default=0.0)

class User(Base):
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True)
    wallet_address = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# 引擎和会话
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# ---------- 数据源采集函数 ----------

def fetch_alphadrops() -> List[Dict]:
    """采集 AlphaDrops（改进：多重选择器回退）"""
    url = "https://alphadrops.io/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        sel = Selector(text=resp.text)
        projects = []
        # 多重选择器尝试
        selectors = [
            ".project-item",
            ".card-project",
            "[class*='project']"
        ]
        items = []
        for sel_str in selectors:
            items = sel.css(sel_str)
            if items:
                break
        if not items:
            logger.warning("AlphaDrops: No items found with any selector")
            return []

        for item in items:
            name = item.css(".project-name::text, .name::text, h4::text").get(default="").strip()
            if not name:
                continue
            chain = item.css(".chain::text, .chain-tag::text, .badge-chain::text").get(default="").strip()
            funding = item.css(".funding::text, .amount::text").get(default="").strip()
            claimable_tag = item.css(".claimable::text, .status::text").get(default="")
            claimable = bool(claimable_tag and ("claim" in claimable_tag.lower() or "available" in claimable_tag.lower()))
            # 融资额解析
            funding_val = 0.0
            if funding:
                match = re.search(r'[\d.]+', funding.replace(',', ''))
                if match:
                    funding_val = float(match.group())
                    if 'B' in funding:
                        funding_val *= 1000
                    elif 'M' in funding:
                        pass
                    elif 'K' in funding:
                        funding_val /= 1000
            # 评分（可扩展）
            score = 0.0
            if funding_val > 0:
                score += min(funding_val * 5, 50)
            if claimable:
                score += 20
            if chain and chain.lower() in ["ethereum", "arbitrum", "optimism", "polygon", "zksync", "base"]:
                score += 10

            projects.append({
                "name": name,
                "chain": chain,
                "funding": funding,
                "claimable": claimable,
                "score": score,
                "url": item.css("a::attr(href)").get(),
                "source": "alphadrops"
            })
        return projects
    except Exception as e:
        logger.error(f"AlphaDrops fetch error: {e}")
        return []

def fetch_binance_alpha() -> List[Dict]:
    """采集 Binance Alpha（模拟，实际需适配页面结构）"""
    # 注意：Binance Alpha 页面需要动态加载，此函数为示例框架
    # 实际可使用 Selenium 或 解析其API（若有）
    # 这里返回空列表，如需实现请参考官方页面调整
    logger.info("Binance Alpha fetch not implemented yet")
    return []

def fetch_dropsearn_rss() -> List[Dict]:
    """通过 RSS 获取 DropsEarn 数据"""
    rss_url = "https://dropsearn.com/feed"  # 示例，实际需确认RSS地址
    try:
        feed = feedparser.parse(rss_url)
        projects = []
        for entry in feed.entries[:20]:
            title = entry.title
            link = entry.link
            published = entry.published
            # 简单评分
            score = 50  # 默认分
            projects.append({
                "name": title,
                "chain": "Unknown",
                "funding": "N/A",
                "claimable": True,
                "score": score,
                "url": link,
                "source": "dropsearn"
            })
        return projects
    except Exception as e:
        logger.error(f"DropsEarn RSS error: {e}")
        return []

def fetch_github_trending() -> List[Dict]:
    """监控 GitHub Trending 中 web3 相关仓库"""
    url = "https://github.com/trending?l=javascript&l=python&l=rust&l=go&q=web3"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        sel = Selector(text=resp.text)
        repos = sel.css("article.Box-row")
        projects = []
        for repo in repos[:10]:
            name = repo.css("h1 a::text").get(default="").strip().replace("\n", "").replace(" ", "")
            if not name:
                continue
            desc = repo.css("p::text").get(default="").strip()
            if "web3" in desc.lower() or "blockchain" in desc.lower():
                score = 30  # 基础分
                projects.append({
                    "name": name,
                    "chain": "GitHub",
                    "funding": "N/A",
                    "claimable": False,
                    "score": score,
                    "url": f"https://github.com/{name}",
                    "source": "github"
                })
        return projects
    except Exception as e:
        logger.error(f"github trending error: {e}")
        return []

# ---------- 链上监听（可选，需 Infura） ----------
def fetch_onchain_contracts() -> List[Dict]:
    """监听新 ERC-20 合约部署（简化版，需实现）"""
    if not INFURA_PROJECT_ID:
        return []
    # 示例：使用 web3.py 监听 pending 交易中的合约创建
    # 实际需要复杂逻辑，此处返回空
    return []

# ---------- 数据聚合与去重 ----------
def collect_all_sources() -> List[Dict]:
    """聚合所有数据源"""
    all_projects = []
    all_projects.extend(fetch_alphadrops())
    all_projects.extend(fetch_binance_alpha())
    all_projects.extend(fetch_dropsearn_rss())
    all_projects.extend(fetch_github_trending())
    all_projects.extend(fetch_onchain_contracts())

    # 按名称去重（保留最高评分）
    unique = {}
    for p in all_projects:
        name = p['name']
        if name not in unique or p['score'] > unique[name]['score']:
            unique[name] = p
    return list(unique.values())

def filter_high_value(projects: List[Dict]) -> List[Dict]:
    """筛选高价值（评分>=70 或 可领取且>=60）"""
    return [p for p in projects if p["score"] >= 70 or (p["claimable"] and p["score"] >= 60)]

# ---------- 推送与去重 ----------
def is_pushed(name: str) -> bool:
    with get_db() as db:
        return db.query(PushedProject).filter_by(name=name).first() is not None

def mark_pushed(name: str, source: str, score: float):
    with get_db() as db:
        db.merge(PushedProject(name=name, source=source, score=score))

def format_message(project: Dict) -> str:
    msg = f"🚀 *{project['name']}*\n"
    msg += f"🔗 Chain: {project['chain'] or 'Unknown'}\n"
    msg += f"💰 Funding: {project['funding'] or 'N/A'}\n"
    msg += f"🎁 Claimable: {'✅ Yes' if project['claimable'] else '❌ No'}\n"
    msg += f"⭐ Score: {project['score']}\n"
    msg += f"📡 Source: {project.get('source', 'unknown')}\n"
    if project['url']:
        msg += f"🔗 [More Info]({project['url']})"
    return msg

# ---------- Telegram Bot ----------
application = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Airdrop Radar Bot (Enhanced)\n"
        "Commands:\n"
        "/scan - Manual scan\n"
        "/status - Stats\n"
        "/bind <wallet> - Bind wallet"
    )

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scanning all sources...")
    projects = collect_all_sources()
    filtered = filter_high_value(projects)
    if not filtered:
        await update.message.reply_text("No high-value projects found.")
        return
    sent = 0
    for p in filtered[:10]:
        if not is_pushed(p['name']):
            await update.message.reply_text(format_message(p), parse_mode="Markdown", disable_web_page_preview=True)
            mark_pushed(p['name'], p.get('source', 'unknown'), p['score'])
            sent += 1
    await update.message.reply_text(f"Done. Pushed {sent} new projects.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as db:
        pushed_count = db.query(PushedProject).count()
        users_count = db.query(User).count()
    await update.message.reply_text(f"📊 Total pushed: {pushed_count}\n👥 Users: {users_count}")

async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /bind <wallet_address>")
        return
    wallet = context.args[0]
    with get_db() as db:
        db.merge(User(user_id=update.effective_user.id, wallet_address=wallet))
    await update.message.reply_text(f"Wallet bound: {wallet}")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("bind", bind))

# ---------- FastAPI 服务 ----------
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return "Airdrop Radar Bot (Enhanced) is running."

@app.on_event("startup")
async def on_startup():
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com") + "/webhook"
    await application.bot.set_webhook(webhook_url)
    await application.bot.set_my_commands([
        ("start", "Start"),
        ("scan", "Scan now"),
        ("status", "Stats"),
        ("bind", "Bind wallet")
    ])
    logger.info("Webhook set and commands registered.")

# ---------- 独立运行模式（用于GitHub Actions） ----------
def run_collect():
    logger.info("Collect mode started...")
    projects = collect_all_sources()
    filtered = filter_high_value(projects)
    sent = 0
    for p in filtered:
        if not is_pushed(p['name']):
            asyncio.run(send_message(p))
            mark_pushed(p['name'], p.get('source', 'unknown'), p['score'])
            sent += 1
            time.sleep(0.5)  # 防限流
    logger.info(f"Collect finished. Pushed {sent} new projects.")

async def send_message(project):
    bot = application.bot
    try:
        await bot.send_message(chat_id=CHAT_ID, text=format_message(project), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Send error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--collect":
        run_collect()
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))