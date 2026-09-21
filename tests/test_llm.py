from unittest.mock import patch

from agent.llm import get_llm


class TestGetLlm:
    def test_uses_default_model_when_not_overridden(self):
        with patch("agent.llm.ChatOpenAI") as mock_chat, patch("agent.llm.MODEL_NAME", "default-model"):
            get_llm()

        _, kwargs = mock_chat.call_args
        assert kwargs["model"] == "default-model"

    def test_uses_override_model_when_provided(self):
        with patch("agent.llm.ChatOpenAI") as mock_chat:
            get_llm(model_name="override-model")

        _, kwargs = mock_chat.call_args
        assert kwargs["model"] == "override-model"
