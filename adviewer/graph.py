import asyncio
import logging
import threading

from qtpy import QtWidgets, QtCore

from ophyd import CamBase
import qtpynodeeditor
from typhon.utils import raise_to_operator

from . import utils
from . import data_model

logger = logging.getLogger(__name__)


class PortTreeWidget(QtWidgets.QTreeWidget):
    'Tree representation of AreaDetector port graph'
    port_selected = QtCore.Signal(str)

    def __init__(self, monitor, parent=None):
        super().__init__(parent=parent)
        self.monitor = monitor
        self.port_to_item = {}
        self.setDragEnabled(True)
        self.setDragDropMode(self.InternalMove)
        self.monitor.update.connect(self._ports_updated)
        self.headerItem().setHidden(True)

        def item_changed(current, previous):
            self.port_selected.emit(str(current.text(0)))

        self.currentItemChanged.connect(item_changed)

    def dropEvent(self, ev):
        dragged_to = self.itemAt(ev.pos())
        if dragged_to is None:
            ev.ignore()
            return

        source_port = dragged_to.text(0)
        dest_port = self.currentItem().text(0)

        try:
            self.monitor.set_new_source(source_port, dest_port)
        except Exception as ex:
            raise_to_operator(ex)
        else:
            super().dropEvent(ev)

    def _get_port(self, name):
        try:
            return self.port_to_item[name]
        except KeyError:
            twi = QtWidgets.QTreeWidgetItem([name])
            self.port_to_item[name] = twi
            return twi

    def _ports_updated(self, ports_removed, ports_added, edges_removed,
                       edges_added):
        root = self.invisibleRootItem()
        for item in self.port_to_item.values():
            parent = (root if item.parent() is None
                      else item.parent())
            parent.takeChild(parent.indexOfChild(item))

        monitor = self.monitor
        edges = monitor.edges
        cams = monitor.cameras
        for cam in cams:
            item = self._get_port(cam)
            self.addTopLevelItem(item)

        for src, dest in sorted(edges):
            src_item = self._get_port(src)
            dest_item = self._get_port(dest)

            old_parent = dest_item.parent()
            if old_parent is not None:
                old_parent.removeChild(dest_item)
            if src_item != dest_item:
                src_item.addChild(dest_item)

        for item in self.port_to_item.values():
            item.setExpanded(True)


