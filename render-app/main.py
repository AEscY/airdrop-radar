import os, re, json, asyncio
from fastapi import FastAPI, Request
import httpx
from datetime import datetime

app = FastAPI()

# ========== 配置 ==========
ALPHA_DROPS_LIST = "https://alphadrops.net/alpha"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"
DROPS_API = "https://api.drops.bot/shared/v1/airdrops/evm/{addr}"

# ========== 评分引擎 ==========
def score_drop(name, funding_str, chains, is_claimable, is_premium):
    s = 0
    m = re.search(r"\$?([\d.]+)\s*([MB])?", funding_str or "")
    if m:
        amt = float(m.group(1))
        if m.group(2) == "B":
            amt *= 1000
        s += min(amt / 10, 30)
    if is_claimable:
        s += 25
    if "Solana" in (chains or []):
        s += 5
    if is_premium:
        s += 10
    return int(s)

# ========== 爬取 Alpha Drops 免费列表 ==========
async def fetch_alpha_drops():
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            ALPHA_DROPS_LIST,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AirdropRadar/1.0)"}
        )
        resp.raise_for_status()
        html = resp.text
    
    # 简化解析：提取项目名称、融资额、链信息
    drops = []
    # 匹配常见模式
    projects = re.findall(r'class="[^"]*project[^"]*"[^>]*>.*?<h[23][^>]*>(.*?)</h[23]>', html, re.DOTALL)
    
    # 备选：直接抓文本中的项目名（Alpha Drops 页面结构）
    known_projects = ["Polymarket", "Phoenix", "JTX", "Pascal", "Titan", "Cambria", 
                     "Backpack", "Monad", "Berachain", "Perpl", "Orbinum", "Canopy",
                     "Hypertrade", "Velvet", "Superform", "Plume", "Grass", "MetaMask"]
    
    for p in known_projects:
        if p in html:
            # 尝试提取融资额
            funding_match = re.search(rf'{p}.*?\$([\d.]+\s*[MB])', html, re.DOTALL)
            funding = funding_match.group(1) if funding_match else ""
            is_claimable = 'claimable' in html[html.find(p):html.find(p)+500].lower()
            is_premium = 'premium' in html[html.find(p):html.find(p)+500].lower()
            chains = []
            for chain in ["Solana", "Ethereum", "Base", "Arbitrum", "Polygon", "Abstract", "Monad"]:
                if chain.lower() in html[html.find(p):html.find(p)+500].lower():
                    chains.append(chain)
            sc = score_drop(p, funding, chains, is_claimable, is_premium)
            drops.append({
                "name": p, "funding_amount": funding,
                "blockchains": chains, "is_claimable": is_claimable,
                "premium": is_premium, "score": sc
            })
    
    # 去重
    seen = set()
    unique = []
    for d in drops:
        if d["name"] not in seen:
            seen.add(d["name"])
            unique.append(d)
    return unique

# ========== Telegram 推送 ==========
async def notify(token, chat_id, text):
    async with httpx.AsyncClient() as client:
        await client.post(
            TELEGRAM_SEND.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )

# ========== KV 读写 ==========
async def kv_get(env, key):
    kv_url = env.get("KV_URL")
    cf_token = env.get("CF_API_TOKEN")
    if not kv_url:
        return None
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{kv_url}/{key}",
            headers={"Authorization": f"Bearer {cf_token}"}
        )
        if r.status_code == 200:
            return r.text
    return None

async def kv_put(env, key, value):
    kv_url = env.get("KV_URL")
    cf_token = env.get("CF_API_TOKEN")
    if not kv_url:
        return
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{kv_url}/{key}",
            content=value,
            headers={"Authorization": f"Bearer {cf_token}"}
        )

# ========== 主雷达 ==========
async def run_radar():
    env = dict(os.environ)
    token = env.get("TG_BOT_TOKEN")
    chat_id = env.get("TG_CHAT_ID")
    
    # 1. 爬取
    drops = await fetch_alpha_drops()
    
    # 2. 读上次快照
    prev_raw = await kv_get(env, "last_snapshot")
    prev = json.loads(prev_raw) if prev_raw else {}
    
    # 3. diff + 过滤
    new_signals = []
    for d in drops:
        key = d["name"]
        prev_item = prev.get(key, {})
        changed = (prev_item.get("is_claimable") != d["is_claimable"] or
                   prev_item.get("score", 0) != d["score"])
        if (changed and d["score"] >= 70) or (d["is_claimable"] and d["score"] >= 60):
            new_signals.append(d)
    
    # 4. 高分优先，最多 5 条
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    pushed = 0
    for d in new_signals[:5]:
        emoji = "🚨" if d["score"] >= 80 else "⚠️"
        msg = f"{emoji} [{d['score']}分] {d['name']}\n"
        msg += f"链: {', '.join(d['blockchains']) or 'N/A'}\n"
        msg += f"融资: {d['funding_amount'] or 'N/A'}\n"
        msg += f"状态: {'✅可领取' if d['is_claimable'] else '活跃'}\n"
        await notify(token, chat_id, msg)
        pushed += 1
        await asyncio.sleep(0.5)  # 避免 Telegram 限速
    
    # 5. 存快照
    snapshot = {d["name"]: {"is_claimable": d["is_claimable"], "score": d["score"]} 
                for d in drops}
    await kv_put(env, "last_snapshot", json.dumps(snapshot))
    
    return f"OK: 扫描 {len(drops)} 个空投，推送 {pushed} 条新信号"

# ========== 端点 ==========
@app.get("/health")
async def health():
    return {"status": "alive", "ts": datetime.now().isoformat()}

@app.post("/collect")
async def collect(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    result = await run_radar()
    return {"result": result}

@app.get("/run")
async def manual_run():
    result = await run_radar()
    return {"result": result}
