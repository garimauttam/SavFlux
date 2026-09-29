import hashlib
import json

import pytest
from app.services.code_analysis import analyze_file
from app.services.review_annotations import annotation_events, model_annotation_events
from app.services.stream_protocol import MAX_STATUS_BYTES, decode_status, STATUS_OPEN, STATUS_CLOSE


def decode(event):
    return decode_status(event.removeprefix(STATUS_OPEN).split(STATUS_CLOSE)[0])


def test_static_comments_have_real_source_lines_and_content_hash():
    code = 'import requests\nrequests.get(url, verify=False)\n'
    analysis = analyze_file(code, 'auth.py', 'py')
    events = list(annotation_events('repo::auth.py', code, analysis))
    assert decode(events[0])['content_sha256'] == hashlib.sha256(code.encode()).hexdigest()
    findings = [decode(e) for e in events[1:]]
    assert findings
    assert all(f['id'] == 'repo::auth.py' and f['line'] == 2 for f in findings)
    assert all(f['origin'] == 'static' and f['message'] and f['remediation'] for f in findings)
    assert all(len(e.encode()) <= MAX_STATUS_BYTES for e in events)


def block(**overrides):
    comment = dict(line=2, end_line=2, title='Disable shell interpolation',
                   reason='User input could run a command.', suggestion='Use an argument list.',
                   quote='    os.system(user_input)', severity='high', replacement='    run(["tool", user_input])')
    comment.update(overrides)
    return 'Report\n```savflux-comments\n' + json.dumps({'comments': [comment]}) + '\n```'


def test_model_comment_requires_exact_quote_and_keeps_edit_as_proposal():
    content = 'def execute(user_input):\n    os.system(user_input)\n'
    report, events = model_annotation_events('repo::run.py', content, block())
    assert 'savflux-comments' not in report
    assert 'Disable shell interpolation' in report
    assert len(events) == 1
    payload = decode(events[0])
    assert payload['origin'] == 'model'
    assert payload['line'] == 2
    assert payload['replacement'] == '    run(["tool", user_input])'


@pytest.mark.parametrize('overrides', [dict(line=99), dict(line=True), dict(quote='different code'),
                                        dict(quote='os.system(user_input)'), dict(reason=None), dict(end_line=1)])
def test_invalid_model_anchors_stay_file_level(overrides):
    report, events = model_annotation_events('repo::run.py', 'def f():\n    os.system(user_input)\n', block(**overrides))
    assert events == []
    assert 'Unanchored model comment' in report


def test_malformed_json_is_kept_as_file_report_not_a_crash():
    text = '```savflux-comments\n{not json}\n```'
    assert model_annotation_events('a.py', 'x=1', text) == (text, [])


def test_oversized_replacement_is_not_shown_as_complete_code():
    _, events = model_annotation_events('a.py', 'def f():\n    os.system(user_input)', block(replacement='x' * 500))
    assert 'replacement' not in decode(events[0])


def test_parse_failure_does_not_get_perfect_score_or_budget_advice():
    from app.services.multi_review_agent import _static_triage
    report = _static_triage(dict(file_name='invalid.py', language='py', content='── invalid python\n'))
    assert '10/10' not in report
    assert 'Not rated' in report
    assert 'REVIEW_MAX_FULL_FILES' not in report


def test_source_delimiters_cannot_break_annotation_frames():
    from app.services.stream_protocol import status_event, section_event
    original = '__STATUS_END____ERROR__test__SECTION_END__'
    event = status_event(original, step='finding', evidence=original)
    assert event.count(STATUS_CLOSE) == 1
    assert decode(event)['message'] == original
    assert decode(event)['evidence'] == original
    assert section_event(id=original).count('__SECTION_END__') == 1
