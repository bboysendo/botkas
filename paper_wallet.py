"""
Cartera virtual de paper trading — modo SPOT puro, compartida entre el bot y
el dashboard.

No hay posiciones apalancadas, márgenes ni cortos: `buy()` simplemente gasta
USDT virtuales y ACUMULA esa cantidad exacta de token en
`balances[symbol]["quantity"]` (si ya tenías algo de ese token, se promedia
el precio de entrada con lo nuevo — igual que en un exchange spot real).
`buy()` puede llamarse varias veces seguidas sobre el mismo símbolo sin
restricción: cada llamada suma más token al balance ya acumulado. `sell()`
vende TODO el balance acumulado de ese token (liquidación total manual/legacy).

Estrategia de Acumulación Activa (venta parcial + recompra escalonada):
en vez de liquidar toda la posición en la señal de toma de beneficios,
`take_profit_sell()` vende solo una fracción (`SELL_FRACTION`, 25% por
defecto) del balance acumulado y usa el USDT obtenido para crear 3 órdenes
LIMIT_BUY escalonadas (`LADDER_LEVELS`) por debajo del precio de venta.
`create_manual_limit_order()` crea órdenes LIMIT sueltas a mano (herramienta
de testeo del dashboard), de compra o de venta (LIMIT_BUY/LIMIT_SELL) a
cualquier precio objetivo. En ambos casos lo comprometido por la orden queda
RESERVADO en `active_orders` apenas se crea — una LIMIT_BUY descuenta su
`usdt_amount` de `usdt_balance`; una LIMIT_SELL descuenta su `token_amount`
del balance del símbolo — así `portfolio_value()` nunca lo cuenta dos veces.
`match_and_execute_orders()` es el motor de ejecución simulado: compara el
precio de mercado actual contra cada orden pendiente y la llena (al precio
límite, no al de mercado) en cuanto se alcanza — hacia abajo para LIMIT_BUY,
hacia arriba para LIMIT_SELL, como en cualquier libro de órdenes real.

Máximo 1 ciclo de recompra activo por par: mientras `symbol` tenga alguna
orden LIMIT pendiente (automática o manual), `take_profit_sell()` se niega a
vender otro 25% de ese mismo símbolo (`has_pending_orders`) — evita apilar
ventas parciales sobre ventas parciales sin resolver. Este control es por
símbolo: KAS_USDT y ZKAS_USDT tienen ciclos totalmente independientes, así que
uno puede seguir con sus 3 órdenes pendientes mientras el otro ya las resolvió
y puede volver a tomar beneficios de inmediato. Y por si el precio nunca
vuelve a cruzar alguna orden: `expire_stale_orders()` cancela sola cualquier
orden que lleve `ORDER_EXPIRY_HOURS` (24h) sin ejecutarse y libera su reserva
(`_release_order_reservation`, también usada por `cancel_order`).

No existe ningún concepto de "posición abierta/cerrada" aparte del balance
de token en sí: si `quantity > 0` tienes el token; si es `0` no lo tienes.
Esa es la única fuente de verdad, tanto para el bot como para el dashboard.

nonkyc_trading_bot.py escribe este archivo (paper_wallet.json, en la misma
carpeta) automáticamente en cada señal BUY/SELL real y en cada orden limit
ejecutada. nonkyc_dashboard.py lo lee siempre, y además puede escribirlo bajo
demanda a través del panel "🧪 Herramientas de Testeo" (forzar BUY/SELL,
cancelar/forzar órdenes limit, reiniciar cartera) — son dos procesos
separados que pueden escribir en momentos distintos, así que cada escritura
sigue siendo atómica (archivo temporal + os.replace) para que quien lea
nunca vea el archivo a medio escribir. Si el bot y una acción manual
coinciden en el mismo instante exacto, gana la última escritura (pérdida de
una actualización, no corrupción); aceptable para una herramienta de
pruebas locales.

Para reiniciar la cartera desde cero: usa el botón del panel, o borra
paper_wallet.json a mano (se vuelve a crear con INITIAL_CAPITAL_USDT en el
siguiente load_wallet()).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

WALLET_FILE = Path(__file__).resolve().parent / "paper_wallet.json"
INITIAL_CAPITAL_USDT = 1000.0

# Cartera "mixta" de pruebas (reset_wallet_mixed): arranca ya con balance de
# KAS y ZKAS en vez de 100% USDT, para poder probar la toma de beneficios sin
# esperar a que el bot acumule token primero.
MIXED_WALLET_USDT = 500.0
MIXED_WALLET_BALANCES = {
    "KAS_USDT": 5000.0,
    "ZKAS_USDT": 20000.0,
}

# Cantidades objetivo del botón de carga rápida de balance de prueba (barra
# lateral): a diferencia de MIXED_WALLET_BALANCES (estado inicial fijo), esto
# se puede pulsar en cualquier momento sobre una cartera ya en marcha.
TEST_BALANCE_TARGETS = {
    "KAS_USDT": 10000.0,
    "ZKAS_USDT": 20000.0,
}

# Fracción del balance acumulado que se vende en cada señal de toma de
# beneficios (el resto se queda acumulando, de ahí "acumulación activa").
SELL_FRACTION = 0.25

# Niveles de la recompra escalonada: (fracción del USDT obtenido en la venta,
# descuento bajo el precio de venta al que se coloca esa orden LIMIT).
LADDER_LEVELS = [
    (0.33, 0.035),
    (0.33, 0.075),
    (0.34, 0.12),
]

# Rebalanceo por tiempo: una orden LIMIT pendiente que lleve más de esto activa
# sin ejecutarse se cancela sola (ver expire_stale_orders) para no dejar USDT
# atrapado indefinidamente esperando un precio que no llega.
ORDER_EXPIRY_HOURS = 24.0


def _default_wallet() -> dict:
    return {
        "initial_capital": INITIAL_CAPITAL_USDT,
        "usdt_balance": INITIAL_CAPITAL_USDT,
        "balances": {},  # symbol -> {"quantity": float, "avg_entry_price": float}
        "active_orders": [],  # [{"id","type","symbol","target_price","token_amount","usdt_amount","created_at"}]
        "trade_history": [],  # dicts más recientes al final
    }


def _migrate_wallet(wallet: dict) -> dict:
    """Carteras guardadas antes de la Estrategia de Acumulación Activa usaban
    `holdings` y no tenían `active_orders`; las adapta en memoria al leerlas."""
    if "balances" not in wallet and "holdings" in wallet:
        wallet["balances"] = wallet.pop("holdings")
    wallet.setdefault("balances", {})
    wallet.setdefault("active_orders", [])
    wallet.setdefault("initial_capital", INITIAL_CAPITAL_USDT)
    wallet.setdefault("usdt_balance", INITIAL_CAPITAL_USDT)
    wallet.setdefault("trade_history", [])
    return wallet


def load_wallet() -> dict:
    if not WALLET_FILE.exists():
        wallet = _default_wallet()
        save_wallet(wallet)
        return wallet
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            wallet = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Archivo corrupto o leído a mitad de una escritura (no debería pasar
        # gracias al os.replace atómico, pero por si acaso): devolvemos una
        # cartera nueva EN MEMORIA sin guardarla, para no pisar un historial
        # que probablemente se pueda leer bien en el siguiente intento.
        return _default_wallet()
    return _migrate_wallet(wallet)


def save_wallet(wallet: dict) -> None:
    tmp_path = WALLET_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(wallet, f, indent=2)
    os.replace(tmp_path, WALLET_FILE)


def reset_wallet() -> dict:
    """Vuelve la cartera a su estado inicial: borra balances, órdenes e
    historial, repone INITIAL_CAPITAL_USDT."""
    wallet = _default_wallet()
    save_wallet(wallet)
    return wallet


def reset_wallet_mixed(current_prices: dict[str, float]) -> Optional[dict]:
    """Variante de pruebas de `reset_wallet()`: en vez de arrancar 100% en USDT,
    arranca con `MIXED_WALLET_USDT` libres y el balance de `MIXED_WALLET_BALANCES`
    ya cargado (KAS y ZKAS), usando el precio de mercado actual como coste de
    entrada de cada balance — así el PnL no realizado empieza en 0 en vez de
    salir inflado por un coste de entrada arbitrario. `initial_capital` se fija
    al valor total de la cartera justo tras cargarla, para que el PnL total
    también empiece en 0. Devuelve None (sin tocar el archivo) si falta el
    precio de mercado de algún símbolo de MIXED_WALLET_BALANCES."""
    if any(current_prices.get(symbol) is None for symbol in MIXED_WALLET_BALANCES):
        return None

    wallet = _default_wallet()
    wallet["usdt_balance"] = MIXED_WALLET_USDT
    for symbol, quantity in MIXED_WALLET_BALANCES.items():
        wallet["balances"][symbol] = {"quantity": quantity, "avg_entry_price": current_prices[symbol]}
    wallet["initial_capital"] = portfolio_value(wallet, current_prices)
    save_wallet(wallet)
    return wallet


def buy(wallet: dict, symbol: str, price: float, usdt_amount: float) -> Optional[dict]:
    """Gasta hasta `usdt_amount` USDT virtuales comprando `symbol` al precio actual y
    ACUMULA esa cantidad de token sobre lo que ya hubiera (modo spot: sin tope de
    una sola compra por símbolo — llamar varias veces sigue sumando balance)."""
    if price <= 0 or usdt_amount <= 0:
        return None
    usdt_amount = min(usdt_amount, wallet["usdt_balance"])
    if usdt_amount <= 0:
        return None

    quantity = usdt_amount / price
    balance = wallet["balances"].get(symbol, {"quantity": 0.0, "avg_entry_price": 0.0})

    # Si ya había token acumulado de antes, promedia el precio de entrada con
    # la compra nueva (coste medio ponderado, igual que en un exchange spot real).
    new_quantity = balance["quantity"] + quantity
    balance["avg_entry_price"] = (
        (balance["quantity"] * balance["avg_entry_price"] + quantity * price) / new_quantity
        if new_quantity > 0 else price
    )
    balance["quantity"] = new_quantity
    wallet["balances"][symbol] = balance
    wallet["usdt_balance"] -= usdt_amount

    trade = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "side": "BUY",
        "price": price,
        "quantity": quantity,
        "usdt_amount": usdt_amount,
        "pnl": None,
        "usdt_balance_after": wallet["usdt_balance"],
    }
    wallet["trade_history"].append(trade)
    return trade


def sell(wallet: dict, symbol: str, price: float) -> Optional[dict]:
    """Vende TODO el balance acumulado de `symbol` al precio actual, lo convierte a
    USDT y calcula el PnL realizado contra el precio medio de entrada. Liquidación
    total manual/legacy — para la toma de beneficios parcial del bot ver
    `take_profit_sell`."""
    if price <= 0:
        return None
    balance = wallet["balances"].get(symbol)
    if not balance or balance["quantity"] <= 0:
        return None

    quantity = balance["quantity"]
    entry_price = balance["avg_entry_price"]
    proceeds = quantity * price
    pnl = proceeds - (quantity * entry_price)

    wallet["usdt_balance"] += proceeds
    wallet["balances"][symbol] = {"quantity": 0.0, "avg_entry_price": 0.0}

    trade = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "side": "SELL",
        "price": price,
        "quantity": quantity,
        "usdt_amount": proceeds,
        "pnl": pnl,
        "usdt_balance_after": wallet["usdt_balance"],
    }
    wallet["trade_history"].append(trade)
    return trade


def _next_order_id(wallet: dict) -> int:
    return max((o["id"] for o in wallet["active_orders"]), default=0) + 1


def create_ladder_orders(wallet: dict, symbol: str, sell_price: float, usdt_amount: float) -> list[dict]:
    """Reparte `usdt_amount` en 3 órdenes LIMIT de compra escalonadas por debajo de
    `sell_price`, según `LADDER_LEVELS`. El USDT de cada orden queda reservado en
    `active_orders` (no se toca `usdt_balance`, que sigue siendo solo el USDT
    libre no comprometido)."""
    orders = []
    next_id = _next_order_id(wallet)
    for fraction, discount in LADDER_LEVELS:
        order_usdt = usdt_amount * fraction
        target_price = sell_price * (1 - discount)
        if order_usdt <= 0 or target_price <= 0:
            continue
        order = {
            "id": next_id,
            "type": "LIMIT_BUY",
            "symbol": symbol,
            "target_price": target_price,
            "usdt_amount": order_usdt,
            "token_amount": order_usdt / target_price,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        wallet["active_orders"].append(order)
        orders.append(order)
        next_id += 1
    return orders


def create_manual_limit_order(
    wallet: dict, symbol: str, side: str, target_price: float, amount: float
) -> Optional[dict]:
    """Orden LIMIT manual (herramienta de testeo), independiente de la recompra
    escalonada automática. `side` es "BUY" (reserva hasta `amount` USDT libres;
    se ejecuta cuando el precio cae a `target_price` o más abajo) o "SELL"
    (reserva hasta `amount` unidades ya acumuladas de `symbol`; se ejecuta
    cuando el precio sube a `target_price` o más arriba). El USDT o el token
    reservado se descuenta de inmediato (igual que las órdenes automáticas), así
    que `match_and_execute_orders`/`cancel_order`/`expire_stale_orders` las
    tratan exactamente igual. Devuelve None (sin tocar la cartera) si el precio
    o la cantidad no son válidos, o si no hay USDT libre / balance de token
    suficiente para reservar nada."""
    if target_price <= 0 or amount <= 0:
        return None
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return None

    order = {
        "id": _next_order_id(wallet),
        "type": f"LIMIT_{side}",
        "symbol": symbol,
        "target_price": target_price,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    if side == "BUY":
        usdt_amount = min(amount, wallet["usdt_balance"])
        if usdt_amount <= 0:
            return None
        order["usdt_amount"] = usdt_amount
        order["token_amount"] = usdt_amount / target_price
        wallet["usdt_balance"] -= usdt_amount
    else:
        balance = wallet["balances"].get(symbol, {"quantity": 0.0, "avg_entry_price": 0.0})
        token_amount = min(amount, balance.get("quantity", 0.0))
        if token_amount <= 0:
            return None
        order["token_amount"] = token_amount
        order["usdt_amount"] = token_amount * target_price
        # Coste de entrada del balance en el momento de reservar: así, si la
        # orden se cancela más tarde, cancel_order puede reintegrar el token al
        # balance re-promediando el coste correctamente en vez de perderlo.
        order["entry_price"] = balance["avg_entry_price"]
        balance["quantity"] -= token_amount
        if balance["quantity"] <= 1e-12:
            balance["quantity"] = 0.0
            balance["avg_entry_price"] = 0.0
        wallet["balances"][symbol] = balance

    wallet["active_orders"].append(order)
    return order


def has_pending_orders(wallet: dict, symbol: str) -> bool:
    """True si `symbol` ya tiene un ciclo de recompra escalonada en marcha (una o
    más de sus órdenes LIMIT siguen en `active_orders`). Máximo 1 ciclo activo
    por par: mientras esto sea True, `take_profit_sell` se niega a abrir uno
    nuevo para ese símbolo — hay que esperar a que las órdenes anteriores se
    ejecuten (`match_and_execute_orders`), expiren (`expire_stale_orders`) o se
    cancelen a mano (`cancel_order`)."""
    return any(order["symbol"] == symbol for order in wallet["active_orders"])


def take_profit_sell(wallet: dict, symbol: str, price: float, sell_fraction: float = SELL_FRACTION) -> Optional[dict]:
    """Estrategia de Acumulación Activa: en una señal de toma de beneficios,
    vende solo `sell_fraction` (25% por defecto) del balance acumulado de
    `symbol` al precio actual y usa el USDT obtenido para crear 3 órdenes LIMIT
    de recompra escalonada (`create_ladder_orders`), en vez de liquidar toda la
    posición. El precio medio de entrada del balance restante no cambia (sigue
    siendo el mismo coste por unidad). No hace nada (devuelve None) si `symbol`
    ya tiene un ciclo de recompra pendiente (ver `has_pending_orders`) — evita
    vender un 25% más sobre otro 25% ya vendido y a medio recomprar."""
    if price <= 0:
        return None
    if has_pending_orders(wallet, symbol):
        return None
    balance = wallet["balances"].get(symbol)
    if not balance or balance.get("quantity", 0) <= 0:
        return None

    sell_quantity = balance["quantity"] * sell_fraction
    if sell_quantity <= 0:
        return None

    entry_price = balance["avg_entry_price"]
    proceeds = sell_quantity * price
    pnl = proceeds - (sell_quantity * entry_price)

    balance["quantity"] -= sell_quantity
    if balance["quantity"] <= 1e-12:
        balance["quantity"] = 0.0
        balance["avg_entry_price"] = 0.0
    wallet["balances"][symbol] = balance

    ladder_orders = create_ladder_orders(wallet, symbol, price, proceeds)

    trade = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "side": "TAKE_PROFIT_SELL",
        "price": price,
        "quantity": sell_quantity,
        "usdt_amount": proceeds,
        "pnl": pnl,
        "usdt_balance_after": wallet["usdt_balance"],
        "ladder_order_ids": [o["id"] for o in ladder_orders],
    }
    wallet["trade_history"].append(trade)
    return trade


def _fill_order(wallet: dict, order: dict, fill_price: float) -> dict:
    """Acredita una orden LIMIT ejecutada, según su tipo:
    - LIMIT_BUY: suma `token_amount` al balance del símbolo (promediando el
      precio de entrada, igual que `buy()`) — el USDT ya se descontó al crear
      la orden.
    - LIMIT_SELL: suma `usdt_amount` al saldo libre — el token ya se descontó
      del balance al crear la orden.
    `fill_price` es el precio al que se considera ejecutada la orden (el
    precio límite, no el de mercado: simulación conservadora, sin "mejora de
    precio" por gap)."""
    symbol = order["symbol"]
    token_amount = order["token_amount"]

    if order["type"] == "LIMIT_SELL":
        wallet["usdt_balance"] += order["usdt_amount"]
        side = "LIMIT_SELL_FILLED"
    else:
        balance = wallet["balances"].get(symbol, {"quantity": 0.0, "avg_entry_price": 0.0})
        new_quantity = balance["quantity"] + token_amount
        balance["avg_entry_price"] = (
            (balance["quantity"] * balance["avg_entry_price"] + token_amount * fill_price) / new_quantity
            if new_quantity > 0 else fill_price
        )
        balance["quantity"] = new_quantity
        wallet["balances"][symbol] = balance
        side = "LIMIT_BUY_FILLED"

    trade = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "side": side,
        "price": fill_price,
        "quantity": token_amount,
        "usdt_amount": order["usdt_amount"],
        "pnl": None,
        "usdt_balance_after": wallet["usdt_balance"],
        "order_id": order["id"],
    }
    wallet["trade_history"].append(trade)
    return trade


