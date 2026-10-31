"""Placeholder for the 空间拓扑 environment layer algorithm."""

METHOD_NAME = '空间拓扑'
MODULE_KIND = "environment_layer"
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
