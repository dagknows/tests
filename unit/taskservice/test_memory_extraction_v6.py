"""Unit tests for V6 memory extraction.

Verifies the conv-mgr → candidate-dict pipeline without spinning up the
taskservice. The LLM call (`do_prompt_completion`) is monkeypatched so the
tests run offline and deterministically.

Run:
    pytest tests/unit/taskservice/test_memory_extraction_v6.py -v
"""

import pytest


# Minimal fixture mimicking conv-mgr's message stream for a completed V6
# session. Mirrors the schema produced by ConvBridge.append_message in
# v6/conv_bridge.py: each message has message_type, msg, metadata, tstp.
def _build_fixture_messages():
    return [
        # User goal
        {
            "message_type": "v6_context_user",
            "msg": "Diagnose why pod elasticsearch-0 is in CrashLoopBackOff",
            "metadata": {},
            "tstp": 1.0,
        },
        # First tool call: succeeds
        {
            "message_type": "v6_tool_call",
            "msg": "Calling kubectl_describe_pod({...})",
            "metadata": {
                "tool_call_id": "tc_1",
                "tool_name": "kubectl_describe_pod",
                "arguments": {"pod": "elasticsearch-0", "namespace": "default"},
            },
            "tstp": 2.0,
        },
        {
            "message_type": "v6_tool_result",
            "msg": "Pod is in CrashLoopBackOff. Last error: OOMKilled.",
            "metadata": {
                "tool_call_id": "tc_1",
                "tool_name": "kubectl_describe_pod",
                "success": True,
                "duration_ms": 312,
                "expert_tags": ["Kubernetes", "tooltask"],
            },
            "tstp": 3.0,
        },
        # Second tool call: fails twice on the same tool — feeds failure_pattern
        {
            "message_type": "v6_tool_call",
            "msg": "Calling es_query({...})",
            "metadata": {
                "tool_call_id": "tc_2",
                "tool_name": "es_query",
                "arguments": {"index": "logs-2026.04", "query": "FATAL OOM"},
            },
            "tstp": 4.0,
        },
        {
            "message_type": "v6_tool_result",
            "msg": "Index not found",
            "metadata": {
                "tool_call_id": "tc_2",
                "tool_name": "es_query",
                "success": False,
                "duration_ms": 45,
                "expert_tags": ["Elasticsearch"],
            },
            "tstp": 5.0,
        },
        {
            "message_type": "v6_tool_call",
            "msg": "Calling es_query({...})",
            "metadata": {
                "tool_call_id": "tc_3",
                "tool_name": "es_query",
                "arguments": {"index": "logs-2026.04.27", "query": "FATAL OOM"},
            },
            "tstp": 6.0,
        },
        {
            "message_type": "v6_tool_result",
            "msg": "Index not found",
            "metadata": {
                "tool_call_id": "tc_3",
                "tool_name": "es_query",
                "success": False,
                "duration_ms": 38,
                "expert_tags": ["Elasticsearch"],
            },
            "tstp": 7.0,
        },
        # Final assistant conclusion
        {
            "message_type": "v6_assistant",
            "msg": "elasticsearch-0 is OOMKilled because heap=1Gi but JVM was set to -Xmx2g. Bump the memory limit or shrink heap.",
            "metadata": {},
            "tstp": 8.0,
        },
        # Terminal session marker
        {
            "message_type": "v6_summary",
            "msg": "Session complete: 2 iterations, 3 tool calls, 1.2s",
            "metadata": {
                "iteration_count": 2,
                "tool_calls": 3,
                "canonical_session_goal": "Diagnose pod CrashLoopBackOff",
                "duration_seconds": 1.2,
                "token_usage": {"prompt": 3000, "completion": 800, "calls": 4},
            },
            "tstp": 9.0,
        },
    ]


@pytest.mark.unit
def test_parse_v6_session_extracts_goal_and_tool_calls():
    """_parse_v6_session should pull the goal, pair tool_call/tool_result by
    tool_call_id, and bucket failures by tool name."""
    from memory_extraction_v6 import _parse_v6_session

    parsed = _parse_v6_session(_build_fixture_messages())

    assert parsed["goal_title"].startswith("Diagnose why pod elasticsearch-0")
    # 3 tool result entries paired correctly
    assert len(parsed["tool_calls"]) == 3
    # 1 successful tool call → eligible for command_recipe
    assert len(parsed["successful_tool_calls"]) == 1
    assert parsed["successful_tool_calls"][0]["title"] == "kubectl_describe_pod"
    # 2 failures on es_query → grouped under that tool
    assert "es_query" in parsed["failed_by_tool"]
    assert len(parsed["failed_by_tool"]["es_query"]) == 2
    # outcomes_chain has tool steps + a synthetic final-conclusion step
    assert len(parsed["outcomes_chain"]) >= 3
    assert parsed["outcomes_chain"][-1]["title"] == "Final assistant conclusion"
    # Internal tags filtered out (tooltask is in INTERNAL_TAGS)
    assert "tooltask" not in parsed["expert_tags"]
    assert "Kubernetes" in parsed["expert_tags"]
    assert "Elasticsearch" in parsed["expert_tags"]
    # Final summary contains the assistant's conclusion text
    assert "OOMKilled" in parsed["final_summary"]


