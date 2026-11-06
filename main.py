import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from statistics import mean
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
# Parse .env safely: inline comments are allowed, real process environment takes priority.
# Never log secrets.
def load_env(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key,value=line.split('=',1)
        key=key.strip()
        if not key.isidentifier():
            continue
        value=value.strip()
        if value.startswith(('"', "'")):
            quote=value[0]
            end=value.find(quote,1)
            if end!=-1:
                value=value[1:end]
        else:
            value=value.split(' #',1)[0].strip()
        os.environ.setdefault(key,value)
load_env(ROOT / '.env')
NETWORK = os.getenv('BYBIT_ENV','testnet').strip().lower()
if NETWORK not in ('mainnet','testnet'):
    raise RuntimeError('BYBIT_ENV must be mainnet or testnet')
DB = ROOT / ('ayla_mainnet_paper.db' if NETWORK == 'mainnet' else 'ayla_v04.db')
# PAPER ONLY: no authenticated Bybit order endpoints in this application
BASE_URLS = {'mainnet':'https://api.bybit.com','testnet':'https://api-testnet.bybit.com'}

SYMBOL = 'SOLUSDT'
FEE = 0.001
GRID_STEPS = 6
GRID_GAP = 0.012
ORDER_USDT = 10.0
RESERVE_USDT = 20.0
MAX_DRAWDOWN = 0.08
log = logging.getLogger('ayla')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
state = {'price': None, 'bid': None, 'ask': None, 'updated': None, 'error': None, 'running': False, 'last_success': None}

def connection():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connection() as c:
        c.execute('CREATE TABLE IF NOT EXISTS wallet (id INTEGER PRIMARY KEY CHECK(id=1), usdt REAL NOT NULL, sol REAL NOT NULL, initial REAL NOT NULL, peak REAL NOT NULL)')
        c.execute('INSERT OR IGNORE INTO wallet VALUES (1,100,0,100,100)')
        c.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), center REAL NOT NULL, created TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, side TEXT NOT NULL, level INTEGER NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, filled TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL, order_id INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS prices (ts TEXT NOT NULL, price REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS equity_history (ts TEXT NOT NULL, equity REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), gap REAL NOT NULL, steps INTEGER NOT NULL, order_usdt REAL NOT NULL, reserve REAL NOT NULL, adaptive INTEGER NOT NULL)')
        c.execute('INSERT OR IGNORE INTO config VALUES (1,0.012,6,10,20,0)')
        c.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('network',os.getenv('BYBIT_ENV','mainnet').lower() if os.getenv('BYBIT_ENV','mainnet').lower() in BASE_URLS else 'mainnet'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('paper_running','0'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('tg_enabled','0'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('tg_daily','1'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('tg_trades','1'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',('tg_hour','21'))
        c.execute('CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS market_samples (ts TEXT PRIMARY KEY, price REAL NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL, equity REAL NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id)')
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)', ('outside_since',''))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)', ('alert_outside_hours','6'))
        c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)', ('alert_no_trade_hours','24'))

def setting(key, default=''):
    if key == 'network':
        return NETWORK
    with connection() as c:
        row=c.execute('SELECT value FROM app_settings WHERE key=?',(key,)).fetchone()
        return row['value'] if row else default

def put_settings(items):
    with connection() as c:
        for k,v in items.items():
            c.execute('INSERT OR REPLACE INTO app_settings VALUES (?,?)',(k,str(v)))

async def telegram_send(message):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
    chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:
        raise RuntimeError('Вкажіть TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у .env')
    async with httpx.AsyncClient(timeout=12) as client:
        r=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':message})
        r.raise_for_status()
        j=r.json()
        if not j.get('ok'): raise RuntimeError('Telegram API error')

def daily_text():
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    tz=ZoneInfo('Europe/Kyiv')
    today=datetime.now(tz).date()
    start=datetime.combine(today,datetime.min.time(),tzinfo=tz).astimezone(timezone.utc).isoformat()
    end=datetime.combine(today+timedelta(days=1),datetime.min.time(),tzinfo=tz).astimezone(timezone.utc).isoformat()
    with connection() as c:
        rows=c.execute('SELECT side,price,qty,fee FROM trades WHERE ts>=? AND ts<?',(start,end)).fetchall()
        w=c.execute('SELECT * FROM wallet WHERE id=1').fetchone()
    fees=sum(r['fee'] for r in rows)
    equity=w['usdt']+w['sol']*(state['bid'] or state['price'] or 0)
    return (f'🐶 Ayla Grid · {today} · PAPER\nМережа: {setting("network")}\n'
            f'Виконань сьогодні: {len(rows)} (BUY {sum(r["side"]=="BUY" for r in rows)}, SELL {sum(r["side"]=="SELL" for r in rows)})\n'
            f'Комісії за день: {fees:.4f} USDT\nПортфель: {equity:.4f} USDT\n'
            f'Загальний PnL від старту: {equity-w["initial"]:+.4f} USDT\n'
            f'Режим: {"RUNNING" if state["running"] else "PAUSED"}')

async def notification_loop():
    from zoneinfo import ZoneInfo
    while True:
        try:
            if setting('tg_enabled')=='1':
                with connection() as c:
                    pending=c.execute("SELECT t.id,t.ts,t.side,t.price,t.qty,t.fee FROM trades t LEFT JOIN notifications n ON n.key='trade:'||t.id WHERE n.key IS NULL ORDER BY t.id LIMIT 20").fetchall()
                if setting('tg_trades')=='1':
                    for t in pending:
                        await telegram_send(f'🐶 PAPER {t["side"]} {t["qty"]:.6f} SOL @ {t["price"]:.4f} USDT\nКомісія: {t["fee"]:.5f} USDT\n{setting("network")}')
                        with connection() as c: c.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',(f'trade:{t["id"]}',now()))
                else:
                    with connection() as c:
                        for t in pending:c.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',(f'trade:{t["id"]}',now()))
                local=datetime.now(ZoneInfo('Europe/Kyiv'))
                key='daily:'+local.date().isoformat()
                if setting('tg_daily')=='1' and local.hour>=int(setting('tg_hour','21')):
                    with connection() as c: sent=c.execute('SELECT 1 FROM notifications WHERE key=?',(key,)).fetchone()
                    if not sent:
                        await telegram_send(daily_text())
                        with connection() as c:c.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',(key,now()))
        except Exception as exc: log.warning('Telegram notification failed: %s',exc)
        await asyncio.sleep(30)

def now():
    return datetime.now(timezone.utc).isoformat()

def make_grid(c, price):
    """Initial buy-only ladder below market; sell orders are funded by completed buys."""
    if c.execute('SELECT COUNT(*) FROM settings').fetchone()[0]:
        return
    cfg = c.execute('SELECT * FROM config WHERE id=1').fetchone()
    gap = cfg['gap']
    if cfg['adaptive']:
        recent = [r[0] for r in c.execute('SELECT price FROM prices ORDER BY rowid DESC LIMIT 360')][::-1]
        if len(recent) >= 30:
            movements = [abs(recent[i]/recent[i-1]-1) for i in range(1,len(recent))]
            gap = max(0.006, min(0.03, mean(movements)*8))
    c.execute('INSERT INTO settings VALUES (1,?,?)', (price, now()))
    c.execute('UPDATE config SET gap=? WHERE id=1',(gap,))
    for level in range(1, cfg['steps'] + 1):
        buy = round(price * (1 - gap * level), 6)
        qty = cfg['order_usdt'] / (buy * (1 + FEE))
        c.execute('INSERT INTO orders(side,level,price,qty,status,created) VALUES (?,?,?,?,?,?)', ('BUY',level,buy,qty,'OPEN',now()))

def simulate(price, bid, ask):
    with connection() as c:
        c.execute('INSERT INTO prices VALUES (?,?)', (now(),price))
        c.execute('DELETE FROM prices WHERE rowid NOT IN (SELECT rowid FROM prices ORDER BY rowid DESC LIMIT 1500)')
        if not state['running']:
            return
        make_grid(c, price)
        wallet = c.execute('SELECT * FROM wallet WHERE id=1').fetchone()
        usdt, sol, initial, peak = (wallet[k] for k in ('usdt','sol','initial','peak'))
        # Safe auto-recenter: only when flat (no SOL), all orders BUY, and
        # market has stayed outside the ladder for >=6 hours. Existing trades
        # and balances are preserved; no forced sale or order cancellation with SOL.
        if os.getenv('AYLA_AUTO_RECENTER','1') == '1':
            open_orders=c.execute("SELECT side,price FROM orders WHERE status='OPEN'").fetchall()
            outside=(bool(open_orders) and all(o['side']=='BUY' for o in open_orders)
                     and (price<min(o['price'] for o in open_orders)
                          or price>max(o['price'] for o in open_orders)))
            old=c.execute("SELECT value FROM app_settings WHERE key='outside_since'").fetchone()
            old_ts=old['value'] if old else ''
            if outside and sol<1e-8:
                if not old_ts:
                    c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('outside_since',?)",(now(),))
                elif (datetime.now(timezone.utc)-datetime.fromisoformat(old_ts)).total_seconds()>=6*3600:
                    c.execute("DELETE FROM orders WHERE status='OPEN' AND side='BUY'")
                    c.execute('DELETE FROM settings')
                    make_grid(c,price)
                    c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('outside_since','')")
                    log.warning('PAPER safe auto-recenter at %.4f (flat portfolio, 6h outside)',price)
            elif old_ts:
                c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('outside_since','')")
        cfg = c.execute('SELECT * FROM config WHERE id=1').fetchone()
        equity = usdt + sol * bid
        peak = max(peak, equity)
        if equity <= peak * (1 - MAX_DRAWDOWN):
            state['running'] = False
            put_settings({'paper_running':'0'})
            state['error'] = 'Захист: просадка портфеля досягла 8%. Торгівлю зупинено.'
            c.execute('UPDATE wallet SET peak=? WHERE id=1', (peak,))
            return
        # One execution per price update prevents unrealistic cascades on large price jumps.
        rows = c.execute('SELECT * FROM orders WHERE status=? ORDER BY id', ('OPEN',)).fetchall()
        for order in rows:
            side, limit_price, qty = order['side'], order['price'], order['qty']
            if side == 'BUY' and ask <= limit_price and usdt >= qty * ask * (1 + FEE) + cfg['reserve']:
                fill = ask  # optimistic simulated execution; spread is included, slippage is not
                fee = qty * fill * FEE
                usdt -= qty * fill + fee
                sol += qty
                counterpart = 'SELL'
                next_price = round(limit_price * (1 + cfg['gap']), 6)
            elif side == 'SELL' and bid >= limit_price and sol + 1e-10 >= qty:
                fill = bid
                fee = qty * fill * FEE
                usdt += qty * fill - fee
                sol -= qty
                counterpart = 'BUY'
                next_price = round(limit_price / (1 + cfg['gap']), 6)
            else:
                continue
            ts = now()
            c.execute('UPDATE orders SET status=?,filled=? WHERE id=?', ('FILLED',ts,order['id']))
            c.execute('INSERT INTO trades(ts,side,price,qty,fee,order_id) VALUES (?,?,?,?,?,?)', (ts,side,fill,qty,fee,order['id']))
            c.execute('INSERT INTO orders(side,level,price,qty,status,created) VALUES (?,?,?,?,?,?)', (counterpart,order['level'],next_price,qty,'OPEN',ts))
            log.info('PAPER %s %.6f SOL at %.4f USDT',side,qty,fill)
            break
        c.execute('UPDATE wallet SET usdt=?,sol=?,peak=? WHERE id=1',(usdt,max(0,sol),peak))
        c.execute('INSERT INTO equity_history VALUES (?,?)',(now(),usdt+max(0,sol)*bid))
        c.execute('DELETE FROM equity_history WHERE rowid NOT IN (SELECT rowid FROM equity_history ORDER BY rowid DESC LIMIT 10000)')
        # Durable 15-minute snapshots for future analytics (no 1500-tick cap).
        bucket = datetime.now(timezone.utc).replace(minute=(datetime.now(timezone.utc).minute//15)*15,second=0,microsecond=0).isoformat()
        c.execute('INSERT OR REPLACE INTO market_samples(ts,price,bid,ask,equity) VALUES (?,?,?,?,?)',
                  (bucket,price,bid,ask,usdt+max(0,sol)*bid))

async def market_loop():
    async with httpx.AsyncClient(timeout=12, trust_env=True) as client:
        while True:
            try:
                r = await client.get(BASE_URLS[setting('network')]+ '/v5/market/tickers', params={'category':'spot','symbol':SYMBOL})
                r.raise_for_status()
                data = r.json()
                if data.get('retCode') != 0:
                    raise RuntimeError(data.get('retMsg','Bybit API error'))
                ticker = data['result']['list'][0]
                price = float(ticker['lastPrice'])
                bid = float(ticker['bid1Price'])
                ask = float(ticker['ask1Price'])
                if min(price,bid,ask) <= 0 or bid > ask or (ask-bid)/price > 0.05:
                    raise ValueError('Некоректні ціни Bybit')
                state.update(price=price,bid=bid,ask=ask,updated=now(),last_success=now(),error=None)
                simulate(price,bid,ask)
            except Exception as exc:
                state['error'] = f'{type(exc).__name__}: {exc}'
                # Keep user intent on transient API failures; simulate() runs only on fresh valid quotes.
                # No fills are replayed for missing ticks.
                log.exception('Market tick / paper simulation failed (will retry): %s', exc)
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app):
    init_db()
    state['running'] = setting('paper_running','0') == '1'
    task = asyncio.create_task(market_loop())
    notifier = asyncio.create_task(notification_loop())
    alert_task = asyncio.create_task(strategy_alerts())
    try:
        yield
    finally:
        task.cancel()
        notifier.cancel()
        alert_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try: await notifier
        except asyncio.CancelledError: pass
        try: await alert_task
        except asyncio.CancelledError: pass

app = FastAPI(title='Ayla Grid Bot', version='0.9.0', lifespan=lifespan)

@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html', headers={'Cache-Control':'no-store'})

@app.get('/api/status')
def status():
    with connection() as c:
        w = c.execute('SELECT * FROM wallet WHERE id=1').fetchone()
        center = c.execute('SELECT center FROM settings WHERE id=1').fetchone()
        orders = [dict(r) for r in c.execute('SELECT * FROM orders WHERE status=? ORDER BY price', ('OPEN',))]
        trades = [dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 100')]
        prices = [dict(r) for r in c.execute('SELECT * FROM (SELECT rowid,ts,price FROM prices ORDER BY rowid DESC LIMIT 300) ORDER BY rowid')]
        cfg = dict(c.execute('SELECT * FROM config WHERE id=1').fetchone())
        equity_history = [dict(r) for r in c.execute('SELECT * FROM (SELECT rowid,ts,equity FROM equity_history ORDER BY rowid DESC LIMIT 500) ORDER BY rowid')]
    with connection() as c:
        totals = c.execute('SELECT COUNT(*) AS n, COALESCE(SUM(fee),0) AS fees FROM trades').fetchone()
    equity = w['usdt'] + w['sol'] * state['bid'] if state['bid'] else None
    quote_age=(datetime.now(timezone.utc)-datetime.fromisoformat(state['updated'])).total_seconds() if state['updated'] else None
    return {'quote_age_seconds':round(quote_age,1) if quote_age is not None else None,'mode':'PAPER','paper_only':True,'network':setting('network'),'symbol':SYMBOL,**state,'usdt':w['usdt'],'sol':w['sol'],
            'initial':w['initial'],'equity':equity,'pnl':equity-w['initial'] if equity is not None else None,
            'drawdown_pct':max(0,(1-equity/w['peak'])*100) if equity is not None and w['peak'] else None,
            'center':center['center'] if center else None,'orders':orders,'trades':trades,'prices':prices,
            'fee_rate':FEE,'grid_gap_pct':cfg['gap']*100,'reserve_usdt':cfg['reserve'],
            'config':cfg,'equity_history':equity_history,'fees_total':totals['fees'],'fills_count':totals['n'],
            'telegram':{'enabled':setting('tg_enabled')=='1','daily':setting('tg_daily')=='1','trades':setting('tg_trades')=='1','hour':int(setting('tg_hour','21')),'configured':bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'))}}

@app.post('/api/start')
def start():
    if not state['price'] or state['error'] or not state['updated'] or (datetime.now(timezone.utc)-datetime.fromisoformat(state['updated'])).total_seconds()>20:
        raise HTTPException(503,'Немає актуальних котирувань Bybit')
    state['running'] = True
    put_settings({'paper_running':'1'})
    return {'ok':True,'running':True}

@app.post('/api/pause')
def pause():
    state['running'] = False
    put_settings({'paper_running':'0'})
    return {'ok':True,'running':False}

@app.post('/api/reset')
def reset():
    if state['running']:
        raise HTTPException(409,'Спочатку натисніть Pause')
    with connection() as c:
        for table in ('wallet','settings','orders','trades','prices','equity_history','market_samples'):
            c.execute(f'DELETE FROM {table}')
        c.execute('INSERT INTO wallet VALUES (1,100,0,100,100)')
    state['error'] = None
    put_settings({'paper_running':'0'})
    return {'ok':True}

@app.get('/api/health')
def health():
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(state['updated'])).total_seconds() if state['updated'] else None
    return {'ok':True,'market_connected':age is not None and age<20 and state['error'] is None,'quote_age_seconds':round(age,1) if age is not None else None,'error':state['error'],'updated':state['updated']}


from pydantic import BaseModel, Field

class ConfigInput(BaseModel):
    gap_pct: float = Field(ge=0.4, le=5)
    steps: int = Field(ge=2, le=12)
    order_usdt: float = Field(ge=5, le=25)
    reserve_usdt: float = Field(ge=0, le=70)
    adaptive: bool = False

@app.post('/api/config')
def save_config(cfg: ConfigInput):
    if state['running']:
        raise HTTPException(409, 'Спочатку натисніть Pause')
    if cfg.steps * cfg.order_usdt + cfg.reserve_usdt > 100:
        raise HTTPException(422, 'Сума ордерів і резерву перевищує 100 USDT')
    with connection() as c:
        if c.execute('SELECT COUNT(*) FROM trades').fetchone()[0]:
            raise HTTPException(409, 'Для зміни сітки після угод потрібен Reset Paper')
        c.execute('UPDATE config SET gap=?,steps=?,order_usdt=?,reserve=?,adaptive=? WHERE id=1',
                  (cfg.gap_pct/100,cfg.steps,cfg.order_usdt,cfg.reserve_usdt,int(cfg.adaptive)))
        c.execute('DELETE FROM orders')
        c.execute('DELETE FROM settings')
    return {'ok':True}

@app.get('/api/backtest')
async def backtest(days: int = 7, interval: str = '60'):
    if days not in (1,3,7,14,30):
        raise HTTPException(422, 'days: 1, 3, 7, 14, 30')
    if interval not in ('15','60','240'):
        raise HTTPException(422, 'interval: 15, 60, 240')
    minutes = int(interval)
    needed = min(1000, days*24*60//minutes)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r=await client.get(BASE_URLS[setting('network')]+'/v5/market/kline',params={
                'category':'spot','symbol':SYMBOL,'interval':interval,'limit':needed})
            r.raise_for_status()
            data=r.json()
            if data.get('retCode') != 0: raise ValueError(data.get('retMsg'))
            candles=sorted(data['result']['list'],key=lambda x:int(x[0]))
    except Exception as exc:
        raise HTTPException(503, f'Bybit kline: {exc}')
    if len(candles)<10: raise HTTPException(503,'Недостатньо свічок')
    with connection() as c:
        cfg=dict(c.execute('SELECT * FROM config WHERE id=1').fetchone())
    # Conservative candle simulation: each bar can fill at most one order;
    # if both directions are possible, BUY has priority. No intrabar path inference.
    start=float(candles[0][4]);cash=100.;coin=0.;peak=100.;maxdd=0.;fees=0.;fills=0
    gap=cfg['gap']
    if cfg['adaptive']:
        changes=[abs(float(candles[i][4])/float(candles[i-1][4])-1) for i in range(1,min(len(candles),50))]
        gap=max(.006,min(.03,mean(changes)*2)) if changes else gap
    orders=[{'side':'BUY','price':start*(1-gap*k),'qty':cfg['order_usdt']/(start*(1-gap*k)*(1+FEE))} for k in range(1,cfg['steps']+1)]
    curve=[]
    for bar in candles[1:]:
        ts, high,low,close=int(bar[0]),float(bar[2]),float(bar[3]),float(bar[4])
        # BUY first for ambiguous bars; one fill per candle; no guaranteed fill at touch in reality.
        for i,o in enumerate(orders):
            q=o['qty'];px=o['price']
            if o['side']=='BUY' and low<=px and cash>=q*px*(1+FEE)+cfg['reserve']:
                fee=q*px*FEE;cash-=q*px+fee;coin+=q;fees+=fee
                orders[i]={'side':'SELL','price':px*(1+gap),'qty':q};fills+=1;break
            if o['side']=='SELL' and high>=px and coin+1e-9>=q:
                fee=q*px*FEE;cash+=q*px-fee;coin-=q;fees+=fee
                orders[i]={'side':'BUY','price':px/(1+gap),'qty':q};fills+=1;break
        equity=cash+coin*close;peak=max(peak,equity)
        maxdd=max(maxdd,(peak-equity)/peak*100)
        curve.append({'ts':ts,'equity':round(equity,5),'hold':round(100*close/start,5)})
        if maxdd>=8: break
    end=curve[-1]['equity']
    return {'mode':'BACKTEST','network':setting('network'),'symbol':SYMBOL,'candles':len(candles),'bars_processed':len(curve),
            'days_requested':days,'interval':interval,'initial':100,'final':end,
            'pnl':round(end-100,5),'pnl_pct':round(end-100,3),
            'buy_hold_pnl_pct':round(curve[-1]['hold']-100,3),
            'max_drawdown_pct':round(maxdd,3),'fills':fills,'fees':round(fees,5),
            'stopped_on_drawdown':maxdd>=8,'gap_pct_used':round(gap*100,3),
            'curve':curve,
            'limitations':'OHLC: один fill на свічку, без спреду, прослизання та часткових виконань. Результат не є прогнозом.'}

class TelegramInput(BaseModel):
    enabled: bool
    trades: bool = True
    daily: bool = True
    hour: int = Field(default=21,ge=0,le=23)

@app.post('/api/telegram/settings')
def telegram_settings(data: TelegramInput):
    if data.enabled and not (os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')):
        raise HTTPException(422,'Додайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID до .env та перезапустіть сервер')
    put_settings({'tg_enabled':int(data.enabled),'tg_trades':int(data.trades),'tg_daily':int(data.daily),'tg_hour':data.hour})
    return {'ok':True}

@app.post('/api/telegram/test')
async def telegram_test():
    try: await telegram_send('🐶 Ayla Grid Bot v0.7 · тестове повідомлення · PAPER')
    except Exception as exc: raise HTTPException(503,str(exc))
    return {'ok':True}

@app.get('/api/analytics')
def analytics():
    with connection() as c:
        trades=[dict(r) for r in c.execute('SELECT * FROM trades ORDER BY id')]
        history=[dict(r) for r in c.execute('SELECT * FROM equity_history ORDER BY rowid DESC LIMIT 2000')][::-1]
        w=c.execute('SELECT * FROM wallet WHERE id=1').fetchone()
    fees=sum(t['fee'] for t in trades)
    equity=w['usdt']+w['sol']*(state['bid'] or state['price'] or 0)
    peak=w['initial']; max_dd=0
    for x in history:
        peak=max(peak,x['equity'])
        if peak: max_dd=max(max_dd,(peak-x['equity'])/peak*100)
    return {'fills':len(trades),'buys':sum(t['side']=='BUY' for t in trades),
            'sells':sum(t['side']=='SELL' for t in trades),'fees':fees,'equity':equity,
            'pnl':equity-w['initial'],'max_drawdown_pct':max_dd,
            'note':'PnL включає відкриті позиції; завершені цикли окремо не розраховуються.'}




# --- v0.9 analytics, range advisory and alerts ---
from collections import deque

def position_analysis(c, bid):
    """Match fills FIFO by grid level. Realized PnL includes both fees.
    Existing trades contain level via their filled order, but not direct parent IDs.
    FIFO per level is exact for the one-open-order-per-level grid model.
    """
    rows=c.execute("SELECT t.id,t.ts,t.side,t.price,t.qty,t.fee,o.level FROM trades t LEFT JOIN orders o ON o.id=t.order_id ORDER BY t.id").fetchall()
    queues={}; realized=0.; matched=0; unmatched_sells=0
    for t in rows:
        level=t['level']
        if level is None: continue
        q=queues.setdefault(level,deque())
        if t['side']=='BUY':
            q.append([t['qty'],t['price'],t['fee']/t['qty'] if t['qty'] else 0])
        elif t['side']=='SELL':
            remaining=t['qty']
            while remaining>1e-10 and q:
                lot=q[0]; take=min(remaining,lot[0]);
                realized+=take*(t['price']-lot[1]-lot[2]-(t['fee']/t['qty'] if t['qty'] else 0))
                matched+=take; remaining-=take; lot[0]-=take
                if lot[0]<1e-10:q.popleft()
            if remaining>1e-9: unmatched_sells+=1
    open_cost=sum(lot[0]*(lot[1]+lot[2]) for q in queues.values() for lot in q)
    open_qty=sum(lot[0] for q in queues.values() for lot in q)
    unrealized=(open_qty*bid-open_cost) if bid is not None else None
    return {'realized_pnl':round(realized,6),'unrealized_pnl':round(unrealized,6) if unrealized is not None else None,
            'open_sol_cost':round(open_cost,6),'open_sol_qty':round(open_qty,9),
            'avg_open_cost':round(open_cost/open_qty,6) if open_qty else None,
            'matched_sell_sol':round(matched,9),'unmatched_sells':unmatched_sells,
            'method':'FIFO per grid level; both trading fees included'}

def range_analysis(c, price):
    orders=[dict(x) for x in c.execute("SELECT side,price FROM orders WHERE status='OPEN'")]
    buys=[o['price'] for o in orders if o['side']=='BUY']
    sells=[o['price'] for o in orders if o['side']=='SELL']
    if price is None or not orders: return {'state':'NO_GRID','out_of_range':False}
    lowest=min(o['price'] for o in orders); highest=max(o['price'] for o in orders)
    if price<lowest: state_name='BELOW'
    elif price>highest: state_name='ABOVE'
    else: state_name='INSIDE'
    nearest_sell=min(sells) if sells else None
    nearest_buy=max(buys) if buys else None
    return {'state':state_name,'out_of_range':state_name!='INSIDE','low':lowest,'high':highest,
            'buy_count':len(buys),'sell_count':len(sells),
            'nearest_sell':nearest_sell,'nearest_buy':nearest_buy,
            'distance_to_nearest_sell_pct':round((nearest_sell/price-1)*100,3) if nearest_sell and price else None,
            'suggested_center':round(price,4),
            'advice':'Rebase is advisory only; existing positions are never automatically sold or discarded.'}

@app.get('/api/strategy')
def strategy():
    with connection() as c:
        w=c.execute('SELECT * FROM wallet WHERE id=1').fetchone()
        trades=c.execute('SELECT COUNT(*), MAX(ts) FROM trades').fetchone()
        bid=state['bid']
        p=position_analysis(c,bid)
        r=range_analysis(c,state['price'])
        samples=c.execute('SELECT COUNT(*), MIN(ts), MAX(ts) FROM market_samples').fetchone()
    equity=w['usdt']+w['sol']*bid if bid is not None else None
    return {'mode':'PAPER','network':NETWORK,'position':p,'range':r,
            'capital':{'usdt':w['usdt'],'sol':w['sol'],'equity':equity,
                       'sol_pct':round(w['sol']*bid/equity*100,2) if equity and bid else None},
            'last_trade':trades[1],'trades_count':trades[0],
            'history':{'samples':samples[0],'first':samples[1],'last':samples[2]},
            'auto_recenter':os.getenv('AYLA_AUTO_RECENTER','1')=='1',
            'safety':'Mainnet public prices only; auto-recenter only if SOL=0 after 6h outside; no exchange order placement.'}

@app.get('/api/strategy/compare')
async def strategy_compare(days:int=7,interval:str='60'):
    if days not in (1,3,7,14,30) or interval not in ('15','60','240'):
        raise HTTPException(422,'days 1,3,7,14,30; interval 15,60,240')
    # Limit to Bybit 1000 recent candles, and disclose actual coverage.
    needed=min(1000,max(20,days*1440//int(interval)))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp=await client.get(BASE_URLS[NETWORK]+'/v5/market/kline',params={'category':'spot','symbol':SYMBOL,'interval':interval,'limit':needed})
            resp.raise_for_status(); payload=resp.json()
            if payload.get('retCode')!=0: raise ValueError(payload.get('retMsg'))
            bars=sorted(payload['result']['list'],key=lambda x:int(x[0]))
    except Exception as exc: raise HTTPException(503,str(exc))
    if len(bars)<20: raise HTTPException(503,'Insufficient candles')
    with connection() as c: cfg=dict(c.execute('SELECT * FROM config WHERE id=1').fetchone())
    def simulate_variant(dynamic):
        cash=100.;sol=0.;start=float(bars[0][4]);center=start;peak=100.;dd=0.;fills=0;fees=0.;rebalances=0
        gap=cfg['gap']; steps=cfg['steps']; reserve=cfg['reserve'];size=cfg['order_usdt']
        orders=[{'side':'BUY','price':center*(1-gap*i),'qty':size/(center*(1-gap*i)*(1+FEE))} for i in range(1,steps+1)]
        below_count=0
        for bar in bars[1:]:
            high,low,close=float(bar[2]),float(bar[3]),float(bar[4])
            # One fill per bar. Same fill assumptions for both strategies.
            for i,o in enumerate(orders):
                q=o['qty'];px=o['price']
                if o['side']=='BUY' and low<=px and cash>=q*px*(1+FEE)+reserve:
                    fee=q*px*FEE;cash-=q*px+fee;sol+=q;fees+=fee;fills+=1
                    orders[i]={'side':'SELL','price':px*(1+gap),'qty':q};break
                if o['side']=='SELL' and high>=px and sol+1e-9>=q:
                    fee=q*px*FEE;cash+=q*px-fee;sol-=q;fees+=fee;fills+=1
                    orders[i]={'side':'BUY','price':px/(1+gap),'qty':q};break
            # Conservative dynamic rebase: only when FLAT (no SOL inventory).
            # This avoids resetting underwater SELL positions.
            if dynamic:
                below_count=below_count+1 if close<min(o['price'] for o in orders) else 0
                if below_count>=6 and sol<1e-8:
                    center=close
                    orders=[{'side':'BUY','price':center*(1-gap*i),'qty':size/(center*(1-gap*i)*(1+FEE))} for i in range(1,steps+1)]
                    rebalances+=1;below_count=0
            equity=cash+sol*close;peak=max(peak,equity);dd=max(dd,(peak-equity)/peak*100)
            if dd>=8:break
        return {'final':round(equity,4),'pnl_pct':round(equity-100,4),'fills':fills,'fees':round(fees,5),
                'max_drawdown_pct':round(dd,3),'rebases':rebalances}
    start=float(bars[0][4]);end=float(bars[-1][4]);coverage=(int(bars[-1][0])-int(bars[0][0]))/86400000
    return {'mode':'BACKTEST','network':NETWORK,'candles':len(bars),'coverage_days':round(coverage,2),
            'requested_days':days,'static':simulate_variant(False),'adaptive':simulate_variant(True),
            'buy_hold_pct':round((end/start-1)*100,4),
            'limitations':'OHLC approximation: one fill/bar, no spread/slippage, no intrabar sequence. Adaptive rebase only when SOL=0; may not trigger. Not a forecast.'}

async def strategy_alerts():
    # Persistent de-duplication: at most one message per condition per Kyiv day.
    from zoneinfo import ZoneInfo
    while True:
        try:
            if setting('tg_enabled')=='1' and state['price'] and state['running']:
                with connection() as c:
                    r=range_analysis(c,state['price'])
                    last=c.execute('SELECT MAX(ts) FROM trades').fetchone()[0]
                    center=c.execute('SELECT created FROM settings WHERE id=1').fetchone()
                    since=last or (center['created'] if center else None)
                    elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(since)).total_seconds()/3600 if since else 0
                    today=datetime.now(ZoneInfo('Europe/Kyiv')).date().isoformat()
                    alerts=[]
                    if r.get('out_of_range'):
                        alerts.append(('outside',f'🐶 Grid поза діапазоном: {r["state"]}; SOL {state["price"]:.4f}; сітка {r["low"]:.4f}–{r["high"]:.4f}. Автопереносу немає.'))
                    if r.get('buy_count')==0:
                        alerts.append(('no_buys','🐶 Усі активні ордери SELL; нових BUY поки немає.'))
                    if elapsed>=float(setting('alert_no_trade_hours','24')):
                        alerts.append(('idle',f'🐶 Немає виконань {elapsed:.0f} годин.'))
                    pending=[]
                    for key,message in alerts:
                        fullkey=f'alert:{key}:{today}'
                        if not c.execute('SELECT 1 FROM notifications WHERE key=?',(fullkey,)).fetchone():pending.append((fullkey,message))
                for key,message in pending:
                    await telegram_send(message)
                    with connection() as c:c.execute('INSERT OR IGNORE INTO notifications VALUES (?,?)',(key,now()))
        except Exception as exc: log.warning('Strategy alerts failed: %s',exc)
        await asyncio.sleep(900)

@app.get('/api/markets')
async def markets():
    """Public ticker comparison; never changes the trading environment."""
    async def get_one(client, network):
        try:
            r=await client.get(BASE_URLS[network]+'/v5/market/tickers',params={'category':'spot','symbol':SYMBOL})
            r.raise_for_status()
            payload=r.json()
            if payload.get('retCode') != 0: raise ValueError(payload.get('retMsg','Bybit error'))
            t=payload['result']['list'][0]
            return {'network':network,'price':float(t['lastPrice']),
                    'change_24h_pct':round(float(t.get('price24hPcnt') or 0)*100,3),
                    'volume_24h':float(t.get('volume24h') or 0), 'updated':now()}
        except Exception as exc:
            return {'network':network,'error':f'{type(exc).__name__}: {exc}'}
    async with httpx.AsyncClient(timeout=10) as client:
        result=await asyncio.gather(get_one(client,'mainnet'),get_one(client,'testnet'))
    return {'trading_network':setting('network'),'markets':result,'mode':'PAPER'}

@app.get('/api/candles')
async def candles(interval:str='15',limit:int=500,network:str|None=None):
    if interval not in ('1','5','15','30','60','240','D'): raise HTTPException(422,'Невірний інтервал')
    if not 20<=limit<=1000: raise HTTPException(422,'limit 20..1000')
    network=network or setting('network','mainnet')
    if network not in BASE_URLS: raise HTTPException(422,'network: mainnet або testnet')
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r=await client.get(BASE_URLS[network]+'/v5/market/kline',params={'category':'spot','symbol':SYMBOL,'interval':interval,'limit':limit})
            r.raise_for_status(); payload=r.json()
    except (httpx.HTTPError,ValueError) as e: raise HTTPException(502,str(e))
    if payload.get('retCode')!=0: raise HTTPException(502,payload.get('retMsg','Bybit error'))
    bars=[{'time':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in payload.get('result',{}).get('list',[])]
    bars.sort(key=lambda x:x['time'])
    with connection() as c:
        orders=[{'price':row['price'],'side':row['side']} for row in c.execute('SELECT side,price FROM orders WHERE status=?', ('OPEN',))]
        trades=[{'ts':row['ts'],'price':row['price'],'side':row['side']} for row in c.execute('SELECT ts,price,side FROM trades ORDER BY id DESC LIMIT 200')]
    if network != setting('network'):
        orders=[]; trades=[]  # Never overlay orders from another market on comparison chart
    return {'network':network,'trading_network':setting('network'),'symbol':SYMBOL,'interval':interval,'candles':bars,'orders':orders,'trades':trades,'mode':'PAPER'}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.getenv('HOST','0.0.0.0'), port=int(os.getenv('PORT','8090')), log_level='info')
