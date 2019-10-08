===============================
adviewer
===============================

.. image:: https://img.shields.io/travis/pcdshub/adviewer.svg
        :target: https://travis-ci.org/pcdshub/adviewer

.. image:: https://img.shields.io/pypi/v/adviewer.svg
        :target: https://pypi.python.org/pypi/adviewer


AreaDetector configurator (and - *one day* - viewer)

adviewer - despite the `viewer` in its name - is primarily a configuration
tool for already-running areaDetector IOCs. It can help you determine how
any given plugin pipeline has been configured, displaying an interactively-
reconfigurable node graph and tree-based view. It may also help you discover
what plugins are installed for an unfamiliar installation.

Eventually, there will be a full-featured viewer based on PyDM integrated
as well. For now, a simple PyDM user interface is spawned in a separate 
process when using the image plugin right-click context menu in the graph.

Requirements
------------

* Python 3.6+
* `qtpy <https://github.com/spyder-ide/qtpy>`_ + PyQt5
* `qtpynodeeditor <https://github.com/klauer/qtpynodeeditor>`_, a pure Python port of `nodeeditor <https://github.com/paceholder/nodeeditor>`_
* `networkx <https://networkx.github.io>`_
* `ophyd <https://blueskyproject.io/ophyd/>`_

Optionally:

* `PyDM <https://github.com/slaclab/pydm>`_, to spawn an image viewer

Running adviewer
----------------

To install::

        $ pip install git+https://github.com/pcdshub/adviewer
        
To run::

        $ adviewer --help
        $ adviewer 13SIM1:
        $ adviewer --pvlist filename.pvlist

Running the Tests
-----------------
::

  $ python run_tests.py
