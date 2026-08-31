"""Bug real reportado por el usuario: "tanto en gnutella2 como en
e2dk/kad pone muchos nodos conocidos pero al abrir la ventana de
servidores conocidos, no sale ninguno". Causa: la pestaña de detalles
de red muestra `len(backend._discovered_hubs)`/`len(backend._kad_contacts)`
-descubiertos en vivo vía tráfico real de protocolo (`/KHL` en G2,
`OP_SERVERLIST` en eMule) mientras la red está conectada- pero el
diálogo "Servidores conocidos" (`_load_g2_hubs`/`_load_emule_servers`
en `gui/widgets/network_tab.py`) solo consultaba la caché en disco
-que ambos backends solo escriben en `disconnect()`, nunca mientras
siguen conectados- más una consulta externa fresca (GWebCache/
server.met público), sin mirar nunca el propio conjunto en memoria del
backend ya conectado. Arreglado pasándole ese conjunto en vivo
(`G2Backend.discovered_hubs`/`EMuleBackend.discovered_servers`, ya
existían como propiedades públicas) a los loaders vía
`functools.partial` desde `_on_browse_servers`."""

from gui.widgets.network_tab import _load_emule_servers, _load_g2_hubs


async def test_load_g2_hubs_includes_live_discovered_hubs(monkeypatch):
    import backends.g2_backend as g2_module

    monkeypatch.setattr(g2_module, "load_hub_cache", lambda: [])

    async def _no_hubs(**kwargs):
        return []

    monkeypatch.setattr(g2_module, "discover_hubs", _no_hubs)

    live = {("1.2.3.4", 6346)}
    entries = await _load_g2_hubs(live)
    assert {(e["host"], e["port"]) for e in entries} == live


async def test_load_g2_hubs_deduplicates_live_against_cache_and_discovered(monkeypatch):
    import backends.g2_backend as g2_module

    monkeypatch.setattr(g2_module, "load_hub_cache", lambda: [("1.2.3.4", 6346)])

    async def _one_discovered(**kwargs):
        return [("5.6.7.8", 6346)]

    monkeypatch.setattr(g2_module, "discover_hubs", _one_discovered)

    live = {("1.2.3.4", 6346), ("9.9.9.9", 6346)}
    entries = await _load_g2_hubs(live)
    assert {(e["host"], e["port"]) for e in entries} == {
        ("1.2.3.4", 6346), ("5.6.7.8", 6346), ("9.9.9.9", 6346),
    }


async def test_load_emule_servers_includes_live_discovered_servers(monkeypatch):
    import backends.emule_backend as emule_module

    async def _no_public_servers(**kwargs):
        return []

    monkeypatch.setattr(emule_module, "fetch_public_server_list", _no_public_servers)

    live = {("10.0.0.1", 4661)}
    entries = await _load_emule_servers(live)
    assert {(e["host"], e["port"]) for e in entries} == live


async def test_load_emule_servers_deduplicates_live_against_public_list(monkeypatch):
    import backends.emule_backend as emule_module

    async def _one_public(**kwargs):
        return [{"host": "10.0.0.1", "port": 4661, "name": "Servidor público"}]

    monkeypatch.setattr(emule_module, "fetch_public_server_list", _one_public)

    live = {("10.0.0.1", 4661), ("10.0.0.2", 4661)}
    entries = await _load_emule_servers(live)
    assert {(e["host"], e["port"]) for e in entries} == {
        ("10.0.0.1", 4661), ("10.0.0.2", 4661),
    }
    # el que también vino de la lista pública conserva su nombre, no se
    # duplica como entrada "solo host/port" sin datos.
    public_entry = next(e for e in entries if e["host"] == "10.0.0.1")
    assert public_entry["name"] == "Servidor público"
