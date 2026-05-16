import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from routers import api as api_router


class ApiLangAndFilterTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_items_passes_lang_and_multi_filters(self):
        expected_payload = {"items": [], "total": 0, "limit": 500, "offset": 0}

        with patch.object(api_router.db, "get_all_items", AsyncMock(return_value=expected_payload)) as mock_get_all:
            with TestClient(self.app) as client:
                resp = client.get(
                    "/api/items",
                    params=[
                        ("lang", "en"),
                        ("seiyuu", "A"),
                        ("seiyuu", "B"),
                        ("tag", "T1"),
                        ("tag", "T2"),
                    ],
                )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), expected_payload)

        _, kwargs = mock_get_all.await_args
        self.assertEqual(kwargs["lang"], "en")
        self.assertEqual(kwargs["seiyuu"], ["A", "B"])
        self.assertEqual(kwargs["tag"], ["T1", "T2"])


class ApiRefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_refetch_metadata_handles_tuple_response(self):
        with patch.object(api_router, "fetch_metadata_for_code", AsyncMock(return_value=({"title": "ok"}, None))), \
             patch.object(api_router.db, "update_item_metadata", AsyncMock()) as mock_update:
            await api_router._refetch_metadata(1, "RJ00000001")
            self.assertEqual(mock_update.await_count, 1)


class ApiDeleteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_delete_item_ignores_codes_by_default(self):
        item = {"id": 9, "product_code": "RJ12345678", "original_code": "DLJ-12345678"}

        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "add_ignored_codes", AsyncMock(return_value=["RJ12345678", "DLJ-12345678"])) as mock_ignore, \
             patch.object(api_router.db, "delete_item_by_id", AsyncMock(return_value=True)):
            with TestClient(self.app) as client:
                resp = client.delete("/api/items/9")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ignored_codes"], ["RJ12345678", "DLJ-12345678"])
        mock_ignore.assert_awaited_once()

    def test_delete_item_can_skip_ignore(self):
        item = {"id": 10, "product_code": "RJ87654321", "original_code": None}

        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "add_ignored_codes", AsyncMock()) as mock_ignore, \
             patch.object(api_router.db, "delete_item_by_id", AsyncMock(return_value=True)):
            with TestClient(self.app) as client:
                resp = client.delete("/api/items/10?ignore_code=false")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ignored_codes"], [])
        mock_ignore.assert_not_called()


class ApiCoverUploadTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_upload_cover_success(self):
        item = {"id": 5, "product_code": "RJ12345678", "cover_local": None}
        updated = {"id": 5, "product_code": "RJ12345678", "cover_local": "data/covers/RJ12345678_manual_x.png"}

        with tempfile.TemporaryDirectory() as tmp:
            covers = Path(tmp)
            with patch.object(api_router, "COVERS_DIR", covers), \
                 patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
                 patch.object(api_router.db, "set_item_cover", AsyncMock(return_value=updated)):
                with TestClient(self.app) as client:
                    resp = client.put(
                        "/api/items/5/cover",
                        json={"filename": "cover.png", "data_url": "data:image/png;base64,iVBORw0KGgpBQkM="},
                    )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cover_local"], updated["cover_local"])

    def test_upload_cover_rejects_bad_extension(self):
        item = {"id": 7, "product_code": "RJ99999999", "cover_local": None}
        with tempfile.TemporaryDirectory() as tmp:
            covers = Path(tmp)
            with patch.object(api_router, "COVERS_DIR", covers), \
                 patch.object(api_router.db, "get_item", AsyncMock(return_value=item)):
                with TestClient(self.app) as client:
                    resp = client.put(
                        "/api/items/7/cover",
                        json={"filename": "cover.gif", "data_url": "data:image/gif;base64,R0lGODlh"},
                    )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unsupported image type", resp.json()["detail"])


class ApiBulkActionTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_bulk_confirm_reports_success_counts(self):
        first = {"id": 1, "product_code": "RJ00000001"}
        confirmed = {"id": 1, "product_code": "RJ00000001", "confidence": "verified"}
        with patch.object(api_router.db, "get_item", AsyncMock(side_effect=[first, None])), \
             patch.object(api_router.db, "set_confidence_verified", AsyncMock(return_value=confirmed)):
            with TestClient(self.app) as client:
                resp = client.put("/api/bulk/items/confirm", json={"item_ids": [1, 2]})

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["requested"], 2)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 1)


class ApiMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_get_integrity_report(self):
        report = {"duplicate_original_code_groups": 0, "stale_cover_files_count": 0}
        with patch.object(api_router.db, "get_integrity_report", AsyncMock(return_value=report)) as mock_report:
            with TestClient(self.app) as client:
                resp = client.get("/api/maintenance/integrity")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), report)
        mock_report.assert_awaited_once()

    def test_cleanup_stale_covers_dry_run(self):
        payload = {"dry_run": True, "candidate_count": 3, "deleted_count": 0}
        with patch.object(api_router.db, "cleanup_stale_covers", AsyncMock(return_value=payload)) as mock_cleanup:
            with TestClient(self.app) as client:
                resp = client.post("/api/maintenance/cleanup-stale-covers?dry_run=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), payload)
        mock_cleanup.assert_awaited_once()

    def test_list_jobs(self):
        payload = [{"id": 1, "job_type": "scan", "status": "completed"}]
        with patch.object(api_router.db, "get_recent_jobs", AsyncMock(return_value=payload)) as mock_jobs:
            with TestClient(self.app) as client:
                resp = client.get("/api/jobs?limit=8")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"jobs": payload})
        mock_jobs.assert_awaited_once()

    def test_bulk_override_accepts_mixed_results(self):
        item = {"id": 5, "product_code": "RJ11111111"}
        updated = {"id": 5, "product_code": "RJ22222222", "confidence": "verified"}
        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "override_product_code", AsyncMock(return_value=updated)):
            with TestClient(self.app) as client:
                resp = client.put(
                    "/api/bulk/items/override",
                    json={"overrides": [{"item_id": 5, "product_code": "RJ22222222"}, {"item_id": 7, "product_code": "BAD"}]},
                )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["requested"], 2)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 1)


class ApiMetadataTranslationTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_translate_item_metadata_success(self):
        item = {"id": 12, "title": "æ—¥æœ¬èªžã‚¿ã‚¤ãƒˆãƒ«", "description": "æ—¥æœ¬èªžèª¬æ˜Ž", "title_en": None, "description_en": None}
        updated = {"id": 12, "title_en": "English Title", "description_en": "English Description"}
        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router, "_translate_title_description_with_gemini", AsyncMock(return_value=("English Title", "English Description"))), \
             patch.object(api_router.db, "set_item_english_text", AsyncMock(return_value=updated)):
            with TestClient(self.app) as client:
                resp = client.post("/api/items/12/translate-metadata")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "translated")
        self.assertEqual(payload["title_en"], "English Title")
        self.assertEqual(payload["description_en"], "English Description")

    def test_translate_item_metadata_rejects_when_no_jp_fields(self):
        item = {"id": 12, "title": None, "description": None}
        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)):
            with TestClient(self.app) as client:
                resp = client.post("/api/items/12/translate-metadata")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("No JP title/description", resp.json()["detail"])

    def test_format_english_description_inserts_paragraph_breaks(self):
        text = (
            "Mizuki is the owner chef in Tokyo. He falls in love at first sight. "
            "He confesses and they begin dating. He struggles with lustful thoughts. "
            "He tries to hide his arousal during dates. He fears she will hate him if she knows."
        )
        formatted = api_router._format_english_description(text)
        self.assertIn("\n\n", formatted)
        self.assertGreaterEqual(len(formatted.split("\n\n")), 2)

    def test_coerce_translation_payload_accepts_list_shape(self):
        payload = [{"title_en": "Title", "description_en": "Desc"}]
        coerced = api_router._coerce_translation_payload(payload)
        self.assertEqual(coerced["title_en"], "Title")
        self.assertEqual(coerced["description_en"], "Desc")

    def test_coerce_translation_payload_rejects_unusable_shape(self):
        with self.assertRaises(Exception):
            api_router._coerce_translation_payload([{"foo": "bar"}])

    def test_translate_item_metadata_propagates_429(self):
        item = {"id": 12, "title": "æ—¥æœ¬èªžã‚¿ã‚¤ãƒˆãƒ«", "description": "æ—¥æœ¬èªžèª¬æ˜Ž"}
        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "get_runtime_openrouter_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_chutes_api_key", AsyncMock(return_value=None)), \
             patch.object(
                 api_router,
                 "_translate_title_description_with_gemini",
                 AsyncMock(side_effect=HTTPException(status_code=429, detail="Gemini quota/rate limit")),
             ):
            with TestClient(self.app) as client:
                resp = client.post("/api/items/12/translate-metadata")

        self.assertEqual(resp.status_code, 429)

    def test_translate_item_metadata_falls_back_to_openrouter_on_429(self):
        item = {"id": 12, "title": "JP title", "description": "JP desc", "title_en": None, "description_en": None}
        updated = {"id": 12, "title_en": "EN title", "description_en": "EN desc"}
        with patch.object(api_router.db, "get_item", AsyncMock(return_value=item)), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "get_runtime_openrouter_api_key", AsyncMock(return_value="or-key")), \
             patch.object(api_router.db, "get_runtime_chutes_api_key", AsyncMock(return_value=None)), \
             patch.object(
                 api_router,
                 "_translate_title_description_with_gemini",
                 AsyncMock(side_effect=HTTPException(status_code=429, detail="quota")),
             ), \
             patch.object(
                 api_router,
                 "_translate_title_description_with_openrouter",
                 AsyncMock(return_value=("EN title", "EN desc")),
             ), \
             patch.object(api_router.db, "set_item_english_text", AsyncMock(return_value=updated)):
            with TestClient(self.app) as client:
                resp = client.post("/api/items/12/translate-metadata")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload.get("fallback_used"))
        self.assertEqual(payload.get("provider_used"), "openrouter")


class ApiSettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)

    def test_get_ai_settings(self):
        with patch.object(api_router.db, "get_app_setting", AsyncMock(side_effect=[None, None, None])), \
             patch.object(api_router.db, "get_runtime_gemini_api_key", AsyncMock(return_value="abc123")), \
             patch.object(api_router.db, "get_runtime_openrouter_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_chutes_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash")), \
             patch.object(api_router.db, "get_runtime_openrouter_model", AsyncMock(return_value="openrouter/auto")), \
             patch.object(api_router.db, "get_runtime_chutes_model", AsyncMock(return_value="deepseek-ai/DeepSeek-V3.1")), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")):
            with TestClient(self.app) as client:
                resp = client.get("/api/settings/ai")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["translation_provider"], "gemini")
        self.assertEqual(payload["gemini_model"], "gemini-2.0-flash")
        self.assertTrue(payload["gemini_has_api_key"])
        self.assertEqual(payload["gemini_api_key_source"], "env")
        self.assertEqual(payload["openrouter_model"], "openrouter/auto")
        self.assertFalse(payload["openrouter_has_api_key"])
        self.assertEqual(payload["openrouter_api_key_source"], "env")
        self.assertEqual(payload["chutes_model"], "deepseek-ai/DeepSeek-V3.1")
        self.assertFalse(payload["chutes_has_api_key"])
        self.assertEqual(payload["chutes_api_key_source"], "env")

    def test_update_ai_settings_model_and_key(self):
        with patch.object(api_router.db, "set_runtime_gemini_api_key", AsyncMock(return_value=True)) as mock_set_key, \
             patch.object(api_router.db, "set_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash-lite")) as mock_set_model, \
             patch.object(api_router.db, "set_runtime_translation_provider", AsyncMock(return_value="gemini")) as mock_set_provider, \
             patch.object(api_router.db, "get_app_setting", AsyncMock(side_effect=["runtime-key", None, None])), \
             patch.object(api_router.db, "get_runtime_gemini_api_key", AsyncMock(return_value="runtime-key")), \
             patch.object(api_router.db, "get_runtime_openrouter_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_chutes_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash-lite")), \
             patch.object(api_router.db, "get_runtime_openrouter_model", AsyncMock(return_value="openrouter/auto")), \
             patch.object(api_router.db, "get_runtime_chutes_model", AsyncMock(return_value="deepseek-ai/DeepSeek-V3.1")), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")):
            with TestClient(self.app) as client:
                resp = client.put(
                    "/api/settings/ai",
                    json={"translation_provider": "gemini", "gemini_model": "gemini-2.0-flash-lite", "gemini_api_key": "runtime-key"},
                )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["translation_provider"], "gemini")
        self.assertEqual(payload["gemini_api_key_source"], "runtime")
        self.assertIn("translation_provider", payload["updated_fields"])
        self.assertIn("gemini_model", payload["updated_fields"])
        self.assertIn("gemini_api_key", payload["updated_fields"])
        mock_set_provider.assert_awaited_once_with("gemini")
        mock_set_key.assert_awaited_once_with("runtime-key")
        mock_set_model.assert_awaited_once_with("gemini-2.0-flash-lite")

    def test_update_ai_settings_clear_runtime_key(self):
        with patch.object(api_router.db, "clear_runtime_gemini_api_key", AsyncMock(return_value=True)) as mock_clear, \
             patch.object(api_router.db, "get_app_setting", AsyncMock(side_effect=[None, None, None])), \
             patch.object(api_router.db, "get_runtime_gemini_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_openrouter_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_chutes_api_key", AsyncMock(return_value=None)), \
             patch.object(api_router.db, "get_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash")), \
             patch.object(api_router.db, "get_runtime_openrouter_model", AsyncMock(return_value="openrouter/auto")), \
             patch.object(api_router.db, "get_runtime_chutes_model", AsyncMock(return_value="deepseek-ai/DeepSeek-V3.1")), \
             patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")):
            with TestClient(self.app) as client:
                resp = client.put("/api/settings/ai", json={"clear_gemini_api_key": True})

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["translation_provider"], "gemini")
        self.assertEqual(payload["gemini_api_key_source"], "env")
        self.assertFalse(payload["gemini_has_api_key"])
        mock_clear.assert_awaited_once()

    def test_update_ai_settings_rejects_empty_payload(self):
        with TestClient(self.app) as client:
            resp = client.put("/api/settings/ai", json={})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("No settings provided", resp.json()["detail"])

    def test_test_ai_settings_success(self):
        with patch.object(api_router.db, "get_runtime_translation_provider", AsyncMock(return_value="gemini")), \
             patch.object(api_router.db, "get_runtime_gemini_api_key", AsyncMock(return_value="g-key")), \
             patch.object(
                 api_router,
                 "_translate_title_description_with_gemini",
                 AsyncMock(return_value=("English Title", "English Description")),
             ):
            with TestClient(self.app) as client:
                resp = client.post("/api/settings/ai/test")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["provider"], "gemini")


if __name__ == "__main__":
    unittest.main()

