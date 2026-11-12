class MyQError(Exception):
    pass


class MyQAuthenticationError(MyQError):
    pass


class MyQInvalidCredentialsError(MyQAuthenticationError):
    pass


class MyQInvalidMfaError(MyQAuthenticationError):
    pass


class MyQCloudflareChallengeError(MyQError):
    pass


class MyQBrowserSessionExpiredError(MyQError):
    pass


class MyQInvalidCallbackError(MyQError):
    pass


class MyQApiError(MyQError):
    pass


class MyQRateLimitError(MyQApiError):
    """A service has asked the client to pause further requests."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("MyQ service requests are temporarily paused")
        self.retry_after = retry_after


class MyQUnsupportedAuthPageError(MyQApiError):
    pass
