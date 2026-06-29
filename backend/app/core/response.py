"""统一响应封装：所有接口返回 {code, message, data}。"""
from typing import Any, Optional

from pydantic import BaseModel


class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: Optional[Any] = None


def success(data: Any = None, message: str = "success", code: int = 200) -> ApiResponse:
    return ApiResponse(code=code, message=message, data=data)


def error(message: str = "error", code: int = 500, data: Any = None) -> ApiResponse:
    return ApiResponse(code=code, message=message, data=data)


class BizError(Exception):
    """业务异常，由全局异常处理器转成统一响应。"""

    def __init__(self, message: str = "业务异常", code: int = 400, data: Any = None):
        self.message = message
        self.code = code
        self.data = data
        super().__init__(message)
