"""文档相关 schema。"""
from typing import List, Optional

from pydantic import BaseModel


class UploadOut(BaseModel):
    successList: List[str] = []
    failList: List[str] = []


class DocItem(BaseModel):
    docId: str
    docName: str
    docType: str
    status: str
    chunkCount: int
    uploadUser: str
    createdAt: Optional[str] = None
