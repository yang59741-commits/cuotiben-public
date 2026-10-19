"""A lane reuses its polling connection without sharing transactions/threads."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from unittest.mock import patch

import pytest

from cuoti_app.services.jobs import JobCoordinator
from tests.test_jobs import JobTestCase


class WorkerPollingConnectionTests(JobTestCase):
    def test_idle_lane_sees_new_jobs_from_an_independent_request_connection(self):
        idle = threading.Event()
        finished = threading.Event()
        stop = threading.Event()
        worker = JobCoordinator(
            self.database, lane="fast", handlers={"analysis": lambda _context, payload: payload},
            outcome_observer=lambda _job: finished.set(),
        )
        run_once = worker.run_once

        def poll():
            result = run_once()
            if result is None:
                idle.set()
            return result

        with patch.object(worker, "run_once", side_effect=poll), patch.object(worker, "cleanup_retention"):
            lane = threading.Thread(target=worker.run_forever, args=(stop,), kwargs={"poll_interval": .01})
            lane.start()
            try:
                assert idle.wait(5)
                request = JobCoordinator(self.database)
                job, created = request.submit("analysis", {"problem": "fixture"})
                assert created and finished.wait(5)
                saved = request.get_job(job["id"])
                assert saved["status"] == "succeeded"
                assert saved["result"] == {"problem": "fixture"}
            finally:
                stop.set()
                lane.join(timeout=5)
            assert not lane.is_alive()

    def test_idle_loop_reuses_one_connection_and_releases_locks_between_polls(self):
        worker = JobCoordinator(self.database)
        stop = threading.Event()
        connections = []
        connect = worker._connect
        run_once = worker.run_once
        polls = []

        def opened():
            db = connect()
            connections.append(db)
            return db

        def poll():
            result = run_once()
            assert result is None
            assert not connections[0].in_transaction
            with self.connection() as peer:
                peer.execute("BEGIN IMMEDIATE")
            polls.append(result)
            if len(polls) == 4:
                stop.set()
            return result

        with patch.object(worker, "_connect", side_effect=opened), patch.object(
            worker, "run_once", side_effect=poll
        ), patch.object(worker, "cleanup_retention"):
            worker.run_forever(stop, poll_interval=.01)

        assert len(polls) == 4
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")

    def test_poll_failure_rolls_back_before_connection_is_reused(self):
        worker = JobCoordinator(self.database)
        with self.connection() as peer:
            peer.execute("CREATE TABLE worker_poll_probe(key TEXT PRIMARY KEY, value TEXT)")
        with worker._polling_session():
            with pytest.raises(RuntimeError, match="abort poll"):
                with worker._connection("claim_next") as db:
                    db.execute("INSERT INTO worker_poll_probe(key,value) VALUES('poll-test','pending')")
                    raise RuntimeError("abort poll")
            with worker._connection("recover_stale_jobs") as next_db:
                assert next_db is db
                assert not next_db.in_transaction
                assert next_db.execute("SELECT value FROM worker_poll_probe WHERE key='poll-test'").fetchone() is None
            with worker._connection("claim_next") as next_db:
                next_db.execute("INSERT INTO worker_poll_probe(key,value) VALUES('poll-test','committed')")
        with self.connection() as peer:
            assert peer.execute("SELECT value FROM worker_poll_probe WHERE key='poll-test'").fetchone()[0] == "committed"

    def test_heartbeat_and_handler_connections_are_not_the_polling_connection(self):
        worker = JobCoordinator(self.database)
        with worker._polling_session():
            with worker._connection("claim_next") as polling:
                polling.execute("SELECT 1")
            with worker._connection() as heartbeat:
                assert heartbeat is not polling
                heartbeat.execute("SELECT 1")
            with worker._connection("claim_next") as next_poll:
                assert next_poll is polling
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                heartbeat.execute("SELECT 1")

    def test_polling_connections_are_owned_and_closed_by_each_thread(self):
        worker = JobCoordinator(self.database)
        barrier = threading.Barrier(2)

        def in_thread():
            with worker._polling_session():
                with worker._connection("claim_next") as db:
                    db.execute("SELECT 1")
                barrier.wait(timeout=5)
                with worker._connection("claim_next") as again:
                    assert again is db
                    again.execute("SELECT 1")
                identity = id(db)
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
            return identity

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(in_thread) for _ in range(2)]
            assert first.result(timeout=10) != second.result(timeout=10)

    def test_loop_exception_closes_connection_and_restores_stop_event(self):
        worker = JobCoordinator(self.database)
        opened = []

        def failed_loop(*_args, **_kwargs):
            with worker._connection("claim_next") as db:
                db.execute("SELECT 1")
                opened.append(db)
            raise RuntimeError("stop loop")

        with patch("cuoti_app.services.jobs.run_worker_loop", side_effect=failed_loop):
            with pytest.raises(RuntimeError, match="stop loop"):
                worker.run_forever(threading.Event())
        assert worker._worker_stop_event is None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")

    def test_single_step_and_request_coordinators_still_use_short_lived_connections(self):
        worker = JobCoordinator(self.database)
        with worker._connection("claim_next") as first:
            first.execute("SELECT 1")
        with worker._connection("claim_next") as second:
            assert second is not first
        for db in [first, second]:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
