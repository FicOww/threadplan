"""Preview one labelled v0.3 simulated input. No model or calendar calls."""

import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from threadplan import Intake


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',default='accept04')
    args=parser.parse_args()
    rows=map(json.loads,(ROOT/'evals/candidate_v03.inputs.jsonl').read_text(encoding='utf-8').splitlines())
    row=next((row for row in rows if row['case_id']==args.case),None)
    if row is None:raise ValueError('unknown_case')
    request=Intake(row['input'])
    print(json.dumps({'mode':'preview_only','source_category':row['source_category'],
                      'request':request.to_dict(),'external_calls':0},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
