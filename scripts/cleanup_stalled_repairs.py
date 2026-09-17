"""Run the bounded local-only stalled-repair cleanup policy."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowhub.db import Database
from flowhub.pipeline_modules.repair_cleanup import cleanup
if __name__=='__main__':
    print(json.dumps(cleanup(Database()),ensure_ascii=False))
