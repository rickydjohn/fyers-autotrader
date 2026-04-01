from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime
import uuid


class ApiResponse(BaseModel):
    success: bool = True
    data: Any = None
    meta: dict = {}
    error: Optional[str] = None

    @classmethod
    def ok(cls, data: Any) -> "ApiResponse":
        return cls(
            success=True,
            data=data,
            meta={"timestamp": datetime.utcnow().isoformat(), "request_id": str(uuid.uuid4())},
        )

    @classmethod
    def err(cls, message: str) -> "ApiResponse":
        return cls(
            success=False,
            error=message,
            meta={"timestamp": datetime.utcnow().isoformat()},
        )
