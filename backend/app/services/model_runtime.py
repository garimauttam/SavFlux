"""Bounded per-account runtime diagnostics and local-only fallback.

Never store prompts, provider response bodies or credentials here. Stream errors after
visible output are raised rather than mixing two models into one answer.
"""

import time
from collections import OrderedDict
from langchain_core.runnables import Runnable
from app.core.tenant import current_user_id
from app.services.provider_catalog import safe_error


class EmptyModelOutput(RuntimeError):
    pass


def _require_output(value):
    if not (
        value
        if isinstance(value, str)
        else getattr(value, "content", None) or getattr(value, "tool_calls", None)
    ):
        raise EmptyModelOutput()


_EVENTS: OrderedDict = OrderedDict()
_COOLDOWN: OrderedDict = OrderedDict()


def _remember(store, key, value):
    store[key] = value
    store.move_to_end(key)
    while len(store) > 256:
        store.popitem(last=False)


def latest():
    event = _EVENTS.get(current_user_id() or "__local__")
    return event if event and time.time() - event["at"] < 3600 else None


def clear():
    tenant = current_user_id() or "__local__"
    _EVENTS.pop(tenant, None)
    for key in list(_COOLDOWN):
        if key[0] == tenant:
            del _COOLDOWN[key]


class RoutedModel(Runnable):
    def __init__(self, primary, fallback, provider, model, fallback_model):
        self.primary, self.fallback = primary, fallback
        self.provider, self.model, self.fallback_model = provider, model, fallback_model

    def bind_tools(self, tools, **kwargs):
        # Old local adapters do not implement this; the review caller switches to
        # its deterministic-tools + text-review path instead of failing the review.
        return RoutedModel(
            self.primary.bind_tools(tools, **kwargs),
            self.fallback.bind_tools(tools, **kwargs) if self.fallback else None,
            self.provider,
            self.model,
            self.fallback_model,
        )

    def _event(self, provider, model, reason=None, ok=True):
        _remember(
            _EVENTS,
            current_user_id() or "__local__",
            {
                "provider": provider,
                "model": model,
                "reason": reason,
                "ok": ok,
                "fallback": provider != self.provider,
                "at": time.time(),
            },
        )

    def _failed(self, exc):
        reason = safe_error(exc)
        code = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if code in (401, 402, 403, 429):
            _remember(
                _COOLDOWN,
                (current_user_id() or "__local__", self.provider),
                (time.monotonic() + 60, reason),
            )
        self._event(self.provider, self.model, reason, False)
        return reason

    def _cooldown(self):
        entry = _COOLDOWN.get((current_user_id() or "__local__", self.provider))
        return entry[1] if entry and entry[0] > time.monotonic() else None

    def invoke(self, input, config=None, **kwargs):
        reason = self._cooldown()
        if not reason:
            try:
                value = self.primary.invoke(input, config, **kwargs)
                _require_output(value)
                self._event(self.provider, self.model)
                return value
            except Exception as exc:
                reason = self._failed(exc)
                if not self.fallback:
                    raise RuntimeError(reason) from None
        if not self.fallback:
            raise RuntimeError(reason)
        try:
            value = self.fallback.invoke(input, config, **kwargs)
            _require_output(value)
            self._event("ollama", self.fallback_model, reason)
            return value
        except Exception as exc:
            self._event(
                "ollama",
                self.fallback_model,
                reason + " Local fallback also failed: " + safe_error(exc),
                False,
            )
            raise RuntimeError("Local fallback failed: " + safe_error(exc)) from None

    async def ainvoke(self, input, config=None, **kwargs):
        reason = self._cooldown()
        if not reason:
            try:
                value = await self.primary.ainvoke(input, config, **kwargs)
                _require_output(value)
                self._event(self.provider, self.model)
                return value
            except Exception as exc:
                reason = self._failed(exc)
                if not self.fallback:
                    raise RuntimeError(reason) from None
        if not self.fallback:
            raise RuntimeError(reason)
        try:
            value = await self.fallback.ainvoke(input, config, **kwargs)
            _require_output(value)
            self._event("ollama", self.fallback_model, reason)
            return value
        except Exception as exc:
            self._event(
                "ollama",
                self.fallback_model,
                reason + " Local fallback also failed: " + safe_error(exc),
                False,
            )
            raise RuntimeError("Local fallback failed: " + safe_error(exc)) from None

    async def astream(self, input, config=None, **kwargs):
        reason = self._cooldown()
        emitted = False
        if not reason:
            try:
                async for chunk in self.primary.astream(input, config, **kwargs):
                    # Do not switch after tool-call deltas either.
                    emitted = emitted or bool(
                        getattr(chunk, "content", None)
                        or getattr(chunk, "tool_call_chunks", None)
                    )
                    yield chunk
                if not emitted:
                    raise EmptyModelOutput()
                self._event(self.provider, self.model)
                return
            except Exception as exc:
                reason = self._failed(exc)
                if emitted or not self.fallback:
                    raise RuntimeError(reason) from None
        if not self.fallback:
            raise RuntimeError(reason)
        try:
            emitted = False
            async for chunk in self.fallback.astream(input, config, **kwargs):
                emitted = emitted or bool(
                    getattr(chunk, "content", None)
                    or getattr(chunk, "tool_call_chunks", None)
                )
                yield chunk
            if not emitted:
                raise EmptyModelOutput()
            self._event("ollama", self.fallback_model, reason)
        except Exception as exc:
            self._event(
                "ollama",
                self.fallback_model,
                reason + " Local fallback also failed: " + safe_error(exc),
                False,
            )
            raise RuntimeError("Local fallback failed: " + safe_error(exc)) from None
