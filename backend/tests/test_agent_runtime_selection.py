from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.schema import AgentProfileCreateRequest, AgentProfileUpdateRequest
from app.api.agents import create_agent, update_agent
from app.db import database
from app.db.models import AgentProfile, ChatSession, Tenant, User
from app.runtimes import AgentRuntimeKind, parse_runtime_kind, resolve_runtime_kind


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_users(db: Session) -> tuple[User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        display_name="Owner",
        password_hash="x",
    )
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="Admin",
        role="admin",
        password_hash="x",
    )
    db.add(owner)
    db.add(admin)
    db.commit()
    return owner, admin


def _add_agent(db: Session, agent_id: str, runtime: str, tenant_id: str = "tenant_demo") -> AgentProfile:
    agent = AgentProfile(
        id=agent_id,
        tenant_id=tenant_id,
        name=f"员工-{agent_id}",
        runtime=runtime,
    )
    db.add(agent)
    db.commit()
    return agent


# ---------------------------------------------------------------------------
# registry.resolve_runtime_kind
# ---------------------------------------------------------------------------


def test_parse_runtime_kind_falls_back_for_unknown_values() -> None:
    assert parse_runtime_kind(None) == AgentRuntimeKind.NATIVE
    assert parse_runtime_kind("") == AgentRuntimeKind.NATIVE
    assert parse_runtime_kind("codex") == AgentRuntimeKind.CODEX
    assert parse_runtime_kind("not-a-runtime") == AgentRuntimeKind.NATIVE


def test_resolve_runtime_kind_reads_agent_profile_runtime() -> None:
    with _test_session() as db:
        _add_agent(db, "agent_codex", "codex")
        _add_agent(db, "agent_plain", "native")
        assert resolve_runtime_kind(db, "tenant_demo", "agent_codex") == AgentRuntimeKind.CODEX
        assert resolve_runtime_kind(db, "tenant_demo", "agent_plain") == AgentRuntimeKind.NATIVE


def test_resolve_runtime_kind_falls_back_to_session_agent() -> None:
    with _test_session() as db:
        _add_agent(db, "agent_codex", "codex")
        db.add(
            ChatSession(
                id="session_x",
                tenant_id="tenant_demo",
                agent_id="agent_codex",
            )
        )
        db.commit()
        assert resolve_runtime_kind(db, "tenant_demo", None, "session_x") == AgentRuntimeKind.CODEX


def test_resolve_runtime_kind_ignores_cross_tenant_and_unknown_rows() -> None:
    with _test_session() as db:
        _add_agent(db, "agent_codex", "codex")
        _add_agent(db, "agent_other_tenant", "codex", tenant_id="tenant_other")
        assert resolve_runtime_kind(db, "tenant_demo", "agent_other_tenant") == AgentRuntimeKind.NATIVE
        assert resolve_runtime_kind(db, "tenant_demo", "agent_missing") == AgentRuntimeKind.NATIVE
        assert resolve_runtime_kind(db, "tenant_demo", None, "session_missing") == AgentRuntimeKind.NATIVE


def test_resolve_runtime_kind_treats_unknown_stored_value_as_native() -> None:
    with _test_session() as db:
        _add_agent(db, "agent_legacy", "some_future_runtime")
        assert resolve_runtime_kind(db, "tenant_demo", "agent_legacy") == AgentRuntimeKind.NATIVE


# ---------------------------------------------------------------------------
# agents API runtime fields
# ---------------------------------------------------------------------------


def test_create_agent_persists_runtime_and_config() -> None:
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="Codex 员工",
                source_mode="blank",
                runtime=AgentRuntimeKind.CODEX,
                runtime_config={"model": "gpt-5-codex"},
            ),
            db=db,
            current_user=owner,
        )
        assert created.runtime == "codex"
        assert created.runtime_config == {"model": "gpt-5-codex"}
        row = db.get(AgentProfile, created.id)
        assert row is not None
        assert row.runtime == "codex"
        assert row.runtime_config_json == {"model": "gpt-5-codex"}


def test_create_agent_defaults_to_native_runtime() -> None:
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="默认员工",
                source_mode="blank",
            ),
            db=db,
            current_user=owner,
        )
        assert created.runtime == "native"
        assert created.runtime_config == {}


def test_overall_agent_must_use_native_runtime() -> None:
    with _test_session() as db:
        _owner, admin = _seed_users(db)
        with pytest.raises(HTTPException) as exc:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo",
                    name="广场总员工",
                    is_overall=True,
                    source_mode="blank",
                    runtime=AgentRuntimeKind.CODEX,
                ),
                db=db,
                current_user=admin,
            )
        assert exc.value.status_code == 409


