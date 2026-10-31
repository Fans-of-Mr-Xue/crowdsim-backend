"""Placeholder for the 规则知识库 modeling algorithm.

The file reserves the stable module path used by the artificial-society
contract. Runtime execution is intentionally unavailable until implemented.
"""

METHOD_NAME = '规则知识库'
MODULE_KIND = 'memory'
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
