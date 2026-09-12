"""Precios por millón de tokens, para convertir el consumo medido en dólares.

La tabla lleva la fecha en que se consultó: un precio sin fecha envejece en silencio y el
contador de gasto empieza a mentir. Cuando el número no coincida con la factura, se corrige
acá y se mueve la fecha.

Un modelo que no esté en la tabla cuenta tokens y suma cero dólares, y `sin_precio` lo
enumera: la alternativa —adivinar un precio— haría que el freno de presupuesto corte por un
número inventado.
"""

import sys

import app.nucleo.mensajes as msj

CONSULTADO = "2026-09-06"

# USD por millón de tokens: (entrada, salida).
PRECIOS: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "claude-haiku-4-5": (1.00, 5.00),
}

_sin_precio: set[str] = set()


def costo(modelo: str, tokens_entrada: int, tokens_salida: int) -> float:
    """Los dólares que cuesta ese consumo, o 0.0 si el modelo no está en la tabla.

    **Un modelo sin tarifar avisa la primera vez que aparece.** Cero dólares se lee igual que
    "no gastó nada": el gasto del mes se queda quieto, el freno por presupuesto no dispara
    nunca, y desde afuera el sistema parece gratis. Cambiar `MODELO_SUPERVISOR` por uno que no
    esté en la tabla alcanza para eso, y nada en el registro lo delataba.
    """
    clave = _normalizar(modelo)
    if clave not in PRECIOS:
        nombre = modelo or "(sin nombre)"
        if nombre not in _sin_precio:
            _sin_precio.add(nombre)
            print(msj.AVISO_MODELO_SIN_PRECIO.format(modelo=nombre, fecha=CONSULTADO),
                  file=sys.stderr, flush=True)
        return 0.0
    entrada, salida = PRECIOS[clave]
    return (tokens_entrada * entrada + tokens_salida * salida) / 1_000_000


def sin_precio() -> list[str]:
    """Los modelos que se usaron y no están tarifados. Van al manifiesto de la corrida."""
    return sorted(_sin_precio)


def _normalizar(modelo: str) -> str:
    """Saca el sufijo de fecha que algunos identificadores traen ("gpt-4o-mini-2024-07-18")."""
    nombre = (modelo or "").strip()
    for conocido in PRECIOS:
        if nombre == conocido or nombre.startswith(conocido + "-"):
            return conocido
    return nombre
