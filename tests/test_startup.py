import errno
from http.server import HTTPServer
import unittest
from unittest.mock import patch

import app
from finder.server import Server


class StartupTests(unittest.TestCase):
    def test_bind_failure_preserves_original_error_and_starts_no_download_threads(self):
        error = PermissionError(errno.EACCES, 'denied')
        with patch.object(HTTPServer, 'server_bind', side_effect=error), patch('finder.server.Downloads') as downloads:
            with self.assertRaises(PermissionError) as caught:
                Server(('127.0.0.1', 13006))
        self.assertIs(caught.exception, error)
        downloads.assert_not_called()

    def test_default_port_conflict_falls_back_to_os_selected_port(self):
        for error in (PermissionError(errno.EACCES, 'denied'), OSError(errno.EADDRINUSE, 'in use')):
            with self.subTest(error=error), patch.object(app, 'Server', side_effect=[error, 'server']) as server:
                self.assertEqual(app.start_server('127.0.0.1'), 'server')
                self.assertEqual([c.args[0] for c in server.call_args_list], [('127.0.0.1', 13006), ('127.0.0.1', 0)])

    def test_explicit_port_and_unrelated_errors_are_not_silently_changed(self):
        for port, error in ((13007, PermissionError(errno.EACCES, 'denied')), (None, OSError(errno.ENOMEM, 'memory'))):
            with self.subTest(port=port), patch.object(app, 'Server', side_effect=error) as server:
                with self.assertRaises(OSError) as caught:
                    app.start_server('127.0.0.1', port)
                self.assertIs(caught.exception, error)
                self.assertEqual(server.call_count, 1)
