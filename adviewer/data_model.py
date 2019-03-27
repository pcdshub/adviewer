import inspect
import qtpynodeeditor
import ophyd
from ophyd.areadetector import plugins


models = {}


def register_model(ophyd_class):
    def wrapper(cls):
        cls.ophyd_class = cls
        models[ophyd_class] = cls
        return cls

    return wrapper


def get_node_data_model(cls):
    if not inspect.isclass(cls):
        cls = cls.__class__

    try:
        return models[cls]
    except KeyError:
        ...

    candidates = {}

    for ophyd_cls, model in models.items():
        if issubclass(cls, ophyd_cls):
            shared_mro = set(ophyd_cls.mro()).intersection(set(cls.mro()))
            candidates[len(shared_mro)] = model

    if candidates:
        return candidates[max(candidates)]

    raise ValueError(f'Class has no corresponding data model: {cls.__name__}')


def summarize_node(node, *, port_information=None):
    if port_information is None:
        port_information = {}

    inputs = []
    outputs = []
    for conn in node.state.all_connections:
        dest, _ = conn.nodes
        if dest is node:
            inputs.append(dest.data.port_name)
        else:
            outputs.append(dest.data.port_name)

    connectivity = {}
    connectivity['input'] = inputs[0] if inputs else 'N/A'

    # But multiple outputs
    connectivity.update(**{f'output{idx}': output for idx, output
                           in enumerate(outputs, 1)})

    return {'version': port_information.get(node.data.port_name, {}),
            'connectivity': connectivity
            }


class PortData(qtpynodeeditor.NodeData):
    data_type = qtpynodeeditor.NodeDataType(id='Port', name='Port')

    def __init__(self, port):
        self.port = port


@register_model(ophyd.areadetector.CamBase)
class NodeCam(qtpynodeeditor.NodeDataModel):
    port_name = None
    num_ports = {'input': 0,
                 'output': 1,
                 }
    port_caption = {'output': {0: 'Out'}}

    def data_type(self, port_type, port_index):
        return PortData.data_type

    def port_caption_visible(self, port_type, port_index):
        return True


@register_model(plugins.PluginBase)
class NodePlugin(qtpynodeeditor.NodeDataModel):
    port_name = None
    num_ports = {'input': 1,
                 'output': 1,
                 }
    port_caption = {'input': {0: 'In'},
                    'output': {0: 'Out'},
                    }

    def data_type(self, port_type, port_index):
        return PortData.data_type

    def port_caption_visible(self, port_type, port_index):
        return True