@pytest.mark.unit
def test_extract_memory_candidates_v6_produces_all_types(monkeypatch):
    """End-to-end extraction with a stub LLM. Should produce qa, cause_effect,
    command_recipe, failure_pattern (no reasoning_template by design)."""
    import memory_extraction_v6 as mev6

    # Stub do_prompt_completion: returns shape-appropriate JSON per call.
    # The function name is forwarded as `source_function_str` so we can
    # discriminate without parsing the prompt text.
    def fake_completion(messages, as_json=False, selected_llm="gpt-4o",
                       source_function_str="", temperature=0.0):
        if source_function_str == "extract_qa_memory_v6":
            return {
                "title": "OOMKilled pods diagnosis",
                "generalized_content": "Q: ...\nA: Check JVM -Xmx vs k8s memory limit.",
                "specific_content": "Q: ...\nA: heap=1Gi but Xmx=2g.",
                "relevant_experts": ["Kubernetes"],
            }
        if source_function_str == "extract_cause_effect_v6":
            return {
                "title": "CrashLoopBackOff → OOM → Heap mismatch",
                "content": "Symptom: ...\n→ Root cause: heap > memory limit.",
                "relevant_experts": ["Kubernetes"],
            }
        if source_function_str == "extract_command_recipes_v6":
            return [{
                "title": "kubectl describe pod for crashloop diag",
                "content": "kubectl describe pod <name> — check Last State.Reason.",
                "relevant_experts": ["Kubernetes"],
            }]
        if source_function_str == "extract_failure_pattern_v6":
            return {
                "title": "Don't query ES indices without the date suffix",
                "content": "ES rolling indices need the YYYY.MM.DD suffix.",
                "has_solution": True,
                "relevant_experts": ["Elasticsearch"],
            }
        return None

    monkeypatch.setattr(mev6, "do_prompt_completion", fake_completion)

    candidates = mev6.extract_memory_candidates_v6(
        conv_messages=_build_fixture_messages(),
        conv_id="conv_test_abc",
        user_info={"org": "acme", "uid": "u1"},
        selected_llm="gpt-4o",
    )

    types = [c["type"] for c in candidates]
    assert "qa" in types
    assert "cause_effect" in types
    assert "command_recipe" in types
    assert "failure_pattern" in types
    # reasoning_template intentionally skipped for V6
    assert "reasoning_template" not in types

    # Provenance correctly stamped
    for c in candidates:
        assert c["conv_id"] == "conv_test_abc"
        assert c["org_id"] == "acme"
        assert c["created_by"] == "u1"
        assert c["source_engine"] == "v6"
        assert c["session_goal"].startswith("Diagnose")
        # No fabricated runbook_task_id
        assert c["runbook_task_id"] == ""


@pytest.mark.unit
def test_extract_memory_candidates_v6_empty_session():
    """A conv with no user prompt yields no candidates."""
    from memory_extraction_v6 import extract_memory_candidates_v6
    candidates = extract_memory_candidates_v6(
        conv_messages=[],
        conv_id="conv_empty",
        user_info={"org": "acme", "uid": "u1"},
    )
    assert candidates == []


@pytest.mark.unit
def test_failure_pattern_skipped_with_only_one_failure(monkeypatch):
    """A single failure on a tool isn't a 'pattern' — needs >=2 to extract."""
    import memory_extraction_v6 as mev6

    msgs = [
        {"message_type": "v6_context_user", "msg": "Goal", "metadata": {}, "tstp": 1.0},
        {"message_type": "v6_tool_call", "msg": "x", "metadata": {
            "tool_call_id": "x1", "tool_name": "flaky_tool", "arguments": {}}, "tstp": 2.0},
        {"message_type": "v6_tool_result", "msg": "boom", "metadata": {
            "tool_call_id": "x1", "tool_name": "flaky_tool", "success": False}, "tstp": 3.0},
        {"message_type": "v6_assistant", "msg": "I gave up.", "metadata": {}, "tstp": 4.0},
    ]

    monkeypatch.setattr(mev6, "do_prompt_completion",
                        lambda *a, **kw: {"title": "x", "content": "y",
                                          "generalized_content": "g", "specific_content": "s",
                                          "relevant_experts": []})

    cands = mev6.extract_memory_candidates_v6(
        conv_messages=msgs, conv_id="c", user_info={"org": "o", "uid": "u"},
    )
    types = [c["type"] for c in cands]
    assert "failure_pattern" not in types