class PortGraphMonitor(QtCore.QObject):
    '''Monitors the connectivity of all AreaDetector ports in a detector

    Parameters
    ----------
    detector : ophyd.Detector
        The detector to monitor
    parent : QtCore.QObject, optional
        The parent widget

    Attributes
    ----------
    edge_added : Signal
        An edge was added between (src, dest)
    edge_removed : Signal
        An edge was removed between (src, dest)
    port_added : Signal
        A port was added with name (port_name, )
    update : Signal
        A full batch update including all edges added and removed, ports added
        and removed, with the signature (ports_removed, ports_added,
        edges_removed, edges_added), all of which are lists of strings.
    '''
    edge_added = QtCore.Signal(str, str)
    edge_removed = QtCore.Signal(str, str)
    port_added = QtCore.Signal(str)
    port_removed = QtCore.Signal(str)
    update = QtCore.Signal(list, list, list, list)
    port_information_attrs = ['plugin_type', 'ad_core_version',
                              'driver_version']

    def __init__(self, detector, parent=None):
        super().__init__(parent=parent)
        self.known_ports = []
        self.edges = set()
        self.positions = {}
        self.detector = detector
        self.lock = threading.Lock()
        self._port_map = {}
        self._port_information = {}
        self._subscriptions = {}

    def update_port_map(self):
        'Update the port map'
        self.detector.wait_for_connection()
        self._port_map.clear()
        self._port_map.update(**self.detector.get_asyn_port_dictionary())

        self._port_information.clear()
        self._port_information.update(
            **{port: self.get_port_information(port)
               for port in self._port_map
               }
        )

    @property
    def port_map(self):
        'Port map of {port_name: ophyd_plugin}'
        if not self._port_map:
            self.update_port_map()
        return dict(self._port_map)

    @property
    def port_information(self):
        'Map of {port_name: dict(information_key=...)}'
        if not self._port_map:
            self.update_port_map()
        return dict(self._port_information)

    def get_port_information(self, port):
        'Get information on a specific port/plugin'
        info = {}
        plugin = self.port_map[port]
        for attr in self.port_information_attrs:
            try:
                info[attr] = getattr(plugin, attr).get()
            except AttributeError:
                ...
        return info

    def get_edges(self):
        '''Get an updated list of the directed graph edges

        Returns
        -------
        edges : set
            List of (src, dest)
        '''
        edges = set()
        for out_port, cpt in self.port_map.items():
            try:
                in_port = cpt.nd_array_port.get()
            except AttributeError:
                ...
            else:
                edges.add((in_port, out_port))

        return edges

    def set_new_source(self, source_port, dest_port):
        '''Set a new source port for a plugin

        Parameters
        ----------
        source_port : str
            The source port (e.g., CAM1)
        dest_port : str
            The destination port (e.g., ROI1)
        '''
        logger.info('Attempting to connect %s -> %s', source_port, dest_port)
        try:
            source_plugin = self.port_map[source_port]
            dest_plugin = self.port_map[dest_port]
        except KeyError as ex:
            raise ValueError(
                f'Invalid source/destination port: {ex}') from None

        if source_plugin == dest_plugin or source_port == dest_port:
            raise ValueError('Cannot connect a port to itself')

        try:
            signal = dest_plugin.nd_array_port
        except AttributeError:
            raise ValueError(f'Destination plugin {dest_plugin} does not '
                             f'have an input')
        else:
            signal.put(source_port, wait=False)

    @property
    def cameras(self):
        'All camera port names'
        return [port
                for port, plugin in self.port_map.items()
                if isinstance(plugin, CamBase)]

    def _port_changed_callback(self, value=None, obj=None, **kwargs):
        logger.debug('Source port of %s changed to %s', obj.name, value)
        self.update_ports()

    def update_ports(self):
        'Read the port digraph/dictionary from the detector and emit updates'
        with self.lock:
            port_map = self.port_map
            edges = utils.break_cycles(self.get_edges())
            for port, plugin in sorted(port_map.items()):
                if (port not in self._subscriptions and
                        hasattr(plugin, 'nd_array_port')):
                    logger.debug('Subscribing to port %s (%s) NDArrayPort',
                                 port, plugin.name)
                    self._subscriptions[port] = plugin.nd_array_port.subscribe(
                        self._port_changed_callback, run=False)

            ports_removed = list(sorted(set(self.known_ports) - set(port_map)))
            ports_added = list(sorted(set(port_map) - set(self.known_ports)))
            edges_removed = list(sorted(set(self.edges) - set(edges)))
            edges_added = list(sorted((set(edges) - set(self.edges))))

            self.edges = edges
            self.known_ports = list(port_map)

            for port in ports_removed:
                sub = self._subscriptions.pop(port, None)
                if sub is not None:
                    plugin = port_map[port].nd_array_port.unsubscribe(sub)

        for port in ports_removed:
            self.port_removed.emit(port)

        for port in ports_added:
            self.port_added.emit(port)

        for src, dest in edges_removed:
            self.edge_removed.emit(src, dest)

        for src, dest in edges_added:
            self.edge_added.emit(src, dest)

        if ports_removed or ports_added or edges_removed or edges_added:
            self.update.emit(ports_removed, ports_added, edges_removed,
                             edges_added)


