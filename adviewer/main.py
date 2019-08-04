import logging
import sys
import threading

from qtpy import QtWidgets, QtCore
from qtpy.QtCore import Qt, Signal

import ophyd

from . import discovery


logger = logging.getLogger(__name__)

# TODO this will all break in versions < 2.2 (?) without ADCoreVersion_RBV


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

    def get_driver_version(self):
        # TODO: this should be set when we get a callback
        for cpt in self.components.values():
            try:
                return cpt['info']['driver_version']
            except KeyError:
                ...

        return 'TODO'

    def to_ophyd_class(self, class_name, *, base_class=ophyd.DetectorBase):
        class_dict = {}

        for suffix, info in self.components.items():
            if suffix in self.checked_components:
                identifier = discovery.category_to_identifier(suffix)
                attr = category_to_identifier(plugin_suffix)
                class_dict[attr] = ophyd.Component(info['class_'], suffix)

        return ophyd.device.create_device_from_components(
            name=class_name,
            docstring='Auto-generated AreaDetector instance from adviewer',
            base_class=base_class,
            **class_dict
        )

    def to_ophyd_class_code(self, prefix, class_name, *,
                            base_class=ophyd.DetectorBase):
        checked_components = {
            suffix: component
            for suffix, component in self.components.items()
            if suffix in self.checked_components
        }

        classes = {info['class_']
                   for suffix, info in checked_components.items()
                   }
        cam_classes = {cls for cls in classes
                       if issubclass(cls, ophyd.CamBase)
                       }
        plugin_classes = {cls for cls in classes
                          if cls not in cam_classes}

        cam_imports = ', '.join(sorted(cls.__name__ for cls in cam_classes))
        yield f'from ophyd.areadetector.cam import ({cam_imports})'

        plugin_imports = ', '.join(sorted(cls.__name__ for cls in plugin_classes))
        yield f'from ophyd.areadetector.plugins import ({plugin_imports})'

        yield ''

        if not class_name:
            class_name = discovery.category_to_identifier(prefix).capitalize()
            if class_name.startswith('_'):
                class_name = 'Detector' + class_name.lstrip('_').capitalize()
        driver_version = discovery.version_tuple_from_string(self.get_driver_version())

        yield f'class {class_name}({base_class.__name__}, version={driver_version}):'

        for suffix, info in checked_components.items():
            identifier = discovery.category_to_identifier(suffix)
            class_ = info['class_'].__name__
            yield f'    {identifier} = Cpt({class_}, {suffix!r})'

        yield ''
        yield ''
        yield f'# det = {class_name}({prefix!r}, name="det")'

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

            header = self.horizontalHeader()
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)


class DiscoveryWidget(QtWidgets.QFrame):
    def __init__(self, prefix=None, parent=None):
        super().__init__(parent=parent)

        self.setMinimumSize(500, 400)

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
        self.graph_button.clicked.connect(self.graph_open)

    def graph_open(self):
        ...

    def create_ophyd_class(self):
        model = self.view.model
        if not model:
            return

        prefix = self.view.prefix
        code = '\n'.join(
            model.to_ophyd_class_code(
                prefix=prefix,
                class_name=discovery.class_name_from_prefix(prefix)
            )
        )
        print(f'\n\n{code}\n\n')

        editor = QtWidgets.QTextEdit()
        editor.setFontFamily('Courier')
        editor.setText(code)
        editor.setReadOnly(True)
        editor.show()
        self._code_editor = editor


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    w = DiscoveryWidget(prefix='13SIM1:')
    w.show()
    app.exec_()
