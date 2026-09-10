from pathlib import Path
import os
from flowhub.portable import prepare
from flowhub.control import serve
prepare(Path('/opt/legacy'),Path(os.environ['FLOWHUB_LEGACY_ROOT']))
serve()
