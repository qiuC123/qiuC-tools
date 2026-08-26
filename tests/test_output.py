"""Tests for structured output and expected errors."""

from io import StringIO

from wxcli.errors import ERROR_EXIT_CODES, ErrorCode, ExitCode, InputError, WxcliError
from wxcli.output import Output


def test_json_output_is_one_utf8_json_document() -> None:
    stdout = StringIO()
    output = Output(json_mode=True, stdout=stdout, stderr=StringIO())

    output.success({"title": "中文"})

    assert stdout.getvalue() == '{"ok":true,"data":{"title":"中文"}}\n'


def test_json_error_contains_safe_machine_code() -> None:
    stdout = StringIO()
    output = Output(json_mode=True, stdout=stdout, stderr=StringIO())

    output.error(InputError("缺少 URL", field="url"))

    assert stdout.getvalue() == (
        '{"ok":false,"error":{"code":"INVALID_ARGUMENT",'
        '"message":"缺少 URL","details":{"field":"url"}}}\n'
    )


def test_text_error_goes_to_standard_error() -> None:
    stderr = StringIO()
    output = Output(json_mode=False, stdout=StringIO(), stderr=stderr)

    output.error(InputError("missing URL"))

    assert stderr.getvalue() == f"{ErrorCode.INVALID_ARGUMENT}: missing URL\n"


def test_each_error_code_has_its_contractual_exit_code() -> None:
    assert ERROR_EXIT_CODES == {
        ErrorCode.GENERAL_ERROR: ExitCode.GENERAL,
        ErrorCode.INVALID_ARGUMENT: ExitCode.INPUT,
        ErrorCode.VALIDATION_ERROR: ExitCode.VALIDATION,
        ErrorCode.NOT_FOUND: ExitCode.NOT_FOUND,
        ErrorCode.NETWORK_ERROR: ExitCode.NETWORK,
        ErrorCode.AUTHENTICATION_ERROR: ExitCode.AUTHENTICATION,
        ErrorCode.CHROME_ERROR: ExitCode.CHROME,
        ErrorCode.PARSING_ERROR: ExitCode.PARSING,
        ErrorCode.LOCAL_CONFIGURATION_ERROR: ExitCode.LOCAL_CONFIGURATION,
        ErrorCode.VERIFICATION_REQUIRED: ExitCode.AUTHENTICATION,
    }
    assert WxcliError(ErrorCode.CHROME_ERROR, "Chrome failed").exit_code == ExitCode.CHROME
