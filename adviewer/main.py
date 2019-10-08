import argparse
import logging
import sys
import threading

from qtpy import QtCore, QtWidgets
from qtpy.QtCore import Qt, Signal, QSortFilterProxyModel

import ophyd

from . import discovery, graph
from .utils import locked

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

        for suffix, info in sorted(self.components.items(),
                                   key=lambda item: item[0].lower()):
            if suffix in self.checked_components:
                attr = discovery.category_to_identifier(suffix)
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
            for suffix, component in sorted(self.components.items())
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
        yield f'from ophyd import Component as Cpt'
        yield f'from ophyd.areadetector.cam import ({cam_imports})'
        yield f'from ophyd.areadetector.detectors import DetectorBase'

        plugin_imports = ', '.join(
            sorted(cls.__name__ for cls in plugin_classes))
        yield f'from ophyd.areadetector.plugins import ({plugin_imports})'

        yield ''

        if not class_name:
            class_name = discovery.category_to_identifier(prefix).capitalize()
            if class_name.startswith('_'):
                class_name = 'Detector' + class_name.lstrip('_').capitalize()
        driver_version = discovery.version_tuple_from_string(
            self.get_driver_version())

        yield (f'class {class_name}({base_class.__name__}, '
               f'version={driver_version}):')

        for suffix, info in sorted(checked_components.items(),
                                   key=lambda item: item[0].lower()):
            identifier = discovery.category_to_identifier(suffix)
            class_ = info['class_'].__name__
            yield f'    {identifier} = Cpt({class_}, {suffix!r})'

        yield ''
        yield ''
        yield f'det = {class_name}({prefix!r}, name="det")'

    @property
    def adcore_version(self):
        return self._adcore_version

    @adcore_version.setter
    def adcore_version(self, adcore_version):
        if isinstance(adcore_version, str):
            adcore_version = discovery.version_tuple_from_string(
                adcore_version)

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
            if column == 0:
                return suffix
            if column == 1:
                return info['class_'].__name__
            if column == 2:
                return '/'.join(info['info'].values())

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

    @property
    def pvlist(self):
        return list(
            sorted(
                pv.pvname
                for status in (self.cams, self.plugins)
                for pv in status.pvs
                if pv.connected
            )
        )

    @locked
    def _adcore_version_received(self):
        for pending_plugin in list(self.pending_plugins):
            self._plugin_callback(category=pending_plugin,
                                  status=self.plugins)
        self.pending_plugins.clear()

    @locked
    def _cam_callback(self, *, pv, category, status, **kwargs):
        cam_info = dict(status.info[category])
        if not all(key in cam_info for key in ('model', 'manufacturer', )):
            return

        try:
            cls = discovery.get_cam_from_info(
                manufacturer=cam_info['manufacturer'],
                model=cam_info['model'],
                adcore_version=cam_info.get('adcore_version', None),
                driver_version=cam_info.get('driver_version', None),
            )
        except Exception as ex:
            logger.debug('get_cam_from_info failed', exc_info=ex)
            return

        self.component_updated.emit(
            category, cls, cam_info
        )

    @locked
    def _plugin_callback(self, *, category, status, **kwargs):
        if not self.adcore_version:
            # no cams yet - so we don't have the core version
            if category not in self.pending_plugins:
                self.pending_plugins.append(category)
            return
        plugin_info = dict(status.info[category])
        if 'plugin_type' not in plugin_info:
            return

        try:
            cls = discovery.get_plugin_from_info(
                adcore_version=self.adcore_version,
                plugin_type=plugin_info['plugin_type']
            )
        except Exception as ex:
            logger.error('get_plugin_from_info failed', exc_info=ex)
            return

        self.component_updated.emit(
            category, cls, plugin_info
        )


class DetectorFromPrefixModel(DetectorFromChannelAccessModel):
    def __init__(self, prefix, **kwargs):
        super().__init__(**kwargs)

        self.component_updated.connect(self._update_component)
        self.have_adcore_version.connect(self._adcore_version_received)

        self.prefix = prefix
        self.lock = threading.RLock()
        self.pending_plugins = []
        self.cams = discovery.find_cams_over_channel_access(
            prefix, callback=self._cam_callback)
        self.plugins = discovery.find_plugins_over_channel_access(
            prefix, callback=self._plugin_callback)


