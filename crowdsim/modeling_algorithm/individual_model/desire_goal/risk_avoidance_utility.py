"""Placeholder for the 风险规避效用 modeling algorithm.

The file reserves the stable module path used by the artificial-society
contract. Runtime execution is intentionally unavailable until implemented.
"""

METHOD_NAME = '风险规避效用'
MODULE_KIND = 'desire'
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
