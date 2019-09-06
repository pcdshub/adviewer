import pytest
from qtpy import QtCore

from adviewer import PortGraphWindow


@pytest.fixture(scope='function')
def fake_detector():
    try:
        from ophyd import (SimDetector, CommonPlugins, SimDetectorCam,
                           Component as Cpt, select_version)
        from ophyd.sim import make_fake_device
    except ImportError as ex:
        pytest.skip(f'ophyd version not compatible ({ex})')

    CommonPlugins_V32 = select_version(CommonPlugins, (3, 2))

    class Detector(SimDetector, CommonPlugins_V32):
        cam = Cpt(SimDetectorCam, 'cam1:')
        cam2 = Cpt(SimDetectorCam, 'cam2:')

    FakeDetector = make_fake_device(Detector)
    det = FakeDetector('13SIM1:', name='det')

    for dotted, subdev in sorted(det.walk_subdevices(include_lazy=True)):
        if hasattr(subdev, 'nd_array_port'):
            subdev.port_name.sim_put(subdev.dotted_name)
            subdev.nd_array_port.sim_put('cam')
        elif hasattr(subdev, 'port_name'):
            subdev.port_name.sim_put(subdev.dotted_name)

    return det


@pytest.fixture(scope='function')
def graph_window(qtbot, fake_detector):
    widget = PortGraphWindow(fake_detector)
    qtbot.addWidget(widget)
    if not widget._interface_ready.wait(2):
        raise RuntimeError('Failed to update ports?')
    return widget


@pytest.fixture(scope='function')
def flow_chart(graph_window):
    return graph_window.chart


@pytest.fixture(scope='function')
def tree(graph_window):
    return graph_window.tree


@pytest.fixture(scope='function')
def monitor(graph_window):
    return graph_window.monitor


def test_monitor(monitor):
    print(list(sorted(monitor.port_map.keys())))
    assert monitor._port_map == monitor.port_map
    assert len(monitor.port_map) == len(monitor.port_information)
    assert monitor.cameras == ['cam', 'cam2']
    # Default configuration: all plugins connected to cam
    # Subtract 2 for: cam and cam2
    assert len(monitor.get_edges()) == (len(monitor.port_map) - 2)


def test_monitor_sources(monitor):
    # connected to itself
    with pytest.raises(ValueError):
        monitor.set_new_source('tiff1', 'tiff1')

    # invalid source/dest port
    with pytest.raises(ValueError):
        monitor.set_new_source('ab', 'cd')

    # no input on cams
    with pytest.raises(ValueError):
        monitor.set_new_source('tiff1', 'cam')

    # actually modify the graph: roi1 -> tiff1
    monitor.set_new_source('roi1', 'tiff1')
    assert ('roi1', 'tiff1') in monitor.get_edges()


def test_graph_smoke(qtbot, monitor, flow_chart):
    print('edges are', flow_chart.edges)


def reload_graph(qtbot, flow_chart):
    flow_chart.monitor.update_ports()
    qtbot.waitSignal(flow_chart.flowchart_updated)


def test_add_edge(qtbot, monitor, flow_chart):
    reload_graph(qtbot, flow_chart)
    monitor.set_new_source('roi1', 'tiff1')
    reload_graph(qtbot, flow_chart)


def test_add_then_remove_edge(qtbot, monitor, flow_chart):
    reload_graph(qtbot, flow_chart)
    monitor.set_new_source('roi1', 'tiff1')
    reload_graph(qtbot, flow_chart)
    monitor.set_new_source('cam', 'tiff1')
    reload_graph(qtbot, flow_chart)


def test_graph_basic(qtbot, fake_detector, flow_chart):
    reload_graph(qtbot, flow_chart)


def test_graph_bad_edge(qtbot, fake_detector, flow_chart):
    fake_detector.tiff1.nd_array_port.sim_put('UNKNOWN')
    reload_graph(qtbot, flow_chart)


def test_graph_cycle(qtbot, fake_detector, flow_chart):
    fake_detector.tiff1.nd_array_port.sim_put('tiff1')
    reload_graph(qtbot, flow_chart)


def get_node(flow_chart, node_name):
    return flow_chart.nodes[node_name]['node']


def test_graph_select_node(qtbot, fake_detector, flow_chart):
    reload_graph(qtbot, flow_chart)

    item = get_node(flow_chart, 'cam').graphics_object
    qtbot.waitSignal(flow_chart.flowchart_updated)

    item.setSelected(True)
    item.setSelected(False)
    # TODO pyqtgraph remnant?


class FakeDragEvent:
    def __init__(self, pos, scene_pos=None, *, button=QtCore.Qt.LeftButton,
                 finish=False):
        self._button = button
        self._finish = finish
        self._pos = pos
        self._scene_pos = scene_pos
        self._accepted = False
        self._ignored = False

    def pos(self):
        return self._pos

    def scenePos(self):
        return self._scene_pos

    def button(self):
        return self._button

    def isStart(self):
        return not self._finish

    def isFinish(self):
        return self._finish

    def accept(self):
        self._accepted = True

    def ignore(self):
        self._ignored = True


def test_graph_connect_output_to_input(qtbot, fake_detector, flow_chart):
    reload_graph(qtbot, flow_chart)

    pytest.skip('TODO')

    cam_node = get_node(flow_chart, 'cam')
    cam_out = cam_node['Out']._graphicsItem
    tiff_node = get_node(flow_chart, 'tiff1')
    tiff_in = tiff_node['In']._graphicsItem

    ev = FakeDragEvent(scene_pos=cam_out.scenePos(), pos=cam_out.pos())
    cam_out.mouseDragEvent(ev)

    ev = FakeDragEvent(scene_pos=tiff_in.scenePos(), pos=tiff_in.pos(),
                       finish=True,
                       )
    cam_out.mouseDragEvent(ev)

    # TODO: this does not find the tiff_in terminal, somehow (see coverage)


def test_tree_drag(qtbot, flow_chart, tree):
    reload_graph(qtbot, flow_chart)

    tree.port_to_item['roi1']
    tree.port_to_item['tiff1']
    # qtbot.mousePress(tree, QtCore.Qt.LeftButton, pos=roi1.pos())
    # qtbot.mouseMove(tree, pos=tiff1.pos())
    # qtbot.mouseRelease(tree, pos=roi1.pos())
    # TODO?


def test_smoke_tree_select(qtbot, flow_chart, tree):
    reload_graph(qtbot, flow_chart)
    tree.port_selected.emit('tiff1')
    tree.port_selected.emit('unknown')
