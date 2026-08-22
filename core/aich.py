"""
AICH (Advanced Intelligent Corruption Handling): mecanismo de eD2k para
verificar el contenido descargado a nivel de sub-bloque de 180 KiB
(`EMBLOCKSIZE`), en vez de la parte entera de ~9,3 MiB (`PARTSIZE`) como hace
la verificación MD4 preexistente en `backends/emule_backend.py`. Así, cuando
falla la comprobación MD4 de una parte, se puede identificar exactamente qué
sub-bloque está corrupto en lugar de descartar la parte completa.

El árbol es un Merkle tree binario sobre SHA1 (no MD4, y sin bytes
discriminadores como los 0x00/0x01 de TTH): las hojas son
`SHA1(hasta EMBLOCKSIZE bytes de datos)` y los nodos internos son
`SHA1(hash_izquierdo + hash_derecho)`. El particionado en cada nivel no es
una simple mitad de bytes sino la regla real de aMule (`CAICHHashTree`,
`SHAHashSet.cpp`): el número de bloques del nodo se reparte a la mitad
redondeando hacia arriba en la rama izquierda, y el tamaño base de cada hijo
pasa a ser `EMBLOCKSIZE` en cuanto su tamaño ya no supera `PARTSIZE`.

Este módulo solo calcula localmente las cantidades necesarias para verificar
un `OP_AICHANSWER` recibido de un peer: cuántos "verifying hashes" preceden
a los block-hashes propios de una parte (`levels_to_part`) y cuántos
block-hashes tiene esa parte (`block_count`). No reconstruye el master hash
completo del fichero (eso requeriría el árbol entero de todas las partes, y
en esta implementación el master hash se toma directamente del peer
conectado vía OP_AICHFILEHASHANS, sin el modelo de confianza multi-fuente de
aMule real).
"""

import hashlib

PARTSIZE = 9_728_000
EMBLOCKSIZE = 184_320


def block_sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def combine_sha1(left: bytes, right: bytes) -> bytes:
    return hashlib.sha1(left + right).digest()


def levels_to_part(file_size: int, part_index: int) -> int:
    """Profundidad (número de nodos hermanos, es decir "verifying hashes")
    desde la raíz del árbol AICH hasta la raíz del subárbol que cubre la
    parte `part_index` (de tamaño PARTSIZE, salvo la última si es menor)."""
    target_start = part_index * PARTSIZE
    target_size = min(PARTSIZE, file_size - target_start)

    start = 0
    data_size = file_size
    is_left = True
    base_size = EMBLOCKSIZE if file_size <= PARTSIZE else PARTSIZE
    levels = 0
    while not (start == target_start and data_size == target_size):
        n_blocks = -(-data_size // base_size)  # ceil(data_size / base_size)
        left_blocks = ((n_blocks + 1) if is_left else n_blocks) // 2
        left_size = left_blocks * base_size
        if target_start < start + left_size:
            data_size = left_size
            is_left = True
        else:
            start += left_size
            data_size -= left_size
            is_left = False
        base_size = EMBLOCKSIZE if data_size <= PARTSIZE else PARTSIZE
        levels += 1
    return levels


def block_count(part_size: int) -> int:
    """Número de bloques de EMBLOCKSIZE (hojas del subárbol) que cubren una
    parte de `part_size` bytes."""
    return -(-part_size // EMBLOCKSIZE)
