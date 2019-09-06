from ._version import get_versions
from .graph import *  # noqa
from .utils import *  # noqa

__version__ = get_versions()['version']
del get_versions
