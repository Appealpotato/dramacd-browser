"""Pipeline concurrency caps: the DynamicSemaphore gate + the runtime
max-whisper/max-llm job settings that back it."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from pipeline.concurrency import DynamicSemaphore


class DynamicSemaphoreTests(unittest.TestCase):
    def test_caps_concurrency_to_limit(self):
        async def run():
            limit = {"n": 2}
            sem = DynamicSemaphore(lambda: limit["n"], name="t")
            state = {"cur": 0, "peak": 0}

            async def worker():
                async with sem:
                    state["cur"] += 1
                    state["peak"] = max(state["peak"], state["cur"])
                    await asyncio.sleep(0.03)
                    state["cur"] -= 1

            await asyncio.gather(*[worker() for _ in range(8)])
            return state["peak"]

        self.assertEqual(asyncio.run(run()), 2)

    def test_limit_one_serializes(self):
        async def run():
            sem = DynamicSemaphore(lambda: 1)
            state = {"cur": 0, "peak": 0}

            async def worker():
                async with sem:
                    state["cur"] += 1
                    state["peak"] = max(state["peak"], state["cur"])
                    await asyncio.sleep(0.02)
                    state["cur"] -= 1

            await asyncio.gather(*[worker() for _ in range(5)])
            return state["peak"]

        self.assertEqual(asyncio.run(run()), 1)

    def test_would_wait_tracks_dynamic_limit(self):
        async def run():
            limit = {"n": 1}
            sem = DynamicSemaphore(lambda: limit["n"])
            await sem.acquire()                       # active = 1
            self.assertTrue(await sem.would_wait())   # at limit 1
            limit["n"] = 2                            # raise live
            self.assertFalse(await sem.would_wait())  # now under limit
            await sem.acquire()                       # active = 2
            self.assertTrue(await sem.would_wait())
            await sem.release()
            await sem.release()
            self.assertFalse(await sem.would_wait())

        asyncio.run(run())

    def test_raising_limit_wakes_waiters(self):
        async def run():
            limit = {"n": 1}
            sem = DynamicSemaphore(lambda: limit["n"])
            order = []

            async def worker(tag):
                async with sem:
                    order.append(tag)
                    await asyncio.sleep(0.02)

            await sem.acquire()              # occupy the only slot
            t = asyncio.create_task(worker("waiter"))
            await asyncio.sleep(0.02)
            self.assertEqual(order, [])      # waiter is parked
            limit["n"] = 2                   # room opens up
            await sem.release()              # also notifies
            await t
            return order

        self.assertEqual(asyncio.run(run()), ["waiter"])

    def test_async_getter_supported(self):
        async def run():
            async def aget():
                return 1
            sem = DynamicSemaphore(aget)
            await sem.acquire()
            self.assertTrue(await sem.would_wait())
            await sem.release()

        asyncio.run(run())

    def test_broken_getter_falls_back_to_default(self):
        async def run():
            def boom():
                raise RuntimeError("nope")
            sem = DynamicSemaphore(boom, default=1)
            self.assertEqual(await sem.current_limit(), 1)

        asyncio.run(run())


class ConcurrencySettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_defaults_when_unset(self):
        # Fresh DB: should report the config defaults (whisper=1, llm=3).
        self.assertEqual(asyncio.run(database.get_runtime_max_whisper_jobs()), 1)
        self.assertEqual(asyncio.run(database.get_runtime_max_llm_jobs()), 3)

    def test_round_trip(self):
        asyncio.run(database.set_runtime_max_whisper_jobs(2))
        asyncio.run(database.set_runtime_max_llm_jobs(5))
        self.assertEqual(asyncio.run(database.get_runtime_max_whisper_jobs()), 2)
        self.assertEqual(asyncio.run(database.get_runtime_max_llm_jobs()), 5)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_max_whisper_jobs(0))
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_max_whisper_jobs(99))
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_max_llm_jobs(0))
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_max_llm_jobs(99))

    def test_rejects_non_integer(self):
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_max_llm_jobs("lots"))

    def test_corrupt_stored_value_clamps_to_default(self):
        # A hand-edited / garbage setting must not crash the gate.
        asyncio.run(database.set_app_setting(database.RUNTIME_MAX_LLM_JOBS_SETTING, "garbage"))
        self.assertEqual(asyncio.run(database.get_runtime_max_llm_jobs()), database.MAX_LLM_JOBS)


if __name__ == "__main__":
    unittest.main()
