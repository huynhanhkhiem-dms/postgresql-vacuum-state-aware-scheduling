#!/usr/bin/env python3
"""Run all archived-data analyses, validation, and figure generation."""
from pathlib import Path
import subprocess, sys
root=Path(__file__).resolve().parent
analysis=root/'analysis'
commands=[
 [sys.executable,'analyze1.py'],
 [sys.executable,'analyze2.py'],
 [sys.executable,'analyze2.py','../data/exp2_k8.jsonl'],
 [sys.executable,'analyze3.py'],
 [sys.executable,'analyze5.py'],
 [sys.executable,'validate_headlines.py'],
 [sys.executable,'plots.py'],
]
for cmd in commands:
    print('+',' '.join(cmd), flush=True)
    subprocess.run(cmd,cwd=analysis,check=True)
print('All archived-data analyses completed successfully.')