"""智能问答历史软删：纯函数测试（模型属性 + schema）。DB 耦合逻辑靠端到端验证。"""


def test_conversation_model_has_soft_delete_column():
    """Conversation / Message 两模型都应有 is_deleted 列（mapped_column 创建类属性，import 即可断言）。"""
    from app.models.conversation import Conversation, Message

    assert hasattr(Conversation, "is_deleted")
    assert hasattr(Message, "is_deleted")


def test_batch_delete_request_accepts_ids():
    from app.schemas.qa import BatchDeleteRequest

    req = BatchDeleteRequest(ids=["conv1", "conv2"])
    assert req.ids == ["conv1", "conv2"]


def test_batch_delete_request_requires_ids():
    import pytest
    from pydantic import ValidationError

    from app.schemas.qa import BatchDeleteRequest

    with pytest.raises(ValidationError):
        BatchDeleteRequest()  # ids 缺省必须报错
