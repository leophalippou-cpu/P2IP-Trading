# api/main.py
import asyncio
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from sqlalchemy import create_engine, Column, Integer, Float, String, ForeignKey, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from passlib.context import CryptContext
from jose import jwt, JWTError

from fastapi.security import OAuth2PasswordRequestForm

# ============================================================
# 1) DB (SQLite) + Models
# ============================================================
DATABASE_URL = "sqlite:///./papertrading.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    is_admin = Column(Boolean, default=False)


class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True)
    cash = Column(Float, default=10000.0)


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    symbol = Column(String, index=True)
    qty = Column(Float, default=0.0)
    avg = Column(Float, default=0.0)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    t = Column(Integer, index=True)
    type = Column(String)  # BUY / SELL / AUTO_BUY / AUTO_SELL
    symbol = Column(String)
    qty = Column(Float)
    price = Column(Float)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_t = Column(Integer, default=0)
    side = Column(String)          # BUY / SELL
    symbol = Column(String, index=True)
    qty = Column(Float)
    condition = Column(String)     # GTE / LTE
    trigger_price = Column(Float)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================
# 2) Auth (PBKDF2-SHA256, no 72-byte limit) + JWT
# ============================================================
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

JWT_SECRET = "CHANGE_ME_SUPER_SECRET"
JWT_ALG = "HS256"
JWT_EXPIRE_MIN = 60 * 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(p: str) -> str:
    return pwd_context.hash(p)


def verify_password(p: str, hashed: str) -> bool:
    return pwd_context.verify(p, hashed)


