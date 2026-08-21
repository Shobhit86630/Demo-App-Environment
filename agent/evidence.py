from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass
class Evidence:
    source: str
    category: str
    finding: str
    raw_data: str
    severity: str = "INFO"

    def to_dict(self):
        data = asdict(self)
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
        return data