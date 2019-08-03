import distutils
import logging
import re
import time
import threading
from functools import partial

import epics
import ophyd
import ophyd.areadetector.cam
import ophyd.areadetector.plugins


logger = logging.getLogger(__name__)


_RE_NONALPHA = re.compile('[^0-9a-zA-Z_]+')

# semi-private dict in ophyd
plugin_type_to_class = dict(ophyd.areadetector.plugins._plugin_class)

# Some plugins do not report correctly. This may need to be user-customizable
# at some point:
plugin_type_to_class['NDPluginFile'] = ophyd.areadetector.plugins.NexusPlugin

manufacturer_model_to_cam_class = {
    ('Simulated detector', 'Basic simulator'): ophyd.areadetector.cam.SimDetectorCam,
}
plugin_suffix_regex_to_class = {
    plugin_cls._suffix_re: plugin_cls
    for plugin_cls in plugin_type_to_class.values()
    if hasattr(plugin_cls, '_suffix_re')
}


def connect_to_many(prefix, pv_to_category_and_key, callback):
    '''
    Connect to many PVs, keeping track of the status of connections

    Parameters
    ----------
    prefix : str
        The overall prefix, to identify the ConnectStatus
    pv_to_category_and_key : dict
        Where the key is `pv` and the value is `(category, key)`, this
        will be used to create the resulting dictionary.
    callback : callable or None
        Called on each connection event

    Returns
    -------
    status : ConnectStatus
        A namespace with the following information::
            connected_count - a tally of the number of pvs connected
            info - a dictionary of (category, key) to PV value
            pvs - a list of epics.PV instances

    '''
    def connected(category, key, pv, conn, **kwargs):
        if not conn:
            logger.debug('Disconnected from %s (%s, %s)=%s', pv, category, key,
                         value)
            return

        value = pv.get()
        status.info[category][key] = value
        logger.debug('Connected to %s (%s, %s)=%s', pv, category, key, value)

        with status._lock:
            status.connected_count += 1

        if callback is not None:
            callback(pv=pv, category=category, key=key,
                     value=value, connected_count=connected_count)

    class ConnectStatus:
        _lock = threading.Lock()
        connected_count = 0
        info = {category: {}
                for category, key in pv_to_category_and_key.values()
                }

        def __repr__(self):
            return (f'<ConnectStatus {self.connected_count} '
                    f'connected of {len(self.pvs)}>'
                    )

    status = ConnectStatus()
    status.prefix = prefix
    status.pvs = [
        epics.get_pv(pv,
                     connection_callback=partial(connected, category, key),
                     auto_monitor=False
                     )
        for pv, (category, key) in pv_to_category_and_key.items()
    ]
    return status


def find_cams_over_channel_access(prefix, *, cam_re=r'cam\d:', max_count=2,
                                  callback=None):
    '''
    Find any areaDetector cameras of a certain prefix, given a pattern

    Parameters
    ----------
    prefix : str
        The shared detector prefix, without any cam/plugin
    cam_re : str
        A regular expression to be used
    max_count : int
        Maximum number of cams to search for - [1, max_count]
    callback : callable, optional
        Called on each connection

    Returns
    -------
    status : ConnectStatus
        See `connect_to_many` for full information.
    '''

    suffix_to_key = {
        'ADCoreVersion_RBV': 'core_version',
        'DriverVersion_RBV': 'driver_version',
        'Manufacturer_RBV': 'manufacturer',
        'Model_RBV': 'model',
    }

    cams = [cam_re.replace(r'\d', str(idx))
            for idx in range(1, max_count + 1)
            ]

    pvs = {f'{prefix}{cam}{suffix}': (cam, key)
           for cam in cams
           for suffix, key in suffix_to_key.items()
           }

    return connect_to_many(prefix, pvs, callback)


def version_tuple_from_string(ver_string):
    'AD version string to tuple'
    return tuple(distutils.version.LooseVersion(ver_string).version)


def get_cam_from_info(manufacturer, model, *, core_version=None,
                      driver_version=None,
                      default_class=ophyd.areadetector.cam.AreaDetectorCam):
    '''
    Get a camera class given at least its manufacturer and model
    '''
    cam_class = manufacturer_model_to_cam_class.get((manufacturer, model), default_class)

    core_version = version_tuple_from_string(core_version)
    if driver_version is not None:
        driver_version = version_tuple_from_string(driver_version)
    # TODO mix in new base components using core_version
    # TODO then cam is versioned on driver_version
    return cam_class


