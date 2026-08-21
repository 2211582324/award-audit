"""协议层（bus）的单元测试：信封往返 + 判别联合解析。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from award_audit.core.bus.commands import Command, ImportBatchCommand, ReviewRecordCommand
from award_audit.core.bus.envelope import PARSE_ERROR, JsonRpcRequest, make_error
from award_audit.core.bus.events import BatchPromotedEvent, Event

_CMD = TypeAdapter(Command)
_EVT = TypeAdapter(Event)


# 功能：验证请求信封 JSON 往返无损
# 设计：model_dump_json → model_validate_json 断言相等，锁定线格式契约
def test_request_roundtrip() -> None:
    req = JsonRpcRequest(id=7, method="cmd.ping", params={"a": 1})
    again = JsonRpcRequest.model_validate_json(req.model_dump_json())
    assert again == req and again.jsonrpc == "2.0"


# 功能：验证错误响应构造带正确码与消息
# 设计：make_error 后断言字段，覆盖 id=None（解析失败时无法回显 id）情形
def test_make_error() -> None:
    err = make_error(None, PARSE_ERROR, "bad json")
    assert err.id is None and err.error.code == PARSE_ERROR and "bad" in err.error.message


# 功能：验证命令判别联合按 type 字段解析到正确的类
# 设计：喂 review_record 的 dict，断言得到 ReviewRecordCommand 且枚举校验生效
def test_command_discriminated_union() -> None:
    cmd = _CMD.validate_python({"type": "review_record", "staging_id": 3, "decision": "打回"})
    assert isinstance(cmd, ReviewRecordCommand) and cmd.decision == "打回"
    cmd2 = _CMD.validate_python({"type": "import_batch", "folder": "x"})
    assert isinstance(cmd2, ImportBatchCommand)


# 功能：验证非法 decision 与未知 type 被协议层拒绝
# 设计：Literal 枚举外的值/未注册的 type 都应抛 ValidationError——契约边界挡脏数据
def test_command_validation_rejects() -> None:
    with pytest.raises(ValidationError):
        _CMD.validate_python({"type": "review_record", "staging_id": 3, "decision": "随便"})
    with pytest.raises(ValidationError):
        _CMD.validate_python({"type": "no_such_cmd"})


# 功能：验证事件判别联合解析
# 设计：batch_promoted dict → BatchPromotedEvent，字段完整
def test_event_discriminated_union() -> None:
    evt = _EVT.validate_python({"type": "batch_promoted", "batch_id": 1,
                                "inserted": 3, "skipped_dup": 1, "skipped_fail": 0})
    assert isinstance(evt, BatchPromotedEvent) and evt.inserted == 3