def create_access_token(sub: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MIN)
    payload = {"sub": sub, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def get_current_user(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        username = payload.get("sub")
        if not username:
            raise HTTPException(401, "Token invalide")
    except JWTError:
        raise HTTPException(401, "Token invalide")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "Utilisateur introuvable")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not bool(getattr(user, "is_admin", False)):
        raise HTTPException(403, "Accès admin requis")
    return user


def ensure_wallet(db: Session, user_id: int) -> Wallet:
    w = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if not w:
        w = Wallet(user_id=user_id, cash=10_000.0)
        db.add(w)
        db.commit()
        db.refresh(w)
    return w


# ============================================================
# 3) FastAPI app
# ============================================================
app = FastAPI(title="PaperTrading API (Auth + Market + Orders + Admin)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en prod, limite à ton domaine
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 4) Marché simulé (global)
# ============================================================
ASSETS = {
    "VIRBTC": {"S0": 30000.0},
    "VIRCAC": {"S0": 7000.0},
    "VIRTECH": {"S0": 120.0},
}

ALPHA = 0.00005
SIGMA_NOISE = 0.003

N_BOTS_BASE = 35
N_BOTS_JITTER = 25
BOT_QTY_MEDIAN = 0.6
BOT_QTY_SIGMA = 1.1
P_WHALE = 0.02
WHALE_MULT_MIN = 8
WHALE_MULT_MAX = 30

USER_IMPACT_MULT = 15

P_CRASH = 0.0001
P_PUMP = 0.0001
CRASH_MIN, CRASH_MAX = 0.03, 0.12
PUMP_MIN, PUMP_MAX = 0.03, 0.15

state = {"t": 0, "prices": {sym: info["S0"] for sym, info in ASSETS.items()}}
market_history = []  # {t,symbol,price,buy_vol,sell_vol,net_flow,event}
MAX_MARKET_ROWS = 8000

auto_task = None
auto_running = False
tick_interval = 0.5


def _append_history(t: int, sym: str, price: float, buy_vol: float, sell_vol: float, net_flow: float, event: str):
    market_history.append({
        "t": int(t),
        "symbol": sym,
        "price": float(price),
        "buy_vol": float(buy_vol),
        "sell_vol": float(sell_vol),
        "net_flow": float(net_flow),
        "event": event
    })
    if len(market_history) > MAX_MARKET_ROWS:
        del market_history[: MAX_MARKET_ROWS // 5]


def _get_position(db: Session, user_id: int, symbol: str) -> Optional[Position]:
    return db.query(Position).filter(Position.user_id == user_id, Position.symbol == symbol).first()


def _execute_buy(db: Session, user_id: int, symbol: str, qty: float, trade_type: str):
    if qty <= 0:
        raise HTTPException(400, "Quantité invalide")

    w = ensure_wallet(db, user_id)
    price_before = float(state["prices"][symbol])

    if price_before * qty > float(w.cash):
        raise HTTPException(400, "Pas assez de cash")

    impact = ALPHA * (qty * USER_IMPACT_MULT) ** 0.8
    state["prices"][symbol] = float(state["prices"][symbol] * np.exp(impact))
    price_exec = float(state["prices"][symbol])

    if price_exec * qty > float(w.cash):
        raise HTTPException(400, "Pas assez de cash (impact)")

    pos = _get_position(db, user_id, symbol)
    if not pos:
        pos = Position(user_id=user_id, symbol=symbol, qty=0.0, avg=0.0)
        db.add(pos)

    old_qty = float(pos.qty)
    new_qty = old_qty + qty
    pos.avg = float((float(pos.avg) * old_qty + price_exec * qty) / new_qty)
    pos.qty = float(new_qty)

    w.cash = float(w.cash) - (price_exec * qty)
    db.add(Trade(user_id=user_id, t=state["t"], type=trade_type, symbol=symbol, qty=qty, price=price_exec))
    db.commit()

    return price_exec, float(w.cash)


def _execute_sell(db: Session, user_id: int, symbol: str, qty: float, trade_type: str):
    if qty <= 0:
        raise HTTPException(400, "Quantité invalide")

    w = ensure_wallet(db, user_id)
    pos = _get_position(db, user_id, symbol)
    if not pos or float(pos.qty) < qty:
        raise HTTPException(400, "Pas assez d'actifs à vendre")

    impact = -ALPHA * (qty * USER_IMPACT_MULT) ** 0.8
    state["prices"][symbol] = float(state["prices"][symbol] * np.exp(impact))
    price_exec = float(state["prices"][symbol])

    w.cash = float(w.cash) + (price_exec * qty)

    pos.qty = float(pos.qty) - qty
    if float(pos.qty) <= 1e-12:
        db.delete(pos)

    db.add(Trade(user_id=user_id, t=state["t"], type=trade_type, symbol=symbol, qty=qty, price=price_exec))
    db.commit()

    return price_exec, float(w.cash)


def process_orders_for_symbol(db: Session, symbol: str):
    price_now = float(state["prices"][symbol])
    orders = db.query(Order).filter(Order.symbol == symbol).order_by(Order.id.asc()).all()

    for o in orders:
        trig = float(o.trigger_price)
        should_fire = (price_now >= trig) if o.condition == "GTE" else (price_now <= trig)
        if not should_fire:
            continue

        try:
            if o.side == "BUY":
                _execute_buy(db, o.user_id, o.symbol, float(o.qty), trade_type="AUTO_BUY")
            else:
                _execute_sell(db, o.user_id, o.symbol, float(o.qty), trade_type="AUTO_SELL")

            db.delete(o)
            db.commit()
        except HTTPException:
            db.rollback()
            continue


def do_tick() -> dict:
    state["t"] += 1
    t = state["t"]

    for sym in ASSETS.keys():
        buy_vol = 0.0
        sell_vol = 0.0

        n_bots = int(max(1, N_BOTS_BASE + np.random.randint(-N_BOTS_JITTER, N_BOTS_JITTER + 1)))
        for _ in range(n_bots):
            side_buy = (np.random.rand() < 0.5)
            qty = float(np.random.lognormal(mean=np.log(BOT_QTY_MEDIAN), sigma=BOT_QTY_SIGMA))
            if np.random.rand() < P_WHALE:
                qty *= float(np.random.uniform(WHALE_MULT_MIN, WHALE_MULT_MAX))
            if side_buy:
                buy_vol += qty
            else:
                sell_vol += qty

        net_flow = buy_vol - sell_vol

        P = float(state["prices"][sym])
        logP = float(np.log(P))
        impact = ALPHA * np.sign(net_flow) * (abs(net_flow) ** 0.8)  # Option B
        noise = float(SIGMA_NOISE * np.random.normal())
        logP_new = logP + impact + noise
        P_new = float(np.exp(logP_new))

        u = float(np.random.rand())
        event = "NONE"
        if u < P_CRASH:
            drop = float(np.random.uniform(CRASH_MIN, CRASH_MAX))
            P_new *= (1.0 - drop)
            event = f"CRASH -{drop*100:.1f}%"
        elif u < P_CRASH + P_PUMP:
            jump = float(np.random.uniform(PUMP_MIN, PUMP_MAX))
            P_new *= (1.0 + jump)
            event = f"PUMP +{jump*100:.1f}%"

        state["prices"][sym] = float(P_new)
        _append_history(t, sym, P_new, buy_vol, sell_vol, net_flow, event)

    db = SessionLocal()
    try:
        for sym in ASSETS.keys():
            process_orders_for_symbol(db, sym)
    finally:
        db.close()

    return {"t": t, "prices": state["prices"]}


async def auto_market_loop():
    global auto_running
    while auto_running:
        do_tick()
        await asyncio.sleep(tick_interval)


@app.on_event("startup")
async def start_auto_market():
    global auto_task, auto_running
    auto_running = True
    auto_task = asyncio.create_task(auto_market_loop())


# ============================================================
# 5) Schemas
# ============================================================
class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TradeRequest(BaseModel):
    symbol: str
    qty: float


class OrderRequest(BaseModel):
    side: str
    symbol: str
    qty: float
    condition: str
    trigger_price: float


class AutoConfig(BaseModel):
    running: bool
    interval: float | None = None


# ============================================================
# 6) Auth endpoints
# ============================================================
@app.post("/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    username = req.username.strip()
    if len(username) < 3:
        raise HTTPException(400, "username trop court (min 3)")
    if len(req.password) < 4:
        raise HTTPException(400, "password trop court (min 4)")

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(400, "username déjà utilisé")

    # ✅ Le compte "admin" devient admin automatiquement
    is_admin = (username.lower() == "admin")

    u = User(username=username, password_hash=hash_password(req.password), is_admin=is_admin)
    db.add(u)
    db.commit()
    db.refresh(u)

    ensure_wallet(db, u.id)

    token = create_access_token(username)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/auth/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2PasswordRequestForm donne: form.username / form.password
    username = form.username.strip()
    u = db.query(User).filter(User.username == username).first()
    if not u or not verify_password(form.password, u.password_hash):
        raise HTTPException(401, "Identifiants invalides")

    token = create_access_token(username)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "is_admin": bool(user.is_admin)}


# ============================================================
# 7) Public endpoints
# ============================================================
@app.get("/assets")
def list_assets():
    return {"assets": list(ASSETS.keys())}


@app.get("/prices")
def get_prices():
    return {"t": state["t"], "prices": state["prices"]}


@app.get("/market_history")
def get_market_history(symbol: str, limit: int = 400):
    rows = [r for r in market_history if r["symbol"] == symbol]
    return {"rows": rows[-limit:]}


@app.get("/auto")
def get_auto():
    return {"running": auto_running, "interval": tick_interval}


@app.post("/auto")
async def set_auto(cfg: AutoConfig):
    global auto_running, tick_interval, auto_task

    if cfg.interval is not None:
        if cfg.interval < 0.05:
            raise HTTPException(400, "interval trop petit (min 0.05s)")
        tick_interval = float(cfg.interval)

    if cfg.running and not auto_running:
        auto_running = True
        auto_task = asyncio.create_task(auto_market_loop())

    if (not cfg.running) and auto_running:
        auto_running = False

    return {"running": auto_running, "interval": tick_interval}


# ============================================================
# 8) Protected endpoints
# ============================================================
@app.get("/portfolio")
def portfolio(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    w = ensure_wallet(db, user.id)
    positions = db.query(Position).filter(Position.user_id == user.id).all()

    total = float(w.cash)
    out_positions = []
    for p in positions:
        price = float(state["prices"].get(p.symbol, 0.0))
        value = float(p.qty * price)
        pnl = float((price - p.avg) * p.qty)
        total += value
        out_positions.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg": float(p.avg),
            "price": price,
            "value": value,
            "pnl": pnl
        })

    return {"cash": float(w.cash), "total": total, "positions": out_positions}


@app.post("/buy")
def buy(req: TradeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if req.symbol not in ASSETS:
        raise HTTPException(400, "Symbole inconnu")
    price_exec, cash = _execute_buy(db, user.id, req.symbol, float(req.qty), trade_type="BUY")
    return {"ok": True, "price": price_exec, "cash": cash}


@app.post("/sell")
def sell(req: TradeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if req.symbol not in ASSETS:
        raise HTTPException(400, "Symbole inconnu")
    price_exec, cash = _execute_sell(db, user.id, req.symbol, float(req.qty), trade_type="SELL")
    return {"ok": True, "price": price_exec, "cash": cash}


@app.get("/orders")
def list_orders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Order).filter(Order.user_id == user.id).order_by(Order.id.desc()).all()
    return {"orders": [
        {
            "id": int(o.id),
            "created_t": int(o.created_t),
            "side": o.side,
            "symbol": o.symbol,
            "qty": float(o.qty),
            "condition": o.condition,
            "trigger_price": float(o.trigger_price),
        }
        for o in rows
    ]}


@app.post("/orders")
def create_order(req: OrderRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    side = req.side.upper()
    cond = req.condition.upper()

    if req.symbol not in ASSETS:
        raise HTTPException(400, "Symbole inconnu")
    if side not in ("BUY", "SELL"):
        raise HTTPException(400, "side doit être BUY ou SELL")
    if cond not in ("GTE", "LTE"):
        raise HTTPException(400, "condition doit être GTE ou LTE")
    if req.qty <= 0:
        raise HTTPException(400, "qty invalide")
    if req.trigger_price <= 0:
        raise HTTPException(400, "trigger_price invalide")

    o = Order(
        user_id=user.id,
        created_t=int(state["t"]),
        side=side,
        symbol=req.symbol,
        qty=float(req.qty),
        condition=cond,
        trigger_price=float(req.trigger_price),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return {"ok": True, "id": int(o.id)}


@app.delete("/orders/{order_id}")
def delete_order(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    o = db.query(Order).filter(Order.id == order_id, Order.user_id == user.id).first()
    if not o:
        raise HTTPException(404, "Ordre introuvable")
    db.delete(o)
    db.commit()
    return {"ok": True}


@app.get("/trades")
def trades(db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: int = 50):
    rows = db.query(Trade).filter(Trade.user_id == user.id).order_by(Trade.id.desc()).limit(limit).all()
    return {"trades": [
        {"t": int(r.t), "type": r.type, "symbol": r.symbol, "qty": float(r.qty), "price": float(r.price)}
        for r in rows
    ]}


# ============================================================
# 9) Admin endpoints (admin only)
# ============================================================
@app.get("/admin/users")
def admin_list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    users = db.query(User).order_by(User.id.asc()).all()

    out = []
    for u in users:
        w = db.query(Wallet).filter(Wallet.user_id == u.id).first()
        cash = float(w.cash) if w else 0.0

        positions = db.query(Position).filter(Position.user_id == u.id).all()
        total = cash
        pos_count = 0
        for p in positions:
            pos_count += 1
            price = float(state["prices"].get(p.symbol, 0.0))
            total += float(p.qty) * price

        out.append({
            "id": int(u.id),
            "username": u.username,
            "is_admin": bool(u.is_admin),
            "cash": float(cash),
            "total": float(total),
            "positions": int(pos_count),
        })

    return {"users": out}