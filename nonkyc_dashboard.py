"""
Panel web (Streamlit) para el bot de NonKYC — KAS/USDT y ZKAS/USDT.

Este dashboard es un proceso independiente del bot (`nonkyc_trading_bot.py`):
consulta la misma API pública de NonKYC por su cuenta para mostrar precios en
vivo, y solo IMPORTA la constante DRY_RUN del bot para mostrar en qué modo
está configurado — no detecta si el proceso del bot está corriendo de verdad,
porque son dos programas separados sin canal de comunicación entre ellos.

Persistencia:
- price_history.json guarda cada lectura de precio (con marca de tiempo) para
  que la curva sobreviva a un reinicio del panel o a una pestaña nueva del
  navegador — ver price_history.py.
- paper_wallet.json es la cartera virtual; la escribe el bot en cada señal
  real, y también la sección "🧪 Herramientas de Testeo" de la barra lateral
  (forzar BUY/SELL, reiniciar cartera) — es la única parte de este archivo
  que escribe ahí. Ver la nota de concurrencia en paper_wallet.py.

Instalación:
    pip install -r requirements.txt

Ejecución:
    streamlit run nonkyc_dashboard.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import aiohttp
import altair as alt
import pandas as pd
import streamlit as st

import paper_wallet
import price_history

BASE_URL = "https://api.nonkyc.io/api/v2"
SYMBOLS = ["KAS_USDT", "ZKAS_USDT"]
SYMBOL_COLORS = {"KAS_USDT": "#2fb3a3", "ZKAS_USDT": "#7c6cf0"}

# El estado de DRY_RUN se lee del bot si está en la misma carpeta; si no se
# puede importar (bot ausente, renombrado, etc.) se cae a un valor local y se
# avisa en la interfaz en vez de fallar.
try:
    from nonkyc_trading_bot import DRY_RUN as BOT_DRY_RUN
    from nonkyc_trading_bot import TRADE_USDT_PER_ORDER as DEFAULT_TRADE_USDT
    DRY_RUN_SOURCE = "importado de nonkyc_trading_bot.py"
except Exception:
    BOT_DRY_RUN = True
    DEFAULT_TRADE_USDT = 100.0
    DRY_RUN_SOURCE = "valor local (no se pudo importar el bot)"


async def fetch_ticker(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    url = f"{BASE_URL}/ticker/{symbol}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
    except aiohttp.ClientError:
        pass
    return None


async def fetch_all_tickers(symbols: list[str]) -> dict[str, dict | None]:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(fetch_ticker(session, s) for s in symbols))
        return dict(zip(symbols, results))


def extract_price(ticker: dict | None) -> float | None:
    if not ticker:
        return None
    raw = ticker.get("last_price") or ticker.get("price") or ticker.get("last")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def init_state() -> None:
    if "history" not in st.session_state:
        # Primera carga de esta sesión del navegador: recupera lo que haya
        # quedado guardado en price_history.json en vez de empezar en blanco.
        saved = price_history.load_history()
        for row in saved:
            try:
                row["time"] = datetime.fromisoformat(row["time"])
            except (KeyError, ValueError):
                continue
        st.session_state.history = saved
    if "last_prices" not in st.session_state:
        st.session_state.last_prices = {s: None for s in SYMBOLS}
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = None


def record_prices(tickers: dict[str, dict | None]) -> None:
    now = datetime.now()
    new_points = []
    for symbol in SYMBOLS:
        price = extract_price(tickers.get(symbol))
        if price is None:
            continue
        point = {"time": now, "symbol": symbol, "price": price}
        st.session_state.history.append(point)
        st.session_state.last_prices[symbol] = price
        new_points.append(point)

    if st.session_state.history:
        df = pd.DataFrame(st.session_state.history)
        trimmed = df.groupby("symbol", group_keys=False).tail(price_history.MAX_POINTS_PER_SYMBOL)
        st.session_state.history = trimmed.to_dict("records")

    if new_points:
        # Persiste en JSON (tiempos como texto ISO, serializable) para que la
        # curva sobreviva a un reinicio del panel.
        to_save = [{**row, "time": row["time"].isoformat()} for row in st.session_state.history]
        price_history.save_history(to_save)

    st.session_state.last_refresh = now


def force_take_profit(symbol: str) -> None:
    """Botón de testeo: toma de beneficios parcial (25%) para un token concreto,
    al precio de mercado actual de ese símbolo. Usado por los dos botones
    independientes KAS/ZKAS en la barra lateral."""
    price = st.session_state.last_prices.get(symbol)
    if price is None:
        st.warning(f"Todavía no hay precio de mercado para {symbol}.")
        st.rerun()
        return

    test_wallet = paper_wallet.load_wallet()
    if paper_wallet.has_pending_orders(test_wallet, symbol):
        st.warning(
            f"Ya hay un ciclo de recompra activo para {symbol}: espera a que sus "
            "órdenes limit se ejecuten, expiren (24h) o las canceles a mano."
        )
        st.rerun()
        return

    trade = paper_wallet.take_profit_sell(test_wallet, symbol, price)
    if trade:
        paper_wallet.save_wallet(test_wallet)
        st.success(
            f"Vendidos {trade['quantity']:.6f} {symbol} @ {price:,.6f} "
            f"(PnL {trade['pnl']:+,.2f} USDT) · {len(trade['ladder_order_ids'])} órdenes limit creadas"
        )
    else:
        st.warning(f"No tienes balance de {symbol} para vender un 25%.")
    st.rerun()


def compute_y_domain(values: list[float], padding_ratio: float = 0.12) -> list[float]:
    """Rango [min, max] con margen para el eje Y, en vez de forzar que arranque en 0 —
    así la acción del precio real llena el gráfico en lugar de verse como una línea plana.
    `values` debe incluir ya el precio de entrada (si hay uno) para que la línea de
    entrada nunca quede fuera de rango."""
    lo, hi = min(values), max(values)
    span = hi - lo
    # Mercado casi plano en esta ventana: usa un margen relativo mínimo (no
    # absoluto) para que siga siendo proporcional al precio del par (KAS y
    # ZKAS difieren en más de un orden de magnitud).
    pad = span * padding_ratio if span > 0 else max(abs(lo), abs(hi), 1e-9) * 0.01
    return [lo - pad, hi + pad]


# Naranja, amarillo, verde claro — en ese orden para ORDER LIMIT 1/2/3; si hay
# más de 3 órdenes activas en el mismo par (p. ej. una manual encima del ciclo
# automático), el ciclo de colores se repite.
ORDER_LINE_COLORS = ["#f5a623", "#e8d44d", "#8de971"]


def build_price_chart(
    symbol_df: pd.DataFrame,
    color: str,
    entry_price: float | None,
    pending_orders: list[dict] | None = None,
) -> alt.LayerChart:
    """Curva de precio suavizada + área de relleno + marcas en cada lectura real +
    tooltip al pasar el cursor, más una línea horizontal marcando el precio medio
    de entrada si hay token acumulado de ese símbolo, más una línea horizontal
    discontinua por cada orden LIMIT activa de `pending_orders` (ya filtradas al
    símbolo de este gráfico), etiquetada "ORDER LIMIT N" con su precio objetivo.
    El eje Y hace zoom al rango real de precios (+ entrada + órdenes) en vez de
    arrancar siempre en 0."""
    pending_orders = pending_orders or []
    y_values = symbol_df["price"].tolist()
    if entry_price is not None:
        y_values.append(entry_price)
    y_values.extend(order["target_price"] for order in pending_orders)
    y_domain = compute_y_domain(y_values)

    base = alt.Chart(symbol_df).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False)),
        y=alt.Y(
            "price:Q",
            title="Precio (USDT)",
            scale=alt.Scale(domain=y_domain, zero=False, nice=False),
            axis=alt.Axis(gridOpacity=0.15),
        ),
    )
    area = base.mark_area(interpolate="monotone", line=False, opacity=0.12, color=color)
    # `point=True` marca cada lectura real recopilada sobre la curva suavizada,
    # no solo la interpolación entre ellas.
    line = base.mark_line(
        interpolate="monotone", color=color, strokeWidth=2.2,
        point=alt.OverlayMarkDef(size=32, filled=True, color=color),
    )
    # Puntos invisibles con radio de impacto más ancho que la línea, solo para
    # que el tooltip aparezca con facilidad al pasar el cursor cerca.
    hover_points = base.mark_circle(size=90, opacity=0).encode(
        tooltip=[alt.Tooltip("time:T", title="Hora", format="%H:%M:%S"), alt.Tooltip("price:Q", title="Precio", format=".6f")],
    )

    layers = [area, line, hover_points]

    if entry_price is not None:
        entry_df = pd.DataFrame({"price": [entry_price]})
        entry_rule = alt.Chart(entry_df).mark_rule(color="#f5a623", strokeDash=[6, 4], size=1.5).encode(y="price:Q")
        entry_label = alt.Chart(entry_df).mark_text(
            align="left", baseline="bottom", dx=4, dy=-4, color="#f5a623", fontSize=11, fontWeight="bold",
        ).encode(y="price:Q", text=alt.value(f"Entrada: ${entry_price:,.6f}"))
        layers += [entry_rule, entry_label]

    for i, order in enumerate(pending_orders):
        line_color = ORDER_LINE_COLORS[i % len(ORDER_LINE_COLORS)]
        order_df = pd.DataFrame({"price": [order["target_price"]]})
        order_rule = alt.Chart(order_df).mark_rule(color=line_color, strokeDash=[4, 3], size=1.3).encode(y="price:Q")
        order_label = alt.Chart(order_df).mark_text(
            align="left", baseline="top", dx=4, dy=4, color=line_color, fontSize=10, fontWeight="bold",
        ).encode(y="price:Q", text=alt.value(f"ORDER LIMIT {i + 1}: ${order['target_price']:,.6f}"))
        layers += [order_rule, order_label]

    return alt.layer(*layers).properties(height=320).configure_view(strokeWidth=0)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="KASMAN · NonKYC Bot Dashboard", page_icon="📈", layout="wide")
init_state()

st.title("📈 NonKYC Trading Bot — Panel en vivo")
st.caption("KAS/USDT y ZKAS/USDT · datos públicos de api.nonkyc.io")

with st.sidebar:
    st.header("Controles")
    auto_refresh = st.checkbox("Auto-actualizar", value=True)
    refresh_seconds = st.slider("Intervalo de actualización (segundos)", 10, 120, 30, step=5)
    st.divider()
    if st.button("🔄 Actualizar ahora"):
        st.rerun()
    st.divider()
    st.caption(f"Símbolos monitoreados: {', '.join(SYMBOLS)}")

    st.divider()
    st.header("🧪 Herramientas de Testeo / Modo Manual")
    st.caption("Para probar la cartera virtual sin esperar a un cruce de medias real. Escribe directamente en paper_wallet.json.")

    test_symbol = st.selectbox("Símbolo", SYMBOLS, key="test_symbol")
    live_price = st.session_state.last_prices.get(test_symbol)

    use_live_price = st.checkbox("Usar precio actual de mercado", value=True, key="use_live_price")
    if use_live_price and live_price is not None:
        manual_price = live_price
        st.caption(f"Precio actual: ${live_price:,.6f}")
    else:
        manual_price = st.number_input(
            "Precio manual (USDT)",
            min_value=0.0,
            value=float(live_price or 0.01),
            format="%.6f",
            key="manual_price",
        )

    order_type = st.radio("Tipo de Orden", ["Mercado", "Limit"], horizontal=True, key="order_type")

    if order_type == "Mercado":
        manual_usdt = st.number_input(
            "USDT virtuales a invertir (BUY)",
            min_value=0.0,
            value=float(DEFAULT_TRADE_USDT),
            step=10.0,
            key="manual_usdt",
        )

        buy_col, sell_col = st.columns(2)
        with buy_col:
            if st.button("🟢 Forzar BUY", width="stretch"):
                test_wallet = paper_wallet.load_wallet()
                trade = paper_wallet.buy(test_wallet, test_symbol, manual_price, manual_usdt)
                if trade:
                    paper_wallet.save_wallet(test_wallet)
                    st.success(f"BUY: {trade['quantity']:.6f} {test_symbol} @ {manual_price:,.6f}")
                else:
                    st.error("No se pudo comprar (precio/monto inválido o sin saldo libre).")
                st.rerun()
        with sell_col:
            if st.button("🔴 Forzar SELL total", width="stretch"):
                test_wallet = paper_wallet.load_wallet()
                trade = paper_wallet.sell(test_wallet, test_symbol, manual_price)
                if trade:
                    paper_wallet.save_wallet(test_wallet)
                    st.success(f"SELL: PnL {trade['pnl']:+,.2f} USDT")
                else:
                    st.error(f"No hay balance acumulado de {test_symbol} para vender.")
                st.rerun()
    else:
        limit_side_label = st.radio(
            "Lado de la orden", ["Comprar (LIMIT_BUY)", "Vender (LIMIT_SELL)"], key="limit_side"
        )
        is_limit_buy = limit_side_label.startswith("Comprar")
        limit_target_price = st.number_input(
            "Precio Objetivo (USDT)",
            min_value=0.0,
            value=float(manual_price or 0.01),
            format="%.6f",
            key="limit_target_price",
        )
        preview_wallet = paper_wallet.load_wallet()
        if is_limit_buy:
            limit_usdt_amount = st.number_input(
                "USDT a reservar",
                min_value=0.0,
                value=float(DEFAULT_TRADE_USDT),
                step=10.0,
                key="limit_usdt_amount",
            )
            st.caption(f"USDT libres disponibles: ${preview_wallet['usdt_balance']:,.2f}")
        else:
            available_qty = preview_wallet["balances"].get(test_symbol, {}).get("quantity", 0.0)
            limit_token_qty = st.number_input(
                f"Cantidad de {test_symbol.split('_')[0]} a reservar",
                min_value=0.0,
                value=float(available_qty),
                format="%.6f",
                key="limit_token_qty",
            )
            st.caption(f"Balance disponible: {available_qty:,.6f} {test_symbol}")

        if st.button("📝 Colocar Orden Limit Manual", width="stretch"):
            test_wallet = paper_wallet.load_wallet()
            if is_limit_buy:
                order = paper_wallet.create_manual_limit_order(
                    test_wallet, test_symbol, "BUY", limit_target_price, limit_usdt_amount
                )
            else:
                order = paper_wallet.create_manual_limit_order(
                    test_wallet, test_symbol, "SELL", limit_target_price, limit_token_qty
                )
            if order:
                paper_wallet.save_wallet(test_wallet)
                st.success(
                    f"Orden #{order['id']} creada: {order['type']} {test_symbol} "
                    f"@ ${limit_target_price:,.6f} ({order['token_amount']:.6f} token)"
                )
            else:
                st.warning("No se pudo crear la orden (precio/cantidad inválidos o saldo/balance insuficiente).")
            st.rerun()

    st.caption(
        f"Toma de beneficios parcial: vende {paper_wallet.SELL_FRACTION:.0%} del balance "
        "acumulado de cada token (al precio de mercado actual) y crea sus 3 "
        "órdenes LIMIT de recompra escalonada."
    )
    if st.button("🟢 Forzar Toma de Beneficios KAS (25%)", width="stretch"):
        force_take_profit("KAS_USDT")
    if st.button("🟣 Forzar Toma de Beneficios ZKAS (25%)", width="stretch"):
        force_take_profit("ZKAS_USDT")

    st.divider()
    st.caption(
        "Carga balance de prueba del símbolo seleccionado arriba, descontando su "
        "equivalente en USDT libres al precio usado arriba (de mercado o manual)."
    )
    test_target_qty = paper_wallet.TEST_BALANCE_TARGETS.get(test_symbol, 0.0)
    test_token_label = test_symbol.split("_")[0]
    if st.button(f"📦 Cargar Balance de Prueba ({test_target_qty:,.0f} {test_token_label})", width="stretch"):
        if manual_price <= 0:
            st.warning("No hay un precio válido para calcular el USDT necesario.")
        else:
            test_wallet = paper_wallet.load_wallet()
            usdt_needed = test_target_qty * manual_price
            trade = paper_wallet.buy(test_wallet, test_symbol, manual_price, usdt_needed)
            if trade:
                paper_wallet.save_wallet(test_wallet)
                st.success(
                    f"Cargados {trade['quantity']:,.4f} {test_symbol} @ {manual_price:,.6f} "
                    f"({trade['usdt_amount']:,.2f} USDT descontados)."
                )
            else:
                st.warning("No hay saldo libre suficiente para cargar ese balance de prueba.")
        st.rerun()

    st.divider()
    st.caption("Reiniciar borra todo el historial de pruebas y repone el capital inicial.")
    if st.button("♻️ Reiniciar cartera (1,000 USDT)", width="stretch"):
        paper_wallet.reset_wallet()
        st.success("Cartera reiniciada.")
        st.rerun()

    st.caption(
        f"Reiniciar cartera mixta: {paper_wallet.MIXED_WALLET_USDT:,.0f} USDT libres + "
        + " + ".join(f"{qty:,.0f} {sym.split('_')[0]}" for sym, qty in paper_wallet.MIXED_WALLET_BALANCES.items())
        + " (usa el precio de mercado actual como coste de entrada)."
    )
    if st.button("♻️ Reiniciar cartera mixta", width="stretch"):
        mixed_prices = {s: st.session_state.last_prices.get(s) for s in paper_wallet.MIXED_WALLET_BALANCES}
        mixed_wallet = paper_wallet.reset_wallet_mixed(mixed_prices)
        if mixed_wallet:
            st.success("Cartera mixta cargada.")
        else:
            st.warning("Todavía no hay precio de mercado para KAS y ZKAS — espera a la primera lectura y reintenta.")
        st.rerun()

# --- Estado del bot (DRY_RUN) ------------------------------------------------
status_col, time_col = st.columns([3, 2])
with status_col:
    if BOT_DRY_RUN:
        st.success("🟢 Modo simulación (DRY_RUN=True) — no se ejecutan órdenes reales.")
    else:
        st.error("🔴 DRY_RUN=False — el bot está configurado para operar con fondos reales.")
    st.caption(f"Fuente del estado: {DRY_RUN_SOURCE}")
with time_col:
    if st.session_state.last_refresh:
        st.metric("Última actualización", st.session_state.last_refresh.strftime("%H:%M:%S"))

st.divider()

# --- Obtener precios y registrarlos (antes de valorar la cartera, para que
# use los precios de este mismo ciclo y no los del rerun anterior) ----------
tickers = asyncio.run(fetch_all_tickers(SYMBOLS))
record_prices(tickers)

# --- Resumen de la cartera virtual (Paper Trading) ---------------------------
st.subheader("💼 Cartera virtual (Paper Trading)")
wallet = paper_wallet.load_wallet()
current_prices = {s: p for s, p in st.session_state.last_prices.items() if p is not None}

# Motor de ejecución simulado: con los precios recién refrescados, comprueba si
# alguna orden LIMIT de recompra escalonada ya alcanzó su precio objetivo.
filled_orders = paper_wallet.match_and_execute_orders(wallet, current_prices)
# Rebalanceo por tiempo: lo que no se llenó y ya lleva >24h pendiente se
# cancela solo, liberando su USDT reservado.
expired_orders = paper_wallet.expire_stale_orders(wallet)
if filled_orders or expired_orders:
    paper_wallet.save_wallet(wallet)
    for order_trade in filled_orders:
        st.toast(
            f"✅ Orden LIMIT #{order_trade['order_id']} ejecutada: "
            f"{order_trade['quantity']:.6f} {order_trade['symbol']} @ {order_trade['price']:,.6f}"
        )
    for order in expired_orders:
        st.toast(
            f"⏱️ Orden LIMIT #{order['id']} expirada (>{paper_wallet.ORDER_EXPIRY_HOURS:.0f}h) en "
            f"{order['symbol']}: {order['usdt_amount']:,.2f} USDT devueltos al saldo libre."
        )

total_value = paper_wallet.portfolio_value(wallet, current_prices)
total_pnl = total_value - wallet["initial_capital"]
total_pnl_pct = (total_pnl / wallet["initial_capital"] * 100) if wallet["initial_capital"] else 0.0
reserved_usdt = sum(order["usdt_amount"] for order in wallet["active_orders"])

st.markdown("**Desglose de balances**")
bcol1, bcol2, bcol3, bcol4 = st.columns(4)
bcol1.metric("USDT libres", f"${wallet['usdt_balance']:,.2f}")
for col, symbol in zip((bcol2, bcol3), SYMBOLS):
    token_label = symbol.split("_")[0]
    balance = wallet["balances"].get(symbol, {})
    quantity = balance.get("quantity", 0.0)
    price = current_prices.get(symbol)
    value_text = f"≈ ${quantity * price:,.2f}" if price is not None else "sin precio"
    col.metric(token_label, f"{quantity:,.4f}", delta=value_text, delta_color="off")
bcol4.metric("USDT reservado (órdenes limit)", f"${reserved_usdt:,.2f}")

wcol1, wcol2, wcol3 = st.columns(3)
wcol1.metric(
    "Valor total de la cartera",
    f"${total_value:,.2f}",
    delta=f"{total_pnl:+,.2f} USDT ({total_pnl_pct:+.2f}%)",
)
wcol2.metric("Capital inicial", f"${wallet['initial_capital']:,.2f}")
wcol3.metric("Órdenes limit activas", f"{len(wallet['active_orders'])}")

st.markdown("**Historial de operaciones simuladas**")
if wallet["trade_history"]:
    trades_df = pd.DataFrame(wallet["trade_history"]).sort_values("time", ascending=False)
    st.dataframe(trades_df, width="stretch")
else:
    st.caption("Todavía no se ha simulado ninguna operación BUY/SELL.")

st.divider()

# --- Panel por par: precio + acumulación/PnL + gráfico, todo junto -----------
# (modo spot: sin posiciones cortas ni apalancamiento — el balance de token
# acumulado es la única fuente de verdad sobre si "hay posición" o no)
st.subheader("📊 Pares monitoreados")
history_df = pd.DataFrame(st.session_state.history)

pair_cols = st.columns(len(SYMBOLS))
for col, symbol in zip(pair_cols, SYMBOLS):
    with col:
        price = st.session_state.last_prices.get(symbol)
        symbol_df = history_df[history_df["symbol"] == symbol] if not history_df.empty else history_df

        delta = None
        if len(symbol_df) >= 2:
            delta = float(symbol_df["price"].iloc[-1] - symbol_df["price"].iloc[-2])

        st.markdown(f"#### {symbol.replace('_', '/')}")
        if price is None:
            st.metric("Precio", "sin datos")
        else:
            st.metric("Precio", f"${price:,.6f}", delta=f"{delta:+.6f}" if delta is not None else None)

        balance = wallet["balances"].get(symbol)
        if balance and balance.get("quantity", 0) > 0:
            pnl = paper_wallet.unrealized_pnl(wallet, symbol, price)
            mcol1, mcol2, mcol3, mcol4 = st.columns(4)
            mcol1.metric("Token acumulado", f"{balance['quantity']:,.4f}")
            mcol2.metric("Precio entrada", f"${balance['avg_entry_price']:,.6f}")
            if price is not None:
                mcol3.metric("Valor en USDT", f"${balance['quantity'] * price:,.2f}")
            else:
                mcol3.metric("Valor en USDT", "sin datos")
            if pnl:
                mcol4.metric("PnL no realizado", f"{pnl['pnl_usdt']:+,.2f} USDT", delta=f"{pnl['pnl_pct']:+.2f}%")
            else:
                mcol4.metric("PnL no realizado", "sin datos")
        else:
            st.caption("Sin token acumulado de este par en este momento.")

        if symbol_df.empty:
            st.info("Esperando la primera lectura de precios...")
        else:
            entry_price = balance["avg_entry_price"] if balance and balance.get("quantity", 0) > 0 else None
            # Órdenes LIMIT activas de este símbolo, de la más cercana (precio
            # objetivo más alto) a la más lejana, para que ORDER LIMIT 1 sea
            # siempre la más próxima al precio actual.
            symbol_orders = sorted(
                (o for o in wallet["active_orders"] if o["symbol"] == symbol),
                key=lambda o: o["target_price"],
                reverse=True,
            )
            chart = build_price_chart(symbol_df, SYMBOL_COLORS.get(symbol, "#2fb3a3"), entry_price, symbol_orders)
            st.altair_chart(chart, width="stretch")

st.divider()

# --- Órdenes LIMIT pendientes (recompra escalonada + órdenes manuales) ------
st.subheader("🎯 Órdenes Limit Pendientes")
if wallet["active_orders"]:
    buy_orders = [o for o in wallet["active_orders"] if o["type"] == "LIMIT_BUY"]
    sell_orders = [o for o in wallet["active_orders"] if o["type"] == "LIMIT_SELL"]
    reserved_usdt = sum(o["usdt_amount"] for o in buy_orders)
    st.caption(
        f"{len(wallet['active_orders'])} órdenes activas "
        f"({len(buy_orders)} compra, {len(sell_orders)} venta) · "
        f"{reserved_usdt:,.2f} USDT reservados en las de compra."
    )
    orders_df = pd.DataFrame(wallet["active_orders"])[
        ["id", "symbol", "type", "target_price", "token_amount", "usdt_amount", "created_at"]
    ]
    st.dataframe(orders_df.sort_values("target_price", ascending=False), width="stretch")

    for order in wallet["active_orders"]:
        ocol1, ocol2, ocol3 = st.columns([4, 1, 1])
        token_label = order["symbol"].split("_")[0]
        if order["type"] == "LIMIT_SELL":
            ocol1.caption(
                f"#{order['id']} · {order['symbol']} · vender {order['token_amount']:.6f} {token_label} "
                f"@ ${order['target_price']:,.6f} (≈{order['usdt_amount']:,.2f} USDT esperados)"
            )
        else:
            ocol1.caption(
                f"#{order['id']} · {order['symbol']} · comprar {order['token_amount']:.6f} {token_label} "
                f"@ ${order['target_price']:,.6f} ({order['usdt_amount']:,.2f} USDT reservados)"
            )
        if ocol2.button("✅ Forzar", key=f"force_order_{order['id']}", width="stretch"):
            test_wallet = paper_wallet.load_wallet()
            trade = paper_wallet.force_fill_order(test_wallet, order["id"])
            if trade:
                paper_wallet.save_wallet(test_wallet)
                st.success(f"Orden #{order['id']} ejecutada manualmente.")
            st.rerun()
        if ocol3.button("❌ Cancelar", key=f"cancel_order_{order['id']}", width="stretch"):
            test_wallet = paper_wallet.load_wallet()
            if paper_wallet.cancel_order(test_wallet, order["id"]):
                paper_wallet.save_wallet(test_wallet)
                refund_text = "token devuelto al balance" if order["type"] == "LIMIT_SELL" else "USDT devuelto al saldo libre"
                st.success(f"Orden #{order['id']} cancelada, {refund_text}.")
            st.rerun()
else:
    st.caption("No hay órdenes limit pendientes en este momento.")

with st.expander("Ver historial crudo de precios"):
    if history_df.empty:
        st.caption("Todavía no hay lecturas de precio registradas.")
    else:
        st.dataframe(history_df.sort_values("time", ascending=False), width="stretch")

# --- Auto-refresh (patrón estándar de Streamlit: sleep + rerun) -------------
if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
