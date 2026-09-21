# agent/graph.py
# Wires the LLM and tools into a ReAct loop using LangGraph.
#
# Flow:
#   START → [agent node] ──has tool_calls?──> [ToolNode] ──> [agent node] (loop)
#                        ──no tool_calls?──> END
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from agent.llm import get_llm
from agent.tools import TOOLS

SYSTEM_PROMPT = (
    "You are an assistant that helps manage diabetes. You have access to the user's CGM sensor data. "
    "For historical and time-range CGM analysis, prefer SQLite-backed tools "
    "(get_recent_cgm_readings, get_cgm_readings, get_cgm_summary_from_db, "
    "get_cgm_spikes_from_db, get_nearest_cgm_reading). "
    "You can help users calculate insulin dosing for meals using their personal insulin-to-carb ratios. "
    "When a user mentions a meal with carbs, proactively use calculate_insulin_dosage. "
    "Always remind users this is informational and not medical advice."
)


def _agent_node(state: MessagesState, llm_with_tools):
    messages = state["messages"]
    if not any(getattr(m, "type", None) == "system" for m in messages):
        from langchain_core.messages import SystemMessage

        messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
    return {"messages": [llm_with_tools.invoke(messages)]}


def _should_continue(state: MessagesState):
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


def build_graph(model_name: str | None = None):
    llm_with_tools = get_llm(model_name=model_name).bind_tools(TOOLS)
    tool_node = ToolNode(TOOLS)

    def agent_node(state: MessagesState):
        return _agent_node(state, llm_with_tools)

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", _should_continue, ["tools", END])
    builder.add_edge("tools", "agent")
    return builder.compile()


graph = build_graph()
