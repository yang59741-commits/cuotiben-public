import json
import sqlite3

import pytest

from tools import release_bundle as release
from tests.test_precise_backups import repository, release_inputs


@pytest.fixture
def guarded(release_inputs):
    args = release_inputs
    args['rollback_mode'] = 'catalog-guarded'
    payload = json.loads(args['acceptance'].read_text())
    payload.pop('executable_only_rollback')
    payload['catalog_guarded_rollback'] = {
        'no_release_data_migration': True, 'previous_compatible_without_new_codes': True,
        'guard_rehearsal_passed': True, 'new_method_codes': ['telescoping']}
    args['acceptance'].write_text(json.dumps(payload))
    data = args['root'] / 'data'
    data.mkdir()
    with sqlite3.connect(data / 'cuoti.db') as db:
        for table, column in [('mistake_classifications', 'method_annotations_json'), ('classification_suggestions', 'payload_json'),
                              ('daily_review_items', 'snapshot_json'), ('daily_review_rounds', 'selection_json'),
                              ('job_inputs', 'payload_json'), ('background_jobs', 'result_json'), ('job_business_results', 'result_json')]:
            db.execute(f'CREATE TABLE {table}({column} TEXT)')
        db.execute('PRAGMA user_version=65')
    return args


def test_guarded_publish_preserves_existing_release_checks(guarded):
    args = guarded
    release.prepare_release_bundle(**args)
    verified = release.verify_release_bundle(args['root'], args['output'])
    assert verified['snapshot'] is None
    assert verified['bundle']['rollback']['mode'] == 'catalog-guarded'
    release.publish_release_bundle(args['root'], args['output'], expected_bundle_sha256=verified['bundle_sha256'],
                                   expected_candidate=args['candidate'], expected_provenance=args['provenance'],
                                   expected_product_version=args['product_version'])
    assert args['formal_executable'].read_bytes() == b'candidate'


@pytest.mark.parametrize('table,column,value', [
    ('mistake_classifications', 'method_annotations_json', [{'code': 'telescoping', 'role': 'primary'}]),
    ('classification_suggestions', 'payload_json', {'method_annotations': [{'code': 'telescoping'}]}),
    ('daily_review_items', 'snapshot_json', {'classification': {'method_annotations': [{'code': 'telescoping'}]}}),
    ('daily_review_rounds', 'selection_json', {'method_code': 'telescoping'}),
    ('job_inputs', 'payload_json', {'selection': {'method_code': 'telescoping'}}),
])
def test_guard_rejects_new_references_without_changing_data(guarded, table, column, value):
    database = guarded['root'] / 'data/cuoti.db'
    with sqlite3.connect(database) as db:
        db.execute(f'INSERT INTO {table} VALUES(?)', (json.dumps(value),))
    with pytest.raises(release.ReleaseBundleError, match='禁止仅替换旧 EXE'):
        release.prepare_release_bundle(**guarded)
    with sqlite3.connect(database) as db:
        assert json.loads(db.execute(f'SELECT {column} FROM {table}').fetchone()[0]) == value


def test_guard_rechecks_before_publication(guarded):
    args = guarded
    release.prepare_release_bundle(**args)
    verified = release.verify_release_bundle(args['root'], args['output'])
    with sqlite3.connect(args['root'] / 'data/cuoti.db') as db:
        db.execute('INSERT INTO job_business_results VALUES(?)', (json.dumps({'method_code': 'telescoping'}),))
    with pytest.raises(release.ReleaseBundleError, match='禁止仅替换旧 EXE'):
        release.publish_release_bundle(args['root'], args['output'], expected_bundle_sha256=verified['bundle_sha256'],
                                       expected_candidate=args['candidate'], expected_provenance=args['provenance'],
                                       expected_product_version=args['product_version'])
    assert args['formal_executable'].read_bytes() == b'previous'


def test_catalog_guard_does_not_allow_schema_change(guarded):
    guarded.update(schema_after=66, candidate_max_schema=66)
    with pytest.raises(release.ReleaseBundleError, match='同 schema'):
        release.prepare_release_bundle(**guarded)
