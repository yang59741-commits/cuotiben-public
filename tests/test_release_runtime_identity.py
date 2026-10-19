"""A matching application label must not select another database."""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from tools import run_q1c2_release_app as runner


@pytest.fixture(autouse=True)
def restore_process_temp():
    with patch.dict(os.environ), patch.object(tempfile, "tempdir", tempfile.tempdir):
        yield


@pytest.fixture(autouse=True)
def no_native_observer_for_mocked_startup():
    # These tests replace the service itself; real observer coverage uses child processes.
    with patch.object(runner, 'diagnostic_session'):
        yield


def _args(tmp_path):
    database = tmp_path / 'requested.db'
    database.touch()
    return runner.build_argument_parser().parse_args([
        '--database', str(database), '--runtime-root', str(tmp_path / 'runtime'),
        '--no-browser',
    ])


def _response(identity=None):
    payload = {'contract_version': '1', 'overall_status': 'healthy',
               'service_instance_id': runner.RELEASE_INSTANCE_ID}
    if identity is not None:
        payload['runtime_identity'] = identity
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


def test_legacy_service_cannot_be_reused_without_database_identity(tmp_path):
    args = _args(tmp_path)
    with patch.object(runner.socket, 'create_connection'), patch.object(
        runner.urllib.request, 'urlopen', return_value=_response()
    ), patch.object(runner, '_run_locked') as start:
        with pytest.raises(runner.ReleaseAppStartupError, match='release_identity_unavailable'):
            runner.run(args)
    start.assert_not_called()


def test_another_database_is_occupied_and_requested_database_starts(tmp_path):
    args = _args(tmp_path)
    identity = {'database': str(tmp_path / 'other.db'), 'runtime_root': str(tmp_path / 'other-runtime')}
    def connect(address, **_kwargs):
        if address[1] != args.port:
            raise OSError('free')
        return MagicMock()
    with patch.object(runner.socket, 'create_connection', side_effect=connect), patch.object(
        runner.urllib.request, 'urlopen', return_value=_response(identity)
    ), patch.object(runner, '_run_locked', return_value=0) as start:
        assert runner.run(args) == 0
    start.assert_called_once_with(args, args.port + 1, f'http://127.0.0.1:{args.port + 1}')


def test_same_database_with_other_runtime_is_not_reused(tmp_path):
    args = _args(tmp_path)
    identity = {'database': str(args.database), 'runtime_root': str(tmp_path / 'other-runtime')}
    with patch.object(runner.socket, 'create_connection'), patch.object(
        runner.urllib.request, 'urlopen', return_value=_response(identity)
    ), patch.object(runner, '_run_locked') as start:
        with pytest.raises(runner.ReleaseAppStartupError, match='release_runtime_mismatch'):
            runner.run(args)
    start.assert_not_called()


def test_same_resolved_layout_reuses_without_initialization(tmp_path):
    args = _args(tmp_path)
    identity = runner.runtime_identity(args.database, args.runtime_root)
    identity['database'] = str(tmp_path / '.' / 'requested.db')
    with patch.object(runner.socket, 'create_connection'), patch.object(
        runner.urllib.request, 'urlopen', return_value=_response(identity)
    ), patch.object(runner, '_run_locked') as start:
        assert runner.run(args) == 0
    start.assert_not_called()


def test_missing_target_is_rejected_before_probing_other_services(tmp_path):
    args = _args(tmp_path)
    args.database = tmp_path / 'missing.db'
    with patch.object(runner, '_select_release_port') as probe:
        with pytest.raises(runner.ReleaseAppStartupError, match='formal_database_missing'):
            runner.run(args)
    probe.assert_not_called()


def test_candidate_server_port_does_not_change_desktop_default():
    import launcher
    with patch.object(launcher, 'run_server_mode', return_value=0) as run:
        assert launcher.main(['--server', '--port', '18765']) == 0
        run.assert_called_once_with(port=18765)
    with patch.object(launcher, 'run_server_mode', return_value=0) as run:
        assert launcher.main(['--server']) == 0
        run.assert_called_once_with(port=8080)
    with pytest.raises(SystemExit):
        launcher.main(['--server', '--port', '0'])


def test_health_reports_the_explicit_layout_without_changing_contract(tmp_path):
    from flask import Flask
    from cuoti_app.routes import health
    app = Flask(__name__)
    app.config.update(DATABASE=str(tmp_path / 'data.db'), RUNTIME_ROOT=str(tmp_path / 'runtime'))
    app.register_blueprint(health.bp)
    with patch.object(health, 'collect_runtime_health', return_value={
        'contract_version': '1', 'overall_status': 'healthy', 'checks': [],
    }):
        response = app.test_client().get('/api/v2/health')
    assert response.status_code == 200
    assert response.json['contract_version'] == '1'
    assert response.json['runtime_identity'] == runner.runtime_identity(app.config['DATABASE'], app.config['RUNTIME_ROOT'])
    assert response.headers['Cache-Control'] == 'no-store'
