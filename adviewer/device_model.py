import logging
import threading
import time

from qtpy import QtCore, QtWidgets
from qtpy.QtCore import QSortFilterProxyModel, Qt, QThread, Signal

import numpy as np
import ophyd
import ophyd.device


logger = logging.getLogger(__name__)


class _DevicePollThread(QThread):
    data_changed = Signal(str)

    def __init__(self, device, data, poll_rate, *, parent=None):
        super().__init__(parent=parent)
        self.device = device
        self.data = data
        self.poll_rate = poll_rate

    def run(self):
        self.running = True
        attrs = set(self.data)

        # Instantiate all signals first
        with ophyd.device.do_not_wait_for_lazy_connection(self.device):
            for attr in list(attrs):
                try:
                    getattr(self.device, attr)
                except Exception:
                    logger.exception(
                        'Poll thread for %s.%s @ %.3f sec failure '
                        'on initial access',
                        self.device.name, attr, self.poll_rate
                    )
                    attrs.remove(attr)

        while self.running:
            t0 = time.monotonic()
            for attr in list(attrs):
                setpoint = None
                try:
                    sig = getattr(self.device, attr)

                    if hasattr(sig, 'get_setpoint'):
                        setpoint = sig.get_setpoint()
                    elif hasattr(sig, 'setpoint'):
                        setpoint = sig.setpoint
                    readback = sig.get()
                except Exception:
                    logger.exception(
                        'Poll thread for %s.%s @ %.3f sec failure',
                        self.device.name, attr, self.poll_rate
                    )
                    attrs.remove(attr)
                    continue

                new_data = {}
                if readback is not None:
                    new_data['readback'] = readback
                if setpoint is not None:
                    new_data['setpoint'] = setpoint

                for key, value in new_data.items():
                    old_value = self.data[attr][key]

                    try:
                        changed = np.any(old_value != value)
                    except Exception:
                        ...

                    if changed or old_value is None:
                        self.data[attr].update(**new_data)
                        self.data_changed.emit(attr)
                        break

            elapsed = time.monotonic() - t0
            time.sleep(max((0, self.poll_rate - elapsed)))


COL_SETPOINT = 2


def _create_data_dict(device):
    def create_data(attr):
        inst = getattr(device, attr)
        return dict(pvname=getattr(inst, 'pvname', '(Python)'),
                    readback=None,
                    setpoint=None,
                    )

    data = {
        attr: create_data(attr)
        for attr in device.component_names
        if attr not in device._sub_devices
    }

    for sub_name in device._sub_devices:
        sub_dev = getattr(device, sub_name)
        sub_data = {f'{sub_name}.{key}': value
                    for key, value in _create_data_dict(sub_dev).items()
                    }
        data.update(sub_data)

    return data


class PolledDeviceModel(QtCore.QAbstractTableModel):
    def __init__(self, device, *, poll_rate=1.0, parent=None, **kwargs):
        super().__init__(parent=parent, **kwargs)
        self.device = device
        self.poll_rate = float(poll_rate)
        self._polling = False
        self.poll_thread = None

        self._data = _create_data_dict(device)
        self.horizontal_header = [
            'Attribute', 'Readback', 'Setpoint', 'PV Name',
        ]
        self.start()

    def start(self):
        'Start the polling thread'
        if self._polling:
            return

        self._polling = True
        self._poll_thread = _DevicePollThread(
            self.device, self._data, self.poll_rate,
            parent=self)
        self._poll_thread.data_changed.connect(self._data_changed)
        self._poll_thread.start()

    def _data_changed(self, attr):
        row = list(self._data).index(attr)
        self.dataChanged.emit(self.createIndex(row, 0),
                              self.createIndex(row, self.columnCount(0)))

    def stop(self):
        thread = self._poll_thread
        if self._polling or not thread:
            return

        thread.running = False
        self._poll_thread = None
        self._polling = False

    def _row_to_data(self, row):
        'Returns (attr, data)'
        key = list(self._data)[row]
        return (key, self._data[key])

    def hasChildren(self, index):
        # TODO sub-devices?
        return False

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return self.horizontal_header[section]

    def setData(self, index, value, role=Qt.EditRole):
        row = index.row()
        column = index.column()
        attr, info = self._row_to_data(row)

        if role != Qt.EditRole or column != COL_SETPOINT:
            return False

        attr, info = self._row_to_data(row)
        obj = getattr(self._poll_thread.device, attr)

        def set_thread():
            try:
                logger.debug('Setting %s = %r', obj.name, value)
                st = obj.set(value)
                ophyd.status.wait(st, timeout=2)
            except Exception as ex:
                logger.exception('Failed to set %s to %r', obj.name, value)
            else:
                logger.debug('Set complete: %s = %r (%s)', obj.name, value, st)

        self._set_thread = threading.Thread(target=set_thread, daemon=True)
        self._set_thread.start()
        return True

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == COL_SETPOINT:
            return flags | Qt.ItemIsEditable
        return flags

    def data(self, index, role):
        row = index.row()
        column = index.column()
        attr, info = self._row_to_data(row)

        if role == Qt.EditRole and column == COL_SETPOINT:
            return info['setpoint']

        if role == Qt.DisplayRole:
            # value = attr
            columns = {
                0: attr,
                1: info['readback'],
                2: info['setpoint'] or '',
                3: info['pvname'],
            }
            return str(columns[column])

    def columnCount(self, index):
        return 4

    def rowCount(self, index):
        return len(self._data)


class DeviceView(QtWidgets.QTableView):
    def __init__(self, device, parent=None):
        super().__init__(parent=parent)
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setFilterKeyColumn(-1)
        self.proxy_model.setDynamicSortFilter(True)
        self.setModel(self.proxy_model)

        self.models = {}
        self._device = None

        # Set the property last
        self.device = device

    def clear(self):
        for model in self.models.values():
            model.stop()
        self.models.clear()
        self._device = None

    @property
    def device(self):
        return self._device

    @device.setter
    def device(self, device):
        if device is self._device:
            return

        if self._device is not None:
            self.models[device].stop()

        self._device = device
        if device:
            try:
                model = self.models[device]
            except KeyError:
                model = PolledDeviceModel(device=device)
                self.models[device] = model

            model.start()

            self.proxy_model.setSourceModel(model)


class DeviceWidget(QtWidgets.QFrame):
    closed = Signal()

    def __init__(self, device, parent=None):
        super().__init__(parent=parent)

        self.setWindowTitle(device.name)

        self.setMinimumSize(500, 400)

        self.filter_label = QtWidgets.QLabel('&Filter')
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_label.setBuddy(self.filter_edit)

        def set_filter(text):
            self.view.proxy_model.setFilterRegExp(text)

        self.filter_edit.textEdited.connect(set_filter)
        self.view = DeviceView(device=device)
        self.layout = QtWidgets.QGridLayout()

        self.layout.addWidget(self.filter_label, 0, 0)
        self.layout.addWidget(self.filter_edit, 0, 1)
        self.layout.addWidget(self.view, 1, 0, 1, 2)
        self.setLayout(self.layout)

    def closeEvent(self, ev):
        super().closeEvent(ev)
        self.view.clear()
        self.closed.emit()
