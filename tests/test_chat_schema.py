"""Tests for chat_schema: Event, Instruction, Header, build methods."""
from __future__ import annotations

from app.schema.chat_schema import (
    Dialog,
    Event,
    Header,
    Instruction,
    Internal,
    Nlp,
    Template,
)


class TestHeader:
    def test_fields(self):
        h = Header(type="event", namespace="Nlp", name="Request",
                   timestamp=1000, request_id="r1", session_id="s1")
        assert h.type == "event"
        assert h.namespace == "Nlp"
        assert h.request_id == "r1"


class TestEventBuild:
    def test_build_event(self):
        event = Event.build_event(Nlp.Request(query="hello"), request_id="r1", session_id="s1")
        assert event.header.namespace == "Nlp"
        assert event.header.name == "Request"
        assert event.payload["query"] == "hello"

    def test_build_event_auto_ids(self):
        event = Event.build_event(Nlp.Request(query="test"))
        assert event.header.request_id is not None
        assert event.header.session_id is not None


class TestInstructionBuild:
    def test_toast_stream(self):
        inst = Instruction.build_instruction(
            Template.ToastStream(stream="hello"), "r1", "s1"
        )
        assert inst.header.namespace == "Template"
        assert inst.header.name == "ToastStream"
        assert inst.payload["stream"] == "hello"

    def test_call_tool(self):
        inst = Instruction.build_instruction(
            Template.CallTool(id="t1", service_name="local", tool_name="test"),
            "r1", "s1"
        )
        assert inst.payload["tool_name"] == "test"

    def test_call_tool_result(self):
        inst = Instruction.build_instruction(
            Template.CallToolResult(id="t1", success=True, tool_name="test",
                                    tool_response={"data": "ok"}),
            "r1", "s1"
        )
        assert inst.payload["success"] is True

    def test_dialog_finish(self):
        inst = Instruction.build_instruction(
            Dialog.Finish(success=True), "r1", "s1"
        )
        assert inst.header.namespace == "Dialog"
        assert inst.payload["success"] is True

    def test_internal_dispatcher(self):
        inst = Instruction.build_instruction(
            Internal.Dispatcher(current_query="hi", need_storage_history=True),
            "r1", "s1"
        )
        assert inst.header.namespace == "Internal"
        assert inst.payload["current_query"] == "hi"
