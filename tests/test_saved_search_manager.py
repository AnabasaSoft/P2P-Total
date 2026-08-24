"""core/saved_search_manager.py: clave usada para saber qué resultados
de una búsqueda guardada ya se habían visto en la comprobación anterior."""

from core.models import Network, SearchResult
from core.saved_search_manager import _result_key


def test_result_key_stable_for_same_result():
    result = SearchResult(network=Network.SOULSEEK, title="cancion.mp3", size_bytes=1000, source_id="a")
    assert _result_key(result) == _result_key(result)


def test_result_key_differs_by_title_size_or_network():
    base = SearchResult(network=Network.SOULSEEK, title="cancion.mp3", size_bytes=1000, source_id="a")
    other_title = SearchResult(network=Network.SOULSEEK, title="otra.mp3", size_bytes=1000, source_id="a")
    other_size = SearchResult(network=Network.SOULSEEK, title="cancion.mp3", size_bytes=2000, source_id="a")
    other_network = SearchResult(network=Network.DCPP, title="cancion.mp3", size_bytes=1000, source_id="a")
    keys = {_result_key(r) for r in (base, other_title, other_size, other_network)}
    assert len(keys) == 4


def test_result_key_ignores_source_id():
    # Dos fuentes distintas del mismo fichero (mismo título+tamaño) no
    # deben contar como resultados "nuevos" diferentes.
    a = SearchResult(network=Network.SOULSEEK, title="cancion.mp3", size_bytes=1000, source_id="usuario_a")
    b = SearchResult(network=Network.SOULSEEK, title="cancion.mp3", size_bytes=1000, source_id="usuario_b")
    assert _result_key(a) == _result_key(b)
