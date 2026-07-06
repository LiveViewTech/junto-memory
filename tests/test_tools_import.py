"""Tests that all tools register correctly."""


def test_all_tools_register():
    """All 64 tools are registered with the MCP server.

    (57 + 6 skill-registry tools + memory_ack_directive.)
    """
    from shared_memory.app import create_app

    mcp = create_app()
    tools = mcp._tool_manager._tools

    assert len(tools) == 64, f"Expected 64 tools, got {len(tools)}: {sorted(tools.keys())}"


def test_expected_tools_present():
    """Key tools are present by name."""
    from shared_memory.app import create_app

    mcp = create_app()
    tools = set(mcp._tool_manager._tools.keys())

    expected = {
        "memory_start_session", "memory_end_session",
        "memory_query", "memory_store", "memory_record_learning",
        "memory_lock_files", "memory_unlock_files", "memory_get_locks",
        "memory_send_message", "memory_get_messages",
        # Stage 2 / CLAIMING (design:unified-messaging-v0)
        "memory_claim_message",
        "memory_add_backlog_item", "memory_list_backlog",
        "memory_register_function", "memory_find_function",
        "memory_project", "memory_checklist", "memory_db",
        "memory_define_spec", "memory_list_agents", "memory_guidelines",
        # Skill registry (design:skill-registry-v0, authoring layer)
        "memory_register_skill", "memory_confirm_skill", "memory_get_skill",
        "memory_list_skills", "memory_pin_skill", "memory_export_skills",
        "memory_ack_directive",
        "memory_admin", "memory_standup",
        # Phase C1
        "memory_set_autopilot", "memory_pause_autopilot",
        "memory_autopilot_status", "memory_autopilot_digest",
        # Phase C2 (budget enforcement)
        "memory_autopilot_check_budget",
        # Phase C2.1 (read-only observability counterpart)
        "memory_autopilot_count",
        # Phase 0 local-first band-aid (no-session diagnostic)
        "memory_health",
        # Phase 2 replication endpoints (sync_push is a stub until the
        # next session ships materialization machinery)
        "memory_sync_pull", "memory_sync_push",
    }

    missing = expected - tools
    assert not missing, f"Missing tools: {missing}"
