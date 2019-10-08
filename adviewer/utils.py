import collections
import functools
import io
import logging
import traceback

import networkx

from ophyd import CamBase
from qtpy import QtWidgets


logger = logging.getLogger(__name__)


def break_cycles(edges):
    '''Break any graph cycles present in the list of edges'''
    dig = networkx.digraph.DiGraph()
    for src, dest in edges:
        dig.add_edge(src, dest)

    while True:
        try:
            cycle = networkx.find_cycle(dig)
        except networkx.NetworkXNoCycle:
            break

        src, dest = cycle[-1]
        logger.warning('Found cycles in port graph: %s; breaking at %s->%s',
                       cycle, src, dest)

        dig.remove_edge(src, dest)
        edges.remove((src, dest))

    return edges


def position_nodes(edges, port_map, *, x_spacing, y_spacing, x=0, y=0):
    '''
    Generate an (x, y) position dictionary for all nodes in the port dictionary

    Parameters
    ----------
    edges : list of (src, dest)
        Directed graph edges that connect source -> destination ports
    port_map : dict
        Dictionary of port name to ophyd plugin
    x_spacing : float
        Horizontal spacing between items
    y_spacing : float
        Horizontal spacing between items
    x : float, optional
        Starting x position
    y : float, optional
        Starting y position
    '''
    def position_port(port, x, y):
        if y < y_minimum[x]:
            y = y_minimum[x]

        y_minimum[x] = y + y_spacing

        positions[port] = (x, y)
        dests = [dest for src, dest in edges
                 if src == port
                 and src != dest
                 and dest not in positions]
        y -= y_spacing * (len(dests) // 2)
        for idx, dest in enumerate(sorted(dests)):
            position_port(dest, x + x_spacing, y + idx * y_spacing)

    cameras = [port for port, cam in port_map.items()
               if isinstance(cam, CamBase)]

    y_minimum = collections.defaultdict(lambda: -len(port_map) * y_spacing)

    start_x = x
    positions = {}

    def get_next_y():
        if positions:
            return y_spacing + max(y for x, y in positions.values())
        else:
            return 0

    # Start with all of the cameras and the plugins connected
    for camera in sorted(cameras):
        position_port(camera, start_x, get_next_y())

    # Add any ports that are otherwise unconnected
    for port in port_map:
        if port not in positions:
            position_port(port, start_x, get_next_y())

    return positions


def locked(func):
    '''
    Instance method decorator which wraps a method call in a `with self.lock`
    block.
    '''
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return func(self, *args, **kwargs)

    return wrapped


def raise_to_operator(exc, execute=True):
    """
    Utility function to show a Python Exception in QMessageBox The type and
    representation of the Exception are shown in a pop-up QMessageBox. The
    entire traceback is available via a drop-down detailed text box in the
    QMessageBox

    Parameters
    ----------
    exc: Exception
    execute: bool, optional
        Whether to execute the QMessageBox
    """
    # Assemble QMessageBox with Exception details
    err_msg = QtWidgets.QMessageBox()
    err_msg.setText(f'{exc.__class__.__name__}: {exc}')
    err_msg.setWindowTitle(type(exc).__name__)
    err_msg.setIcon(QtWidgets.QMessageBox.Critical)

    # Format traceback as detailed text
    with io.StringIO() as handle:
        traceback.print_tb(exc.__traceback__, file=handle)
        handle.seek(0)
        err_msg.setDetailedText(handle.read())

    if execute:
        # Execute
        err_msg.exec_()
    return err_msg
