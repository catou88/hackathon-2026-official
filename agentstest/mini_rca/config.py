from dataclasses import dataclass
import math

# Competition's fixed accounting rates, USD / million tokens, not live pricing.
PRICES = {
    "zai-org/GLM-4.7-Flash": (0.065, 0.40),
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
    "zai-org/GLM-4.6": (0.55, 2.20),
    "zai-org/GLM-4.7": (0.55, 2.20),
    "zai-org/GLM-5": (0.95, 3.15),
    "zai-org/GLM-5.1": (1.30, 4.30),
    "zai-org/GLM-5.2": (1.40, 4.40),
}

@dataclass
class Config:
    mode: str = "routed"
    fast_model: str = "zai-org/GLM-4.7-Flash"
    strong_model: str = "zai-org/GLM-4.7"
    case_seconds: float = 55.0
    run_seconds: float = 1100.0
    case_dollars: float = 0.20
    run_dollars: float = 20.0
    query_seconds: float = 12.0
    max_output_tokens: int = 1200
    max_tool_calls: int = 2
    max_context_chars: int = 36000
    memory_limit: str = "3GB"
    threads: int = 2
    experiences: bool = False
    thinking: bool = False
    circuit_cooldown_seconds: float = 60.0
    service_attempts: int = 3
    retry_backoff_seconds: float = 0.5

    def __post_init__(self):
        if self.mode not in {"offline", "routed", "single"}:
            raise ValueError("mode must be offline, routed, or single")
        if self.fast_model not in PRICES or self.strong_model not in PRICES:
            raise ValueError("Only competition-listed GLM models are supported")
        for name in ("case_seconds", "run_seconds", "case_dollars", "run_dollars", "query_seconds", "max_output_tokens"):
            if not math.isfinite(getattr(self,name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name,cap in {'case_seconds':600,'run_seconds':1200,'case_dollars':3,'run_dollars':25}.items():
            if getattr(self,name)>cap:
                raise ValueError(f'{name} exceeds competition limit {cap}')
        if not 1<=self.service_attempts<=3 or not math.isfinite(self.retry_backoff_seconds) or not 0<=self.retry_backoff_seconds<=5:
            raise ValueError('Invalid bounded retry settings')