class PortGraphFlowchart(QtWidgets.QWidget):
    '''
    A flow chart representing one AreaDetector's port connectivity

    Parameters
    ---------- detector : ophyd.Detector
        The detector to monitor
    parent : QtCore.QObject, optional
        The parent widget
    '''

    flowchart_updated = QtCore.Signal()
    port_selected = QtCore.Signal(str)
    configure_request = QtCore.Signal(str, object)

    def __init__(self, monitor, *, parent=None):
        super().__init__(parent=parent)

        self.monitor = monitor
        self.detector = monitor.detector

        self.monitor.update.connect(self._ports_updated)

        self.registry = qtpynodeeditor.DataModelRegistry()
        for ophyd_cls, model in data_model.models.items():
            print('Register model', ophyd_cls, model, model.name)
            self.registry.register_model(model, category='Area Detector')

        self.scene = qtpynodeeditor.FlowScene(
            registry=self.registry, allow_node_deletion=False,
            allow_node_creation=False)

        self.scene.connection_created.connect(self._user_connected_nodes)
        self.scene.connection_deleted.connect(self._user_deleted_connection)

        self.view = qtpynodeeditor.FlowView(self.scene)
        self.view.setMinimumSize(400, 400)

        self.layout = QtWidgets.QVBoxLayout()
        self.dock = QtWidgets.QDockWidget()
        self.layout.addWidget(self.view)
        self.setLayout(self.layout)

        self._nodes = {}
        self._edges = set()
        self._auto_position = True

    def _user_deleted_connection(self, conn):
        src_node, dest_node = conn.nodes
        try:
            cam = self.monitor.cameras[0]
            self.monitor.set_new_source(cam, dest_node)
        except Exception as ex:
            raise_to_operator(ex)

    def _user_connected_nodes(self, conn):
        dest_node, src_node = conn.nodes
        src, dest = src_node.model.port_name, dest_node.model.port_name
        if (src, dest) in self._edges:
            return

        try:
            self.monitor.set_new_source(src, dest)
        except Exception as ex:
            raise_to_operator(ex)

    @property
    def nodes(self):
        'Nodes in the scene'
        return dict(self._nodes)

    @property
    def edges(self):
        'Set of (src, dest) ports that make up the AreaDetector port graph'
        return self.monitor.edges

    def _ports_updated(self, ports_removed, ports_added, edges_removed,
                       edges_added):
        self.port_map = self.monitor.port_map

        for src, dest in edges_removed:
            try:
                src_info = self._nodes[src]
                dest_info = self._nodes[dest]
            except KeyError:
                logger.debug('Edge removed that did not connect a known port, '
                             'likely in error: %s -> %s', src, dest)
                continue

            src_node = src_info['node']
            dest_node = dest_info['node']

            # TODO keeping track of connections like this is less than ideal...
            conn = src_info['connections'].pop(dest_node, None)
            dest_info['connections'].pop(src_node, None)

            if conn is not None:
                self.scene.delete_connection(conn)

            self._edges.remove((src, dest))

        for port in ports_removed:
            node = self._nodes.pop(port)
            self.scene.remove_node(node)

        for port in ports_added:
            plugin = self.port_map[port]
            self._nodes[port] = dict(node=self.add_port(port, plugin),
                                     plugin=plugin,
                                     connections={},
                                     )

        for src, dest in edges_added:
            try:
                src_node = self._nodes[src]['node']
                dest_node = self._nodes[dest]['node']
            except KeyError:
                # Scenarios:
                #  1. Invalid port name used
                #  2. Associated plugin missing from the Detector class
                logger.debug('Edge added to unknown port: %s -> %s', src, dest)
                continue

            self._edges.add((src, dest))

            if src_node != dest_node:
                try:
                    connection = self.scene.create_connection(
                        src_node['output'][0],
                        dest_node['input'][0],
                    )
                except Exception:
                    logger.exception('Failed to connect terminals %s -> %s',
                                     src, dest)
                else:
                    self._nodes[src]['connections'][dest] = connection
                    self._nodes[dest]['connections'][src] = connection

        if self._auto_position:
            positions = utils.position_nodes(
                self._edges, self.port_map,
                x_spacing=150.0,  # TODO less magic numbers
                y_spacing=100.0,
            )
            for port, (px, py) in positions.items():
                node = self._nodes[port]['node']
                node.graphics_object.setPos(QtCore.QPointF(px, py))

        self.flowchart_updated.emit()

    def add_port(self, name, plugin, pos=None):
        model = data_model.get_node_data_model(plugin)
        node = self.scene.create_node(model)
        node.model.port_name = name
        node.model.caption = name
        return node


class PortGraphWindow(QtWidgets.QMainWindow):
    def __init__(self, detector, *, parent=None):
        super().__init__(parent=parent)

        self._interface_ready = threading.Event()
        self.detector = detector
        self.setWindowTitle(f'adviewer - {self.detector.name}')

        self.monitor = PortGraphMonitor(detector, parent=self)
        self.chart = PortGraphFlowchart(self.monitor)
        self.tree = PortTreeWidget(self.monitor)
        self.info_label = QtWidgets.QLabel()
        self.loop = asyncio.get_event_loop()

        self.setCentralWidget(self.chart.view)

        self.tree_dock = QtWidgets.QDockWidget('Port &Tree')
        self.tree_dock.setWidget(self.tree)

        self.info_dock = QtWidgets.QDockWidget('Port &Info')
        self.info_dock.setWidget(self.info_label)

        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.tree_dock)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.info_dock)

        self.chart.scene.node_hovered.connect(self._user_node_hovered)
        self.chart.flowchart_updated.connect(self.tree.update)

        self.chart.view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.chart.view.customContextMenuRequested.connect(self._user_context_menu)
        self.tree.port_selected.connect(self._tree_port_selected)
        threading.Thread(target=self._startup).start()

    def _tree_port_selected(self, port_name):
        try:
            node = self.chart.nodes[port_name]['node']
        except KeyError:
            return

        self.chart.view.centerOn(node.graphics_object)
        self.chart.scene.clearSelection()
        self._user_node_hovered(node, pos=None)

    def _user_context_menu(self, pos):
        menu = self.createPopupMenu()
        menu.exec_(self.chart.view.mapToGlobal(pos))

    def _user_node_hovered(self, node, pos):
        info = data_model.summarize_node(
            node, port_information=self.monitor.port_information)
        info_text = [node.model.port_name]
        for category, info in info.items():
            info_text.append(f'<b>{category}</b>')
            for sub_category, item in info.items():
                prefix = f'- {sub_category}: '
                if isinstance(item, list):
                    info_text.append(
                        '{} {}'.format(prefix,
                                       ', '.join(str(s) for s in item)))
                else:
                    info_text.append(f'{prefix} {item}')

        self.info_label.setText('<br>'.join(str(s) for s in info_text))

    def _startup(self):
        asyncio.set_event_loop(self.loop)
        self.monitor.update_ports()
        self._interface_ready.set()
