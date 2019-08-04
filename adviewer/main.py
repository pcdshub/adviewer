import logging
import sys
import threading

from qtpy import QtWidgets, QtCore
from qtpy.QtCore import Qt, Signal

from ophyd import CamBase
import qtpynodeeditor
from typhon.utils import raise_to_operator
from . import discovery


logger = logging.getLogger(__name__)


class DetectorModel(QtCore.QAbstractTableModel):
    have_adcore_version = Signal()
    new_component = Signal()

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent=parent, **kwargs)
        self.components = {}
        self.checked_components = set()
        self._adcore_version = None

        self.horizontal_header = [
            'Prefix', 'Class', 'Info'
        ]

    def to_ophyd_class_code(self, base_class='DetectorBase', indent=' ' * 4):
        for suffix, info in self.components.items():
            identifier = discovery.category_to_identifier(prefix)
            class_ = info['class_']
            yield f'{indent}    {identifier} = Cpt({class_}, {suffix!r})'

    @property
    def adcore_version(self):
        return self._adcore_version

    @adcore_version.setter
    def adcore_version(self, adcore_version):
        if isinstance(adcore_version, str):
            adcore_version = discovery.version_tuple_from_string(adcore_version)

        self._adcore_version = adcore_version
        if adcore_version is not None:
            self.have_adcore_version.emit()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return self.horizontal_header[section]

    def setData(self, index, value, role):
        column = index.column()
        if column != 0 or role != Qt.CheckStateRole:
            return False

        row = index.row()
        with self.lock:
            key = list(self.components)[row]
            if value:
                self.checked_components.add(key)
            else:
                self.checked_components.remove(key)
        return True

    def flags(self, index):
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            return flags | Qt.ItemIsUserCheckable
        return flags

    def data(self, index, role):
        row = index.row()
        column = index.column()
        suffix, info = list(self.components.items())[row]
        if role == QtCore.Qt.CheckStateRole:
            if index.column() == 0:
                return (QtCore.Qt.Checked
                        if suffix in self.checked_components
                        else QtCore.Qt.Unchecked
                        )

        elif role == Qt.DisplayRole:
            columns = {
                0: suffix,
                1: info['class_'].__name__,
                2: info['info'],
            }
            return str(columns[column])

    def columnCount(self, index):
        return 3

    def rowCount(self, index):
        return len(self.components)

    def _update_component(self, category, class_, info):
        with self.lock:
            # TODO: determine if... more specific (?) than last time
            new_row = category not in self.components
            self.components[category] = dict(
                class_=class_,
                info=info,
            )
            row = list(self.components).index(category)

        if new_row:
            self.checked_components.add(category)
            self.layoutAboutToBeChanged.emit()

        self.dataChanged.emit(self.createIndex(row, 0),
                              self.createIndex(row, self.columnCount(0)))

        if self._adcore_version is None:
            adcore_version = info.get('adcore_version', None)
            if adcore_version:
                self.adcore_version = adcore_version

        if new_row:
            self.layoutChanged.emit()
            self.new_component.emit()


class DetectorFromChannelAccessModel(DetectorModel):
    component_updated = Signal(str, object, dict)

    def __init__(self, prefix, **kwargs):
        super().__init__(**kwargs)

        self.prefix = prefix
        self.lock = threading.RLock()
        self.cams = discovery.find_cams_over_channel_access(
                prefix, callback=self._cam_callback)
        self.plugins = discovery.find_plugins_over_channel_access(
                prefix, callback=self._plugin_callback)
        self.pending_plugins = []

        self.component_updated.connect(self._update_component)
        self.have_adcore_version.connect(self._adcore_version_received)

    def _adcore_version_received(self):
        with self.lock:
            for pending_plugin in list(self.pending_plugins):
                self._plugin_callback(category=pending_plugin)
            self.pending_plugins.clear()

    def _cam_callback(self, *, pv, category, **kwargs):
        with self.lock:
            try:
                cls = discovery.get_cam_from_info(**self.cams.info[category])
            except Exception as ex:
                logger.debug('get_cam_from_info failed', exc_info=ex)
                return

            self.component_updated.emit(
                category, cls, self.cams.info[category]
            )


    def _plugin_callback(self, *, category, **kwargs):
        with self.lock:
            if not self.adcore_version:
                # no cams yet - so we don't have the core version
                if category not in self.pending_plugins:
                    self.pending_plugins.append(category)
                return

            try:
                cls = discovery.get_plugin_from_info(
                    adcore_version=self.adcore_version, **self.plugins.info[category])
            except Exception as ex:
                logger.error('get_plugin_from_info failed', exc_info=ex)
                return

            self.component_updated.emit(
                category, cls, self.plugins.info[category]
            )


class DetectorView(QtWidgets.QTableView):
    def __init__(self, prefix, parent=None):
        super().__init__(parent=parent)
        self._prefix = None
        self.model = None
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)

        # Set the property last
        self.prefix = prefix

    @property
    def prefix(self):
        return self._prefix

    @prefix.setter
    def prefix(self, prefix):
        self._prefix = prefix
        if prefix:
            if self.model is not None:
                ...
            self.model = DetectorFromChannelAccessModel(prefix=prefix)
            self.setModel(self.model)


class DiscoveryWidget(QtWidgets.QFrame):
    def __init__(self, prefix=None, parent=None):
        super().__init__(parent=parent)

        self.view = DetectorView(prefix=prefix)
        self.layout = QtWidgets.QGridLayout()

        self.prefix_edit = QtWidgets.QLineEdit(prefix)
        self.ophyd_class_button = QtWidgets.QPushButton('Ophyd Class...')
        self.graph_button = QtWidgets.QPushButton('Node Graph...')

        self.layout.addWidget(QtWidgets.QLabel('Prefix'), 0, 0)
        self.layout.addWidget(self.prefix_edit, 0, 1)
        self.layout.addWidget(self.view, 1, 0, 1, 2)
        self.layout.addWidget(self.ophyd_class_button, 2, 0)
        self.layout.addWidget(self.graph_button, 2, 1)
        self.setLayout(self.layout)

        self.ophyd_class_button.clicked.connect(self.create_ophyd_class)

    def create_ophyd_class(self):
        model = self.view.model
        if not model:
            print('model unset?')
            return

        print()
        print()
        for line in model.to_ophyd_class_code():
            print(line)
        print()
        print()


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    w = DiscoveryWidget(prefix='13SIM1:')
    w.show()
    app.exec_()
