"""Stable, sanitized application failures shared by HTTP, streams and persisted runs."""
class ServiceError(Exception):
    def __init__(self, code, message, status=503, retryable=False, retry_after=None, details=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.details = details
        self.status, self.retryable, self.retry_after = status, retryable, retry_after

    def public(self):
        value = {'code': self.code, 'message': self.message, 'retryable': self.retryable,
                'retry_after': self.retry_after}
        if self.details:value['details']=self.details
        return value


def public_error(error):
    if isinstance(error, ServiceError):
        return error.public()
    return ServiceError('INTERNAL_ERROR', '任务服务异常，请凭请求编号联系管理员。', 500).public()
