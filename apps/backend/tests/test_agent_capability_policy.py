from services.agent_capability_policy import (
    FULL_ACCESS,
    READ_ONLY,
    SCOPED_WRITE,
    WORKSPACE_WRITE,
    compile_capability_mode,
    resolve_capability_policy,
)


def test_compile_capability_mode_uses_read_scoped_and_workspace_boundaries() -> None:
    assert compile_capability_mode(can_write=False) == READ_ONLY
    assert (
        compile_capability_mode(
            can_write=True,
            allowed_write_roots=["chapters/", "chapters/"],
        )
        == SCOPED_WRITE
    )
    assert compile_capability_mode(can_write=True) == WORKSPACE_WRITE
    assert (
        compile_capability_mode(
            can_write=True,
            knowledge_write_mode="candidate_extraction",
        )
        == READ_ONLY
    )


def test_resolve_capability_policy_fails_closed_on_conflicting_read_only_intent() -> None:
    policy = resolve_capability_policy(
        {
            "intentFrame": {"canWrite": False},
            "executionPolicy": {
                "capabilityMode": FULL_ACCESS,
                "directFileWrites": True,
            },
        }
    )
    assert policy.mode == READ_ONLY
    assert policy.writes_allowed is False
    assert policy.source == "intent_read_only"


def test_resolve_scoped_write_normalizes_roots_and_core_boundary() -> None:
    policy = resolve_capability_policy(
        {
            "intentFrame": {"canWrite": True},
            "executionPolicy": {
                "capabilityMode": SCOPED_WRITE,
                "directFileWrites": True,
                "allowedWriteRoots": ["chapters\\", "chapters/"],
            },
        }
    )
    assert policy.mode == SCOPED_WRITE
    assert policy.writes_allowed is True
    assert policy.core_writes_allowed is True
    assert policy.allowed_write_roots == ("chapters/",)


def test_invalid_scoped_write_without_roots_is_read_only() -> None:
    policy = resolve_capability_policy(
        {
            "intentFrame": {"canWrite": True},
            "executionPolicy": {"capabilityMode": SCOPED_WRITE},
        }
    )
    assert policy.mode == READ_ONLY
    assert policy.source == "invalid_scoped_write_without_roots"


def test_legacy_direct_file_write_false_is_read_only() -> None:
    policy = resolve_capability_policy(
        {
            "intentFrame": {"canWrite": True},
            "executionPolicy": {
                "directFileWrites": False,
                "allowedWriteRoots": [],
            },
        }
    )
    assert policy.mode == READ_ONLY
    assert policy.source == "legacy_contract_compiler"


def test_explicit_binding_keeps_only_guarded_scoped_write() -> None:
    policy = resolve_capability_policy(
        {
            "intentFrame": {"canWrite": True},
            "knowledgeWritePolicy": {"mode": "explicit_binding"},
            "executionPolicy": {
                "capabilityMode": SCOPED_WRITE,
                "directFileWrites": False,
                "allowedWriteRoots": [
                    ".storydex/.agent/runtime/knowledge-write-plans/"
                ],
            },
        }
    )
    assert policy.mode == SCOPED_WRITE
    assert policy.writes_allowed is True
    assert policy.core_writes_allowed is False


def test_plan_mode_is_an_independent_read_only_overlay() -> None:
    contract = {
        "intentFrame": {"canWrite": True},
        "executionPolicy": {
            "capabilityMode": WORKSPACE_WRITE,
            "directFileWrites": True,
        },
    }
    assert resolve_capability_policy(contract).mode == WORKSPACE_WRITE
    assert resolve_capability_policy(contract, plan_mode=True).mode == READ_ONLY
