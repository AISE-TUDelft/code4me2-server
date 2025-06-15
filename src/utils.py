"""
Secret Detection and Redaction Utilities

This module provides utilities to extract and redact secrets from text using
a variety of detectors from the `detect-secrets` library.
"""

import re
import uuid

from detect_secrets.plugins.artifactory import ArtifactoryDetector
from detect_secrets.plugins.aws import AWSKeyDetector
from detect_secrets.plugins.azure_storage_key import AzureStorageKeyDetector
from detect_secrets.plugins.basic_auth import BasicAuthDetector
from detect_secrets.plugins.discord import DiscordBotTokenDetector
from detect_secrets.plugins.github_token import GitHubTokenDetector
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HexHighEntropyString,
)
from detect_secrets.plugins.jwt import JwtTokenDetector
from detect_secrets.plugins.keyword import KeywordDetector
from detect_secrets.plugins.openai import OpenAIDetector
from detect_secrets.plugins.private_key import PrivateKeyDetector
from detect_secrets.plugins.slack import SlackDetector
from detect_secrets.plugins.stripe import StripeDetector
from detect_secrets.plugins.telegram_token import TelegramBotTokenDetector
from detect_secrets.plugins.twilio import TwilioKeyDetector

# List of initialized secret detectors
secret_detectors = [
    AWSKeyDetector(),
    AzureStorageKeyDetector(),
    GitHubTokenDetector(),
    SlackDetector(),
    DiscordBotTokenDetector(),
    TelegramBotTokenDetector(),
    StripeDetector(),
    TwilioKeyDetector(),
    OpenAIDetector(),
    PrivateKeyDetector(),
    Base64HighEntropyString(),
    HexHighEntropyString(),
    JwtTokenDetector(),
    BasicAuthDetector(),
    KeywordDetector(),
    ArtifactoryDetector(),
]


def extract_secrets(text: str, file_name: str = "unknown_file") -> set[str]:
    """
    Extract secrets from the given text using all enabled detectors.

    Args:
        text (str): The text to analyze for secrets.
        file_name (str, optional): The name of the file being analyzed. Defaults to "unknown_file".

    Returns:
        set[str]: A set of detected secret values.
    """
    secrets = set()

    for detector in secret_detectors:
        findings = detector.analyze_line(
            filename=file_name,
            line=text,
        )
        for finding in findings or []:
            if finding.secret_value:
                secrets.add(finding.secret_value)

    return secrets


def redact_secrets(text: str, secrets: list[str]) -> str:
    """
    Redact all detected secrets in the given text.

    Args:
        text (str): The input text containing secrets.
        secrets (list[str]): A list of secret values to redact.

    Returns:
        str: The redacted text with secrets replaced by "<hidden>".
    """
    if not secrets:
        return text

    # Compile a single regex pattern to match all secrets
    pattern = re.compile("|".join(re.escape(secret) for secret in secrets))
    return pattern.sub("<hidden>", text)


def create_uuid(version: int = 4) -> str:
    """
    Generate a UUID of a specified version.

    Args:
        version (int, optional): UUID version to generate. Must be 1, 3, 4, or 5. Defaults to 4.

    Returns:
        str: The generated UUID as a string.

    Raises:
        ValueError: If an unsupported version is specified.
    """
    if version == 1:
        return str(uuid.uuid1())
    elif version == 3:
        return str(uuid.uuid3(uuid.NAMESPACE_DNS, "example.com"))
    elif version == 4:
        return str(uuid.uuid4())
    elif version == 5:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, "example.com"))
    else:
        raise ValueError("Invalid UUID version. Supported versions are 1, 3, 4, and 5.")
