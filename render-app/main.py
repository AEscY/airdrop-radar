"""
Web3 空投雷达 - 终极版 (异步并发 + 多源聚合 + 自动化基座)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import Column, String, DateTime, Float, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------- 配置 ----------
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///airdrop.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Pydantic 数据模型 ----------
class Project(BaseModel):
    name: str
    chain: str = "Unknown"
    funding: str = "N/A"
    claimable: bool = False
    score: float = 0.0
    url: Optional[str] = None
    source: str = "unknown"

# ---------- 异步数据库 ORM ----------
Base = declarative_base()

class PushedProject(Base):
    __tablename__ = 'pushed'
    name = Column(String, primary_key=True)
    source = Column(String)
    score = Column(Float)
    pushed_at = Column(DateTime, default=datetime.utcnow)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@asynccontextmanager
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
        await session.commit()

# ---------- 数据采集：异步并发 ----------
async def fetch_alphadrops(client: httpx.AsyncClient) -> List[Project]:
    """从 AlphaDrops 采集项目（增强健壮性）"""
    try:
        resp = await client.get("https://alphadrops.io/", timeout=12.0)
        resp.raise_for_status()
        from parsel import Selector
        sel = Selector(text=resp.text)
        # 多重选择器
        items = sel.css(".project-item, .card-project, [class*='project']")
        projects = []
        for item in items[:20]:
            name = item.css(".project-name::text, h4::text, .name::text").get(default="").strip()
            if not name:
                continue
            chain = item.css(".chain::text, .chain-tag::text, .badge-chain::text").get(default="").strip()
            funding = item.css(".funding::text, .amount::text").get(default="").strip()
            claimable_tag = item.css(".claimable::text, .status::text").get(default="")
            claimable = bool(claimable_tag and ("claim" in claimable_tag.lower() or "available" in claimable_tag.lower()))
            
            # 评分（可根据融资额、公链、可领取性加权）
            score = 50.0
            if any(x in chain.lower() for x in ["ethereum", "arbitrum", "optimism", "polygon", "base"]):
                score += 20
            if claimable:
                score += 30
            if funding and '$' in funding:
                # 简单判断金额大小
                import re
                nums = re.findall(r'[\d.]+', funding.replace(',', ''))
                if nums:
                    val = float(nums[0])
                    if 'B' in funding:
                        val *= 1000
                    elif 'M' in funding:
                        pass
                    elif 'K' in funding:
                        val /= 1000
                    if val > 10:
                        score += 25
                    elif val > 1:
                        score += 15
                    else:
                        score += 5
            
            url = item.css("a::attr(href)").get()
            projects.append(Project(
                name=name,
                chain=chain,
                funding=funding,
                claimable=claimable,
                score=score,
                source="alphadrops",
                url=url
            ))
        return projects
    except Exception as e:
        logger.error(f"AlphaDrops error: {e}")
        return []

async def fetch_github_trending() -> List[Project]:
    """从 GitHub Trending 获取 web3 相关仓库"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://github.com/trending?l=python&l=javascript&q=web3", timeout=10.0)
            resp.raise_for_status()
            from parsel import Selector
            sel = Selector(text=resp.text)
            repos = sel.css("article.Box-row")
            projects = []
            for repo in repos[:5]:
                name = repo.css("h1 a::text").get(default="").strip().replace("\n", "").replace(" ", "")
                if not name:
                    continue
                desc = repo.css("p::text").get(default="").strip()
                if "web3" in desc.lower() or "blockchain" in desc.lower() or "defi" in desc.lower():
                    projects.append(Project(
                        name=name,
                        source="github",
                        score=30,
                        url=f"https://github.com/{name}"
                    ))
            return projects
    except Exception as e:
        logger.error(f"github trending error: {e}")
        return []

async def collect_all() -> List[Project]:
    """并发采集所有数据源，去重并排序"""
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        tasks = [
            fetch_alphadrops(client),
            fetch_github_trending(),
            # 未来可添加更多数据源函数
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_projects = []
    for res in results:
        if isinstance(res, list):
            all_projects.extend(res)
        else:
            logger.error(f"Collect error: {res}")
    
    # 按名称去重，保留最高评分
    unique = {}
    for p in all_projects:
        if p.name not in unique or p.score > unique[p.name].score:
            unique[p.name] = p
    # 按评分降序
    return sorted(unique.values(), key=lambda x: x.score, reverse=True)

# ---------- 消息格式化 ----------
def format_project(p: Project) -> str:
    msg = f"🚀 *{p.name}*\n"
    msg += f"💰 融资: {p.funding}\n"
    msg += f"🔗 公链: {p.chain}\n"
    msg += f"🎁 可领取: {'✅' if p.claimable else '❌'}\n"
    msg += f"⭐ 评分: {p.score}\n"
    msg += f"📡 来源: {p.source}\n"
    if p.url:
        msg += f"🔗 [详情]({p.url})"
    return msg

# ---------- 推送与去重 ----------
async def send_project(p: Project, bot):
    """发送单个项目到 Telegram，并去重"""
    async for db in get_db():
        stmt = select(PushedProject).where(PushedProject.name == p.name)
        exists = (await db.execute(stmt)).scalar_one_or_none()
        if not exists:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=format_project(p),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            db.add(PushedProject(name=p.name, source=p.source, score=p.score))
            return True
    return False

# ---------- Telegram Bot 命令 ----------
app_tg = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛸 **Web3 终极雷达已启动**\n"
        "命令:\n"
        "/scan - 立即全量扫描并推送\n"
        "/status - 查看统计"
    )

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ 正在并发采集全网数据...")
    projects = await collect_all()
    filtered = [p for p in projects if p.score >= 60]  # 阈值
    if not filtered:
        await msg.edit_text("暂无高分项目。")
        return

    count = 0
    for p in filtered[:10]:  # 限制单次推送数量
        if await send_project(p, app_tg.bot):
            count += 1
            await asyncio.sleep(0.5)  # 防频率限制
    await update.message.reply_text(f"✅ 扫描完成，新增推送 {count} 个项目。")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async for db in get_db():
        total = (await db.execute(select(PushedProject))).scalar()
    await update.message.reply_text(f"📊 已推送项目总数: {total or 0}")

app_tg.add_handler(CommandHandler("start", start))
app_tg.add_handler(CommandHandler("scan", scan))
app_tg.add_handler(CommandHandler("status", status))

# ---------- FastAPI Web 服务 ----------
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, app_tg.bot)
    await app_tg.process_update(update)
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/")
async def root():
    return "Web3 Radar Ultimate is running."

@app.on_event("startup")
async def startup():
    # 创建数据库表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 设置 Webhook
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "https://default.onrender.com") + "/webhook"
    await app_tg.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

# ---------- 独立采集模式（用于 GitHub Actions） ----------
async def run_collect():
    logger.info("开始定时采集...")
    projects = await collect_all()
    filtered = [p for p in projects if p.score >= 60]
    count = 0
    for p in filtered:
        if await send_project(p, app_tg.bot):
            count += 1
            await asyncio.sleep(0.5)
    logger.info(f"定时采集完成，推送 {count} 个新项目。")

if __name__ == "__main__":
    if "--collect" in sys.argv:
        asyncio.run(run_collect())
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))