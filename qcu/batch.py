"""Bounded form-action groups; reuse routing and stop on failure/uncertainty.

This is deliberately not a script interpreter or a transaction: completed
actions stay completed. A navigation/click/key action can only end a group.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from typing import Any

from qcu.common.types import ACTION_TYPES, Action
from qcu.common.verification import validate_condition


def parse_actions(value: str) -> list[Action]:
    raw = json.loads(value)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise ValueError('batch requires a JSON array of 1..32 actions')
    actions = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get('type'), str):
            raise ValueError(f'action {index} needs a string type')
        if item['type'] not in ACTION_TYPES:
            raise ValueError(f'unsupported batch action {item["type"]!r}')
        params = item.get('params', {})
        if not isinstance(params, dict):
            raise ValueError(f'action {index} params must be an object')
        if any(key in params and not isinstance(params[key], str) for key in ('ref', 'ref_from', 'ref_to')):
            raise ValueError(f'action {index} refs must be strings')
        if index < len(raw)-1 and item['type'] != 'fill':
            raise ValueError(f'action {index}: only fills may precede the final action; observe after a UI transition')
        if item['type'] == 'fill' and (not isinstance(params.get('ref'), str)
                                       or not isinstance(params.get('text'), str)):
            raise ValueError(f'action {index}: fill requires string ref and text')
        validate_condition(params.get('verify'))
        actions.append(Action(type=item['type'], params=dict(params)))
    return actions


def _capture(fn):
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn()
        raw = out.getvalue() or err.getvalue()
        payload = json.loads(raw) if raw.strip() else {'ok': False, 'error': 'missing result'}
    except Exception as exc:
        rc, payload = 1, {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                          'dispatch_state': 'unknown', 'outcome': 'unknown',
                          'reason': 'outcome_unknown', 'retry_safe': False}
    if err.getvalue():
        print(err.getvalue(), end='', file=sys.stderr)
    return rc, payload


def execute(actions: list[Action], *, layer: str | None, observe_after: bool, compact: bool) -> int:
    from qcu import cli_handlers as h
    from qcu.session import load

    session = load()
    known = {r.get('ref') for r in (session.last_refs or [])} if session else set()
    for index, action in enumerate(actions):
        condition = action.params.get('verify')
        try:
            validate_condition(condition)
        except ValueError as exc:
            print(json.dumps({'ok': False, 'executed': 0, 'stopped_at': index,
                              'dispatch_state': 'not_sent', 'outcome': 'unknown',
                              'reason': 'invalid_verification', 'error': str(exc)}))
            return 2
        if condition and condition.get('ref') not in known and condition.get('ref') is not None:
            print(json.dumps({'ok': False, 'executed': 0, 'stopped_at': index,
                              'dispatch_state': 'not_sent', 'outcome': 'unknown',
                              'reason': 'unknown_verification_ref', 'error': 'observe again before batching'}))
            return 2
        for key in ('ref', 'ref_from', 'ref_to'):
            if key in action.params and action.params[key] not in known:
                print(json.dumps({'ok': False, 'executed': 0, 'stopped_at': index,
                                  'reason': 'unknown_ref', 'error': 'observe again before batching'}))
                return 2

    start = time.perf_counter()
    results: list[dict[str, Any]] = []
    rc = 0
    reason = None
    for index, action in enumerate(actions):
        # A stale DOM locator must never become a coordinate click in a batch.
        params = dict(action.params, strict_ref=True)
        rc, result = _capture(lambda: h.act(Action(action.type, params).to_json(), layer=layer))
        result.pop('router_decision', None)  # full decisions remain in telemetry
        results.append(dict(index=index, **result))
        data = result.get('data') or {}
        verification = data.get('verification') or {}
        # Explicit postconditions are authoritative. Old UI-change hints do
        # not negate a confirmed result, nor turn an unsent refusal into an
        # uncertain dispatch. Unknown acknowledgement still stops the group.
        dispatched = result.get('dispatch_state')
        outcome = result.get('outcome')
        uncertain = (dispatched == 'unknown' or
                     (dispatched != 'not_sent' and (
                         outcome == 'unknown' or
                         (outcome != 'verified' and (
                             result.get('reason', data.get('reason')) == 'outcome_unknown'
                             or data.get('no_effect') is True or data.get('effect_verified') is False
                             or verification.get('verified') in (False, 'no'))))))
        if rc or not result.get('ok') or uncertain:
            reason = 'outcome_unknown' if uncertain else 'action_failed'
            rc = rc or 2
            break

    payload: dict[str, Any] = dict(ok=rc == 0, executed=len(results), total=len(actions),
                                   results=results, elapsed_ms=round((time.perf_counter()-start)*1000, 2))
    if reason:
        payload.update(reason=reason, stopped_at=len(results)-1)
    if observe_after:
        obs_rc, obs = _capture(lambda: h.observe(layer=layer, max_depth=10, compact=compact))
        payload['observation'] = obs
        if obs_rc:
            payload.update(ok=False, observation_failed=True)
            rc = rc or obs_rc
    payload['elapsed_ms'] = round((time.perf_counter()-start)*1000, 2)
    print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2))
    return rc
