# main.py
# Entry point for the DiabetesAgent interactive REPL.
#
# Uses LangGraph's stream_mode="messages" to print tokens as they arrive,
# and surfaces tool calls and results inline so reasoning is visible.
import argparse
import uuid
from typing import Any, cast

from langchain_core.messages import AIMessageChunk, ToolMessage

from agent.graph import build_graph
from agent.metrics import LLMRunMetrics, format_metrics, now, usage_from_chunk
from agent.reporter import generate_and_save_report
from agent.session_store import load_sessions, save_sessions
from config import MODEL_NAME, REPORT_PERIOD_DAYS


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DiabetesAgent interactive CLI")
    parser.add_argument(
        "--model",
        dest="model",
        help="Override model name for this CLI session",
        default=None,
    )
    parser.add_argument(
        "--session",
        dest="session_id",
        help="Load an existing saved chat session by id",
        default=None,
    )
    return parser.parse_args()


def _print_help() -> None:
    print("Commands:")
    print("  /help                 Show this help")
    print("  /report [days]        Generate glucose report")
    print("  /sessions             List saved sessions")
    print("  /load <session_id>    Load a saved session")
    print("  /new                  Start a new chat session")
    print("  /save                 Save current chat immediately")
    print("  exit | quit           Exit")


def _print_sessions(
    sessions: dict[str, list[dict[str, str]]],
    active_session_id: str,
) -> None:
    if not sessions:
        print("No saved sessions yet.\n")
        return

    print("Saved sessions:")
    for session_id in sorted(sessions.keys()):
        marker = "*" if session_id == active_session_id else " "
        message_count = len(sessions.get(session_id, []))
        print(f"{marker} {session_id} ({message_count} messages)")
    print("")


if __name__ == "__main__":
    args = _parse_args()
    active_model = args.model or MODEL_NAME
    graph = build_graph(model_name=args.model)

    sessions = load_sessions()

    if args.session_id:
        if args.session_id not in sessions:
            print(
                f"Warning: session '{args.session_id}' was not found. Starting a new session."
            )
            active_session_id = str(uuid.uuid4())
            history: list[dict[str, str]] = []
        else:
            active_session_id = args.session_id
            history = list(sessions.get(active_session_id, []))
    else:
        active_session_id = str(uuid.uuid4())
        history = []

    print("DiabetesAgent ready. Multi-turn memory is enabled and chats are auto-saved.")
    print("Type '/help' for commands.\n")
    print(f"Model: {active_model}")
    print(f"Session: {active_session_id}\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            sessions[active_session_id] = history
            save_sessions(sessions)
            break

        if not user_input:
            continue

        if user_input == "/help":
            _print_help()
            print("")
            continue

        if user_input == "/sessions":
            sessions = load_sessions()
            sessions[active_session_id] = history
            save_sessions(sessions)
            _print_sessions(sessions, active_session_id)
            continue

        if user_input.startswith("/load"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("Usage: /load <session_id>\n")
                continue

            requested_session = parts[1].strip()
            sessions = load_sessions()
            if requested_session not in sessions:
                print(f"Unknown session id: {requested_session}\n")
                continue

            sessions[active_session_id] = history
            save_sessions(sessions)
            active_session_id = requested_session
            history = list(sessions.get(active_session_id, []))
            print(f"Loaded session: {active_session_id} ({len(history)} messages)\n")
            continue

        if user_input == "/new":
            sessions[active_session_id] = history
            save_sessions(sessions)
            active_session_id = str(uuid.uuid4())
            history = []
            print(f"Started new session: {active_session_id}\n")
            continue

        if user_input == "/save":
            sessions[active_session_id] = history
            save_sessions(sessions)
            print(f"Saved session: {active_session_id}\n")
            continue

        if user_input.startswith("/report"):
            parts = user_input.split()
            days = int(parts[1]) if len(parts) > 1 else REPORT_PERIOD_DAYS
            path = generate_and_save_report(period_days=days)
            print(f"Report saved to {path}\n")
            continue

        print("\nAgent: ", end="", flush=True)
        seen_tools: set[str] = set()
        response_parts: list[str] = []
        run_start = now()
        first_token_ts: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None

        messages_payload = history + [{"role": "user", "content": user_input}]

        for chunk, metadata in cast(Any, graph).stream(
            {"messages": messages_payload},
            stream_mode="messages",
        ):
            meta = metadata if isinstance(metadata, dict) else {}
            node = meta.get("langgraph_node", "")

            if isinstance(chunk, AIMessageChunk):
                # Qwen 3 (and o1-style models) stream thinking tokens in
                # reasoning_content rather than content
                reasoning = chunk.additional_kwargs.get("reasoning_content", "")
                # if reasoning:
                #     if first_token_ts is None:
                #         first_token_ts = now()
                # print(reasoning, end="", flush=True)

                # Stream the final response tokens
                if chunk.content:
                    if first_token_ts is None:
                        first_token_ts = now()
                    response_chunk = (
                        chunk.content
                        if isinstance(chunk.content, str)
                        else str(chunk.content)
                    )
                    response_parts.append(response_chunk)
                    print(response_chunk, end="", flush=True)

                in_t, out_t, total_t = usage_from_chunk(chunk)
                if in_t is not None:
                    input_tokens = in_t
                if out_t is not None:
                    output_tokens = out_t
                if total_t is not None:
                    total_tokens = total_t

                # Show each tool name the first time it appears mid-stream
                for tc in getattr(chunk, "tool_call_chunks", []):
                    name = tc.get("name") if isinstance(tc, dict) else None
                    if name and name not in seen_tools:
                        seen_tools.add(name)
                        print(f"\n\n[Calling: {name}]", flush=True)

            elif node == "tools" and isinstance(chunk, ToolMessage):
                print(f"[Result: {chunk.content}]\n\nAgent: ", end="", flush=True)
                seen_tools.clear()

        response_text = "".join(response_parts).strip()
        history = messages_payload + [{"role": "assistant", "content": response_text}]
        sessions[active_session_id] = history
        save_sessions(sessions)

        latency = now() - run_start
        ttft = None if first_token_ts is None else (first_token_ts - run_start)
        metrics = LLMRunMetrics(
            latency_seconds=latency,
            time_to_first_token_seconds=ttft,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        print(f"\n[Metrics] {format_metrics(metrics)}")
        print("\n")
