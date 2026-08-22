"""Filtro de tipo de archivo al estilo aMule/eD2k, compartido entre la
pestaña de Búsqueda (`gui/widgets/search_tab.py`) y las búsquedas
guardadas en segundo plano (`core/saved_search_manager.py`, punto 8
del backlog): como no todos los protocolos soportados (Soulseek, DC++,
Gnutella2, BitTorrent) tienen un filtro de tipo en el propio wire
protocol de búsqueda, se aplica en cliente por extensión del nombre de
fichero -- funciona igual para las cinco redes."""

FILE_TYPE_EXTENSIONS: dict[str, frozenset[str]] = {
    "archive": frozenset({"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "cab", "arj"}),
    "audio": frozenset({"mp3", "flac", "wav", "ogg", "m4a", "aac", "wma", "ape", "opus", "mid", "midi"}),
    "cdimage": frozenset({"iso", "bin", "cue", "nrg", "mdf", "img"}),
    "picture": frozenset({"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp", "svg", "ico"}),
    "program": frozenset({"exe", "msi", "apk", "deb", "rpm", "dmg"}),
    "document": frozenset({"pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx", "epub", "mobi", "csv"}),
    "video": frozenset({"mp4", "mkv", "avi", "mov", "wmv", "flv", "mpg", "mpeg", "webm", "m4v", "ts", "vob"}),
}


def matches_file_type(title: str, file_type: str) -> bool:
    if file_type == "all":
        return True
    ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
    return ext in FILE_TYPE_EXTENSIONS.get(file_type, frozenset())
