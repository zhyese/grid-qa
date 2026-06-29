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


class ParseRequest(BaseModel):
    docIds: List[str]


class ParseItem(BaseModel):
    docId: str
    chunkCount: int
    chunkList: List[str] = []


class VectorRequest(BaseModel):
    docId: str


class VectorOut(BaseModel):
    docId: str
    vectorCount: int
    milvusCollection: str