def test_gallery_published_agent_rejected_when_cli_unavailable(monkeypatch) -> None:
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        monkeypatch.setattr("app.runtimes.adapters.codex.codex_cli_available", lambda: False)
        with pytest.raises(HTTPException) as exc:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo",
                    name="广场员工",
                    source_mode="blank",
                    runtime=AgentRuntimeKind.CODEX,
                    metadata={"published_to_gallery": True},
                ),
                db=db,
                current_user=owner,
            )
        assert exc.value.status_code == 409


def test_gallery_published_agent_allowed_when_cli_available(monkeypatch) -> None:
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        monkeypatch.setattr("app.runtimes.adapters.codex.codex_cli_available", lambda: True)
        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="广场编码员工",
                source_mode="blank",
                runtime=AgentRuntimeKind.CODEX,
                metadata={"published_to_gallery": True},
            ),
            db=db,
            current_user=owner,
        )
        assert created.runtime == "codex"
        assert created.metadata.get("published_to_gallery") is True


def test_update_agent_switches_runtime_and_validates_resulting_state(monkeypatch) -> None:
    monkeypatch.setattr("app.runtimes.adapters.codex.codex_cli_available", lambda: False)
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="可切换员工",
                source_mode="blank",
            ),
            db=db,
            current_user=owner,
        )
        updated = update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                runtime=AgentRuntimeKind.CODEX,
                runtime_config={"model": "gpt-5-codex"},
            ),
            db=db,
            current_user=owner,
        )
        assert updated.runtime == "codex"
        assert updated.runtime_config == {"model": "gpt-5-codex"}

        # 已切到 codex 后再发布到广场：CLI 不可用 → 409（运行时不在本次请求中也按结果态校验）
        with pytest.raises(HTTPException) as exc:
            update_agent(
                created.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo",
                    metadata={"published_to_gallery": True},
                ),
                db=db,
                current_user=owner,
            )
        assert exc.value.status_code == 409

        # 已发布员工（先切回 native 并发布）再切 codex：CLI 不可用 → 409
        update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                runtime=AgentRuntimeKind.NATIVE,
                metadata={"published_to_gallery": True},
            ),
            db=db,
            current_user=owner,
        )
        with pytest.raises(HTTPException) as exc:
            update_agent(
                created.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo",
                    runtime=AgentRuntimeKind.CODEX,
                ),
                db=db,
                current_user=owner,
            )
        assert exc.value.status_code == 409


def test_update_agent_can_publish_codex_when_cli_available(monkeypatch) -> None:
    monkeypatch.setattr("app.runtimes.adapters.codex.codex_cli_available", lambda: True)
    with _test_session() as db:
        owner, _admin = _seed_users(db)
        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="发布编码员工",
                source_mode="blank",
            ),
            db=db,
            current_user=owner,
        )
        update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                runtime=AgentRuntimeKind.CODEX,
                metadata={"published_to_gallery": True},
            ),
            db=db,
            current_user=owner,
        )
        # 已发布状态下再切回 codex（已在 codex）不冲突；切 native 再切回也放行
        updated = update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                runtime=AgentRuntimeKind.NATIVE,
            ),
            db=db,
            current_user=owner,
        )
        assert updated.runtime == "native"
        assert updated.metadata.get("published_to_gallery") is True
        updated = update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                runtime=AgentRuntimeKind.CODEX,
            ),
            db=db,
            current_user=owner,
        )
        assert updated.runtime == "codex"
        assert updated.metadata.get("published_to_gallery") is True


# ---------------------------------------------------------------------------
# schema migration
# ---------------------------------------------------------------------------


def _create_legacy_agent_profiles(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE agent_profiles (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    name VARCHAR,
                    description VARCHAR,
                    persona_prompt VARCHAR,
                    is_overall BOOLEAN,
                    status VARCHAR,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_profiles "
                "(id, tenant_id, name, is_overall, status, metadata_json) VALUES "
                "('agent_legacy', 'tenant_demo', '旧员工', 0, 'active', '{}')"
            )
        )


def test_agent_profiles_runtime_migration_is_idempotent_and_preserves_rows(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "legacy-agent-profiles.db"
    engine = sa_create_engine(f"sqlite:///{db_path}")
    _create_legacy_agent_profiles(engine)
    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    database._migrate_sqlite_skill_schema()

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(agent_profiles)"))}
        assert "runtime" in columns
        assert "runtime_config_json" in columns
        row = conn.execute(
            text("SELECT runtime, runtime_config_json FROM agent_profiles WHERE id = 'agent_legacy'")
        ).first()
        assert row is not None
        assert row[0] == "native"