class DetectorFromPVListModel(DetectorFromChannelAccessModel):
    def __init__(self, pvlist, **kwargs):
        super().__init__(**kwargs)

        self.full_pvlist = list(sorted(set(pvlist)))
        self.lock = threading.RLock()
        self.pending_plugins = []
        self.component_updated.connect(self._update_component)
        self.have_adcore_version.connect(self._adcore_version_received)

        self.cams, self.plugins = discovery.cams_and_plugins_from_pvlist(
            self.full_pvlist,
            cam_callback=self._cam_callback,
            plugin_callback=self._plugin_callback
        )
        self.prefix = self.cams.prefix

    @property
    def pvlist(self):
        return self.full_pvlist


class DetectorView(QtWidgets.QTableView):
    def __init__(self, prefix, parent=None):
        super().__init__(parent=parent)
        self._prefix = None
        self._pvlist = None
        self._pvlist_key = None
        self.models = {}
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setDynamicSortFilter(True)
        self.proxy_model.setSortCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.setModel(self.proxy_model)
        self.setSortingEnabled(True)

        # Set the property last
        self.prefix = prefix

    @property
    def model(self):
        if self._pvlist_key:
            return self.models.get(self._pvlist_key, None)
        return self.models.get(self._prefix, None)

    @property
    def prefix(self):
        return self._prefix

    @prefix.setter
    def prefix(self, prefix):
        if prefix == self._prefix:
            return

        logger.info('New prefix: %s', prefix)
        self._prefix = prefix
        self._pvlist = None
        self._pvlist_key = None

        if prefix:
            try:
                model = self.models[prefix]
            except KeyError:
                model = DetectorFromPrefixModel(prefix=prefix)
                self.models[prefix] = model

            self.proxy_model.setSourceModel(model)

            header = self.horizontalHeader()
            for col in range(3):
                header.setSectionResizeMode(
                    col, QtWidgets.QHeaderView.ResizeToContents)

    @property
    def pvlist(self):
        return self.model.pvlist

    @pvlist.setter
    def pvlist(self, pvlist):
        pvlist = list(sorted(set(pvlist)))
        unique_id = '/'.join(pvlist)
        pvlist_key = f'pvlist_{hash(unique_id)}'

        if pvlist_key == self._pvlist_key:
            return

        self._pvlist = pvlist
        self._pvlist_key = pvlist_key

        try:
            model = self.models[self._pvlist_key]
        except KeyError:
            model = DetectorFromPVListModel(pvlist=self._pvlist)
            self.models[self._pvlist_key] = model

        logger.info(
            'New PV list (%d PVs) - searching for %d cam PVs, %d plugin PVs',
            len(pvlist), len(model.cams.pvs), len(model.plugins.pvs)
        )

        self._prefix = model.prefix
        self.proxy_model.setSourceModel(model)

        header = self.horizontalHeader()
        for col in range(3):
            header.setSectionResizeMode(
                col, QtWidgets.QHeaderView.ResizeToContents)


