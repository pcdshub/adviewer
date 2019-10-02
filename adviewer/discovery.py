import distutils
import logging
import os
import re
import threading
import time

from functools import partial

import epics

import ophyd
import ophyd.areadetector.cam
import ophyd.areadetector.plugins
from ophyd import areadetector

logger = logging.getLogger(__name__)


_RE_NONALPHA = re.compile('[^0-9a-zA-Z_]+')
MIN_VERSION = '1.9.1'


# semi-private dict in ophyd
plugin_type_to_class = dict(areadetector.plugins._plugin_class)

# Some plugins do not report correctly. This may need to be user-customizable
# at some point:
plugin_type_to_class['NDPluginFile'] = areadetector.plugins.NexusPlugin

manufacturer_model_to_cam_class = {
    ('Simulated detector', 'Basic simulator'): areadetector.cam.SimDetectorCam,
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
            logger.debug('Disconnected from %s (%s, %s)=%s', pv, category, key)
            return

        value = pv.get()
        try:
            status.info[category][key] = value
        except KeyError:
            logger.debug('Callback with user-deleted key (pv=%s, %s %s)', pv,
                         category, key)
            return

        logger.debug('Connected to %s (%s, %s)=%s', pv, category, key, value)

        with status._lock:
            status.connected_count += 1

        if callback is not None:
            callback(pv=pv, category=category, key=key, value=value,
                     status=status)

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
        'ADCoreVersion_RBV': 'adcore_version',
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
    if ver_string is None:
        raise ValueError('Version string cannot be None')
    return tuple(distutils.version.LooseVersion(ver_string).version)


def get_cam_from_info(manufacturer, model, *, adcore_version=None,
                      driver_version=None,
                      default_class=ophyd.areadetector.cam.AreaDetectorCam):
    '''
    Get a camera class given at least its manufacturer and model
    '''
    cam_class = manufacturer_model_to_cam_class.get(
        (manufacturer, model), default_class)

    if adcore_version is None:
        adcore_version = MIN_VERSION

    adcore_version = version_tuple_from_string(adcore_version)

    if driver_version is not None:
        driver_version = version_tuple_from_string(driver_version)
    # TODO mix in new base components using adcore_version
    # TODO then cam is versioned on driver_version
    return cam_class


def get_plugin_from_info(plugin_type, *, adcore_version):
    '''
    Get a plugin class given its type and ADCore version
    '''
    if ' ' in plugin_type:
        # HDF5 includes version number, remove it
        plugin_type, _ = plugin_type.split(' ', 1)

    plugin_class = plugin_type_to_class[plugin_type]
    return ophyd.select_version(plugin_class, adcore_version)


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
        base_class=ophyd.DetectorBase):
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

    adcore_version = default_core_version

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

            if 'adcore_version' in info:
                adcore_version = version_tuple_from_string(
                    info['adcore_version'])

            attr = category_to_identifier(cam_suffix)
            class_dict[attr] = ophyd.Component(cam_cls, cam_suffix)

    if not class_dict:
        logger.info('No cams found for prefix %s', prefix)
        return

    logger.debug('%s core version: %s', prefix, adcore_version)

    logger.debug('%s plugin-related PVs connected %d', prefix,
                 plugins.connected_count)
    for plugin_suffix, info in sorted(plugins.info.items()):
        if info:
            try:
                plugin_cls = get_plugin_from_info(
                    **info, adcore_version=adcore_version)
            except Exception as ex:
                logger.warning('Failed to get plugin class', exc_info=ex)
            else:
                attr = category_to_identifier(plugin_suffix)
                class_dict[attr] = ophyd.Component(plugin_cls, plugin_suffix)

    if class_name is None:
        class_name = class_name_from_prefix(prefix)

    return ophyd.device.create_device_from_components(
        name=class_name,
        docstring='Auto-generated AreaDetector instance from adviewer',
        base_class=base_class,
        **class_dict
    )


def class_name_from_prefix(prefix):
    '''
    Create a Python identifier for the detector to be used as the class name
    '''
    class_name = category_to_identifier(prefix).capitalize()
    if class_name.startswith('_'):
        return 'Detector' + class_name.lstrip('_').capitalize()
    return class_name


def cams_and_plugins_from_pvlist(pvs, cam_callback, plugin_callback, *,
                                 min_version=None):
    '''
    Given a list of PVs from an AreaDetector IOC, search for cameras and
    plugins
    '''

    if min_version is None:
        min_version = MIN_VERSION

    def matching_prefixes(suffix):
        for pv in sorted(pvs):
            if pv.endswith(suffix):
                yield ':'.join(pv.split(':')[:-1]) + ':'

    version_pv_found = False
    cam_prefixes = list(matching_prefixes(':Manufacturer_RBV'))
    plugin_prefixes = list(matching_prefixes(':PluginType_RBV'))
    prefix = os.path.commonprefix(cam_prefixes + plugin_prefixes)

    cam_query = {}

    for idx, cam_prefix in enumerate(cam_prefixes, 1):
        cam_category = cam_prefix[len(prefix):]
        cam_query.update(
            {f'{cam_prefix}{suffix}': (cam_category, key)
             for suffix, key in (('Manufacturer_RBV', 'manufacturer'),
                                 ('Model_RBV', 'model'),
                                 ('PortName_RBV', 'port')
                                 )
             }
        )
        core_version_pv = f'{cam_prefix}ADCoreVersion_RBV'
        if core_version_pv in pvs:
            cam_query[core_version_pv] = (cam_category, 'adcore_version')
            version_pv_found = True

        version_pv = f'{cam_prefix}DriverVersion_RBV'
        if version_pv in pvs:
            cam_query[version_pv] = (cam_category, 'driver_version')

    plugin_query = {
        f'{plugin_prefix}PluginType_RBV': (plugin_prefix[len(prefix):],
                                           'plugin_type')
        for plugin_prefix in plugin_prefixes
    }

    cams = connect_to_many(prefix, cam_query, cam_callback)
    if not version_pv_found:
        # No ADCoreVersion found, assume the worst - R1-9-1
        for key, info_dict in cams.info.items():
            info_dict['adcore_version'] = min_version
            cam_callback(pv=None, category=key, key='adcore_version',
                         value=min_version, status=cams)

    plugins = connect_to_many(prefix, plugin_query, plugin_callback)
    return cams, plugins
