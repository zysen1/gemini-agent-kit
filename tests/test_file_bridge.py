"""Offline relay fixtures. These never invoke a live model or prove client integration."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import file_bridge as bridge


class FakeGateway:
    key = 'test-fixture-secret-never-real'
    model = 'gemini-fixture'
    base_url = 'http://127.0.0.1:8045/v1'

    def __init__(self, delay=0):
        self.calls = []
        self.delay = delay

    def call(self, document):
        self.calls.append(document)
        if self.delay:
            return {'success': False, 'http_status': 429, 'error': 'HTTP_ERROR', 'retry_after_seconds': self.delay}, self.delay
        return {'success': True, 'text': 'fixture reply', 'provider_reported_model': self.model,
                'route_headers': {'X-Mapped-Model': self.model, 'X-Upstream-Model': self.model}}, 0


@unittest.skipUnless(os.name == "posix", "File relay needs macOS/Linux directory descriptors")
class FileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = Path(self.temp.name) / 'relay'
        self.gateway = FakeGateway()
        self.worker = bridge.Worker(self.queue, self.gateway, min_interval=0, stable_seconds=0)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, request_id='demo1', **changes):
        obj = {'id': request_id, 'prompt': 'Write a short fictional scene.', 'max_tokens': 200}
        obj.update(changes)
        path = self.queue / 'requests' / (request_id + '.json')
        path.write_text(json.dumps(obj), encoding='utf8')
        return path

    def step(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.worker.step(immediate=True)

    def result(self, request_id='demo1'):
        return json.loads((self.queue / 'results' / (request_id + '.json')).read_text())

    def test_complete_call_and_immutable_result(self):
        self.submit()
        self.assertTrue(self.step())
        result = self.result()
        self.assertTrue(result['success'])
        self.assertEqual(result['inference_request_count'], 1)
        self.assertEqual(result['provider_reported_model'], 'gemini-fixture')
        self.assertFalse(result['model_identity_independently_verified'])
        self.submit(prompt='A changed prompt under the same id.')
        self.assertFalse(self.step())
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.result(), result)

    def test_schema_rejects_url_and_model_override_before_inference(self):
        for i, field in enumerate(('url', 'model', 'shell', 'output_path', 'api_key')):
            name = 'override' + str(i)
            self.submit(name, **{field: 'not allowed'})
            self.assertTrue(self.step())
            self.assertEqual(self.result(name)['error'], 'INVALID_REQUEST')
        self.assertEqual(self.gateway.calls, [])

    def test_schema_bounds_and_credential_rejection(self):
        cases = ({'max_tokens': True}, {'max_tokens': 4097}, {'max_tokens': 0},
                 {'prompt': 'x' * 16001}, {'prompt': ''},
                 {'prompt': self.gateway.key}, {'prompt': '1//abcdefghijklmno'})
        for i, changes in enumerate(cases):
            name = 'bounds' + str(i)
            self.submit(name, **changes)
            self.step()
            self.assertFalse(self.result(name)['success'])
        self.assertEqual(self.gateway.calls, [])

    def test_filename_id_must_match(self):
        self.submit(id='different')
        self.step()
        self.assertEqual(self.result()['inference_request_count'], 0)

    def test_symlink_input_not_read(self):
        target = Path(self.temp.name) / 'private.txt'
        target.write_text('do not read or echo this secret', encoding='utf8')
        try:
            (self.queue / 'requests' / 'linked.json').symlink_to(target)
        except OSError:
            self.skipTest('Symbolic links unavailable on this platform')
        self.step()
        self.assertEqual(self.result('linked')['inference_request_count'], 0)
        self.assertNotIn('do not read', json.dumps(self.result('linked')))
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(target.read_text(), 'do not read or echo this secret')

    def test_partial_json_waits_then_complete_is_processed(self):
        path = self.queue / 'requests' / 'demo1.json'
        path.write_text('{"id":', encoding='utf8')
        self.assertFalse(self.step())
        self.assertEqual(self.gateway.calls, [])
        self.submit()
        self.assertTrue(self.step())

    def test_oversize_request_rejected_without_call(self):
        path = self.queue / 'requests' / 'huge.json'
        path.write_bytes(b'x' * (bridge.MAX_REQUEST_BYTES + 1))
        self.step()
        self.assertEqual(self.result('huge')['inference_request_count'], 0)

    def test_cooldown_blocks_next_request_without_retrying(self):
        self.worker.gateway = self.gateway = FakeGateway(delay=60)
        self.submit('a'); self.submit('b')
        self.assertTrue(self.step())
        self.assertFalse(self.step())
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertTrue((self.queue / 'requests' / 'b.json').exists())
        self.assertEqual(self.result('a')['automatic_retries'], 0)
        self.assertEqual(self.result('a')['retry_after_seconds'], 60)

    def test_receipt_is_outside_shared_queue_and_matches_result(self):
        self.submit(); self.step()
        result = self.result()
        receipt = json.loads((self.queue.parent / (self.queue.name + '-private-receipts') / 'demo1.json').read_text())
        import hashlib
        digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest, receipt['result_canonical_sha256'])
        self.assertEqual(receipt['inference_request_count'], 1)
        self.assertEqual(receipt['receipt_id'], result['receipt_id'])

    def test_directory_replacement_stops_worker(self):
        self.submit()
        original = self.queue / 'requests'
        original.rename(self.queue / 'requests-old')
        other = Path(self.temp.name) / 'elsewhere'; other.mkdir()
        original.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.step()
        self.assertEqual(self.gateway.calls, [])

    def test_precreated_fake_result_has_no_private_receipt(self):
        self.submit()
        (self.queue / 'results' / 'demo1.json').write_text('{"success":true}')
        self.assertFalse(self.step())
        self.assertFalse((self.queue.parent / (self.queue.name + '-private-receipts') / 'demo1.json').exists())
        self.assertEqual(self.gateway.calls, [])

    def test_shared_files_cannot_reset_private_replay_protection(self):
        self.submit(); self.step()
        (self.queue / 'results' / 'demo1.json').unlink()
        (self.queue / 'processed' / 'demo1.json').unlink()
        self.submit()
        restarted = bridge.Worker(self.queue, self.gateway, min_interval=0, stable_seconds=0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(restarted.step(immediate=True))
        self.assertEqual(len(self.gateway.calls), 1)

    def test_google_retryinfo_delay_parsed(self):
        self.assertEqual(bridge.cooldown_seconds({}, '{"error":{"details":[{"retryDelay":"120.5s"}]}}'), 120.5)

    def test_null_message_returns_failure_instead_of_crashing(self):
        gateway = bridge.HTTPGateway(self.gateway.base_url, 'gemini-fixture', self.gateway.key)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        response.read.return_value = b'{"choices":[{"message":null}]}'
        gateway.opener.open = mock.Mock(return_value=response)
        result, delay = gateway.call({'prompt':'test', 'max_tokens':10})
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'NO_TEXT_RESPONSE')

    def test_200_error_preserves_retryinfo(self):
        gateway = bridge.HTTPGateway(self.gateway.base_url, 'gemini-fixture', self.gateway.key)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        response.read.return_value = b'{"error":{"code":429,"details":[{"retryDelay":"120s"}]}}'
        gateway.opener.open = mock.Mock(return_value=response)
        result, delay = gateway.call({'prompt':'test', 'max_tokens':10})
        self.assertFalse(result['success'])
        self.assertEqual(delay, 120)

    def test_cooldown_survives_restart(self):
        self.worker.gateway = self.gateway = FakeGateway(delay=60)
        self.submit('a'); self.submit('b'); self.step()
        restarted = bridge.Worker(self.queue, self.gateway, min_interval=0, stable_seconds=0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(restarted.step(immediate=True))
        self.assertEqual(len(self.gateway.calls), 1)

    def test_no_clobber_publishing(self):
        target = self.queue / 'results' / 'test.json'
        bridge.exclusive_json(target, {'original': True})
        with self.assertRaises(FileExistsError):
            bridge.exclusive_json(target, {'original': False})
        self.assertEqual(json.loads(target.read_text()), {'original': True})

    def test_redirects_disabled_and_credentials_redacted(self):
        gateway = bridge.HTTPGateway(self.gateway.base_url, 'gemini-fixture', self.gateway.key)
        error = urllib.error.HTTPError(self.gateway.base_url, 503, 'Unavailable', {'Retry-After': '50'},
                                      io.BytesIO(('Cooldown wait 60s. Bearer ' + self.gateway.key).encode()))
        gateway.opener.open = mock.Mock(side_effect=error)
        result, delay = gateway.call({'prompt': 'test', 'max_tokens': 10})
        self.assertEqual(delay, 60)
        self.assertNotIn(self.gateway.key, json.dumps(result))
        self.assertTrue(any(isinstance(handler, bridge.NoRedirect) for handler in gateway.opener.handlers))

    def test_missing_key_and_nonlocal_gateway_rejected(self):
        with self.assertRaises(ValueError):
            bridge.HTTPGateway(self.gateway.base_url, 'gemini-fixture', '')
        with self.assertRaises(ValueError):
            bridge.HTTPGateway('https://remote.example/v1', 'gemini-fixture', 'fixture')

    def test_nested_secret_redaction(self):
        value = bridge.scrub({'api_key': 'other', 'text': '1//abcdefghijklmno ' + self.gateway.key}, self.gateway.key)
        self.assertNotIn(self.gateway.key, json.dumps(value))
        self.assertNotIn('1//', json.dumps(value))
        self.assertEqual(value['api_key'], '[REDACTED]')


if __name__ == '__main__':
    unittest.main()
