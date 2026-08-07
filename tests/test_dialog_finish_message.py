"""Dialog.Finish message 字段测试。"""

from app.schema.chat_schema import Dialog


def test_finish_has_message_field():
    """Finish 可带 message（默认空）。"""
    finish = Dialog.Finish(success=True)
    assert finish.message == ""

    finish_with_msg = Dialog.Finish(success=True, message="已转交处理")
    assert finish_with_msg.message == "已转交处理"


def test_finish_serializes_message():
    """Finish 序列化包含 message。"""
    finish = Dialog.Finish(success=False, message="被打断")
    dumped = finish.model_dump()
    assert dumped["message"] == "被打断"
    assert dumped["success"] is False
