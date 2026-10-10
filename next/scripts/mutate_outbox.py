"""C06 copied-source semantic faults; dependency failures never count as kills."""
from pathlib import Path
import re
import sys

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT/'tests'))
from test_receipts import classify, report, suite


def mutation_status(result, expected):
    value = report(result)
    if value is not None and any(re.search(r'(?:^|\n)(?:ImportError|ModuleNotFoundError):', row['traceback'])
                                 for row in value['failures']):
        return 'inconclusive'
    return classify(result, expected)
