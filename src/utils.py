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


def extract_secrets(text: str, file_name: str = "unknown_file") -> str:
    """
    Redact secrets from the given text using various detectors.
    """
    secrets = set()
    for detector in secret_detectors:
        findings = detector.analyze_line(
            filename=file_name,
            line=text,
        )
        if findings:
            for finding in findings:
                if finding.secret_value:
                    secrets.add(finding.secret_value)
    return secrets


def redact_secrets(text: str, secrets: list[str]) -> str:
    """
    Redact secrets in the given text using various detectors.
    """
    pattern = re.compile("|".join(re.escape(secret) for secret in secrets))
    return pattern.sub("<hidden>", text)


def create_uuid(version: int = 4) -> str:
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


#
# if __name__ == "__main__":
#     file_content = "secret_key = 'AKIAIOSFODNN7EXAMPLE'\npassword= 'mypassword123'\n"+ "normal_code = 1\n"*300
#
#     redacted_file_content = ""
#     attempts = 100
#
#     # Approach 1 -> Slower takes about 40 milliseconds on average per 100 lines of code
#     sum_time1 = 0
#     for _ in range(attempts):
#         t0 = time.perf_counter()
#         redacted_file_content = ""
#         for line_number, line in enumerate(file_content.split("\n"), start=1):
#             secrets = extract_secrets(text=line, file_name="dummy.py", line_number=line_number)
#             for secret in secrets:
#                 line = re.sub(re.escape(secret), "<hidden>", line)
#             redacted_file_content+=line+"\n"
#         sum_time1+=time.perf_counter()-t0
#
#     # Approach 2 -> Much faster takes about 1 millisecond on average per 100 lines of code
#     sum_time2 = 0
#     for _ in range(attempts):
#         t0 = time.perf_counter()
#         redacted_file_content=file_content
#         secrets = extract_secrets(text=file_content, file_name="dummy.py", line_number=1)
#         for secret in secrets:
#             redacted_file_content = re.sub(re.escape(secret), "<hidden>", redacted_file_content)
#         sum_time2+=time.perf_counter()-t0
#
#     print(f"Average time approach 1 took: {sum_time1/attempts}")
#     print(f"Average time approach 2 took: {sum_time2/attempts}")
