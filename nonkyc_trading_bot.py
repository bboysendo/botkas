import asyncio
import os
from collections import deque

import aiohttp
from dotenv import load_dotenv

import paper_wallet

load_dotenv()

API_KEY = os.getenv("NONKYC_API_KEY", "")
API_SECRET = os.getenv("NONKYC_API_SECRET", "")
BASE_URL = "https://api.nonkyc.io/api/v2"

SYMBOLS = ["KAS_USDT", "ZKAS_USDT"]
DRY_RUN = True  # Modo simulación activado por seguridad

POLL_SECONDS = 60
MA_FAST = 5     # nº de sondeos (no velas reales: NonKYC no expone histórico por REST)
MA_SLOW = 20
TRADE_USDT_PER_ORDER = 100.0  # USDT virtuales que arriesga cada entrada BUY

# Historial de precios en memoria por símbolo (se pierde si se reinicia el bot;
# la cartera virtual en paper_wallet.json sí sobrevive a los reinicios).
price_history: dict[str, deque] = {symbol: deque(maxlen=200) for symbol in SYMBOLS}


async def get_ticker(session, symbol):
    url = f"{BASE_URL}/ticker/{symbol}"
    async with session.get(url) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data
        return None


def sma(values: deque, period: int):
    if len(values) < period:
        return None
    window = list(values)[-period:]
    return sum(window) / period


def check_signal(symbol: str, holding_quantity: float) -> str:
    """Cruce de medias móviles simple (MA_FAST vs MA_SLOW) sobre los precios sondeados,
    usado como proxy de señal de "resistencia/agotamiento" (NonKYC no expone velas
    reales de 15m por REST, solo el último precio — ver price_history más abajo).

    Modo spot puro: un cruce dorado siempre da BUY (se acumula más token aunque
    ya tengamos algo de antes, tipo DCA); un cruce de muerte da SELL (toma de
    beneficios parcial, ver take_profit_sell en paper_wallet.py) solo si hay
    balance de ese token. No hay un flag de "posición abierta" separado — el
    propio balance acumulado (`holding_quantity`) es la única fuente de verdad.
    """
    history = price_history[symbol]
    fast, slow = sma(history, MA_FAST), sma(history, MA_SLOW)
    if fast is None or slow is None or len(history) < 2:
        return "HOLD"  # todavía no hay suficiente historial

    prev_history = deque(list(history)[:-1], maxlen=history.maxlen)
    prev_fast, prev_slow = sma(prev_history, MA_FAST), sma(prev_history, MA_SLOW)
    if prev_fast is None or prev_slow is None:
        return "HOLD"

    golden_cross = prev_fast <= prev_slow and fast > slow
    death_cross = prev_fast >= prev_slow and fast < slow

    if golden_cross:
        return "BUY"
    if death_cross and holding_quantity > 0:
        return "SELL"
    return "HOLD"


async def main():
    print("Iniciando bot de trading para NonKYC...")
    print(f"Modo DRY_RUN: {DRY_RUN}")

    wallet = paper_wallet.load_wallet()
    print(
        f"Cartera virtual cargada: {wallet['usdt_balance']:.2f} USDT libres "
        f"(capital inicial {wallet['initial_capital']:.2f} USDT)"
    )

    async with aiohttp.ClientSession() as session:
        while True:
            current_prices: dict[str, float] = {}
            for symbol in SYMBOLS:
                ticker = await get_ticker(session, symbol)
                if not ticker:
                    print(f"[{symbol}] No se pudo obtener el ticker.")
                    continue

                raw_price = ticker.get("last_price") or ticker.get("price") or ticker.get("last")
                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    print(f"[{symbol}] Precio inválido en la respuesta: {raw_price!r}")
                    continue

                price_history[symbol].append(price)
                current_prices[symbol] = price

            # Motor de ejecución simulado: antes de evaluar nuevas señales,
            # comprueba si alguna orden LIMIT de recompra escalonada ya alcanzó
            # su precio objetivo con los precios recién sondeados.
            filled_orders = paper_wallet.match_and_execute_orders(wallet, current_prices)
            # Rebalanceo por tiempo: lo que no se llenó y ya lleva >24h pendiente
            # se cancela solo, liberando su USDT reservado.
            expired_orders = paper_wallet.expire_stale_orders(wallet)
            if filled_orders or expired_orders:
                paper_wallet.save_wallet(wallet)
                for order_trade in filled_orders:
                    print(
                        f"  -> {order_trade['side']} #{order_trade['order_id']}: "
                        f"{order_trade['quantity']:.6f} {order_trade['symbol']} @ {order_trade['price']}"
                    )
                for order in expired_orders:
                    print(
                        f"  -> ORDEN LIMIT #{order['id']} EXPIRADA (>{paper_wallet.ORDER_EXPIRY_HOURS:.0f}h) "
                        f"en {order['symbol']}: {order['usdt_amount']:.2f} USDT devueltos al saldo libre"
                    )

            for symbol, price in current_prices.items():
                balance_quantity = wallet["balances"].get(symbol, {}).get("quantity", 0.0)
                signal = check_signal(symbol, balance_quantity)
                print(
                    f"[{symbol}] Precio actual en vivo: {price} | balance: {balance_quantity:.6f} | señal: {signal}"
                )

                if signal == "BUY":
                    trade = paper_wallet.buy(wallet, symbol, price, TRADE_USDT_PER_ORDER)
                    if trade:
                        paper_wallet.save_wallet(wallet)
                        print(
                            f"  -> PAPER BUY {trade['quantity']:.6f} {symbol} @ {price} "
                            f"(gastado {trade['usdt_amount']:.2f} USDT virtuales, "
                            f"balance acumulado {wallet['balances'][symbol]['quantity']:.6f})"
                        )
                elif signal == "SELL":
                    if paper_wallet.has_pending_orders(wallet, symbol):
                        print(
                            f"  -> Señal SELL ignorada en {symbol}: ya hay un ciclo de recompra "
                            "activo (órdenes limit pendientes)."
                        )
                    else:
                        trade = paper_wallet.take_profit_sell(wallet, symbol, price)
                        if trade:
                            paper_wallet.save_wallet(wallet)
                            print(
                                f"  -> TOMA DE BENEFICIOS 25%: vendidos {trade['quantity']:.6f} {symbol} @ {price} "
                                f"(PnL {trade['pnl']:+.2f} USDT, {len(trade['ladder_order_ids'])} "
                                f"órdenes limit de recompra creadas)"
                            )

            print(
                f"Balance virtual libre: {wallet['usdt_balance']:.2f} USDT | "
                f"Órdenes limit activas: {len(wallet['active_orders'])}"
            )
            print(f"Esperando {POLL_SECONDS} segundos para la siguiente comprobación...\n")
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot detenido por el usuario.")
