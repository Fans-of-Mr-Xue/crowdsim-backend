"""Placeholder for the 瓶颈流理论 modeling algorithm.

The file reserves the stable module path used by the artificial-society
contract. Runtime execution is intentionally unavailable until implemented.
"""

METHOD_NAME = '瓶颈流理论'
MODULE_KIND = 'environment_response'
IMPLEMENTED = False


class AlgorithmNotImplementedError(NotImplementedError):
    pass


def execute(*args, **kwargs):
    raise AlgorithmNotImplementedError(f"{METHOD_NAME} is not implemented")