def get_plugin_from_info(plugin_type, *, core_version):
    '''
    Get a plugin class given its type and ADCore version
    '''
    if ' ' in plugin_type:
        # HDF5 includes version number, remove it
        plugin_type, _ = plugin_type.split(' ', 1)

    plugin_class = plugin_type_to_class[plugin_type]
    return ophyd.select_version(plugin_class, core_version)


def find_plugins_over_channel_access(
        prefix, *, max_count=5, skip_classes=None, callback=None):
    '''
    Find any areaDetector plugins of a certain prefix. The default ophyd
    patterns are used to determine these, but others can be added via
    `adviewer.discovery.plugin_suffix_regex_to_class`.

    Parameters
    ----------
    prefix : str
        The shared detector prefix, without any cam/plugin
    max_count : int
        Maximum number of cams to search for - [1, max_count]
    skip_classes : list, optional
        Skip these plugin classes in the search
    callback : callable, optional
        Called on each connection

    Returns
    -------
    status : ConnectStatus
        See `connect_to_many` for full information.
    '''

    suffix_to_key = {
        'PluginType_RBV': 'plugin_type',
    }

    skip_classes = skip_classes or []

    plugins = {
        plugin_re.replace(r'\d', str(idx)): {}
        for plugin_re, plugin_cls in plugin_suffix_regex_to_class.items()
        if plugin_cls not in skip_classes
        for idx in range(1, max_count + 1)
    }

    pvs = {f'{prefix}{plugin}{suffix}': (plugin, key)
           for plugin in plugins
           for suffix, key in suffix_to_key.items()
           }

    return connect_to_many(prefix, pvs, callback)


def category_to_identifier(category):
    'Create an identifier name from a category/PV suffix'
    attr = _RE_NONALPHA.sub('_', category.lower())
    attr = attr.strip('_')
    return attr if attr.isidentifier() else f'_{attr}'


def create_detector_class(
        cams, plugins, default_core_version, *, class_name=None,
        base_class=ophyd.ADBase):
    '''
    Create a Detector class with the base `base_class`, including all cameras
    and plugins found from `find_cams_over_channel_access` and
    `find_plugins_over_channel_access`, respectively.

    Parameters
    ----------
    cams : ConnectStatus
        Result from `find_cams_over_channel_access`
    plugins : ConnectStatus
        Result from `find_plugins_over_channel_access`
    class_name : str, optional
        The class name to create
    base_class : class, optional
        Defaults to `ophyd.ADBase`
    '''

    prefix = cams.prefix
    if not cams.connected_count:
        logger.info('No cams found for prefix %s', prefix)
        return

    core_version = default_core_version

    class_dict = {}

    logger.debug('%s cam-related PVs connected %d', prefix,
                 cams.connected_count)
    for cam_suffix, info in cams.info.items():
        if info:
            try:
                cam_cls = get_cam_from_info(**info)
            except Exception as ex:
                logger.warning('Failed to get cam class', exc_info=ex)
                continue

            if 'core_version' in info:
                core_version = version_tuple_from_string(info['core_version'])

            attr = category_to_identifier(cam_suffix)
            class_dict[attr] = ophyd.Component(cam_cls, cam_suffix)

    if not class_dict:
        logger.info('No cams found for prefix %s', prefix)
        return

    logger.debug('%s core version: %s', prefix, core_version)

    logger.debug('%s plugin-related PVs connected %d', prefix,
                 plugins.connected_count)
    for plugin_suffix, info in sorted(plugins.info.items()):
        if info:
            try:
                plugin_cls = get_plugin_from_info(**info, core_version=core_version)
            except Exception as ex:
                logger.warning('Failed to get plugin class', exc_info=ex)
            else:
                attr = category_to_identifier(plugin_suffix)
                class_dict[attr] = ophyd.Component(plugin_cls, plugin_suffix)

    if class_name is None:
        class_name = category_to_identifier(prefix)

    return ophyd.device.create_device_from_components(
        name=class_name,
        docstring='Auto-generated AreaDetector instance from adviewer',
        base_class=base_class,
        **class_dict
    )


if __name__ == '__main__':
    import sys
    handler = logging.StreamHandler(sys.stdout)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logging.basicConfig()

    prefix = '13SIM1:'
    cams = find_cams_over_channel_access(prefix)
    plugins = find_plugins_over_channel_access(prefix)
    time.sleep(1.5)
    cls = create_detector_class(cams, plugins, default_core_version=(1, 9, 1))
