class XlogError(Exception):
    """Expected public failure with a stable machine-readable code."""

    def __init__(self, code, message, details=None):
        super(XlogError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
