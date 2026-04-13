"""Load and validate per-game configs from config/games/*.json.

Each game config points at a contract address, its ABI file, a `start_block`,
the set of tokens the contract handles, a semantic-event mapping, and a
dictionary of known counterparty labels used to classify flows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from eth_utils import is_address, to_checksum_address
from pydantic import BaseModel, Field, field_validator


load_dotenv()


class TokenConfig(BaseModel):
    symbol: str
    decimals: int


class LabelConfig(BaseModel):
    category: str
    name: str


class EventMapping(BaseModel):
    wager: Optional[str] = None
    payout: Optional[str] = None


class GameConfig(BaseModel):
    name: str
    address: str
    abi_path: Optional[str] = None  # optional — unverified contracts don't have one
    start_block: int = 0
    tokens: Dict[str, TokenConfig] = Field(default_factory=dict)
    events: EventMapping = Field(default_factory=EventMapping)
    labels: Dict[str, LabelConfig] = Field(default_factory=dict)

    # Resolved at load time
    abi: List[dict] = Field(default_factory=list)

    @property
    def native_only(self) -> bool:
        """True when the only tracked token is native APE."""
        return list(self.tokens.keys()) == ["native"]

    @field_validator("address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        if v.startswith("0xFILL_ME"):
            return v  # allow placeholder so config can be committed
        if not is_address(v):
            raise ValueError(f"invalid game contract address: {v}")
        return to_checksum_address(v)

    @property
    def is_placeholder(self) -> bool:
        return self.address.startswith("0xFILL_ME")

    def erc20_tokens(self) -> List[str]:
        """Return ERC-20 token addresses (excludes 'native')."""
        return [addr for addr in self.tokens if addr != "native"]

    def normalized_labels(self) -> Dict[str, LabelConfig]:
        """Return labels keyed by lowercase address for case-insensitive lookup."""
        out: Dict[str, LabelConfig] = {}
        for addr, label in self.labels.items():
            if addr.startswith("0xFILL_ME"):
                continue  # skip placeholders
            if not is_address(addr):
                raise ValueError(f"invalid label address in {self.name}: {addr}")
            out[addr.lower()] = label
        return out


class Settings(BaseModel):
    rpc_url: str
    apescan_base_url: str
    apescan_api_key: str
    db_path: str
    config_dir: str
    indexer_window: int
    tail_interval: int


def load_settings() -> Settings:
    return Settings(
        rpc_url=os.environ.get("RPC_URL", "https://rpc.apechain.com"),
        apescan_base_url=os.environ.get(
            "APESCAN_BASE_URL", "https://api.etherscan.io/v2/api"
        ),
        apescan_api_key=os.environ.get("APESCAN_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "pnl.db"),
        config_dir=os.environ.get("CONFIG_DIR", "config/games"),
        indexer_window=int(os.environ.get("INDEXER_WINDOW", "2000")),
        tail_interval=int(os.environ.get("TAIL_INTERVAL", "10")),
    )


def load_game_config(name: str, config_dir: Optional[str] = None) -> GameConfig:
    settings = load_settings()
    cfg_dir = Path(config_dir or settings.config_dir)
    path = cfg_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"game config not found: {path}")
    raw = json.loads(path.read_text())
    # Drop any comment fields that start with '$'
    raw = {k: v for k, v in raw.items() if not k.startswith("$")}
    cfg = GameConfig.model_validate(raw)
    if cfg.abi_path:
        abi_path = Path(cfg.abi_path)
        if not abi_path.is_absolute():
            abi_path = Path.cwd() / abi_path
        if abi_path.exists():
            try:
                parsed = json.loads(abi_path.read_text())
                if isinstance(parsed, list):
                    cfg.abi = parsed
            except json.JSONDecodeError:
                pass
    return cfg


def load_all_game_configs(config_dir: Optional[str] = None) -> List[GameConfig]:
    settings = load_settings()
    cfg_dir = Path(config_dir or settings.config_dir)
    if not cfg_dir.exists():
        return []
    out: List[GameConfig] = []
    for path in sorted(cfg_dir.glob("*.json")):
        if path.name.endswith(".abi.json"):
            continue
        out.append(load_game_config(path.stem, config_dir=str(cfg_dir)))
    return out
