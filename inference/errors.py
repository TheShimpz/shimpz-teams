"""The Brain runtime client's error type, shared with its request abort handle."""


class BrainRuntimeError(RuntimeError):
    """The private runtime was unavailable or violated its closed response contract."""
