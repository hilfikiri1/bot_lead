"""Agent v4.2 unified project workspace regression and integration contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent import (
    digest,
    executor,
    notion_gateway,
    project_snapshot,
    project_updates,
    service as agent_service,
)
from app.agent.lead_refs import LeadRefType, parse_lead_references
from app.agent.planner import deterministic_plan
from app.models.pending_agent_action import PendingAgentAction
from app.models.project_artifact import ProjectArtifact
from app.services import (
    google_drive_service,
    identity_service,
    kommo_service,
    project_artifact_service,
    storage_service,
)


def test_migration_and_models_cover_workspace_audit():
    pending_columns = set(PendingAgentAction.__table__.columns.keys())
    artifact_columns = set(ProjectArtifact.__table__.columns.keys())
    assert "batch_group_id" in pending_columns
    assert {
        "project_link_id",
        "kommo_lead_id",
        "telegram_user_id",
        "telegram_message_id",
        "artifact_type",
        "suggested_filename",
        "subfolder_name",
        "drive_file_id",
        "notion_page_id",
        "kommo_note_created",
        "uploaded_by_telegram_user_id",
    }.issubset(artifact_columns)


def test_planner_opens_project_by_internal_number():
    plan = deterministic_plan("покажи проект 134", {})
    assert plan is not None
    assert plan.intent == "project_snapshot"
    assert plan.mode == "read"
    assert plan.lead_refs[0]["internal_lead_number"] == "134"


def test_planner_opens_project_by_person_name():
    plan = deterministic_plan("что по Maciej Walasek?", {})
    assert plan is not None
    assert plan.intent == "project_snapshot"
    assert plan.lead_refs[0]["name_query"] == "maciej walasek"


def test_planner_searches_project_by_phone():
    plan = deterministic_plan("найди проект по телефону +48 790 870 113", {})
    assert plan is not None
    assert plan.intent == "search_project"
    assert "+48 790 870 113" in str(plan.query)


def test_planner_recognizes_spoken_project_update():
    plan = deterministic_plan(
        "По проекту 134 поговорил с клиентом. Подготовить расчёт до пятницы.",
        {},
    )
    assert plan is not None
    assert plan.intent == "project_update_bundle"
    assert plan.mode == "write"


def test_project_word_marks_internal_reference():
    refs = parse_lead_references("обнови проект 134", {})
    assert any(
        ref.ref_type == LeadRefType.INTERNAL_NUMBER
        and ref.internal_lead_number == "134"
        for ref in refs
    )


@pytest.mark.parametrize(
    ("caption", "filename", "mime", "expected_type", "expected_folder"),
    [
        (
            "Это предложение производителя для проекта 134",
            "offer.pdf",
            "application/pdf",
            "supplier_offer",
            "04 Прайсы фабрик",
        ),
        (
            "техническое задание",
            "spec.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "technical_spec",
            "02 Техническое задание",
        ),
        (
            "каталог фабрики",
            "catalog.pdf",
            "application/pdf",
            "catalog",
            "03 Поставщики и RFQ",
        ),
        (
            "подписанный договор",
            "agreement.pdf",
            "application/pdf",
            "contract",
            "10 Договоры, инвойсы и оплата",
        ),
        (
            None,
            "photo.jpg",
            "image/jpeg",
            "photo",
            "05 Фото, видео и образцы",
        ),
        (
            None,
            "calculation.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "calculation",
            "06 Расчёты и сравнение",
        ),
    ],
)
def test_smart_file_classification(
    caption, filename, mime, expected_type, expected_folder
):
    result = project_artifact_service.classify_artifact(
        filename=filename,
        mime_type=mime,
        caption=caption,
        kind="photo" if mime.startswith("image/") else "document",
    )
    assert result.artifact_type == expected_type
    assert result.subfolder_name == expected_folder


def test_suggested_filename_is_project_scoped_and_preserves_extension():
    classification = project_artifact_service.classify_artifact(
        filename="报价 07.28.pdf",
        mime_type="application/pdf",
        caption="предложение производителя",
    )
    result = project_artifact_service.suggested_filename(
        project_key="BBS-PL-0134",
        classification=classification,
        original_filename="报价 07.28.pdf",
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    assert result.startswith("2026-07-29")
    assert "BBS-PL-0134" in result
    assert "Предложение производителя" in result
    assert result.endswith(".pdf")
    assert "/" not in result


def test_spoken_update_splits_note_task_deadline_and_followup():
    result = project_updates.analyse_update(
        "По проекту 134 поговорил с клиентом. Ему нужны кормушки и поилки "
        "в одинаковом количестве. Подготовить расчёт до пятницы."
    )
    assert "кормушки и поилки" in result.note_text
    assert result.task_text == "Подготовить расчёт"
    assert result.due_at == "пятницу в 17:00"
    assert result.next_step == "Подготовить расчёт"
    assert result.should_prepare_followup


def test_spoken_update_preserves_explicit_time():
    result = project_updates.analyse_update(
        "По проекту 134 поговорил с клиентом. Позвонить завтра в 10:30."
    )
    assert result.due_at == "завтра в 10:30"


def test_bundle_markup_has_individual_and_all_confirmations():
    staged = [
        SimpleNamespace(id=1, action_type="add_kommo_note"),
        SimpleNamespace(id=2, action_type="create_project_task"),
    ]
    markup = project_updates.bundle_markup(staged, "abc123")
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    callback_values = {button["callback_data"] for button in buttons}
    assert "agent:ok:1" in callback_values
    assert "agent:ok:2" in callback_values
    assert "agent:bundle:abc123:all" in callback_values
    assert "agent:bundle:abc123:no" in callback_values


@pytest.mark.asyncio
async def test_stage_bundle_creates_independent_audited_actions():
    lead = {
        "id": 77,
        "name": "134 - кормушки",
        "url": "https://kommo.test/77",
        "contacts": [],
    }
    proposal = project_updates.analyse_update(
        "По проекту 134 поговорил с клиентом. Подготовить расчёт до пятницы."
    )
    created = []

    async def fake_stage(*args, **kwargs):
        item = SimpleNamespace(
            id=len(created) + 1,
            action_type=kwargs["action_type"],
            batch_group_id=kwargs.get("batch_group_id"),
        )
        created.append((item, kwargs))
        return item

    with patch.object(project_updates.actions, "stage_action", new=fake_stage):
        staged, group_id = await project_updates.stage_bundle(
            AsyncMock(),
            chat_id=10,
            telegram_user_id=20,
            lead=lead,
            proposal=proposal,
            followup_draft={"body": "Dzień dobry", "language": "pl"},
            language_source="market_fallback",
            client_id=None,
        )
    action_types = [item.action_type for item in staged]
    assert action_types == [
        "add_kommo_note",
        "create_notion_communication",
        "create_project_task",
        "update_project_next_step",
        "prepare_client_followup",
    ]
    assert all(kwargs["batch_group_id"] == group_id for _, kwargs in created)


def test_project_card_actions_cover_workspace_commands():
    snapshot = project_snapshot.ProjectSnapshot(
        identity={"kommo_lead_id": 77},
        kommo={"url": "https://kommo.test/77"},
        notion={"url": "https://notion.test/page"},
        drive={"url": "https://drive.test/folder"},
    )
    markup = project_snapshot.project_actions_markup(snapshot)
    labels = {
        button["text"]
        for row in markup["inline_keyboard"]
        for button in row
    }
    assert {
        "🔄 Обновить статус",
        "✅ Добавить задачу",
        "📎 Загрузить файл",
        "✍️ Follow-up",
        "💬 Переписка",
        "🕘 История",
        "📁 Drive",
        "🔗 Kommo",
        "📝 Notion",
    }.issubset(labels)


def test_unified_card_formats_contacts_tasks_files_and_next_step():
    snapshot = project_snapshot.ProjectSnapshot(
        identity={
            "project_key": "BBS-PL-0134",
            "internal_lead_number": "134",
            "kommo_lead_id": 77,
        },
        client={
            "name": "Maciej Walasek",
            "company": "MasterTech",
            "phones": ["+48790870113"],
            "emails": ["maciej@example.pl"],
            "language": "pl",
        },
        kommo={
            "name": "134 - tarcze lamelkowe",
            "status": "W pracy",
            "updated_at": 1785290000,
            "notes": [{"text": "Klient czeka na próbki."}],
            "url": "https://kommo.test/77",
        },
        responsible={"name": "Евгений"},
        notion={"url": "https://notion.test/page"},
        drive={"url": "https://drive.test/folder"},
        open_tasks=[{"title": "Подготовить расчёт", "due_at": "2026-07-31T17:00:00+02:00"}],
        documents=[
            {
                "name": "factory-offer.pdf",
                "type": "Предложение производителя",
                "url": "https://drive.test/file",
            }
        ],
        recommended_next_action="Подготовить расчёт",
    )
    text = project_snapshot.format_snapshot(snapshot)
    for expected in (
        "Maciej Walasek",
        "MasterTech",
        "PL",
        "Евгений",
        "Подготовить расчёт",
        "factory-offer.pdf",
        "Kommo",
        "Notion",
        "Drive",
    ):
        assert expected in text
    assert len(text) <= 4000


def test_digest_format_includes_cross_system_health_and_top_five():
    item = {
        "position": 1,
        "score": 100,
        "internal_lead_number": "134",
        "kommo_lead_id": 77,
        "name": "Кормушки",
        "priority": "Высокий",
        "reason": "просрочена задача",
        "next_step": "Позвонить",
        "url": "https://kommo.test/77",
    }
    result = {
        "open_count": 10,
        "digest_map": [item],
        "top_actions": [item],
        "sections": digest.group_digest_sections([item]),
        "health": {
            "overdue_tasks": 2,
            "without_next_step": 3,
            "stale_clients": 4,
            "new_files": 5,
            "pending_actions": 6,
            "sync_discrepancies": 1,
            "discrepancy_examples": ["BBS-PL-0134: нет Notion"],
        },
    }
    text = digest.format_digest(result)
    assert "Просроченные задачи" in text
    assert "Без следующего шага" in text
    assert "Новые файлы" in text
    assert "Неподтверждённые действия" in text
    assert "Расхождения Kommo/Notion/Drive" in text
    assert "Пять главных действий" in text


@pytest.mark.asyncio
async def test_kommo_open_tasks_are_normalized():
    payload = {
        "_embedded": {
            "tasks": [
                {
                    "id": 1,
                    "text": "Подготовить КП",
                    "complete_till": 1785500000,
                    "responsible_user_id": 9,
                    "is_completed": False,
                },
                {"id": 2, "text": "Старое", "is_completed": True},
            ]
        }
    }
    with patch.object(kommo_service, "_request", new=AsyncMock(return_value=payload)):
        tasks = await kommo_service.get_open_lead_tasks(77)
    assert tasks == [
        {
            "id": 1,
            "text": "Подготовить КП",
            "complete_till": 1785500000,
            "responsible_user_id": 9,
            "task_type_id": None,
            "is_completed": False,
            "source": "kommo",
        }
    ]


@pytest.mark.asyncio
async def test_project_search_uses_contact_link_when_title_does_not_match():
    contact_payload = {
        "_embedded": {
            "contacts": [
                {"id": 11, "_embedded": {"leads": [{"id": 77}]}}
            ]
        }
    }
    with (
        patch.object(
            kommo_service,
            "search_open_leads",
            new=AsyncMock(return_value={"leads": []}),
        ),
        patch.object(
            kommo_service,
            "_request",
            new=AsyncMock(return_value=contact_payload),
        ),
        patch.object(
            kommo_service,
            "get_lead_details",
            new=AsyncMock(
                return_value={
                    "id": 77,
                    "name": "134 - tarcze",
                    "updated_at": 10,
                    "closed_at": None,
                }
            ),
        ),
    ):
        result = await kommo_service.search_projects("+48790870113")
    assert result["leads"][0]["id"] == 77
    assert result["search_kind"] == "project_title_contact_company_phone"


@pytest.mark.asyncio
async def test_project_search_uses_company_link_when_contact_does_not_match():
    company_payload = {
        "_embedded": {
            "companies": [
                {
                    "id": 12,
                    "name": "MasterTech",
                    "_embedded": {"leads": [{"id": 77}]},
                }
            ]
        }
    }
    request = AsyncMock(
        side_effect=[
            {"_embedded": {"contacts": []}},
            company_payload,
        ]
    )
    with (
        patch.object(
            kommo_service,
            "search_open_leads",
            new=AsyncMock(return_value={"leads": []}),
        ),
        patch.object(kommo_service, "_request", new=request),
        patch.object(
            kommo_service,
            "get_lead_details",
            new=AsyncMock(
                return_value={
                    "id": 77,
                    "name": "134 - tarcze",
                    "updated_at": 10,
                    "closed_at": None,
                }
            ),
        ),
    ):
        result = await kommo_service.search_projects("MasterTech")
    assert result["leads"][0]["company_name"] == "MasterTech"
    assert request.await_args_list[1].args[1] == "/api/v4/companies"


@pytest.mark.asyncio
async def test_exact_project_search_does_not_leak_another_managers_deal():
    manager = SimpleNamespace(
        status="active",
        role="manager",
        lead_access_scope="assigned",
        kommo_user_id=5,
    )
    identity_service.set_current_user(manager)
    try:
        with (
            patch.object(
                kommo_service,
                "_request",
                new=AsyncMock(
                    return_value={
                        "id": 77,
                        "name": "Secret project",
                        "responsible_user_id": 6,
                        "closed_at": None,
                    }
                ),
            ),
            patch.object(
                kommo_service,
                "get_all_open_leads",
                new=AsyncMock(return_value={"leads": []}),
            ),
        ):
            result = await kommo_service.search_open_leads("77")
    finally:
        identity_service.set_current_user(None)
    assert result["leads"] == []


@pytest.mark.asyncio
async def test_kommo_update_rechecks_manager_access_before_patch():
    request = AsyncMock()
    with (
        patch.object(
            kommo_service,
            "get_lead_details",
            new=AsyncMock(side_effect=PermissionError("reassigned")),
        ),
        patch.object(kommo_service, "_request", new=request),
    ):
        with pytest.raises(PermissionError, match="reassigned"):
            await kommo_service.update_kommo_lead(77, status_id=123)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_uploaded_telegram_file_is_not_staged_again():
    existing = SimpleNamespace(
        id=9,
        status="uploaded",
        drive_file_url="https://drive.test/file",
    )
    with (
        patch.object(
            agent_service.project_artifact_service,
            "get_by_telegram_message",
            new=AsyncMock(return_value=existing),
        ),
        patch.object(
            agent_service.actions,
            "stage_action",
            new=AsyncMock(),
        ) as stage,
    ):
        reply = await agent_service.handle_project_file_upload(
            AsyncMock(),
            chat_id=1,
            telegram_user_id=99,
            telegram_message_id=555,
            filename="offer.pdf",
            mime_type="application/pdf",
            content=b"%PDF",
        )
    assert reply.intent == "file_upload_duplicate"
    assert "уже обработан" in reply.text
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_file_upload_rechecks_access_and_requires_target_folder():
    action = SimpleNamespace(
        id=88,
        telegram_user_id=99,
        action_type="save_file_to_drive_project",
        payload={
            "kommo_lead_id": 77,
            "filename": "offer.pdf",
            "mime_type": "application/pdf",
            "storage_path": "/tmp/offer.pdf",
            "subfolder_name": "04 Прайсы фабрик",
        },
    )
    link = SimpleNamespace(
        project_key="BBS-PL-0134",
        drive_folder_id="root-folder",
        notion_project_page_id=None,
    )
    with (
        patch.object(
            executor.kommo_service,
            "get_lead_details",
            new=AsyncMock(return_value={"id": 77, "responsible_user_id": 5}),
        ) as access_check,
        patch.object(
            executor.project_link_service,
            "get_by_kommo_lead_id",
            new=AsyncMock(return_value=link),
        ),
        patch.object(
            executor.google_drive_service,
            "list_project_files",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            executor.google_drive_service,
            "upload_file",
            new=AsyncMock(),
        ) as upload,
    ):
        with pytest.raises(ValueError, match="Подпапка Drive"):
            await executor._execute(AsyncMock(), action)
    access_check.assert_awaited_once_with(77)
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_file_upload_runs_drive_notion_kommo_chain():
    action = SimpleNamespace(
        id=88,
        telegram_user_id=99,
        action_type="save_file_to_drive_project",
        payload={
            "kommo_lead_id": 77,
            "filename": "offer.pdf",
            "mime_type": "application/pdf",
            "storage_path": "/tmp/offer.pdf",
            "subfolder_name": "04 Прайсы фабрик",
            "artifact_type": "supplier_offer",
            "artifact_type_label": "Предложение производителя",
        },
    )
    link = SimpleNamespace(
        project_key="BBS-PL-0134",
        drive_folder_id="root-folder",
        notion_project_page_id="notion-project",
    )
    upload_result = {
        "id": "drive-file",
        "name": "offer.pdf",
        "webViewLink": "https://drive.test/file",
    }
    notion_result = {
        "id": "notion-project",
        "url": "https://notion.test/project",
    }
    with (
        patch.object(
            executor.kommo_service,
            "get_lead_details",
            new=AsyncMock(return_value={"id": 77, "responsible_user_id": 5}),
        ),
        patch.object(
            executor.project_link_service,
            "get_by_kommo_lead_id",
            new=AsyncMock(return_value=link),
        ),
        patch.object(
            executor.google_drive_service,
            "list_project_files",
            new=AsyncMock(
                return_value=[
                    {"id": "price-folder", "name": "04 Прайсы фабрик"}
                ]
            ),
        ),
        patch.object(
            executor.storage_service,
            "read_project_file_bytes",
            return_value=b"%PDF",
        ),
        patch.object(
            executor.google_drive_service,
            "upload_file",
            new=AsyncMock(return_value=upload_result),
        ) as upload,
        patch.object(
            executor.notion_gateway,
            "append_project_file_record",
            new=AsyncMock(return_value=notion_result),
        ) as notion,
        patch.object(
            executor.kommo_service,
            "add_common_note",
            new=AsyncMock(return_value=True),
        ) as kommo_note,
        patch.object(
            executor.storage_service,
            "delete_project_file",
        ) as cleanup,
    ):
        result = await executor._execute(AsyncMock(), action)
    assert result["data"]["file_id"] == "drive-file"
    assert result["data"]["warnings"] == []
    assert upload.await_args.kwargs["parent_folder_id"] == "price-folder"
    notion.assert_awaited_once()
    kommo_note.assert_awaited_once()
    cleanup.assert_called_once_with("/tmp/offer.pdf")


@pytest.mark.asyncio
async def test_failed_drive_upload_marks_audit_and_cleans_staging_file():
    action = SimpleNamespace(
        id=88,
        telegram_user_id=99,
        action_type="save_file_to_drive_project",
        payload={
            "kommo_lead_id": 77,
            "artifact_id": 9,
            "filename": "offer.pdf",
            "mime_type": "application/pdf",
            "storage_path": "/tmp/offer.pdf",
            "subfolder_name": "04 Прайсы фабрик",
        },
    )
    link = SimpleNamespace(
        project_key="BBS-PL-0134",
        drive_folder_id="root-folder",
    )
    artifact = SimpleNamespace(id=9, kommo_lead_id=77)
    with (
        patch.object(
            executor.kommo_service,
            "get_lead_details",
            new=AsyncMock(return_value={"id": 77}),
        ),
        patch.object(
            executor.project_link_service,
            "get_by_kommo_lead_id",
            new=AsyncMock(return_value=link),
        ),
        patch.object(
            executor.project_artifact_service,
            "get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch.object(
            executor.google_drive_service,
            "list_project_files",
            new=AsyncMock(
                return_value=[
                    {"id": "price-folder", "name": "04 Прайсы фабрик"}
                ]
            ),
        ),
        patch.object(
            executor.storage_service,
            "read_project_file_bytes",
            return_value=b"%PDF",
        ),
        patch.object(
            executor.google_drive_service,
            "upload_file",
            new=AsyncMock(side_effect=RuntimeError("Drive unavailable")),
        ),
        patch.object(
            executor.storage_service,
            "delete_project_file",
        ) as cleanup,
        patch.object(
            executor.project_artifact_service,
            "mark_failed",
            new=AsyncMock(),
        ) as mark_failed,
    ):
        with pytest.raises(RuntimeError, match="Drive unavailable"):
            await executor._execute(AsyncMock(), action)
    cleanup.assert_called_once_with("/tmp/offer.pdf")
    mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_project_file_s3_staging_supports_read_and_cleanup():
    client = MagicMock()
    client.get_object.return_value = {"Body": BytesIO(b"%PDF")}
    with (
        patch.object(storage_service.settings, "storage_backend", "s3"),
        patch.object(storage_service.settings, "s3_bucket_name", "bbs-test"),
        patch.object(storage_service, "_s3_client", return_value=client),
    ):
        path = await storage_service.save_project_file(
            b"%PDF",
            "offer.pdf",
            "application/pdf",
        )
        assert path.startswith("s3://bbs-test/project_files/")
        assert storage_service.read_project_file_bytes(path) == b"%PDF"
        storage_service.delete_project_file(path)
    put_kwargs = client.put_object.call_args.kwargs
    assert put_kwargs["ContentType"] == "application/pdf"
    assert put_kwargs["Key"].startswith("project_files/")
    client.delete_object.assert_called_once_with(
        Bucket="bbs-test",
        Key=put_kwargs["Key"],
    )


@pytest.mark.asyncio
async def test_bundle_confirm_all_executes_each_independent_action():
    actions_list = [
        SimpleNamespace(
            id=1,
            action_type="add_kommo_note",
            result=None,
        ),
        SimpleNamespace(
            id=2,
            action_type="create_project_task",
            result=None,
        ),
    ]
    with (
        patch.object(
            agent_service.memory,
            "get_or_create_session",
            new=AsyncMock(return_value=SimpleNamespace()),
        ),
        patch.object(
            agent_service.memory,
            "build_context",
            new=AsyncMock(return_value={}),
        ),
        patch.object(
            agent_service.actions,
            "get_batch_actions",
            new=AsyncMock(return_value=actions_list),
        ),
        patch.object(
            agent_service,
            "execute_action",
            new=AsyncMock(side_effect=["✅ note", "✅ task"]),
        ) as execute,
    ):
        reply = await agent_service.handle_callback(
            AsyncMock(),
            callback_data="agent:bundle:batch123:all",
            telegram_user_id=99,
            chat_id=1,
        )
    assert reply is not None
    assert reply.intent == "bundle_executed"
    assert reply.metadata["success"] == 2
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_unified_snapshot_aggregates_all_connected_sources():
    link = SimpleNamespace(
        project_key="BBS-PL-0134",
        notion_project_page_id="notion-page",
        notion_project_url="https://notion.test/page",
        drive_folder_id="drive-folder",
        drive_folder_url="https://drive.test/folder",
        drive_folder_name="BBS-PL-0134 — кормушки",
        metadata_json={"next_step": "Подготовить расчёт"},
    )
    lead = {
        "id": 77,
        "name": "134 - кормушки",
        "status_name": "В работе",
        "responsible_user_id": 5,
        "contacts": [
            {
                "id": 11,
                "name": "Maciej",
                "phones": ["+48123"],
                "emails": ["m@example.pl"],
                "custom_fields": [],
            }
        ],
        "notes": [{"text": "Клиент ждёт расчёт"}],
    }
    with (
        patch.object(
            project_snapshot.project_link_service,
            "get_by_kommo_lead_id",
            new=AsyncMock(return_value=link),
        ),
        patch.object(
            project_snapshot.client_language_service,
            "read_communication_language",
            new=AsyncMock(
                return_value=SimpleNamespace(language="pl", source="client")
            ),
        ),
        patch.object(
            project_snapshot.kommo_service,
            "get_user_summary",
            new=AsyncMock(return_value={"id": 5, "name": "Евгений"}),
        ),
        patch.object(
            project_snapshot.notion_gateway,
            "read_project_workspace",
            new=AsyncMock(
                return_value={
                    "project": {"id": "notion-page", "url": "https://notion.test/page"},
                    "tasks": [
                        {
                            "title": "Notion task",
                            "due_at": "2026-08-01",
                            "source": "notion",
                        }
                    ],
                    "communications": [{"summary": "Последний звонок"}],
                    "warnings": [],
                }
            ),
        ),
        patch.object(
            project_snapshot.kommo_service,
            "get_open_lead_tasks",
            new=AsyncMock(
                return_value=[
                    {
                        "text": "Kommo task",
                        "complete_till": 1,
                        "source": "kommo",
                    }
                ]
            ),
        ),
        patch.object(
            project_snapshot.project_artifact_service,
            "recent_for_project",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            project_snapshot.google_drive_service,
            "list_project_files",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "file-1",
                        "name": "offer.pdf",
                        "webViewLink": "https://drive.test/file",
                        "modifiedTime": "2026-07-29T10:00:00Z",
                    }
                ]
            ),
        ),
        patch.object(
            project_snapshot,
            "_pending_for_lead",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            project_snapshot.settings,
            "notion_api_token",
            "configured",
        ),
        patch.object(
            project_snapshot.settings,
            "google_drive_enabled",
            True,
        ),
    ):
        snapshot = await project_snapshot.build_snapshot(
            AsyncMock(), lead=lead, context={}
        )
    assert snapshot.client["language"] == "pl"
    assert snapshot.responsible["name"] == "Евгений"
    assert {item.get("source") for item in snapshot.open_tasks} == {
        "notion",
        "kommo",
    }
    assert snapshot.documents[0]["name"] == "offer.pdf"
    assert snapshot.recommended_next_action == "Подготовить расчёт"


def test_notion_property_parser_handles_project_fields():
    page = {
        "properties": {
            "Название": {
                "type": "title",
                "title": [{"plain_text": "134 - кормушки"}],
            },
            "Следующий шаг": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "Подготовить расчёт"}],
            },
            "Приоритет": {
                "type": "select",
                "select": {"name": "Высокий"},
            },
            "Срок": {
                "type": "date",
                "date": {"start": "2026-07-31T17:00:00+02:00"},
            },
        }
    }
    parsed = notion_gateway._page_properties(page)
    assert parsed["Название"] == "134 - кормушки"
    assert parsed["Следующий шаг"] == "Подготовить расчёт"
    assert parsed["Приоритет"] == "Высокий"
    assert parsed["Срок"].startswith("2026-07-31")


def test_drive_subfolder_contract_still_contains_all_workspace_sections():
    assert len(google_drive_service.PROJECT_SUBFOLDERS) == 11
    assert "04 Прайсы фабрик" in google_drive_service.PROJECT_SUBFOLDERS
    assert "07 Коммерческие предложения" in google_drive_service.PROJECT_SUBFOLDERS
    assert "10 Договоры, инвойсы и оплата" in google_drive_service.PROJECT_SUBFOLDERS


def test_executor_contains_all_new_confirmed_handlers():
    source = Path("app/agent/executor.py").read_text(encoding="utf-8")
    for action_type in (
        "create_notion_communication",
        "create_project_task",
        "update_project_next_step",
        "prepare_client_followup",
        "save_file_to_drive_project",
    ):
        assert f'action_type == "{action_type}"' in source


def test_telegram_passes_attachment_kind_to_smart_upload():
    source = Path("app/api/telegram.py").read_text(encoding="utf-8")
    assert 'kind=str(project_file.get("kind") or "document")' in source
    assert "telegram_message_id=message_id" in source
