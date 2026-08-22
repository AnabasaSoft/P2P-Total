"""
Cálculo de Tiger Tree Hash (TTH), el identificador de fichero usado por
DC++/ADC para búsquedas por hash y verificación de contenido — un árbol
de Merkle construido con `core/tiger.py` sobre hojas de 1024 bytes.

Convención estándar TTH (verificada contra la implementación de
referencia en C de RHash, módulo `tth.c`/`tth.h`, comparando esta
implementación con la salida de un pequeño programa C compilado ex
profeso para varios tamaños de mensaje — vacío, una hoja parcial, una
hoja exacta, dos y tres hojas...): cada hoja se hashea como
Tiger(0x00 + datos_de_hasta_1024_bytes); cada nodo interno del árbol
se hashea como Tiger(0x01 + hash_izquierdo + hash_derecho); cuando un
nivel del árbol tiene un número impar de nodos, el último nodo sin
pareja se propaga sin cambios al nivel siguiente (no se duplica ni se
descarta). El TTH de un fichero vacío es Tiger(0x00) (caso especial:
no hay ninguna hoja de datos, pero el propio byte discriminador 0x00
se hashea igualmente).
"""

from core.tiger import tiger

LEAF_SIZE = 1024


def _leaf_hash(data: bytes) -> bytes:
    return tiger(b"\x00" + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return tiger(b"\x01" + left + right).digest()


def tth_from_leaves(leaf_hashes: list[bytes]) -> bytes:
    """Combina hashes de hoja ya calculados en el hash TTH raíz."""
    if not leaf_hashes:
        return tiger(b"\x00").digest()
    level = leaf_hashes
    while len(level) > 1:
        next_level = []
        i = 0
        while i + 1 < len(level):
            next_level.append(_node_hash(level[i], level[i + 1]))
            i += 2
        if i < len(level):
            next_level.append(level[i])
        level = next_level
    return level[0]


def tth_of_bytes(data: bytes) -> bytes:
    leaves = [
        _leaf_hash(data[offset:offset + LEAF_SIZE])
        for offset in range(0, len(data), LEAF_SIZE)
    ]
    return tth_from_leaves(leaves)


def tth_of_file(path: str) -> bytes:
    leaves = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(LEAF_SIZE)
            if not chunk:
                break
            leaves.append(_leaf_hash(chunk))
    return tth_from_leaves(leaves)
