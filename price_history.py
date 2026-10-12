"""
Historial de precios persistente (price_history.json, misma carpeta), para
que las curvas del panel de Streamlit sobrevivan a un reinicio o a una nueva
pestaña del navegador en vez de empezar siempre en blanco.

Solo nonkyc_dashboard.py escribe este archivo, en cada sondeo de precios.
Escritura atómica (archivo temporal + os.replace), igual que paper_wallet.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HISTORY_FILE = Path(__file__).resolve().parent / "price_history.json"
MAX_POINTS_PER_SYMBOL = 2000  # por símbolo; limita el tamaño del archivo y del gráfico


def load_history() -> list[dict]:
    """Devuelve la lista de lecturas guardadas: [{"time": iso, "symbol": ..., "price": ...}, ...]."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Archivo corrupto o leído a mitad de una escritura: no hay nada que
        # recuperar de forma segura, se empieza de cero en memoria sin pisar
        # un archivo que probablemente se pueda leer bien la próxima vez.
        return []


def save_history(rows: list[dict]) -> None:
    tmp_path = HISTORY_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    os.replace(tmp_path, HISTORY_FILE)


def trim(rows: list[dict], symbols: list[str]) -> list[dict]:
    """Recorta cada símbolo a los últimos MAX_POINTS_PER_SYMBOL puntos, preservando el orden temporal."""
    trimmed: list[dict] = []
    for symbol in symbols:
        symbol_rows = [r for r in rows if r.get("symbol") == symbol]
        trimmed.extend(symbol_rows[-MAX_POINTS_PER_SYMBOL:])
    return trimmed
