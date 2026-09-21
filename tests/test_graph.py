# tests/test_graph.py
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END

from agent.graph import _agent_node, _should_continue
from agent.graph import build_graph


class TestShouldContinue:
    def test_routes_to_tools_when_tool_calls_present(self):
        msg = MagicMock()
        msg.tool_calls = [{"name": "get_latest_glucose", "args": {}, "id": "call_1"}]
        assert _should_continue({"messages": [msg]}) == "tools"

    def test_routes_to_end_when_tool_calls_empty(self):
        msg = MagicMock()
        msg.tool_calls = []
        assert _should_continue({"messages": [msg]}) == END

    def test_routes_to_end_when_no_tool_calls_attribute(self):
        msg = MagicMock(spec=[])  # no attributes at all
        assert _should_continue({"messages": [msg]}) == END


class TestAgentNode:
    def test_injects_system_message_when_absent(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock()
        _agent_node({"messages": [HumanMessage(content="hello")]}, mock_llm)

        call_messages = mock_llm.invoke.call_args[0][0]
        assert any(isinstance(m, SystemMessage) for m in call_messages)

    def test_does_not_duplicate_existing_system_message(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock()
        _agent_node({
            "messages": [
                SystemMessage(content="existing system"),
                HumanMessage(content="hello"),
            ]
        }, mock_llm)

        call_messages = mock_llm.invoke.call_args[0][0]
        system_msgs = [m for m in call_messages if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 1

    def test_returns_llm_response_in_messages(self):
        mock_response = MagicMock()
        mock_llm = MagicMock()
        with patch("agent.graph.get_llm") as mock_get_llm:
            mock_get_llm.return_value.bind_tools.return_value = mock_llm
            mock_llm.invoke.return_value = mock_response
            result = _agent_node({"messages": [HumanMessage(content="hello")]}, mock_llm)

        assert result == {"messages": [mock_response]}


class TestBuildGraph:
    def test_passes_model_name_to_get_llm(self):
        with patch("agent.graph.get_llm") as mock_get_llm:
            mock_llm = MagicMock()
            mock_get_llm.return_value = mock_llm
            mock_llm.bind_tools.return_value = MagicMock()

            build_graph(model_name="my-model")

        mock_get_llm.assert_called_once_with(model_name="my-model")
