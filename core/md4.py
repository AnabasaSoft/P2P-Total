"""
Implementación nativa en Python puro del algoritmo de hash MD4 (RFC 1320).

eD2k identifica ficheros y trozos de fichero (partes de 9.28 MB) con
hashes MD4, y el OpenSSL de este sistema no lo trae habilitado
(hashlib.new("md4") lanza UnsupportedDigestmodError porque MD4 está
deshabilitado por ser criptográficamente roto para usos de seguridad,
pero eD2k lo sigue usando como simple checksum de integridad, no como
primitiva criptográfica). Como no podemos depender de que esté disponible
en cualquier máquina, lo implementamos a mano en vez de asumir un
paquete de sistema instalado.
"""

import struct

_S = (
    (3, 7, 11, 19),
    (3, 5, 9, 13),
    (3, 9, 11, 15),
)

_MASK = 0xFFFFFFFF


def _f(x, y, z):
    return (x & y) | (~x & z)


def _g(x, y, z):
    return (x & y) | (x & z) | (y & z)


def _h(x, y, z):
    return x ^ y ^ z


def _rotl(x, n):
    x &= _MASK
    return ((x << n) | (x >> (32 - n))) & _MASK


class MD4:
    """Interfaz compatible con hashlib: .update(data), .digest(), .hexdigest()."""

    digest_size = 16
    block_size = 64

    def __init__(self, data: bytes = b"") -> None:
        self._buffer = b""
        self._length = 0
        self._a, self._b, self._c, self._d = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
        if data:
            self.update(data)

    def update(self, data: bytes) -> None:
        # OJO rendimiento: si esto se escribiera como
        # `self._buffer += data` seguido de recortar `self._buffer =
        # self._buffer[64:]` en cada bloque de 64 bytes dentro del bucle,
        # cada recorte copiaría el búfer entero restante -> O(n) por
        # bloque, O(n²) en total para un `data` grande (p.ej. los chunks
        # de 1 MB que lee SharedLibrary.rescan() al hashear un fichero
        # compartido: con eso, hashear un solo fichero de varios GB podía
        # tardar horas). En su lugar, `self._buffer` solo guarda como
        # mucho 63 bytes de resto entre llamadas, y se avanza con un
        # índice sobre `data` en vez de ir reasignando el búfer entero.
        self._length += len(data)
        if self._buffer:
            data = self._buffer + data
        n = len(data)
        offset = 0
        while offset + 64 <= n:
            self._process_block(data[offset:offset + 64])
            offset += 64
        self._buffer = data[offset:]

    def _process_block(self, block: bytes) -> None:
        x = struct.unpack("<16I", block)
        a, b, c, d = self._a, self._b, self._c, self._d

        # Ronda 1
        for i in range(16):
            k = i
            s = _S[0][i % 4]
            a, b, c, d = d, _rotl((a + _f(b, c, d) + x[k]) & _MASK, s), b, c

        # Ronda 2
        for i in range(16):
            k = (i % 4) * 4 + (i // 4)
            s = _S[1][i % 4]
            a, b, c, d = d, _rotl((a + _g(b, c, d) + x[k] + 0x5A827999) & _MASK, s), b, c

        # Ronda 3
        order = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)
        for i in range(16):
            k = order[i]
            s = _S[2][i % 4]
            a, b, c, d = d, _rotl((a + _h(b, c, d) + x[k] + 0x6ED9EBA1) & _MASK, s), b, c

        self._a = (self._a + a) & _MASK
        self._b = (self._b + b) & _MASK
        self._c = (self._c + c) & _MASK
        self._d = (self._d + d) & _MASK

    def _finalize_state(self):
        """Aplica el padding MD4 estándar sobre una copia del estado y
        devuelve (a,b,c,d) finales, sin mutar self (para poder llamar a
        digest() más de una vez, como hace hashlib)."""
        msg_len_bits = (self._length * 8) & 0xFFFFFFFFFFFFFFFF
        pad_len = (56 - (self._length + 1) % 64) % 64
        padding = b"\x80" + b"\x00" * pad_len + struct.pack("<Q", msg_len_bits)

        a, b, c, d = self._a, self._b, self._c, self._d
        tail = self._buffer + padding
        for offset in range(0, len(tail), 64):
            block = tail[offset:offset + 64]
            saved = (self._a, self._b, self._c, self._d)
            self._a, self._b, self._c, self._d = a, b, c, d
            self._process_block(block)
            a, b, c, d = self._a, self._b, self._c, self._d
            self._a, self._b, self._c, self._d = saved
        return a, b, c, d

    def digest(self) -> bytes:
        a, b, c, d = self._finalize_state()
        return struct.pack("<4I", a, b, c, d)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def copy(self) -> "MD4":
        clone = MD4()
        clone._buffer = self._buffer
        clone._length = self._length
        clone._a, clone._b, clone._c, clone._d = self._a, self._b, self._c, self._d
        return clone


def md4(data: bytes = b"") -> MD4:
    return MD4(data)
