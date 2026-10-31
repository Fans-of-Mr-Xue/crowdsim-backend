"""Placeholder for the 演化博弈 modeling algorithm.

The file reserves the stable module path used by the artificial-society
contract. Runtime execution is intentionally unavailable until implemented.
"""

METHOD_NAME = '演化博弈'
MODULE_KIND = 'game'
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
