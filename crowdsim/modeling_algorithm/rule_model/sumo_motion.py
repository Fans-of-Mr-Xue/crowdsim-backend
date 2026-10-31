"""Placeholder for the SUMO 运动 modeling algorithm.

The file reserves the stable module path used by the artificial-society
contract. Runtime execution is intentionally unavailable until implemented.
"""

METHOD_NAME = 'SUMO 运动'
MODULE_KIND = 'rule'
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
