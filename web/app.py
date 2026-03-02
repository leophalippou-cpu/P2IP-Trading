# web/app.py
import time
import requests
import streamlit as st
import pandas as pd

API = "https://p2ip-trading.onrender.com"

st.set_page_config(page_title="Paper Trading (Login)", layout="wide")
st.title("📈 Paper Trading Virtuel — Connexion requise")

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def auth_headers():
    tok = st.session_state.get("token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}

def api_get(path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers = {**headers, **auth_headers()}
    r = requests.get(f"{API}{path}", timeout=5, headers=headers, **kwargs)
    r.raise_for_status()
    return r.json()

def api_post(path, json=None, data=None, **kwargs):
    headers = kwargs.pop("headers", {})
    headers = {**headers, **auth_headers()}
    r = requests.post(f"{API}{path}", json=json, data=data, timeout=5, headers=headers, **kwargs)
    return r

def api_delete(path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers = {**headers, **auth_headers()}
    r = requests.delete(f"{API}{path}", timeout=5, headers=headers, **kwargs)
    return r

def fmt_int(x: float) -> str:
    return f"{float(x):,.0f}"

# ------------------------------------------------------------
# Session defaults
# ------------------------------------------------------------
if "token" not in st.session_state:
    st.session_state.token = None
if "username" not in st.session_state:
    st.session_state.username = None
if "show_admin_panel" not in st.session_state:
    st.session_state.show_admin_panel = False

# ------------------------------------------------------------
# LOGIN / REGISTER (blocking)
# ------------------------------------------------------------
with st.sidebar:
    st.header("🔐 Compte")

    if st.session_state.token:
        st.success(f"Connectée : {st.session_state.username}")
        if st.button("Se déconnecter", use_container_width=True):
            st.session_state.token = None
            st.session_state.username = None
            st.session_state.show_admin_panel = False
            st.rerun()
    else:
        tab1, tab2 = st.tabs(["Connexion", "Créer un compte"])

        with tab1:
            u = st.text_input("Username", key="login_u")
            p = st.text_input("Password", type="password", key="login_p")

            if st.button("Se connecter", use_container_width=True):
                # ✅ IMPORTANT: OAuth2PasswordRequestForm attend du form-data (data=), pas du JSON
                resp = requests.post(f"{API}/auth/login", data={"username": u, "password": p}, timeout=5)
                if resp.ok:
                    data = resp.json()
                    st.session_state.token = data["access_token"]
                    st.session_state.username = u
                    st.rerun()
                else:
                    st.error(resp.text)

        with tab2:
            u2 = st.text_input("Username (min 3)", key="reg_u")
            p2 = st.text_input("Password (min 4)", type="password", key="reg_p")

            if st.button("Créer le compte", use_container_width=True):
                # register est en JSON (comme dans l'API)
                resp = requests.post(f"{API}/auth/register", json={"username": u2, "password": p2}, timeout=5)
                if resp.ok:
                    data = resp.json()
                    st.session_state.token = data["access_token"]
                    st.session_state.username = u2
                    st.rerun()
                else:
                    st.error(resp.text)

# Si pas connectée, on bloque le reste
if not st.session_state.token:
    st.info("Connecte-toi (ou crée un compte) pour utiliser le site.")
    st.stop()

# ------------------------------------------------------------
# Sidebar: réglages + admin
# ------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Réglages")
    auto_refresh = st.toggle("Rafraîchissement automatique", value=True)
    refresh_s = st.slider("Intervalle (secondes)", 0.2, 3.0, 0.7, 0.1)
    edit_mode = st.toggle("Mode saisie (désactive auto-refresh)", value=False)

# ------------------------------------------------------------
# Load data
# ------------------------------------------------------------
error_box = st.empty()
try:
    assets = api_get("/assets")["assets"]
    prices = api_get("/prices")
    portfolio = api_get("/portfolio")
    orders = api_get("/orders")["orders"]
except Exception as e:
    error_box.error(f"Erreur API: {e}")
    st.stop()

# ------------------------------------------------------------
# Detect admin (via /me)
# ------------------------------------------------------------
try:
    me = api_get("/me")
    is_admin = bool(me.get("is_admin", False))
except Exception:
    is_admin = False

with st.sidebar:
    if is_admin:
        st.divider()
        st.header("🛡️ Admin")
        st.caption("Visible uniquement si ton compte est admin.")
        if st.button("📋 Voir utilisateurs + soldes", use_container_width=True):
            st.session_state.show_admin_panel = True
        if st.button("❌ Cacher panneau admin", use_container_width=True):
            st.session_state.show_admin_panel = False

# ------------------------------------------------------------
# Admin panel (top of page)
# ------------------------------------------------------------
if is_admin and st.session_state.show_admin_panel:
    st.subheader("🛡️ Admin — Liste des utilisateurs + soldes")

    try:
        users = api_get("/admin/users")["users"]
        dfu = pd.DataFrame(users)[["id", "username", "cash", "total", "positions", "is_admin"]]
        st.dataframe(dfu, use_container_width=True, height=320)
        st.caption("Ces informations sont accessibles uniquement via le compte admin.")
    except Exception as e:
        st.error(f"Erreur /admin/users : {e}")

    st.divider()

# ------------------------------------------------------------
# Layout
# ------------------------------------------------------------
left, right = st.columns([1, 2], gap="large")

# ------------------------------------------------------------
# LEFT: Portfolio + manual trade + orders + trades
# ------------------------------------------------------------
with left:
    st.subheader("💼 Portefeuille")
    c1, c2 = st.columns(2)
    c1.metric("Cash", fmt_int(portfolio["cash"]))
    c2.metric("Valeur totale", fmt_int(portfolio["total"]))

    if portfolio["positions"]:
        dfp = pd.DataFrame(portfolio["positions"]).set_index("symbol")[["qty", "avg", "price", "value", "pnl"]]
        st.dataframe(dfp, use_container_width=True, height=210)
    else:
        st.info("Aucune position.")

    st.divider()
    st.subheader("🧾 Acheter / Vendre")

    sym_trade = st.selectbox("Actif", assets, key="sym_trade")
    p_now = float(prices["prices"][sym_trade])
    st.caption(f"Prix actuel ~ {p_now:,.4f}")

    max_qty = max(0.0, (float(portfolio["cash"]) / p_now) * 0.98)
    qty = st.number_input(
        "Quantité",
        min_value=0.0,
        max_value=float(max_qty) if max_qty > 0 else 0.0,
        value=min(1.0, float(max_qty)) if max_qty > 0 else 0.0,
        step=0.01,
        key="qty"
    )
    st.info(f"Coût estimé ~ {p_now * float(qty):,.2f} (cash: {fmt_int(portfolio['cash'])})")

    b1, b2 = st.columns(2)
    if b1.button("✅ BUY", use_container_width=True):
        resp = api_post("/buy", json={"symbol": sym_trade, "qty": float(qty)})
        if resp.ok:
            st.success("Achat OK")
        else:
            st.error(resp.text)
        st.rerun()

    if b2.button("❌ SELL", use_container_width=True):
        resp = api_post("/sell", json={"symbol": sym_trade, "qty": float(qty)})
        if resp.ok:
            st.success("Vente OK")
        else:
            st.error(resp.text)
        st.rerun()

    st.divider()
    st.subheader("🤖 Auto Buy / Auto Sell")

    colA, colB = st.columns(2)
    side = colA.selectbox("Type", ["BUY", "SELL"], key="order_side")
    symbol = colB.selectbox("Actif", assets, key="order_symbol")
    price_now = float(prices["prices"][symbol])
    st.caption(f"Prix actuel {symbol} ~ {price_now:,.4f}")

    if side == "BUY":
        cond_label = st.selectbox(
            "Déclenchement",
            ["Si le prix <= (buy the dip)", "Si le prix >= (breakout)"],
            key="order_cond_buy"
        )
    else:
        cond_label = st.selectbox(
            "Déclenchement",
            ["Si le prix >= (take profit)", "Si le prix <= (stop loss)"],
            key="order_cond_sell"
        )
    condition = "LTE" if "<=" in cond_label else "GTE"

    # Inputs stables malgré le rerun
    k_trig = f"trigger_{side}_{symbol}_{condition}"
    if k_trig not in st.session_state:
        st.session_state[k_trig] = price_now

    trigger_price = st.number_input(
        "Prix déclencheur",
        min_value=0.0001,
        value=float(st.session_state[k_trig]),
        step=0.1,
        key=k_trig
    )

    k_qty = f"orderqty_{side}_{symbol}_{condition}"
    if k_qty not in st.session_state:
        st.session_state[k_qty] = 0.1

    order_qty = st.number_input(
        "Quantité ordre",
        min_value=0.0,
        value=float(st.session_state[k_qty]),
        step=0.01,
        key=k_qty
    )

    if st.button("➕ Créer l'ordre auto", use_container_width=True):
        resp = api_post("/orders", json={
            "side": side,
            "symbol": symbol,
            "qty": float(order_qty),
            "condition": condition,
            "trigger_price": float(trigger_price),
        })
        if resp.ok:
            st.success("Ordre créé ✅")
        else:
            st.error(resp.text)
        st.rerun()

    st.divider()
    st.subheader("📌 Ordres en attente")
    if orders:
        df_orders = pd.DataFrame(orders)[["id", "side", "symbol", "qty", "condition", "trigger_price", "created_t"]]
        st.dataframe(df_orders, use_container_width=True, height=220)

        oid = st.selectbox("ID à annuler", [int(o["id"]) for o in orders], key="cancel_oid")
        if st.button("🗑️ Annuler", use_container_width=True):
            resp = api_delete(f"/orders/{oid}")
            if resp.ok:
                st.success("Annulé ✅")
            else:
                st.error(resp.text)
            st.rerun()
    else:
        st.info("Aucun ordre en attente.")

    st.divider()
    st.subheader("🧾 Derniers trades")
    try:
        tr = api_get("/trades", params={"limit": 30})["trades"]
        if tr:
            st.dataframe(pd.DataFrame(tr), use_container_width=True, height=220)
        else:
            st.caption("Aucun trade.")
    except Exception as e:
        st.warning(f"Trades indisponibles: {e}")

# ------------------------------------------------------------
# RIGHT: Market + charts
# ------------------------------------------------------------
with right:
    st.subheader("🏦 Marché")
    st.caption(f"Temps t = {prices['t']}")
    dfm = pd.DataFrame([{"Actif": k, "Prix": float(v)} for k, v in prices["prices"].items()]).set_index("Actif")
    st.dataframe(dfm, use_container_width=True)

    st.divider()
    st.subheader("📊 Courbes — 1 panneau par monnaie")
    metric = st.radio("Affichage", ["Prix", "Volumes (Buy/Sell)", "Net flow"], horizontal=True)
    limit = st.slider("Historique (points)", 80, 600, 250, 10)

    for asset in assets:
        st.markdown(f"### {asset}")
        try:
            rows = api_get("/market_history", params={"symbol": asset, "limit": int(limit)})["rows"]
        except Exception as e:
            st.warning(f"Historique indisponible pour {asset}: {e}")
            st.divider()
            continue

        if not rows:
            st.info("Historique en cours…")
            st.divider()
            continue

        df = pd.DataFrame(rows).set_index("t").sort_index()

        if "event" in df.columns:
            last_event = df["event"].iloc[-1]
            if isinstance(last_event, str) and last_event != "NONE":
                st.warning(f"⚠️ {last_event}")

        if metric == "Prix":
            st.line_chart(df[["price"]], height=220, use_container_width=True)
        elif metric == "Volumes (Buy/Sell)":
            st.line_chart(df[["buy_vol", "sell_vol"]], height=220, use_container_width=True)
        else:
            st.line_chart(df[["net_flow"]], height=220, use_container_width=True)

        st.divider()

# ------------------------------------------------------------
# Auto refresh
# ------------------------------------------------------------
if auto_refresh and not edit_mode:
    time.sleep(refresh_s)
    st.rerun()