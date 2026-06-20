import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cognee_adapter


class CogneeAdapterTests(unittest.TestCase):
    def test_adapter_reports_unavailable_without_installed_cognee(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            queue = root / "cognee_ingest_queue.jsonl"
            status = root / "cognee_ingest_status.json"
            queue.write_text(json.dumps({"inspection_id": "HSE-1", "summary": "loose cable"}) + "\n")

            with mock.patch.object(cognee_adapter.importlib.util, "find_spec", return_value=None):
                result = asyncio.run(cognee_adapter.ingest_queue(queue, status_path=status, limit=10, dry_run=False))

            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(result["package_available"])
            self.assertEqual(result["records_seen"], 1)
            self.assertEqual(result["records_sent"], 0)
            written = json.loads(status.read_text())
            self.assertEqual(written["status"], "unavailable")

    def test_adapter_dry_run_builds_memory_text_without_importing_cognee(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            queue = root / "cognee_ingest_queue.jsonl"
            queue.write_text(json.dumps({
                "inspection_id": "HSE-2",
                "created_at": "2026-06-20T12:00:00+00:00",
                "location": "Workshop",
                "summary": "Trailing cable across walkway",
                "hazard_categories": ["trip_hazard"],
                "findings": [{"title": "Trailing cable", "risk_level": "medium", "corrective_action": "route cable safely"}],
            }) + "\n")

            with mock.patch.object(cognee_adapter.importlib.util, "find_spec", return_value=None):
                result = asyncio.run(cognee_adapter.ingest_queue(queue, status_path=root / "status.json", dry_run=True))

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["records_seen"], 1)
            self.assertEqual(result["records_sent"], 0)
            self.assertIn("HSE-2", result["preview"][0])
            self.assertIn("trip_hazard", result["preview"][0])

    def test_adapter_sends_records_to_cognee_module(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            queue = root / "cognee_ingest_queue.jsonl"
            status = root / "status.json"
            queue.write_text("\n".join([
                json.dumps({"inspection_id": "HSE-3", "location": "Office", "summary": "Blocked exit", "hazard_categories": ["fire_or_exit_risk"]}),
                json.dumps({"inspection_id": "HSE-4", "location": "Office", "summary": "Loose charger", "hazard_categories": ["electrical_hazard"]}),
            ]) + "\n")

            calls = []

            class FakeCognee:
                async def serve(self, url=None, api_key=None):
                    calls.append(("serve", url, api_key))

                async def remember(self, text, dataset_name="main_dataset"):
                    calls.append(("remember", dataset_name, text))
                    return {"ok": True}

            fake = FakeCognee()
            old = sys.modules.get("cognee")
            sys.modules["cognee"] = fake
            try:
                with mock.patch.object(cognee_adapter.importlib.util, "find_spec", return_value=object()), \
                     mock.patch.dict("os.environ", {
                         "COGNEE_SERVICE_URL": "https://example.cognee.local",
                         "COGNEE_API_KEY": "secret-not-printed",
                     }, clear=False):
                    result = asyncio.run(cognee_adapter.ingest_queue(queue, status_path=status, dataset_name="proofsight_hse"))
            finally:
                if old is None:
                    sys.modules.pop("cognee", None)
                else:
                    sys.modules["cognee"] = old

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["records_seen"], 2)
            self.assertEqual(result["records_sent"], 2)
            self.assertEqual(calls[0], ("serve", "https://example.cognee.local", "secret-not-printed"))
            self.assertEqual(calls[1][0:2], ("remember", "proofsight_hse"))
            self.assertIn("HSE-3", calls[1][2])
            written = json.loads(status.read_text())
            self.assertEqual(written["records_sent"], 2)


if __name__ == "__main__":
    unittest.main()