def match_and_execute_orders(wallet: dict, current_prices: dict[str, float]) -> list[dict]:
    """Motor de ejecución simulado (simulated matching engine): recorre
    `active_orders` y ejecuta cada orden LIMIT cuyo precio objetivo ya fue
    alcanzado — para LIMIT_BUY, precio actual <= precio límite; para
    LIMIT_SELL, precio actual >= precio límite (dirección opuesta, como en
    cualquier libro de órdenes real). Debe llamarse en cada iteración del
    bot/dashboard justo después de refrescar los precios de mercado. Devuelve
    la lista de trades de las órdenes que se llenaron en esta pasada (ya
    quitadas de `active_orders`)."""
    filled = []
    remaining = []
    for order in wallet["active_orders"]:
        price = current_prices.get(order["symbol"])
        if price is None:
            remaining.append(order)
            continue
        reached = price >= order["target_price"] if order["type"] == "LIMIT_SELL" else price <= order["target_price"]
        if reached:
            filled.append(_fill_order(wallet, order, order["target_price"]))
        else:
            remaining.append(order)
    wallet["active_orders"] = remaining
    return filled


def _release_order_reservation(wallet: dict, order: dict) -> None:
    """Deshace la reserva de una orden LIMIT que se cancela o expira sin
    llenarse: para LIMIT_BUY devuelve su `usdt_amount` al saldo libre; para
    LIMIT_SELL reintegra su `token_amount` al balance del símbolo,
    re-promediando el coste de entrada con el `entry_price` que tenía guardado
    desde que se reservó (`create_manual_limit_order`)."""
    if order["type"] == "LIMIT_SELL":
        symbol = order["symbol"]
        returned_qty = order["token_amount"]
        returned_entry = order.get("entry_price", order["target_price"])
        balance = wallet["balances"].get(symbol, {"quantity": 0.0, "avg_entry_price": 0.0})
        new_quantity = balance["quantity"] + returned_qty
        balance["avg_entry_price"] = (
            (balance["quantity"] * balance["avg_entry_price"] + returned_qty * returned_entry) / new_quantity
            if new_quantity > 0 else returned_entry
        )
        balance["quantity"] = new_quantity
        wallet["balances"][symbol] = balance
    else:
        wallet["usdt_balance"] += order["usdt_amount"]


