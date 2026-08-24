"""Punto 34.7 del backlog (accesibilidad): variantes de `QTableView`/
`QTableWidget` con el menú contextual accesible por teclado. Por
defecto, cuando el menú contextual se abre con la tecla Menú o
Mayús+F10 (en vez de con el botón derecho del ratón), Qt manda la
posición del cursor -- o (0, 0) si el ratón no está sobre la tabla --
en vez de la posición de la fila realmente seleccionada. Como el resto
de la aplicación decide sobre qué fila actuar mirando esa posición
(`table.indexAt(pos)`), sin este arreglo Mayús+F10 abriría siempre el
menú de la primera fila visible en vez de la fila seleccionada -- un
cliente navegable por teclado necesita que Mayús+F10 actúe igual que
el clic derecho, sobre la fila con el foco.

El arreglo no puede ir en `contextMenuEvent()`: el evento de menú
contextual, tanto de ratón como de teclado, se entrega en realidad al
`viewport()` interno de `QAbstractScrollArea` (del que heredan
`QTableView`/`QTableWidget`), no al propio `self` -- así que hay que
interceptarlo en `viewportEvent()`, el gancho que Qt ofrece
precisamente para esto, antes de que se emita `customContextMenuRequested`
con la posición sin corregir."""

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QTableView, QTableWidget


class _KeyboardContextMenuMixin:
    def viewportEvent(self, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.ContextMenu
            and self.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
            and event.reason() == QContextMenuEvent.Reason.Keyboard
        ):
            index = self.currentIndex()
            pos = self.visualRect(index).center() if index.isValid() else event.pos()
            self.customContextMenuRequested.emit(pos)
            return True
        return super().viewportEvent(event)


class AccessibleTableView(_KeyboardContextMenuMixin, QTableView):
    pass


class AccessibleTableWidget(_KeyboardContextMenuMixin, QTableWidget):
    pass
