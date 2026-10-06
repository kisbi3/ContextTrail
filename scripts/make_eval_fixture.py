#!/usr/bin/env python3
"""Export a small, explicit session slice AFTER project-scope filtering. No AI calls.

Run from the installed source checkout:
  python scripts/make_eval_fixture.py /path/to/project --session ID --output /tmp/case.json
Review the exported records and add expectations before running contexttrail eval.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path

from contexttrail.evaluation import fixture_integrity
from contexttrail.git_context import Scope
from contexttrail.sources import collect_logs
from contexttrail.util import FlowError, dumps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--session', required=True)
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--claude-home', type=Path)
    parser.add_argument('--start', type=int, default=0, help='정규화 후 0-based session record 위치')
    parser.add_argument('--max-records', type=int, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or not 1 <= args.max_records <= 100:
        parser.error('--start >= 0, --max-records 1~100이 필요합니다.')
    scope = Scope.resolve(args.folder)
    snapshot = collect_logs(scope, codex_home=args.codex_home, claude_home=args.claude_home)
    selected = [r for r in snapshot.records if r.session_id == args.session]
    selected = selected[args.start:args.start + args.max_records]
    if not selected:
        raise FlowError('이 프로젝트 범위에서 해당 session 구간을 찾지 못했습니다.')
    if sum(len(r.content) for r in selected) > 120_000:
        raise FlowError('선택한 원문이 120,000 chars를 넘습니다. --max-records를 줄이세요.')
    integrity = fixture_integrity(selected)
    data = {'format':'contexttrail-eval-v1', 'name':'local-session-slice',
            'records':[{k:v for k,v in dataclasses.asdict(r).items() if k != 'pinned_hash'} for r in selected],
            'expectations':{}, 'notes':{'selected_scope':str(scope.folder), 'session':args.session,
            'slice_start':args.start, 'records':len(selected), 'limitations':snapshot.limitations,
            'integrity':integrity,
            'warning':'자동 절단한 작은 표본입니다. 앞뒤 맥락·call/result·fragment 완결성을 사람이 확인해야 합니다. '
                      '빈 expectations로는 의미 정확도를 채점하지 않습니다.'}}
    output = args.output.expanduser()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0)
    fd = os.open(output, flags, 0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        stream.write(dumps(data, pretty=True))
    print(f'{output}: {len(selected)} records; AI calls=0. 민감한 원문이 포함됩니다.')
    for message in integrity['limitations']:
        print('경고:', message)


if __name__ == '__main__':
    try:
        main()
    except (FlowError, OSError) as exc:
        raise SystemExit(str(exc))
