"""Placeholder for the 执行接口 environment layer algorithm."""

METHOD_NAME = '执行接口'
MODULE_KIND = "environment_layer"
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
