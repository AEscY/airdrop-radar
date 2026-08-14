"""
Web3 空投雷达 - 终极版 (异步并发 + 多源聚合 + 自动化基座)
"""
import asyncio, logging, os, sys
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

# ---------- 数据模型 ----------
class Project(BaseModel):
    name: str
    chain: str = "Unknown"
    funding: str = "N/A"
    claimable: bool = False
    score: float = 0.0
    url: Optional[str] = None
    source: str = "unknown"

# ---------- 异步数据库 ----------
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

# ---------- 数据采集（异步并发） ----------
async def fetch_alphadrops(client: httpx.AsyncClient) -> List[Project]:
    try:
        resp = await client.get("https://alphadrops.io/", timeout=12.0)
        from parsel import Selector
        sel = Selector(text=resp.text)
        items = sel.css(".project-item, .card-project")
        projects = []
        for item in items[:20]:
            name = item.css(".project-name::text, h4::text").get(default="").strip()
            if not name: continue
            chain = item.css(".chain::text, .chain-tag::text").get(default="").strip()
            funding = item.css(".funding::text, .amount::text").get(default="").strip()
            claimable = bool(item.css(".claimable::text").get())
            score = 50.0
            if any(x in chain.lower() for x in ["ethereum", "arbitrum", "optimism"]): score += 20
            if claimable: score += 30
            if funding and '$' in funding: score += 15
            projects.append(Project(name=name, chain=chain, funding=funding, claimable=claimable, score=score, source="alphadrops", url=item.css("a::attr(href)").get()))
        return projects
    except Exception as e:
        logger.error(f"AlphaDrops error: {e}")
        return []

async def fetch_github() -> List[Project]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://github.com/trending?l=python&q=web3", timeout=10.0)
            from parsel import Selector
            sel = Selector(text=resp.text)
            projects = []
            for repo in sel.css("article.Box-row")[:5]:
                name = repo.css("h1 a::text").get(default="").strip().replace("\n", "").replace(" ", "")
                if name and "web3" in name.lower():
                    projects.append(Project(name=name, source="github", score=30, url=f"https://github.com/{name}"))
            return projects
    except Exception as e:
        logger.error(f"github error: {e}")
        return []

async def collect_all() -> List[Project]:
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        results = await asyncio.gather(fetch_alphadrops(client), fetch_github(), return_exceptions=True)
    all_projects = []
    for res in results:
        if isinstance(res, list): all_projects.extend(res)
    unique = {}
    for p in all_projects:
        if p.name not in unique or p.score > unique[p.name].score:
            unique[p.name] = p
    return sorted(unique.values(), key=lambda x: x.score, reverse=True)

# ---------- 推送 ----------
def format_msg(p: Project) -> str:
    return f"🚀 *{p.name}*\n💰 {p.funding}\n🔗 {p.chain}\n⭐ 评分: {p.score}\n📡 {p.source}\n🔗 [详情]({p.url})"

# ---------- Telegram 命令 ----------
app_tg = Application.builder().token(TOKEN).build()
async def start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text("🛸 终极雷达已启动！\n命令: /scan 立即扫描")
async def scan(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await upd.message.reply_text("⏳ 并发采集全网数据...")
    projects = await collect_all()
    filtered = [p for p in projects if p.score >= 60]
    if not filtered:
        await msg.edit_text("暂无高分项目。")
        return
    count = 0
    async for db in get_db():
        for p in filtered[:8]:
            exists = (await db.execute(select(PushedProject).where(PushedProject.name == p.name))).scalar_one_or_none()
            if not exists:
                await upd.message.reply_text(format_msg(p), parse_mode="Markdown", disable_web_page_preview=True)
                db.add(PushedProject(name=p.name, source=p.source, score=p.score))
                count += 1
        await db.commit()
    await upd.message.reply_text(f"✅ 完成！新增推送 {count} 个项目。")
app_tg.add_handler(CommandHandler("start", start))
app_tg.add_handler(CommandHandler("scan", scan))

# ---------- FastAPI ----------
app = FastAPI()
@app.post("/webhook")
async def webhook(req: Request):
    await app_tg.process_update(Update.de_json(await req.json(), app_tg.bot))
    return Response(status_code=200)
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await app_tg.bot.set_webhook(os.getenv("RENDER_EXTERNAL_URL", "https://default.onrender.com") + "/webhook")

# ---------- Actions 采集模式 ----------
async def run_collect():
    logger.info("开始采集...")
    projects = await collect_all()
    filtered = [p for p in projects if p.score >= 60]
    count = 0
    async for db in get_db():
        for p in filtered:
            exists = (await db.execute(select(PushedProject).where(PushedProject.name == p.name))).scalar_one_or_none()
            if not exists:
                await app_tg.bot.send_message(chat_id=CHAT_ID, text=format_msg(p), parse_mode="Markdown", disable_web_page_preview=True)
                db.add(PushedProject(name=p.name, source=p.source, score=p.score))
                count += 1
        await db.commit()
    logger.info(f"推送完成，共 {count} 个新项目。")

if __name__ == "__main__":
    if "--collect" in sys.argv:
        asyncio.run(run_collect())
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))