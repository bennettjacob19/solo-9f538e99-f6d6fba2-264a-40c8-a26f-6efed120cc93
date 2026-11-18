"""Application errors that map cleanly onto HTTP responses."""

from __future__ import annotations


class DirectoryError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = list(details or [])

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class InputError(DirectoryError):
    status_code = 400
    code = "invalid_input"


class NotFoundError(DirectoryError):
    status_code = 404
    code = "not_found"


class ConflictError(DirectoryError):
    status_code = 409
    code = "conflict"


class RestoreInProgress(ConflictError):
    status_code = 409
    code = "restore_in_progress"

    def __init__(self, message: str = "备份恢复进行中，新的修改已被拒绝，请稍后重试") -> None:
        super().__init__(message)


class BackupNameTaken(ConflictError):
    status_code = 409
    code = "backup_name_taken"


class BackupValidationError(DirectoryError):
    status_code = 422
    code = "invalid_backup"

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message, details=details)


class DatabaseBusy(DirectoryError):
    status_code = 503
    code = "database_busy"

    def __init__(self) -> None:
        super().__init__("数据库繁忙，请稍后重试")


class DatabaseError(DirectoryError):
    status_code = 500
    code = "database_error"

    def __init__(self, message: str = "数据库操作失败") -> None:
        super().__init__(message)
