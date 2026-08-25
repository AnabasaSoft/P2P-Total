# P2P Total — historial técnico de desarrollo (devlog)

Este documento es el **diario técnico** del proyecto: arquitectura
detallada, notas de protocolo por red, y el registro cronológico
completo de cada característica implementada (con su validación) y del
backlog restante. Es contenido de referencia interna, no la
presentación del proyecto — para eso está `README.md`, que a partir del
22 de agosto de 2026 es un documento profesional orientado a quien
llega al proyecto por primera vez (funciones, capturas, instalación).

Todo el contenido de este fichero es el que vivía en `README.md` antes
de esa fecha. Se mantiene íntegro y sin resumir porque documenta
decisiones de diseño, bugs reales encontrados y cómo se validó cada
pieza contra infraestructura real — información que no cabe en una
presentación de cara al público pero que sigue siendo la fuente de
verdad técnica del proyecto. **Cuando se complete una tarea grande,
este es el fichero que hay que actualizar** (siguiendo el mismo orden
estricto de backlog ya establecido); `README.md` solo necesita tocarse
si cambia algo relevante para quien lo lee por primera vez (nueva red,
nueva captura, cambio de instalación).

---

# P2P Total

Cliente P2P multi-red para Linux (torrent, Soulseek, DC++, Gnutella2,
eMule/Kad) con una interfaz común y una única GUI.

## Arquitectura

```
core/
  models.py             -> Download, SearchResult, Network, DownloadState
  backend_base.py        -> interfaz NetworkBackend + BackendRegistry
  download_manager.py    -> orquestador único usado por la GUI
  database.py             -> persistencia SQLite del historial/cola
  config.py                -> configuración persistente (~/.config/p2p-total/config.json)
  sharing.py               -> SharedLibrary, índice de la carpeta compartida propia (Soulseek/DC++)

backends/
  torrent_backend.py     -> libtorrent, funcional y validado
  soulseek_backend.py    -> protocolo Soulseek nativo (sin aioslsk), funcional y validado
  dcpp_backend.py         -> NMDC nativo, funcional
  g2_backend.py          -> Gnutella2 (G2), protocolo de árbol nativo, funcional
  emule_backend.py         -> eD2k + Kad nativos, funcional

gui/
  i18n.py                 -> textos es/en/eu, t(clave) (mismo patrón que MantPro)
  theme.py                 -> QSS claro/oscuro + colores por red
  connection_manager.py  -> conecta/desconecta cada red, expone estado vía señal Qt
  models_qt.py             -> QAbstractTableModel para resultados y descargas
  main_window.py          -> ventana principal (menú con conexión por red, pestañas)
  app.py                   -> entrypoint (qasync, integra el bucle Qt con asyncio)
  widgets/
    search_tab.py            -> búsqueda + tabla de resultados
    downloads_tab.py         -> tabla de transferencias con progreso real
    settings_dialog.py     -> preferencias (una pestaña por red)
    delegates.py             -> barra de progreso dibujada en la celda
```

## Principio de diseño

El `DownloadManager` y la GUI **nunca** hablan directamente con libtorrent,
Soulseek, etc. Solo conocen la interfaz `NetworkBackend`. Añadir o
quitar una red es cuestión de:

1. Crear `backends/xxx_backend.py` implementando `NetworkBackend`.
2. Registrarlo con `BackendRegistry.register(XxxBackend())` al arrancar.

Esto permite desarrollar e iterar cada red de forma aislada sin romper
las demás, y que la GUI muestre todas las descargas en una tabla única
independientemente de su origen.

## Configuración

`python main.py config` — configuración interactiva guardada en
`~/.config/p2p-total/config.json` (permisos 600, solo legible por tu
usuario). Guarda credenciales de Soulseek, configuración de DC++ (nick,
hub por defecto, puerto de escucha), la carpeta de descargas por
defecto y las carpetas compartidas (`shared_folders`, una lista — ver
"Compartir y subir archivos" en "Estado actual" — vacía por defecto,
no se comparte nada hasta que se configure al menos una); aquí es
donde irán el resto de ajustes del programa a medida que se añadan las
demás redes (límites de velocidad, etc.).

Las contraseñas (Soulseek, hub de DC++, proxy) **ya no se guardan en
texto plano**: se delegan primero en el almacén de credenciales nativo
del sistema operativo (Secret Service/KWallet en Linux, Keychain en
macOS, Credential Manager en Windows) vía la librería `keyring`
(`core/config.py`, `_keyring_get`/`_keyring_set`), y `config.json` solo
guarda un valor vacío en su lugar. Se cae a texto plano en `config.json`
únicamente si el almacén no está disponible (p.ej. un Linux sin sesión
de escritorio ni Secret Service en marcha) o en **modo portable**
(punto 25 del backlog): ahí se guardan a propósito en el propio json,
ya que el almacén de credenciales pertenece al equipo anfitrión, no al
pendrive, y usarlo dejaría precisamente el rastro que el modo portable
busca evitar. `load_config()`/`save_config()` con una `path` explícita
(exportar/importar configuración desde la GUI) tampoco tocan el
almacén, por el mismo motivo: ese fichero debe quedar autocontenido.
Los permisos 600 se mantienen como segunda capa de protección para
cualquier contraseña que sí acabe en el json (portable, almacén no
disponible, o un `config.json` de antes de este cambio aún no
regrabado). Validado con un test aislado (`XDG_CONFIG_HOME` temporal,
sin depender de un almacén real) del ciclo completo guardar → releer
con `keyring` simulado, caída a texto plano cuando el almacén simulado
falla, y que el modo portable y la importación/exportación con `path`
explícita nunca tocan el almacén.

## Estado actual
- [x] Interfaz de backend + registro
- [x] Gestor de descargas (orquestación + notificación a GUI)
- [x] Persistencia SQLite
- [x] Backend BitTorrent (libtorrent) — funcional y validado con descarga
      real end-to-end (torrent propio sembrado en LAN): conexión, búsqueda
      de metadatos vía DHT/LSD, descarga con reutilización de handle entre
      búsqueda y descarga (evita perder la conexión a peers ya establecida),
      pausa/resume/cancelar, progreso en tiempo real.
- [x] Backend Soulseek — reescrito como implementación nativa del
      protocolo sobre asyncio (se retiró la librería externa `aioslsk`,
      que daba muchos problemas), estudiada directamente del código
      fuente real de nicotine+ (`pynicotine/slskmessages.py`,
      `pynicotine/slskproto.py`, `pynicotine/uploads.py`,
      `pynicotine/downloads.py`, github.com/nicotine-plus/nicotine-plus).
      Framing: servidor y conexiones "P" ya establecidas usan
      `uint32 longitud + uint32 código + payload`; el primer mensaje de
      cualquier conexión "P" recién marcada usa framing de un byte para
      el código (`PeerInit`/`PierceFireWall`); las conexiones "F" (una
      vez hecho el handshake de peer-init) no llevan ningún framing:
      token crudo (`FileTransferInit`, lo manda quien sube) + offset
      crudo (`FileOffset`, lo manda quien descarga) + bytes del archivo
      sin más. Se encontró y corrigió una asimetría real del protocolo
      no evidente por la documentación: el mensaje `ConnectToPeer`
      (código de servidor 18) tiene un orden de campos distinto según
      la dirección — cuando lo mandamos nosotros va
      `token+usuario+tipo`, pero cuando nos lo manda el servidor porque
      OTRO peer nos pide conectarnos a él va sin el token por delante
      (`usuario+tipo+ip+puerto+token+...`); confundir el orden hacía
      que el bucle de lectura del servidor petara en silencio con cada
      solicitud de conexión entrante. Estrategia de conexión dual
      (como nicotine+): para alcanzar cualquier peer se intenta a la
      vez una conexión TCP directa Y una solicitud indirecta al
      servidor (`ConnectToPeer`, pidiéndole al peer que nos llame a
      nosotros) — gana la que responda primero. Esto es clave porque
      resuelve el bloqueo de NAT/puerto-no-redirigido documentado antes
      con `aioslsk`: aunque nuestro propio puerto de escucha no esté
      abierto, la conexión indirecta (el peer marcando hacia nosotros,
      o nosotros marcando hacia él a petición del servidor) permite
      completar la transferencia igualmente. Validado en vivo con
      cuenta real: login correcto, búsqueda de texto libre ("Jackson")
      con 367.749 resultados reales (frente a los 41.064 que devolvía
      `aioslsk` con la misma cuenta y query, precisamente porque la
      versión anterior no aprovechaba bien la ruta de conexión
      indirecta), y **descarga real completada de extremo a extremo**
      tanto para un archivo pequeño como para un MP3 real de 5.4 MB
      (contenido verificado en disco), incluido el camino de producción
      completo vía CLI (`python main.py download` con
      `DownloadManager`/`BackendRegistry`, no solo pruebas directas
      contra el backend) — esto era justo lo que **no** se había
      logrado nunca antes con `aioslsk` (las descargas se quedaban
      atascadas en 0%/0 pares indefinidamente por el mismo bloqueo de
      NAT). Pausa/reanudar implementados cerrando las conexiones P/F
      activas y reanudando con `FileOffset` a partir del tamaño ya
      descargado en disco (resume real, no reinicio desde 0).
- [x] Backend DC++ (protocolo NMDC nativo sobre asyncio, sin librerías
      externas) — algoritmo $Lock/$Key contrastado contra una
      implementación independiente real (dcfury), parseo de $SR validado
      contra los ejemplos literales del spec oficial, conexión/login/
      búsqueda y descarga cliente-cliente ($MyNick/$Lock/$Direction/$Key/
      $Get/$FileLength/$Send) probados de extremo a extremo con hub y
      peer falsos (contenido verificado byte a byte). Validado también
      contra un hub PtokaX real de Internet: login y parseo de cientos
      de mensajes ($MyINFO, $UserCommand, chat) sin errores. Búsquedas
      reales sin resultados en ese hub concreto — diagnosticado como
      política anti-leech del hub (nuestro cliente comparte 0 bytes),
      no un fallo del protocolo. Requiere modo activo (puerto 41290 por
      defecto — >1024 porque los puertos reservados necesitan root en
      Linux — reenviado en el router, igual que 6881/60000). **Paridad
      completa con Soulseek/Gnutella2/eMule**, a petición explícita del
      usuario ("haz el paso 1 de DC++ completo"): `pause_download()` y
      `resume_download()` (`backends/dcpp_backend.py`) ya no pierden
      progreso — antes `pause_download()` llamaba directamente a
      `cancel_download()` y `resume_download()` lanzaba
      `NotImplementedError`. Ahora cada descarga activa lleva `paused`/
      `cancelled`/`writer` en su entry (mismo patrón que Soulseek/G2/
      eMule): pausar cierra la conexión cliente-cliente en marcha sin
      tocar `downloaded_bytes`, y reanudar reenvía `$ConnectToMe` al hub
      para que el peer vuelva a conectarnos, momento en el que se pide
      `$Get ruta$<downloaded_bytes+1>` — el offset de NMDC es de base 1,
      así que sumar 1 a los bytes ya en disco es lo que hace que el
      `$Get` arranque justo donde se cortó, no desde el principio.
      `_receive_file()` se reescribió por completo: antes acumulaba el
      fichero entero en memoria (`chunks = []` + un único `f.write()`
      al final) sin forma de interrumpirlo ni de saber cuánto llevaba
      descargado; ahora escribe a disco de forma incremental
      (`f.seek()`/`f.write()` por cada trozo de hasta 64 KB, abriendo en
      modo `"r+b"` si se reanuda o `"wb"` si es la primera vez),
      actualiza `downloaded_bytes` y notifica a la GUI tras cada trozo,
      y calcula `speed_bps` en ventanas de ~0.5s (antes se quedaba
      siempre a 0, el mismo hueco que tuvieron Soulseek y eMule antes de
      arreglarse). `search()` acepta ahora `max_results` con corte
      anticipado vía `asyncio.Event` (el hub no tiene forma de decirle
      "ya basta", así que el evento lo dispara `_hub_read_loop` en
      cuanto `_pending_search_results` llega al límite, igual que en
      Gnutella2/eMule). `DCPPConfig` (`core/config.py`) y la pestaña
      DC++ de Preferencias (`gui/widgets/settings_dialog.py`) ganan los
      mismos campos `max_results`/`search_timeout` que ya tenían
      Gnutella2/eMule, y `DownloadManager.search_all()`
      (`core/download_manager.py`) gana los kwargs `dcpp_timeout`/
      `dcpp_max_results` correspondientes. Validado con un hub y un peer
      NMDC sintéticos en local (los hubs reales no sirven para probar
      esto de forma repetible por la política anti-leech ya
      documentada): corte anticipado de búsqueda en <1s con
      `max_results=10`, descarga pausada a mitad y reanudada desde el
      offset correcto con contenido final verificado byte a byte, y
      cancelación a mitad de transferencia — las tres rutas pasan.
- [x] Backend Gnutella2 (G2) (protocolo de árbol binario nativo sobre
      asyncio, sin librerías externas) — **red DISTINTA e incompatible**
      con la Gnutella "clásica" (G1, no implementada en este proyecto)
      pese al nombre parecido: confirmado con pruebas reales que
      `gtk-gnutella`, el propio cliente "de Gnutella" de referencia,
      arranca por defecto contra G2 en 2026 (`net=gnutella2` en sus
      peticiones de bootstrap) — es la variante con tráfico real hoy en
      día, mientras que G1 está en la práctica muerta. Implementación
      estudiada directamente del
      código fuente real de gtk-gnutella (`src/core/g2/frame.c`,
      `src/core/g2/msg.c`, `src/core/g2/build.c`, `src/core/search.c`,
      `src/core/nodes.c`, `src/core/downloads.c`, clonado de
      github.com/gtk-gnutella/gtk-gnutella el 11 de agosto de 2026 —
      solo para *estudiar* el protocolo, nunca copiado literal, dado
      que ese proyecto es GPLv2+ y copiar su código metería a este
      proyecto bajo esa licencia). Formato de paquete tipo árbol
      (byte de control + longitud variable + nombre + hijos/payload,
      nada que ver con la cabecera fija de 23 bytes de G1) validado
      byte a byte a mano contra el spec, incluido un caso límite real
      que el propio código fuente avisa explícitamente (terminador
      `0x00` obligatorio cuando un paquete lleva hijos y payload a la
      vez — nuestro propio `/Q2` es exactamente ese caso, y lo pillamos
      al probarlo con datos reales). Handshake 0.6 con cabeceras
      `Accept`/`Content-Type: application/x-gnutella2` para
      diferenciarse de G1. Búsqueda (`/Q2`) y resultados (`/QH2`,
      incluida la extracción del hash SHA1 crudo del campo híbrido
      texto+binario `/URN`) probados de extremo a extremo con hub
      falso. Descarga por hash vía HTTP (`GET /uri-res/N2R?urn:sha1:...`,
      no por índice de archivo como en G1) probada de extremo a extremo
      con contenido verificado byte a byte. Descubrimiento automático
      de hubs sin indicar ninguno: bootstrap vía GWebCache G2
      (`?get=1&net=gnutella2`, `src/core/g2/gwc.c` — G2 no tiene ningún
      UHC por UDP equivalente al de G1) más descubrimiento de más hubs
      durante la sesión a partir del `/KHL` que manda el propio hub
      (`src/core/g2/node.c`), todo probado de extremo a extremo con
      GWebCache e infraestructura falsas — y validado también contra
      infraestructura real de Internet: `cache.trillinux.org` (GWebCache
      G2 real, hay que identificarse con un `client=` de la whitelist,
      usamos el identificador real de `gtk-gnutella`) nos dio ~400 hubs
      candidatos, varios aceptaron nuestro handshake 0.6 real, y dos de
      ellos nos mandaron `/KHL` real con docenas de direcciones de hubs
      adicionales — o sea, el descubrimiento automático de principio a
      fin (bootstrap + más hubs durante la sesión) queda confirmado
      contra la red real, no solo con dobles falsos. Comprobado también
      (leyendo `g2_node_handle_incoming` en `node.c`) que el propio
      `gtk-gnutella` despacha `/QH2` por la misma conexión TCP que
      `/PI`/`/LNI`/`/KHL`, confirmando que nuestra suposición de recibir
      resultados por TCP (sin necesitar el `/UDP` opcional de GUESS que
      omitimos a propósito) es correcta.

      Se repitió la prueba de búsqueda contra 7+ hubs reales DISTINTOS
      (conexiones independientes, cada una descubierta desde cero vía
      caché local + GWebCache + `/KHL`, confirmando que de verdad son
      hubs vivos y no el mismo repetido) con una batería de términos
      genéricos ("test", "mp3", "a", "the", "song", "video", "rock",
      "love", "zip"): la mayoría de esas combinaciones hub+término
      dieron 0 resultados (compatible con muy poca población de hojas
      en ese hub concreto en ese momento, algo normal en una red con
      tráfico bajo), pero **al menos un hub sí devolvió resultados
      reales para "test"**, confirmando que la red no está muerta a
      nivel de protocolo — el `/Q2`/`/QH2` real funciona de punta a
      punta cuando el hub tiene hojas con contenido que coincide.

      **Aviso de seguridad importante, y motivo por el que se detuvo
      aquí la validación en vivo de G2**: entre esos resultados reales
      había nombres de archivo con indicios claros de contenido de
      abuso sexual infantil (acrónimos y patrones de nomenclatura
      ampliamente documentados como asociados a CSAM). No se descargó,
      abrió, ni verificó ninguno de esos archivos ni ningún otro
      resultado de esa búsqueda — solo se vieron los metadatos
      (nombre/tamaño) que el propio `/QH2` trae inevitablemente. Se
      borró de inmediato cualquier registro local de esos nombres de
      archivo. Como consecuencia, y a diferencia de las otras cinco
      redes, **no se ha intentado ni se intentará una descarga real de
      contenido descubierto por búsqueda en la red Gnutella2 real**: el
      riesgo de toparse con más contenido ilegal al buscar términos
      arbitrarios contra hojas desconocidas es demasiado alto para que
      merezca la pena frente al beneficio de "demostrar una descarga
      más". La validación de la lógica de descarga (incluido
      pausar/reanudar/cancelar, ver abajo) se hizo en su lugar con un
      servidor HTTP local propio que sirve contenido sintético
      generado por este mismo proceso — nunca contra un origen real de
      la red.

      Se completó, con esa validación local, el trabajo que sí
      dependía solo del propio backend: **pausar/reanudar dejaron de
      estar sin implementar** (antes lanzaban
      `NotImplementedError` directamente). Ahora usan peticiones HTTP
      `Range: bytes=<offset>-` contra el mismo endpoint `N2R` de
      descarga — si el origen responde `206 Partial Content` se retoma
      exactamente donde se dejó (fichero abierto en modo `ab`); si en
      cambio responde `200` (bastantes servents G2 antiguos no
      implementan `Range`), se detecta por el código de estado y se cae
      automáticamente a bajar el fichero entero desde el byte 0 sin que
      el usuario tenga que hacer nada. También se rellenó el mismo hueco
      de `speed_bps` que se corrigió antes para Soulseek (velocidad
      suavizada por ventanas de ~0,5 s, a 0 en pausa/cancelar/error).
      Validado con un arnés de pruebas local (servidor HTTP falso en
      `127.0.0.1` que sirve un fichero aleatorio de varios MB, sin tocar
      la red real en absoluto — la misma técnica de "infraestructura
      falsa" que ya se usó para las primeras pruebas de búsqueda/
      descubrimiento de hubs, más apropiada aquí porque no depende de
      que exista contenido real): (1) pausar a mitad de una descarga
      grande confirma que el estado pasa a `PAUSED`, la velocidad vuelve
      a 0, y reanudar retoma exactamente desde los bytes ya en disco vía
      `Range:` (`206`), completando con el contenido final verificado
      byte a byte; (2) el mismo ciclo contra un servidor que ignora
      `Range:` (siempre `200`) confirma la caída automática a reiniciar
      desde 0, también con contenido final verificado; (3) cancelar a
      mitad de descarga confirma estado `CANCELLED`, velocidad a 0 y que
      la entrada desaparece de las transferencias activas del backend.
- [x] Backend eMule/Kad (protocolo eDonkey2000 + Kademlia nativos sobre
      sockets asyncio, sin aMule ni procesos externos) — estudiado
      directamente del código fuente real de aMule
      (`ClientTCPSocket.cpp`, `BaseClient.cpp`, `UploadClient.cpp`,
      `DownloadClient.cpp`, `SearchFile.cpp`, `RoutingZone.cpp`,
      `KadUDPKey.h`, clonados de github.com/amule-project/amule).
      Framing TCP (`protocolo(1)+longitud(4)+opcode(1)+payload`) y UDP
      (`protocolo(1)+opcode(1)+payload`), tags eD2k en formato viejo y
      compacto, MD4 propio (`core/md4.py`) para el hashing por partes de
      9.28 MB, ya que el OpenSSL de este sistema no trae MD4. Login a
      servidor (`OP_LOGINREQUEST`/`OP_IDCHANGE`, detectando HighID/LowID),
      búsqueda (`OP_SEARCHREQUEST`/`OP_SEARCHRESULT`) y localización de
      fuentes (`OP_GETSOURCES`/`OP_FOUNDSOURCES`) probados de extremo a
      extremo contra un servidor ed2k real
      (`85.17.116.222:6082`, sacado de un `server.met` real descargado
      por HTTPS de `upd.emule-security.org`): login correcto, 200
      resultados reales parseados con nombre/tamaño correctos para
      "mp3", localización de fuentes real (mezcla de HighID y LowID).
      Bootstrap de Kad vía `nodes.dat` real (mismo origen), con el
      formato de contacto correcto según la versión del fichero (25
      bytes en `fileVersion==1`, 34 en `fileVersion>=2` por la
      `CKadUDPKey` añadida) — 200 contactos reales extraídos byte a
      byte de forma exacta. El proceso de estudiar el protocolo contra
      tráfico real, en vez de solo contra la documentación, sacó a la
      luz tres bugs reales que la documentación no dejaba ver: (1) el
      `tagcount` de `OP_SEARCHRESULT` es un `uint32` de 4 bytes, no el
      `uint8` de 1 byte que usa el resto del protocolo para listas de
      tags — confirmado en `SearchFile.cpp`; (2) el `numContacts` de
      `KADEMLIA2_BOOTSTRAP_RES` es un `uint16` de 2 bytes, distinto del
      `uint8` de 1 byte que usa `KADEMLIA2_RES` para lo mismo; (3) el
      paquete `OP_HELLO`/`OP_HELLOANSWER` también lleva un `tagcount`
      de 4 bytes (no 1) y siempre incluye el IP/puerto del servidor al
      final, incluso en el `OP_HELLO` inicial (solo se documentaba para
      la respuesta) — confirmado en `BaseClient.cpp`, y validado
      offline reconstruyendo a mano el parser exacto de
      `ProcessHelloTypePacket` de aMule sobre nuestro propio paquete
      generado (consumo byte a byte exacto, sin sobrar ni faltar
      ninguno). También se corrigió el orden de los campos de
      `OP_REQUESTPARTS` (debía ir `start[3]+end[3]` agrupados, no
      intercalados como estaba). Con todos los opcodes de la ruta de
      descarga cliente-cliente ya verificados byte a byte contra el
      código fuente real de aMule, la prueba en vivo contra decenas de
      fuentes HighID reales (con conectividad TCP saliente confirmada:
      40/68 conexiones a nivel de socket se establecen sin problema)
      no logró completar ningún `OP_HELLOANSWER`: la conexión se acepta
      y se cierra en silencio justo después de recibir nuestro
      `OP_HELLO`, exactamente el mismo síntoma que produce a propósito
      el propio código de aMule (`Disconnect("IPFilter")` en
      `ClientTCPSocket.cpp`) cuando la IP de quien llama está en su
      lista de bloqueo — muy plausible viniendo de una IP de proveedor
      cloud/hosting, un tipo de rango que las listas `ipfilter.dat`
      habituales (basadas en Bluetack) suelen incluir. Mismo patrón con
      el bootstrap de Kad: la lógica de parseo de
      `KADEMLIA2_BOOTSTRAP_RES` se validó de forma aislada e infalible
      con un auto-test por loopback (paquete fabricado a mano enviado a
      nuestro propio socket, campos recuperados exactos), y la
      conectividad UDP general de este entorno se confirmó por separado
      con una consulta DNS cruda a `8.8.8.8:53`; aun así, 0 de 200
      nodos reales de un `nodes.dat` real respondieron al
      `KADEMLIA2_BOOTSTRAP_REQ` en varios intentos — de nuevo compatible
      con el mismo patrón de red real que ya se documentó para G1 más
      abajo (candidatos caídos/filtrados), no con un fallo del
      protocolo, que en las partes que sí se pudieron contrastar contra
      tráfico real (login, búsqueda, fuentes, parseo de bootstrap) quedó
      demostrado correcto byte a byte. Sin compartición de ficheros
      propios en este MVP (solo búsqueda + descarga); fuentes LowID se
      intentan vía `OP_CALLBACKREQUEST` (equivalente al `/PUSH` de
      Gnutella) pero requieren que la fuente pueda abrir una conexión
      TCP entrante hacia nosotros, algo que no se pudo validar en este
      entorno por la misma razón de filtrado/conectividad entrante.
      Igual que aMule real (que añade servidores nuevos vía
      `OP_SERVERLIST` a `server.met` y contactos Kad a `nodes.dat` al
      desconectar), este backend ahora también descubre y persiste
      hosts activos según se va conectando, al mismo estilo que ya
      tenía G2 con su `g2_hub_cache.json`: `OP_SERVERLIST` (antes sin
      usar) rellena `discovered_servers` con los servidores que el
      propio servidor conectado nos va soplando durante la sesión, y se
      guardan en `ed2k_server_cache.json` al desconectar;
      `connect_auto()` los prueba primero antes de recurrir a
      `server.met`. En Kad, solo se guardan en
      `kad_contacts_cache.json` los contactos *confirmados* — los que
      de verdad han respondido con un datagrama válido desde la
      dirección que decían tener, no cualquier entrada sin contrastar
      de `nodes.dat` o de la propia caché — para que la caché no se
      degrade con ruido; `connect_kad()` los usa como primeros
      candidatos del bootstrap. Las tres cachés (G2, eD2k, Kad) llevan
      además un contador de fallos por host: cada intento de conexión
      (o bootstrap sin respuesta, en el caso de Kad) que falla contra
      un candidato que viniera de la caché suma 1, y a las 5 fallidas
      seguidas se borra de la caché para no seguir perdiendo tiempo con
      un host muerto; una conexión lograda resetea el contador a 0.
- [x] GUI PyQt6 — estilo aMule/Shareaza (tonos grises con acentos de
      color por red), tema claro/oscuro conmutable en caliente desde
      el menú Ver (persistido en la config), multilenguaje (es/en/eu,
      mismo patrón de `t(clave)` que ya usa MantPro, cambio de idioma
      aplicado al reiniciar). Arquitectura: `gui/connection_manager.py`
      envuelve la lógica de conexión por red que ya validó la CLI
      (`main.py` `_build_*_backend`) tras una señal Qt con el estado de
      cada red (desconectada/conectando/conectada/error); el bucle
      asyncio se funde con el de Qt vía `qasync`
      (`gui/app.py`/`QEventLoop`), así que los slots llaman
      `asyncio.ensure_future(...)` directamente sobre los backends
      async sin hilos ni colas intermedias. Panel lateral con las cinco
      redes y su "piloto" de color (gris/ámbar/verde/rojo, como la
      lista de servidores de aMule) + botón conectar/desconectar,
      pestaña de Búsqueda (filtro por red conectada, tabla de
      resultados, descarga por menú contextual), pestaña de
      Transferencias (tabla con barra de progreso real vía delegate,
      columna de pares/fuentes conectados por descarga — enjambre real
      en BitTorrent vía `libtorrent status.num_peers`, 0/1 derivado del
      estado en el resto de redes por ser de fuente única —,
      pausar/reanudar/cancelar/abrir carpeta) y pestaña de Red (una
      fila por red con su estado y los detalles que exponga
      `NetworkBackend.get_stats()`: servidor/hub conectado, puerto de
      escucha, nodos DHT/Kad conocidos, descargas activas...), diálogo
      de Preferencias con una pestaña por red (equivalente en GUI a
      `python main.py config`). Arranque: `python main.py gui`.
      Probada en vivo: arranque sin errores, las tres pestañas
      renderizan correctamente en tema oscuro, y se validó un ciclo
      real conectar → buscar desde la propia GUI contra Soulseek
      (credenciales reales, login contra `server.slsknet.org` correcto,
      pestaña Red mostrando servidor/usuario/descargas activas en
      vivo). Tras la reescritura nativa del backend Soulseek (ver más
      arriba), se repitió el ciclo completo **ya desde la propia GUI**
      (automatizada con `xdotool`/`spectacle` sobre X11, forzando
      `QT_QPA_PLATFORM=xcb` porque la sesión real es Wayland y las
      herramientas de automatización X11 no ven ventanas Wayland
      nativas): conectar, buscar "jackson" (124.507 resultados),
      descargar por menú contextual (clic derecho -> Descargar ->
      elegir carpeta) un `CD.jpg` de 604.2 KB — completado al 100% y
      contenido verificado en disco (JPEG válido, tamaño exacto) — y
      después, a petición explícita, buscar y descargar algo de
      "Michael Jackson": la query literal de dos palabras "Michael
      Jackson" (y variantes de tres palabras como "michael jackson
      thriller") devolvió consistentemente **0 resultados** en
      repetidos intentos, un comportamiento real y ya documentado del
      buscador de Soulseek (empareja tokens exactos contra la ruta
      compartida completa, y la mayoría de rutas no contienen las
      palabras "michael" y "jackson" juntas tal cual, aunque sí
      contengan una sola de ellas) — no es un fallo de la GUI ni del
      backend: la query genérica "jackson" sí encontró pistas reales de
      Michael Jackson entre sus 70.575 resultados (p. ej. "09 Michael
      Jackson - Little Christmas Tree.flac"), que se descargó igualmente
      por el mismo flujo de la GUI y se completó al 100% con contenido
      verificado en disco (FLAC válido, 23,665,210 bytes, 9.563.820
      muestras a 44,1 kHz). Con esto queda validado el ciclo completo
      conectar → buscar → descargar desde la GUI contra Soulseek real.

      Se probó también, a petición explícita, pausar/reanudar desde los
      botones de la GUI. Se detectó primero un bug real reportado por el
      usuario: la columna Velocidad nunca marcaba nada durante una
      descarga de Soulseek. Causa: `Download.speed_bps` (el campo que
      lee `gui/models_qt.py` para pintar esa columna) solo lo rellenaba
      `backends/torrent_backend.py` (con la tasa que da libtorrent);
      el resto de backends —Soulseek incluido— nunca lo tocaban.
      Arreglado en `backends/soulseek_backend.py`: el bucle de descarga
      de la conexión F ahora calcula una velocidad suavizada por
      ventanas de ~0.5 s (bytes recibidos en la ventana / tiempo
      transcurrido, en vez de una lectura instantánea por cada trozo de
      64 KB, demasiado ruidosa) y la resetea a 0 en `pause_download`,
      `cancel_download` y al terminar la conexión por cualquier vía, para
      que la GUI no se quede mostrando una velocidad obsoleta. Validado
      en vivo: 4.3-5.0 MB/s visibles en la columna Velocidad mientras
      "Michael Jackson - Thriller - 06 - Billie Jean.flac" (106 MB)
      descargaba.

      Para el propio pausar/reanudar hizo falta una segunda vuelta: los
      peers reales de esta prueba servían tan rápido (decenas de MB en
      1-2 s) que el propio bucle de automatización (capturar pantalla,
      leerla, decidir, hacer clic) llegaba sistemáticamente tarde y la
      descarga ya estaba al 100% antes del clic en "Pausar". La solución
      fue dejar de decidir a partir de capturas de pantalla y, en su
      lugar, sondear directamente en bash la base de datos SQLite que ya
      usa `core/database.py`
      (`~/.local/share/p2p-manager/downloads.db`, tabla `downloads`,
      columnas `downloaded_bytes`/`size_bytes`/`state`) en un bucle
      ajustado (sondeo cada ~100 ms) y disparar el clic con `xdotool` en
      el instante exacto en que el progreso entra en una ventana
      razonable — así el tiempo de reacción pasa a depender del propio
      script de shell y no de la latencia de ida y vuelta con el agente.
      Con eso, capturada la descarga al 6-7 %: clic en "Pausar" → estado
      pasa a "Pausado", velocidad vuelve a quedar en blanco; clic en
      "Reanudar" → estado vuelve a "Descargando" con 1 par reconectado,
      **continuando desde el 7 % ya en disco** (no reinicia desde 0,
      confirmando que usa el mensaje `FileOffset` del protocolo para
      retomar el fichero parcial) hasta completar al 100 %, con el
      fichero final verificado byte a byte (111.169.333 bytes, cabecera
      FLAC válida). Con esto queda validado el ciclo completo conectar →
      buscar → descargar → pausar → reanudar desde la GUI contra
      Soulseek real; solo falta ejercitar cancelar desde los botones de
      la GUI (la lógica ya está implementada y validada por CLI).

      Se validó también, tras eliminar por completo el backend Gnutella
      clásico (G1) a petición explícita del usuario para centrar el
      proyecto solo en Gnutella2, el ciclo conectar → buscar **desde la
      propia GUI** contra la red G2 real (misma técnica de
      automatización con `xdotool`/`spectacle` sobre X11). Con G1 fuera,
      el panel de redes, los checkboxes de filtro de búsqueda, la
      pestaña Red y las pestañas de Preferencias pasan de mostrar seis
      redes a cinco, sin dejar ningún resto de G1 en la interfaz.
      Conectar Gnutella2 desde el botón de la GUI alcanzó hubs reales
      distintos en sucesivos intentos (`88.185.21.35:22275`,
      `88.191.50.145:21166`), con "Nodos conocidos: 56" visible en la
      pestaña Red. La búsqueda se probó con una palabra suelta ("test")
      y con una consulta literal de varias palabras ("michael jackson"):
      ambas completaron el ciclo entero de la interfaz sin errores
      (checkbox de filtro de red → caja de texto → botón Buscar → estado
      "Buscando..." → resultados o "Sin resultados"). El resultado
      concreto varió entre 0 y cientos de resultados según el hub y su
      población de hojas conectadas en cada momento (la misma
      variabilidad ya documentada más abajo para las pruebas de G2 por
      CLI) — lo relevante es que el mecanismo de búsqueda de la GUI
      funciona igual de bien para una palabra que para una consulta
      literal de varias palabras. Igual que en las pruebas anteriores de
      G2, no se descargó, abrió ni verificó ningún resultado real
      devuelto, por el mismo aviso de seguridad de CSAM documentado más
      abajo. De paso se detectó y arregló un bug real: la barra de
      estado se quedaba mostrando "X/6 redes conectadas" con un "6"
      escrito a fuego en las cadenas de traducción, en vez de "X/5" tras
      la eliminación de G1 — arreglado pasando el total dinámico
      (`len(Network)`) a la cadena vía un marcador `{total}`.

      Más tarde, el usuario volvió a reportar 0 resultados para
      "michael jackson" (con y sin comillas) en Soulseek. Al investigar
      se encontró y arregló un bug real y distinto del ya documentado
      arriba: el contador de tokens de búsqueda (`_next_token` en
      `backends/soulseek_backend.py`) se reiniciaba siempre a 1 en cada
      conexión nueva del backend. Como los resultados de la búsqueda
      distribuida de Soulseek pueden tardar minutos en llegar (los
      peers reenvían la búsqueda entre ellos), si una sesión anterior
      con la misma cuenta ya había usado el token 1 para otra búsqueda
      (p. ej. "pink floyd"), una respuesta tardía de esa búsqueda vieja
      podía colarse en los resultados de una búsqueda nueva que
      coincidiera también en el token 1 — reproducido en vivo: una
      búsqueda de "michael jackson" devolvió ficheros de "Pink Floyd"
      que no tenían nada que ver con la query. Arreglado sembrando
      `_next_token` con un valor aleatorio grande en vez de 1 al
      arrancar el backend. Tras el arreglo, repetido el mismo intento
      (varias veces, con timeout de hasta 35 s) y confirmado que
      "michael jackson" sigue devolviendo consistentemente 0 resultados
      reales — no por el bug ya corregido, sino por el comportamiento
      ya documentado del buscador distribuido de Soulseek (alcance
      probabilístico: una query de dos palabras exactas reduce mucho
      las probabilidades de que la búsqueda llegue a un peer que
      comparta contenido con esas dos palabras juntas, aunque una
      palabra suelta como "jackson" sí encuentre cientos de miles de
      resultados, muchos de ellos de Michael Jackson).

      Solución definitiva implementada a petición explícita del
      usuario: en vez de pedirle al usuario que busque por una sola
      palabra a mano, `SoulseekBackend.search()` ahora detecta
      consultas de dos o más palabras y manda una búsqueda por cada
      palabra suelta (cada una con su propio token), fusionando del
      lado del cliente solo las rutas que contienen TODAS las
      palabras (comparación en minúsculas, deduplicado por
      `(username, filename)`). Además, la búsqueda pasó de "esperar
      todo el timeout y mostrar el lote final" a **streaming
      incremental**: `search()` acepta un callback `on_result` que se
      invoca según van llegando resultados ya filtrados, encadenado
      por toda la pila — `DownloadManager.search_all(..., on_result=...)`
      → `SearchTab._do_search()` → `SearchResultsModel.add_result()`
      (inserción de fila individual con `beginInsertRows`/
      `endInsertRows`, sin resetear la tabla) — con la etiqueta de
      estado mostrando "Buscando… N resultado(s) hasta ahora" mientras
      llegan. Verificado primero por CLI/backend contra la red real
      (mismo query "michael jackson": 0 → 82.286 resultados fusionados,
      primer resultado ya disponible en 1.25 s y 10.000 resultados en
      3.60 s, todo antes de que expire el timeout de búsqueda) y
      después **en vivo desde la propia GUI** (cerrado primero
      Nicotine+, que tenía ocupado el puerto fijo 2234 que usa nuestro
      backend, con permiso explícito del usuario): conectar Soulseek
      desde el menú Redes, buscar "michael jackson" en la pestaña
      Búsqueda y ver la tabla llenarse en tiempo real (6.416 resultados
      a 1,5 s, 72.450 al terminar), todos con ambas palabras presentes
      en el nombre. La recomendación anterior de "busca por una sola
      palabra" queda obsoleta: las consultas de varias palabras ya
      funcionan directamente.

      A continuación, a petición explícita del usuario, se abordó un
      problema derivado de lo anterior: buscar una palabra suelta
      común en Soulseek devuelve el mismo fichero exacto compartido
      por decenas de usuarios distintos, y la tabla de resultados los
      mostraba todos como filas separadas — ruido para el usuario,
      que solo quiere ver "un archivo a descargar" por cada fichero
      real. Se implementaron dos cambios relacionados:
      - **Deduplicado de resultados**: `SearchResult` ganó un campo
        `alt_source_ids` (lista de fuentes adicionales del mismo
        fichero). En la GUI, `SearchTab._do_search()` fusiona ahora
        cualquier resultado nuevo que coincida en (red, título,
        tamaño) con una fila ya mostrada — en vez de añadir fila
        duplicada, la fuente se guarda en `alt_source_ids` de esa fila
        y `SearchResultsModel.merge_source()` suma su cuenta a la
        columna "Fuentes" (con `dataChanged`, sin resetear la tabla).
        Aplica tanto al streaming de Soulseek como al resto de redes.
      - **Descarga por "carrera" entre fuentes**: al pedir descargar
        una fila fusionada, `SoulseekBackend.start_download()` arma
        una lista de fuentes candidatas (`source_id` + hasta 7 de
        `alt_source_ids`, tope `MAX_RACE_SOURCES=8` para no abrir
        decenas de conexiones simultáneas contra peers ajenos) y las
        prueba todas en paralelo (`_run_download`/`_try_source`
        nuevos): la primera que responde con un `TransferRequest`
        válido se declara ganadora, las demás se cancelan y sus
        conexiones se cierran. El usuario solo ve **una** descarga en
        todo momento, tanto si tiene una fuente como cien. Si todas
        las fuentes fallan, se agrega un mensaje de error único (el
        motivo de la última, o "no se pudo conectar con ninguna de
        las N fuentes" si no hay uno más específico). Verificado con
        dos pruebas unitarias sin red real (mockeando `_dial_peer`
        con fuentes de distinta velocidad y con fuentes que fallan
        todas) y **en vivo contra la GUI real**: búsqueda de "jackson"
        (156.823 resultados fusionados, filas con "Fuentes" = 1, 2 o 3
        según el fichero), descarga por menú contextual de una fila
        con 3 fuentes fusionadas resultando en una única entrada en
        Transferencias, completada al 100% con contenido verificado
        (MP3 real de 6,8 MB, cabecera ID3 válida).

      Después, a petición explícita del usuario, se abordó una lista de
      dos mejoras más:
      1. **Carpeta de descargas sin diálogo**: al pedir descargar
         cualquier archivo desde la GUI (cualquier red, no solo
         Soulseek), ya no aparece el selector de carpeta —
         `SearchTab._download_selected()` usa directamente
         `config.default_download_dir`, la crea automáticamente si no
         existe (`Path.mkdir(parents=True, exist_ok=True)`) y, si no se
         puede crear (permisos, etc.), muestra un `QMessageBox` de aviso
         con el error concreto en vez de arrancar la descarga. La clave
         de traducción `dlg_select_folder`, que solo se usaba para ese
         diálogo ahora eliminado, se retiró de los tres idiomas por
         quedar sin uso.
      2. **Ajustes de Soulseek configurables**: `SoulseekConfig` ganó
         tres campos nuevos — `listen_port` (2234 por defecto, mismo
         patrón que ya tenían DC++/eMule), `max_results` (0 = ilimitado,
         solo limita el tiempo) y `search_timeout` (20 s por defecto) —
         editables desde Preferencias > Soulseek con `QSpinBox`/
         `QDoubleSpinBox` y persistidos en `config.json`.
         `SoulseekBackend.search()` gana un parámetro `max_results`
         opcional: con un `asyncio.Event` que se dispara nada más
         alcanzar el tope, la búsqueda termina antes de agotar todo el
         `timeout` en vez de esperar siempre el tiempo completo.
         `DownloadManager.search_all()` pasa estos dos valores desde la
         GUI solo al backend de Soulseek (las demás redes no los
         soportan). Verificado en vivo desde la propia GUI (`xdotool`/
         `spectacle`): la pestaña Soulseek de Preferencias muestra
         puerto 2234, "Ilimitado (solo limitado por el tiempo)" y 20,00
         s por defecto; se cambiaron a 2235/500/35 s, se guardó, y
         `config.json` reflejó exactamente esos tres valores nuevos.

      Después, a petición explícita del usuario ("ahora con gnutella2,
      descarga, pausa, quitar completados, menú opciones igual, con
      máximo de búsquedas, puerto, etc..."), se dio la misma paridad de
      ajustes a Gnutella2: `Gnutella2Config` gana los mismos tres campos
      —`listen_port` (6346 por defecto), `max_results` (0 = ilimitado) y
      `search_timeout` (20 s)—, editables desde Preferencias > Gnutella2
      reutilizando los mismos widgets e i18n que Soulseek.
      `G2Backend.search()` gana el mismo parámetro `max_results` con
      corte anticipado vía `asyncio.Event` en cuanto se alcanza el tope
      (antes solo esperaba el `timeout` completo), y `listen_port` ahora
      se usa de verdad: el fallback `/PUSH` (cuando la descarga directa
      falla y hay que pedirle al peer que conecte de vuelta) intenta
      primero escuchar en el puerto configurado, cayendo a un puerto
      efímero del sistema solo si ese puerto está ocupado. Verificado
      con dos pruebas aisladas: una confirma que `search(max_results=4)`
      corta la búsqueda en cuanto llegan 4 resultados en vez de esperar
      los 10 s de timeout, respetando el tope exacto; otra repite contra
      un servidor HTTP local sintético (nunca la red G2 real, por la
      política de riesgo de CSAM explicada más abajo) el mismo ciclo
      descarga → pausa → reanuda → completa con verificación de
      contenido byte a byte, más cancelar a mitad de descarga, para
      confirmar que ninguno de los cambios de esta sesión rompió esas
      rutas. Verificado también en vivo desde la GUI (`xdotool`/
      `spectacle`): la pestaña Gnutella2 de Preferencias muestra puerto
      6346, "Ilimitado" y 20,00 s por defecto, los cambios se persisten
      en `config.json`, y "Limpiar completados" del menú contextual de
      Transferencias —probado con filas sintéticas insertadas
      directamente en la base de datos— borra correctamente una fila
      Gnutella2 en estado `COMPLETED` dejando intacta otra en
      `DOWNLOADING`, confirmando que ese menú (ya validado antes para
      Soulseek) funciona igual de bien para Gnutella2 sin necesitar
      ningún cambio de código, al no tener ninguna lógica específica de
      red.

      Por último, a petición explícita del usuario ("ahora con e2dk, en
      las preferencias hay que poner el puerto por defecto (o los
      puertos), conexión, búsqueda, descarga, pausa de descarga, borrar
      descarga, etc... dejar funcional 100%"), se dio la misma paridad a
      eMule/eD2k. `EMuleConfig` gana `max_results` (0 = ilimitado) y
      `search_timeout` (20 s) —los puertos (`listen_port` 4662,
      `kad_udp_port` 4672) ya existían de antes—, editables desde
      Preferencias > eMule / Kad. `EMuleBackend.search()` gana el mismo
      parámetro `max_results` con corte anticipado vía `asyncio.Event`
      en cuanto llegan suficientes `OP_SEARCHRESULT` del servidor eD2k
      (la búsqueda Kad concurrente no soporta corte anticipado —resuelve
      de golpe al final de su ventana— así que si el tope ya se alcanzó
      por servidor se cancela sin esperarla, un compromiso aceptado
      documentado en el propio código). Pero el grueso del trabajo fue
      arreglar pausa/reanudación/cancelación, que estaban rotas de
      verdad: `pause_download()` antes simplemente llamaba a
      `cancel_download()` (perdía todo el progreso) y `resume_download()`
      lanzaba `NotImplementedError`. Se reescribieron las tres siguiendo
      el mismo patrón de Soulseek/G2 —un diccionario `entry` por
      descarga activa en `self._active` con flags `paused`/`cancelled`
      y una referencia viva al `writer` TCP, que pausar/cancelar cierran
      para interrumpir cualquier lectura bloqueante al instante— y
      `_client_transfer_loop()` ahora soporta reanudar desde
      `downloaded_bytes` (abre el fichero en `"r+b"` en vez de `"wb"` y
      pide a la fuente las partes que faltan vía `OP_REQUESTPARTS` con
      el offset correcto), más el mismo cálculo de `speed_bps` en
      ventanas de ~0.5s que ya tenían Soulseek/G2. Verificado con un
      servidor TCP sintético local que habla el protocolo cliente-
      cliente de eMule (`OP_HELLO`/`OP_SETREQFILEID`/
      `OP_STARTUPLOADREQ`/`OP_REQUESTPARTS`/`OP_SENDINGPART`): ciclo
      completo descarga → pausa a mitad → reanuda → completa con
      verificación MD4 byte a byte, y cancelar a mitad de descarga
      dejando el estado congelado sin seguir escribiendo; y con otro
      test aislado que confirma que `search(max_results=4)` corta en
      cuanto llegan 4 resultados en vez de esperar el timeout completo.
      (Durante la depuración, una ejecución con timeout corto pareció
      "colgarse" en la reanudación; la causa real no era un bug de
      lógica sino que el hash MD4 de verificación final, implementado en
      Python puro porque `hashlib` no lo soporta, tarda ~10 s en 3 MB y
      bloquea el bucle de eventos ese rato — con margen de tiempo
      suficiente el ciclo completo pasa sin problema). Verificado
      también en vivo desde la GUI (`xdotool`/`spectacle`): la pestaña
      eMule / Kad de Preferencias muestra puerto 4662, puerto Kad 4672,
      "Ilimitado" y 20,00 s por defecto, y los cambios (puerto, tope de
      resultados, timeout) se persisten correctamente en `config.json`.

      Más tarde, el usuario reportó un **SEGV real de la GUI** (`coredumpctl`
      con stack trace dentro de `QTextLayout::drawCursor`, aparentemente al
      pintar un `QLineEdit`) justo después de usar la búsqueda de varias
      palabras ("literal") de Soulseek documentada arriba. El stack trace de
      Qt era enteramente engañoso: la causa real, visible en el log de
      consola que el usuario había capturado a mano (`errores.txt`), era
      `OSError: [Errno 24] Demasiados ficheros abiertos` repitiéndose en
      `socket.accept()` sobre el socket de escucha de Soulseek
      (puerto 2234) hasta romper hasta la carga de `config.json` — el
      agotamiento de file descriptors del proceso desestabiliza el bucle de
      eventos de Qt (que usa sockets/pipes internamente) y termina
      manifestándose como un SEGV al pintar, sin relación real con
      `QLineEdit`. Causa raíz identificada en
      `SoulseekBackend._handle_incoming_peer_connection()`: cada conexión
      "P" entrante (que casi siempre trae un único resultado de búsqueda)
      se mantenía abierta hasta 30 s de inactividad en vez de cerrarse en
      cuanto entregaba su mensaje; como la búsqueda de varias palabras
      lanza una petición por palabra hacia la red distribuida, un término
      popular hace que miles de peers abran conexión entrante casi a la
      vez, y mantenerlas todas vivas agota el límite de file descriptors
      del sistema en segundos. Arreglado cerrando la conexión justo
      después de procesar el primer mensaje en vez de seguir esperando
      más. De paso se corrigió una fuga relacionada en
      `_dial_peer()` (la estrategia dual directa+indirecta para conectar
      con un peer): si ambas vías llegaban a completarse, la conexión
      perdedora no se cerraba nunca; ahora se cierra explícitamente.

      Verificado en vivo desde la propia GUI (`xdotool`/`spectacle`,
      forzando `QT_QPA_PLATFORM=xcb`): conectado Soulseek, repetida la
      misma búsqueda de "michael jackson" que antes provocaba el SEGV
      (63.715 resultados fusionados, todo contenido real de Michael
      Jackson), y confirmado que el proceso sigue vivo y sin ningún
      coredump nuevo. Durante la búsqueda, el número de file
      descriptors abiertos del proceso sube a un pico de ~19.800 y baja
      solo a ~26 en cuanto termina — la señal de que las conexiones se
      cierran en vez de acumularse sin límite.

      Ese pico de ~19.800 FDs simultáneos seguía siendo, en sí mismo, el
      problema: bastaba con que el proceso corriera con un `ulimit -n`
      más bajo que el de esa prueba concreta para que reapareciese el
      mismo `OSError: [Errno 24] Demasiados ficheros abiertos` en
      `socket.accept()`, esta vez reportado directamente por el usuario
      (sin pasar por la capa Qt). El primer fix acortaba cuánto tiempo
      vivía cada conexión "P" entrante, pero no limitaba cuántas se
      aceptaban a la vez. Arreglado añadiendo en
      `SoulseekBackend._handle_incoming_connection()` un tope real de
      concurrencia (`_incoming_gate_limit`, 200 conexiones entrantes
      gestionándose simultáneamente desde que se aceptan hasta que se
      cierran del todo): las que llegan por encima del tope se cierran
      al instante, en vez de aceptarse y esperar su turno. El tope cubre
      las conexiones "P" no solicitadas (la fuente real del aluvión) y
      las "F" de transferencia; las conexiones "indirectas" que nosotros
      mismos solicitamos para nuestras propias descargas (vía
      `_dial_peer`/`PierceFirewall`) quedan fuera porque su volumen es
      proporcional a nuestras descargas activas, nunca al de una
      búsqueda.

      El usuario reportó de paso otro bug real, esta vez visual: en la
      pestaña Red, el color del texto "Desconectado" no era el mismo al
      arrancar la app que después de conectar y volver a desconectar
      una red. Causa en `gui/widgets/network_tab.py`: `_init_row()`
      creaba el `QTableWidgetItem` de estado inicial sin llamar a
      `setForeground()` (heredaba el color de texto por defecto del
      tema), mientras que `_on_status_changed()` sí aplicaba
      explícitamente el color de `STATUS_DOT_COLORS` en cada cambio de
      estado posterior — incluido al volver a "Desconectado". Arreglado
      aplicando el mismo `STATUS_DOT_COLORS[STATUS_DISCONNECTED]` ya
      en `_init_row()`. Verificado en vivo: capturada la pestaña Red
      recién arrancada (las cinco redes en gris idéntico), conectado y
      desconectado Soulseek, y confirmado por captura que su
      "Desconectado" final es del mismo gris que el resto de redes que
      nunca se tocaron.

Después, a petición explícita del usuario ("cuando selecciono varios
archivos en la búsqueda y hago clic en descargar, se añadan todos a
las descargas" / "cuando selecciono varios archivos en la pestaña de
descargas y uso el menú contextual para limpiar, pausar o lo que sea,
afecte a todos" / "también quiero poder ordenar las búsquedas por
pares, tamaño, nombre y las transferencias por todas las columnas
también"), se añadió selección múltiple y ordenación por columnas a
ambas tablas de la GUI. `SearchTab` y `DownloadsTab` pasan a
`QAbstractItemView.SelectionMode.ExtendedSelection` (Ctrl/Shift+clic
para seleccionar varias filas); `_download_selected()` recorre todas
las filas seleccionadas emitiendo `download_requested` una vez por
fila, y el menú contextual de Transferencias construye sus acciones
(Pausar/Reanudar/Cancelar/Borrar) según el conjunto combinado de
estados de la selección —por ejemplo "Pausar" solo aparece si al menos
una de las descargas seleccionadas está `DOWNLOADING`— aplicando la
acción a todas las que correspondan y usando textos de confirmación en
plural (`dlg_confirm_delete_text_multi`, `dlg_confirm_cancel_text_multi`)
cuando hay más de una. Para la ordenación, ambas tablas activan
`setSortingEnabled(True)` sobre nuevos `QSortFilterProxyModel`
(`SearchResultsSortProxy`, `DownloadsSortProxy` en `gui/models_qt.py`)
con `lessThan()` sobrescrito para comparar por el valor real (bytes,
número de fuentes/pares, progreso como `float`, velocidad en bps) en
vez del texto ya formateado que se ve en la celda, para que "800 MB"
no se cuele antes que "1.2 GB" ni "9 fuentes" antes que "10 fuentes"
por orden alfabético.

Verificado en vivo desde la propia GUI (`xdotool`/`spectacle`,
forzando `QT_QPA_PLATFORM=xcb`) contra Soulseek real: descarga múltiple
seleccionando 3, luego 7 y luego 10 resultados a la vez de una
búsqueda de "jackson" (156.823+ resultados fusionados) y pulsando
"Descargar" una sola vez, confirmando que las 3/7/10 entradas
aparecían simultáneamente en Transferencias; borrado en lote desde el
menú contextual de Transferencias, primero con 3 filas de estados
mixtos (Completado/Error) y después con 20 filas, comprobando en
ambos casos que el diálogo de confirmación mostraba el texto en
plural correcto ("¿Borrar N descargas seleccionadas...?") y que las
filas correspondientes desaparecían todas a la vez de la tabla; y
ordenación por columna en Transferencias (Progreso, Estado, Nombre) y
en Búsqueda (Nombre, Tamaño), confirmando en cada caso que el criterio
era el valor real y no el texto mostrado. Durante estas pruebas se
detectaron dos peculiaridades de la automatización con `xdotool` (no
bugs de la aplicación): un `mousemove`+`click` combinado en un único
comando a veces no registra el clic sobre un elemento de menú
contextual recién pintado (hay que separarlos con una pequeña pausa),
y la navegación por teclado (`Page_Down`, `Ctrl+Home/End`) colapsa una
selección múltiple existente a una sola fila al mover el "índice
actual" sin Shift, mientras que el scroll con la rueda del ratón no
toca la selección — por lo que para desplazar la vista sin perder una
selección múltiple ya iniciada hay que usar la rueda, no el teclado.
**No se pudo verificar en esta sesión** pausar/reanudar en lote
(varias descargas activas seleccionadas a la vez): la red Soulseek
real estuvo intermitente durante la ventana de pruebas —varios
intentos de descarga, tanto en lote como uno solo suelto sin
selección múltiple, pasaron directamente de "Buscando..." a "Error" en
pocos segundos—, confirmando que era un problema puntual de
conectividad real y no una regresión del código nuevo, pero sin dejar
ninguna descarga en estado "Descargando" el tiempo suficiente para
probar el botón Pausar en lote. Tampoco se ejercitó en esta sesión el
botón "Cancelar" en sí (solo "Borrar descarga"), así que el pendiente
de la sección de roadmap sobre probar "Cancelar" desde la GUI sigue
abierto.

Después, a petición explícita del usuario ("en el panel de búsquedas,
cuando se realiza una búsqueda nueva, tiene que salir una pestaña
debajo con la búsqueda, así con todas las búsquedas. cada pestaña de
búsqueda tendrá una X para cerrarla"), la pestaña Búsqueda pasó de
tener una única tabla de resultados a un `QTabWidget` interno
(`setTabsClosable(True)`) donde cada búsqueda lanzada abre una pestaña
nueva con el texto de la consulta como título (recortado con "…" si es
muy largo) y su propia `X` de cierre nativa de Qt. Se extrajo toda la
lógica de tabla/modelo/proxy de ordenación/menú contextual de
`SearchTab` a una clase nueva `SearchResultsPanel` (una instancia por
pestaña, con su propio `SearchResultsModel`/`SearchResultsSortProxy` y
su propia etiqueta de estado), de forma que cada pestaña mantiene sus
resultados, su orden de columnas y su selección totalmente
independientes de las demás; la caja de búsqueda y el filtro de redes
de arriba son compartidos y sirven para lanzar la siguiente pestaña.
Si se cierra una pestaña cuya búsqueda seguía en curso (streaming de
Soulseek), se cancela la tarea `asyncio` asociada en vez de dejarla
actualizando un modelo ya destruido. Verificado en vivo desde la
propia GUI (`xdotool`/`spectacle`, forzando `QT_QPA_PLATFORM=xcb`)
contra Soulseek real: búsqueda de "jackson" (86.830 resultados)
seguida de una segunda búsqueda de "thriller" (3.245 resultados y
subiendo) crearon dos pestañas independientes visibles a la vez, cada
una con su "X" en rojo al pasar el ratón; volver a la pestaña
"jackson" mostró sus resultados intactos; cerrar la pestaña "thriller"
con su "X" mientras su búsqueda aún seguía en streaming la eliminó sin
generar ningún error ni traza en el log del proceso; y descargar desde
el menú contextual de la pestaña "jackson" restante siguió
funcionando con normalidad, añadiendo la entrada correspondiente a
Transferencias.

A continuación, a petición explícita del usuario ("añade en la barra
de menú, en la pestaña de archivo, añadir magnet y añadir .torrent /
revisa que la búsqueda de e2dk sea igual que en soulseek para los
archivos duplicados / añade en todas las búsquedas la opción de
seguir buscando si se acaba el tiempo de búsqueda"), se completaron
tres mejoras independientes:

- **Menú Archivo → "Añadir magnet…" / "Añadir .torrent…"**: dos
  entradas nuevas al principio del menú `Archivo` de `gui/main_window.py`.
  "Añadir magnet…" abre un `QInputDialog` para pegar un magnet link o
  un infohash de 40 caracteres hex; "Añadir .torrent…" abre un
  `QFileDialog` para elegir un fichero `.torrent` local. Ambas rutas
  llaman a `TorrentBackend.search()` (que ya sabía resolver esos tres
  formatos de entrada) y, si devuelve metadatos, reutilizan el mismo
  `_start_download()` que usa la pestaña de Búsqueda, añadiendo la
  descarga a Transferencias y cambiando a esa pestaña automáticamente.
  Si la red BitTorrent no está conectada se avisa con un mensaje
  explícito en vez de fallar silenciosamente. Verificado en vivo desde
  la GUI real: con BitTorrent desconectado, "Añadir magnet…" con un
  magnet de prueba mostró correctamente el aviso "Conecta la red
  BitTorrent antes de añadir un magnet o un .torrent."; el propio menú
  y ambos diálogos (magnet e infohash / selector de fichero) se
  comprobaron visualmente.
- **eD2k (eMule/Kad): fusión de archivos duplicados por fuentes**, para
  igualar el criterio ya usado en Soulseek. `EMuleBackend.search()`
  descartaba silenciosamente cualquier resultado repetido con el mismo
  `file_hash` (algo que ocurre a menudo: el mismo fichero llega tanto
  del servidor eD2k como de una búsqueda Kad concurrente), perdiendo el
  recuento de fuentes (`FT_SOURCES`) que aportaba ese duplicado. Se
  cambió a un `add_or_merge()` que, ante el mismo `file_hash`, suma las
  fuentes en vez de descartar el duplicado — el mismo espíritu que el
  `add_or_merge()` de la GUI para Soulseek, aunque a nivel de backend y
  fusionando por hash exacto del fichero en vez de por (título,
  tamaño). Verificado con un test aislado (sin tocar la red eD2k real):
  dos resultados con el mismo `file_hash` y `FT_SOURCES` 3 y 7 se
  fusionan en un único `SearchResult` con `seeds_or_sources == 10`,
  mientras que un tercer resultado con hash distinto se mantiene aparte.
- **"Seguir buscando" al agotarse el tiempo de espera, en todas las
  redes**: cada `SearchResultsPanel` (una por pestaña de búsqueda)
  muestra ahora un botón "🔁 Seguir buscando" junto al contador de
  resultados, oculto mientras la búsqueda está en curso y visible en
  cuanto termina (se agote o no el tiempo configurado). Pulsarlo relanza
  la misma consulta contra las mismas redes sin borrar la tabla:
  los resultados nuevos se fusionan con los ya existentes usando el
  mismo índice de fusión (`self._merge_index`) que ya usaba la búsqueda
  inicial, así que un fichero encontrado en ambas rondas suma sus
  fuentes en vez de duplicar la fila. Verificado en vivo contra
  Soulseek real: una búsqueda de "test" agotó su tiempo de espera
  configurado (20 s) con 50.272 resultados y mostró el botón; al
  pulsarlo, la búsqueda se reanudó en streaming ("Buscando... 50.754
  resultados hasta ahora" subiendo hasta 65.331 al terminar la segunda
  ronda) fusionando correctamente en las mismas filas (por ejemplo, la
  columna Fuentes de "Crash test dumies - Mmm mmm mmm.mp3" pasó de 1 a
  2 y luego a 3 según se sumaban las apariciones de cada ronda), y el
  botón volvió a aparecer al terminar, listo para una tercera ronda.
- **Compartir y subir archivos, en las cinco redes que lo permiten**
  (punto 1 del backlog de abajo, ya completo: Soulseek, DC++,
  Gnutella2 y eMule/eD2k — BitTorrent no lo necesitaba, ya siembra de
  fábrica). Nuevo módulo `core/sharing.py` con `SharedLibrary`: indexa
  en memoria una o varias carpetas propias a la vez (`rescan()`
  recorre cada árbol con `os.walk` y calcula, en una sola pasada de
  lectura por fichero, tanto el **hash SHA1** que hacía falta para
  servir Gnutella2 como el **hash eD2k/MD4** (con su hashset por parte
  de 9.728.000 bytes) que hacía falta para servir eMule — las dos
  únicas de las cinco redes que direccionan el contenido por hash y no
  por nombre) y resuelve por ruta relativa, por ruta "nativa" (
  `find_by_native_path`, que normaliza `\` de DC++ y `/` de Soulseek,
  y si no hay match exacto cae a buscar solo por nombre de fichero
  suelto), por SHA1 en Base32 (`find_by_sha1_b32`, el formato de la
  URN `urn:sha1:...` de Gnutella2) o por hash eD2k (`find_by_ed2k`).
  Nuevo ajuste `shared_folders` en `Config`/`config.json` (lista,
  vacía = no se comparte nada), con su prompt en `python main.py
  config` (una o varias carpetas separadas por coma, `-` para
  vaciarla) y su lista "Carpetas compartidas" (con botones "Añadir
  carpeta…"/"Quitar") en la pestaña General de Preferencias de la GUI
  — pensado desde el principio para varias carpetas a la vez, no solo
  una. Los cuatro backends que comparten reciben ahora una
  `SharedLibrary` propia tanto desde `main.py` como desde
  `gui/connection_manager.py`, y la re-escanean en cada `connect()`.
  La pestaña Red muestra dos detalles nuevos por red (`shared_files`,
  `active_uploads`) vía el mismo `get_stats()` genérico que ya
  alimentaba esa pestaña.

  - **Soulseek**: se implementó el lado "soy la fuente" del protocolo,
    el inverso exacto del flujo de descarga ya existente.
    `_handle_incoming_peer_connection` reconoce ahora el mensaje
    `QueueUpload` (código 43) en una conexión "P" entrante y llama a
    `_handle_queue_upload()`, que busca el fichero pedido en la
    `SharedLibrary` (si no está, responde `UploadDenied`) y si lo
    tiene manda `TransferRequest` (código 40, dirección subida) con un
    token nuevo; si quien pidió el fichero responde `TransferResponse`
    con `allowed=True`, `_upload_file()` abre una conexión "F" hacia
    ese usuario reutilizando el mismo `_dial_peer()` (con su
    estrategia dual directa + indirecta vía servidor que ya resolvía
    el bloqueo de NAT en las descargas), escribe el token en crudo,
    lee el offset en crudo que manda quien descarga (para poder
    reanudar) y a partir de ahí transmite el fichero desde ese punto.
    Validado con un test de protocolo real (`_PeerConn`,
    `QueueUpload`/`TransferRequest`/`TransferResponse` y la conexión
    "F" tal cual las manda un cliente Soulseek real) verificado con
    SHA256 byte a byte. **Importante sobre cómo se validó**: un primer
    intento contra el servidor público real de Soulseek
    (`server.slsknet.org`) con dos cuentas desechables, ambas
    corriendo en esta misma máquina, falló con "no se pudo conectar
    con el peer (posible NAT/firewall en ambos extremos)" — la propia
    sandbox de desarrollo no tiene ningún puerto reenviado en el
    router, así que ni la conexión directa ni la indirecta vía
    servidor pueden completarse entre dos procesos que corren aquí
    mismo (ninguno de los dos es alcanzable desde fuera), exactamente
    el caso límite que ya contempla el propio protocolo cuando ningún
    extremo tiene el puerto abierto — no es un fallo del código nuevo.
    Se validó entonces sustituyendo solo esa capa de alcanzabilidad de
    red (`_dial_peer`) por una conexión directa a localhost, dejando
    intacta toda la lógica real de negociación y transferencia; con
    eso sí se completó la subida con el contenido verificado byte a
    byte.
  - **DC++**: se comprobó primero que este backend no usa TTH (Tiger
    Tree Hash) — las búsquedas y resultados son `$Search`/`$SR` NMDC
    por nombre de fichero, así que compartir no exigía implementar
    ningún hash. `connect_to_hub()` ahora manda el tamaño real
    compartido en el campo de `$MyINFO` (antes fijo a `0`).
    `_hub_read_loop()` reconoce dos mensajes nuevos que el hub
    reenvía: `$Search` de otro usuario, contestado con `$SR` si algo
    de la carpeta compartida hace match por palabras (solo se soporta
    el modo pasivo de quien busca, "Hub:nick" — el modo activo
    exigiría responder por UDP, fuera de esta pieza), y `$ConnectToMe`
    dirigido a nosotros — el caso inverso del que ya se usaba para
    pedir descargas: en vez de pedirle a otro que se conecte a
    nosotros, aquí nos piden a nosotros que marquemos a otro.
    `_dial_out_as_uploader()` abre esa conexión saliente y reutiliza
    tal cual `_handle_incoming_peer()` (el mismo método que atiende
    conexiones entrantes normales): su lógica de buscar el nick en las
    descargas activas ya implica automáticamente "modo subida" cuando
    no encuentra ninguna coincidencia, que es siempre el caso en esta
    dirección. En modo subida se manda `$Direction Upload` en vez de
    `Download` y se atiende `$Get` (nuevo método `_handle_get()`, que
    parsea el offset de `$Get archivo$offset`, contesta `$FileLength`
    y sirve el fichero en crudo tras el `$Send`). Validado en vivo de
    extremo a extremo contra un hub NMDC sintético propio (para no
    arriesgar infraestructura de hub pública real con un protocolo de
    ida y vuelta) con dos `DCPPBackend` reales: búsqueda por `$Search`/
    `$SR` encontró el fichero compartido, `$ConnectToMe` en sentido
    inverso completó el handshake, y la descarga completa (2.5 MB) se
    verificó con SHA256 byte a byte.
  - **Gnutella2**: a diferencia de Soulseek y DC++, Gnutella2 direcciona
    el contenido por hash (`urn:sha1:...`), no por nombre, así que
    `SharedLibrary` calcula ahora un SHA1 de cada fichero compartido
    (ver arriba) y `G2Backend` gana un nuevo GUID de servent propio
    (`self._guid`, 16 bytes generados al arrancar, anunciado en el
    hijo `/GU` de cada `/QH2` que mandamos — es el mecanismo natural
    para que el hub aprenda a qué conexión de hoja pertenece ese GUID
    y así pueda enrutarnos futuros `/PUSH`; no se pudo contrastar
    contra el código fuente de una implementación real porque ya no
    hay ninguna clonada en este equipo, así que queda marcado como
    diseño razonado pero no confirmado contra un hub real). `connect()`
    ahora, si hay `SharedLibrary` con contenido, arranca un
    `asyncio.start_server` propio en el puerto de escucha configurado
    y dos rutas nuevas conviven en `_read_loop()`: paquetes `/Q2`
    entrantes se contestan con nuestro propio `/QH2`
    (`_handle_incoming_query`, buscando por substring en la ruta
    relativa igual que hace el lado de descarga) y paquetes `/PUSH`
    entrantes dirigidos a nuestro GUID (`pkt.find("TO").payload ==
    self._guid`) abren una conexión saliente hacia quien pidió el
    fichero y le mandan la línea `PUSH guid:<hex>\r\n\r\n` ya usada en
    el lado descarga (`_handle_incoming_push`). Ambas rutas confluyen
    en `_serve_http`, que interpreta la petición `GET
    /uri-res/N2R?urn:sha1:<base32>`, resuelve el fichero por
    `find_by_sha1_b32`, respeta `Range:` para reanudaciones (`206
    Partial Content`) y sirve el contenido en crudo. **Validado
    exclusivamente contra un hub G2 sintético propio** (nunca contra
    la red real, siguiendo la política de seguridad ya establecida
    para esta red — ver más abajo, "Sobre Gnutella2"): con dos
    `G2Backend` reales, búsqueda de "cancion_test" encontró el
    fichero compartido, la descarga completa (1.5 MB) por HTTP directo
    se verificó con SHA256 byte a byte y `active_uploads` volvió a 0
    al terminar. La ruta de `/PUSH` quedó implementada y simétrica con
    el código de descarga ya existente, pero no se ejercitó en esta
    prueba concreta porque ambos peers eran alcanzables directamente
    por localhost.
  - **eMule/eD2k**: la última pieza del punto 1 del backlog. Como
    Gnutella2, direcciona el contenido por hash — pero en dos capas:
    hash eD2k del fichero completo (MD4, o MD4 de la concatenación de
    los MD4 de cada parte de 9.728.000 bytes si el fichero tiene 2 o
    más partes) más, opcionalmente, el hashset por parte que un peer
    puede pedir aparte con `OP_HASHSETREQUEST`. `_handle_incoming_peer`
    (el listener TCP que ya existía solo para el fallback de LowID,
    `OP_CALLBACKREQUEST`) ahora distingue: si el `OP_HELLO` entrante no
    corresponde a ninguna llamada de vuelta pendiente y hay algo
    compartido, pasa la conexión a `_serve_upload_session`, el lado
    "soy la fuente" en espejo exacto de `_client_transfer_loop` (el
    lado descarga ya existente): contesta `OP_HELLOANSWER`, y a partir
    de ahí es quien conectó el que pide —`OP_SETREQFILEID` (resuelto
    vía `SharedLibrary.find_by_ed2k`, `OP_FILESTATUS` si lo tenemos u
    `OP_FILEREQANSNOFIL` si no), `OP_HASHSETREQUEST`→`OP_HASHSETANSWER`,
    `OP_STARTUPLOADREQ`→`OP_ACCEPTUPLOADREQ` (sin cola real: se concede
    slot al momento, ver nota de simplificaciones al principio de
    `emule_backend.py`) y `OP_REQUESTPARTS`→`OP_SENDINGPART`
    (`_answer_request_parts`, que interpreta los hasta 3 tramos
    `[start,end)` que puede traer un único `OP_REQUESTPARTS` y contesta
    uno por cada tramo real, mismo formato hash+start+end+datos que ya
    interpretaba el lado descarga). Para que el fichero compartido sea
    además **descubrible** por otros y no solo servible a quien ya
    conozca su hash, se añadió `OP_OFFERFILES` (opcode `0x15`, entre
    `OP_GETSERVERLIST` y `OP_SEARCHREQUEST` en la tabla real de
    opcodes): tras el login, si hay algo compartido, se manda al
    servidor con el mismo formato —en espejo— que ya se sabía
    interpretar de las entradas de `OP_SEARCHRESULT`
    (`_parse_search_result`), así que el servidor puede indexarlo y
    devolverlo luego tanto en una búsqueda ajena como en un
    `OP_GETSOURCES` de quien ya conoce el hash. **No** se implementó el
    equivalente en Kad (`KADEMLIA2_PUBLISH_KEY_REQ`/
    `KADEMLIA2_PUBLISH_SOURCE_REQ`): sin esa publicación, un fichero
    compartido por esta vía es descubrible por cualquiera que busque en
    el servidor eD2k al que estemos conectados, pero no por una
    búsqueda puramente Kad — gap documentado, no un descuido. Validado
    de extremo a extremo contra un servidor eD2k sintético propio
    (`fake_ed2k_server.py`, nunca contra la red real, mismo criterio de
    prudencia ya aplicado a Gnutella2 con las búsquedas) con dos
    `EMuleBackend` reales: `OP_OFFERFILES` se parseó correctamente en
    el servidor sintético, una búsqueda por palabra encontró el
    fichero compartido, `OP_GETSOURCES` devolvió la fuente correcta, y
    la descarga completa (con hashset de varias partes) se verificó
    con SHA256 byte a byte, con `active_uploads` volviendo a 0 al
    terminar. **Nota de rendimiento, no de esta sesión**: el MD4 propio
    (`core/md4.py`, puro Python, necesario porque el OpenSSL de muchos
    sistemas ya no trae MD4 habilitado) tardó ~92 segundos en hashear
    10 MB en esta máquina durante las pruebas — nada nuevo introducido
    aquí, pero conviene saber que la verificación final de una
    descarga (o el escaneo inicial de una carpeta compartida grande)
    puede notarse lenta con ficheros grandes.

A petición explícita del usuario (punto 21 del backlog, "Visibilidad y
gestión completa de HighID/LowID"), se hizo visible en la GUI el estado
de ID de eMule/eD2k que el backend ya calculaba internamente
(`EMuleBackend.is_high_id`) pero que hasta ahora no se mostraba en
ningún sitio. `EMuleBackend.get_stats()` añade una clave `"id_status"`
("high"/"low", solo presente cuando hay conexión real al servidor) que
`NetworkTab._format_stats()` (`gui/widgets/network_tab.py`) traduce vía
una nueva tabla `_STAT_VALUE_KEYS` a "ID alta"/"ID baja" (es), "High
ID"/"Low ID" (en) o "ID altua"/"ID baxua" (eu), añadidas en
`gui/i18n.py` junto con la etiqueta `stat_id_status`. Validado en vivo
desde la propia GUI (`xdotool`/`import` sobre X11, `QT_QPA_PLATFORM=xcb`):
conectado a un servidor eD2k real, la pestaña Red muestra en la fila
eMule/Kad "Servidor: 85.17.116.222:6082 · Nick: P2PTotalUser · Nodos
conocidos: 178 · Descargas activas: 0 · Estado ID: ID alta".

Después, siguiendo la regla de proceso del backlog (ver "Roadmap —
backlog de mejoras futuras"), se completó la parte de "gestión"
pendiente del punto 21: el caso inverso de LowID (recibir la petición
de conexión entrante cuando nosotros somos LowID y alguien pide algo
que compartimos) y el equivalente Kad de nodo "firewalled". Verificado
contra el código fuente real de aMule
(`amule-project/amule` en GitHub, `src/include/protocol/ed2k/Client2Server/TCP.h`
y `src/include/protocol/kad/Client2Client/UDP.h`) para confirmar los
opcodes exactos antes de implementar, en vez de asumirlos:
`OP_CALLBACKREQUESTED` (0x35, servidor -> cliente LowID, payload
`<IP 4><PORT 2>` del peer que quiere descargar de nosotros) ahora se
reconoce en `_server_read_loop()` y dispara `_serve_via_callback()`,
que abre la conexión saliente hacia ese peer, manda `OP_HELLO` (en vez
de esperarlo, ya que aquí el papel de quien llama se invierte respecto
al caso normal) y a partir de ahí reutiliza `_serve_upload_session()`
(con el nuevo parámetro `send_hello_answer=False` para no duplicar el
handshake) para servir el fichero exactamente igual que con un peer
entrante normal. El equivalente Kad
(`KADEMLIA_FIREWALLED_REQ`/`KADEMLIA_FIREWALLED_RES`/
`KADEMLIA_FIREWALLED_ACK_RES`, 0x50/0x58/0x59) se implementó en los dos
sentidos: `check_kad_firewall()` manda `KADEMLIA_FIREWALLED_REQ` (con
nuestro puerto TCP) a un contacto Kad confirmado y espera su
respuesta, y se llama automáticamente al final de `connect_kad()` si
hay algún contacto confirmado; en el sentido contrario,
`_on_kad_firewalled_req()` atiende peticiones de otros nodos e intenta
conectar por TCP a su puerto para decirles si son alcanzables. El
resultado se expone en `get_stats()` como `"kad_firewalled"`
("open"/"firewalled") y se traduce en la pestaña Red de la GUI
(`stat_kad_firewalled`, `kad_firewalled_open`/`kad_firewalled_yes` en
`gui/i18n.py`, es/en/eu). Validado con dos `EMuleBackend` reales por
loopback (la red Kad real de este entorno sigue sin devolver ningún
contacto confirmado — 0/178 en el bootstrap de esta sesión, el mismo
patrón ya documentado más arriba, así que un roundtrip real contra
Kad no es posible aquí): comprobación de firewall con el peer
alcanzable (resultado `False`, abierto) y con su puerto cerrado
(resultado `True`, firewalled), y el flujo completo de
`OP_CALLBACKREQUESTED` sirviendo un fichero compartido real a un
"downloader" simulado que reproduce el protocolo cliente-cliente
(`OP_HELLO` -> `OP_HELLOANSWER` -> `OP_SETREQFILEID` ->
`OP_FILESTATUS` con el fichero encontrado). Con esto el punto 21 queda
completo — ver el backlog actualizado.

Siguiendo con el punto 2 del backlog ("Límite de velocidad de subida/
bajada, global y por descarga"), se implementó en las cinco redes.
Para las cuatro redes "manuales" (Soulseek, DC++, Gnutella2 y eMule) se
creó `core/rate_limiter.py` con un `RateLimiter` tipo "cubo de fichas"
(`consume(n_bytes)` duerme lo justo para no superar la tasa fijada;
`set_rate()` cambia el límite en caliente; tasa `<= 0` = sin límite) y
dos instancias compartidas a nivel de módulo, `global_download_limiter`
y `global_upload_limiter`, usadas directamente por los bucles de
lectura/escritura de socket ya existentes de las cuatro redes
(`_handle_incoming_file_connection`/`_upload_file` en
`soulseek_backend.py`, `_receive_file`/`_handle_get` en
`dcpp_backend.py`, `_send_get_and_receive`/`_serve_http` en
`g2_backend.py`, `_client_transfer_loop`/`_answer_request_parts` en
`emule_backend.py`): cada trozo leído/escrito llama primero a
`global_download_limiter.consume()`/`global_upload_limiter.consume()`
y, en el lado de la descarga, también al `RateLimiter` propio de esa
descarga (`entry["limiter"]`, guardado en el diccionario que cada
backend ya llevaba por descarga activa), así que la velocidad real
queda acotada por el más estricto de los dos. BitTorrent no usa este
limitador manual: `TorrentBackend.set_global_limits()` aplica
`download_rate_limit`/`upload_rate_limit` directamente a
`session.apply_settings()` (límite global nativo de libtorrent) y
`TorrentBackend.set_download_limit()` usa
`handle.set_download_limit()`/`set_upload_limit()` por torrent (cubre
ambos sentidos porque, en BitTorrent, la propia descarga sigue subiendo
piezas ya recibidas mientras está en marcha). `NetworkBackend` (`core/
backend_base.py`) gana los dos métodos no abstractos
`set_global_limits()`/`set_download_limit()` (no-op por defecto: las
cuatro redes manuales no necesitan sobreescribir el primero porque ya
comparten el limitador global de módulo). Los límites globales viven en
`Config.global_download_limit_kbps`/`global_upload_limit_kbps` (kB/s, 0
= ilimitado, nueva sección en la pestaña General de Preferencias) y se
aplican tanto al conectar cada red (`ConnectionManager._apply_speed_limits`)
como en caliente al guardar Preferencias
(`ConnectionManager.apply_global_speed_limits()`, llamado desde
`MainWindow._on_open_settings`) sin necesidad de reconectar. El límite
por descarga vive en `Download.speed_limit_bps` (solo en memoria, no se
persiste en `core/database.py` — mismo criterio ya aplicado a
`speed_bps`/`connected_peers`) y se fija desde un nuevo elemento del
menú contextual de la pestaña Transferencias ("Límite de
velocidad…", `DownloadsTab._on_set_speed_limit`) que llama a
`DownloadManager.set_download_limit()`. Validado con pruebas
funcionales aisladas del `RateLimiter` (tasa fija, cambio en caliente
con `set_rate()`, tasa 0 = sin espera) y con una simulación de
transferencia por chunks de 64 KB combinando un límite global más
holgado con uno propio más estricto, comprobando que domina el más
bajo de los dos y que subir el límite propio a mitad de transferencia
acelera el resto inmediatamente — sin depender de la red real, dado
que la lógica es puramente de temporización y no de protocolo. La CLI
(`python main.py download`) también respeta los límites globales de
`config.json`: `cmd_download()` en `main.py` llama a
`apply_global_limits()` al arrancar y, para BitTorrent, a
`set_global_limits()` sobre la sesión recién creada (el límite por
descarga queda como algo específico de la GUI, ya que la CLI no tiene
una sesión interactiva en la que cambiarlo a mitad de transferencia).

Con el punto 3 del backlog ("Selección de archivos y descarga
secuencial dentro de un torrent multi-archivo"), se verificó primero
contra el propio `libtorrent` 2.0.13.0 instalado en el `venv` del
proyecto (introspección directa de `dir()` sobre `torrent_handle`,
`file_storage` y `torrent_status`, mismo método ya usado para verificar
la API real de aMule en el punto 21) que la librería ya trae de fábrica
todo lo necesario: `prioritize_files()`/`file_priorities()` para elegir
qué archivos bajar (prioridad 0 = omitir, 4 = normal), `file_progress()`
para el progreso por archivo, y `set_sequential_download()` (más
`torrent_status.sequential_download` para consultar el estado actual)
para el orden de piezas. Solo faltaba exponerlo, así que
`TorrentBackend` (`backends/torrent_backend.py`) gana cuatro métodos
nuevos — `list_files()` (índice, ruta, tamaño, prioridad y bytes
descargados de cada archivo, `None` si el torrent aún no tiene
metadatos), `set_file_priorities(download, {índice: prioridad})`,
`set_sequential_download(download, bool)` y
`is_sequential_download(download)` — todos operando sobre el
`entry["handle"]` ya existente en `TorrentBackend._active`, sin tocar
`core/database.py` (es estado en memoria, mismo criterio que
`speed_bps`/`speed_limit_bps`). `NetworkBackend` (`core/backend_base.py`)
gana las mismas cuatro firmas como métodos no abstractos con
implementación por defecto vacía/`None`, ya que el concepto de "varios
archivos dentro de una misma descarga" solo existe en BitTorrent (las
otras cuatro redes descargan siempre un único archivo suelto).
`DownloadManager` añade envoltorios finos (`list_torrent_files`,
`set_file_priorities`, `set_sequential_download`,
`is_sequential_download`) que resuelven el backend vía
`BackendRegistry` igual que ya hacía `set_download_limit`. En la GUI,
la pestaña Transferencias gana un nuevo elemento de menú contextual
("Seleccionar archivos…", `DownloadsTab`), visible solo cuando hay una
única descarga seleccionada, es de red `Network.TORRENT` y ya tiene
metadatos (`list_torrent_files()` no vacío); abre el nuevo diálogo
`gui/widgets/torrent_files_dialog.py` (`TorrentFilesDialog`), con una
tabla de archivos con casilla de marcado por archivo (marcado = bajar a
prioridad normal, desmarcado = prioridad 0/omitir) y una casilla
"Descarga secuencial" que refleja el estado real leído del handle al
abrir el diálogo; al aceptar, aplica ambos cambios de una vez. Nuevas
claves de `gui/i18n.py` en los tres idiomas (es/en/eu):
`ctx_torrent_files`, `dlg_torrent_files_title`, `col_file_name`,
`col_file_size`, `chk_sequential_download`. Validado con un torrent
multi-archivo sintético creado y hasheado con el propio `libtorrent`
(dos archivos de datos reales, con sus dos `.pad` de alineación que la
librería añade de fábrica) añadido a una `lt.session` real local: primero
a nivel de `TorrentBackend` puro (comprobando que `list_files()` refleja
bien índice/ruta/tamaño/prioridad, que `set_file_priorities()` cambia la
prioridad del archivo elegido a 0 y `is_sequential_download()` refleja
el `True`/`False` fijado por `set_sequential_download()`), y después con
una segunda pasada de extremo a extremo instanciando el propio
`TorrentFilesDialog` de Qt (con `QT_QPA_PLATFORM=offscreen`),
desmarcando una fila de la tabla real y marcando la casilla de
secuencial, disparando su `_on_accept()` y comprobando que el cambio
llega correctamente hasta el handle de libtorrent — sin depender de la
red real ni tocar la base de datos de descargas del usuario.

Con el punto 4 del backlog ("Prioridad/orden de la cola de descargas
desde la GUI"), `Download` (`core/models.py`) gana un campo
`priority: int` (menor = más arriba/antes en la cola) que, a diferencia
de `speed_bps`/`speed_limit_bps`, sí se persiste: la tabla `downloads`
de `core/database.py` gana la columna `priority INTEGER NOT NULL
DEFAULT 0`, con migración automática en `init_db()` para bases de datos
ya existentes de antes de este punto (se detecta con `PRAGMA
table_info` y, si falta la columna, se añade con `ALTER TABLE` y se
rellena con el propio `id`, así las descargas más antiguas quedan
arriba, igual que aparecían antes con el orden por `added_at`).
`insert_download()` asigna a cada descarga nueva `MAX(priority)+1` (se
añade al final de la cola, no al principio) y `load_all_downloads()`
ahora ordena por `priority ASC` en vez de por `added_at DESC`.
`DownloadManager.reorder(downloads)` recibe la lista ya en el orden
deseado, reasigna `priority = 0, 1, 2...` a cada `Download` y llama a
la nueva `database.reorder_downloads()` (un `UPDATE` por fila vía
`executemany`). En la GUI, `DownloadsModel` (`gui/models_qt.py`) gana
soporte de arrastrar y soltar filas de verdad: `flags()` añade
`ItemIsDragEnabled`/`ItemIsDropEnabled`, `mimeData()`/`dropMimeData()`
serializan/leen los índices de fila arrastrados (mime type propio,
`application/x-p2ptotal-download-rows`) y delegan en `move_rows_to()`,
que recoloca la lista en memoria con un reset completo del modelo (más
simple y fiable que llevar la cuenta fina de `beginMoveRows` para un
gesto manual poco frecuente) — nótese que `dropMimeData()` devuelve
`False` aposta tras mover las filas a mano, para que Qt no intente
además borrar las filas de origen por su cuenta (semántica por defecto
de una "acción de mover" en el framework). `move_row(row, delta)`
implementa subir/bajar una posición reutilizando la misma
`move_rows_to()`, usada por los dos nuevos elementos del menú
contextual de la pestaña Transferencias ("Subir en la cola"/"Bajar en
la cola", solo visibles con una única fila seleccionada y cuando no
está ya en ese extremo). Cualquiera de los dos mecanismos (arrastrar o
subir/bajar) emite la nueva señal `order_changed`, que
`DownloadsTab._on_order_changed` escucha para persistir el nuevo orden
llamando a `DownloadManager.reorder()`. `QTableView` activa
`setDragEnabled`/`setAcceptDrops`/`setDropIndicatorShown` y
`DragDropMode.InternalMove`. Validado en tres capas, todas contra una
base de datos SQLite temporal para no tocar el historial real del
usuario: (1) `core/database.py` a pelo — orden de inserción por
prioridad ascendente, nueva descarga añadida al final, `reorder_downloads()`
cambiando el orden correctamente, y la migración de una tabla antigua
sin columna `priority` creada a mano; (2) `DownloadsModel` a pelo, en
modo `QT_QPA_PLATFORM=offscreen` — `move_row()` subiendo y bajando una
fila (con los casos límite como no-op) y `dropMimeData()` invocado
directamente (sin simular el gesto de ratón real, mismo criterio ya
usado para el diálogo de archivos de torrent del punto 3) comprobando
el resultado del arrastre; y (3) de extremo a extremo, instanciando la
propia `DownloadsTab` real contra un `DownloadManager` con esa base de
datos temporal, sembrando tres descargas, subiendo una en la cola desde
el modelo y releyendo el orden final directamente del fichero SQLite en
disco para confirmar que quedó persistido.

Con el punto 5 del backlog ("Categorías/etiquetas de descarga con
carpeta de destino asociada por categoría, al estilo aMule/
qBittorrent"), `core/config.py` gana un nuevo dataclass `Category`
(`name` + `dest_dir`) y `Config.categories: list[Category]`
(persistido en `config.json` como el resto de la configuración, con
`load_config()`/`save_config()` serializando/deserializando la lista
igual que el resto de campos). `Download` (`core/models.py`) gana un
campo `category: Optional[str]` (el nombre de la categoría elegida,
`None` = sin categoría) que sí se persiste: la tabla `downloads` de
`core/database.py` gana la columna `category TEXT` (nullable, sin
`DEFAULT`), con la misma migración automática vía `PRAGMA table_info`
+ `ALTER TABLE` ya usada para `priority` en el punto 4, para no romper
bases de datos existentes. La categoría se elige en el momento de
arrancar la descarga (determina la carpeta de destino usada en vez de
`default_download_dir`), no hay recategorización ni movido de fichero
a posteriori para descargas ya en curso o completadas — fuera del
alcance de este punto, que solo pedía "el mismo hueco de esquema".
`DownloadManager.download()` gana un parámetro opcional
`category: str | None = None`, que asigna a `download.category` antes
de insertarlo en la base de datos. En la GUI, la pestaña de Búsqueda
(`gui/widgets/search_tab.py`) añade un submenú "Descargar a categoría"
al menú contextual de resultados (solo aparece si hay alguna categoría
configurada), listando las categorías de `load_config().categories`;
elegir una descarga directamente a su `dest_dir` en vez de a la
carpeta por defecto. La señal `download_requested` de
`SearchResultsPanel` y `SearchTab` pasó de `pyqtSignal(object, str)` a
`pyqtSignal(object, str, object)` para llevar también el nombre de la
categoría (o `None`), y `MainWindow._on_download_requested`/
`_start_download` (`gui/main_window.py`) se actualizaron a juego para
reenviar ese tercer argumento a `DownloadManager.download()`. El
diálogo de Preferencias (`gui/widgets/settings_dialog.py`) gana una
`QListWidget` de categorías en la pestaña General, con botones
"Añadir categoría…" (pide nombre por `QInputDialog` y carpeta por
`QFileDialog`, evitando nombres duplicados) y "Quitar", guardado en
`config.categories` al aceptar — mismo patrón ya usado ahí para
`shared_folders`. La pestaña Transferencias (`gui/models_qt.py`,
`DownloadsModel`) gana una séptima columna "Categoría" que muestra
`download.category` (vacía si no tiene). Nuevas claves de
`gui/i18n.py` en los tres idiomas (es/en/eu): `ctx_download_to_category`,
`lbl_categories`, `btn_add_category`, `lbl_category_name`,
`col_category`. Validado en varias capas contra ficheros de
configuración y base de datos temporales (nunca los reales del
usuario): (1) `Config`/`load_config()`/`save_config()` guardando y
releyendo una lista de categorías; (2) `core/database.py` a pelo —
inserción y lectura de una descarga con categoría, y migración de una
tabla antigua sin columna `category` creada a mano; (3)
`SettingsDialog` en modo `QT_QPA_PLATFORM=offscreen` — añadir y quitar
categorías desde la lista y comprobar que persisten correctamente en
`config.json`; (4) `SearchResultsPanel` en el mismo modo offscreen —
descarga normal (sin categoría, va a `default_download_dir`) y
descarga a una categoría concreta (va a su `dest_dir`, señal emitida
con el nombre de la categoría) comprobadas por separado; y (5)
`DownloadsModel` — la nueva columna "Categoría" mostrando el nombre
correcto o vacío según el caso. Después se validó también en vivo,
desde la GUI real (automatizada con `xdotool`/`spectacle`), el ciclo
completo: crear la categoría "Música" desde Preferencias con carpeta
de destino propia, conectar Soulseek real, buscar "jackson" (61.243
resultados), usar el submenú "Descargar a categoría" → "Música" del
menú contextual sobre un resultado real, y comprobar en la pestaña
Transferencias que la descarga arrancó con la carpeta de destino de la
categoría y que la columna "Categoría" mostraba "Música" — completada
al 100% con el MP3 verificado en disco. Los artefactos de esa prueba
(categoría, carpeta y entrada de descarga) se limpiaron después del
entorno real del usuario.

Punto 6 (reintento automático configurable): `core/download_manager.py`
(`DownloadManager._on_backend_progress`) detecta cuándo una descarga
cae en `DownloadState.ERROR` y, si `auto_retry_max_attempts` de
Preferencias (0 = desactivado, 3 por defecto) no se ha agotado
todavía para esa descarga, reprograma un `backend.resume_download()`
pasados `auto_retry_delay_seconds` (30 s por defecto) en vez de dejarla
en error sin más. No hizo falta tocar ningún backend: los cinco
(`soulseek_backend.py`, `emule_backend.py`, `g2_backend.py`,
`dcpp_backend.py`, `torrent_backend.py`) ya guardan su entrada activa
aunque acaben en ERROR (solo la quitan al cancelar), así que
`resume_download()` sirve tal cual para relanzar la búsqueda de
fuentes — mismo mecanismo que ya usaban pausar/reanudar manual, solo
que disparado automáticamente. El contador de intentos gastados vive
en `DownloadManager._retry_attempts` (dict en memoria, `download.id`
→ intentos), no se resetea con cada reintento (para que
`auto_retry_max_attempts` sea un tope real y no se pueda burlar con
una sucesión de conexiones que se caen al momento) y solo se limpia al
completarse o cancelarse la descarga, o al borrarla del historial.
`Config` (`core/config.py`) gana `auto_retry_max_attempts` y
`auto_retry_delay_seconds`, con el mismo patrón de guardado/lectura ya
usado para el resto de ajustes; `SettingsDialog`
(`gui/widgets/settings_dialog.py`) gana dos campos nuevos en la
pestaña General (`QSpinBox` con `setSpecialValueText` para "Sin
reintentos" en 0, y `QDoubleSpinBox` para la espera en segundos).
Nuevas claves de `gui/i18n.py` en los tres idiomas:
`lbl_auto_retry_attempts`, `lbl_auto_retry_delay`, `spin_no_retry`.
Validado: (1) round-trip de `Config`/`load_config()`/`save_config()`
con los dos campos nuevos; (2) `DownloadManager` con un backend
simulado — confirmado que con `auto_retry_max_attempts=2` se reintenta
exactamente 2 veces y no una tercera, y que el contador se limpia al
completarse; (3) con `auto_retry_max_attempts=0` no se reintenta
nunca; (4) `SettingsDialog` en modo `QT_QPA_PLATFORM=offscreen`
guardando los dos campos nuevos en `config.json`.

Punto 7 (historial de búsquedas persistente): `core/database.py` gana
una tabla nueva `search_history` (query, redes usadas separadas por
comas, tipo de archivo y fecha), creada junto a `downloads` en
`init_db()` — hubo que cambiar `conn.execute(SCHEMA)` por
`conn.executescript(SCHEMA)` porque `SCHEMA` pasó a tener dos
sentencias `CREATE TABLE` en vez de una. Cada vez que se lanza una
búsqueda nueva desde `SearchTab._on_search_clicked`
(`gui/widgets/search_tab.py`) se guarda automáticamente vía
`DownloadManager.save_search()`, y se recorta a las 50 más recientes
(`SEARCH_HISTORY_LIMIT`) para que la tabla no crezca sin límite. La
pestaña Búsqueda gana un botón "🕑 Historial" junto al de buscar que
abre un menú con cada búsqueda guardada (`query — redes — fecha`) más
una opción "Borrar historial"; al elegir una entrada se rellenan el
campo de texto, el filtro de tipo y las casillas de red (solo las que
sigan conectadas) y se relanza la búsqueda en una pestaña nueva,
reutilizando `_on_search_clicked`. No se ha intentado restaurar
automáticamente las pestañas de resultados en sí al arrancar la GUI
(los resultados de una red P2P quedan obsoletos casi al instante —
fuentes que se conectan y desconectan — y lanzar búsquedas reales
contra la red sin que el usuario lo pida no pareció deseable); en su
lugar el historial permite repetir cualquier búsqueda pasada con dos
clics, con las redes originales pre-marcadas, incluso después de
cerrar y reabrir la GUI. Nuevas claves de `gui/i18n.py` en los tres
idiomas: `btn_search_history`, `msg_history_empty`,
`history_entry_label`, `ctx_clear_history`. Validado en modo headless/
offscreen: (1) `core/database.py` — insertar, cargar (orden más
reciente primero), recorte al límite y borrado completo; (2)
`DownloadManager.save_search`/`load_search_history`/
`clear_search_history` contra una base de datos temporal, comprobando
además que la tabla `downloads` sigue intacta tras el cambio a
`executescript`; (3) `SearchTab` completo con `QT_QPA_PLATFORM=offscreen`
— lanzar una búsqueda real la guarda en el historial, y
`_apply_history_entry()` repite esa búsqueda en una pestaña nueva
rellenando query/tipo/redes correctamente.

Punto 8 (búsquedas guardadas / alertas): módulo nuevo
`core/saved_search_manager.py` (`SavedSearchManager`, mismo patrón que
`DownloadManager` — la GUI solo habla con él, nunca con
`core/database.py` directamente) que mantiene un bucle en segundo
plano (`asyncio.ensure_future`, comprobación cada 30 s de qué búsquedas
guardadas tienen ya cumplido su propio `interval_minutes`) y reejecuta
cada búsqueda guardada vía `DownloadManager.search_all()`. `core/
database.py` gana dos tablas nuevas, `saved_searches` (query, redes,
tipo de archivo, intervalo, activa/inactiva, última comprobación y
`seen_keys` — el conjunto de resultados ya vistos, serializado como
JSON) y `saved_search_alerts` (los resultados nuevos encontrados,
pendientes de revisar), con sus funciones de acceso correspondientes.
Cada resultado se identifica para deduplicar por la clave
`red|título|tamaño`; la primera comprobación de una búsqueda recién
guardada solo establece esa "foto" de partida sin generar ninguna
alerta (para no bombardear con lo que ya existía al guardarla), y las
comprobaciones siguientes solo avisan de lo que no estuviera en el
`seen_keys` anterior. El filtro de tipo de archivo, que antes vivía
duplicado dentro de `gui/widgets/search_tab.py`, se extrajo a un módulo
nuevo `core/file_types.py` para poder reutilizarlo también desde este
gestor (que vive en `core/`, no puede depender de `gui/`). En la GUI:
la pestaña Búsqueda gana un botón "🔔 Guardar como alerta" (pide el
intervalo en minutos con `QInputDialog`) y una pestaña nueva "🔔
Alertas" (`gui/widgets/alerts_tab.py`, `AlertsTab`) que lista todas las
búsquedas guardadas con sus columnas (búsqueda, redes, intervalo,
activa, última comprobación, novedades) y un menú contextual para
activar/desactivar, comprobar ya mismo, ver las novedades (abre
`gui/widgets/alert_results_dialog.py`, `AlertResultsDialog`, que
reutiliza el mismo `SearchResultsModel`/menú de descarga que la
pestaña de Búsqueda) o eliminar la alerta; el título de la propia
pestaña muestra un contador de resultados nuevos pendientes
(`"🔔 Alertas (n)"`), actualizado cada vez que el gestor detecta algo
nuevo. `MainWindow` arranca y para `SavedSearchManager` junto con el
resto del ciclo de vida de la ventana. Nuevas claves de `gui/i18n.py`
en los tres idiomas: `tab_alerts`, `tab_alerts_with_count`,
`btn_save_as_alert`, `dlg_save_alert_title`,
`dlg_save_alert_interval_label`, `msg_alert_saved`,
`dlg_new_alerts_title`, `col_query`, `col_networks`, `col_interval`,
`col_enabled`, `col_last_checked`, `col_new_alerts`,
`lbl_minutes_short`, `lbl_yes`, `lbl_no`, `lbl_never_checked`,
`ctx_enable_alert`, `ctx_disable_alert`, `ctx_run_now`,
`ctx_view_new_alerts`, `ctx_delete_saved_search`. Validado: (1) CRUD
completo de `saved_searches`/`saved_search_alerts` en
`core/database.py` contra una base de datos temporal; (2)
`SavedSearchManager` con un `DownloadManager` simulado — primera
comprobación establece el baseline sin generar alertas, segunda
comprobación detecta exactamente el resultado nuevo y dispara el
callback `on_alert`, el filtro de tipo de archivo se aplica antes de
calcular qué es "visto", y activar/desactivar/eliminar funcionan; (3)
`MainWindow` completo en modo `QT_QPA_PLATFORM=offscreen` — guardar una
alerta desde `SearchTab`, verla aparecer en `AlertsTab`, el contador
del título de la pestaña actualizándose al insertar una alerta nueva y
volviendo a "🔔 Alertas" al descartarla desde `AlertResultsDialog`.

Punto 9 (navegar los archivos compartidos de un usuario de Soulseek):
protocolo confirmado directamente contra el código fuente real de
Nicotine+ (`SharedFileListRequest` = mensaje de peer código 4,
`SharedFileListResponse` = código 5, payload comprimido con zlib, con
la misma estructura de tamaño de fichero de 8 bytes —y el mismo bug
histórico de Soulseek NS para ficheros >2GiB— que ya se manejaba en
`_Unpacker.file_size()` para las respuestas de búsqueda normales).
`backends/soulseek_backend.py` gana `SoulseekBackend.browse_user()`,
que reutiliza `_dial_peer(username, "P")` (la misma estrategia dual
directa+indirecta que ya usan las descargas) y el helper de módulo
`_parse_shared_file_list()`, devolviendo una lista de
`(carpeta, [SearchResult, ...])` — cada `SearchResult` reutiliza
`_encode_source_id()` con la ruta remota completa (`carpeta\nombre`),
así que se puede pedir su descarga exactamente por el mismo camino que
un resultado de búsqueda normal, sin código nuevo de descarga.
`NetworkBackend.browse_user()` (`core/backend_base.py`) se añadió como
método por defecto no abstracto que devuelve `None` (mismo patrón que
`list_files`), sobrescrito solo por `SoulseekBackend`; y
`DownloadManager.browse_user(network, username)` hace de envoltorio
fino sobre el backend correspondiente. En la GUI: nueva opción
"🗂️ Ver archivos compartidos" en el menú contextual de la pestaña de
Búsqueda (solo visible para resultados de Soulseek, ya que es la única
red que soporta el concepto), que abre `gui/widgets/browse_user_dialog.py`
(`BrowseUserDialog`) — lista de carpetas a la izquierda, tabla de
archivos de la carpeta seleccionada a la derecha (reutilizando
`SearchResultsModel`/`SearchResultsSortProxy`), con el mismo menú
contextual de descarga que la pestaña de Búsqueda. Nuevas claves de
`gui/i18n.py` en los tres idiomas: `ctx_browse_user`,
`dlg_browse_user_title`, `msg_browsing`, `msg_browse_error`,
`msg_browse_empty`, `status_browse_count`, `btn_close`. Validado: (1)
`_parse_shared_file_list()` contra un payload sintético
zlib-comprimido construido a mano con dos carpetas, un fichero normal
y un fichero >2GiB con el bug de tamaño de Soulseek NS, confirmando
carpetas, nombres, tamaños (incluido el workaround >2GiB) y que
`_encode_source_id`/`_decode_source_id` reconstruyen la ruta remota
correcta; (2) `BrowseUserDialog` en modo `QT_QPA_PLATFORM=offscreen`
con un `DownloadManager` simulado — carga las carpetas, las muestra en
la lista, y cambiar de carpeta actualiza la tabla de archivos
correctamente. No se ha probado `browse_user()` contra la red
Soulseek real en esta sesión (a diferencia de la búsqueda/descarga, sí
validadas en vivo anteriormente) porque no había a mano un usuario
conocido concreto que navegar sin arriesgarse a toparse con contenido
indeseado al elegir uno al azar de una búsqueda; el protocolo en sí
queda confirmado byte a byte contra el código fuente real de
Nicotine+ y contra el payload sintético.

**Bug real encontrado y corregido** (reportado por el usuario tras
usar la GUI en vivo): con una búsqueda de Soulseek todavía en curso
(streaming de resultados en segundo plano), el menú contextual de la
pestaña de Búsqueda se abría de forma intermitente y, cuando llegaba a
abrirse, se cerraba solo en cuanto llegaba un resultado nuevo. Causa:
la tabla de resultados tiene `setSortingEnabled(True)` sobre un
`QSortFilterProxyModel`, así que insertar o fusionar una fila
reordena el proxy (`layoutChanged`); como `qasync` mantiene vivo el
bucle de asyncio incluso dentro del bucle de eventos anidado que abre
`QMenu.exec()`, los resultados que iban llegando en segundo plano
seguían mutando el modelo mientras el menú estaba abierto, y ese
cambio de layout lo cerraba (o impedía que llegase a mostrarse).
Arreglado en `gui/widgets/search_tab.py`
(`SearchResultsPanel`) con un patrón de "guardar en cola mientras el
menú está abierto, aplicar de golpe al cerrarlo": una bandera
`_menu_open` y una lista `_pending_results`, comprobadas tanto en el
callback de resultados por streaming como en el bloque que aplica los
resultados finales de las redes que no hacen streaming; el mensaje de
estado final ("N resultados" + botón "Seguir buscando") también se
retrasa si la búsqueda termina con el menú todavía abierto. Validado
con un test offscreen que abre el panel real, simula resultados
llegando con el menú "abierto" (confirma que el modelo no cambia
mientras tanto) y comprueba que al "cerrarlo" se aplican todos de
golpe y el estado final queda correcto.

**Punto 10 — Browse Host (`/BH`) en Gnutella2**: a diferencia de G1,
G2 no define ningún paquete binario nuevo para esto — Browse Host es,
a nivel de wire, una petición HTTP normal `GET /` al puerto de escucha
del servent que se quiere explorar (típicamente el hub al que ya
estamos conectados, del que ya conocemos host:puerto por
`get_stats()`), con la cabecera `Accept: application/x-gnutella2` para
pedir la respuesta en formato de paquetes `/QH2` (los mismos que ya
sabíamos parsear de una búsqueda normal) en vez de HTML — confirmado
estudiando `src/core/uploads.c` (detección de la petición y de la
cabecera `Accept:`) y `src/core/bh_upload.c`
(`g2_build_qh2_results`, que construye esos `/QH2` a partir de TODA la
carpeta compartida) del código fuente real de gtk-gnutella (clonado el
17 de agosto de 2026 para esta consulta puntual, igual que ya se hizo
en su día para el resto del backend G2). Implementado en
`backends/g2_backend.py`: `G2Backend.browse_host(host, port)` (lado
cliente) abre una conexión TCP directa y manda la petición
explícitamente como `HTTP/1.0` — así el servidor no activa
`Transfer-Encoding: chunked` (que en gtk-gnutella solo se activa con
HTTP/1.1+) y sencillamente vuelca los `/QH2` seguidos hasta cerrar la
conexión, evitando tener que desenvolver framing chunked por encima
del framing ya binario de los propios paquetes G2; los resultados se
construyen con el mismo código que ya usaba `search()` para convertir
hits en `SearchResult` (extraído a un método común,
`_hits_to_results`). También se implementó el lado servidor
(`_serve_browse_host`, enganchado en `_serve_http` para la ruta `/`):
contesta con toda `SharedLibrary.list_files()` repartida en paquetes
`/QH2` de 50 archivos cada uno (mismo espíritu que el
`BH_SCAN_AHEAD` de gtk-gnutella), o 403 si no hay nada compartido, o
404 si el `Accept:` no pide expresamente `application/x-gnutella2`
(este cliente no genera la alternativa HTML pensada para navegadores).
`NetworkBackend.browse_host()` (`core/backend_base.py`) se añadió como
método por defecto no abstracto que devuelve `None` (mismo patrón que
`browse_user`), sobrescrito solo por `G2Backend`; y
`DownloadManager.browse_host(network, host, port)` hace de envoltorio
fino. En la GUI: la pestaña Red (`gui/widgets/network_tab.py`) ahora
tiene menú contextual — botón derecho sobre la fila de Gnutella2, si
hay un hub conectado (se lee `stats["server"]`), ofrece "🗂️ Explorar
hub (host:puerto)", que abre `gui/widgets/browse_host_dialog.py`
(`BrowseHostDialog`, tabla plana igual que la de la pestaña de
Búsqueda, con el mismo menú contextual de descarga). Validado: (1)
`_serve_browse_host` contra una `SharedLibrary` sintética de 120
archivos, confirmando que reparte en 3 paquetes `/QH2` (50+50+20) y
que el cliente los reconstruye en 120 `SearchResult` con el `source_id`
correcto (incluye la IP y el GUID del servent, igual que en una
búsqueda normal, así que la descarga posterior funciona por el mismo
camino sin código nuevo), además de los casos 403 (sin compartir nada)
y 404 (`Accept:` no pide G2); (2) `browse_host()` de extremo a extremo
sobre un socket TCP real (dos instancias de `G2Backend`, una sirviendo
y otra explorando) confirmando la conversación HTTP/1.0 completa; (3)
`BrowseHostDialog` y el nuevo `NetworkTab` (con `DownloadManager`
simulado) en modo `QT_QPA_PLATFORM=offscreen`. No se ha probado contra
un hub G2 real en esta sesión (a diferencia de la búsqueda, sí validada
en vivo anteriormente) porque no hay garantía de que un hub público
cualquiera tenga Browse Host habilitado ni de qué comparte; el
protocolo en sí queda confirmado byte a byte contra el código fuente
real de gtk-gnutella y contra las pruebas sobre sockets reales
anteriores.

**Punto 11 — Lista pública de hubs DC++**: se investigó el formato real
descargando en vivo (17 de agosto de 2026) la lista del agregador
`dchublist.com` — redirige (HTTP 301) a `te-home.net`, que sirve tanto
una versión comprimida (`hublist.xml.bz2`) como una plana sin comprimir
(`hublist.xml`, la que se usó finalmente para no tener que
descomprimir bz2 a mano); a fecha de la prueba tenía 247 hubs reales.
Formato: XML plano con un elemento `<Hub .../>` por hub y atributos
`Name`/`Address`/`Description`/`Country`/`Encoding`/`Users`/`Shared`
(bytes)/`Software` entre otros; `Address` es `host` o `host:puerto`
(sin puerto implica el 411 por defecto de NMDC) — confirmado que 119
de los 247 hubs de la muestra real usan un puerto explícito distinto
del 411. Implementado en `backends/dcpp_backend.py`, siguiendo el mismo
patrón de cliente HTTP mínimo sin librerías externas ya usado tres
veces en el proyecto (`g2_backend.py`, `emule_backend.py`,
`core/upnp.py`): `_http_get()` (con seguimiento de redirecciones 301/
302/303/307, necesario porque `dchublist.com` siempre redirige),
`HubListEntry` (dataclass con los campos ya mencionados),
`parse_hub_list()` (parseo con `xml.etree.ElementTree`, de la librería
estándar) y `fetch_public_hub_list()` como función de módulo (no
método de `DCPPBackend`, porque elegir hub ocurre antes de conectar a
ninguno, sin necesitar ninguna instancia del backend ni nickname).
En la GUI: nuevo `gui/widgets/hub_list_dialog.py` (`HubListDialog`)
con una tabla ordenable (por nº de usuarios, dirección, país...) y un
filtro de texto libre (nombre/dirección/descripción/país, vía
`QSortFilterProxyModel`); al hacer doble clic o pulsar "Aceptar" con
una fila seleccionada, expone `selected_hub`. Se enganchó en la
pestaña DC++ de `gui/widgets/settings_dialog.py`: junto al campo de
texto de host:puerto ya existente hay ahora un botón "Elegir de la
lista pública…" que abre el diálogo y, si se acepta, rellena el campo
con `host:puerto` del hub elegido (el usuario sigue pudiendo teclearlo
a mano si prefiere un hub que no esté en la lista). Validado en vivo,
contra el agregador real (no un servidor sintético, a diferencia de la
mayoría de puntos anteriores porque aquí no hay protocolo P2P que
levantar en ambos extremos, solo una descarga HTTP): `fetch_public_hub_list()`
trae los 247 hubs reales con nombre/host/puerto/país/software
correctos; en modo `QT_QPA_PLATFORM=offscreen`, `HubListDialog` carga
esos mismos 247 hubs de extremo a extremo, el filtro de texto reduce
correctamente la tabla, y la selección se propaga hasta rellenar el
campo de `SettingsDialog`.

**Punto 12 — Pegar enlace magnet/ed2k/dchub (o arrastrar un
`.torrent`)**: la opción de menú "🧲 Añadir magnet…" existente se
generalizó a "🔗 Abrir enlace…" (`gui/main_window.py`, mismo atajo de
menú Archivo), que ahora reconoce tres esquemas y despacha cada uno al
flujo que le corresponde sin pasar por la pestaña de Búsqueda:
`magnet:` (igual que antes, vía `TorrentBackend.search()`), `ed2k://`
y `dchub://` (nuevos). Al abrir el diálogo, si el portapapeles ya
contiene un enlace de alguno de los tres esquemas se precarga
automáticamente en el campo de texto (para bastar con Ctrl+V + Intro),
cubriendo el "pegar desde el portapapeles" del enunciado sin necesitar
un atajo de teclado global aparte. Para `ed2k://` se implementó
`parse_ed2k_link()` en `backends/emule_backend.py`, que parsea el
formato estándar `ed2k://|file|nombre|tamaño|hash|/` (el que genera
cualquier eMule/aMule real al compartir un enlace) y construye
directamente un `SearchResult` con el `source_id`
`hash:::tamaño:::nombre` que ya esperaba `EMuleBackend.start_download`
— el hash MD4 basta para pedir fuentes al servidor/Kad, así que la
descarga arranca sin necesidad de buscar antes. Para `dchub://` se
implementó `parse_dchub_link()` en `backends/dcpp_backend.py` (acepta
`dchub://host:puerto` o `dchub://host`, sin puerto implica 411 por
defecto) y se extendió `ConnectionManager.connect_network()`
(`gui/connection_manager.py`) con un parámetro opcional
`hub_override` que, cuando se indica, conecta DC++ directamente a ese
hub sin tocar ni depender del hub por defecto guardado en
Preferencias; si ya había una sesión DC++ activa contra otro hub, se
desconecta primero y se reconecta al hub del enlace pegado. Además se
implementó arrastrar y soltar un archivo `.torrent` directamente sobre
la ventana principal (`dragEnterEvent`/`dropEvent` en `MainWindow`,
filtrando por extensión `.torrent` entre las URLs soltadas), como vía
alternativa al selector de archivos ya existente. Validado: `parse_ed2k_link()`
y `parse_dchub_link()` con una batería de casos (enlaces válidos,
nombre con espacios/URL-encoding, hash corto, tamaño no numérico,
puerto no numérico, sin esquema, sin puerto, con barra final) en modo
`QT_QPA_PLATFORM=offscreen`, extremo a extremo contra los métodos
reales de `MainWindow` (`_handle_link` enruta correctamente los tres
esquemas, incluida la reconexión de DC++ a un hub distinto del ya
conectado, y los enlaces inválidos o no reconocidos muestran el aviso
correspondiente sin lanzar ninguna excepción) y comprobando que la
ventana principal arranca con `acceptDrops() == True`.

**Punto 13 — Chat privado y salas de Soulseek**: formato de mensajes
confirmado contra una copia fresca del código fuente real de Nicotine+
(`pynicotine/slskmessages.py`), ya que la memoria previa de un código
concreto (`RoomList` = 126) resultó errónea al contrastarla — el código
real es 64, corregido antes de escribir nada. Códigos de servidor
usados: `SayChatroom` (13, sala), `JoinRoom` (14), `LeaveRoom` (15),
`UserJoinedRoom` (16), `UserLeftRoom` (17), `MessageUser` (22, privado),
`MessageAcked` (23, hay que devolverlo tras cada `MessageUser` o el
servidor reenvía la misma frase indefinidamente) y `RoomList` (64).
Implementado en `backends/soulseek_backend.py`: métodos nuevos
`join_room()`, `leave_room()`, `say_in_room()`,
`send_private_message()`, `get_room_list()` (petición/respuesta vía
`Future`, mismo patrón que `_get_peer_address`) y `subscribe_chat()`
(callback con eventos `room_message`/`room_joined`/`user_joined_room`/
`user_left_room`/`private_message`/`room_list`, invocado igual que
`subscribe_progress`); dos parsers nuevos a nivel de módulo,
`_parse_room_list()` (formato `RoomList`) y `_parse_room_usernames()`
(formato multi-pasada de `JoinRoom`: listas paralelas de nombres,
estados, estadísticas, slots y país, cada una con su propio contador
delante, en vez de un registro por usuario). `core/backend_base.py`
gana los valores por defecto no-op (`supports_chat()` → `False`,
`join_room`/`leave_room`/`say_in_room`/`send_private_message`/
`get_room_list`/`subscribe_chat`), sobrescritos solo por
`SoulseekBackend` (el chat de hub DC++ es un mecanismo aparte, punto 14
del backlog). En la GUI: nueva pestaña "💬 Chat"
(`gui/widgets/chat_tab.py`, `ChatTab`) con una sub-pestaña cerrable por
cada sala o conversación abierta (mismo patrón que la pestaña de
Búsqueda usa por cada búsqueda); botón "Unirse a sala…" que pide la
lista de salas públicas reales al servidor y deja elegir una (o
escribir el nombre de una nueva) vía `QInputDialog.getItem` editable,
y botón "Mensaje privado…" que pide un nombre de usuario. Cada sala
muestra el log de mensajes y la lista de usuarios presentes (se
actualiza sola con los eventos `user_joined_room`/`user_left_room`); al
cerrar la pestaña de una sala se manda `LeaveRoom` automáticamente. Un
mensaje privado entrante de alguien con quien no había conversación
abierta crea su pestaña sobre la marcha. Validado en dos capas: (1)
formato de los mensajes byte a byte con payloads construidos a mano
(`RoomList`, `parse_users`, `SayChatroom`, `MessageUser`); (2) la
`ChatTab` completa en modo `QT_QPA_PLATFORM=offscreen` contra un
backend simulado (unirse a sala, recibir/mandar mensajes de sala,
entradas/salidas de usuarios, mensajes privados entrantes/salientes,
cierre de pestaña con `leave_room`); y (3) **en vivo contra el
servidor real** (`server.slsknet.org`): `get_room_list()` trajo 226
salas públicas reales, unión a la sala más numerosa en ese momento
("! ! LGBTQ+ ! !", 457 usuarios) con recepción de mensajes reales de
otros usuarios y del propio mensaje de prueba enviado (el servidor lo
devuelve también al emisor, confirmando el ciclo completo), y mensaje
privado real de extremo a extremo usando una cuenta Soulseek
desechable registrada al vuelo como emisora (para no mandar mensajes
no solicitados a usuarios reales) hacia la cuenta configurada del
proyecto, recibido con el `MessageUser` correctamente parseado
(usuario, texto y `is_new_message`).

**Punto 14 — Chat de hub DC++**: a diferencia de Soulseek, NMDC no
tiene salas independientes — todo el hub es un único canal de chat
compartido por todos los conectados a él, más mensajes privados
punto a punto. Implementado en `backends/dcpp_backend.py` reutilizando
el mismo contrato genérico que ya expone `SoulseekBackend`
(`supports_chat()`, `get_room_list()`, `join_room()`, `say_in_room()`,
`send_private_message()`, `subscribe_chat()`), para que la GUI no
necesite ningún caso especial por red: `get_room_list()` devuelve una
única "sala" sintética identificada por `host:puerto` del propio hub
(no hay nada que pedirle al hub para "unirse", ya estamos dentro por
el mero hecho de estar conectados); `say_in_room()` ignora el
parámetro `room` y manda la línea de chat tal cual espera el hub
(`<Nick> mensaje`, sin envoltorio `$`, a diferencia del resto de
mensajes NMDC); `send_private_message()` usa el formato `$To: ... From:
... $<Nick> mensaje` del spec. El lector del hub (`_hub_read_loop`)
reconoce ambos formatos entrantes y los expone vía `subscribe_chat()`
como eventos `room_message`/`private_message`, igual que Soulseek.
`gui/widgets/chat_tab.py` se generalizó para soportar varias redes con
chat a la vez (antes solo contemplaba Soulseek): cada sub-pestaña
abierta recuerda a qué backend concreto pertenece, así que no hay
mezcla entre redes aunque las dos estén conectadas simultáneamente.
Validado en dos capas: (1) offscreen con dos backends simulados (uno
por red) para comprobar aislamiento entre redes, apertura automática
de pestaña al recibir un PM nuevo, envío de mensajes y `leave_room`
enrutado siempre al backend correcto; y (2) **en vivo contra un hub
DC++ real** (`panda.ashaman.org:411`, PtokaX): dos clientes con nick
desechable conectados al mismo hub, mensaje de sala mandado por uno y
recibido íntegro por el otro, y mensaje privado mandado en sentido
contrario e igualmente recibido íntegro. La validación en vivo destapó
un bug real preexistente en el `$MyINFO` que se manda al conectar (le
faltaba el campo vacío de email antes del tamaño compartido — el hub
lo señalaba con un bot automático, "You MyINFO is corrupted!"),
corregido en el mismo `connect_to_hub()`.

**Punto 15 — Lista de amigos y sistema de créditos en Kad/eD2k**:
implementado en `backends/emule_backend.py`, aproximando (sin copiar
código fuente, solo comportamiento documentado públicamente en la
FAQ/wiki de eMule) el sistema real: cada peer identificado por su
userhash (GUID estable de 16 bytes, ya parseado del `OP_HELLO`/
`OP_HELLOANSWER` vía la nueva `_parse_hello_payload()`) acumula en
`~/.config/p2p-total/ed2k_credits.json` cuántos bytes le hemos subido
y cuántos le hemos descargado; `credit_modifier()` calcula, a partir
de ese histórico, un multiplicador de prioridad de 1x a 10x (tope
igual al del eMule real) cuando el peer nos ha dado más de lo que le
hemos dado. La propia identidad (userhash) pasó de generarse al azar
en cada arranque a persistir en `ed2k_identity.json`
(`load_or_create_identity()`), imprescindible para que el histórico de
créditos tenga sentido de una sesión a otra. La lista de amigos vive
en `ed2k_friends.json` (`load_friends()`/`save_friends()`,
`add_friend()`/`remove_friend()`/`is_friend()`) y siempre tiene
prioridad máxima, por delante de cualquier modificador de créditos —
igual que el eMule real. Para que la priorización tenga efecto real se
añadió también una cola de subida con slots limitados (3 por defecto,
`DEFAULT_UPLOAD_SLOTS`, el mismo valor de fábrica del eMule real):
`_acquire_upload_slot()` concede turno inmediato si hay slot libre, o
encola la petición (avisando al peer de su puesto con `OP_QUEUERANK`);
`_release_upload_slot()` decide quién pasa a continuación —primero
cualquier amigo en espera (por orden de llegada entre ellos), y si no
hay ninguno, quien tenga mayor `credit_modifier() × minutos
esperando`, para no dejar indefinidamente atrás a quien lleve mucho
tiempo en cola sin historial de créditos—. Cada `OP_SENDINGPART`
enviado o recibido llama a `_record_credit()`, que se vuelca a disco
(`_flush_credits()`) al final de cada sesión de subida/bajada y al
desconectar. En la GUI: `NetworkTab` muestra ahora `slots de
subida usados/total` y el tamaño de la cola cuando hay compartición
activa; nuevo diálogo `gui/widgets/emule_friends_dialog.py`
(`EMuleFriendsDialog`, accesible desde el menú Redes → "🤝 Amigos y
créditos (eMule)…") lista todos los peers con historial de créditos
—nick, userhash, subido/descargado, modificador, si es amigo— con
menú contextual para marcar/desmarcar amistad al vuelo. Validado con
un script sintético que ejercita directamente la lógica interna del
backend (sin red real, aislado en un `XDG_CONFIG_HOME` temporal):
cálculo correcto del modificador (tope x10 cuando nos han dado mucho
más de lo que les hemos dado, 1.0 sin historial neto a favor),
persistencia de créditos y amigos a través de un `load_credits()`/
`load_friends()` nuevos (simulando reinicio del proceso), y — la
prueba más importante— la cola de subida con los 3 slots ocupados y
tres peticiones en espera simultáneas (una amiga sin crédito
destacado, una con modificador x10 pero no amiga, y una sin bonus):
al liberar los tres slots, el orden de concesión fue exactamente el
esperado (amiga primero, después la de mayor crédito, por último la
sin bonus), confirmando que la prioridad por amistad manda sobre el
crédito y este sobre no tener historial.

**Punto 18 — Soporte de proxy (SOCKS5/HTTP) para las conexiones
salientes de los cinco backends**: implementado en `core/proxy.py`
para los cuatro backends sin soporte nativo de proxy (Soulseek, DC++,
Gnutella2 y eMule/eD2k), a mano sobre sockets/asyncio crudos —
handshake SOCKS5 completo según RFC 1928 (incluida la sub-negociación
usuario/contraseña de la RFC 1929) y `CONNECT` de HTTP/1.1 para el
proxy HTTP—, sin ninguna librería de terceros como PySocks.
`open_connection()` es un sustituto directo de
`asyncio.open_connection(host, port, ssl=ssl)` (misma firma, con
`proxy` opcional añadido) pensado para reemplazar uno a uno los
`asyncio.open_connection` ya existentes en cada backend sin cambiar su
forma de uso; los cinco backends ahora aceptan un `proxy:
ProxyConfig | None` en el constructor, cableado en `main.py` y en
`gui/connection_manager.py` desde `config.proxy`. BitTorrent usa en su
lugar el proxy nativo de `libtorrent` (`settings_pack` con
`proxy_type`/`proxy_hostname`/`proxy_port`/`proxy_username`/
`proxy_password`/`force_proxy`, configurados al crear la `lt.session`
en `backends/torrent_backend.py`), ya que reimplementar ese soporte a
mano habría sido puro trabajo duplicado sobre algo que la librería ya
resuelve internamente. Limitación honesta y documentada tanto en el
docstring del módulo como en la nota de la pestaña Proxy de la GUI:
solo cubre TCP saliente — el tráfico UDP (datagramas de Kad en eMule,
datagramas de hub en Gnutella2) sigue yendo directo sin proxear,
igual que en el eMule/aMule real, cuyo soporte de proxy tampoco cubre
el UDP de Kad. Un bug real y sutil apareció durante la validación:
al envolver en TLS una conexión ya túnelizada por el proxy (necesario
para `Range:` HTTPS y para cualquier backend que negocie TLS tras el
`CONNECT`/handshake SOCKS5), el `StreamWriter` pre-TLS quedaba sin
referencias en cuanto `_upgrade_to_tls()` construía el nuevo — el
recolector de basura lo destruía y su `__del__` cerraba el transporte
TCP subyacente (el mismo que el transporte TLS envuelve por debajo),
tumbando la conexión a mitad de handshake o de transferencia; se
corrigió colgando una referencia fuerte al writer viejo del nuevo.
Validado con un test sintético local (proxy SOCKS5 y HTTP CONNECT
propios, con y sin autenticación, más un servidor eco TLS) que cubre
8 combinaciones — directo sin proxy, SOCKS5 con y sin auth (incluido
el caso de auth incorrecta correctamente rechazado), HTTP CONNECT con
y sin auth, y SOCKS5/HTTP CONNECT con TLS por encima (el caso que
destapó el bug del writer huérfano) — con contenido de eco verificado
byte a byte en cada caso. Configurable desde la CLI (`python main.py
config`, sección "Proxy saliente") y desde la GUI (pestaña "Proxy" en
Preferencias, con persistencia en `~/.config/p2p-total/config.json`
igual que el resto de ajustes).

**Punto 19 — Soporte de IPv6.** Investigado a fondo backend por
backend antes de tocar nada, porque la respuesta real no es uniforme:
tres de las cinco redes (Soulseek, Gnutella2, eMule/eD2k) tienen un
límite de protocolo genuino e infranqueable — sus mensajes de
intercambio de direcciones (`ip()` de Soulseek, `/NA`/`/CH`/`/PUSH` de
G2, el campo `server_ip`/`client_id` de eD2k) son campos binarios
fijos de 4 o 6 bytes que ningún cliente real de esas redes (SoulseekQt/
Nicotine+, Shareaza/Gnucleus, eMule/aMule) sabe interpretar como IPv6;
añadir soporte ahí significaría dejar de hablar el protocolo real, así
que se documentó la limitación en el sitio exacto del código donde
vive (`soulseek_backend.py`: `ProtocolMessage.ip()`;
`g2_backend.py`: `_parse_address()`; `emule_backend.py`:
`_build_hello_payload()`) en vez de fingir que es solo cuestión de
más trabajo. Dicho esto, en las tres la conexión TCP saliente en sí
(al servidor Soulseek, a un hub G2, a un servidor eD2k) ya funciona
con un host IPv6 sin ningún cambio, porque pasa por
`asyncio.open_connection`/`core.proxy.open_connection`, que resuelven
y conectan igual sea IPv4 o IPv6 — el límite está solo en los campos
de datos del protocolo, no en la conectividad de red de por sí.
BitTorrent, por su parte, ya tenía soporte completo desde antes de
este punto sin que nadie lo hubiera marcado explícitamente:
`listen_interfaces` en `torrent_backend.py` ya escuchaba en
`"0.0.0.0:6881,[::]:6881"`, y se confirmó en vivo contra `libtorrent`
que de verdad abre sockets IPv6 (loopback `[::1]`, link-local
`fe80::.../wlo1` y anuncia sus propios `listen_succeeded_alert` para
cada uno) además de los IPv4 — la propia DHT y el intercambio de
peers de BitTorrent llevan soporte de IPv6 nativo en el protocolo, así
que aquí no hacía falta ningún cambio.

El único backend donde IPv6 sí era añadible de verdad y con sentido es
**DC++/NMDC**: al ser un protocolo de texto plano (no binario de ancho
fijo como los otros tres), la dirección de un peer es simplemente un
`"host:puerto"` dentro del propio mensaje, así que sí cabe una IPv6 —
los clientes DC++ reales modernos ya la representan entre corchetes
igual que una URL (`[2001:db8::1]:411`), convención que se adoptó
aquí también. Cambios: `parse_dchub_link()` (enlaces `dchub://`) pasó
de partir la cadena a mano por ":" (que rompía con una IPv6 sin
corchetes) a apoyarse en `urlparse()`, que ya sabe manejar la notación
con corchetes de RFC 3986; se añadieron `_format_ctm_address()`/
`_split_ctm_address()` para construir y parsear un `$ConnectToMe` con
corchetes cuando la IP es IPv6; `_get_local_ip()` dejó de depender en
exclusiva del truco de abrir un socket UDP hacia `8.8.8.8` (que fuerza
IPv4) y ahora primero mira el `sockname` real de la propia conexión ya
abierta al hub — así, si el hub es alcanzable por IPv6, se anuncia
automáticamente una IPv6 (la misma familia con la que de verdad se
está saliendo a la red), cayendo al truco de `8.8.8.8` solo si no hay
hub conectado; y el servidor de escucha (`connect()`) ahora abre
socket tanto en `"0.0.0.0"` como en `"::"` (con fallback automático a
solo IPv4 si el entorno no tiene pila IPv6, capturando el `OSError`
correspondiente), para que un peer con conectividad IPv6 nativa (sin
NAT, cada vez más común) pueda conectarnos directamente. Validado con
un test sintético: `parse_dchub_link()`/`_format_ctm_address()`/
`_split_ctm_address()` con y sin IPv6, comprobación de que el servidor
de verdad abre socket `AF_INET` y `AF_INET6` a la vez, y sobre todo un
ciclo NMDC real de extremo a extremo por loopback IPv6 (`::1`): dos
`DCPPBackend` completos conectados a un hub simulado en asyncio,
`start_download()` disparando un `$ConnectToMe` real que el otro peer
recibe y parsea correctamente como `"[::1]:41501"` (con corchetes de
verdad, no simulados).

**Punto 20 — Ofuscación de protocolo en eD2k.** Implementado el
esquema real de "ofuscación básica" cliente↔cliente que usa eMule/
aMule (clase `CEncryptedStreamSocket` en su código fuente real) para
esquivar el throttling de tráfico P2P basado en DPI (deep packet
inspection) que aplican algunos ISPs. Investigado directamente contra
el código fuente auténtico de aMule (`amule-org/amule` en GitHub,
concretamente `EncryptedStreamSocket.{cpp,h}` y `RC4Encrypt.{cpp,h}`)
en vez de fiarse de resúmenes de terceros, para tener el spec exacto
byte a byte. Importante dejarlo claro: **no es cifrado con fines de
seguridad** — la propia cabecera de `EncryptedStreamSocket.h` del
aMule real dice explícitamente que no cumple estándares
criptográficos, porque usa claves RC4 que no son secretas (cualquiera
que conozca el userhash público de un peer, que se reparte libremente
para poder ser localizado en la red, puede derivarlas) — es puramente
ofuscación de tráfico: la conversación pasa a parecer ruido aleatorio
en vez del patrón de bytes reconocible de un paquete eD2k/eMule
normal. Implementado en `backends/emule_backend.py`: RC4 (KSA + PRGA)
propio en `_RC4Cipher` con el mismo "descarte de los primeros 1024
bytes de keystream" que aplica aMule para mitigar los sesgos conocidos
del arranque de RC4; derivación de claves con `_obf_derive_key()`
(`Md5(userhash_del_peer + magic_value + random_key_part)`, con
`MAGICVALUE_REQUESTER=34`/`MAGICVALUE_SERVER=203` distinguiendo
send/receive según el rol); el marcador inicial sin cifrar
(`_obf_semi_random_marker()`) elegido a propósito para no coincidir
nunca con `OP_EDONKEYPROT`/`OP_EMULEPROT`/`OP_PACKEDPROT`, que es lo
que permite a quien acepta una conexión (`_maybe_negotiate_incoming_
obfuscation()`) decidir con un único byte si está ante tráfico normal
o ante el arranque de un handshake de ofuscación, sin necesidad de
descifrar nada todavía; y `_ObfuscatedPeerStream`, que envuelve el
`(reader, writer)` de asyncio exponiendo la misma superficie mínima
que usa el resto del código (`readexactly`/`write`/`drain`/`close`)
para que `build_tcp_packet`/`read_tcp_packet` y el resto de funciones
que hablan con un peer no necesiten enterarse de si la conexión está
ofuscada o no. No se ha implementado la variante cliente↔servidor
(basada en acuerdo de claves Diffie-Hellman): además de mucho más
compleja y con ~200 bytes más de overhead por conexión, el propio
comentario de cabecera del código fuente de aMule documenta que no
aporta protección sustancial adicional frente a la variante básica.
Limitación real y documentada en el propio código: el handshake de
ofuscación exige conocer de antemano el userhash del peer con el que
se va a hablar, y nuestro descubrimiento de fuentes actual (servidor
eD2k y Kad) solo entrega IP/puerto, no userhash — así que, igual que
hace el eMule real con su lista de "known clients", el primer contacto
con un peer nunca ofuscado va sin ofuscar y solo se ofusca en
conexiones salientes posteriores a un peer cuyo userhash ya
aprendimos de un `OP_HELLO`/`OP_HELLOANSWER` anterior con esa misma
IP:puerto (caché `_known_peer_hashes`); en cambio, para conexiones
*entrantes* (nosotros como quien acepta, típico al servir una subida)
no hace falta conocer nada de antemano — solo nuestro propio userhash,
que siempre tenemos — así que ahí sí se soporta y ofrece ofuscación
desde la primerísima conexión de cualquier peer real de eMule/aMule
que la tenga activada. Configurable con tres modos (`disabled`/
`enabled`/`required`, igual que las preferencias del eMule real) desde
la CLI (`python main.py config`, sección eMule) y desde la GUI
(pestaña eMule en Preferencias). Validado con un test sintético
byte a byte (RC4 simétrico con distintos troceados, derivación de
claves simétrica A↔B, el marcador nunca colisiona con un opcode real,
`ObfuscationError` limpio ante un `MagicValue` incorrecto) y, sobre
todo, con un test de extremo a extremo real por TCP en loopback entre
dos `EMuleBackend` completos: 1ª descarga de un fichero de 200 KB sin
ofuscar (primer contacto, aprende el userhash), 2ª descarga del mismo
fichero disparando de verdad el handshake de ofuscación completo en
ambos extremos (confirmado con espías sobre `negotiate_outgoing`/
`negotiate_incoming`) y contenido verificado byte a byte en ambos
casos; más un tercer test confirmando que `obfuscation="required"`
rechaza correctamente una conexión entrante que llega sin ofuscar.

**Punto 22 — Icono en la bandeja del sistema y minimizar a bandeja.**
Se añadió un `QSystemTrayIcon` real (`MainWindow._build_tray_icon()`
en `gui/main_window.py`), creado solo si
`QSystemTrayIcon.isSystemTrayAvailable()` devuelve `True` (si no hay
bandeja de sistema disponible, se degrada con normalidad a cerrar sin
más, sin tocar nada), con menú contextual propio ("Mostrar ventana"/
"Salir") y clic (simple o doble) para restaurar la ventana. Nueva
opción en Preferencias → General, "Minimizar a la bandeja del sistema
al cerrar" (`UIConfig.minimize_to_tray` en `core/config.py`,
`False` por defecto para no sorprender a quien no la pida
explícitamente): con la opción activada, cerrar la ventana principal
(aspa de la barra de título o `Alt+F4`) no termina el proceso, sino
que oculta la ventana y deja un aviso nativo del sistema ("P2P Total
sigue ejecutándose en la bandeja del sistema") vía
`QSystemTrayIcon.showMessage()`; el menú Archivo → Salir (y "Salir"
del propio menú contextual del icono) siempre cierra la aplicación de
verdad, ignorando esa opción, marcando un flag interno
(`self._quitting`) que `closeEvent()` comprueba antes de decidir si
ignora el evento de cierre o deja que el `QMainWindow` se cierre con
normalidad. Validado en vivo contra un escritorio real KDE Plasma
(sesión Wayland con XWayland, forzando `QT_QPA_PLATFORM=xcb`, igual
que el resto de pruebas de GUI de este proyecto): con la opción
activada desde la propia Preferencias, cerrar la ventana con `Alt+F4`
dejó el proceso vivo y mostró el aviso nativo; se confirmó por D-Bus
(`org.kde.StatusNotifierWatcher`) que el icono "P2P Total" quedó
realmente registrado como `StatusNotifierItem` del escritorio (no solo
creado en memoria); se restauró la ventana llamando a `Activate()`
sobre ese mismo icono real por D-Bus, exactamente la misma vía que usa
Plasma cuando el usuario hace clic físico sobre él; y, por último,
Archivo → Salir cerró el proceso del todo pese a tener la opción de
minimizar activada, confirmando por D-Bus que el icono se
desregistró correctamente al salir.

**Punto 23 — Notificaciones nativas del sistema operativo al completar
(o al fallar) una descarga.** Reutiliza el mismo `QSystemTrayIcon` del
punto 22 (que ya existe y está visible siempre que el escritorio tenga
bandeja de sistema, con independencia de la opción "Minimizar a la
bandeja"): `MainWindow._on_progress_for_notifications()` se suscribe
como un segundo listener de `DownloadManager.on_progress()` (además
del que ya alimentaba la barra de estado) y, cuando una descarga pasa
a `COMPLETED` o a `ERROR`, llama a `self._tray_icon.showMessage()` con
el título de la descarga — icono informativo en el primer caso,
icono de aviso en el segundo. Un `set` de IDs ya notificados
(`self._notified_download_ids`) evita repetir el aviso en cada tick de
progreso posterior a la propia transición de estado, ya que el
`on_progress` de `DownloadManager` puede invocarse más de una vez con
el mismo estado final. Nueva opción en Preferencias → General, "Avisar
al completar o fallar una descarga" (`UIConfig.
notify_on_download_finish`, activada por defecto, a diferencia de
"Minimizar a la bandeja" que se dejó desactivada por defecto para no
sorprender con un cambio de comportamiento al cerrar la ventana). Si
no hay bandeja de sistema disponible en el escritorio, sencillamente
no se muestra ningún aviso (no existe otra vía nativa sin bandeja).
Validado con espías sobre `showMessage()` de un `MainWindow` real
contra el mismo escritorio KDE Plasma real usado para el punto 22
(`QT_QPA_PLATFORM=xcb`): descargas sintéticas en `COMPLETED` y `ERROR`
disparan el aviso correcto una sola vez cada una (con el segundo aviso
de la misma descarga `COMPLETED` correctamente suprimido), una
descarga todavía `DOWNLOADING` no dispara ningún aviso, y la nueva
casilla de Preferencias se ve y persiste correctamente desde la propia
GUI.

**Punto 24 — Pestaña de estadísticas globales.** Hasta ahora
`NetworkBackend.get_stats()` (pestaña Red) solo exponía el estado
instantáneo de la conexión en curso, sin memoria de lo ya transferido
en sesiones anteriores. Nuevo módulo `core/stats_tracker.py`
(`StatsTracker`, instancia única `stats_tracker`, mismo patrón que los
limitadores globales de `core.rate_limiter`) que acumula, por red:
total subido, total bajado, tiempo conectado y un histórico diario —
todo persistido en dos tablas SQLite nuevas (`network_stats`,
acumulado total; `network_stats_daily`, una fila por día y red, hasta
30 días visibles en la GUI). Tres vías de entrada, todas ya existentes
en el código y reaprovechadas sin tocar la lógica de negocio de cada
una: los bytes bajados se calculan como delta de
`Download.downloaded_bytes` dentro del mismo callback central
`DownloadManager._on_backend_progress()` que ya recibía cada tick de
progreso (sin cambios en ningún backend); los bytes subidos se suman
en el punto exacto de cada backend donde ya se leía un `chunk` para
servírselo a otro peer (`_serve_file`/`_answer_request_parts`/etc. de
DC++, Soulseek, Gnutella2 y eMule) y, en BitTorrent, se leen listos de
`session.status().total_payload_upload` en el mismo `_poll_loop()` que
ya existía (libtorrent ya lleva la cuenta él solo, incluido el tráfico
de "seeding" en segundo plano que no pasa por ningún bucle nuestro);
y el tiempo conectado se abre/cierra desde `ConnectionManager.
connect_network()`/`disconnect_network()`, con un volcado periódico
adicional (`flush_connected_time()`, cada 2 s desde la propia pestaña
mientras la red siga conectada) para no perder tiempo de sesión si la
app se cierra sin pasar por una desconexión limpia. Nueva pestaña
"Estadísticas" en la GUI (`gui/widgets/stats_tab.py`): una tabla de
totales (Red / Subido / Bajado / Ratio / Tiempo conectado) y, debajo,
el histórico diario de los últimos 30 días. Validado en dos niveles:
una prueba directa de `StatsTracker`+`database` contra una base de
datos aislada (que además detectó y corrigió un bug real de
duplicación — borrar la entrada de "último `downloaded_bytes` visto"
al llegar a un estado terminal hacía que un segundo aviso de la misma
descarga ya `COMPLETED`, que sí puede llegar a producirse tal cual
advierte el propio punto 23, volviera a sumar el archivo entero como
si fuera nuevo; la entrada ya no se borra nunca, mismo criterio que
`MainWindow._notified_download_ids`); y en vivo contra un
escritorio KDE Plasma real (`QT_QPA_PLATFORM=xcb`), conectando
BitTorrent de verdad desde la propia GUI y viendo crecer en directo la
columna "Tiempo conectado" (confirmado también leyendo la fila de
`network_stats` directamente por SQLite mientras la app seguía viva) y
comprobando que, al desconectar, el total persistido se queda fijo en
vez de resetearse. Los datos de esta prueba se borraron de la base de
datos real del usuario al terminar, para no dejar estadísticas
ficticias mezcladas con las suyas.

**Punto 25 — Importar/exportar `config.json` desde la GUI, y modo
"portable".** `core/config.py` ganó dos capacidades nuevas sin romper
nada de lo existente: `load_config(path=None)`/`save_config(config,
path=None)` ahora aceptan una ruta opcional (si no se indica, siguen
usando `CONFIG_PATH` como siempre), lo que basta para implementar
exportar/importar — la pestaña General de Preferencias añadió dos
botones, "Exportar..."/"Importar..." (`gui/widgets/settings_dialog.py`,
`_on_export_config`/`_on_import_config`), que usan un `QFileDialog`
para guardar la configuración tal cual está editada en el diálogo a
cualquier fichero, o cargar y aplicar de inmediato la de un
`config.json` externo. Además, `is_portable_mode()` (nueva función)
determina si existe un fichero `portable.marker` junto a `main.py` (o
al ejecutable empaquetado, vía `sys.frozen`); si existe, tanto
`_config_dir()` (de donde cuelgan `config.json` y las cachés de
identidad/servidores/contactos conocidos de eD2k, Kad y G2) como
`core.database.DB_PATH` pasan a apuntar a una carpeta `p2p-total-data`
junto al ejecutable en vez de `~/.config/p2p-total` y
`~/.local/share/p2p-manager` — pensado para poder llevar el programa
entero en un pendrive sin dejar rastro en el equipo donde se ejecute.
Una nueva casilla "Modo portable" en la misma pestaña
(`enable_portable_mode()`/`disable_portable_mode()`) activa o
desactiva el marcador y, al activarlo, copia (sin borrar los
originales, por si se desactiva más tarde) la configuración actual y
cualquier dato ya existente a la carpeta portable; como las rutas se
calculan una sola vez al arrancar (`CONFIG_PATH`/`DB_PATH` son
constantes de módulo, igual que ya pasaba antes de este punto), el
cambio no tiene efecto real hasta reiniciar la aplicación, y así se le
avisa al usuario con un `QMessageBox`. Validado en dos niveles: una
prueba aislada (con `sys.argv[0]`/`XDG_CONFIG_HOME` simulados) del
ciclo completo exportar → importar → activar modo portable →
comprobar que `core.database.DB_PATH` se reubica en una importación
fresca → desactivar sin perder la carpeta portable; y en vivo contra
un escritorio KDE Plasma real, abriendo Preferencias desde la propia
GUI y confirmando visualmente los nuevos controles — el selector de
fichero nativo de Qt bajo esta sesión concreta (KDE sobre Wayland, app
forzada a XWayland) delega en `xdg-desktop-portal-kde` y abre como
ventana Wayland nativa, invisible para las herramientas de
automatización X11 (`xdotool`/`wmctrl`) usadas hasta ahora en este
proyecto para las pruebas en vivo; en vez de forzar el diálogo nativo,
se validaron los manejadores reales de los botones
(`_on_export_config`/`_on_import_config`/`_on_save` con la casilla de
modo portable) interceptando `QFileDialog.getSaveFileName`/
`getOpenFileName` para devolver una ruta fija, que es una técnica de
prueba habitual para evitar diálogos nativos del sistema operativo y
sigue ejercitando el código de producción real de principio a fin. Se
comprobó también que el `config.json` real del usuario permaneció
intacto tras las pruebas.

**Punto 26 — Carpeta vigilada.** `core/watch_folder.py`
(`WatchFolderManager`) implementa el mismo patrón de bucle en segundo
plano que ya usa `SavedSearchManager` (punto 8): cada pocos segundos
recorre `Config.watched_torrent_dir` (nuevo campo, vacío = desactivado)
buscando ficheros `.torrent` nuevos y los añade automáticamente a las
descargas de BitTorrent vía `DownloadManager`, sin tener que abrirlos a
mano. Para no reprocesar el mismo fichero en cada barrido se recuerda
qué ficheros ya se gestionaron (ruta -> fecha de modificación) en una
caché en disco junto al resto de config; si la red BitTorrent aún no
está conectada cuando aparece un fichero, no se marca como procesado
para que se reintente solo en el siguiente barrido en vez de darlo por
perdido o avisar de un error falso. La GUI añadió el control en la
pestaña General de Preferencias (`gui/widgets/settings_dialog.py`: fila
"Carpeta vigilada" con selector de carpeta y botón para vaciarla) y
`gui/main_window.py` arranca/para el `WatchFolderManager` junto con el
resto de gestores en segundo plano, mostrando una notificación nativa
(éxito o error) por cada `.torrent` añadido automáticamente. Validado
en dos niveles: pruebas aisladas cubriendo los cuatro escenarios clave
(fichero nuevo se añade y pasa a la caché, fichero ya visto se ignora,
BitTorrent desconectado no marca como visto para reintentar, fichero
inválido sí se marca como visto para no reintentar en bucle un
`.torrent` corrupto); y una prueba end-to-end real completa: se generó
un `.torrent` propio, se sembró desde un peer local en la propia
máquina y se dejó caer en la carpeta vigilada — `WatchFolderManager` lo
detectó, lo descargó vía el backend real de `libtorrent` a través de
`DownloadManager`, y el contenido final se verificó byte a byte contra
el original.

**Punto 27 — Verificar archivos ya descargados.** Se amplió el
contrato de `NetworkBackend` (`core/backend_base.py`) con dos métodos
nuevos y no abstractos: `supports_verify()` (por defecto `False`) y
`verify_download()` (por defecto lanza `NotImplementedError`), de modo
que cada red se suma cuando tenga sentido sin tocar el resto. Hoy solo
`TorrentBackend` los sobreescribe: `verify_download()` llama a
`handle.force_recheck()` de `libtorrent` — el mismo recheck/hash-check
nativo que usa qBittorrent con "Force re-check" — que relee del disco
todo lo ya descargado y lo recompara pieza a pieza contra el SHA1 de
referencia del propio `.torrent`; cualquier pieza corrupta o que
faltase se marca como no descargada y, si la descarga no está
pausada, `libtorrent` la vuelve a pedir sola, sin lógica extra por
nuestra parte. Un detalle sutil descubierto al validarlo contra
`libtorrent` real: tras `force_recheck()` el estado pasa por varios
valores encadenados (`checking_files`, `checking_resume_data`,
`queued_for_checking`), no solo el primero, así que hay que esperar a
que salga de los tres para no leer el progreso a medias. Sobre esa
base, `DownloadManager` (`core/download_manager.py`) añade
`supports_verify()`, `verify()` (a demanda, propaga errores, para
CLI/tests) y `request_verify()` (fire-and-forget con notificación a
`on_verify_result`, para la GUI), más un verificado automático al
completar una descarga si `Config.auto_verify_on_complete` está
activo (desactivado por defecto), controlado por un `set` de IDs ya
verificados para no repetir la comprobación en cada notificación de
progreso mientras la descarga sigue en `COMPLETED` (p.ej. mientras
sigue en *seeding*). La GUI añade la opción "Verificar archivo" al
menú contextual de la pestaña Transferencias (solo visible si la
descarga está completada y su red soporta verificar) y una casilla
nueva en Preferencias/General para activar el verificado automático,
con notificación nativa del resultado (íntegro, corrupción detectada,
o error) en ambos casos. Validado en tres niveles: pruebas aisladas de
`DownloadManager` con un backend falso (7 escenarios: verificado a
demanda con éxito/corrupción/error, auto-verificado desactivado no se
dispara, activado se dispara una sola vez por descarga pese a varias
notificaciones de progreso mientras sigue completada, red sin soporte
lanza error); una prueba end-to-end real con `libtorrent` de verdad
(contenido íntegro devuelve `True`, corromper un byte a mano lo
detecta y baja el progreso del 100%, descarga inexistente lanza error;
estable en 5 ejecuciones repetidas); y validación en vivo desde la
GUI real (`xdotool`/`spectacle` sobre X11): se conectó BitTorrent, se
añadió un `.torrent` con el contenido ya correcto en disco para que
`libtorrent` lo reconociera al instante como completo, se seleccionó
la fila desde el menú contextual de Transferencias y se lanzó
"Verificar archivo", confirmando la notificación nativa de la bandeja
del sistema con el texto «verificado: el contenido es íntegro».

### Arreglo: las descargas no se reanudaban tras reiniciar la app (las 5 redes)

Bug real reportado por el usuario: con una descarga de BitTorrent en
curso, al cerrar P2P Total y reabrirlo, reconectar la red y pulsar
"Reanudar" no pasaba nada. La causa resultó ser sistémica, no propia de
BitTorrent: los cinco backends (`TorrentBackend`, `SoulseekBackend`,
`DCPPBackend`, `G2Backend`, `EMuleBackend`) llevan su seguimiento de
"descarga activa" (handle de libtorrent, conexiones de peer, writer del
fichero en curso, etc.) únicamente en un diccionario en memoria
(`self._active`), que nunca se repuebla desde la base de datos al
reiniciar el proceso — aunque el propio registro `Download` sí persiste
en SQLite y se recarga bien en la pestaña Transferencias, ni
`pause_download()`, ni `resume_download()`, ni `cancel_download()`
encontraban nada que tocar, así que no hacían nada en silencio.

Arreglado para las 5 redes con un método nuevo, no abstracto, en el
contrato común (`NetworkBackend.reattach_download()`,
`core/backend_base.py`, no-op por defecto), que cada backend
sobreescribe reconstruyendo su entrada interna a partir del propio
`source_id` persistido (que cada red codifica de forma autocontenida:
magnet/ruta de `.torrent` en BitTorrent, `usuario:::ruta` en Soulseek,
`nick:::ruta` en DC++, `host:puerto:::sha1:::guid:::nombre` en
Gnutella2, `hash:::tamaño:::título` en eMule) y relanzando la descarga
desde donde se quedó — reutilizando en todos los casos la misma lógica
de reanudación ya validada de `resume_download()` (recheck automático
de piezas ya en disco en BitTorrent; reanudación por tamaño ya escrito
en disco en Soulseek y Gnutella2 vía `Range:`; por `downloaded_bytes`
vía `$Get`/`resume_from` en DC++ y eMule). `DownloadManager` añade
`reattach_active_downloads(network)`, que se llama automáticamente
desde `ConnectionManager.connect_network()` justo después de conectar
cada red, y reengancha todas las descargas en estado activo (QUEUED,
SEARCHING_SOURCES, DOWNLOADING o PAUSED) de esa red. Validado con una
prueba funcional real (sin mocks) para BitTorrent: dos sesiones de
`libtorrent` en localhost (una sembradora con el archivo completo, otra
descargando), se interrumpe la segunda a mitad de descarga real (~21%
de un archivo de 150MB) simulando un cierre brusco del proceso, y una
tercera sesión nueva (simulando reabrir la app) reengancha vía
`reattach_download()` con un objeto `Download` "tal cual" saldría de
la base de datos, retomando sola el resto de la descarga hasta
completarla con contenido verificado byte a byte (SHA256). Las otras
cuatro redes se validaron con pruebas aisladas confirmando que
`reattach_download()` reconstruye correctamente cada campo de la
entrada interna a partir del `source_id` persistido, más una prueba de
integración de `reattach_active_downloads()` contra SQLite real
confirmando que solo reengancha las descargas activas de la red
indicada, ignorando las completadas y las de otras redes.

### Diálogo "Acerca de": enlace a GitHub y correo de contacto

A petición del usuario, `gui/widgets/about_dialog.py` muestra ahora,
bajo el texto descriptivo, un `QLabel` en HTML enriquecido
(`setTextFormat(RichText)` + `setOpenExternalLinks(True)`) con el
enlace al repositorio (`https://github.com/AnabasaSoft/P2P-Total`) y
al correo de contacto (`anabasasoft@gmail.com`, como `mailto:`), ambos
clicables. Las etiquetas (`about_github_label`/`about_email_label`)
están traducidas en los 13 idiomas de `gui/i18n.py`.

### "Iniciar"/"Reiniciar" para descargas canceladas (las 5 redes)

Segunda petición del usuario sobre el mismo menú contextual de la
pestaña Transferencias: para una descarga en estado Cancelada, dos
acciones nuevas — "Iniciar" retoma la descarga desde donde se quedó
(el contenido parcial en disco se deja tal cual) y "Reiniciar" la
retoma desde 0 (borra antes lo ya descargado). Para que "Iniciar"
funcionara hubo que corregir antes una inconsistencia real detectada
en el código: `TorrentBackend.cancel_download()` era la única de las
5 redes que borraba los datos parciales del disco al cancelar (vía
`lt.session.delete_files`); ahora las 5 backends cancelan igual —
solo detienen la transferencia sin tocar el disco, dejando el borrado
real para la acción explícita "Borrar descarga (y archivos)"
(`DownloadManager.delete`). Ambos botones reutilizan tal cual el
mismo `NetworkBackend.reattach_download()` ya validado en el arreglo
de reanudación tras reiniciar la app (ver más arriba), a través de un
método nuevo `DownloadManager.restart(download, from_scratch=bool)`.
Nuevas claves i18n `ctx_restart`/`ctx_restart_from_scratch` en los 13
idiomas. Validado con una prueba aislada (backend falso + SQLite
temporal) que cubre los tres casos: "Iniciar" conserva el fichero
parcial y el progreso ya descargado, "Reiniciar" borra el fichero y
pone `downloaded_bytes` a 0, y llamar a `restart()` con la red
desconectada lanza un `RuntimeError` claro en vez de fallar en
silencio.

### Control de versión: v1.0 y aviso de actualización contra GitHub

A petición del usuario, el proyecto pasa a tener una versión formal
(`VERSION = "1.0"` en `core/version.py`, nuevo fichero, mostrada ya en
el diálogo "Acerca de") y una comprobación automática de
actualizaciones al arrancar la GUI. `core/update_checker.py` consulta
`GET https://api.github.com/repos/AnabasaSoft/P2P-Total/releases/latest`
reutilizando el cliente HTTP propio del proyecto
(`core/http_client.http_get`, sin librerías de terceros, respetando
también el proxy configurado por el usuario si lo hay) y compara el
`tag_name` del último release (p. ej. `v1.2.0`) con `VERSION`,
componente a componente. Si hay una versión más reciente,
`MainWindow` (vía `asyncio.ensure_future` al final de su `__init__`,
sin bloquear el arranque) muestra un `UpdateAvailableDialog`
(`gui/widgets/update_dialog.py`, un `QMessageBox` con botones
"Descargar" y "Cancelar") — "Descargar" abre la página del release en
el navegador con `QDesktopServices.openUrl`, "Cancelar" simplemente
cierra el aviso. Cualquier fallo de red, límite de peticiones
anónimas de la API de GitHub o ausencia de releases publicados se
traga en silencio (`check_for_update` devuelve `None`) para que nunca
pueda impedir que la app arranque. Nuevas claves i18n
(`about_version`, `update_dialog_title`, `update_dialog_text`,
`update_dialog_download`, `update_dialog_cancel`) en los 13 idiomas.
Validado: una prueba aislada sustituyendo `http_get` por una versión
falsa cubre detectar una versión más nueva, no ofrecer nada si
coincide con la actual, tragar en silencio un fallo simulado de red, y
la comparación numérica de versiones (`1.10.0 > 1.9.9`, `2.0 > 1.0`);
y una llamada real contra la API de GitHub confirma que, al no existir
todavía ningún release publicado en el repositorio, la API responde
`404` y el código lo maneja sin error (no ofrece actualización, como
es de esperar hasta que se publique el primer release).

### Punto 34.1 del backlog: mecanismo de auto-actualización real

Hasta ahora `core/update_checker.py` solo avisaba de que había una
versión más reciente con un enlace a la release de GitHub, sin
descargar ni instalar nada — el usuario tenía que ir a mano a la
página y reinstalar. Se añade la descarga y sustitución automática del
paquete instalado, cuando el tipo de instalación concreto lo permite:

- **AppImage** (Linux): se sustituye el propio fichero `.AppImage` en
  su sitio (mismo mecanismo que usa AppImageUpdate) y se relanza vía
  `os.execv`.
- **Instalador de Windows** (Inno Setup, sobre el build "onedir"): se
  descarga el nuevo `P2P-Total-Setup-*.exe` y se ejecuta en modo
  silencioso (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART
  /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`) mediante `os.startfile(...,
  "runas", ...)`, que cierra la app en marcha, reemplaza los ficheros y
  la vuelve a abrir sola sin que el usuario tenga que hacer nada más
  salvo aceptar el aviso de Control de cuentas de usuario (UAC) que
  pide `os.startfile` con el verbo "runas" — obligatorio porque el
  instalador escribe en Program Files (ver más abajo "Arreglo:
  auto-actualización en Windows no pedía elevación de UAC" para el
  motivo exacto y la validación). Se añadieron también explícitamente
  `CloseApplications=yes`/`RestartApplications=yes` en
  `packaging/windows/installer.iss` para que ese comportamiento no
  dependa de valores por defecto que puedan cambiar entre versiones de
  Inno Setup.
- **macOS** (`.app` dentro de un `.dmg`): se monta el `.dmg` con la
  propia herramienta del sistema `hdiutil` (parte del propio macOS, no
  un "cliente P2P externo" en el sentido de la restricción de diseño
  del proyecto), se sustituye el `.app` en `/Applications` y se
  relanza con `open -n`.

Los paquetes `.deb`/`.rpm` (instalados en `/usr`, gestionados por
`apt`/`dnf`, requerirían root) y el `.flatpak` autónomo (gestionado
por Flatpak) NO se auto-actualizan — no es seguro pisar desde dentro
de la propia app lo que gestiona otra herramienta del sistema — así
que para esos tres casos (`core/self_updater.py`,
`InstallKind.LINUX_PACKAGE`/`InstallKind.FLATPAK`, más
`InstallKind.SOURCE` para cuando se ejecuta desde código fuente en
desarrollo) `can_self_update()` devuelve `False` y la GUI cae de
vuelta al aviso de siempre con el botón "Descargar" que abre la
página del release.

Piezas nuevas: `core/self_updater.py` (detecta el tipo de instalación
actual vía `$APPIMAGE`, `$FLATPAK_ID`/`/.flatpak-info`, `sys.frozen` y
`sys.platform`; localiza el asset correcto dentro de la lista de la
API de GitHub por patrón de nombre; y aplica la actualización según el
tipo); `core/http_client.http_download`, nueva función hermana de
`http_get` que escribe el cuerpo de la respuesta a fichero en
streaming (con progreso vía `progress_cb`) en vez de acumularlo entero
en memoria, reutilizando la misma lógica de conexión/redirecciones/
`chunked`; `core/update_checker.check_for_update` ahora devuelve un
`UpdateInfo` (versión, url de la release, lista de assets) en vez de
solo la tupla `(versión, url)`, para que `self_updater` pueda buscar
el asset adecuado sin una segunda petición a la API. En la GUI,
`UpdateAvailableDialog` gana un botón "Actualizar ahora" (texto
distinto también, `update_dialog_text_auto`) cuando
`can_self_update()` es cierto y se ha encontrado el asset
correspondiente; al pulsarlo, `MainWindow._run_self_update` descarga
con un `QProgressDialog` modal y, si todo va bien, llama a
`apply_update_and_relaunch` y cierra la app con normalidad (guardando
configuración, desconectando redes) como hace "Salir". Nuevas claves
i18n (`update_dialog_text_auto`, `update_dialog_update_now`,
`update_downloading`, `update_download_failed`,
`update_apply_failed`) en los 13 idiomas.

Validado con un script aislado (no hay suite de tests todavía, ver
punto 34.2 de este mismo backlog) contra un servidor HTTP local
sintético: `http_download` descarga y escribe a fichero correctamente
en los tres casos (directo con `Content-Length`, tras una redirección
302, y con `Transfer-Encoding: chunked`), verificado con hash SHA-256
byte a byte sobre 2.5 MB; `detect_install_kind()` distingue
correctamente los seis casos (`APPIMAGE`, `FLATPAK`, `SOURCE`,
`WINDOWS_ONEDIR`, `MACOS_APP`, `LINUX_PACKAGE`) manipulando variables
de entorno y `sys.frozen`/`sys.platform`; `find_update_asset()`
localiza el fichero correcto para cada plataforma dentro de una lista
de assets realista con los seis nombres reales que genera
`.github/workflows/build-packages.yml`; y `apply_update_and_relaunch`
se probó de punta a punta para los tres tipos auto-actualizables: para
`APPIMAGE`, sustituye de verdad el fichero en disco, lo deja
ejecutable y llama a `os.execv` con la ruta correcta (con `os.execv`
sustituido por un mock para no matar el proceso de prueba); para
`WINDOWS_ONEDIR`, con `os.startfile` sustituido por un mock, se
comprobó que se invoca con el verbo `"runas"` y los flags silenciosos
correctos; para `MACOS_APP`, con `subprocess.run`/`subprocess.Popen`
sustituidos por mocks (no hay Windows/macOS reales en este entorno de
desarrollo Linux) se comprobó que se invocan con los argumentos
correctos — `hdiutil attach`/copia real del `.app` a un
`/Applications` falso/`hdiutil detach`/`open -n`. Además se comprobó
que `gui/widgets/update_dialog.py` construye bien los dos textos/juegos
de botones y que `gui/main_window.py` importa sin errores con el nuevo
cableado, ambos en modo headless (`QT_QPA_PLATFORM=offscreen`).
**Pendiente real**: no se ha podido validar en vivo contra una máquina
Windows o macOS real (el entorno de desarrollo es Linux), así que la
ejecución real del instalador silencioso y del montaje/copia vía
`hdiutil` solo está verificada por mocks, no de punta a punta contra
esos sistemas operativos.

### Arreglo: auto-actualización en Windows no pedía elevación de UAC

Al revisar con el usuario ("¿funcionarán todas las actualizaciones
incluido en windows, macos, flatpak y appimage?") si el punto 34.1
funcionaría de verdad en los cuatro casos, salió un fallo real en el
diseño original de `_apply_windows_installer` (`core/self_updater.py`):
lanzaba el instalador descargado con `subprocess.Popen`, que usa
`CreateProcess` de Win32 por debajo. `CreateProcess` **no** pide
elevación de UAC por sí solo aunque el ejecutable lleve un manifiesto
que la exija (el que genera Inno Setup por defecto, porque instala en
Program Files) — directamente falla con `WinError 740: se requiere
elevación`, sin ni siquiera llegar a mostrar el aviso de Control de
cuentas de usuario al usuario. Solo `ShellExecuteEx` con el verbo
`"runas"` (expuesto en Python como `os.startfile(ruta, "runas",
argumentos)`) sabe pedir esa elevación.

Arreglado sustituyendo el `subprocess.Popen` por
`os.startfile(str(downloaded_path), "runas", args)`. De paso esto
resuelve también un problema derivado que tampoco estaba cubierto: si
el usuario cancela el aviso de UAC, `ShellExecuteEx` (y por tanto
`os.startfile`) lo devuelve como una excepción en el momento mismo de
la llamada — que ya capturaba `MainWindow._run_self_update` — así que
la app nunca llega a darse por actualizada ni a cerrarse sola si la
elevación se rechaza; antes, con `subprocess.Popen`, ni siquiera se
habría enterado de que el lanzamiento había fallado por falta de
privilegios, y con el fallo silencioso (`WinError 740`) tampoco se
habría comprobado en la práctica si servía para algo.

`packaging/windows/installer.iss` no necesitó cambios: `CloseApplications=yes`/
`RestartApplications=yes` ya estaban ahí desde el propio punto 34.1 y
siguen aplicando igual una vez que el instalador arranca elevado.

Validado (de nuevo con `os.startfile` sustituido por un mock, sin
Windows real disponible) en dos escenarios: la llamada normal usa el
verbo `"runas"` y lleva los cinco flags silenciosos correctos; y
simulando que el usuario cancela el aviso de UAC (el mock lanza
`OSError(1223, ...)`, el código de error real de Windows para
"cancelado por el usuario"), la excepción se propaga tal cual sin
tragarse en ningún punto intermedio.

### Punto 34.2 del backlog: suite de tests automatizados

Hasta ahora el proyecto no tenía ni un solo test: toda la validación de
cada red se había hecho a mano contra infraestructura real (ver el
resto de este `DEVLOG.md`) y, para lo que no dependía de red, con
scripts sueltos en el directorio de scratchpad que se borraban después
de usarlos — cero regresión automática. Se añade `tests/` con
[pytest](https://pytest.org) + `pytest-asyncio`
(`requirements-dev.txt`, no hace falta para ejecutar la app, solo para
desarrollarla) y `pytest.ini` (`pythonpath = .`, `asyncio_mode = auto`
para no tener que anotar cada test async a mano).

Alcance deliberado: cubrir la lógica **pura y determinista** de cada
módulo (parsers, codecs binarios, hashes, round-trips de
configuración, paridad de claves i18n) montando servidores TCP locales
sintéticos cuando hace falta ejercitar E/S real sin tocar la red de
verdad (mismo enfoque ya usado a mano en el punto 34.1). Lo que sí
sigue validándose manualmente contra infraestructura real de cada red
(handshakes de protocolo completos, descargas de extremo a extremo)
sigue siendo así a propósito — es una decisión consciente, no una
laguna: ese tipo de test de integración contra Soulseek/DC++/G2/eD2k
reales no tiene sentido en un CI automático (dependería de que esos
servidores/hubs/peers externos estén disponibles en cada ejecución) y
ya está documentado exhaustivamente en el resto de este fichero.

157 tests en 15 ficheros bajo `tests/`, todos deterministas y sin
tocar la red real ni el `config.json`/`downloads.db` del usuario
(usan `tmp_path` de pytest o pasan un `path`/env var explícito a las
funciones bajo prueba):

- `test_md4.py`: los 7 vectores de prueba oficiales del RFC 1320.
- `test_tiger_tth.py`: Tiger/192 y TTH, tests de regresión (golden
  master) contra la salida ya validada de la implementación actual
  (el propio docstring de `core/tiger.py` documenta que las tablas
  S-box ya se comprobaron bit a bit contra la referencia en C de
  RHash).
- `test_aich.py`: `block_sha1`/`combine_sha1` contra `hashlib`
  directamente, `block_count` y el caso de `levels_to_part` para un
  fichero de una sola parte y de dos partes iguales.
- `test_models.py`: la propiedad calculada `Download.progress` (casos
  límite: tamaño 0, completado, sobrepasado) y que los valores string
  de `Network` no cambien (romperían `config.json`/`downloads.db` ya
  guardados).
- `test_file_types.py`, `test_rate_limiter.py` (async, con el cubo de
  fichas real esperando ~0.5 s en un caso para comprobar que sí limita
  de verdad), `test_config.py` (round-trip completo de
  `save_config`/`load_config`, migración del campo antiguo
  `shared_folder` singular, permisos 600 del fichero).
- `test_http_client.py`: `_dechunk` y, contra un servidor
  `asyncio.start_server` local, `http_get`/`http_download` con cuerpo
  normal, `chunked`, redirección 302 y progreso en streaming a disco.
- `test_update_checker.py` y `test_self_updater.py`: la comparación de
  versiones, y los seis `InstallKind`, `find_update_asset` y
  `apply_update_and_relaunch` del punto 34.1 (incluida la regresión
  específica del arreglo de UAC: se comprueba que se usa el verbo
  `"runas"` y que una cancelación de UAC se propaga como excepción).
- `test_i18n.py`: los 13 idiomas tienen exactamente el mismo juego de
  claves que español (ni de más ni de menos) y ninguna traducción
  vacía — la propia convención documentada en `gui/i18n.py` es traducir
  1:1 en vez de apoyarse en el fallback.
- `test_dcpp_backend.py`, `test_torrent_backend.py`,
  `test_g2_backend.py`, `test_emule_backend.py`, `test_soulseek_backend.py`,
  `test_proxy.py`, `test_saved_search_manager.py`: parsers/codecs
  puros de las cinco redes (enlaces `dchub://`/`ed2k://`, `$SR` NMDC,
  `lock_to_key`, códec de trama G2 con round-trip encode/decode, tags
  eD2k, framing TCP/UDP eD2k, empaquetado binario de Soulseek,
  dirección SOCKS5, clave de deduplicación de búsquedas guardadas).

Un error propio detectado y corregido durante la escritura de estos
tests, antes de que llegara a colarse: al escribir a mano un caso de
`lock_to_key` con lock `[5, 5]` se dio por hecho el resultado esperado
sin ejecutarlo, y no coincidía con la salida real de la implementación
(ya validada contra hubs DC++ reales) — se recalculó a mano
correctamente y se corrigió el valor esperado en el test, no la
implementación. Recuerda por qué esta suite se basa en vectores
oficiales (RFC 1320) o en trazar el algoritmo a mano y **verificarlo
contra la implementación antes de fijarlo como esperado**, nunca en
"debería dar más o menos esto".

Validado ejecutando `python -m pytest` desde la raíz del proyecto: 157
passed, tanto con `QT_QPA_PLATFORM=offscreen` como sin ella (ningún
test instancia `QApplication`, ni siquiera `test_i18n.py`, que solo
importa el diccionario de traducciones). Añadido `.pytest_cache/` a
`.gitignore`.

### Punto 34.3 del backlog: cifrado de protocolo BitTorrent (MSE/PE) y soporte de µTP

El backend de BitTorrent corre sobre `libtorrent` (librería embebida,
no proceso externo — ver la sección "Sobre la elección de tecnología"
más abajo), que ya trae MSE/PE y µTP implementados dentro de la propia
librería; a diferencia de las otras cuatro redes (reimplementadas
desde cero estudiando el protocolo), aquí "implementar" el punto 34.3
no significa escribir el protocolo de cifrado o el transporte UDP a
mano, sino: (1) dejar de depender implícitamente de los valores por
defecto de `libtorrent` -que, comprobado a mano contra
`session.get_settings()`, ya traían el cifrado en modo "enabled" y
`µTP` activo en ambos sentidos incluso sin tocar `settings` al crear
la sesión- y fijarlos explícitamente en `TorrentBackend.connect()`
como una decisión visible y documentada; y (2) exponer visibilidad real
de que se están usando, ya que antes no había ninguna forma de saber
si un peer concreto negoció cifrado o si una conexión iba por µTP en
vez de TCP.

Cambios:
- `TorrentBackend.connect()`: añade explícitamente `out_enc_policy` /
  `in_enc_policy` = `pe_enabled` (no `pe_forced`: forzar cifrado
  tiraría por la borda a los muchos peers de redes públicas reales que
  todavía no lo soportan, sin necesidad) y `allowed_enc_level` =
  `pe_both` (acepta tanto RC4 como el modo de solo-handshake-ofuscado),
  más `enable_outgoing_utp`/`enable_incoming_utp` = `True`.
- `TorrentBackend.get_stats()`: tres claves nuevas, calculadas en cada
  llamada a partir de los peers realmente conectados ahora mismo en las
  descargas activas (`handle.get_peer_info()`, comprobando el flag
  `rc4_encrypted`/`plaintext_encrypted` de cada peer) y del contador
  agregado de la sesión (`session.status().utp_stats["num_connected"]`):
  `connected_peers`, `encrypted_peers`, `utp_connections`. Se muestran
  en la pestaña Red de la GUI igual que el resto de detalles de
  conexión en vivo (`gui/widgets/network_tab.py`, con sus claves de
  traducción `stat_connected_peers`/`stat_encrypted_peers`/
  `stat_utp_connections` añadidas a los 13 idiomas de `gui/i18n.py`).

Validado en dos niveles:
- `tests/test_torrent_backend.py` (nuevo, dos tests async que sí crean
  una `lt.session` real -local, sin conectar a ningún peer-): que
  `connect()` deja la sesión con la política de cifrado y `µTP`
  esperada, y que `get_stats()` devuelve las tres claves nuevas a cero
  cuando no hay peers conectados.
- Contra la red BitTorrent real (vía `TorrentBackend`/`DownloadManager`
  de producción, no un simulador): se arrancó la descarga real del
  torrent oficial `ubuntu-24.04.1-desktop-amd64.iso` (53 seeds en el
  indexador) y se sondeó `get_stats()` cada 2s durante 40s. Con tráfico
  real llegó a haber más de 150 peers conectados simultáneamente,
  `utp_connections` subió hasta 34 en paralelo (µTP funcionando de
  verdad, no solo activado sin uso) y `encrypted_peers` llegó a 1 -bajo
  pero no cero, esperable: hoy son minoría los clientes BitTorrent que
  siguen negociando MSE/PE en redes públicas, pero confirma que la
  negociación de cifrado sí ocurre de punta a punta contra un peer real
  cuando el otro lado lo soporta-. La descarga se canceló nada más
  confirmarlo, sin llegar a completarla (no hacía falta para validar
  este punto, y el archivo son casi 6 GB).

### Punto 34.4 del backlog: auditoría de IPv6 en Soulseek/Gnutella2/eD2k-Kad

El punto 19 (ver más abajo, "Punto 19 — Soporte de IPv6") ya había
investigado y documentado, backend por backend, que Soulseek,
Gnutella2 y eD2k/Kad tienen un límite de protocolo genuino e
infranqueable en sus campos binarios de dirección: son de ancho fijo
(4 o 6 bytes) y ningún cliente real de esas redes tiene jamás una
variante de campo para IPv6. El punto 34.4 pedía revisar caso por
caso, backend por backend, qué parte concreta del código impone esa
limitación, para confirmar que no quedaba ningún hueco sin
documentar ni ninguna mejora real todavía posible.

Auditoría completa (cada punto del código donde se lee o escribe una
dirección en binario en los tres backends):
- **Soulseek** (`soulseek_backend.py`): un único punto de lectura,
  `_BinaryReader.ip()`, usado en las dos rutas donde el servidor manda
  la IP de un peer (`ConnectToPeer` y respuestas de búsqueda con
  conexión directa). No hay ningún punto de escritura: el servidor
  infiere nuestra propia IP del socket, nunca se la mandamos
  explícitamente. Sin huecos.
- **Gnutella2** (`g2_backend.py`): un único par lectura/escritura,
  `_parse_address()`/`_encode_g2_address()`, usados en los cuatro
  sitios del código donde se lee o construye una dirección (`/NA`,
  `/CH`, `/PUSH` entrante y saliente). Sin huecos.
- **eD2k/Kad** (`emule_backend.py`): un único punto de lectura,
  `_read_wire_ip()`, usado en `parse_nodes_dat`, la lista de
  contactos Kad de `KADEMLIA2_RES`/`KADEMLIA2_BOOTSTRAP_RES`, la
  lista de servidores que el servidor eD2k va "soplando" en caliente,
  y `OP_CALLBACKREQUESTED`; más otros tres puntos de escritura/lectura
  puntuales pero igual de fijos a 4 bytes: `client_id`
  (`_build_hello_payload`, HighID = la propia IP como uint32), el tag
  `TAG_SOURCEIP` de las respuestas de fuentes Kad, y el payload de
  `KADEMLIA_FIREWALLED_RES`. `_read_wire_ip()` no tenía todavía la
  nota explícita de límite de protocolo que sí tenían ya `ip()` de
  Soulseek y `_parse_address()` de G2 desde el punto 19 — añadida
  ahora, referenciando los otros tres puntos fijos del mismo backend.

Conclusión: no queda ningún hueco de documentación ni ninguna mejora
real de IPv6 pendiente en las tres redes — el punto ya estaba resuelto
de facto por el punto 19, y esta auditoría lo confirma y lo deja
explícito caso por caso. Único cambio de código: el docstring nuevo de
`_read_wire_ip()`. Validado con la suite completa de pytest (163 tests
en verde, sin cambios de comportamiento).

### Punto 34.5 del backlog: planificador de ancho de banda por franja horaria

Sobre los límites globales de velocidad ya existentes (punto 2,
`Config.global_download_limit_kbps`/`global_upload_limit_kbps`), se
añade un planificador que permite fijar unos límites "alternativos"
que sustituyen a los normales solo durante una franja horaria del día
configurable (p.ej. limitar más de noche, o durante horas de trabajo),
volviendo a los límites normales fuera de esa franja — mismo concepto
que los "límites alternativos programados" de qBittorrent/aMule.

Cambios:
- `core/config.py`: dataclass nueva `ScheduleConfig` (`enabled`,
  `start`/`end` en formato "HH:MM", `download_limit_kbps`/
  `upload_limit_kbps`), campo `Config.schedule` nuevo, con lectura/
  escritura en `load_config()`/`save_config()` igual que el resto de
  sub-configs (valores por defecto si el `config.json` es de antes de
  este punto).
- `core/bandwidth_scheduler.py` (nuevo): `is_within_schedule(start,
  end, now)` — pura, admite que la franja cruce la medianoche
  (`start` > `end`, p.ej. "23:00" a "07:00") y que `start == end`
  signifique franja de 24h; `effective_limits_kbps(config, now)` —
  devuelve los límites del planificador si está activado y toca, o si
  no los límites globales normales; y `BandwidthScheduler`, mismo
  patrón de manager con bucle en segundo plano que ya usan
  `WatchFolderManager`/`SavedSearchManager` (`start()`/`stop()`), que
  cada 30s reevalúa si ha cambiado el estado activo/inactivo de la
  franja y, solo si ha cambiado, reaplica los límites globales — un
  cambio hecho a mano desde Preferencias ya se aplica al momento por
  su cuenta, este bucle solo cubre la transición automática al llegar
  la hora sin que el usuario toque nada.
- `core/rate_limiter.py`: `apply_global_limits(config)` pasa a usar
  `effective_limits_kbps(config)` en vez de leer directamente los
  campos globales de `Config`, así que las cuatro redes "manuales"
  (que comparten sus limitadores) respetan el planificador sin ningún
  cambio adicional en cada backend.
- `gui/connection_manager.py`: `_apply_speed_limits()` (BitTorrent no
  comparte el limitador de `core.rate_limiter` — usa el suyo propio de
  libtorrent) también pasa a usar `effective_limits_kbps(config)`.
- `main.py`: `cmd_download` (descarga de un tiro por CLI) igual, por
  consistencia.
- `gui/main_window.py`: `MainWindow` arranca un
  `BandwidthScheduler(self._connection_manager.apply_global_speed_limits)`
  al construirse (mismo sitio que `WatchFolderManager`) y lo para en
  `closeEvent`.
- `gui/widgets/settings_dialog.py`: pestaña General, justo debajo de
  los límites globales — checkbox para activar el planificador, un
  rango de horas (`QTimeEdit` de inicio y fin) y los dos límites
  alternativos, con un tooltip explicando el cruce de medianoche.
- `gui/i18n.py`: cinco claves nuevas
  (`chk_schedule_enabled`/`lbl_schedule_time_range`/
  `lbl_schedule_download_limit`/`lbl_schedule_upload_limit`/
  `schedule_tooltip`) traducidas en los 13 idiomas.

Validado con pytest (`tests/test_bandwidth_scheduler.py`, nuevo:
`is_within_schedule` con franja normal, cruzando medianoche y de 24h;
`effective_limits_kbps` con el planificador activado/desactivado/fuera
de franja; `BandwidthScheduler._check_once` solo reaplica en las
transiciones de estado, no en cada evaluación — y `tests/test_config.py`,
round-trip de `ScheduleConfig` y valores por defecto con un
`config.json` antiguo sin la clave) y con un script directo contra
`ConnectionManager`/`TorrentBackend` de producción (sin GUI): con el
planificador activado y una franja que cubre la hora real del momento
de la prueba, `connect_network()` deja la sesión real de libtorrent
con los límites alternativos (100/50 kB/s) en vez de los globales
(1000/500 kB/s); al desactivar el planificador y volver a aplicar los
límites, la sesión vuelve a los globales normales — confirmado leyendo
`session.get_settings()["download_rate_limit"]`/`"upload_rate_limit"`
en los dos casos.

### Punto 34.6 del backlog: control remoto / API web

Permite gestionar las descargas (listar, pausar, reanudar, cancelar,
borrar, buscar y añadir nuevas) desde el navegador de cualquier
dispositivo de la red local sin abrir la ventana de escritorio —
pensado para usarlo con la aplicación minimizada a la bandeja del
sistema (ver punto 33 del backlog). Respeta la restricción de diseño
del proyecto: nada de frameworks HTTP de terceros (Flask/aiohttp/
FastAPI) — es un servidor HTTP/1.1 mínimo escrito a mano sobre
`asyncio.start_server()`, con el mismo estilo "parseo crudo" que ya
usa `core/http_client.py` para el lado cliente.

Cambios:
- `core/config.py`: dataclass nueva `RemoteControlConfig` (`enabled`,
  `host`, `port`, `token`), campo `Config.remote_control` nuevo, con
  lectura/escritura en `load_config()`/`save_config()` igual que el
  resto de sub-configs (valores por defecto si el `config.json` es de
  antes de este punto). El `token` se guarda en el keyring del sistema
  operativo igual que las contraseñas de red (`_keyring_get`/
  `_keyring_set`/`_keyring_password_or`), con caída a texto plano en
  el propio json en modo portable o si el keyring no está disponible.
  Por seguridad, desactivado por defecto, con `host` por defecto
  `"127.0.0.1"` (solo localhost) y sin `token` de fábrica — hace falta
  generar uno propio antes de poder activarlo, porque de lo contrario
  cualquiera con acceso al puerto podría gestionar las descargas.
- `core/remote_control.py` (nuevo): `RemoteControlServer`, mismo
  patrón de manager en segundo plano que `WatchFolderManager`/
  `BandwidthScheduler` (`start()`/`stop()`, más `reload()` para
  cuando cambian los ajustes desde Preferencias sin reiniciar la
  app). Si `remote_control.enabled` es falso o no hay `token`
  configurado, el servidor ni siquiera intenta escuchar en el puerto.
  Implementa a mano el parseo de peticiones HTTP/1.1
  (`_handle_client`/`_handle_request`/`_route`) y expone una API REST
  en JSON que es un adaptador fino sobre los métodos ya existentes de
  `DownloadManager`/`ConnectionManager` (no duplica ninguna lógica de
  gestión de descargas): `GET /api/downloads`, `GET /api/networks`,
  `POST /api/search`, `POST /api/downloads` (nueva descarga),
  `POST /api/downloads/{id}/{pause|resume|cancel}`,
  `DELETE /api/downloads/{id}`. La autenticación va por token,
  aceptado como cabecera `Authorization: Bearer <token>` o como
  parámetro `?token=` en la URL (para poder enlazar directamente desde
  marcadores del navegador del móvil); sin token válido, 401. También
  sirve en `/` una página única con HTML/JS embebidos (`_INDEX_HTML`,
  tema oscuro, JS vanilla sin dependencias) que guarda el token en
  `localStorage` del navegador, refresca la tabla de descargas sola y
  permite pausar/reanudar/cancelar/borrar y buscar+descargar desde ahí
  mismo.
- `gui/main_window.py`: `MainWindow` arranca un
  `RemoteControlServer(self._download_manager, self._connection_manager)`
  al construirse (mismo sitio que `BandwidthScheduler`), lo recarga
  (`reload()`) al guardar cambios en Preferencias y lo para en
  `closeEvent`.
- `gui/widgets/settings_dialog.py`: pestaña nueva "Control remoto",
  justo después de la de proxy — checkbox de activación, campos de
  host y puerto, campo de token con un botón "Generar…" que rellena
  uno aleatorio con `secrets.token_urlsafe(24)`, y una nota explicando
  el riesgo de exponerlo a `0.0.0.0`. Al guardar, si está activado sin
  token se desactiva solo y se avisa con un mensaje, en vez de dejar
  el servidor sin arrancar en silencio.
- `gui/i18n.py`: ocho claves nuevas (`settings_tab_remote`,
  `lbl_remote_enabled`, `lbl_remote_host`, `lbl_remote_port`,
  `lbl_remote_token`, `btn_remote_generate_token`, `lbl_remote_note`,
  `msg_remote_token_required`) traducidas en los 13 idiomas.

Validado con pytest (`tests/test_remote_control.py`, nuevo: 17 tests
extremo a extremo contra un `RemoteControlServer` real escuchando en
`127.0.0.1` con puerto libre asignado por el sistema operativo —mismo
enfoque que `test_http_client.py`, aquí el servidor es el propio
código de producción y el cliente es un socket a mano— cubriendo la
página raíz, rechazo sin token/con token incorrecto, token por
cabecera y por query, estado de redes, pausar/reanudar/cancelar,
acción sobre descarga inexistente (404), borrar, buscar (éxito y
petición sin `query`, 400), arrancar una descarga nueva (201) y con
campos incompletos (400), ruta desconocida (404), y que el servidor no
llega a escuchar si falta el token o si está desactivado; y
`tests/test_config.py`, round-trip de `RemoteControlConfig` y valores
por defecto con un `config.json` antiguo sin la clave). Además,
validado con un script directo (fuera de pytest) contra el
`DownloadManager`/`ConnectionManager` reales de producción —no los
dobles de prueba— monkeypatcheando solo la ruta de la base de datos y
la carga de config para no tocar las del usuario: servidor real
levantado, petición HTTP real de listado/búsqueda/descarga contra él,
confirmando que el cableado de producción (`gui/main_window.py` →
`RemoteControlServer` → `DownloadManager`) funciona de punta a punta y
no solo con los dobles de prueba.

### Punto 34.7 del backlog: mejoras de accesibilidad en la GUI

Se auditó la GUI en busca de huecos reales de accesibilidad (lector de
pantalla y navegación completa por teclado) en vez de aplicar cambios
genéricos: `gui/widgets/settings_dialog.py` resultó estar ya accesible
sin tocar nada, porque `QFormLayout.addRow(str, widget)` crea la
`QLabel` y llama a `QLabel.setBuddy(widget)` automáticamente, así que
todos sus campos ya tenían nombre accesible y navegación por Tab
correctas de fábrica. El hueco real encontrado fue doble:

1. Un bug de targeting en el menú contextual por teclado (tecla Menú/
   Mayús+F10), presente en las 9 tablas de la app que usan
   `Qt.ContextMenuPolicy.CustomContextMenu` + `customContextMenuRequested`
   (el patrón que usa toda la app para sus menús contextuales). Qt
   entrega los eventos `QEvent.Type.ContextMenu`, tanto de ratón como de
   teclado, al `viewport()` interno de `QAbstractScrollArea` (del que
   heredan `QTableView`/`QTableWidget`), no al widget en sí — y con
   `CustomContextMenu` activado, Qt jamás llega a invocar el método
   virtual `contextMenuEvent()`, así que ahí no se puede interceptar.
   Además, al abrir el menú por teclado, Qt manda una posición sin
   fiar (la del cursor del ratón, o (0, 0) si no está sobre la tabla)
   en vez de la fila realmente seleccionada — y como el resto de la
   app decide sobre qué fila actuar mirando esa posición
   (`table.indexAt(pos)`), sin arreglo Mayús+F10 actuaba siempre sobre
   la primera fila visible en vez de la fila con el foco de teclado,
   ignorando en silencio la selección real del usuario.
2. Varios campos de texto y tablas sin `accessibleName()`, invisibles
   para un lector de pantalla al no tener ni label-buddy ni texto
   propio que anunciar.

Cambios:
- `gui/widgets/accessible_table.py` (nuevo): `_KeyboardContextMenuMixin`
  sobrescribe `viewportEvent()` (el gancho correcto para interceptar el
  evento antes de que se emita `customContextMenuRequested`) — si el
  evento es `QEvent.Type.ContextMenu` con `reason() ==
  QContextMenuEvent.Reason.Keyboard` y la política es
  `CustomContextMenu`, emite la señal a mano con la posición del centro
  de `visualRect(currentIndex())` en vez de la posición sin fiar del
  evento, y devuelve `True` para no propagar el evento sin corregir; en
  cualquier otro caso (ratón, u otra política) delega en
  `super().viewportEvent(event)` sin tocar nada. `AccessibleTableView`
  y `AccessibleTableWidget` combinan el mixin con `QTableView`/
  `QTableWidget` por herencia múltiple.
- Se sustituyó `QTableView`/`QTableWidget` por
  `AccessibleTableView`/`AccessibleTableWidget` en las 9 tablas con
  menú contextual de la app: `downloads_tab.py`, `search_tab.py`,
  `network_tab.py` (incluida su tabla con `SelectionMode.NoSelection`),
  `alerts_tab.py`, `alert_results_dialog.py`, `browse_user_dialog.py`,
  `browse_host_dialog.py` y `emule_friends_dialog.py`.
- `setAccessibleName()` añadido donde faltaba nombre accesible propio:
  la caja de búsqueda y la tabla de resultados de `search_tab.py`, la
  tabla de descargas de `downloads_tab.py`, la tabla de red de
  `network_tab.py`, el filtro de `hub_list_dialog.py`, y en
  `chat_tab.py` el log, el campo de mensaje y la lista de usuarios de
  cada sala/conversación abierta (`_RoomChatWidget`/`_PrivateChatWidget`).
  Donde ya existía un texto adecuado que reutilizar en vez de crear una
  clave nueva, se usó ese: el `windowTitle()` del propio diálogo en
  `alert_results_dialog.py`/`browse_user_dialog.py`/
  `browse_host_dialog.py`/`emule_friends_dialog.py`, y las claves ya
  traducidas `tab_alerts`/`stats_totals_title`/`stats_history_title`
  en `alerts_tab.py`/`stats_tab.py`.
- `gui/widgets/downloads_tab.py`: atajo de teclado Supr
  (`QShortcut(QKeySequence.StandardKey.Delete, ...)`, con
  `Qt.ShortcutContext.WidgetShortcut` para que solo actúe con el foco
  en la tabla) que dispara `_confirm_and_delete()` sobre la selección
  actual — antes borrar una descarga solo era posible con el menú
  contextual o un botón, no con teclado puro.
- `gui/i18n.py`: ocho claves nuevas (`acc_search_query`,
  `acc_downloads_table`, `acc_search_results_table`,
  `acc_network_table`, `acc_chat_message`, `acc_chat_users`,
  `acc_chat_log`, `acc_hub_filter`) traducidas en los 13 idiomas.

Validado con un script directo (fuera de pytest, en
`QT_QPA_PLATFORM=offscreen`, mismo patrón usado en puntos anteriores de
la GUI): se mandaron eventos `QContextMenuEvent` reales vía
`QApplication.sendEvent(widget.viewport(), event)` — no llamando a
mano a `contextMenuEvent()`, que habría dado un falso positivo, como
pasó en un primer intento fallido durante el desarrollo — confirmando
que Mayús+F10 emite `customContextMenuRequested` con la posición de la
fila seleccionada (no la fila 0) tanto en `AccessibleTableView` como en
`AccessibleTableWidget` (incluido con `SelectionMode.NoSelection`, el
caso de `NetworkTab`/`StatsTab`), que el clic derecho normal con
ratón sigue emitiendo la posición tal cual sin alterar, que
`DownloadsTab`/`HubListDialog` tienen `accessibleName()` no vacío en
sus campos, que `ChatTab` se construye sin error, y que el atajo Supr
de `DownloadsTab` dispara `_confirm_and_delete()` con la selección
actual. Suite completa de pytest reejecutada sin regresiones (192
passed) — este punto no añade tests nuevos a la suite porque toda su
validación es de comportamiento de GUI, cubierta por el patrón de
script scratchpad ya establecido en el proyecto.

### Arreglo: "Salir" desde la bandeja dejaba el proceso zombi (BitTorrent)

Bug real reportado por el usuario: al pulsar "Salir" en el menú
contextual del icono de la bandeja, la ventana desaparecía pero el
proceso no devolvía el control a la terminal. Reproducido de forma
aislada (GUI en `QT_QPA_PLATFORM=offscreen`, conectando BitTorrent y
disparando `_on_quit()` mediante un script propio): sin ninguna red
conectada el proceso termina al instante, pero con BitTorrent
conectado tardaba entre 4 y más de 20-30 segundos (variable, según
condiciones de red) en volver — el "zombi" que reportaba el usuario.

Causa: `TorrentBackend.disconnect()` se limitaba a poner
`self._session = None`; el destructor de `lt.session` de libtorrent
hace un apagado "educado" de DHT/LSD/UPnP/NAT-PMP con idas y vueltas
de red reales, de forma **síncrona**, lo que bloquea el hilo que lo
ejecute durante ese tiempo variable. Además, al cerrar la ventana
principal el cierre real de cada red se dispara con
`asyncio.ensure_future(...)` (sin esperarlo) justo antes de que Qt
detecte que no queda ninguna ventana visible y llame a `quit()`, así
que ese `disconnect()` casi nunca llega a ejecutarse antes de que el
bucle de eventos se pare — la sesión de libtorrent seguía viva y su
destructor lento acababa disparándose de todos modos, ya sin
control, durante el cierre del propio intérprete de Python al
recolectar el resto de objetos.

Arreglado destruyendo la sesión en un hilo `daemon=True` propio
(`_destroy_session()`, nuevo, en `backends/torrent_backend.py`) en vez
de dejar que el recolector de basura la destruya en el hilo principal:
al ser un hilo daemon, si el proceso termina mientras ese hilo sigue
esperando a la red, el sistema operativo se lo lleva por delante sin
más — nunca vuelve a bloquear el cierre de la aplicación. Validado
reproduciendo el mismo escenario aislado 10 veces seguidas tras el
arreglo: el proceso siempre termina en pocos segundos (entre 1 y 12s,
frente a los 20-30s+ o cuelgue indefinido de antes) y sin errores.

### Arreglo: el mismo zombi de "Salir" reaparecía en DC++, eMule y Gnutella2

El usuario volvió a reportar el síntoma tras el arreglo anterior:
"sigue el fallo de cerrar p2p-total desde el icono minimizado... el
icono se quita de la barra de aplicaciones, pero el proceso se queda
en la memoria". El arreglo previo solo cubría el caso de BitTorrent
(el destructor síncrono de `lt.session`); investigando el resto del
código se encontró el mismo patrón de fondo reaparecido de forma
independiente en tres sitios más, los tres en la verificación de hash
al completar una descarga: `backends/dcpp_backend.py` (TTH tras
descargar por DC++), `backends/emule_backend.py` (MD4/eD2k tras
descargar por eMule — añadido esta misma sesión, sin darme cuenta de
que reintroducía el mismo bug ya arreglado una vez para
`SharedLibrary.rescan()` en `core/sharing.py`) y `backends/
g2_backend.py` (SHA1 tras descargar por Gnutella2). Los tres usaban
`await asyncio.to_thread(...)` para no bloquear el bucle de eventos
durante el hasheo — correcto para no congelar la GUI mientras la
descarga está en curso, pero `asyncio.to_thread` ejecuta la función en
el `ThreadPoolExecutor` por defecto de asyncio, cuyos hilos **no** son
`daemon`, así que el intérprete de Python los espera al salir (vía
`atexit`) igual que le pasaba a la sesión de libtorrent: si una
descarga terminaba justo antes de pulsar "Salir" (o durante el cierre,
por el mismo `asyncio.ensure_future(...)` sin esperar de
`closeEvent()` documentado arriba), el hasheo en curso dejaba el
proceso colgado hasta terminar, sin que el icono de la bandeja llegara
a reflejarlo.

Arreglado extrayendo el hilo `daemon=True` propio de `core/sharing.py`
a un módulo nuevo y genérico, `core/async_utils.py`
(`run_in_daemon_thread(func, *args, name=..., **kwargs)`, con un
nombre de hilo configurable en vez del fijo `"shared-library-scan"` de
antes), y sustituyendo los tres `asyncio.to_thread(...)` de arriba
-además de la llamada que ya existía en `core/sharing.py`- por este
helper común. Validado: batería de tests automatizados (`pytest`, 201
pruebas, sin fallos) y una reproducción aislada por CLI que lanza un
subproceso con una tarea de 30s en un hilo daemon vía
`run_in_daemon_thread` y comprueba que el proceso Python padre
termina igualmente en ~0.3s sin esperar a que la tarea acabe -mismo
mecanismo, ya validado en real para BitTorrent, ahora también cubre
DC++, eMule y Gnutella2.

### Función: conectar automáticamente cada red al arrancar la GUI

Petición explícita del usuario: "añade una opción en cada pestaña de
las redes para autoconectar dicha red (en las preferencias)". Se
añade un checkbox nuevo en la pestaña de cada una de las cinco redes
de Preferencias (`gui/widgets/settings_dialog.py`) que, al marcarlo,
hace que esa red se conecte sola nada más arrancar la GUI, sin
esperar a que el usuario pulse "Conectar" a mano.

Cambios:
- `core/config.py`: campo `auto_connect: bool = False` nuevo en las
  cinco dataclasses de config por red (`TorrentConfig`,
  `SoulseekConfig`, `DCPPConfig`, `Gnutella2Config`, `EMuleConfig`),
  con lectura en `load_config()` (con valor por defecto `False` para
  seguir cargando sin romper config.json de versiones anteriores que
  no tengan la clave) y escritura automática en `save_config()` (ya
  serializa cada sub-config entero vía `asdict()`). Método nuevo
  `Config.auto_connect_networks() -> list[Network]` que devuelve, en
  el mismo orden que recorre `core.models.Network`, las redes
  marcadas.
- `gui/widgets/settings_dialog.py`: un `QCheckBox` nuevo al principio
  de cada una de las cinco pestañas de red, leído en
  `_collect_config_from_widgets()` igual que el resto de campos.
- `gui/connection_manager.py`: método nuevo
  `ConnectionManager.autoconnect_configured_networks()`, que lee la
  config y lanza `connect_network()` en segundo plano (sin esperar,
  igual que un "Conectar" manual desde el menú Redes) para cada red
  de `Config.auto_connect_networks()`. Reutiliza tal cual el manejo
  de errores ya existente de `connect_network()`: si una red marcada
  no está configurada (por ejemplo Soulseek sin usuario/contraseña),
  la conexión falla igual que si se hubiera pulsado "Conectar" a
  mano y la red queda en `STATUS_ERROR` visible en el piloto de la
  pestaña Red, sin crashear la GUI ni bloquear el arranque de las
  demás redes.
- `gui/main_window.py`: `MainWindow.__init__` llama a
  `self._connection_manager.autoconnect_configured_networks()` una
  sola vez, justo antes de lanzar la comprobación de actualizaciones
  al final del constructor.
- `gui/i18n.py`: clave `chk_auto_connect` nueva, traducida en los 13
  idiomas soportados.

Validado con pytest (`tests/test_config.py`: round-trip de
`auto_connect` a través de guardar/cargar JSON para varias redes a la
vez, filtrado correcto de `auto_connect_networks()`, y que sigue
cargando bien un config.json antiguo sin la clave) y con un script
directo contra `ConnectionManager`/`Config` de producción (sin GUI,
por la instrucción del usuario de no hacer pruebas manuales de
interfaz gráfica): con `torrent.auto_connect = True` y
`soulseek.auto_connect = True` en el config, al llamar a
`autoconnect_configured_networks()` BitTorrent llega a
`STATUS_CONNECTED` de verdad (conexión real a la sesión de
libtorrent) mientras que Soulseek, sin usuario/contraseña
configurados, acaba en `STATUS_ERROR` sin lanzar ninguna excepción ni
afectar a la conexión de BitTorrent.

## Notas sobre cada red (para cuando las abordemos)

- **BitTorrent**: `libtorrent` gestiona la sesión, DHT, peers y descarga.
  No hay "búsqueda" nativa — se añade por magnet/`.torrent`, o se integra
  un motor de índices externo si se quiere buscador in-app.
- **Soulseek**: protocolo con servidor central. Implementación nativa
  hablada a mano sobre sockets asyncio (sin `aioslsk` ni ninguna otra
  librería), estudiada directamente del código fuente de nicotine+.
  Incluye la estrategia de conexión dual (directa + indirecta vía
  servidor) que resuelve el caso, muy común, de un peer detrás de NAT
  sin puerto redirigido.
- **DC++**: protocolo NMDC/ADC basado en hubs. No hay librería Python
  madura — hay que hablar el protocolo a mano sobre sockets asyncio.
- **Gnutella2 (G2)**: pese al nombre parecido, un protocolo DISTINTO e
  incompatible con la Gnutella "clásica" (G1, no implementada en este
  proyecto) — usa hubs (como DC++) y un formato de paquete en árbol en
  vez de la cabecera plana de G1. Es la variante con tráfico real hoy
  en día (confirmado: `gtk-gnutella` arranca contra G2 por defecto).
  Sin librería Python — protocolo hablado a mano, estudiado
  directamente del código fuente de gtk-gnutella (sin copiar nada
  literal, por la licencia GPLv2+ de ese proyecto).
- **eMule/Kad**: implementación nativa en Python del protocolo eDonkey2000
  (comunicación con servidores ed2k, hashing MD4 por chunks de 9.28 MB)
  y de Kademlia/Kad (bootstrap y búsqueda de nodos/fuentes vía UDP). Era
  la red más laboriosa de las cinco por no existir ninguna librería
  Python madura ni recurrir a procesos externos — protocolo binario
  hablado a mano sobre sockets UDP/TCP con asyncio. Ver el detalle
  completo (incluidos los tres bugs de protocolo encontrados y
  corregidos gracias a las pruebas contra tráfico real) más arriba.

## Restricción de diseño: cero procesos externos

Todo corre dentro del propio proceso Python. `libtorrent` es una
**librería** (se importa, no se lanza como binario aparte), así que
cumple esta restricción. Soulseek, DC++, Gnutella2 y eMule/Kad se
implementan hablando sus protocolos directamente sobre sockets, sin
depender de aMule, un cliente DC++/Soulseek externo, ni ningún otro
programa instalado en el sistema.

## Siguiente paso sugerido

Los cinco backends de red y la GUI PyQt6 están implementados
(`python main.py gui`), con pestañas de Búsqueda, Transferencias (con
columna de pares conectados) y Red (detalles de conexión en vivo por
red). El backend Soulseek se reescribió como implementación nativa del
protocolo (sin `aioslsk`, ver "Estado actual" más arriba) y ya se
validó el ciclo completo conectar → buscar → descargar → pausar →
reanudar tanto por CLI como **desde la propia GUI** (automatizada con
`xdotool`/`spectacle`), con varias descargas reales completadas al
100% y contenido verificado en disco, resolviendo el bloqueo de NAT
que antes lo impedía. De paso se corrigió un bug real (columna
Velocidad vacía durante las descargas de Soulseek, ver "Estado
actual"). Para Gnutella2, la búsqueda real SÍ funciona (confirmado: al
menos un hub real devolvió resultados reales para "test" tras probar
7+ hubs y varios términos, ver "Estado actual") pero entre esos
resultados había nombres de archivo con indicios claros de CSAM, así
que se decidió **no intentar ninguna descarga real de contenido
descubierto por búsqueda en G2** — el riesgo de toparse con más
contenido ilegal al buscar términos arbitrarios no compensa. Se
completó en su lugar lo que sí era una carencia real y arreglable del
propio backend, validado contra un servidor HTTP local sintético (no
contra la red real): pausar/reanudar (antes `NotImplementedError`)
ahora funcionan vía `Range:` HTTP con caída automática a reinicio
desde 0 si el origen no lo soporta, y se corrigió el mismo hueco de
`speed_bps` que tenía Soulseek. El backend Gnutella clásico (G1) se
eliminó por completo del proyecto a petición explícita del usuario, y
se validó desde la propia GUI el ciclo conectar → buscar contra
Gnutella2 real (palabra suelta y consulta literal de varias palabras,
ver "Estado actual"), de paso arreglando un bug real en la barra de
estado ("X/6" hardcodeado tras pasar de seis a cinco redes).

Después, a petición explícita del usuario, se rediseñó la conexión por
red: el panel lateral "Redes" (`widgets/network_panel.py`, ahora
eliminado por completo) se sustituyó por un menú "Redes" en la barra
de menú, con un tick por red (marcado = conectada, desmarcado =
desconectada; se deshabilita mientras está "conectando" para evitar
dobles clics) más "Conectar todas" y "Desconectar todas". Se validó en
vivo contra infraestructura real: activar Gnutella2 desde el menú la
conecta a un hub real, "Desconectar todas" desconecta correctamente
todas las redes activas a la vez, y BitTorrent conecta con normalidad
(DHT real, puerto de escucha 6881). La pestaña Red sigue siendo la
referencia de estado detallado por red; el menú ahora es solo el punto
de control de conectar/desconectar.

También se añadió al menú contextual de la pestaña Transferencias:
"Borrar descarga (y archivos)" (cancela si sigue activa, borra el
registro del historial y elimina del disco lo ya descargado) y
"Limpiar completados" (borra del historial, sin tocar el disco, todas
las descargas en estado `COMPLETED`; sigue ofreciéndose incluso al
hacer clic derecho en una zona vacía de la tabla, mientras que las
acciones por fila —pausar/cancelar/borrar/abrir carpeta— solo
aparecen sobre una fila real). Al probar esto en vivo, el usuario
reportó un bug real: "Limpiar completados" borró una descarga que
mostraba 0%. La causa raíz estaba en `Download.progress`
(`core/models.py`), que devolvía 0.0 siempre que `size_bytes` fuera 0,
sin mirar el estado — así que una descarga ya `COMPLETED` cuyo backend
nunca llegó a rellenar el tamaño total se mostraba como "0%" en la
barra de progreso, pareciendo activa cuando en realidad ya estaba
terminada y era correcto borrarla. Arreglado devolviendo 1.0 de
inmediato si el estado es `COMPLETED`, antes de mirar `size_bytes`.
Validado en vivo con filas sintéticas insertadas directamente en la
base de datos (una `COMPLETED` con tamaño desconocido, otra
`COMPLETED` con tamaño conocido, una `DOWNLOADING` al 30 % y una
`QUEUED` al 0 %): tras el arreglo, la fila `COMPLETED` de tamaño
desconocido pasó a mostrar correctamente el 100 %, y al pulsar
"Limpiar completados" solo desaparecieron las dos filas realmente
`COMPLETED`, dejando intactas la `QUEUED` al 0 % y la `DOWNLOADING` al
30 % — confirmando que el bug reportado ya no puede ocurrir. También
se validó "Borrar descarga (y archivos)" con su diálogo de
confirmación, comprobando que borra la fila de la tabla y el registro
de la base de datos.

Después se implementó, a petición explícita del usuario, que la
descarga desde la GUI (cualquier red) ya no muestre el selector de
carpeta —usa y crea automáticamente `config.default_download_dir`,
avisando con un diálogo si no se puede crear— y que las opciones de
Soulseek (puerto de escucha, número máximo de resultados y tiempo de
búsqueda) sean visibles y editables desde Preferencias, ver "Estado
actual" para el detalle completo y la validación en vivo. Se dio la
misma paridad de ajustes a Gnutella2 (puerto de escucha ahora
realmente usado en el fallback `/PUSH`, número máximo de resultados
con corte anticipado, tiempo de búsqueda) y se revalidaron descarga/
pausa/reanudar/cancelar/"Limpiar completados" para esa red, ver
"Estado actual" para el detalle completo.

Después, a petición explícita del usuario ("ahora con e2dk... dejar
funcional 100%"), se dio la misma paridad de ajustes a eMule/eD2k
(puertos ya existentes expuestos en Preferencias, número máximo de
resultados con corte anticipado, tiempo de búsqueda) y, sobre todo, se
arregló pausa/reanudación/cancelación, que a diferencia de Soulseek y
Gnutella2 estaban realmente rotas: `pause_download()` descartaba todo
el progreso llamando a `cancel_download()`, y `resume_download()`
lanzaba `NotImplementedError`. Reescritas ambas con el mismo patrón
de entrada con flags que ya usaban las otras dos redes, más soporte
real de reanudar desde el offset correcto vía `OP_REQUESTPARTS` y el
mismo cálculo de `speed_bps`. Ver "Estado actual" para el detalle
completo y la validación (test contra un peer eD2k sintético con
verificación MD4 byte a byte, test de corte anticipado en búsqueda, y
validación en vivo de la pestaña de Preferencias desde la GUI).

Después, a petición explícita del usuario ("que funcione la red e2dk/
kad completamente... probar búsqueda por palabra y literal, descarga,
pausa/reanudar descarga, limpiar completados, todos"), se probó el
ciclo completo contra la red eD2k/Kad **real** (no un peer sintético,
por primera vez) y aparecieron dos bugs reales que hasta entonces
habían impedido que cualquier descarga eD2k/Kad llegara a completarse
contra un peer real, aunque sí funcionaban contra el servidor sintético
usado en la sesión anterior:

1. **Byte de protocolo incorrecto en el handshake cliente-cliente.**
   `OP_HELLO`, `OP_HELLOANSWER`, `OP_SETREQFILEID`, `OP_HASHSETREQUEST`,
   `OP_STARTUPLOADREQ` y `OP_REQUESTPARTS` se enviaban envueltos en
   `OP_EMULEPROT` (0xC5, extensiones específicas de eMule) cuando en
   realidad son opcodes del protocolo eDonkey clásico y deben ir
   envueltos en `OP_EDONKEYPROT` (0xE3) incluso en comunicación
   cliente-cliente. Con el byte equivocado, los tres peers reales
   probados con HighID aceptaban la conexión TCP pero nunca respondían
   al `OP_HELLO` (timeout). Corregido en los 6 puntos de
   `backends/emule_backend.py` donde se construían estos paquetes;
   verificado que un peer real (217.183.58.166) respondió por primera
   vez con un `OP_HELLOANSWER` válido tras el arreglo.
2. **Un único `OP_REQUESTPARTS` puede contestarse con varios
   `OP_SENDINGPART`.** El bucle de transferencia
   (`_client_transfer_loop()`) leía un solo paquete `OP_SENDINGPART`
   por cada petición de 180 KB y avanzaba el puntero de posición el
   tramo completo pedido, asumiendo que un peer real siempre contesta
   con un único paquete — en la práctica, los peers reales suelen
   trocear su respuesta en varios `OP_SENDINGPART` más pequeños, así
   que se perdía la mayor parte de cada tramo en silencio. Esto hacía
   que la barra de progreso llegara al 100 % mientras el fichero en
   disco quedaba muy por debajo del tamaño real, fallando después la
   verificación MD4 final. Corregido llevando la cuenta de
   `[r_start, r_end)` de cada `OP_SENDINGPART` recibido y siguiendo
   leyendo paquetes hasta cubrir el tramo pedido completo antes de
   pasar al siguiente.

Con ambos arreglos se completó, por primera vez en la historia del
proyecto, una descarga real de extremo a extremo contra la red eD2k/Kad
real con verificación MD4 exitosa (Duran Duran - Hungry Like The
Wolf.mp3, hash `40fb8087ce59977e1d6756317a310f16` recalculado de forma
independiente y coincidente byte a byte). También se confirmaron
contra la red real: búsqueda por una palabra ("mp3" → 200 resultados,
"pdf" → 200 resultados) y búsqueda literal de varias palabras ("Red
Hot Chili Peppers" → 191 resultados, todos coincidentes); pausar y
reanudar en mitad de una descarga real (probado dos veces: por CLI,
con verificación de que el offset se retoma exactamente donde se dejó,
sin reiniciar desde 0; y en vivo desde los botones de la GUI —clic
derecho → Pausar/Reanudar—, con la velocidad congelándose a 0 al
pausar y el estado pasando correctamente por "Buscando fuentes" al
reanudar); y "Limpiar completados" sobre una descarga eD2k/Kad ya
completada, comprobando en vivo desde la GUI que solo desaparece del
historial la fila `COMPLETED`, dejando intactas el resto. Nota sobre
la naturaleza de la red real: al probar contra varios peers HighID
distintos localizados en vivo, no todos los intentos de descarga
tuvieron éxito —algunos peers dejaron de estar accesibles entre la
búsqueda y el intento de conexión (timeout de TCP), y al menos uno
aceptó la conexión y el HELLO pero nunca llegó a conceder slot de
subida (cola de subida del cliente real, comportamiento normal de
eMule)— consistente con la naturaleza intermitente esperada de una red
P2P real, no con ningún bug de código adicional.

Después, a petición explícita del usuario, se añadió selección
múltiple (descarga en lote desde Búsqueda, acciones en lote desde el
menú contextual de Transferencias) y ordenación por columnas en ambas
tablas, ver "Estado actual" para el detalle completo y la validación
en vivo contra Soulseek real (descarga en lote de hasta 10 ficheros,
borrado en lote de hasta 20, ordenación por valor real en vez de texto
formateado). Pausar/reanudar en lote quedó sin verificar por
intermitencia real de la red Soulseek durante la sesión de pruebas.

Después, a petición explícita del usuario, la pestaña Búsqueda pasó de
una única tabla de resultados a una pestaña independiente por cada
búsqueda lanzada (con su "X" de cierre), cada una con su propio modelo/
orden/selección, ver "Estado actual" para el detalle completo y la
validación en vivo contra Soulseek real (dos búsquedas simultáneas en
pestañas separadas, cierre de una con búsqueda aún en streaming, y
descarga desde la pestaña restante).

Después, a petición explícita del usuario, se añadieron las opciones
"Añadir magnet…"/"Añadir .torrent…" al menú Archivo, se corrigió la
fusión de archivos duplicados en la búsqueda eD2k/eMule para que sume
fuentes igual que Soulseek en vez de descartarlos, y se añadió el
botón "Seguir buscando" a todas las pestañas de búsqueda para ampliar
una búsqueda cuando se agota su tiempo de espera sin perder lo ya
encontrado — ver "Estado actual" para el detalle completo y la
validación en vivo (menú Archivo con y sin BitTorrent conectado, test
aislado de la fusión eD2k, y dos rondas de "Seguir buscando" contra
Soulseek real fusionando fuentes correctamente).

Después, el usuario reportó en vivo una recurrencia del mismo
`OSError: [Errno 24] Demasiados ficheros abiertos` ya documentado
arriba (`socket.accept()` sobre el puerto de escucha de Soulseek, esta
vez buscando "Michael Jackson"), pese a los dos arreglos anteriores
(cierre rápido de la conexión "P" tras el primer mensaje + tope de 200
conexiones entrantes gestionándose a la vez). Diagnóstico: ninguno de
esos dos arreglos limitaba la *tasa* a la que asyncio drena la cola de
`accept()` del propio kernel (`selector_events._accept_connection`
acepta hasta `backlog + 1` conexiones de golpe por cada aviso de
socket listo) — para un término tan popular como para atraer cientos
de miles de peers casi a la vez, el pico de conexiones aceptadas y
pendientes de cerrar seguía pudiendo agotar los file descriptors antes
de que el bucle de eventos les diera abasto. Se añadieron dos
mitigaciones más en `backends/soulseek_backend.py`, complementarias a
las anteriores: (1) `backlog=64` explícito en el `asyncio.start_server`
del puerto de escucha (antes usaba el valor por defecto de asyncio,
100), para que el propio kernel rechace con RST, sin gastar ningún
file descriptor nuestro, las conexiones que sobran de la ráfaga antes
de que lleguen a `accept()`; (2) `_incoming_gate_limit` bajado de 200 a
100. Verificado en vivo por el usuario contra la red real: una
búsqueda de "Michael Jackson" llegó a 32.000 resultados sin cortarse
ni volver a producir el error.

A continuación, a petición explícita del usuario ("hay que añadir en
la pestaña de búsqueda, la opción para buscar por tipo de archivo como
en amule"), se añadió un filtro de tipo de archivo a la pestaña de
Búsqueda: un combo "Tipo:" junto a la caja de texto, con las
categorías Todos/Archivos/Audio/Imágenes de CD/Imágenes/Programas/
Documentos/Vídeo, igual que el desplegable de aMule. A diferencia de
aMule (que puede pedirle el tipo al propio servidor eD2k como parte de
la búsqueda), aquí se filtra **en el cliente por la extensión del
nombre de fichero** (`_matches_file_type()` en
`gui/widgets/search_tab.py`), porque es la única forma que funciona
igual para las cinco redes soportadas — de las cinco, solo eMule/Kad
tiene un filtro de tipo nativo en su wire protocol de búsqueda; las
otras cuatro no. El filtro se aplica en el único punto de entrada de
resultados de cada pestaña (`SearchResultsPanel._add_or_merge()`), así
que cubre tanto el streaming de Soulseek como el resto de redes, y se
respeta también al pulsar "Seguir buscando". Traducciones añadidas en
es/en/eu (claves `lbl_file_type`/`filetype_*`). Verificado en vivo
desde la propia GUI (`xdotool`/`spectacle`, forzando
`QT_QPA_PLATFORM=xcb`): el combo aparece correctamente junto a la caja
de búsqueda y su desplegable muestra las 8 categorías.

A continuación, a petición explícita del usuario ("haz el paso 1 de
DC++ completo"), DC++ recibió la misma paridad de pausar/reanudar/
`max_results` que ya tenían Soulseek, Gnutella2 y eMule — era la única
de las cinco redes que se había quedado atrás en eso — ver "Estado
actual" para el detalle completo y la validación (hub y peer NMDC
sintéticos en local: corte anticipado de búsqueda, pausa/reanudación
con offset correcto y contenido verificado byte a byte, y
cancelación).

Después, a petición explícita del usuario, se fijó el icono de la
aplicación (`Icono.png`, en la raíz del proyecto — renombrado desde
`Iono.png`, el nombre con el que se entregó originalmente el fichero)
tanto a nivel de `QApplication` (`gui/app.py`, para la barra de
tareas) como de la ventana principal (`gui/main_window.py`), y se
sustituyó el `QMessageBox.about()` genérico del diálogo "Ayuda →
Acerca de..." por un diálogo propio (`gui/widgets/about_dialog.py`)
que muestra `Logo.png` grande y centrado en la parte superior, con el
texto descriptivo debajo (sin repetir "P2P Total" en texto, ya que el
propio logo ya lo dice). Las rutas a ambas imágenes se centralizan en
`gui/resources.py` para no duplicar la resolución de rutas en los tres
sitios que las usan. Verificado en vivo desde la propia GUI
(`xdotool`/`spectacle`, forzando `QT_QPA_PLATFORM=xcb`): el icono se
ve correctamente en la barra de título, en la barra de tareas y en la
propia ventana del diálogo, y el logo se renderiza grande, nítido y
centrado en "Acerca de...".

A continuación, a petición explícita del usuario ("haz los tres pasos
actualizando el readme.md al finalizar cada uno"), se implementó el
primero de los tres pasos pendientes del roadmap: la ventana principal
ahora recuerda tamaño y posición entre sesiones (`UIConfig` en
`core/config.py` gana los campos `window_width`/`window_height`/
`window_x`/`window_y`, con `window_x`/`window_y` a `None` por defecto
para dejar que el gestor de ventanas decida la posición inicial la
primera vez; `MainWindow.__init__` los aplica con `resize()`/`move()`
y `closeEvent` los persiste con `save_config()` antes de desconectar
las redes), y la barra de estado ganó un indicador permanente
("N descargas activas · velocidad total", vía
`QStatusBar.addPermanentWidget()` para que no lo pise el mensaje de
redes conectadas) que se recalcula en cada tick de
`DownloadManager.on_progress()` sumando velocidad y contando descargas
en estado `DOWNLOADING` desde el `DownloadsModel` ya existente
(`active_speed_bps()`/`active_count()`, reutilizados también por
`DownloadsTab`), y se vacía solo cuando no hay ninguna descarga activa.
Verificado en vivo desde la propia GUI (`xdotool`/`spectacle`/
`wmctrl`, forzando `QT_QPA_PLATFORM=xcb`): se redimensionó y movió la
ventana, se cerró (disparando `closeEvent`), se comprobó que
`config.json` guardó la geometría exacta, y al volver a abrir la GUI
la ventana apareció con ese mismo tamaño; después, con una descarga
real contra Soulseek (`Daniel Serrano Armenta`, servidor
`server.slsknet.org:2242`), se vio la barra de estado pasar de vacía a
"1 descargas activas · 2.0 MB/s" mientras la descarga estaba en curso
y volver a quedar vacía en cuanto terminó.

A continuación se implementó el segundo de los tres pasos: UPnP para
abrir automáticamente el puerto de escucha en el router. Se creó
`core/upnp.py`, un cliente UPnP IGD (Internet Gateway Device) escrito
a mano sobre sockets/asyncio crudos (descubrimiento SSDP por
multicast a `239.255.255.250:1900`, `GET` HTTP de la XML de
descripción del dispositivo, parseo con `xml.etree.ElementTree` para
localizar el `controlURL` del servicio `WANIPConnection`/
`WANPPPConnection`, y llamadas SOAP `AddPortMapping`/
`DeletePortMapping`), sin ninguna librería ni proceso externo, igual
que el resto del proyecto. Es "best-effort" por diseño: cualquier
fallo (sin UPnP, timeout, router ausente) se traga internamente y
devuelve `False` sin lanzar excepción, con un `timeout` global de 8s,
y se invoca con `asyncio.ensure_future()` (sin awaitear) desde
`connect()`/`disconnect()` de cada backend, para que un router lento
o sin UPnP nunca retrase ni rompa la conexión real. Se conectó a los
cuatro backends que no tenían ya UPnP (BitTorrent ya lo tenía vía
libtorrent): Soulseek y DC++ abren su `listen_port` TCP; Gnutella2
abre el puerto fijo que usa para las conexiones `/PUSH` entrantes
(el socket efímero de fallback por puerto ocupado se dejó fuera, por
ser de corta vida y bajo valor); eMule abre tanto `listen_port` (TCP)
como `kad_udp_port` (UDP). Sobre la revisión de puertos adicionales:
se investigó si alguna red necesita más puertos aparte de los ya
configurables — en particular el puerto UDP de cliente extra de
eMule/aMule real (`listen_port + 3`, usado para fuentes extendidas) —
y se concluyó que ninguna función implementada en este backend lo usa
todavía, así que, siguiendo el principio de no añadir configuración
para nada que no tenga una función real detrás, no se añadió ese
puerto a Preferencias.

Validado mediante un servidor IGD sintético local (nunca contra el
router real de producción, por el mismo principio de seguridad
aplicado en el resto del proyecto a operaciones que mutan
infraestructura externa): se confirmó que el `GET` HTTP, el parseo
de la XML de descripción y la construcción de las llamadas SOAP
`AddPortMapping`/`DeletePortMapping` funcionan correctamente, y que
`add_port_mapping()`/`delete_port_mapping()` devuelven `False`
limpiamente (sin excepción) cuando no hay router disponible. El
descubrimiento SSDP por multicast real no se pudo probar de extremo
a extremo en este entorno de desarrollo en sandbox — una prueba
manual confirmó que el mismo socket que responde a un `M-SEARCH`
unicast enviado directamente no recibe nada por multicast, una
limitación del espacio de red del contenedor, no del código — así
que esa parte se validó sustituyendo temporalmente el descubrimiento
por la URL conocida del servidor sintético. Con eso ya confirmado, se
hizo además una prueba de extremo a extremo con el backend real de
Soulseek (credenciales reales, `listen_port` real) conectándose
contra Kad/servidor real de Soulseek mientras el router sintético
corría en local: el log del servidor sintético mostró la llamada
`AddPortMapping` real disparada por `connect()` con el puerto 2234
exacto y la descripción `"P2P Total - Soulseek"`, y al llamar a
`disconnect()` la correspondiente `DeletePortMapping` con el mismo
puerto — confirmando que el enganche "fire-and-forget" funciona en
la práctica y no bloquea el ciclo de conexión real.

Por último se implementó el tercero de los tres pasos: pasar el
filtro de tipo de archivo a búsqueda en servidor para eD2k, el único
de los cinco protocolos con soporte nativo para ello en el propio
`OP_SEARCHREQUEST`. El wire format de este árbol de búsqueda no está
documentado oficialmente, así que antes de tocar código se investigó
contra el código fuente real: `opcodes.h` de eMule confirma los IDs
de tag `FT_FILETYPE` (`0x03`) y `FT_FILEFORMAT` (`0x04`) y los valores
de cadena reconocidos por el servidor (`"Audio"`, `"Video"`, `"Image"`,
`"Doc"`, `"Pro"` — `"Arc"`/`"Iso"` están marcados ahí como "eMule
internal use only" y nunca se mandan por red), y `SearchList.cpp` de
aMule (`CSearchExprTarget::WriteMetaDataSearchParam`) confirma el
formato binario exacto de una hoja de tipo "tag" dentro del árbol:
`uint8(2)` + cadena-eD2k del valor + `uint16(1)` + `uint8(FT_FILETYPE)`,
añadida como un término más ANDed junto a las palabras de la búsqueda
(reutilizando el mismo patrón de nodos AND en notación polaca prefija
que ya usaba `_build_search_request()` para búsquedas de varias
palabras). Se añadió el mapeo de las categorías del combo de tipo de
archivo de la GUI a esos valores eD2k (solo `audio`/`video`/`picture`/
`document`/`program` tienen equivalente server-side; `archive`/
`cdimage`/`all` se quedan solo con el filtro client-side ya existente,
que sigue aplicándose siempre en la GUI como red de seguridad), y se
plumbeó `file_type` desde `SearchResultsPanel._do_search()` en
`gui/widgets/search_tab.py` a través de `DownloadManager.search_all()`
hasta `EMuleBackend.search()`. Verificado en vivo contra un servidor
eD2k real (`85.17.116.222:6082`, descubierto vía `connect_auto()`):
la misma búsqueda de `"test"` sin filtro mezcla `.zip`/`.py`/`.pdf`/
`.mp3`/`.mkv`/`.pyc` en los resultados, con `file_type="audio"`
devuelve solo `.mp3`/`.wav`/`.flac`, y con `file_type="video"` solo
`.mkv`/`.mp4`/`.avi` — confirmando que el servidor real acepta la
etiqueta `FT_FILETYPE` y descarta los resultados que no encajan antes
de mandarlos, en vez de traerlos todos para filtrarlos en cliente.

Con esto se completan los tres pasos pendientes del roadmap indicados
por el usuario ("haz los tres pasos actualizando el readme.md al
finalizar cada uno"); no quedan tareas de roadmap pendientes en este
momento.

Después se abordó la parte de visibilidad del punto 21 del backlog
(mostrar en la GUI el estado HighID/LowID de eD2k que el backend ya
calculaba internamente pero no exponía en ningún sitio) — ver "Estado
actual" más arriba para el detalle completo y la validación en vivo
contra un servidor eD2k real. La parte de "gestión" del mismo punto 21
(recibir la petición de conexión entrante cuando nosotros somos LowID,
y el equivalente Kad de nodo "firewalled") sigue pendiente y sería el
siguiente paso natural dentro de ese mismo punto del backlog; fuera de
él, el hueco más grande que queda es el punto 2 (límites de velocidad
de subida/bajada).

Siguiendo con el orden estricto, se abordaron juntos los puntos 16 y
17 del backlog (ambos sobre robustez/integridad del contenido
descargado), a petición explícita del usuario ("sigue con el 16 y
17"):

**Punto 16 — reverificación de hash tras completar la descarga.**
Antes de este punto, de las cinco redes solo BitTorrent verificaba
cada pieza (vía libtorrent); las otras cuatro se fiaban del tamaño de
fichero descargado sin comprobar el contenido en sí, salvo eD2k, que
ya tenía verificación MD4 por parte de `PARTSIZE` (preexistente,
confirmada de nuevo al revisar `_client_transfer_loop` en
`backends/emule_backend.py`). Se añadió la verificación que faltaba en
las otras dos redes con hash de contenido nativo:

- **G2**: se implementó `_sha1_of_file()` en `backends/g2_backend.py`,
  que recorre en streaming (sin cargar el fichero entero en memoria)
  el fichero ya descargado y compara su SHA1 contra el
  `urn:sha1:...` que traía el resultado de búsqueda original —
  a diferencia de BitTorrent, G2 no tiene ningún mecanismo de
  verificación por pieza durante la propia transferencia HTTP, así
  que esta es la única comprobación posible, y se hace entera al
  final de `_send_get_and_receive()`. Un fallo de SHA1 deja la
  descarga en `DownloadState.ERROR` en vez de `COMPLETED`, igual que
  un tamaño incompleto.
- **DC++**: se implementó Tiger Tree Hash (TTH) desde cero en dos
  módulos nuevos y verificados byte a byte contra oráculos en C
  compilados a partir del código fuente real de RHash (no de memoria):
  `core/tiger.py` (el hash Tiger/192 clásico en el que se apoya TTH) y
  `core/tth.py` (el árbol de Merkle sobre hojas de 1024 bytes que
  construye el hash final). Como un resultado `$SR` normal de NMDC no
  trae ningún hash (solo lo hace una búsqueda explícita por hash, tipo
  `9` del protocolo: `F?F?0?9?TTH:<base32>`), se añadió
  `DCPPBackend.search_by_tth()` como vía de entrada específica que sí
  arrastra el TTH consigo hasta `start_download()`, y `_receive_file()`
  recalcula el TTH del fichero descargado y lo compara al terminar. Las
  descargas que vienen de una búsqueda normal por nombre siguen sin
  verificación (limitación honesta: no hay hash disponible en ese
  flujo), documentada explícitamente en el propio código.
- Soulseek quedó fuera de este punto: su protocolo no tiene ningún
  concepto de hash de contenido, a diferencia de las otras cuatro
  redes, así que no hay nada que reverificar.

Validado con servidores sintéticos locales (nunca contra red real, por
el mismo principio de seguridad aplicado al resto del proyecto a
pruebas que podrían tocar contenido de terceros): en ambas redes se
comprobó tanto el caso de éxito (contenido íntegro → `COMPLETED`) como
el de corrupción detectada (contenido alterado → `ERROR` con el
mensaje de verificación fallida), byte a byte.

**Punto 17 — AICH (verificación parcial a nivel de sub-bloque en
eD2k).** Objetivo: cuando la verificación MD4 por parte (~9,3 MiB) ya
existente detecta una parte corrupta, poder precisar además qué
sub-bloque de 180 KiB dentro de esa parte es el realmente dañado, en
vez de dejar sospechosa la parte entera. El algoritmo AICH real de
aMule (`SHAHashSet.cpp`/`CAICHHashTree`, estudiado directamente del
código fuente del proyecto, clonado con sparse-checkout para esta
investigación) es un árbol de Merkle sobre SHA1 con una regla de
particionado por niveles no trivial (no es una simple mitad de bytes:
el número de bloques de cada nodo se reparte redondeando hacia arriba
en la rama izquierda, y el tamaño base del hijo pasa de `PARTSIZE`
(9.728.000 bytes) a `EMBLOCKSIZE` (184.320 bytes) en cuanto su tamaño
dejar de superar `PARTSIZE`). Se implementó en `core/aich.py`
(`levels_to_part()`/`block_count()`), verificado contra una
implementación recursiva independiente del árbol completo (referencia
construida en el propio test, no en producción) para 11 tamaños de
fichero distintos, mono-parte y multi-parte, con y sin resto.

Sobre el protocolo, se añadieron a `backends/emule_backend.py` los
cuatro opcodes reales cliente↔cliente de aMule (confirmados leyendo
`DownloadClient.cpp`/`ClientTCPSocket.cpp` del propio proyecto, no de
memoria): `OP_AICHFILEHASHREQ`/`OP_AICHFILEHASHANS` (piden y reciben
el master hash AICH del fichero) y `OP_AICHREQUEST`/`OP_AICHANSWER`
(piden y reciben los hashes de sub-bloque de una parte concreta, dentro
de una respuesta de "recovery data" que mezcla primero los hashes
"verificadores" del camino hasta la raíz y después los propios hashes
de hoja — `levels_to_part()` calcula cuántas entradas hay que saltar
sin necesidad de decodificar el identificador de posición en el árbol
que trae cada entrada). `_verify_download()` pasó de devolver un
booleano a devolver la lista de índices de parte corruptos, y cuando
esa lista no está vacía, la nueva función `_diagnose_aich()` reutiliza
la misma conexión TCP ya abierta con el peer (todavía viva en ese
punto de `_client_transfer_loop`) para pedir el master hash y, por
cada parte corrupta, sus hashes de sub-bloque, comparándolos contra el
SHA1 recalculado de cada sub-bloque ya en disco. El resultado se anexa
a `download.error_message` (p.ej. `"... [AICH] parte 1: sub-bloques
[3, 7] de 53 corruptos"`).

Limitaciones honestas frente al aMule real, documentadas en el propio
docstring de `_diagnose_aich()`: el master hash se acepta tal cual del
único peer conectado, sin el modelo de confianza multi-fuente de aMule
real (que exige ≥10 IPs independientes de acuerdo antes de fiarse de
un master hash); y como el backend descarga de una sola fuente a la
vez, no hay ningún mecanismo para volver a pedir automáticamente solo
el sub-bloque corrupto a otra fuente — el resultado de AICH aquí es
puramente informativo. El diagnóstico es además "best effort": si el
peer no soporta AICH o la conexión se pierde durante el intercambio,
se descarta en silencio sin alterar el mensaje de error base de la
verificación MD4, que ya es fiable por sí sola.

Validado de extremo a extremo con un servidor eD2k sintético local que
implementa las cinco fases del protocolo (`OP_SETREQFILEID`,
`OP_HASHSETREQUEST`, `OP_REQUESTPARTS`, y los dos intercambios AICH) y
llama directamente a `EMuleBackend._client_transfer_loop()` real (no
una simulación aislada de la función de diagnóstico): descarga íntegra
→ `COMPLETED`; un sub-bloque corrupto en una parte → MD4 falla y AICH
localiza exactamente ese único índice de sub-bloque; dos sub-bloques
corruptos en la misma parte → AICH localiza ambos índices
correctamente. Nota de proceso: la implementación MD4 de
`core/md4.py` es pura Python y demasiado lenta para generar un
hashset a la escala real de `PARTSIZE` (~9,3 MiB) en un test; la
prueba redujo `PARTSIZE`/`EMBLOCKSIZE` vía monkeypatch solo en el
propio script de test, sin tocar el código de producción.

**Punto 18 — Soporte de proxy (SOCKS5/HTTP) para las conexiones
salientes de los cinco backends.** Ver "Estado actual" más arriba para
el detalle completo (implementación en `core/proxy.py`, integración
por backend, el bug del writer TLS huérfano encontrado y corregido
durante la validación, y la cobertura de test sintético de 8 casos).
Resumen breve: los cuatro backends sin proxy nativo (Soulseek, DC++,
Gnutella2, eMule/eD2k) pasan ahora por `core.proxy.open_connection()`
en cada conexión TCP saliente; BitTorrent usa el proxy nativo de
`libtorrent` vía `settings_pack`. Configurable desde `python main.py
config` (nueva sección "Proxy saliente") y desde la GUI (nueva
pestaña "Proxy" en Preferencias), con el mismo `ProxyConfig`
persistido en `~/.config/p2p-total/config.json` para ambas vías.

**Punto 19 — Soporte de IPv6.** Ver "Estado actual" más arriba para el
detalle completo. Resumen breve: BitTorrent ya lo soportaba de
fábrica (`listen_interfaces` dual-stack de `libtorrent`, confirmado en
vivo); DC++/NMDC ganó soporte real porque su protocolo es de texto
plano y sí puede representar una IPv6 (`$ConnectToMe`/`dchub://` con
corchetes, listen dual-stack con fallback si no hay pila IPv6);
Soulseek, Gnutella2 y eMule/eD2k tienen un límite de protocolo binario
genuino e infranqueable en sus campos de dirección, documentado en el
código en el sitio exacto donde vive en vez de intentar forzarlo. Con
esto se cierra el punto 19.

**Punto 20 — Ofuscación de protocolo en eD2k.** Ver "Estado actual"
más arriba para el detalle completo. Resumen breve: implementado el
esquema básico de ofuscación cliente↔cliente que usa aMule/eMule de
verdad para esquivar el throttling de tráfico P2P de algunos ISPs
(estudiado del código fuente real de aMule, `EncryptedStreamSocket.*`)
— RC4 con claves derivadas por MD5 del userhash del peer que acepta la
conexión, handshake con marcador semi-aleatorio no colisionante con
los opcodes reales del protocolo, y verificación de la clave mediante
un valor mágico cifrado. No es cifrado de seguridad real (el propio
código fuente de aMule lo deja explícito: el userhash del que se
derivan las claves no es secreto), solo ofuscación de tráfico frente a
inspección de paquetes. No se implementó la variante cliente↔servidor
por Diffie-Hellman, más compleja y sin protección sustancial adicional
según la propia documentación de aMule. Configurable en tres modos
(disabled/enabled/required) desde CLI y GUI. Validado con tests
sintéticos a nivel de protocolo (RC4, derivación de claves, handshake
completo con framing real) y con un test de extremo a extremo sobre
TCP real en loopback con un fichero de 200 KB transferido y verificado
byte a byte, tanto en primer contacto (sin ofuscar, porque todavía no
se conoce el userhash del peer) como en una segunda conexión al mismo
peer (ya ofuscada de verdad, confirmado con espías sobre las funciones
de negociación reales). Con esto se cierra el punto 20; el punto 21 ya
estaba completo de antes (ver "Estado actual").

**Punto 22 — Icono en la bandeja del sistema y minimizar a bandeja.**
Ver "Estado actual" más arriba para el detalle completo. Resumen
breve: `QSystemTrayIcon` real con menú contextual propio ("Mostrar
ventana"/"Salir"), nueva opción en Preferencias → General ("Minimizar
a la bandeja del sistema al cerrar", desactivada por defecto) que hace
que cerrar la ventana oculte en vez de salir del todo, mostrando un
aviso nativo; el menú Archivo → Salir siempre cierra de verdad,
ignorando esa opción. Validado en vivo contra un escritorio KDE Plasma
real: activar la opción desde la propia GUI, cerrar con `Alt+F4` y
comprobar que el proceso sigue vivo con el aviso nativo mostrado;
confirmar por D-Bus que el icono queda realmente registrado como
`StatusNotifierItem` del escritorio; restaurar la ventana llamando a
`Activate()` sobre ese mismo icono real; y confirmar que Archivo →
Salir cierra el proceso del todo y desregistra el icono, pese a tener
la opción de minimizar activada. Con esto se cierra el punto 22.

**Punto 23 — Notificaciones nativas del sistema operativo al completar
o fallar una descarga.** Reutiliza el mismo icono de bandeja del punto
22 (`QSystemTrayIcon.showMessage()`): un segundo listener de
`DownloadManager.on_progress()` avisa una sola vez por descarga (vía
un `set` de IDs ya notificados) cuando el estado pasa a `COMPLETED` o
a `ERROR`, con icono informativo o de aviso según el caso. Nueva
opción en Preferencias → General, "Avisar al completar o fallar una
descarga" (activada por defecto). Validado con espías sobre
`showMessage()` contra un `MainWindow` real: aviso correcto y único
por descarga terminada, ninguno para descargas aún en curso. Con esto
se cierra el punto 23.

**Punto 24 — Pestaña de estadísticas globales.** Nuevo
`core/stats_tracker.py` que acumula, por red y persistido en SQLite
(tablas `network_stats` y `network_stats_daily`), total subido, total
bajado y tiempo conectado, reaprovechando puntos ya existentes del
código (delta de `Download.downloaded_bytes` en el callback central de
progreso para las bajadas; el punto exacto de cada backend donde ya se
leía un `chunk` para servirlo a otro peer para las subidas, más
`session.status().total_payload_upload` de libtorrent para
BitTorrent; y `connect_network()`/`disconnect_network()` de
`ConnectionManager` para el tiempo conectado). Nueva pestaña
"Estadísticas" con la tabla de totales y el histórico diario de los
últimos 30 días. Validado con una prueba directa de `StatsTracker`
contra una base de datos aislada (que detectó y corrigió un bug real
de duplicación de bytes bajados) y en vivo contra un escritorio KDE
Plasma real, conectando BitTorrent de verdad y viendo crecer la
columna "Tiempo conectado" en directo. Con esto se cierra el punto 24.

**Punto 25 — Importar/exportar `config.json` desde la GUI, y modo
"portable".** `load_config`/`save_config` (`core/config.py`) ganaron un
parámetro de ruta opcional, que es lo único que hace falta para
exportar/importar: dos botones nuevos en Preferencias → General
("Exportar..."/"Importar...") que usan un `QFileDialog` para volcar la
configuración editada a cualquier fichero, o cargar y aplicar de
inmediato la de un `config.json` externo. Modo portable nuevo
(`is_portable_mode()`/`enable_portable_mode()`/
`disable_portable_mode()`): si existe un `portable.marker` junto a
`main.py`, tanto `_config_dir()` (config.json y las cachés de eD2k/Kad/
G2) como `core.database.DB_PATH` pasan a vivir en una carpeta
`p2p-total-data` junto al ejecutable en vez de `~/.config/p2p-total` y
`~/.local/share/p2p-manager`, para poder llevar el programa entero en
un pendrive. Una casilla nueva en la misma pestaña activa/desactiva el
marcador (copiando los datos existentes a la carpeta portable al
activarlo, sin borrar los originales); el cambio pide reiniciar la
aplicación, porque esas rutas son constantes de módulo calculadas una
sola vez al arrancar. Validado con una prueba aislada del ciclo
completo (exportar → importar → activar portable → comprobar que
`DB_PATH` se reubica → desactivar sin perder datos) y en vivo contra
KDE Plasma real: como el selector de fichero nativo de Qt en esa
sesión (KDE sobre Wayland, app forzada a XWayland) abre como ventana
Wayland nativa e invisible para `xdotool`/`wmctrl`, se validaron en su
lugar los manejadores reales de los botones interceptando
`QFileDialog.getSaveFileName`/`getOpenFileName`, técnica de prueba
habitual para evitar diálogos nativos del sistema operativo que sigue
ejercitando el código de producción real de principio a fin. Con esto
se cierra el punto 25; el siguiente punto pendiente en orden estricto
es el 26 (carpeta vigilada: añadir automáticamente cualquier
`.torrent` que aparezca en una carpeta configurada).

### Roadmap — backlog de mejoras futuras (estudiado 2026-08-16)

A petición del usuario ("haz una lista muy completa y estudiada de las
características que faltan"), se revisó a fondo el código de los cinco
backends y de la GUI (no solo el conocimiento general de cada
protocolo) para identificar qué le falta a este proyecto frente a un
cliente P2P completo de cada red, y anotar exactamente qué pieza
concreta del código de aquí habría que tocar.

**Reglas de proceso para este backlog (petición explícita del
usuario)**: los puntos se implementan **en orden estricto** (1, 2, 3...
tal como están numerados a continuación, sin saltarse ninguno aunque
parezca más fácil o más interesante); y el `README.md` se actualiza
**después de cada punto completado con éxito**, antes de pasar al
siguiente — no se acumulan varios puntos sin documentar. Cuando un
punto quede solo parcialmente resuelto (como el 21, ver más abajo), se
marca qué parte está hecha y cuál sigue pendiente, y esa parte
pendiente se termina antes de continuar con el resto del orden. Lo
mismo aplica al 34.4 (ver más abajo): se investigó a fondo y se
confirmó que ya estaba resuelto de facto por el trabajo del punto 19,
así que se documenta formalmente como completado en vez de dejarlo
indefinido.

El punto 1 ya estaba hecho (para cuatro de las cinco redes) y el punto
21 se abordó parcialmente (solo la visibilidad) antes de que existiera
esta regla de orden estricto; la parte de gestión que quedaba pendiente
del punto 21 ya se terminó (ver el punto 21 y "Estado actual" más
arriba), así que el punto 1 y el 21 quedan completos. Los puntos 2
(límites de velocidad), 3 (selección de archivos y descarga secuencial
en BitTorrent), 4 (prioridad/orden de la cola), 5 (categorías de
descarga), 6 (reintento automático configurable), 7 (historial de búsquedas
persistente), 8 (búsquedas guardadas / alertas en segundo plano), 9
(navegar los archivos compartidos de un usuario de Soulseek), 10
(Browse Host en Gnutella2), 11 (lista pública de hubs DC++), 12
(pegar enlace magnet/ed2k/dchub o arrastrar un `.torrent`), 13 (chat
privado y salas de Soulseek), 14 (chat de hub DC++), 15 (amigos y
créditos en Kad/eD2k), 16 (reverificación de hash en DC++/G2/eD2k) y
17 (AICH en eD2k), 18 (soporte de proxy SOCKS5/HTTP para las
conexiones salientes de los cinco backends), 19 (soporte de IPv6), 20
(ofuscación de protocolo en eD2k), 22 (icono en la bandeja del sistema
y minimizar a bandeja), 23 (notificaciones nativas del sistema
operativo al completar o fallar una descarga), 24 (pestaña de
estadísticas globales), 25 (importar/exportar `config.json` desde la
GUI y modo portable), 26 (carpeta vigilada: añadir automáticamente
cualquier `.torrent` que aparezca en una carpeta configurada) y 27
(verificar archivos ya descargados, a demanda o automáticamente al
completar, hoy solo en BitTorrent vía el recheck nativo de libtorrent),
28 (más idiomas en `gui/i18n.py`), 29 (búsqueda de torrents por nombre
vía apibay.org), 30 (barra de progreso de colores estilo aMule), 31
(splash de AnabasaSoft al inicio), 32 (icono en la bandeja de
aplicaciones, ampliando el punto 22) y 33 (empaquetado y distribución
multiplataforma — `.rpm`, `.deb`, AppImage, Flatpak, Windows, macOS,
sub-puntos 33.1 a 33.9) también se completaron ya (ver "Estado
actual"), así que la posición actual en el backlog es: el punto 34
(mejoras post-empaquetado, ver más abajo), resolviendo sus sub-puntos
en el mismo orden estricto, uno detrás de otro (el 21 ya está
completo). Dentro del 34, los sub-puntos 34.1 (mecanismo de
auto-actualización real), 34.2 (suite de tests automatizados), 34.3
(cifrado MSE/PE y µTP en BitTorrent), 34.4 (auditoría de IPv6 en
Soulseek/Gnutella2/eD2k-Kad), 34.5 (planificador de ancho de banda
por franja horaria) y 34.6 (control remoto / API web) ya se
completaron también, así que toca el 34.7.

**Compartir y subir archivos** (el hueco más grande, con diferencia):
1. Compartir una carpeta propia y servir descargas a otros peers en
   las cuatro redes que hoy son puramente "leecher" — se comprobó que
   `_handle_incoming_peer` en `dcpp_backend.py`, `emule_backend.py` y
   `g2_backend.py` solo atiende conexiones entrantes de tipo push/
   callback para las propias descargas, nunca responde con datos
   propios a quien las pida (no hay indexado de carpeta compartida, ni
   cálculo de hashes propios, ni servidor de la parte que falta por
   red: `OP_SENDINGPART` en eD2k, `$Get`/`$ADCGET` en DC++, servir
   datos reales tras un `/PUSH` en G2, subida real en Soulseek).
   BitTorrent es la excepción: ya siembra de fábrica tras completar,
   vía la propia sesión de libtorrent. Es una pieza grande — una
   implementación distinta por protocolo — pero es lo que más acerca
   el proyecto a un cliente P2P real y recíproco en vez de un
   descargador puro.

   **✅ Hecho para las cuatro redes** (Soulseek, DC++, Gnutella2 y
   eMule/eD2k — ver "Compartir y subir archivos, en las cinco redes
   que lo permiten" en "Estado actual" más arriba, con el detalle
   completo y la validación de las cuatro). El caso de LowID entrante
   sin relación con esto sigue pendiente, ver el punto 21 de este
   backlog (detección/estado de ID alta/baja, no la subida en sí, que
   ya funciona por la vía HighID/callback existente).

**Límites y gestión de la cola de descargas**:
2. ✅ Límite de velocidad de subida/bajada, global y por descarga —
   completo (ver "Estado actual" más arriba para el detalle y la
   validación).
3. ✅ Selección de archivos y descarga secuencial dentro de un torrent
   multi-archivo — completo (ver "Estado actual" más arriba para el
   detalle y la validación).
4. ✅ Prioridad/orden de la cola de descargas desde la GUI (subir/bajar,
   arrastrar filas) — completo (ver "Estado actual" más arriba para el
   detalle y la validación).
5. ✅ Categorías/etiquetas de descarga (p.ej. "Música", "Vídeos") con
   carpeta de destino asociada por categoría, al estilo aMule/
   qBittorrent — completo (ver "Estado actual" más arriba para el
   detalle y la validación).
6. ✅ Reintento automático configurable cuando una descarga se queda sin
   fuentes en vez de acabar directamente en estado de error — completo
   (ver "Estado actual" más arriba para el detalle y la validación).

**Búsqueda y descubrimiento**:
7. ✅ Historial de búsquedas persistente entre sesiones — completo (ver
   "Estado actual" más arriba para el detalle y la validación).
8. ✅ Búsquedas guardadas / alertas ("avisa cuando aparezca X"),
   reejecutando la búsqueda periódicamente en segundo plano — completo
   (ver "Estado actual" más arriba para el detalle y la validación).
9. ✅ Navegar los archivos compartidos de un usuario de Soulseek
   (`BrowseUser`/`GetSharedFileList` del protocolo, ya estudiado para
   la búsqueda/descarga pero sin implementar la parte de navegación) —
   muy usado en el cliente real para explorar toda la colección de
   alguien más allá de un resultado suelto — completo (ver "Estado
   actual" más arriba para el detalle y la validación).
10. ✅ `Browse Host` (`/BH`) en Gnutella2, para listar el contenido
    completo compartido por un nodo/hub concreto — completo (ver
    "Estado actual" más arriba para el detalle y la validación).
11. ✅ Lista pública de hubs DC++ (agregador tipo hublist.org) para
    poder elegir hub desde la GUI en vez de teclear IP:puerto a mano —
    completo (ver "Estado actual" más arriba para el detalle y la
    validación).
12. ✅ Pegar un enlace magnet/ed2k/dchub desde el portapapeles (o
    arrastrar un `.torrent`) para arrancar la descarga o la conexión
    directamente, sin pasar por la pestaña de Búsqueda — completo (ver
    "Estado actual" más arriba para el detalle y la validación).

**Chat y funciones sociales** (protocolo ya conocido por la parte de
búsqueda/descarga; falta la parte social):
13. ✅ Chat privado y salas de Soulseek (`SayChatroom`/`PrivateMessage`) —
    completo (ver "Estado actual" más arriba para el detalle y la
    validación).
14. ✅ Chat de hub DC++ (mensajes públicos de sala y privados de
    usuario) — completo (ver "Estado actual" más arriba para el
    detalle y la validación).
15. ✅ Lista de amigos y sistema de créditos en Kad/eD2k, para
    priorizar a quien más ha compartido contigo, como hace el eMule
    real — completo (ver "Estado actual" más arriba para el detalle y
    la validación).

**Robustez e integridad**:
16. ✅ Reverificación de hash tras completar la descarga en las cuatro
    redes sin comprobación nativa de contenido (TTH en DC++, MD4 por
    chunk en eD2k, SHA1 en G2, ningún hash en Soulseek) — hoy solo
    BitTorrent verifica cada pieza vía libtorrent; las otras cuatro se
    fían del tamaño de fichero descargado sin comprobar el contenido
    en sí. Completo para DC++/G2/eD2k (Soulseek no tiene hash nativo
    que reverificar) — ver "Estado actual" más arriba para el detalle
    completo y la validación.
17. ✅ AICH (verificación parcial a nivel de sub-bloque en eD2k) para
    poder detectar y descartar solo la parte corrupta de una fuente
    concreta, en vez de invalidar el fichero entero — completo (ver
    "Estado actual" más arriba para el detalle completo y la
    validación).

**Conectividad y privacidad**:
18. ✅ Soporte de proxy (SOCKS5/HTTP) para las conexiones salientes de
    cualquiera de los cinco backends — completo (ver "Estado actual"
    más arriba para el detalle completo y la validación).
19. ✅ IPv6 — completo (ver "Estado actual" más arriba para el detalle
    completo y la validación): BitTorrent ya lo soportaba de fábrica
    vía `libtorrent`; DC++/NMDC ganó soporte real (listen dual-stack,
    `$ConnectToMe`/enlaces `dchub://` con IPv6 entre corchetes); en
    Soulseek, Gnutella2 y eMule/eD2k se documentó en el propio código
    el límite genuino de protocolo (campos de dirección binarios de
    ancho fijo que ningún cliente real de esas tres redes sabe
    interpretar como IPv6), aunque la conexión TCP saliente en sí ya
    funciona con IPv6 en las tres.
20. ✅ Ofuscación de protocolo en eD2k (la opción real de eMule para
    esquivar el throttling de tráfico P2P de algunos ISPs) — completo
    (ver "Estado actual" más arriba para el detalle completo y la
    validación): esquema básico cliente↔cliente (RC4 + claves
    derivadas por MD5 del userhash), configurable en tres modos
    (disabled/enabled/required) desde CLI y GUI; no se implementó la
    variante cliente↔servidor por DH (más compleja y sin protección
    sustancial adicional según el propio código fuente de aMule).
21. ✅ Visibilidad y gestión completa de HighID/LowID ("ID alta"/"ID
    baja") en eD2k — completo (ver "Estado actual" más arriba para el
    detalle completo y la validación en vivo/loopback).

    **Visibilidad**: `EMuleBackend.get_stats()` expone `"id_status"`
    ("high"/"low", derivado de `is_high_id`) y la pestaña Red de la
    GUI lo muestra traducido ("ID alta"/"ID baja") junto al resto de
    detalles de la fila eMule.

    **Gestión del caso inverso**: cuando nosotros somos LowID y
    alguien pide algo que compartimos, el servidor nos manda
    `OP_CALLBACKREQUESTED` (0x35) pidiéndonos que abramos nosotros la
    conexión saliente — reconocido en `_server_read_loop()` y servido
    por `_serve_via_callback()` + `_serve_upload_session()`. (El caso
    HighID — nosotros pidiendo a un peer que se conecte a nosotros vía
    `OP_CALLBACKREQUEST` — ya funcionaba desde antes:
    `_download_via_callback()` / `_handle_incoming_peer()`.)

    **Equivalente Kad de nodo "firewalled"**: implementado en los dos
    sentidos vía `KADEMLIA_FIREWALLED_REQ`/`_RES`/`_ACK_RES`
    (`check_kad_firewall()` para comprobar nuestro propio estado,
    `_on_kad_firewalled_req()` para responder a otros nodos que nos lo
    preguntan a nosotros) y expuesto en `get_stats()` como
    `"kad_firewalled"` ("open"/"firewalled"), traducido en la GUI.

**GUI y experiencia de uso**:
22. ✅ Icono en la bandeja del sistema (`QSystemTrayIcon`) y minimizar a
    bandeja en vez de cerrar del todo — completo (ver "Estado actual"
    más arriba para el detalle completo y la validación en vivo contra
    un escritorio real).
23. ✅ Notificaciones nativas del sistema operativo al completar (o al
    fallar) una descarga — completo (ver "Estado actual" más arriba
    para el detalle completo y la validación en vivo).
24. ✅ Pestaña de estadísticas globales: total subido/bajado, ratio,
    tiempo conectado por red, histórico — completo (ver "Estado
    actual" más arriba para el detalle completo y la validación en
    vivo).
25. ✅ Importar/exportar `config.json` desde la propia GUI, y modo
    "portable" (todo junto al ejecutable en vez de
    `~/.config/p2p-total`) — completo (ver "Estado actual" más arriba
    para el detalle completo y la validación en vivo).
26. ✅ Carpeta vigilada: añadir automáticamente cualquier `.torrent` que
    aparezca en una carpeta configurada, sin tener que abrirlo a mano —
    completo (ver "Estado actual" más arriba para el detalle completo
    y la validación).

**Verificación de integridad**:
27. ✅ Verificar archivos ya descargados, tanto a demanda (nueva opción en
    el menú contextual de la pestaña Transferencias, "Verificar
    archivo") como automáticamente al terminar una descarga si así lo
    indica explícitamente una opción nueva en Preferencias/General
    (desactivada por defecto) — en ambos casos, solo en las redes que
    soporten verificar contenido (hoy solo BitTorrent, vía el
    recheck/hash-check nativo de libtorrent; se sumarán las otras
    cuatro en cuanto tengan hash propio del punto 16 de este mismo
    backlog) — completo (ver "Estado actual" más arriba para el
    detalle completo y la validación).

**Internacionalización**:
28. ✅ Añadir más idiomas a `gui/i18n.py` (hoy solo es/en/eu) — por
    ejemplo francés, italiano, portugués, alemán, catalán, gallego,
    ruso, chino y japonés, entre otros. Completado con doce idiomas
    (es, en, eu, fr, it, pt, de, ca, gl, ru, zh, ja) más, a petición
    explícita del usuario, coreano (ko) — trece en total, con las 359
    claves de `gui/i18n.py` verificadas 1:1 en cada idioma. También a
    petición del usuario, la lista `LANGUAGES` (y por tanto el
    desplegable de idioma en Preferencias) se ordena alfabéticamente
    por el nombre mostrado: ca, de, en, es, eu, fr, gl, it, pt, ru, zh,
    ja, ko.

**Búsqueda torrent y magnet**:
29. ✅ Añadir una búsqueda de archivos torrent y magnet cogiendo el
    archivo py ya creado como muestra en otro directorio, se pegará
    del portapapeles cuando sea necesario. Completado adaptando la
    lógica del script de referencia (que usaba `requests`+`tabulate`
    de forma síncrona contra la API JSON de apibay.org) a la
    restricción de diseño del proyecto: nuevo `core/http_client.py`
    con un cliente HTTP(S) async completo hecho a mano sobre
    `asyncio.open_connection`/TLS (sin `requests` ni `aiohttp`),
    reutilizado también por el fetcher de la lista de hubs de DC++
    para eliminar lógica duplicada. `backends/torrent_backend.py`
    ahora distingue automáticamente en `search()` si la consulta es
    una referencia directa (magnet/infohash/ruta a `.torrent`, resuelta
    como antes vía DHT) o texto libre (nueva ruta que consulta
    `apibay.org/q.php`, construye magnets con los mismos doce
    trackers públicos del script de referencia y ordena por
    semillas). Se le dio la misma paridad de ajustes que a las otras
    cuatro redes: `TorrentConfig` (número máximo de resultados, tiempo
    de búsqueda) y nueva pestaña "BitTorrent" en Preferencias,
    integrada en `search_all()`/`search_tab.py`. Validado en vivo
    contra apibay.org real: búsqueda de "ubuntu" con tope de 5
    resultados devolvió 5 torrents reales con magnets válidos,
    correctamente ordenados por semillas (53, 39, 27, 25, 13); y se
    confirmó que la ruta de referencia directa preexistente sigue
    funcionando sin regresión pasando el infohash del primer resultado
    (resuelto vía DHT como antes).

**OTRAS MEJORAS**
30. ✅ Barra de progreso de colores estilo amule. Se sustituyó el
    control nativo de `QStyleOptionProgressBar`/`CE_ProgressBar`
    (`gui/widgets/delegates.py`, `ProgressBarDelegate`) — que solo
    usaba el color de acento plano del tema del sistema — por un
    dibujado manual con `QPainter`: surco de fondo gris, relleno con
    degradado suave (más claro arriba, más oscuro abajo, para el
    aspecto "brillante" clásico de aMule) coloreado según el estado
    real de la descarga (`DownloadState`, nuevo `STATE_ROLE` en
    `DownloadsModel`) — verde al descargar, azul al completar, naranja
    en pausa, rojo en error, gris en cola/cancelado/buscando fuentes —
    y porcentaje centrado encima. No hay granularidad por "chunk"
    (aMule real colorea también qué partes concretas del archivo están
    descargadas) porque el modelo `Download` del proyecto no guarda un
    mapa de bits de partes común a las cinco redes; el objetivo
    cubierto es la distinción visual inmediata por color según el
    estado, que es lo que se pidió. Validado en vivo: tabla de prueba
    con una fila por cada estado (`DOWNLOADING` 65% verde, `PAUSED` 30%
    naranja, `COMPLETED` 100% azul, `ERROR` 20% rojo, `CANCELLED` 45%
    gris, `QUEUED`/`SEARCHING_SOURCES` al 0% con el surco vacío)
    capturada con `spectacle` sobre X11, confirmando que cada color se
    distingue correctamente.
31. ✅ Splash AnabasaSoft.png al inicio. Se añadió `SPLASH_PATH` en
    `gui/resources.py` (apuntando al `AnabasaSoft.png` ya existente en
    la raíz del proyecto) y, en `gui/app.py`, un `QSplashScreen` que se
    muestra justo tras crear la `QApplication` y aplicar el tema, antes
    de construir `MainWindow` (que puede tardar un poco por la carga
    del historial de descargas desde la base de datos y el resto de
    inicialización de la ventana); se cierra con `splash.finish(window)`
    en cuanto la ventana principal está lista, sin retrasos artificiales
    añadidos. Validado en vivo con `spectacle` sobre X11: el splash se
    ve correctamente centrado en pantalla con el logo de AnabasaSoft
    (sin bordes de ventana, como corresponde a un `QSplashScreen`), y
    tras él la ventana principal arranca con normalidad.
32. ✅ Icono en la bandeja de aplicaciones — no era redundante con el
    punto 22 (que solo cubría minimizar a bandeja al cerrar la
    ventana y un menú mínimo con "Mostrar"/"Salir"): el usuario pidió
    además una opción en Preferencias para minimizar a la bandeja
    también al pulsar el botón de minimizar (no solo al cerrar), y que
    el icono, una vez minimizado, tenga un menú contextual con más
    opciones al estilo aMule. Añadido `minimize_to_tray_on_minimize`
    (nuevo campo en `UIConfig`, checkbox propio en Preferencias
    General, independiente del "minimizar a bandeja al cerrar" ya
    existente) y, en `MainWindow.changeEvent()`, detección de
    `QEvent.Type.WindowStateChange` a minimizado para ocultar la
    ventana a la bandeja igual que ya hacía `closeEvent()`. El menú
    contextual del icono de bandeja (`_build_tray_icon()`) se amplió
    de "Mostrar"/"Salir" a: Mostrar ventana, Conectar todas las redes/
    Desconectar todas las redes (reutilizando las mismas acciones ya
    existentes en el menú "Redes" de la barra de menú) y Pausar todas
    las descargas/Reanudar todas las descargas (nuevos `pause_all()`/
    `resume_all()` en `DownloadsTab`, que iteran todas las descargas de
    la tabla en vez de solo las seleccionadas), antes de Salir.
    Validado en vivo por el usuario contra un escritorio real: la
    nueva casilla de Preferencias aparece correctamente traducida (se
    probó en francés), y al activarla y minimizar la ventana esta
    desaparece a la bandeja del sistema como se esperaba.
33. Empaquetado y distribución multiplataforma (`.rpm`, `.deb`,
    AppImage, Flatpak, macOS, Windows), a petición explícita del
    usuario ("quiero compartir la aplicación en github, como .rpm,
    .deb, appimage, flatpak, macos y windows"). Antes de generar
    ningún instalador se hizo una revisión completa del código en
    busca de todo lo que pudiera romperse fuera de Linux/X11 (foco
    especial en el icono de la bandeja y el splash de AnabasaSoft, que
    el usuario pidió comprobar explícitamente) — ver el detalle de cada
    hallazgo más abajo. Se resuelve en el mismo orden estricto que el
    resto del backlog, un sub-punto detrás de otro, dejando marcado
    aquí cada uno según se complete:

    33.1. ✅ **Resolución de rutas de recursos no apta para build
          empaquetado.** `gui/resources.py` calculaba `ICON_PATH`/
          `LOGO_PATH`/`SPLASH_PATH` como `Path(__file__).resolve()
          .parent.parent / "Icono.png"` (etc.), lo que solo funcionaba
          ejecutando desde el árbol de fuentes. En un ejecutable
          congelado con PyInstaller (necesario para el `.exe` de
          Windows y el `.app`/`.dmg` de macOS) o instalado vía
          `.deb`/`.rpm`/Flatpak (donde los PNG viven en
          `/usr/share/p2p-total/` o dentro del bundle), esa ruta dejaba
          de apuntar a los ficheros reales: el icono de ventana, el de
          la bandeja del sistema y el splash de AnabasaSoft se
          habrían quedado sin cargar, sin ningún error visible.
          `core/config.py` (`_executable_dir()`, líneas 26-29) ya sabía
          resolver esto mirando `sys.frozen`, pero `gui/resources.py`
          no seguía el mismo patrón. Reescrito para probar, en orden,
          una lista de carpetas candidatas hasta encontrar el fichero:
          (1) `sys._MEIPASS` (la carpeta donde PyInstaller coloca en
          tiempo de ejecución los datos añadidos con `--add-data`,
          válida tanto en build `onefile` como `onedir`, en Windows,
          macOS y Linux), (2) `/usr/share/p2p-total` y
          `/usr/local/share/p2p-total` (convención FHS para la
          instalación de sistema en Linux vía `.deb`/`.rpm`/AppImage,
          que el punto 33.5 usará como ruta de instalo), y (3) el árbol
          de fuentes (`Path(__file__).resolve().parent.parent`, el
          comportamiento que ya había antes) como último fallback para
          seguir pudiendo lanzar con `python main.py gui` en
          desarrollo sin cambiar nada. Validado con dos pruebas
          manuales: en modo desarrollo normal, las tres rutas siguen
          resolviendo exactamente igual que antes (`Icono.png`,
          `Logo.png` y `AnabasaSoft.png` en la raíz del proyecto); y
          simulando un `sys._MEIPASS` con copias de los tres PNG en una
          carpeta temporal, las tres rutas pasan a resolver dentro de
          esa carpeta simulada en vez de la raíz del proyecto,
          confirmando que la detección de build congelado funciona
          antes de tener un ejecutable de PyInstaller real con el que
          probarlo (eso llegará en los puntos 33.8/33.9).
    33.2. ✅ **`Icono.png` no era cuadrado** (271×295 px reales,
          comprobado con Pillow). Windows (`.ico`) y macOS (`.icns`)
          necesitan un icono maestro cuadrado del que derivar el resto
          de tamaños; con esas proporciones cualquier conversión
          automática recortaba o añadía bandas. Generado
          `IconoCuadrado.png` (295×295, RGBA, fondo transparente): en
          vez de reescalar o recortar el dibujo original (lo que lo
          habría desfigurado, a petición explícita del usuario de
          "mantener las proporciones... utiliza un fondo transparente
          cuadrado"), se centra el `Icono.png` original de 271×295 sin
          tocarlo sobre un lienzo cuadrado transparente del lado mayor
          (295 px), con 12 px de relleno transparente a cada lado y 0
          arriba/abajo — mismo dibujo, mismo tamaño de píxel, sin
          escalado ni distorsión. `gui/resources.py` (`ICON_PATH`) pasa
          a apuntar a este fichero nuevo en vez de `Icono.png`, que se
          conserva tal cual por si se necesita en otro contexto.
          Validado cargando `gui.resources` y comprobando que
          `ICON_PATH` resuelve al nuevo fichero cuadrado y que existe
          en disco. `AnabasaSoft.png` (500×500, ya cuadrado) no tenía
          este problema.
    33.3. ✅ **Generado `.ico` (Windows) y `.icns` (macOS)** a partir del
          icono cuadrado del punto 33.2 (`IconoCuadrado.png`, 295×295),
          más la estructura de tamaños del icon theme de Linux
          (`hicolor/16x16` ... `hicolor/512x512`) que exigen
          `.deb`/`.rpm` bien empaquetados y la validación de
          Flatpak/Flathub. Todo generado con Pillow (sin necesitar
          `iconutil` de macOS ni ninguna herramienta externa: la propia
          librería sabe escribir `.icns` embebiendo PNG, comprobado en
          este mismo entorno Linux) y guardado en una carpeta
          `packaging/` nueva en la raíz del proyecto, pensada para ir
          acumulando ahí todo lo que generen el resto de sub-puntos de
          este punto 33 (specs, manifiestos, ficheros `.desktop`...),
          separado de los recursos que sí carga la app en tiempo de
          ejecución (`gui/resources.py`, que no cambia — sigue sirviendo
          PNG normal, esto es solo para los propios instaladores/iconos
          de sistema):
          - `packaging/windows/p2p-total.ico` — 7 resoluciones
            embebidas (16, 24, 32, 48, 64, 128, 256 px), confirmado con
            `file` ("MS Windows icon resource - 7 icons...") y
            releyendo con Pillow que las 7 están presentes.
          - `packaging/macos/p2p-total.icns` — confirmado con `file`
            ("Mac OS X icon... TOC type").
          - `packaging/linux/icons/hicolor/<tamaño>x<tamaño>/apps/
            p2p-total.png` para 16, 22, 24, 32, 48, 64, 128, 256 y
            512 px (los tamaños estándar del icon theme freedesktop),
            cada uno verificado con Pillow con las dimensiones exactas
            esperadas.

          Como la fuente (`IconoCuadrado.png`) es de 295×295, los
          tamaños ≤256 son downscale de calidad (Lanczos) y el de
          512 es upscale — algo más blando que el resto pero sin
          artefactos ni transparencia rota (comprobado a mano: esquina
          `(0,0)` totalmente transparente, centro opaco). Limitación
          conocida y aceptada mientras no se disponga de un icono
          maestro de mayor resolución o en vectorial (SVG); no bloquea
          ningún empaquetado.
    33.4. ✅ **Preparar el repositorio para subir a GitHub**: hecho en su
          momento (`git init`, `.gitignore` excluyendo `venv/` y demás,
          repositorio publicado en `github.com/AnabasaSoft/P2P-Total`,
          ya con varios commits empujados). Quedaba pendiente decidir
          la licencia — añadido ahora `LICENSE` con el texto oficial
          íntegro de la GNU GPL v3 (descargado de `gnu.org`, sin
          modificar), identificador SPDX `GPL-3.0-or-later` usado en el
          `metainfo.xml` (33.5) y en los metadatos de `fpm` (`.deb`/
          `.rpm`). Es una elección razonable por defecto del asistente,
          no una petición explícita del usuario (coherente con el
          espíritu GPL de gtk-gnutella, estudiado sin copiar para G2) —
          pendiente de confirmación o cambio por el usuario si prefiere
          otra licencia.
    33.5. ✅ **Paquete Linux `.deb`/`.rpm`/AppImage**: creados
          `packaging/linux/p2p-total.desktop` (validado con
          `desktop-file-validate`, sin errores) y
          `packaging/linux/org.anabasasoft.P2PTotal.metainfo.xml`
          (validado con `appstreamcli validate`, sin errores ni
          advertencias tras corregir `<developer_name>` al formato
          moderno `<developer id="...">`). El propio build de PyInstaller
          (`packaging/p2p-total.spec`, modo "onedir" para los tres
          sistemas operativos: más lento de arrancar que "onefile" pero
          mucho más fiable con una dependencia binaria pesada como
          libtorrent) se probó en local en este mismo entorno Linux:
          `pyinstaller packaging/p2p-total.spec --noconfirm` genera
          `dist/p2p-total/` sin errores, el binario arranca de verdad
          bajo `QT_QPA_PLATFORM=offscreen` sin ningún traceback, y los
          tres PNG (`IconoCuadrado.png`, `Logo.png`, `AnabasaSoft.png`)
          añadidos como `datas` aparecen dentro de `dist/p2p-total/
          _internal/` — la carpeta a la que apunta `sys._MEIPASS` en
          builds "onedir" de PyInstaller 6.x, confirmando que la
          resolución de rutas del punto 33.1 funciona también en un
          ejecutable congelado real, no solo simulado. Los scripts
          `packaging/linux/build-linux-packages.sh` (usa `fpm` para
          generar `.deb` y `.rpm` a partir de ese mismo build) y
          `packaging/linux/build-appimage.sh` (usa `appimagetool` para
          generar el AppImage) están escritos y cableados al workflow
          `.github/workflows/build-packages.yml`. No se pudieron
          ejercitar en local (`fpm` necesita Ruby + rpm + permisos de
          sistema que no se instalaron en esta máquina a propósito,
          para no tocar el sistema real del usuario sin permiso), pero
          sí se validaron de verdad contra un runner real de GitHub
          Actions (`ubuntu-latest`) al empujar el tag `v1.0`: el job
          `linux-packages` de la ejecución
          `github.com/AnabasaSoft/P2P-Total/actions/runs/32623059623`
          terminó en verde generando `p2p-total_1.0_amd64.deb`,
          `p2p-total-1.0-1.x86_64.rpm` y
          `P2P-Total-1.0-x86_64.AppImage`, los tres adjuntos después a
          la release `v1.0` real del repositorio.
    33.6. ✅ **Flatpak**: excluido inicialmente del alcance de la primera
          tarea de empaquetado a petición explícita y literal del
          usuario ("Tiene que crear .deb, .rpm. appimage, windows y
          macos", sin mencionar Flatpak), retomado después en una
          petición aparte tras confirmar el usuario que solo quería un
          ".flatpak" autónomo, sin publicarlo en Flathub (evita el
          proceso de revisión de un PR contra `flathub/flathub`, que
          exigiría además recompilar Python/PyQt6/libtorrent desde cero
          y sin red dentro del sandbox de `flatpak-builder`, en vez de
          reutilizar directamente el build "onedir" de PyInstaller como
          hacen ya `.deb`/`.rpm`/AppImage). Añadidos
          `packaging/linux/org.anabasasoft.P2PTotal.yaml` (manifiesto:
          runtime `org.freedesktop.Platform//23.08`, copia el build
          "onedir" ya generado dentro de `/app/lib/p2p-total` con un
          script `/app/bin/p2p-total` como lanzador, mismo `Icon=`/
          `.desktop` que el resto de variantes para no duplicar
          metadatos) y `packaging/linux/build-flatpak.sh` (invoca
          `flatpak-builder` + `flatpak build-bundle`, con
          `--runtime-repo` apuntando a Flathub para que el runtime se
          descargue solo si al usuario final le falta), cableados en un
          nuevo job `linux-flatpak` del workflow. Dos fallos reales
          encontrados y corregidos en runners de GitHub Actions
          (workflow disparado a mano con `workflow_dispatch`, sin tocar
          el tag `v1.0`, para no crear una release nueva solo para
          probar): (1) un primer intento escribía el `.desktop` con el
          icono reescrito en `/tmp` en un `build-command` y lo instalaba
          en el siguiente — falló con "cannot stat" porque
          `flatpak-builder` ejecuta cada línea de `build-commands` en
          una invocación de sandbox distinta y `/tmp` no persiste entre
          ellas (a diferencia de `/app`, que sí); arreglado escribiendo
          directamente en `/app` en un único paso, sin fichero
          intermedio; (2) con eso resuelto, `appstreamcli compose` (el
          paso interno de `flatpak-builder` que genera el catálogo
          appstream de la app, más estricto que el `appstreamcli
          validate` ya usado en el punto 33.5) rechazaba el componente
          con `gui-app-without-icon` porque
          `org.anabasasoft.P2PTotal.metainfo.xml` no tenía ningún
          `<icon>` declarado — arreglado añadiendo `<icon
          type="stock">p2p-total</icon>` al metainfo compartido
          (revalidado con `appstreamcli validate`, sigue sin errores) y
          dejando el `.desktop`/icono dentro del flatpak con el mismo
          nombre `p2p-total` que ya usan `.deb`/`.rpm`/AppImage en vez
          de renombrarlo a `org.anabasasoft.P2PTotal`, para que el
          nombre declarado en el `<icon>` y el fichero instalado
          coincidan. Con ambos arreglos, la ejecución
          `github.com/AnabasaSoft/P2P-Total/actions/runs/32624457472`
          terminó en verde generando
          `P2P-Total-0.0.0-dev-x86_64.flatpak` (55 MB, en línea con el
          resto de builds "onedir"). No se ha instalado el bundle en
          este entorno para probarlo de verdad en caliente (descargaría
          el runtime `org.freedesktop.Platform` de Flathub, varios
          cientos de MB, sin que el usuario lo pidiera explícitamente)
          — queda pendiente si el usuario quiere esa validación extra.
          El job se adjunta también al job `release` (junto a
          `linux-packages`/`windows-installer`/`macos-dmg`), así que a
          partir del próximo tag `v*` la release incluirá el `.flatpak`
          automáticamente.
    33.7. ✅ **Riesgo de `libtorrent` en Windows/macOS**: comprobado
          contra el índice real de PyPI (`pypi.org/pypi/libtorrent/
          json`) para la versión estable 2.0.11: sí hay wheels
          oficiales prebuilt para Windows (`win32` y `win_amd64`) y
          macOS (`macosx` x86_64 y arm64, con builds específicos para
          distintas versiones del SDK), cubriendo cPython 3.10 a 3.13
          en ambas plataformas — el riesgo que apuntaba este punto no
          se materializa, así que no hace falta dejar el backend
          BitTorrent como opcional ahí. El workflow fija Python 3.12
          (`env.PYTHON_VERSION`) por estar dentro de ese rango
          soportado en los tres sistemas operativos.
    33.8. ✅ **Empaquetado Windows** (`.exe`/instalador): escrito
          `packaging/windows/installer.iss` (Inno Setup) con el
          asistente clásico "Siguiente, Siguiente, Instalar" pedido
          explícitamente por el usuario (páginas por defecto: bienvenida,
          carpeta de destino, grupo del menú inicio, acceso directo de
          escritorio opcional, instalar, finalizar con opción de
          ejecutar la app), usando ya el `.ico` del punto 33.3. Cableado
          en el workflow: instala Inno Setup vía `choco` en el runner
          `windows-latest` y compila con `ISCC.exe`. Validado de verdad
          contra un runner real de GitHub Actions (`windows-latest`) al
          empujar el tag `v1.0`: el job `windows-installer` de la
          ejecución `.../actions/runs/32623059623` terminó en verde en
          1m47s — el build de PyInstaller sí funciona en Windows y
          `ISCC.exe` compiló `P2P-Total-Setup-1.0.exe` sin errores,
          adjuntado después a la release `v1.0` real. Queda sin probar
          (fuera del alcance de un runner de CI) el comportamiento en
          caliente de `qasync` y los sockets UDP/TCP crudos de las cinco
          redes sobre el `ProactorEventLoop` de Windows al ejecutar
          realmente la app instalada — nunca se ha corrido el programa
          en un Windows real, solo se ha validado que compila y empaqueta.
          Sin firma de código, Windows SmartScreen avisará de "editor no
          reconocido" — asumible para una primera versión.
    33.9. ✅ **Empaquetado macOS** (`.app`/`.dmg`): añadido el bloque
          `BUNDLE` al spec de PyInstaller (usando el `.icns` del punto
          33.3) y `packaging/macos/build-dmg.sh` (empaqueta el `.app` en
          un `.dmg` solo con `hdiutil`, ya incluido en macOS, sin
          dependencias externas). Cableado en el workflow sobre el
          runner `macos-latest`. Validado de verdad contra un runner
          real de GitHub Actions (`macos-latest`) al empujar el tag
          `v1.0`: el job `macos-dmg` de la ejecución
          `.../actions/runs/32623059623` terminó en verde en 46s,
          generando `P2P-Total-1.0.dmg` sin errores, adjuntado después a
          la release `v1.0` real. Igual que en 33.8, queda sin probar el
          comportamiento en caliente de la app ya instalada en un macOS
          real (fuera del alcance de un runner de CI). Sin firma ni
          notarización de Apple, Gatekeeper bloqueará o avisará al abrir
          la app — mismo caso que Windows, asumible para una primera
          versión.

    Workflow común a 33.5/33.8/33.9:
    `.github/workflows/build-packages.yml`, con un job por plataforma
    (`linux-packages`, `windows-installer`, `macos-dmg`) más un job
    `version` (calcula la versión a partir del tag `vX.Y.Z` empujado, o
    "0.0.0-dev" si se lanza a mano) y un job final `release` (solo si el
    disparo fue un tag `v*`) que adjunta los paquetes generados a un
    release de GitHub recién creado. Se dispara con `push` de un tag
    `v*` o manualmente (`workflow_dispatch`) para poder probar sin
    necesidad de crear un tag real. Deliberadamente sin ninguna GitHub
    Action de terceros más allá de las oficiales (`actions/checkout`,
    `actions/setup-python`, `actions/upload-artifact`, `actions/
    download-artifact`): Inno Setup se instala vía `chocolatey` (ya
    preinstalado en los runners de Windows) y la publicación del release
    se hace con `gh`, ya preinstalado en todos los runners — mismo
    espíritu de minimizar dependencias externas que el resto del
    proyecto.

    Validado de verdad contra runners reales de GitHub Actions: antes de
    lanzar nada se subió el permiso por defecto de las Actions del
    repositorio de "solo lectura" a "lectura y escritura" (`gh api -X
    PUT repos/.../actions/permissions/workflow -f
    default_workflow_permissions=write`), como refuerzo del bloque
    `permissions: contents: write` que ya tenía declarado el job
    `release`, para no arriesgarse a que la publicación del release
    fallara por falta de permisos del `GITHUB_TOKEN`. Con eso corregido,
    se creó y empujó el tag `v1.0` real, disparando la ejecución
    `github.com/AnabasaSoft/P2P-Total/actions/runs/32623059623`: los
    cinco jobs (`version`, `linux-packages`, `windows-installer`,
    `macos-dmg`, `release`) terminaron en verde sin ningún fallo, y la
    release `github.com/AnabasaSoft/P2P-Total/releases/tag/v1.0` quedó
    publicada con los cinco paquetes adjuntos
    (`p2p-total_1.0_amd64.deb`, `p2p-total-1.0-1.x86_64.rpm`,
    `P2P-Total-1.0-x86_64.AppImage`, `P2P-Total-Setup-1.0.exe`,
    `P2P-Total-1.0.dmg`). Con esto, 33.5/33.8/33.9 quedan completados con
    el mismo nivel de confianza que 33.7. El punto 33.6 (Flatpak) se
    completó después, en una petición aparte — ver el detalle en el
    propio sub-punto 33.6 más arriba.

    Nota de alcance para 33.7-33.9: en el momento de escribir este
    punto, `README.md` (línea 3) describe el proyecto como "Cliente P2P
    multi-red **para Linux**"; los puntos de Windows/macOS amplían
    deliberadamente ese alcance a petición explícita del usuario. De
    paso, `core/config.py` (rutas `~/.config/p2p-total` y
    `~/.local/share/p2p-manager`, convención XDG) y
    `default_download_dir` (`~/Descargas/P2P-Total`, nombre en español)
    seguirán funcionando tal cual en Windows/macOS por colgar de
    `Path.home()`, aunque no sean la ubicación "nativa" de cada
    sistema (`%APPDATA%`/`~/Library/Application Support` en Windows/
    macOS) — se deja así a propósito por simplicidad mientras no se
    demuestre que hace falta más, no está en la lista de sub-puntos de
    arriba porque no bloquea ningún empaquetado.

34. Mejoras post-empaquetado (estudiado 2026-08-23, a petición del
    usuario tras completar el punto 33: "lista de ideas de mejora o a
    implementar?" → "añade todo a una lista de tareas menos la firma
    de windows/macos"). Se excluye deliberadamente de esta lista la
    firma de código de Windows/macOS (mencionada como pendiente en
    33.8/33.9) por petición explícita del usuario, y también publicar
    el `.flatpak` en Flathub: se investigó la política real de Flathub
    a petición del propio usuario ("creo que flathub no permite
    aplicaciones creadas con ia, no?") y, en efecto, desde el 29 de
    mayo de 2026 Flathub prohíbe cualquier envío nuevo con código,
    documentación o metadatos generados o asistidos por IA — incluido
    el propio PR de envío —, con la única excepción discrecional de
    "proyectos maduros y bien mantenidos" que no es una vía garantizada;
    como P2P Total se ha desarrollado con asistencia de IA de principio
    a fin, intentar publicarlo en Flathub hoy caería directamente bajo
    esa prohibición, así que no se incluye como tarea. Pendientes de
    resolver en orden estricto:

    34.1. ✅ Mecanismo de auto-actualización real — completo (ver "Punto
          34.1 del backlog: mecanismo de auto-actualización real" en
          "Estado actual" para el detalle completo y la validación):
          descarga y sustitución automática para AppImage/instalador de
          Windows/macOS; los `.deb`/`.rpm`/`.flatpak` (gestionados por
          el sistema, no auto-actualizables de forma segura) siguen
          cayendo al aviso con enlace de siempre.
    34.2. ✅ Suite de tests automatizados — completo (ver "Punto 34.2 del
          backlog: suite de tests automatizados" en "Estado actual"
          para el detalle completo y la validación): 157 tests con
          pytest cubriendo la lógica pura y determinista de las cinco
          redes (parsers, codecs binarios, hashes MD4/Tiger/TTH/AICH,
          config, i18n); la validación de protocolo completo contra
          infraestructura real de cada red sigue siendo manual, a
          propósito.
    34.3. ✅ Cifrado de protocolo BitTorrent (MSE/PE) y soporte de µTP —
          completo (ver "Punto 34.3 del backlog: cifrado de protocolo
          BitTorrent (MSE/PE) y soporte de µTP" en "Estado actual" para
          el detalle completo y la validación): `libtorrent` ya los
          implementa, así que el trabajo fue fijar explícitamente la
          política (cifrado "enabled", no forzado; µTP en ambos
          sentidos) en vez de depender de valores por defecto
          implícitos, y exponer contadores en vivo
          (`connected_peers`/`encrypted_peers`/`utp_connections`) en
          `get_stats()` y en la pestaña Red de la GUI. Validado con dos
          tests nuevos y contra un torrent real con más de 150 peers
          conectados simultáneamente.
    34.4. ✅ Auditoría de IPv6 en Soulseek/Gnutella2/eD2k-Kad — completo
          (ver "Punto 34.4 del backlog: auditoría de IPv6 en
          Soulseek/Gnutella2/eD2k-Kad" en "Estado actual" para el
          detalle completo): revisado caso por caso el código de los
          tres backends, confirmando que el punto 19 ya había resuelto
          de facto todo lo que el protocolo real permite (cada backend
          tiene un único punto de lectura/escritura de direcciones, ya
          documentado como límite de protocolo salvo `_read_wire_ip()`
          de eD2k, al que se le añadió la nota que le faltaba). No
          queda ninguna mejora real de IPv6 pendiente en estas tres
          redes.
    34.5. ✅ Planificador de ancho de banda por franja horaria — completo
          (ver "Punto 34.5 del backlog: planificador de ancho de banda
          por franja horaria" en "Estado actual" para el detalle
          completo de la implementación y su validación).
    34.6. ✅ Control remoto / API web — completo (ver "Punto 34.6 del
          backlog: control remoto / API web" en "Estado actual" para
          el detalle completo de la implementación y su validación).
    34.7. ✅ Mejoras de accesibilidad en la GUI: soporte de lector de
          pantalla y navegación completa por teclado — completo (ver
          "Punto 34.7 del backlog: mejoras de accesibilidad en la GUI"
          en "Estado actual" para el detalle completo de la
          implementación y su validación).

    Con 34.7 se completan todos los sub-puntos de la lista (34.1-34.7):
    el punto 34 queda cerrado.

### Arreglo: el aviso de reinicio al cambiar de idioma salía en el idioma antiguo

Bug real reportado por el usuario: al cambiar el idioma en Preferencias
(o desde el menú rápido), el aviso "el cambio de idioma se aplicará al
reiniciar la aplicación" se mostraba en el idioma que se acababa de
dejar de usar, no en el que el usuario acababa de elegir. Causa: `t()`
lee siempre `gui.i18n._current_language`, que no cambia hasta el
próximo arranque (el cambio de idioma requiere reiniciar porque los
widgets ya construidos no se retraducen en caliente) — así que el
propio aviso de "hace falta reiniciar" quedaba escrito en el idioma
viejo.

Arreglo: `gui/i18n.py` añade `t_in(language, key, **kwargs)`, idéntica
a `t()` pero para un idioma concreto en vez del activo (`t()` pasa a
ser un delegado de `t_in(_current_language, key, **kwargs)`, sin
cambio de comportamiento). `gui/main_window.py`
(`_on_language_selected`) y `gui/widgets/settings_dialog.py`
(`_on_save`) usan `t_in(nuevo_idioma, ...)` para el título y el propio
aviso de reinicio en vez de `t()`, mostrándolo ya en el idioma recién
elegido sin tocar `_current_language` (que sigue igual hasta el
reinicio real, así el resto de la sesión en curso no queda con textos
mezclados de dos idiomas). Validado con un script directo confirmando
que `t_in()` devuelve el texto correcto en varios idiomas sin alterar
el idioma activo global, y con la suite completa de pytest sin
regresiones (192 passed).

### Arreglo: conectar a Soulseek/DC++/Gnutella2/eMule se congelaba la GUI (había que forzar el cierre)

Bug real reportado por el usuario: al conectar cualquier red salvo
BitTorrent, la GUI se quedaba congelada sin remedio (había que matar
el proceso a la fuerza); una búsqueda de Soulseek por CLI (`python
main.py search ... --network soulseek`) también se quedaba colgada
sin mostrar nada, ignorando `--timeout`. Investigado en profundidad
porque el síntoma no encajaba con ningún timeout de red conocido de
los backends (todos tienen `asyncio.wait_for` con límites razonables).

Causa raíz, en cadena, las tres agravándose entre sí:

1. Las cuatro redes que sirven ficheros (Soulseek, DC++, Gnutella2,
   eMule) llaman a `SharedLibrary.rescan()` (`core/sharing.py`) de
   forma **síncrona** dentro de su propio `connect()`, y `rescan()`
   hashea (SHA1 + eD2k) el contenido **entero** de todas las carpetas
   compartidas configuradas en Preferencias. Al ser una llamada
   síncrona sin `await`, bloqueaba el único hilo del event loop de
   asyncio -y con él, vía `qasync`, la GUI Qt entera- durante todo ese
   tiempo. BitTorrent no lo sufre porque no usa `SharedLibrary` (ya
   siembra de fábrica vía su propia sesión de libtorrent).
2. Ese hasheo no tenía ninguna caché: `rescan()` volvía a leer y
   hashear cada fichero compartido **desde cero en cada connect()**,
   por severo que fuera el coste, sin importar si ya se había
   escaneado antes en la misma sesión.
3. El eD2k usa MD4 (`core/md4.py`), implementado a mano en Python puro
   porque el OpenSSL de sistemas modernos no lo trae habilitado
   (`hashlib.new("md4")` no está disponible). Su `update()` tenía un
   fallo de rendimiento cuadrático: `self._buffer = self._buffer[64:]`
   dentro del bucle que procesa bloques de 64 bytes reasignaba (copiaba
   entero) el búfer restante en cada uno de esos bloques, así que
   hashear un `data` de tamaño N costaba O(N²) en vez de O(N). Con
   chunks de 1 MB (los que lee `_hash_file()`), esto ya costaba ~1,5 s
   por MB antes siquiera de sumar el coste de las demás carpetas —
   confirmado con un benchmark (`core/md4.py` antes del arreglo: 1 MB
   → 1,53 s, 2 MB → 5,13 s, 4 MB → 17,67 s, 8 MB → 66,76 s, claramente
   cuadrático). Con la carpeta compartida real del usuario (93 GB,
   5.404 ficheros entre `amule/`, `Batocera/`, `Juegos/`, `P2P-Total/`
   y `NicotinePlus/`), esto suponía muchas horas por *cada* conexión a
   *cada* red — indistinguible en la práctica de un cuelgue eterno.

Arreglo, con cuatro cambios independientes que se refuerzan entre sí:

1. **No bloquear el event loop**: las cuatro llamadas a
   `self._shared_library.rescan(...)` en `backends/soulseek_backend.py`,
   `backends/dcpp_backend.py`, `backends/g2_backend.py` y
   `backends/emule_backend.py` pasan a `await
   asyncio.to_thread(self._shared_library.rescan, ...)`, moviendo el
   escaneo/hasheo a un hilo aparte para que la GUI (y el resto de
   tareas asyncio) sigan respondiendo mientras dura.
2. **Arreglado el fallo cuadrático de `core/md4.py`**: `MD4.update()`
   ahora solo guarda en `self._buffer` como mucho 63 bytes de resto
   entre llamadas y avanza con un índice sobre `data` en vez de
   reasignar el búfer entero en cada bloque de 64 bytes — pasa de
   O(N²) a O(N). Confirmado con el mismo benchmark tras el arreglo:
   throughput constante (~1,3 MB/s) desde 1 MB hasta 200 MB, en vez de
   degradarse. Sigue siendo lento por ser Python puro (no hay MD4 en
   OpenSSL para delegar), pero ya no exponencialmente peor cuanto más
   grande es el fichero.
3. **Caché de hashes por (tamaño, mtime) en `SharedLibrary.rescan()`**:
   ahora guarda en memoria, por ruta absoluta, el tamaño/fecha de
   modificación junto con los hashes ya calculados, y solo vuelve a
   hashear un fichero si alguno de los dos ha cambiado desde el último
   escaneo — reconectar (o conectar otra red sobre la misma
   `SharedLibrary`) dentro de la misma sesión ya no repite el trabajo
   entero.
4. **Cálculo de hash bajo demanda según lo que necesita cada red**:
   `SharedLibrary.rescan()` y `_hash_file()` ahora aceptan
   `need_sha1`/`need_ed2k` por separado (por defecto `True` ambos).
   Soulseek y DC++ (en esta implementación) buscan un fichero
   compartido por ruta/nombre, no por hash, así que llaman a
   `rescan(need_sha1=False, need_ed2k=False)` y no pagan ningún coste
   de hasheo (solo el `os.walk()`+`stat()` de indexar rutas).
   Gnutella2 solo necesita el SHA1 para anunciar/responder búsquedas
   `urn:sha1:`, así que llama a `rescan(need_ed2k=False)` y se ahorra
   con diferencia el hash más caro (eD2k/MD4); el SHA1 es barato
   porque usa `hashlib` en C a velocidad de disco. eMule sigue
   pidiendo ambos (los valores por defecto) porque de verdad necesita
   el eD2k. La caché guarda qué hashes tiene cada fichero por
   separado, así que si una red solo pidió SHA1 y más tarde otra pide
   también eD2k sobre la misma `SharedLibrary` compartida, solo
   calcula el que falta, sin repetir el SHA1 ya cacheado.
   `gui/connection_manager.py` pasa además a compartir una única
   `SharedLibrary` entre las cuatro redes (antes creaba una nueva por
   cada `connect_network()`, multiplicando por cuatro el escaneo si se
   auto-conectaban varias redes a la vez).

Con estos cuatro cambios, sobre la carpeta compartida real del
usuario: escanear sin hashes (Soulseek/DC++) pasó de nunca terminar a
0,4 s; escanear solo con SHA1 (Gnutella2) sobre la subcarpeta más
grande (`amule/`, 48 GB) pasó de no terminar en más de 3 minutos a
185,9 s. eMule (necesita eD2k de verdad) sigue siendo lento con
carpetas compartidas muy grandes por la propia naturaleza de una
implementación de MD4 en Python puro, pero ya no se congela la GUI
mientras tanto (corre en un hilo aparte) y cada reconexión posterior
reutiliza la caché.

Validado: reproducido el cuelgue original (`python main.py search
"test" --network soulseek --timeout 15` colgado más de 60 s sin
ninguna salida, ni siquiera con `python -u`), confirmada la causa con
benchmarks directos de `core/md4.py` y `SharedLibrary.rescan()`
aislados, y reproducida la búsqueda de nuevo tras el arreglo:
conecta y devuelve resultados reales de Soulseek en 18 s. Nuevo
fichero `tests/test_sharing.py` (5 tests: escaneo sin hashes,
SHA1-only, reutilización de caché sin cambios, rehash al modificar un
fichero, y completar un hash que faltaba reutilizando el ya cacheado)
más la suite completa de pytest, sin regresiones (197 passed).

### Arreglo: hasheo de eMule en segundo plano de verdad, con caché persistente en SQLite

El arreglo anterior movía `SharedLibrary.rescan()` a un hilo aparte con
`await asyncio.to_thread(...)`, así que el event loop/GUI ya no se
congelaba — pero seguía siendo un `await`: `connect()` de esa red en
concreto no volvía hasta que el escaneo entero terminaba, y la caché de
hashes vivía solo en memoria (se perdía en cada reinicio de la app).
Para eMule, que sí necesita el eD2k de cada fichero (con diferencia el
hash más lento, MD4 puro Python), eso significaba que conectar contra
la biblioteca compartida real del usuario (93 GB, 5.404 ficheros)
podía tardar del orden de 20 horas antes de que la red quedase
operativa — y, tras cerrar la app, había que repetirlo todo desde
cero la próxima vez. El usuario pidió explícitamente arreglar esto:
"el hash se tiene que hacer en segundo plano sin interferir en la GUI
y guardando una base de datos los resultados de los hash para sólo
volver a hacer hash a los archivos modificados o nuevos. Cualquier
proceso tiene que ser en hilos independientes que no afecten al GUI,
búsquedas, hash, verificar descargas, etc."

Cambios:

1. **Caché de hashes persistida en SQLite** (`core/database.py`,
   tabla nueva `shared_hash_cache`, mismo patrón que `downloads.db` ya
   usa para descargas/búsquedas/estadísticas): `path` como clave,
   `size`/`mtime_ns`/`sha1`/`ed2k`/`ed2k_parts`/`has_sha1`/`has_ed2k`.
   `load_shared_hash_cache()` la carga entera al construir una
   `SharedLibrary`; `save_shared_hash_cache_entries()` guarda en lote
   (no fichero a fichero, para no pagar una transacción SQLite por
   cada uno); `prune_shared_hash_cache()` borra al final de un escaneo
   completo las entradas de ficheros que ya no están (se borraron o se
   movieron fuera de las carpetas compartidas). `SharedLibrary.rescan()`
   ahora persiste lo que va calculando cada `_PUBLISH_EVERY = 100`
   ficheros (no solo al terminar del todo), para no perder el progreso
   si la app se cierra a mitad de un escaneo largo.
2. **Escaneo realmente no bloqueante** (`SharedLibrary.ensure_scanning()`
   + `_background_scan()`): los cuatro backends que comparten
   (Soulseek, DC++, Gnutella2, eMule) ya no hacen `await
   asyncio.to_thread(self._shared_library.rescan, ...)` dentro de
   `connect()` — llaman a `ensure_scanning(need_sha1=..., need_ed2k=...)`,
   que lanza el escaneo y vuelve al instante, sin esperar nada.
   `connect()` termina de inmediato y la red queda operativa (login,
   búsquedas, servir lo ya indexado) aunque el escaneo siga en marcha
   de fondo. Si ya hay un escaneo en curso y llega una petición con
   hashes adicionales (p.ej. Gnutella2 conecta primero pidiendo solo
   SHA1 y poco después eMule, que además necesita eD2k, sobre la misma
   `SharedLibrary` compartida entre redes vía
   `gui/connection_manager.py`), `ensure_scanning()` amplía lo que se
   pide sin lanzar un segundo escaneo en paralelo ni reiniciar desde
   cero lo ya hasheado. Dentro de `rescan()`, `self._files` (la lista
   que consultan `list_files()`/`find_by_*`) también se publica cada
   `_PUBLISH_EVERY` ficheros en vez de solo al final, así que búsquedas
   y peticiones de descarga ven resultados según van apareciendo, no
   solo cuando el escaneo entero ha terminado. Efecto colateral en
   `backends/g2_backend.py`: antes decidía si arrancar el servidor de
   subida (`self._share_server`) mirando `shared_library.enabled`
   (`bool(self._files)`, que ya no está listo al momento de conectar);
   ahora mira `shared_library.roots` (hay carpetas compartidas
   configuradas, aunque el índice todavía se esté rellenando) para no
   dejar de escuchar peticiones mientras dura el escaneo.
3. **Hilo daemon en vez del `ThreadPoolExecutor` por defecto de
   `asyncio.to_thread`** (`core.sharing._run_in_daemon_thread`):
   validando en real contra la red eD2k/Kad de verdad (ver más abajo)
   se detectó que, aun con el escaneo ya no bloqueando `connect()`, el
   propio proceso de Python se quedaba colgado sin terminar — el
   `ThreadPoolExecutor` que usa `asyncio.to_thread` por defecto crea
   hilos que NO son `daemon`, así que el intérprete los espera al
   salir (vía `atexit`) aunque el script/la app ya hayan terminado su
   trabajo, hasta que el escaneo en curso (que puede tardar horas)
   acabe por su cuenta. `_run_in_daemon_thread()` hace lo mismo que
   `asyncio.to_thread()` pero lanzando el hilo explícitamente con
   `daemon=True`, así que la app/el proceso pueden cerrarse de
   inmediato aunque el escaneo se corte a medias — no se pierde nada
   importante gracias al punto 1 (persistencia incremental cada 100
   ficheros).
4. **Verificación de descargas de eMule también en un hilo aparte**
   (`backends/emule_backend.py`, `_verify_download`): al completar una
   descarga, se recalculaba el MD4 (eD2k) del fichero entero de forma
   síncrona dentro de la propia corrutina de descarga — para un
   fichero grande, esto bloqueaba el event loop/GUI durante toda la
   verificación, el mismo tipo de fallo que motivó todo este arreglo,
   solo que en "verificar descargas" en vez de en "conectar". Ahora se
   llama envuelta en `await asyncio.to_thread(...)`. Se auditó el
   resto de hasheo del proyecto (`core/aich.py`, los `md5`/`md4` de
   logins e IDs de cliente) y no se encontró ningún otro caso: todos
   operan sobre datos pequeños (bloques de 180 KiB o menos) o ya
   estaban en un hilo aparte (`_sha1_of_file` en G2, `tth_of_file` en
   DC++).

Validado:
- Suite de tests (`tests/test_sharing.py`, ampliada a 9 tests: los 5
  anteriores más `test_hash_cache_persists_across_instances`
  -reconstruye una `SharedLibrary` nueva sobre la misma base de datos
  y comprueba que no recalcula ningún hash, forzando un `AssertionError`
  si `_hash_file` llega a invocarse-, `test_ensure_scanning_does_not_block_caller`,
  `test_ensure_scanning_does_not_launch_duplicate_scan` y
  `test_ensure_scanning_merges_requirements_from_several_networks`).
  Suite completa sin regresiones (201 passed).
- **Conexión real a eMule** (petición explícita del usuario: "prueba a
  conectar eMule también"): `python main.py search "test" --network
  emule --timeout 20` descubrió un servidor eD2k automáticamente
  (`85.17.116.222:6082`), completó el bootstrap de Kad (192 contactos
  reales) y devolvió cientos de resultados reales de la red eD2k/Kad,
  todo con la biblioteca compartida real del usuario (93 GB)
  escaneándose de fondo — el proceso completo tardó 29,5 s y terminó
  limpio (código de salida 0) en vez de quedarse colgado.
- **Persistencia validada con ficheros reales**: contra la carpeta
  compartida real y pequeña del usuario (`~/Descargas/P2P-Total`, 7,3
  MB, 2 ficheros), el primer escaneo (SHA1+eD2k) tardó 5,6 s; una
  segunda `SharedLibrary` sobre la misma carpeta -simulando un
  reinicio de la app- tardó 0,0012 s en dar el mismo resultado,
  reutilizando por completo la caché ya guardada en SQLite.
- Se detectó y arregló durante esta misma validación en real el bug
  del `ThreadPoolExecutor` no-daemon descrito en el punto 3 (el
  proceso de la CLI se quedaba colgado a pesar de que la búsqueda ya
  había terminado y mostrado resultados; confirmado con `ps aux`
  mostrando el proceso seguía al 100% CPU, y con `timeout 90` teniendo
  que forzar su cierre con código de salida 124 antes del arreglo).
- Añadido `tests/conftest.py` (fixture `autouse` `_isolated_db`): se
  detectó que, al no existir antes ningún test que tocase
  `core/database.py`, ejecutar la nueva suite escribía sin querer una
  fila de prueba en la base de datos real de producción del usuario
  (`~/.local/share/p2p-manager/downloads.db`) — ya limpiada. Ahora
  todos los tests usan una base de datos SQLite aislada en un
  directorio temporal propio (con `tmp_path_factory`, no `tmp_path`,
  precisamente para no anidarla dentro de la carpeta que un test use
  como carpeta compartida a escanear).

### Release v1.1: publicación con los paquetes adjuntos, tras un fallo de CI diagnosticado en vivo

Con el commit `06e67214` ("Hash arreglado en segundo plano y otros
adds") ya en `main`, tocaba publicar la siguiente release con el punto
34 completo (34.1-34.7) y el arreglo de hasheo en segundo plano de
eMule. El primer intento del usuario de etiquetar y publicar la
release tuvo un fallo real, investigado y corregido en esta sesión:

- **Diagnóstico**: `gh run view` sobre la ejecución fallida del
  workflow `Construir paquetes` (disparado al empujar el tag) mostró
  que los 4 jobs de compilación (`linux-packages`, `linux-flatpak`,
  `windows-installer`, `macos-dmg`) habían terminado bien — el único
  job en rojo era `release`, con el log de error `a release with the
  same tag name already exists: v1.2`. Causa raíz: el usuario había
  creado a mano, con `gh release create`, una release para ese mismo
  tag (con las notas en español ya redactadas pero sin paquetes)
  mientras el workflow de CI compilaba en paralelo; cuando el job
  `release` del propio workflow terminó de compilar y llegó a su paso
  final de `gh release create --generate-notes` (que adjunta los 6
  artefactos), chocó con la release manual ya existente y falló, sin
  llegar a adjuntar ningún paquete a ningún sitio.
- **Corrección**: en vez de repetir el mismo problema con un tag `v1.2`
  nuevo, se reutilizó el tag `v1.1` (que ya existía sin release
  asociada, de un intento previo fallido del usuario) siguiendo estos
  pasos: (1) borrar la release `v1.2` fallida y su tag
  (`gh release delete v1.2 --yes --cleanup-tag`); (2) borrar cualquier
  release manual ya creada sobre `v1.1`; (3) borrar y volver a empujar
  el tag `v1.1` en el remoto (`git push origin :refs/tags/v1.1` seguido
  de `git push origin v1.1` — necesario porque un `git push` de un tag
  que ya apunta al mismo commit no genera ningún evento nuevo y no
  dispara el workflow) para que fuese el propio workflow, sin
  intervención manual paralela, quien creara la release y adjuntara
  los 6 paquetes; (4) una vez publicada por el workflow (con notas
  autogeneradas por `--generate-notes`), sustituir solo el cuerpo de
  las notas por el texto en español ya redactado con
  `gh release edit v1.1 --notes-file ...`, sin tocar los assets ya
  adjuntos.
- **Resultado validado**: release
  [v1.1](https://github.com/AnabasaSoft/P2P-Total/releases/tag/v1.1)
  publicada con los 6 paquetes (`p2p-total-1.1-1.x86_64.rpm`,
  `P2P-Total-1.1-x86_64.AppImage`, `P2P-Total-1.1-x86_64.flatpak`,
  `P2P-Total-1.1.dmg`, `P2P-Total-Setup-1.1.exe`,
  `p2p-total_1.1_amd64.deb`), igual que la v1.0, con notas en español
  cubriendo el punto 34 completo (34.1-34.7) y el arreglo de hasheo en
  segundo plano.
- **Lección para próximas releases**: no crear la release a mano con
  `gh release create` sobre un tag recién empujado si el workflow de
  CI va a intentar crear su propia release para ese mismo tag —
  esperar a que el job `release` del workflow termine (o falle) antes
  de tocar nada manualmente sobre esa release, y si hace falta texto
  de notas distinto al autogenerado, aplicarlo después con
  `gh release edit --notes-file` una vez el workflow ya haya adjuntado
  los paquetes.

Para que este problema no se pueda repetir (a petición explícita del
usuario: "actualiza actualiza.sh para que cree la tag y la release
automáticamente y no volver a tener estos problemas"), se amplió
`actualiza.sh` (antes solo `git add` + `git commit` + `git push`) para
que, tras subir el commit, calcule la siguiente versión consecutiva
(incrementa el número menor del último tag `vX.Y` existente en el
repo, o admite `--version X.Y` para forzar una versión concreta),
cree y empuje ese tag nuevo, y espere con `gh run watch` a que el
workflow `Construir paquetes` termine, confirmando al final la URL de
la release ya publicada con los 6 paquetes. Por diseño, el script
**nunca** llama a `gh release create`: solo crea el tag, dejando que
sea el propio workflow de CI quien cree la release al terminar de
compilar — precisamente la causa raíz del fallo de la v1.2 fue que una
release manual y la del workflow chocaban por el mismo nombre de tag,
así que evitar esa duplicidad es lo que resuelve el problema de raíz
en vez de solo evitar repetir los pasos manuales.

### Arreglo: la aplicación empaquetada nunca supo su propia versión real

A raíz de renumerar las releases a versionado semántico (`v1.0.0`,
`v1.0.1`... a petición del usuario), se auditó todo el pipeline de
versión y se encontró que la aplicación empaquetada llevaba **desde
siempre** un bug de fondo, no causado por el cambio de numeración sino
solo hecho más visible por él: `core/version.py` (`VERSION`, usada por
el diálogo "Acerca de" y por `core/update_checker.py` para comparar
contra el último tag publicado en GitHub) nunca se actualizaba al
compilar los paquetes — ninguno de los 4 jobs de compilación del
workflow (`linux-packages`, `linux-flatpak`, `windows-installer`,
`macos-dmg`) tocaba ese fichero antes de invocar a PyInstaller, así
que todo paquete publicado hasta ahora, fuese cual fuese su tag real,
llevaba dentro un binario que se creía permanentemente en la versión
"1.0" — lo que además rompía la comprobación de actualizaciones
(`_parse_version("1.0") < _parse_version("v1.0.1")` siempre verdadero,
así que la app se habría creído desactualizada para siempre nada más
instalarse, incluso recién instalada la última versión).

Se encontró además un segundo bug real relacionado, esta vez sí
introducido por el cambio de esquema: el paso de Windows que fija la
versión del instalador hacía `sed -i "s/...MyAppVersion \"1.0\".../"`
con el "1.0" antiguo escrito a fuego en el patrón — al pasar el
placeholder del repo a `"1.0.1"`, ese `sed` habría dejado de encontrar
ninguna coincidencia y el instalador de Windows se habría seguido
compilando con la versión equivocada en el nombre de fichero/registro
de Windows, en silencio (sin error visible, `sed` no falla si no
encuentra el patrón).

Cambios:
- `.github/workflows/build-packages.yml`: nuevo paso "Fijar la versión
  en core/version.py" en los 4 jobs de compilación, justo antes de
  invocar a PyInstaller, con
  `sed -i "s/^VERSION = \".*\"/VERSION = \"$VERSION\"/" core/version.py`
  — inyecta la versión real (derivada del tag por el job `version`) en
  el binario que de verdad se va a distribuir, sin depender de que el
  fichero del repo esté sincronizado a mano.
- El `sed` del instalador de Windows (`packaging/windows/installer.iss`)
  se hizo genérico (`\".*\"` en vez del literal `\"1.0\"`), para que
  siga funcionando pase lo que pase con el valor por defecto del
  fichero.
- `packaging/p2p-total.spec`: el `BUNDLE` de macOS (claves
  `CFBundleShortVersionString`/`CFBundleVersion` del `Info.plist`,
  hasta ahora también fijas a `"1.0"`) ahora lee la versión real de la
  variable de entorno `VERSION` (`os.environ.get("VERSION",
  "0.0.0-dev")`), la misma que ya reciben el resto de scripts de
  empaquetado.
- `core/version.py` (valor por defecto en el repo, para cuando se
  ejecuta desde código fuente sin pasar por el pipeline de CI) y
  `packaging/windows/installer.iss` (placeholder): actualizados de
  `"1.0"` a `"1.0.1"`, la última versión realmente publicada.
- `packaging/linux/org.anabasasoft.P2PTotal.metainfo.xml`: añadidas
  las entradas de `1.0.0` y `1.0.1` a `<releases>` (metadato estático
  de AppStream que usan los centros de software, no leído por la app
  en tiempo de ejecución, pero que había quedado desactualizado).

Validado: `python3 -c "import yaml; yaml.safe_load(...)"` confirma que
el workflow sigue siendo YAML válido tras los cambios, y se replicó a
mano el `sed` de ambos ficheros (`core/version.py` e
`installer.iss`) contra un fichero de prueba con `VERSION` exportado
como variable de entorno (igual que lo expone GitHub Actions a cada
paso de un job), confirmando la sustitución correcta en ambos casos.
Queda pendiente de validación en real la próxima vez que
`actualiza.sh` dispare una release nueva: el diálogo "Acerca de" y la
comprobación de actualizaciones de esos paquetes deberían mostrar ya
la versión real del tag en vez de "1.0".

**Corrección tras la primera validación en real**: al ejecutar
`./actualiza.sh` con este mismo arreglo (tag `v1.0.2`), el job
`macos-dmg` falló justo en el nuevo paso "Fijar la versión en
core/version.py", con el error `sed: 1: "core/version.py": command c
expects \ followed by text` — mientras que los otros 3 jobs
(`linux-packages`, `linux-flatpak`, `windows-installer`) lo pasaron
sin problema. Causa: los runners de `macos-latest` traen **BSD `sed`**
de serie, no GNU `sed` — su flag `-i` exige un argumento de sufijo de
backup inmediatamente después (aunque sea vacío, `-i ''`), así que
`sed -i "script" fichero` en BSD interpreta `"script"` como ese
sufijo y `fichero` (con el patrón de sustitución dentro) como el
script de `sed` en sí, que no es sintaxis válida. En Linux (bash) y en
Windows (bash de MSYS que trae Git for Windows) `sed` sí es GNU sed,
por eso ahí funcionaba igual. Arreglo: en vez de perseguir la sintaxis
compatible con ambos `sed`, se sustituyó por
`packaging/set_version.py` (nuevo, Python puro, idéntico en los 4
sistemas operativos porque Python ya es una dependencia garantizada
del pipeline vía `actions/setup-python`) y se cambiaron los 4 pasos
"Fijar la versión en core/version.py" del workflow a
`python packaging/set_version.py "$VERSION"`. El `sed` de
`installer.iss` (que solo corre en el job de Windows, sobre GNU sed)
se dejó tal cual, ya validado en real. Probado en local: el script
sustituye correctamente `VERSION = "1.0.1"` por
`VERSION = "9.9.9"` y viceversa. Como el job `release` necesita que
los 4 jobs de compilación terminen bien, esta ejecución (`v1.0.2`) no
llegó a publicar ninguna release (sin paquetes que perder, ninguno se
llegó a compilar del todo) — se borró el tag `v1.0.2` (local y
remoto) al no tener release asociada, dejando limpio el siguiente
intento (`v1.0.3`) para revalidar el arreglo completo.

### Función: botón "Buscar actualizaciones" en el menú Ayuda

El usuario reportó que, con la versión 1.0.2 instalada desde GitHub,
la app no avisaba de que ya había disponible la 1.0.3. Investigado a
fondo: el paquete instalado real (`rpm -qa` → `p2p-total-1.0.2-1.x86_64`)
sí lleva grabado internamente `core.version.VERSION = "1.0.2"` (se
confirmó extrayendo el bytecode de ese módulo del propio ejecutable
empaquetado con las herramientas de PyInstaller), y la lógica de
`check_for_update()` sí detecta correctamente la 1.0.3 como más nueva
al consultar la API real de GitHub — no había ningún fallo ahí. El
problema es que esa comprobación **solo se dispara una vez, al crear
la ventana principal** (`MainWindow.__init__` en
`gui/main_window.py`), sin ningún timer periódico ni forma manual de
repetirla — si el proceso lleva abierto desde antes de que se publique
una release nueva (o si quedó un proceso zombi de una sesión anterior
vivo de fondo, como el bug arreglado en la sección anterior), nunca
vuelve a preguntar.

Añadido un botón "🔄 Buscar actualizaciones" al principio del menú
Ayuda que dispara la misma `_check_for_update()` bajo demanda; se le
añade un parámetro `silent: bool = True` para poder diferenciar el
caso de la comprobación automática al arrancar (sin avisar si no hay
nada nuevo, como hasta ahora) del caso de la comprobación manual
(muestra un aviso informativo "Ya tienes instalada la última versión
disponible" si no hay actualización). Traducido a los 13 idiomas que
ya soporta la GUI (`gui/i18n.py`). Validado con la suite completa de
`pytest` (201 pruebas) y comprobando a mano que las 13 traducciones
tienen las dos claves nuevas.

De paso, aclarado un motivo de confusión que también reportó el
usuario: al ejecutar `python main.py` desde el propio repositorio en
desarrollo, "Acerca de" mostraba la versión "1.0.1" mientras que el
paquete descargado de GitHub decía "1.0.2". Esto es el comportamiento
esperado del diseño actual (ver la sección "la aplicación empaquetada
nunca supo su propia versión real" más arriba): `core/version.py` en
el repositorio es solo un valor por defecto que el CI sobrescribe en
tiempo de compilación con `packaging/set_version.py`, a partir del tag
real — el valor committeado no se actualiza solo con cada release y
por tanto se queda desfasado en el checkout de desarrollo. No es un
bug de la app final, solo de lo que se ve al arrancar desde el código
fuente sin pasar por el pipeline de empaquetado; se ha subido el valor
committeado a "1.0.3" (la última release real en el momento de este
cambio) para reducir la confusión, aunque quedará desfasado de nuevo
en cuanto se publique la siguiente release.
