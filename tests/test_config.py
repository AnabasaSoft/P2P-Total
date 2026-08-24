"""core/config.py: round-trip de guardado/carga contra un config.json
en un directorio temporal (pasando `path` explícito), para no tocar ni
el config.json real del usuario ni el almacén de credenciales del
sistema — `use_keyring` se calcula como `path is None` en
load_config/save_config, así que con `path` explícito las contraseñas
se guardan/leen tal cual del propio json, igual que en modo portable."""

from pathlib import Path

from core.config import Category, Config, load_config, save_config
from core.models import Network


def test_load_missing_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "no_existe.json")
    assert config == Config()


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "config.json"
    original = Config()
    original.soulseek.username = "usuario_prueba"
    original.soulseek.password = "contraseña_secreta"
    original.default_download_dir = "/tmp/descargas"
    original.shared_folders = ["/tmp/comparte1", "/tmp/comparte2"]
    original.global_download_limit_kbps = 512
    original.categories = [Category(name="Música", dest_dir="/tmp/musica")]

    save_config(original, path)
    loaded = load_config(path)

    assert loaded.soulseek.username == "usuario_prueba"
    assert loaded.soulseek.password == "contraseña_secreta"
    assert loaded.default_download_dir == "/tmp/descargas"
    assert loaded.shared_folders == ["/tmp/comparte1", "/tmp/comparte2"]
    assert loaded.global_download_limit_kbps == 512
    assert loaded.categories == [Category(name="Música", dest_dir="/tmp/musica")]


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "subdir_nueva" / "config.json"
    save_config(Config(), path)
    assert path.exists()


def test_legacy_shared_folder_singular_field_migrates_to_list(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"shared_folder": "/tmp/carpeta_antigua"}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.shared_folders == ["/tmp/carpeta_antigua"]


def test_partial_json_from_older_version_fills_in_defaults(tmp_path):
    # Un config.json de antes de añadir DC++/Gnutella2/eMule/proxy no
    # lleva esas claves: debe cargar igualmente con los valores por
    # defecto en vez de reventar con KeyError.
    path = tmp_path / "config.json"
    path.write_text('{"soulseek": {"username": "solo_esto"}}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.soulseek.username == "solo_esto"
    assert loaded.dcpp.default_hub_port == 411
    assert loaded.gnutella2.listen_port == 6346
    assert loaded.emule.nickname == "P2PTotalUser"


def test_saved_file_is_only_readable_by_owner(tmp_path):
    import stat

    path = tmp_path / "config.json"
    save_config(Config(), path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_auto_connect_defaults_to_false_for_every_network():
    config = Config()
    assert config.auto_connect_networks() == []


def test_auto_connect_round_trips_per_network(tmp_path):
    path = tmp_path / "config.json"
    original = Config()
    original.torrent.auto_connect = True
    original.emule.auto_connect = True

    save_config(original, path)
    loaded = load_config(path)

    assert loaded.torrent.auto_connect is True
    assert loaded.soulseek.auto_connect is False
    assert loaded.dcpp.auto_connect is False
    assert loaded.gnutella2.auto_connect is False
    assert loaded.emule.auto_connect is True
    assert loaded.auto_connect_networks() == [Network.TORRENT, Network.EMULE]


def test_auto_connect_defaults_to_false_when_missing_from_older_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"soulseek": {"username": "solo_esto"}}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.soulseek.auto_connect is False


def test_schedule_defaults_to_disabled():
    config = Config()
    assert config.schedule.enabled is False
    assert config.schedule.start == "22:00"
    assert config.schedule.end == "07:00"


def test_schedule_round_trips(tmp_path):
    path = tmp_path / "config.json"
    original = Config()
    original.schedule.enabled = True
    original.schedule.start = "23:00"
    original.schedule.end = "06:30"
    original.schedule.download_limit_kbps = 200
    original.schedule.upload_limit_kbps = 50

    save_config(original, path)
    loaded = load_config(path)

    assert loaded.schedule.enabled is True
    assert loaded.schedule.start == "23:00"
    assert loaded.schedule.end == "06:30"
    assert loaded.schedule.download_limit_kbps == 200
    assert loaded.schedule.upload_limit_kbps == 50


def test_schedule_defaults_to_disabled_when_missing_from_older_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"soulseek": {"username": "solo_esto"}}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.schedule.enabled is False


def test_remote_control_defaults_to_disabled_without_token():
    config = Config()
    assert config.remote_control.enabled is False
    assert config.remote_control.host == "127.0.0.1"
    assert config.remote_control.port == 8765
    assert config.remote_control.token == ""


def test_remote_control_round_trips(tmp_path):
    path = tmp_path / "config.json"
    original = Config()
    original.remote_control.enabled = True
    original.remote_control.host = "0.0.0.0"
    original.remote_control.port = 9000
    original.remote_control.token = "un_token_secreto"

    save_config(original, path)
    loaded = load_config(path)

    assert loaded.remote_control.enabled is True
    assert loaded.remote_control.host == "0.0.0.0"
    assert loaded.remote_control.port == 9000
    assert loaded.remote_control.token == "un_token_secreto"


def test_remote_control_defaults_to_disabled_when_missing_from_older_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"soulseek": {"username": "solo_esto"}}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.remote_control.enabled is False
    assert loaded.remote_control.token == ""
