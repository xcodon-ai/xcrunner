"""Exception hierarchy. Every error raised by xcodon-runtime derives from XcodonError."""


class XcodonError(Exception):
    """Base class. The message says what failed and, when known, the fix."""


class ImageNotFound(XcodonError):
    """The image is not in the local store."""


class PullError(XcodonError):
    """Fetching an image failed: HTTP error, auth, digest mismatch, no platform match."""


class UnsupportedLayer(XcodonError):
    """A layer uses a media type or compression we cannot read."""


class EngineUnavailable(XcodonError):
    """No engine can run on this host, or the requested engine cannot."""


class ContainerNotFound(XcodonError):
    """No container matches the id, prefix, or name."""


class ContainerNotRunning(XcodonError):
    """The container has no live keeper."""


class ExecError(XcodonError):
    """A command could not be started inside the container."""
