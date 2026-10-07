"""Native effect tests: no HTTP/framework double at the controller boundary."""
from __future__ import annotations

import asyncio

import pytest

from test_agentsessions_protocol import binding_file, live, text, terminal


async def collect(call):
    import grpc
    events = []
    while (event := await call.read()) is not grpc.aio.EOF:
        events.append(event)
    return events


@pytest.mark.parametrize("inputs", [[], [""], ["", "last"]])
def test_exchange_correlates_serialized_effects_and_preserves_inputs(binding_file, inputs):
    async def check():
        seen = []
        async def runner(binding, request, exchange):
            seen.append((request.config, exchange.input_count))
            for _ in range(2):
                reply = await exchange.call([text(c, "user", "question")])
                assert reply.role == "assistant"
                assert reply.parts[0].text.text == "answer"
            return None
        async with live(binding_file, runner) as (_, c, h, stub, _):
            call = stub.Connect()
            await call.write(h.ControllerFrame(execution_id="exec-1", start=h.Start(config=b"\xff\x00config", inputs=[text(c, "user", s) for s in inputs])))
            ids = []
            for _ in range(2):
                event = await asyncio.wait_for(call.read(), 3)
                assert event.kind == c.EVENT_MODEL_CALL
                assert event.model.model == "host-model"
                assert event.model.input_hash == ""  # host owns hashing
                assert dict(event.model.params) == {}
                assert [(m.role, m.parts[0].text.text) for m in event.model.messages] == [("user", "question")]
                ids.append(event.model.id)
                await call.write(h.ControllerFrame(execution_id="exec-1", model=h.ModelResult(model_call_id=event.model.id, message=text(c, "assistant", "answer"))))
            terminal(await collect(call), c, "COMPLETED")
            assert ids == ["model-1", "model-2"]
            assert seen == [(b"\xff\x00config", len(inputs))]
    asyncio.run(check())


@pytest.mark.parametrize("control", ["wrong-call", "missing-message", "tool-role", "data", "reasoning", "wrong-model", "negative-usage", "empty-parts", "oversized-result", "wrong-execution", "wrong-session", "duplicate", "tool", "unknown", "cancel", "eof"])
def test_pending_effect_reader_fails_closed_and_releases(binding_file, control):
    async def check():
        cleaned = asyncio.Event()
        calls = 0
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unhandled.append(ctx))
        async def runner(binding, request, exchange):
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    await exchange.call([text(c, "user", "q")])
                    await asyncio.Future()
                finally:
                    cleaned.set()
            return None
        async with live(binding_file, runner) as (_, c, h, stub, _):
            call = stub.Connect()
            await call.write(h.ControllerFrame(execution_id="exec-1", start=h.Start()))
            event = await asyncio.wait_for(call.read(), 3)
            assert event.kind == c.EVENT_MODEL_CALL
            frame = h.ControllerFrame(execution_id="exec-1", model=h.ModelResult(model_call_id=event.model.id, message=text(c, "assistant", "answer")))
            if control == "wrong-call": frame.model.model_call_id = "wrong"
            elif control == "missing-message": frame.model.ClearField("message")
            elif control == "tool-role": frame.model.message.role = "tool"
            elif control in {"data", "reasoning"}:
                part = c.Part(data=c.DataPart()) if control == "data" else c.Part(reasoning=c.ReasoningPart(opaque_bytes=b"secret"))
                frame.model.message.parts[0].CopyFrom(part)
            elif control == "wrong-model": frame.model.usage.model = "other-model"
            elif control == "negative-usage": frame.model.usage.input_tokens = -1
            elif control == "empty-parts": frame.model.message.ClearField("parts")
            elif control == "oversized-result": frame.model.message.CopyFrom(text(c, "assistant", "x" * (1024 * 1024)))
            elif control == "wrong-execution": frame.execution_id = "other"
            elif control == "wrong-session": frame.session = "other"
            elif control == "duplicate": await call.write(frame)
            elif control == "tool": frame.tool.CopyFrom(c.ToolResult())
            elif control == "unknown": frame.ClearField("model")
            elif control == "cancel": frame.cancel.CopyFrom(h.Cancel())
            if control == "eof": await call.done_writing()
            else: await call.write(frame)
            events = await asyncio.wait_for(collect(call), 3)
            state = "CANCELED" if control in {"cancel", "eof"} else "FAILED"
            code = 1 if state == "CANCELED" else (12 if control == "tool" else 3)
            terminal(events, c, state, code)
            assert cleaned.is_set()
            assert "secret" not in str(events)
            next_call = stub.Connect()
            await next_call.write(h.ControllerFrame(execution_id="exec-1", start=h.Start()))
            terminal(await collect(next_call), c, "COMPLETED")
            assert unhandled == []
    asyncio.run(check())


@pytest.mark.parametrize("params,want", [({}, "COMPLETED"), ({"tools": "[]"}, "FAILED")])
def test_history_model_metadata_allows_system_text_but_not_options(binding_file, params, want):
    async def check():
        entered = []
        async def runner(binding, request, exchange):
            entered.append(request.history)
            return None
        async with live(binding_file, runner) as (_, c, h, stub, _):
            history = [c.Event(kind=c.EVENT_MODEL_CALL, model=c.ModelCall(model="host-model", id="old", params=params, messages=[text(c, "user" if params else "system", "baked rules")]))]
            call = stub.Connect()
            await call.write(h.ControllerFrame(execution_id="exec-1", start=h.Start(history=history)))
            terminal(await collect(call), c, want, 0 if want == "COMPLETED" else 12)
            assert entered == ([()] if want == "COMPLETED" else [])
    asyncio.run(check())


def test_exchange_rejects_unsupported_or_oversized_requests_before_effect(binding_file):
    async def check():
        async def runner(binding, request, exchange):
            for messages in ([text(c, "tool", "q")], [c.Message(role="user", parts=[c.Part(data=c.DataPart())])], [text(c, "user", "x" * (4 * 1024 * 1024))]):
                with pytest.raises(ValueError):
                    await exchange.call(messages)
            return None
        async with live(binding_file, runner) as (_, c, h, stub, _):
            call = stub.Connect()
            await call.write(h.ControllerFrame(execution_id="exec-1", start=h.Start()))
            terminal(await collect(call), c, "COMPLETED")
    asyncio.run(check())
