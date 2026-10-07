"""Native agentsessions Harness SPI with controller-mediated model effects."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable

import grpc

from ..adapter_support import _wait_for_owner_task
from ..conversation import RunRequest
from ..runtime import RunResult
from ._generated import common_pb2 as common
from ._generated import harness_pb2 as harness
from ._generated import harness_pb2_grpc
from .binding import VerifiedAgentsessionsBinding
from .exchange import ExecutionExchange, MAX_MESSAGE_BYTES
from .history import UnsupportedContent as _Unsupported, run_request as _request

_LOG = logging.getLogger(__name__)
# A fresh, explicitly supplied async execution hook. It owns any per-Start
# resources and must close them under cancellation. Never use build_runtime.
ExecutionRunner = Callable[
    [VerifiedAgentsessionsBinding, RunRequest, ExecutionExchange], Awaitable[RunResult | None]
]


async def _settle(tasks: list[asyncio.Task]) -> bool:
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return any(
        isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        for result in results
    )


async def _invoke_runner(
    runner: ExecutionRunner, binding: VerifiedAgentsessionsBinding, request: RunRequest,
    exchange: ExecutionExchange,
) -> RunResult | None:
    # Invocation also belongs inside the task: callbacks may return any
    # Awaitable or raise synchronously before returning one.
    return await runner(binding, request, exchange)


def _end(state: str, code: grpc.StatusCode | None = None, description: str = "") -> common.HarnessEnd:
    end = common.HarnessEnd(state=state)
    if code is not None:
        end.error.CopyFrom(common.Error(code=code.value[0], description=description))
    return end


def _event(execution_id: str, **kwargs) -> common.Event:
    return common.Event(execution_id=execution_id, schema_version=1, **kwargs)


class HarnessService(harness_pb2_grpc.HarnessServicer):
    """One admitted execution per service, with a reader independent of the runner."""

    def __init__(
        self,
        binding: VerifiedAgentsessionsBinding,
        *,
        runner: ExecutionRunner | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.binding = binding
        self.runner = runner
        self.auth_token = auth_token
        self._active: object | None = None

    async def _authenticate(self, context: grpc.aio.ServicerContext) -> None:
        if self.auth_token is None:
            return
        values = [value for key, value in context.invocation_metadata() if key == "authorization"]
        expected = ("Bearer " + self.auth_token).encode("utf-8")
        if len(values) != 1 or not secrets.compare_digest(values[0].encode("utf-8"), expected):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "authentication required")

    async def Describe(
        self, request: harness.DescribeRequest, context: grpc.aio.ServicerContext
    ) -> harness.HarnessDescriptor:
        await self._authenticate(context)
        return harness.HarnessDescriptor(
            id=self.binding.descriptor_id,
            models=[self.binding.spec.model.name],
            capabilities=harness.Capabilities(
                resumability=harness.RESUMABILITY_STATELESS_REPLAY,
                fork_safe=True,
                requires_gpu=False,
                streaming=False,
                reasoning_replay=False,
            ),
        )

    async def _read_controls(
        self,
        frames: AsyncIterator[harness.ControllerFrame],
        first: harness.ControllerFrame,
        exchange: ExecutionExchange,
    ) -> common.HarnessEnd:
        async for frame in frames:
            if frame.execution_id != first.execution_id or frame.session != first.session:
                return _end("FAILED", grpc.StatusCode.INVALID_ARGUMENT, "control frame identity mismatch")
            kind = frame.WhichOneof("frame")
            if kind == "cancel":
                return _end("CANCELED", grpc.StatusCode.CANCELLED, "execution canceled")
            if kind == "model":
                try:
                    exchange.accept(frame.model)
                except ValueError:
                    return _end("FAILED", grpc.StatusCode.INVALID_ARGUMENT, "invalid model result")
                continue
            if kind in {"tool", "approval"}:
                return _end("FAILED", grpc.StatusCode.UNIMPLEMENTED, "tool and approval replies are unsupported")
            return _end("FAILED", grpc.StatusCode.INVALID_ARGUMENT, "unexpected control frame")
        # A half-close means the controller can no longer service mediated effects.
        return _end("CANCELED", grpc.StatusCode.CANCELLED, "controller disconnected")

    async def Connect(
        self,
        request_iterator: AsyncIterator[harness.ControllerFrame],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[common.Event]:
        await self._authenticate(context)
        frames = request_iterator.__aiter__()
        try:
            first = await anext(frames)
        except StopAsyncIteration:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "first frame must be Start")
            return
        # Leave ample bounded space for an END carrying the exact same ID.
        if (
            first.WhichOneof("frame") != "start"
            or not first.execution_id
            or len(first.execution_id.encode("utf-8")) > 1024
        ):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Start first and execution_id of 1..1024 bytes required",
            )
            return
        execution_id = first.execution_id
        if self._active:
            yield _event(execution_id, kind=common.EVENT_END, end=_end("FAILED", grpc.StatusCode.RESOURCE_EXHAUSTED, "an execution is already active"))
            return
        # No await between checking and reserving admission on this asyncio loop.
        reservation = object()
        self._active = reservation
        reader = None
        execution = None
        outgoing = None
        exchange = ExecutionExchange(execution_id, self.binding.spec.model.name, len(first.start.inputs))
        cleanup_failed = False
        settlement = None

        async def cleanup() -> None:
            nonlocal settlement, cleanup_failed
            if settlement is None:
                exchange.close()
                tasks = [task for task in (reader, execution, outgoing) if task is not None]
                # Retrieve already-failed outcomes even when caller cancellation
                # preempts result handling; classify cleanup failures separately.
                cleanup_tasks = [task for task in tasks if not task.done() or task.cancelling()]
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.exception()
                    elif not task.done() and not task.cancelling():
                        task.cancel()
                settlement = asyncio.create_task(_settle(cleanup_tasks))
            try:
                # Reuse the settlement if cancellation sends us through finally:
                # never cancel a resource owner's teardown a second time.
                await _wait_for_owner_task(settlement)
            finally:
                if not settlement.cancelled() and settlement.result() and not cleanup_failed:
                    _LOG.error("agentsessions execution cleanup failed")
                    cleanup_failed = True
                if self._active is reservation:
                    self._active = None

        try:
            try:
                request = _request(first)
                if first.start.resume_from_seq < 0:
                    raise _Unsupported("negative resume cursor")
            except _Unsupported:
                await cleanup()
                yield _event(execution_id, kind=common.EVENT_END, end=_end("FAILED", grpc.StatusCode.UNIMPLEMENTED, "unsupported Start content"))
                return
            if self.runner is None:
                await cleanup()
                yield _event(execution_id, kind=common.EVENT_END, end=_end("FAILED", grpc.StatusCode.UNIMPLEMENTED, "agentsessions execution is not implemented"))
                return
            reader = asyncio.create_task(self._read_controls(frames, first, exchange))
            execution = asyncio.create_task(_invoke_runner(self.runner, self.binding, request, exchange))
            outgoing = asyncio.create_task(exchange.events.get())
            while True:
                await asyncio.wait((reader, execution, outgoing), return_when=asyncio.FIRST_COMPLETED)
                # Control failure wins over queued effects or concurrent completion.
                if reader.done() or execution.done():
                    break
                yield outgoing.result()
                outgoing = asyncio.create_task(exchange.events.get())
            result = None
            if reader.done():
                try:
                    end = reader.result()
                except asyncio.CancelledError:
                    end = _end("CANCELED", grpc.StatusCode.CANCELLED, "execution canceled")
                except Exception:
                    # Iterator/transport errors can contain private request data.
                    end = _end("FAILED", grpc.StatusCode.INTERNAL, "execution failed")
                if not execution.done() and not execution.cancelling():
                    execution.cancel()
                # A disconnect during awaited teardown must not cancel the
                # resource owner's finally block a second time.
                cleanup_failed = await _wait_for_owner_task(asyncio.create_task(_settle([execution])))
                if cleanup_failed:
                    end = _end("FAILED", grpc.StatusCode.INTERNAL, "execution cleanup failed")
            else:
                try:
                    result = execution.result()
                    end = _end("COMPLETED")
                except asyncio.CancelledError:
                    end = _end("CANCELED", grpc.StatusCode.CANCELLED, "execution canceled")
                except Exception:
                    # Exception text can contain prompts, Config, URLs or tokens.
                    cleanup_failed = bool(execution.cancelling())
                    description = "execution cleanup failed" if cleanup_failed else "execution failed"
                    end = _end("FAILED", grpc.StatusCode.INTERNAL, description)
            if cleanup_failed:
                _LOG.error("agentsessions execution cleanup failed")
            if result is not None:
                try:
                    output = _event(
                        execution_id,
                        kind=common.EVENT_OUTPUT,
                        message=common.Message(
                            role="assistant",
                            parts=[common.Part(text=common.TextPart(text=result.text))],
                        ),
                    )
                    oversized = output.ByteSize() > MAX_MESSAGE_BYTES
                except Exception:
                    # Protobuf text conversion can fail (e.g. lone surrogates).
                    # Keep both the RPC and its diagnostics free of raw output.
                    end = _end("FAILED", grpc.StatusCode.INTERNAL, "execution failed")
                else:
                    if oversized:
                        end = _end("FAILED", grpc.StatusCode.RESOURCE_EXHAUSTED, "output exceeds message limit")
                    else:
                        yield output
            # ClientHarness.Run returns on END and cancels without draining EOF.
            # Finish all owned cleanup and release this reservation before END.
            await cleanup()
            if cleanup_failed:
                end = _end("FAILED", grpc.StatusCode.INTERNAL, "execution cleanup failed")
            yield _event(execution_id, kind=common.EVENT_END, end=end)
        finally:
            await cleanup()


def create_server(
    binding: VerifiedAgentsessionsBinding,
    *,
    runner: ExecutionRunner | None = None,
    auth_token: str | None = None,
) -> grpc.aio.Server:
    """Create a registered, unbound server (call within its owning asyncio loop)."""
    server = grpc.aio.server(options=[
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ])
    harness_pb2_grpc.add_HarnessServicer_to_server(
        HarnessService(binding, runner=runner, auth_token=auth_token), server
    )
    return server


async def serve(
    binding: VerifiedAgentsessionsBinding,
    *,
    bind: str = "127.0.0.1",
    port: int = 8080,
    auth_token: str | None = None,
    runner: ExecutionRunner | None = None,
) -> None:
    """Serve h2c; nonloopback requires auth AND deployment-private networking."""
    bind = bind.strip().lower()
    if bind not in {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"} and not auth_token:
        raise ValueError("nonloopback agentsessions bind requires authentication")
    server = create_server(binding, runner=runner, auth_token=auth_token)
    host = f"[{bind}]" if ":" in bind else bind
    server.add_insecure_port(f"{host}:{port}")
    await server.start()
    termination = asyncio.create_task(server.wait_for_termination())
    try:
        # gRPC shares its termination future with shutdown. Canceling that
        # future would make stop() fail instead of completing server cleanup.
        await asyncio.shield(termination)
    finally:
        await _wait_for_owner_task(asyncio.create_task(server.stop(0)))
        await _wait_for_owner_task(termination)


def run(
    binding: VerifiedAgentsessionsBinding,
    *,
    bind: str = "127.0.0.1",
    port: int = 8080,
    auth_token: str | None = None,
    runner: ExecutionRunner | None = None,
) -> None:
    """Synchronous CLI entrypoint; does not receive a default RuntimeFactory."""
    asyncio.run(serve(binding, bind=bind, port=port, auth_token=auth_token, runner=runner))