class DiscoveryWidget(QtWidgets.QFrame):
    def __init__(self, prefix=None, parent=None):
        super().__init__(parent=parent)

        self.setWindowTitle('adviewer: Channel Access search')

        self.setMinimumSize(500, 400)

        self.view = DetectorView(prefix=prefix)
        self.layout = QtWidgets.QGridLayout()

        self.prefix_edit = QtWidgets.QLineEdit(prefix)

        self.load_pvlist_button = QtWidgets.QToolButton(self)
        self.load_pvlist_button.setToolTip('&Load PV list...')
        self.load_pvlist_button.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton)
        )
        self.load_pvlist_button.clicked.connect(self.load_pvlist)

        self.save_pvlist_button = QtWidgets.QToolButton(self)
        self.save_pvlist_button.setToolTip('&Save PV list...')
        self.save_pvlist_button.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton)
        )
        self.save_pvlist_button.clicked.connect(self.save_pvlist)

        self.ophyd_class_button = QtWidgets.QPushButton('Ophyd Class...')
        self.graph_button = QtWidgets.QPushButton('Node Graph...')

        self.layout.addWidget(QtWidgets.QLabel('Prefix'), 0, 0)
        self.layout.addWidget(self.prefix_edit, 0, 1)

        self.layout.addWidget(self.view, 1, 0, 1, 2)

        self.button_frame = QtWidgets.QFrame()
        self.layout.addWidget(self.button_frame, 2, 0, 2, 0)
        self.setLayout(self.layout)

        frame_layout = QtWidgets.QHBoxLayout()
        frame_layout.addWidget(self.load_pvlist_button)
        frame_layout.addWidget(self.save_pvlist_button)
        frame_layout.addWidget(self.ophyd_class_button)
        frame_layout.addWidget(self.graph_button)
        self.button_frame.setLayout(frame_layout)

        self.prefix_edit.returnPressed.connect(self.prefix_changed)
        self.ophyd_class_button.clicked.connect(self.create_ophyd_class)
        self.graph_button.clicked.connect(self.graph_open)

    def load_pvlist(self, filename=None):
        if not filename:
            filename, filter_ = QtWidgets.QFileDialog.getOpenFileName(
                self, 'Load PV list', '.',
                'PV list (*.pvlist;*.txt);;All files (*.*)'
            )
            if not filename:
                return

        logger.info('Loading PV list from %s', filename)
        with open(filename, 'rt') as f:
            pvlist = f.read().splitlines()

        self.view.pvlist = list(sorted(set(pvlist)))
        self.prefix_edit.setText(self.view.model.prefix)

    def save_pvlist(self, filename=None):
        if not filename:
            filename, filter_ = QtWidgets.QFileDialog.getSaveFileName(
                self, 'Save PV list', '.',
                'PV list (*.pvlist;*.txt);;All files (*.*)'
            )
            if not filename:
                return

        logger.info('Saving PV list to %s', filename)
        with open(filename, 'wt') as f:
            for pv in self.view.pvlist:
                print(pv, file=f)

    def prefix_changed(self):
        new_prefix = self.prefix_edit.text().strip()
        self.view.prefix = new_prefix

    def graph_open(self):
        model = self.view.model
        if not model:
            return

        prefix = self.view.prefix
        class_name = discovery.class_name_from_prefix(prefix)

        cls = model.to_ophyd_class(class_name=class_name)
        instance = cls(prefix, name=f'{class_name}_adviewer')
        instance.wait_for_connection()
        self._graph_window = graph.PortGraphWindow(instance, parent=self)
        self._graph_window.show()

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
        editor.setWindowTitle(f'Detector code for {prefix}')
        editor.setFontFamily('Courier')
        editor.setText(code)
        editor.setReadOnly(True)
        editor.show()
        self._code_editor = editor


def _build_arg_parser():
    parser = argparse.ArgumentParser()
    parser.description = 'adviewer - AreaDetector configurator'

    parser.add_argument(
        'prefix',
        type=str,
        default='13SIM1:',
        nargs='?',
    )

    parser.add_argument(
        '--pvlist', '-p',
        type=str,
        default=None,
    )

    parser.add_argument(
        '--log', '-l', dest='log_level',
        default='DEBUG',
        type=str,
        help='Python logging level (e.g. DEBUG, INFO, WARNING)'
    )

    return parser


def _entry_point():
    parser = _build_arg_parser()
    args = parser.parse_args()
    return main(**vars(args))


def main(prefix, pvlist=None, *, log_level='DEBUG'):
    logger = logging.getLogger('adviewer')
    logger.setLevel(log_level)
    logging.basicConfig()

    app = QtWidgets.QApplication(sys.argv)
    widget = DiscoveryWidget(prefix)
    if pvlist is not None:
        widget.load_pvlist(pvlist)

    widget.show()
    app.exec_()