def expire_stale_orders(wallet: dict, max_age_hours: float = ORDER_EXPIRY_HOURS) -> list[dict]:
    """Rebalanceo por tiempo: cancela automáticamente cualquier orden LIMIT
    pendiente que lleve `max_age_hours` (24h por defecto) o más activa sin
    ejecutarse, liberando su reserva (ver `_release_order_reservation`) — igual
    que `cancel_order`, pero para todo el lote de una vez. Debe llamarse en
    cada iteración del bot/dashboard, junto a `match_and_execute_orders`
    (primero se intenta llenar al precio de mercado; lo que siga pendiente y ya
    sea viejo, se expira). Devuelve las órdenes expiradas (ya quitadas de
    `active_orders`)."""
    now = datetime.now()
    expired = []
    remaining = []
    for order in wallet["active_orders"]:
        try:
            created_at = datetime.fromisoformat(order["created_at"])
        except (KeyError, ValueError, TypeError):
            remaining.append(order)  # fecha ilegible: no se puede medir su edad, se deja como está
            continue
        age_hours = (now - created_at).total_seconds() / 3600.0
        if age_hours >= max_age_hours:
            _release_order_reservation(wallet, order)
            expired.append(order)
        else:
            remaining.append(order)
    wallet["active_orders"] = remaining
    return expired


