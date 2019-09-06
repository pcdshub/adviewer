import time

import pytest

from .. import discovery


@pytest.fixture(scope='function')
def detector_prefix():
    return '13SIM1:'


def test_create_class(detector_prefix):
    cams = discovery.find_cams_over_channel_access(detector_prefix)
    plugins = discovery.find_plugins_over_channel_access(detector_prefix)
    time.sleep(1.5)
    cls = discovery.create_detector_class(cams, plugins,
                                          default_core_version=(1, 9, 1))
    print(cls)
