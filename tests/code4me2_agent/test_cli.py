from unittest.mock import patch

from code4me2_agent.cli import _resolve_agent_command


def test_resolve_agent_command_checks_the_full_console_script_name():
    with patch("code4me2_agent.cli.sys.argv", ["unavailable-launcher"]), patch(
        "code4me2_agent.cli.shutil.which",
        side_effect=lambda command: "/tools/code4me2-agent"
        if command == "code4me2-agent"
        else None,
    ) as which:
        assert _resolve_agent_command() == "/tools/code4me2-agent"

    assert [call.args[0] for call in which.call_args_list] == [
        "unavailable-launcher",
        "code4me2-agent",
    ]
