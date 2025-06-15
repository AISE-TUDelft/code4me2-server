import unittest
from unittest.mock import MagicMock, patch

from backend.email_utils import send_verification_email  # adjust import as needed


class TestSendVerificationEmail(unittest.TestCase):
    def setUp(self):
        # Patch App.get_instance().get_config() to return a mock config object
        patcher = patch("App.App.get_instance")
        self.mock_get_instance = patcher.start()
        self.addCleanup(patcher.stop)

        self.mock_config = MagicMock()
        self.mock_config.email_from = "from@example.com"
        self.mock_config.email_host = "smtp.example.com"
        self.mock_config.email_port = 587
        self.mock_config.email_use_tls = True
        self.mock_config.email_username = "user"
        self.mock_config.email_password = "pass"
        self.mock_config.verification_url = "https://example.com/verify"

        instance = MagicMock()
        instance.get_config.return_value = self.mock_config
        self.mock_get_instance.return_value = instance

    @patch("backend.email_utils.smtplib.SMTP")
    def test_send_email_success(self, mock_smtp_class):
        # Mock the SMTP server instance methods
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Call the function, should return True on success
        result = send_verification_email("test@example.com", "Tester", "token123")

        self.assertTrue(result)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("user", "pass")
        mock_smtp.sendmail.assert_called_once()
        mock_smtp.quit.assert_called_once()

    @patch("backend.email_utils.smtplib.SMTP")
    def test_send_email_no_tls(self, mock_smtp_class):
        # Disable TLS in config
        self.mock_config.email_use_tls = False

        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp

        result = send_verification_email("test2@example.com", "Tester2", "token456")
        self.assertTrue(result)
        mock_smtp.starttls.assert_not_called()  # No TLS should be called

    @patch("backend.email_utils.smtplib.SMTP")
    def test_send_email_failure(self, mock_smtp_class):
        # Simulate exception on SMTP constructor to test exception branch
        mock_smtp_class.side_effect = Exception("SMTP server error")

        result = send_verification_email("fail@example.com", "FailUser", "tokenfail")
        self.assertFalse(result)

    @patch("backend.email_utils.smtplib.SMTP")
    def test_send_email_failure_during_send(self, mock_smtp_class):
        # Mock SMTP instance but raise on sendmail
        mock_smtp = MagicMock()
        mock_smtp.sendmail.side_effect = Exception("Send failed")
        mock_smtp_class.return_value = mock_smtp

        result = send_verification_email("fail2@example.com", "FailUser2", "tokenfail2")
        self.assertFalse(result)
        mock_smtp.quit.assert_not_called()  # since sendmail failed, quit may or may not be called


if __name__ == "__main__":
    unittest.main()
