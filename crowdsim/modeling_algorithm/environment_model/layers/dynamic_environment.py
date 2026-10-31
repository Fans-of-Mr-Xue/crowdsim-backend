"""Placeholder for the 动态环境 environment layer algorithm."""

METHOD_NAME = '动态环境'
MODULE_KIND = "environment_layer"
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