def cancel_order(wallet: dict, order_id: int) -> bool:
    """Cancela una orden LIMIT pendiente y libera lo que tenía reservado (ver
    `_release_order_reservation`) — ni ese USDT ni ese token estaban
    "gastados", solo comprometidos."""
    for i, order in enumerate(wallet["active_orders"]):
        if order["id"] == order_id:
            _release_order_reservation(wallet, order)
            del wallet["active_orders"][i]
            return True
    return False


def force_fill_order(wallet: dict, order_id: int, price: Optional[float] = None) -> Optional[dict]:
    """Ejecuta manualmente una orden LIMIT pendiente (herramienta de testeo), al
    precio dado o, si no se indica, a su propio precio límite."""
    for i, order in enumerate(wallet["active_orders"]):
        if order["id"] == order_id:
            del wallet["active_orders"][i]
            fill_price = price if price is not None else order["target_price"]
            return _fill_order(wallet, order, fill_price)
    return None


def portfolio_value(wallet: dict, current_prices: dict[str, float]) -> float:
    """USDT libres + valor de mercado actual de todo el token acumulado + lo
    reservado en órdenes LIMIT pendientes (sigue siendo parte de la cartera,
    solo que comprometido a una compra/venta futura). Una LIMIT_BUY se valora
    por su `usdt_amount` reservado (es USDT real, ya se sabe exactamente cuánto
    es); una LIMIT_SELL se valora al precio de mercado actual del token
    reservado (no a `usdt_amount`, que es solo el ingreso que se espera *si*
    llega a su precio objetivo) — así colocar una orden de venta no hace saltar
    el valor total de la cartera de la nada."""
    value = wallet["usdt_balance"]
    for symbol, balance in wallet["balances"].items():
        price = current_prices.get(symbol)
        if price:
            value += balance["quantity"] * price
    for order in wallet["active_orders"]:
        if order["type"] == "LIMIT_SELL":
            price = current_prices.get(order["symbol"])
            value += order["token_amount"] * price if price else order["usdt_amount"]
        else:
            value += order["usdt_amount"]
    return value


def unrealized_pnl(wallet: dict, symbol: str, current_price: Optional[float]) -> Optional[dict]:
    """PnL no realizado del token acumulado en `symbol` al precio actual, en USDT y en %.

    Devuelve None si no hay balance de ese token o no se conoce el precio actual.
    """
    balance = wallet["balances"].get(symbol)
    if not balance or balance.get("quantity", 0) <= 0 or current_price is None:
        return None

    quantity = balance["quantity"]
    entry_price = balance["avg_entry_price"]
    pnl_usdt = (current_price - entry_price) * quantity
    pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
    return {
        "quantity": quantity,
        "entry_price": entry_price,
        "current_price": current_price,
        "pnl_usdt": pnl_usdt,
        "pnl_pct": pnl_pct,
    }
